"""
CVRPTW routing engine: one data model, three solver paths, one validator.

    from app.vrp import VrpInstance, Node, solve

    solution = solve(instance, method="auto")
    solution.routes, solution.total_cost, solution.feasible, solution.validation

Solver paths (all take a :class:`VrpInstance`, all return a :class:`VrpSolution`
already checked by :func:`validate_routes`):

* ``cpsat`` - exact CP-SAT model (``app/vrp/cpsat.py``), small instances;
* ``clarke_wright`` - savings heuristic (``app/vrp/savings.py``), fast, large;
* ``ortools`` - OR-Tools routing library (``app/vrp/ortools_routing.py``),
  the general-purpose path.

``method="auto"`` picks ``cpsat`` up to :data:`EXACT_MAX_CUSTOMERS` customers
and ``ortools`` above that.
"""

from __future__ import annotations

from app.vrp.cpsat import solve_cpsat
from app.vrp.model import (
    STATUS_FEASIBLE,
    STATUS_INFEASIBLE,
    STATUS_NO_SOLUTION,
    STATUS_OPTIMAL,
    Node,
    RouteReport,
    ValidationReport,
    VrpInstance,
    VrpSolution,
)
from app.vrp.ortools_routing import solve_ortools
from app.vrp.savings import solve_clarke_wright
from app.vrp.validate import build_solution, validate_routes

#: Largest instance ``method="auto"`` sends to the exact CP-SAT model.
EXACT_MAX_CUSTOMERS = 15

METHODS = ("auto", "cpsat", "clarke_wright", "ortools")


def solve(instance: VrpInstance, method: str = "auto", time_limit_seconds: float = 5.0, random_seed: int = 42) -> VrpSolution:
    """Solve ``instance`` with the named solver path.

    ``time_limit_seconds`` bounds CP-SAT and OR-Tools routing; Clarke-Wright
    is a single greedy pass and ignores it. ``random_seed`` is used by
    CP-SAT and OR-Tools for reproducibility; Clarke-Wright is deterministic.
    """
    if method == "auto":
        method = "cpsat" if instance.num_customers <= EXACT_MAX_CUSTOMERS else "ortools"
    if method == "cpsat":
        return solve_cpsat(instance, time_limit_seconds=time_limit_seconds, random_seed=random_seed)
    if method == "clarke_wright":
        return solve_clarke_wright(instance)
    if method == "ortools":
        return solve_ortools(instance, time_limit_seconds=time_limit_seconds, random_seed=random_seed)
    raise ValueError(f"unknown method {method!r}; expected one of {METHODS}")


__all__ = [
    "EXACT_MAX_CUSTOMERS",
    "METHODS",
    "STATUS_FEASIBLE",
    "STATUS_INFEASIBLE",
    "STATUS_NO_SOLUTION",
    "STATUS_OPTIMAL",
    "Node",
    "RouteReport",
    "ValidationReport",
    "VrpInstance",
    "VrpSolution",
    "build_solution",
    "solve",
    "solve_clarke_wright",
    "solve_cpsat",
    "solve_ortools",
    "validate_routes",
]
