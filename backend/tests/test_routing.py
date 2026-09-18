"""Unit tests for the TSP routing solver."""
from app.optimization.routing import (
    GeoPoint, RoutingNode, solve_pickup_tsp, route_total_distance_km,
)


def test_tsp_single_distributor_returns_single_id():
    depot = GeoPoint(lat=34.0, lng=-82.0)
    nodes = [RoutingNode(id=7, lat=35.0, lng=-83.0, name="d7")]
    order = solve_pickup_tsp(depot, nodes)
    assert order == [7]


def test_tsp_orders_distributors_greedy_on_east_coast():
    # Depot Greenville SC; three distributors roughly collinear north
    depot = GeoPoint(lat=34.8526, lng=-82.3940)
    nodes = [
        RoutingNode(id=10, lat=38.0, lng=-82.0, name="far"),
        RoutingNode(id=20, lat=35.5, lng=-82.0, name="near"),
        RoutingNode(id=30, lat=36.5, lng=-82.0, name="mid"),
    ]
    order = solve_pickup_tsp(depot, nodes)
    # Should visit in geographic order near → mid → far (or reverse)
    assert set(order) == {10, 20, 30}
    assert len(order) == 3
    # Nearest should be first
    assert order[0] == 20


def test_tsp_empty_returns_empty():
    assert solve_pickup_tsp(GeoPoint(0, 0), []) == []


def test_total_distance_closed_tour():
    depot = GeoPoint(0.0, 0.0)
    nodes = [
        RoutingNode(id=1, lat=0.0, lng=1.0, name="a"),
        RoutingNode(id=2, lat=0.0, lng=2.0, name="b"),
    ]
    # Tour: (0,0) → (0,1) → (0,2) → (0,0)
    # Each degree ≈ 111 km at equator; total ≈ 4*111 = 444 km
    d = route_total_distance_km(depot, nodes)
    assert 400 < d < 500


# ── The exact path ───────────────────────────────────────────────────────────
#
# WHY THESE EXIST: `POST /optimize/vrp` took ~10 s on the live site, and ~87% of
# that was GUIDED_LOCAL_SEARCH burning its full 3-second limit once per strategy
# — on tours of 1 and 3 stops. GLS has no convergence criterion, so it always
# spends the whole budget no matter how trivial the instance. A 3-stop tour has
# 3 distinct orderings; enumerating them is microseconds and PROVES optimality,
# which a metaheuristic cannot do at any budget.
#
# Every test below fails against the pre-fix routing.py: the module had no
# `solve_pickup_tsp_detailed`, no method/`proven_optimal` provenance, and a
# 3-stop solve took 3.0 s.

import itertools
import time

import pytest

from app.optimization import routing as routing_mod
from app.optimization.routing import (
    EXACT_MAX_STOPS, METHOD_EXACT, METHOD_GUIDED_LOCAL_SEARCH, METHOD_NONE,
    solve_pickup_tsp_detailed,
)


DEPOT_SC = GeoPoint(lat=34.8526, lng=-82.3940)  # Greenville SC


def _spread_nodes(n: int, seed: int) -> list:
    """`n` distributors scattered over the continental US, deterministically."""
    import random
    r = random.Random(seed)
    return [
        RoutingNode(id=i + 1, lat=25.0 + r.random() * 23.0,
                    lng=-124.0 + r.random() * 55.0, name=f"d{i + 1}")
        for i in range(n)
    ]


def _tour_km(depot: GeoPoint, nodes: list, order: list) -> float:
    by_id = {n.id: n for n in nodes}
    return route_total_distance_km(depot, [by_id[i] for i in order])


def test_small_tour_is_solved_exactly_and_reports_that_it_was():
    """The response has to be able to say WHICH method ran and whether the tour
    is proven — the whole standard here is that a claim traces to a field."""
    nodes = _spread_nodes(3, seed=11)
    sol = solve_pickup_tsp_detailed(DEPOT_SC, nodes)

    assert sol.method == METHOD_EXACT
    assert sol.proven_optimal is True
    assert sol.stop_count == 3
    assert sol.tours_enumerated == 3          # 3! / 2 (reversal symmetry)
    assert sol.time_limit_seconds is None     # nothing to time-limit
    assert sorted(sol.order) == [1, 2, 3]


def test_a_three_stop_tour_no_longer_costs_three_seconds():
    """The measured defect: 3 stops took 3.010 s because GLS always spends its
    entire budget. Enumerating 3 tours is microseconds."""
    nodes = _spread_nodes(3, seed=12)
    t0 = time.perf_counter()
    solve_pickup_tsp_detailed(DEPOT_SC, nodes)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25, f"3-stop solve took {elapsed:.3f}s"


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8])
def test_exact_tour_is_optimal_against_full_enumeration(n):
    """No ordering of the same stops may be shorter than the one returned.
    Enumerated here in km with the shipped `route_total_distance_km`, which is
    NOT the integer-metre matrix the solver minimises — so this also pins that
    the rounding to metres cannot flip which tour wins."""
    nodes = _spread_nodes(n, seed=100 + n)
    sol = solve_pickup_tsp_detailed(DEPOT_SC, nodes)
    assert sol.proven_optimal is True

    best = min(_tour_km(DEPOT_SC, nodes, list(p))
               for p in itertools.permutations([x.id for x in nodes]))
    assert _tour_km(DEPOT_SC, nodes, sol.order) == pytest.approx(best, abs=1e-6)


@pytest.mark.parametrize("n", [4, 6, 8])
def test_exact_tour_is_never_worse_than_the_metaheuristic(n, monkeypatch):
    """An exact solver may only tie or beat the heuristic it replaces. If it ever
    BEAT it on a live-shaped instance, the routes the site published were
    suboptimal — so this comparison is the thing that would catch that."""
    nodes = _spread_nodes(n, seed=200 + n)
    exact = solve_pickup_tsp_detailed(DEPOT_SC, nodes)

    # Force the old path on the same instance by moving the threshold below it.
    monkeypatch.setattr(routing_mod, "EXACT_MAX_STOPS", 0)
    heuristic = solve_pickup_tsp_detailed(DEPOT_SC, nodes)
    assert heuristic.method == METHOD_GUIDED_LOCAL_SEARCH
    assert heuristic.proven_optimal is False

    exact_km = _tour_km(DEPOT_SC, nodes, exact.order)
    heur_km = _tour_km(DEPOT_SC, nodes, heuristic.order)
    assert exact_km <= heur_km + 1e-6, (
        f"n={n}: exact tour {exact_km:.3f} km is LONGER than the heuristic's "
        f"{heur_km:.3f} km — the exact path is wrong"
    )


def test_worst_case_of_the_exact_path_stays_inside_its_stated_bound():
    """Render runs ONE uvicorn worker on 0.5 CPU, so this loop blocks the whole
    API while it runs. At the threshold it is 8! / 2 = 20,160 tours, measured at
    ~7 ms on the dev machine; the assertion leaves two orders of magnitude of
    slack for a slower box but still fails if the threshold is ever raised to a
    size that would block the worker for seconds."""
    assert EXACT_MAX_STOPS == 8
    nodes = _spread_nodes(EXACT_MAX_STOPS, seed=8)
    t0 = time.perf_counter()
    sol = solve_pickup_tsp_detailed(DEPOT_SC, nodes)
    elapsed = time.perf_counter() - t0

    assert sol.tours_enumerated == 20_160
    assert elapsed < 1.0, f"worst-case exact solve took {elapsed:.3f}s"


def test_above_the_threshold_the_tour_is_not_claimed_to_be_optimal():
    """9 stops is out of exhaustive reach, so GLS runs — and must NOT come back
    claiming a certificate it does not have."""
    nodes = _spread_nodes(EXACT_MAX_STOPS + 1, seed=9)
    sol = solve_pickup_tsp_detailed(DEPOT_SC, nodes)

    assert sol.method == METHOD_GUIDED_LOCAL_SEARCH
    assert sol.proven_optimal is False
    assert sol.tours_enumerated == 0
    assert sol.time_limit_seconds == 1.0
    assert len(sol.order) == 9


def test_metaheuristic_budget_matches_what_convergence_was_measured_at():
    """Measured on random continental-US instances against a 5 s reference: at 9,
    12, 16 and 25 stops every limit from 250 ms up found the SAME tour, so 3 s
    bought nothing. At 40 and 60 stops the short limits lost up to 2.8%, so the
    long budget survives there and only there."""
    assert routing_mod._metaheuristic_time_limit(9) == 1
    assert routing_mod._metaheuristic_time_limit(25) == 1
    assert routing_mod._metaheuristic_time_limit(26) == 3


def test_empty_and_single_stop_report_their_method_too():
    empty = solve_pickup_tsp_detailed(GeoPoint(0.0, 0.0), [])
    assert empty.order == [] and empty.method == METHOD_NONE
    # Nothing was solved, so nothing may be badged as proven — an all-import
    # plan (no domestic stops) lands here.
    assert empty.proven_optimal is False

    one = solve_pickup_tsp_detailed(DEPOT_SC, _spread_nodes(1, seed=1))
    assert one.order == [1]
    assert one.method == METHOD_EXACT and one.proven_optimal is True


def test_bare_solve_pickup_tsp_still_returns_just_the_order():
    """The thin wrapper the rest of the code used before this change."""
    nodes = _spread_nodes(4, seed=44)
    assert solve_pickup_tsp(DEPOT_SC, nodes) == solve_pickup_tsp_detailed(DEPOT_SC, nodes).order


