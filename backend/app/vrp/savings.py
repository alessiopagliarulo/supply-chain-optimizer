"""
Clarke-Wright savings heuristic with time windows, for larger instances.

THE ALGORITHM (parallel savings, Clarke & Wright 1964). Start with one route
per customer, ``0 -> i -> 0``. Joining the end of the route that finishes at
``i`` to the start of the route that begins at ``j`` saves

    s(i, j) = d(i, 0) + d(0, j) - d(i, j)

of distance. Take the ordered pairs by decreasing saving and make each merge
that keeps the joined route feasible: ``i`` must be the LAST customer of its
route, ``j`` the FIRST customer of a different route, the joined load must fit
in one vehicle, and the joined route's earliest-start schedule must meet every
time window and the depot's due time.

Routes are never reversed to make a merge fit. With time windows a route is
directional, so reversing one can break it; instead both orders ``(i, j)`` and
``(j, i)`` are candidate savings in their own right, which is what the
asymmetric savings list above already covers. It also keeps the heuristic
correct when distance is not symmetric.

WHAT IT DOES NOT GUARANTEE. The heuristic minimises distance greedily; it does
not target a fleet size. If the merges it can make still leave more routes
than ``num_vehicles`` (or a single-customer route is already infeasible), the
routes are returned anyway and the shared validator marks the solution
``infeasible`` with the reason. It is deterministic: ties are broken by node
index.
"""

from __future__ import annotations

import time
from typing import Dict, List

from app.vrp.model import VrpInstance, VrpSolution
from app.vrp.validate import build_solution, schedule_route

METHOD = "clarke_wright"


def savings_routes(instance: VrpInstance) -> List[List[int]]:
    """The Clarke-Wright routes for ``instance`` (not yet validated)."""
    d = instance.distance
    cap = instance.vehicle_capacity
    customers = list(instance.customers)

    route_of: Dict[int, int] = {c: k for k, c in enumerate(customers)}
    routes: Dict[int, List[int]] = {k: [c] for k, c in enumerate(customers)}
    loads: Dict[int, int] = {k: instance.nodes[c].demand for k, c in enumerate(customers)}

    savings = sorted(
        ((d[i][0] + d[0][j] - d[i][j], i, j) for i in customers for j in customers if i != j),
        key=lambda s: (-s[0], s[1], s[2]),
    )
    for saving, i, j in savings:
        if saving < 0:
            break
        a, b = route_of[i], route_of[j]
        if a == b:
            continue
        ra, rb = routes[a], routes[b]
        if ra[-1] != i or rb[0] != j:
            continue
        if loads[a] + loads[b] > cap:
            continue
        merged = ra + rb
        _, violations = schedule_route(instance, merged)
        if violations:
            continue
        routes[a] = merged
        loads[a] += loads[b]
        for c in rb:
            route_of[c] = a
        del routes[b], loads[b]

    return [routes[k] for k in sorted(routes)]


def solve_clarke_wright(instance: VrpInstance) -> VrpSolution:
    """Run the savings heuristic and validate the result."""
    t0 = time.perf_counter()
    routes = savings_routes(instance)
    return build_solution(instance, METHOD, routes, time.perf_counter() - t0)
