"""
Pickup TSP over a set of distributor stops.

WHAT THIS IS, EXACTLY: a single-vehicle, uncapacitated, SYMMETRIC
Travelling Salesman Problem. One vehicle leaves the depot, visits every
selected distributor exactly once, and returns. There is no capacity
dimension, no time window, no demand dimension and no second vehicle — so
this is a TSP, not a VRP.

The matrix is great-circle (haversine) distance rounded to integer metres,
so d(i, j) == d(j, i) by construction. That makes it a SYMMETRIC TSP.
Nothing here is asymmetric — asymmetry would need directional costs
(one-way streets, road distances, traffic), which this model does not have.

SOLVER — two paths, and the response says which one ran:

1. ``stops <= EXACT_MAX_STOPS`` (8): EXHAUSTIVE ENUMERATION. Every distinct
   tour is evaluated on the same integer-metre matrix and the cheapest is
   returned, so the answer is a PROVEN optimum, not a good local one. With the
   depot pinned at both ends and reversal symmetry folded away there are
   ``n! / 2`` distinct tours — 20,160 at 8 stops, measured at 7 ms on the dev
   machine (9 stops is 66 ms, 10 stops 0.70 s, which is why the cut is at 8:
   Render runs ONE uvicorn worker on 0.5 CPU, so this loop blocks the whole
   API while it runs and its worst case has to stay in the tens of ms).

2. ``stops > EXACT_MAX_STOPS``: OR-Tools routing (``pywrapcp.RoutingModel``),
   PATH_CHEAPEST_ARC first solution, GUIDED_LOCAL_SEARCH metaheuristic. The
   tour is a good local optimum and is NOT certified; ``proven_optimal`` is
   False and stays False.

If the metaheuristic returns no solution at all, ``solve_pickup_tsp`` falls
back to a greedy nearest-neighbour tour so the caller always gets a usable
order.

WHY THE EXACT PATH EXISTS AT ALL: GUIDED_LOCAL_SEARCH has no convergence
criterion — it always spends its entire time budget (measured: wall time
equals the limit at 9, 12, 20 and 40 stops, from 100 ms up to 10 s). The live
site was paying 3 s per strategy, three times per request, to heuristically
order tours of 1 and 3 stops. A 3-stop tour has 3 distinct orderings.

NOT BUILT: OSRM (or any other) road driving distances in the optimizer. The
map page fetches OSRM geometry purely to DRAW a road-shaped polyline; the
optimizer never sees a road distance. Every kilometre, dollar, day and kg of
CO2 in this pipeline is derived from great-circle distance.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from app.optimization.costs import haversine_km


# ── Method names carried back to the API response ────────────────────────────
METHOD_NONE = "no_stops"
METHOD_EXACT = "exact_enumeration"
METHOD_GUIDED_LOCAL_SEARCH = "guided_local_search"
METHOD_GREEDY_FALLBACK = "greedy_nearest_neighbour"

# Above this many stops the exhaustive path is abandoned for the metaheuristic.
# 8 stops = 20,160 distinct tours = 7 ms measured; 9 = 66 ms; 10 = 0.70 s.
EXACT_MAX_STOPS = 8

# Time limits for the metaheuristic path, in seconds. Measured on random
# continental-US instances, comparing the objective at 250 ms / 500 ms / 1 s
# against a 5 s reference: at 9, 12, 16 and 25 stops every limit found the SAME
# tour as the 5 s run, so 3 s bought nothing. At 40 and 60 stops the short
# limits lost up to 2.8%, so the long budget is kept there and only there.
CONVERGED_TIME_LIMIT_SECONDS = 1
LONG_TIME_LIMIT_SECONDS = 3
CONVERGED_MAX_STOPS = 25


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lng: float


@dataclass(frozen=True)
class RoutingNode:
    """A single location in the pickup route (distributor or depot)."""
    id: int  # distributor_id, or -1 for depot
    lat: float
    lng: float
    name: str


@dataclass(frozen=True)
class TspSolution:
    """The tour, plus what actually produced it.

    ``proven_optimal`` is True ONLY when every distinct tour was evaluated. The
    metaheuristic never sets it — a local optimum is not a certificate, and the
    UI must not be able to call one an optimum.
    """
    order: List[int] = field(default_factory=list)   # distributor ids, visit order
    method: str = METHOD_NONE
    proven_optimal: bool = False
    stop_count: int = 0
    tours_enumerated: int = 0            # 0 unless the exhaustive path ran
    time_limit_seconds: Optional[float] = None       # None unless the solver ran
    note: str = ""


def _nearest_neighbor_order(nodes: List[RoutingNode]) -> List[int]:
    """Greedy fallback ordering starting at index 0 (depot)."""
    n = len(nodes)
    visited = {0}
    order = [0]
    current = 0
    while len(visited) < n:
        best = None
        best_d = float("inf")
        for j in range(n):
            if j in visited:
                continue
            d = haversine_km(nodes[current].lat, nodes[current].lng,
                             nodes[j].lat, nodes[j].lng)
            if d < best_d:
                best_d = d
                best = j
        order.append(best)
        visited.add(best)
        current = best
    return order


def _distance_matrix_m(nodes: Sequence[RoutingNode]) -> List[List[int]]:
    """Symmetric haversine matrix in integer metres; index 0 is the depot."""
    n = len(nodes)
    matrix = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = int(round(
                haversine_km(nodes[i].lat, nodes[i].lng,
                             nodes[j].lat, nodes[j].lng) * 1000
            ))
            matrix[i][j] = d
            matrix[j][i] = d
    return matrix


def _tour_length_m(matrix: List[List[int]], perm: Sequence[int]) -> int:
    """Closed-tour length depot(0) → perm → depot(0), in metres."""
    total = matrix[0][perm[0]]
    for a, b in zip(perm, perm[1:], strict=False):
        total += matrix[a][b]
    return total + matrix[perm[-1]][0]


def _exact_tour(matrix: List[List[int]], n_stops: int) -> Tuple[List[int], int]:
    """Exhaustively enumerate every distinct tour; return (best order, tours seen).

    The order returned is a list of MATRIX indices (1..n_stops), depot excluded.

    Reversal symmetry: on a symmetric matrix a tour and its reverse cost exactly
    the same, so only the half with ``perm[0] < perm[-1]`` is evaluated — n!/2
    tours instead of n!. Ties keep the lexicographically first permutation, which
    makes the result deterministic across runs and machines.

    Because the two directions cost the same to the last metre, the winner is then
    oriented to leave the depot toward whichever end stop is closer. That is a
    presentation choice with zero effect on tour length — it just stops the tour
    from being drawn back-to-front on the map.

    WORST CASE at EXACT_MAX_STOPS = 8: 20,160 tours × 9 additions. Measured at
    7 ms on the dev machine. This is a synchronous CPU-bound loop inside a single
    uvicorn worker, so that bound is the whole API's blocking time.
    """
    stops = range(1, n_stops + 1)
    best_order: Tuple[int, ...] = tuple(stops)
    best_cost: Optional[int] = None
    seen = 0
    for perm in itertools.permutations(stops):
        if n_stops > 1 and perm[0] > perm[-1]:
            continue  # mirror image of a tour already evaluated
        seen += 1
        cost = _tour_length_m(matrix, perm)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_order = perm
    if matrix[0][best_order[-1]] < matrix[0][best_order[0]]:
        best_order = tuple(reversed(best_order))
    return list(best_order), seen


def _metaheuristic_time_limit(n_stops: int) -> int:
    """The measured limit for this instance size (see the constants above)."""
    if n_stops <= CONVERGED_MAX_STOPS:
        return CONVERGED_TIME_LIMIT_SECONDS
    return LONG_TIME_LIMIT_SECONDS


def solve_pickup_tsp_detailed(
    depot: GeoPoint,
    distributor_nodes: List[RoutingNode],
    time_limit_seconds: Optional[int] = None,
) -> TspSolution:
    """Solve the pickup tour and report HOW it was solved.

    Returns a :class:`TspSolution` whose ``order`` is distributor ids in visit
    order, depot excluded. Small instances (``<= EXACT_MAX_STOPS``) are solved by
    exhaustive enumeration and come back with ``proven_optimal=True``; larger
    ones run GUIDED_LOCAL_SEARCH and come back with ``proven_optimal=False``.

    ``time_limit_seconds`` overrides the measured default for the metaheuristic
    path only. It has no effect on the exact path, which has no time budget to
    spend — it enumerates and stops.
    """
    n_stops = len(distributor_nodes)
    if n_stops == 0:
        # `proven_optimal` stays False here deliberately. No tour was solved,
        # so there is no optimum to certify — and a UI that badges
        # `proven_optimal` must not badge a plan with no truck tour at all
        # (an all-international plan reaches this branch).
        return TspSolution(
            order=[], method=METHOD_NONE, proven_optimal=False, stop_count=0,
            note=(
                "No domestic pickup stops, so no tour was solved. Nothing here "
                "is optimal or suboptimal — there is no tour to be either."
            ),
        )

    depot_node = RoutingNode(id=-1, lat=depot.lat, lng=depot.lng, name="depot")
    nodes = [depot_node] + list(distributor_nodes)
    matrix = _distance_matrix_m(nodes)

    # ── Exact path: enumerate every tour, return a certified optimum ─────────
    if n_stops <= EXACT_MAX_STOPS:
        order_idx, tours = _exact_tour(matrix, n_stops)
        return TspSolution(
            order=[nodes[i].id for i in order_idx],
            method=METHOD_EXACT,
            proven_optimal=True,
            stop_count=n_stops,
            tours_enumerated=tours,
            time_limit_seconds=None,
            note=(
                f"Exhaustive enumeration of all {tours:,} distinct tours over "
                f"{n_stops} stop(s) on the integer-metre haversine matrix. "
                f"The tour returned is a proven optimum."
            ),
        )

    # ── Metaheuristic path: good, but not certified ──────────────────────────
    # Deferred import: the OR-Tools routing solver costs ~360 ms to import and is
    # needed only on this branch, not at boot and not for the exact path above.
    # Keeping it here takes OR-Tools off the `import app.main` path.
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    limit = _metaheuristic_time_limit(n_stops) if time_limit_seconds is None else time_limit_seconds
    n = len(nodes)

    manager = pywrapcp.RoutingIndexManager(n, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(from_idx, to_idx):
        return matrix[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    transit_cb_idx = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb_idx)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    # GUIDED_LOCAL_SEARCH has NO convergence criterion: it restarts from its best
    # solution until the clock runs out, so this limit is not an upper bound the
    # search finishes early on — it is exactly what every call above the exact
    # threshold will cost. Measured, at every size tried: wall time == limit.
    params.time_limit.seconds = limit

    solution = routing.SolveWithParameters(params)
    if not solution:
        greedy = _nearest_neighbor_order(nodes)
        return TspSolution(
            order=[nodes[i].id for i in greedy if i != 0],
            method=METHOD_GREEDY_FALLBACK,
            proven_optimal=False,
            stop_count=n_stops,
            time_limit_seconds=float(limit),
            note=(
                "The routing solver returned no solution within its time limit; "
                "this is the greedy nearest-neighbour fallback order, which is "
                "feasible but carries no quality guarantee."
            ),
        )

    order_ids: List[int] = []
    idx = routing.Start(0)
    while not routing.IsEnd(idx):
        node_idx = manager.IndexToNode(idx)
        if node_idx != 0:
            order_ids.append(nodes[node_idx].id)
        idx = solution.Value(routing.NextVar(idx))
    return TspSolution(
        order=order_ids,
        method=METHOD_GUIDED_LOCAL_SEARCH,
        proven_optimal=False,
        stop_count=n_stops,
        time_limit_seconds=float(limit),
        note=(
            f"{n_stops} stops is above the {EXACT_MAX_STOPS}-stop exhaustive limit, so "
            f"GUIDED_LOCAL_SEARCH ran for its full {limit}s budget. The tour is a good "
            f"local optimum; it is NOT proven optimal."
        ),
    )


def solve_pickup_tsp(
    depot: GeoPoint,
    distributor_nodes: List[RoutingNode],
    time_limit_seconds: Optional[int] = None,
) -> List[int]:
    """
    Return an ordered list of distributor_ids representing the pickup route.

    Thin wrapper over :func:`solve_pickup_tsp_detailed` for callers that only
    want the order. One vehicle, no capacity constraint, symmetric haversine
    costs; the tour starts and ends at the depot and the returned list EXCLUDES
    the depot.
    """
    return solve_pickup_tsp_detailed(depot, distributor_nodes, time_limit_seconds).order


def route_total_distance_km(
    depot: GeoPoint,
    ordered_nodes: List[RoutingNode],
) -> float:
    """Haversine distance of the closed tour depot → n1 → n2 → ... → depot."""
    if not ordered_nodes:
        return 0.0
    total = haversine_km(depot.lat, depot.lng,
                         ordered_nodes[0].lat, ordered_nodes[0].lng)
    for i in range(len(ordered_nodes) - 1):
        total += haversine_km(
            ordered_nodes[i].lat, ordered_nodes[i].lng,
            ordered_nodes[i + 1].lat, ordered_nodes[i + 1].lng,
        )
    total += haversine_km(
        ordered_nodes[-1].lat, ordered_nodes[-1].lng,
        depot.lat, depot.lng,
    )
    return total
