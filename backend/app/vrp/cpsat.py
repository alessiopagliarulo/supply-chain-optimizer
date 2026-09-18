"""
Exact CVRPTW model on OR-Tools CP-SAT, for small instances.

FORMULATION. One Boolean per arc ``x[i, j]``. ``AddMultipleCircuit`` makes the
chosen arcs a set of depot-rooted cycles covering every customer exactly once
(node 0 may be entered and left several times; every other node exactly once),
which is exactly a set of vehicle routes. The number of arcs leaving the depot
is the number of vehicles used, capped at ``num_vehicles``.

Capacity and time are carried along arcs with integer node variables, only
enforced on the arcs that are used (``OnlyEnforceIf``):

* ``load[j] >= load[i] + demand[j]``, ``load`` in ``[demand, capacity]``;
* ``start[j] >= start[i] + service[i] + travel[i][j]``, ``start`` in
  ``[ready, due]``, with the depot's ready time as the start of every route
  and ``start[i] + service[i] + travel[i][0] <= depot due`` on the way home.

The objective is total distance. Arcs that cannot be feasible on their own
(joint demand over capacity, or the earliest possible start at ``j`` already
past ``j``'s due time) are never created, which keeps the model small.

The solver status decides ``proven_optimal``: only ``OPTIMAL`` is a proof.
``FEASIBLE`` at the time limit is a good solution with an optimality gap,
reported in ``stats``.

SCALE. This is the exact path; its cost grows fast with the number of
customers. It proves optimality on instances of a dozen or so customers in
well under a second, and is the wrong tool beyond a few dozen. Use
Clarke-Wright or OR-Tools routing for anything bigger.

ONE SEARCH WORKER BY DEFAULT. Measured 2026-09-18 on macOS arm64 with
ortools 9.15.6755: once ``pyarrow`` is imported in the same process (pandas
pulls it in whenever it is installed, and ``datasets`` / ``mlflow`` install
it), a multi-worker CP-SAT solve deadlocks and never returns, even on a
one-variable model and past ``max_time_in_seconds``. One worker is
unaffected. It is also what the deployed API can afford (one uvicorn worker
on 0.5 CPU, see ``render.yaml``), and it makes solves deterministic. Raise
``num_workers`` only in a process that never loads pyarrow.
"""

from __future__ import annotations

import time
from typing import Dict, List, Tuple

from ortools.sat.python import cp_model

from app.vrp.model import VrpInstance, VrpSolution
from app.vrp.validate import build_solution

METHOD = "cpsat"


def solve_cpsat(
    instance: VrpInstance,
    time_limit_seconds: float = 10.0,
    num_workers: int = 1,
) -> VrpSolution:
    """Solve ``instance`` exactly (up to the time limit) with CP-SAT."""
    t0 = time.perf_counter()
    if instance.num_customers == 0:
        return build_solution(instance, METHOD, [], time.perf_counter() - t0, proven_optimal=True)

    nodes = instance.nodes
    dist = instance.distance
    tt = instance.travel_time
    assert tt is not None
    cap = instance.vehicle_capacity
    n = len(nodes)
    depot = nodes[0]

    if any(nodes[i].demand > cap for i in instance.customers):
        # No vehicle can carry this customer's demand; the load domain below would be empty.
        return build_solution(instance, METHOD, None, time.perf_counter() - t0, stats={"solver_status": "INFEASIBLE"})

    model = cp_model.CpModel()
    load = {i: model.new_int_var(nodes[i].demand, cap, f"load_{i}") for i in instance.customers}
    start = {i: model.new_int_var(nodes[i].ready, nodes[i].due, f"start_{i}") for i in instance.customers}

    arcs: Dict[Tuple[int, int], cp_model.IntVar] = {}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            earliest_leave = depot.ready if i == 0 else nodes[i].ready + nodes[i].service_time
            if j != 0 and earliest_leave + tt[i][j] > nodes[j].due:
                continue
            if j == 0 and i != 0 and earliest_leave + tt[i][0] > depot.due:
                continue
            if i != 0 and j != 0 and nodes[i].demand + nodes[j].demand > cap:
                continue
            arcs[i, j] = model.new_bool_var(f"x_{i}_{j}")

    model.add_multiple_circuit([(i, j, lit) for (i, j), lit in arcs.items()])
    model.add(sum(lit for (i, _), lit in arcs.items() if i == 0) <= instance.num_vehicles)

    for (i, j), lit in arcs.items():
        if i == 0:
            model.add(start[j] >= depot.ready + tt[0][j]).only_enforce_if(lit)
        elif j == 0:
            model.add(start[i] + nodes[i].service_time + tt[i][0] <= depot.due).only_enforce_if(lit)
        else:
            model.add(start[j] >= start[i] + nodes[i].service_time + tt[i][j]).only_enforce_if(lit)
            model.add(load[j] >= load[i] + nodes[j].demand).only_enforce_if(lit)

    model.minimize(sum(dist[i][j] * lit for (i, j), lit in arcs.items()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_workers = int(num_workers)
    status = solver.solve(model)
    wall = time.perf_counter() - t0

    stats: dict = {"solver_status": solver.status_name(status)}
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return build_solution(instance, METHOD, None, wall, stats=stats)

    stats["objective"] = int(solver.objective_value)
    stats["best_bound"] = int(round(solver.best_objective_bound))

    succ = {i: j for (i, j), lit in arcs.items() if i != 0 and solver.value(lit)}
    routes: List[List[int]] = []
    for (i, j), lit in arcs.items():
        if i == 0 and solver.value(lit):
            route = []
            node = j
            while node != 0:
                route.append(node)
                node = succ[node]
            routes.append(route)

    return build_solution(instance, METHOD, routes, wall, proven_optimal=status == cp_model.OPTIMAL, stats=stats)
