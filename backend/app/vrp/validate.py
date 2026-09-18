"""
The shared solution validator.

Every solver's output goes through :func:`validate_routes`, so "feasible"
means the same thing whichever solver produced the routes. It checks:

* every customer is visited exactly once, and nothing else is visited;
* no more routes than vehicles, and no empty routes;
* each route's total demand fits in one vehicle;
* each service starts inside its customer's time window, with waiting when a
  vehicle arrives early, and every vehicle is back by the depot's due time.

The schedule it computes is the earliest-start one: leave the depot at its
ready time, start each service at ``max(arrival, ready)``. That schedule is
feasible whenever any schedule for the same visit order is, so checking it is
enough.
"""

from __future__ import annotations

import numbers
from typing import Dict, List, Optional, Sequence

from app.vrp.model import (
    STATUS_FEASIBLE,
    STATUS_INFEASIBLE,
    STATUS_NO_SOLUTION,
    STATUS_OPTIMAL,
    RouteReport,
    ValidationReport,
    VrpInstance,
    VrpSolution,
)


def schedule_route(instance: VrpInstance, route: Sequence[int]) -> tuple[RouteReport, List[str]]:
    """Earliest-start schedule for one route, plus any capacity / time violations."""
    dist = instance.distance
    tt = instance.travel_time
    assert tt is not None
    nodes = instance.nodes
    violations: List[str] = []

    depart = nodes[0].ready
    t = depart
    prev = 0
    load = 0
    distance = 0
    starts: List[int] = []
    for c in route:
        distance += dist[prev][c]
        arrival = t + tt[prev][c]
        start = max(arrival, nodes[c].ready)
        if start > nodes[c].due:
            violations.append(f"customer {c}: service starts at {start}, after its due time {nodes[c].due}")
        starts.append(start)
        load += nodes[c].demand
        t = start + nodes[c].service_time
        prev = c
    distance += dist[prev][0]
    return_time = t + tt[prev][0]
    if return_time > nodes[0].due:
        violations.append(f"route {list(route)}: returns to the depot at {return_time}, after {nodes[0].due}")
    if load > instance.vehicle_capacity:
        violations.append(f"route {list(route)}: load {load} exceeds capacity {instance.vehicle_capacity}")

    report = RouteReport(
        customers=list(route),
        load=load,
        distance=distance,
        service_starts=starts,
        depart=depart,
        return_time=return_time,
    )
    return report, violations


def validate_routes(instance: VrpInstance, routes: Sequence[Sequence[int]]) -> ValidationReport:
    """Check ``routes`` against every constraint of ``instance``."""
    violations: List[str] = []
    n = len(instance.nodes)

    seen: Dict[int, int] = {}
    for r in routes:
        if not r:
            violations.append("empty route (a vehicle that leaves and returns without visiting anyone)")
        for c in r:
            if not isinstance(c, numbers.Integral) or c <= 0 or c >= n:
                violations.append(f"route {list(r)}: node {c} is not a customer")
                continue
            seen[c] = seen.get(c, 0) + 1
    for c in instance.customers:
        count = seen.get(c, 0)
        if count == 0:
            violations.append(f"customer {c} is not visited")
        elif count > 1:
            violations.append(f"customer {c} is visited {count} times")
    if len(routes) > instance.num_vehicles:
        violations.append(f"{len(routes)} routes but only {instance.num_vehicles} vehicles")

    reports: List[RouteReport] = []
    for r in routes:
        if any(not isinstance(c, numbers.Integral) or c <= 0 or c >= n for c in r):
            continue  # already reported; cannot be scheduled
        report, route_violations = schedule_route(instance, r)
        reports.append(report)
        violations.extend(route_violations)

    return ValidationReport(
        feasible=not violations,
        violations=violations,
        total_cost=sum(rep.distance for rep in reports),
        routes=reports,
    )


def build_solution(
    instance: VrpInstance,
    method: str,
    routes: Optional[Sequence[Sequence[int]]],
    wall_seconds: float,
    proven_optimal: bool = False,
    stats: Optional[dict] = None,
) -> VrpSolution:
    """Validate a solver's raw routes and wrap them as a :class:`VrpSolution`.

    ``routes=None`` means the solver found nothing. Empty routes are dropped
    before validation (an unused vehicle is not a violation).
    """
    if routes is None:
        empty = validate_routes(instance, [])
        return VrpSolution(
            method=method,
            status=STATUS_NO_SOLUTION,
            routes=[],
            total_cost=0,
            proven_optimal=False,
            wall_seconds=wall_seconds,
            validation=empty,
            stats=stats or {},
        )
    kept = [list(r) for r in routes if r]
    report = validate_routes(instance, kept)
    if not report.feasible:
        status = STATUS_INFEASIBLE
    elif proven_optimal:
        status = STATUS_OPTIMAL
    else:
        status = STATUS_FEASIBLE
    return VrpSolution(
        method=method,
        status=status,
        routes=kept,
        total_cost=report.total_cost,
        proven_optimal=proven_optimal and report.feasible,
        wall_seconds=wall_seconds,
        validation=report,
        stats=stats or {},
    )
