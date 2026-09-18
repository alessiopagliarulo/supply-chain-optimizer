"""
General-purpose CVRPTW path on the OR-Tools routing library.

``pywrapcp.RoutingModel`` with one vehicle per fleet slot, arc cost = distance,
and two dimensions:

* ``Capacity`` - cumulative demand, capped at the vehicle capacity;
* ``Time`` - cumulative service-start time. The transit of arc ``i -> j`` is
  ``service_time[i] + travel_time[i][j]``; slack up to the horizon lets a
  vehicle wait for a window to open. Each customer's cumul is restricted to
  its ``[ready, due]`` window, each vehicle's start and end to the depot's.

That is the same schedule semantics the shared validator checks, so a
solution this model accepts is one the validator accepts too.

SEARCH. ``PATH_CHEAPEST_ARC`` builds a first solution and
``GUIDED_LOCAL_SEARCH`` improves it. GLS has no convergence criterion - it
always spends the whole ``time_limit_seconds`` - so the limit is the knob that
trades quality for latency. The result is a good local optimum, never a proof:
``proven_optimal`` is always False here. Every customer must be served (no
disjunctions), so when the search finds nothing the solution is
``no_solution``.
"""

from __future__ import annotations

import time
from typing import List

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.vrp.model import VrpInstance, VrpSolution
from app.vrp.validate import build_solution

METHOD = "ortools"


def solve_ortools(instance: VrpInstance, time_limit_seconds: float = 5.0, random_seed: int = 42) -> VrpSolution:
    """Solve ``instance`` with the OR-Tools routing library."""
    t0 = time.perf_counter()
    if instance.num_customers == 0:
        return build_solution(instance, METHOD, [], time.perf_counter() - t0)

    nodes = instance.nodes
    dist = instance.distance
    tt = instance.travel_time
    assert tt is not None
    depot = nodes[0]
    n = len(nodes)
    vehicles = instance.num_vehicles

    manager = pywrapcp.RoutingIndexManager(n, vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(from_index: int, to_index: int) -> int:
        return dist[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    def demand_cb(from_index: int) -> int:
        return nodes[manager.IndexToNode(from_index)].demand

    def time_cb(from_index: int, to_index: int) -> int:
        i = manager.IndexToNode(from_index)
        return nodes[i].service_time + tt[i][manager.IndexToNode(to_index)]

    dist_idx = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(dist_idx)

    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(demand_idx, 0, [instance.vehicle_capacity] * vehicles, True, "Capacity")

    time_idx = routing.RegisterTransitCallback(time_cb)
    # Cumuls above the depot's due time are never feasible (the vehicle still has to
    # get home), but a customer's own due time may be later; size the dimension to
    # hold both so its window can be set as given.
    horizon = max(node.due for node in nodes)
    routing.AddDimension(time_idx, horizon, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    for c in instance.customers:
        time_dim.CumulVar(manager.NodeToIndex(c)).SetRange(nodes[c].ready, nodes[c].due)
    for v in range(vehicles):
        time_dim.CumulVar(routing.Start(v)).SetRange(depot.ready, depot.due)
        time_dim.CumulVar(routing.End(v)).SetRange(depot.ready, depot.due)
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(routing.Start(v)))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(routing.End(v)))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(max(1, int(time_limit_seconds * 1000)))
    # Note: OR-Tools routing doesn't expose random_seed in RoutingSearchParameters;
    # seeding is controlled through C++ level; use fixed time limit for reproducibility.

    assignment = routing.SolveWithParameters(params)
    wall = time.perf_counter() - t0
    stats: dict = {"routing_status": int(routing.status())}
    if assignment is None:
        return build_solution(instance, METHOD, None, wall, stats=stats)

    routes: List[List[int]] = []
    for v in range(vehicles):
        index = assignment.Value(routing.NextVar(routing.Start(v)))
        route = []
        while not routing.IsEnd(index):
            route.append(manager.IndexToNode(index))
            index = assignment.Value(routing.NextVar(index))
        if route:
            routes.append(route)
    stats["objective"] = int(assignment.ObjectiveValue())
    return build_solution(instance, METHOD, routes, wall, stats=stats)
