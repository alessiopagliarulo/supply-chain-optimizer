"""
The shared CVRPTW data model: one problem type in, one solution type out.

Every solver in ``app.vrp`` (CP-SAT exact, Clarke-Wright savings, OR-Tools
routing) accepts a :class:`VrpInstance` and returns a :class:`VrpSolution`, and
every solution is checked by the same validator (``app.vrp.validate``). Later
work (simulation, buffer tuning, Solomon benchmarking) builds on these types
rather than on any one solver's internals.

CONVENTIONS
-----------
* Node 0 is the depot. Nodes ``1..n`` are customers.
* All quantities are INTEGERS. CP-SAT and the OR-Tools routing library both
  work on integers, so the model does too; callers with fractional data (e.g.
  Solomon's Euclidean distances) scale before building the instance, as
  :meth:`VrpInstance.from_coordinates` does with its ``scale`` argument.
* ``distance[i][j]`` is what the objective sums (the "cost").
  ``travel_time[i][j]`` is what the schedule uses. When no travel-time matrix
  is given, travel time equals distance (the Solomon convention).
* Time windows are on SERVICE START: a vehicle arriving before ``ready`` waits;
  service must start no later than ``due``; the vehicle leaves after
  ``service_time``. The depot's window is the planning horizon: vehicles leave
  no earlier than its ``ready`` and must be back no later than its ``due``.
* The fleet is homogeneous: ``num_vehicles`` vehicles of ``vehicle_capacity``
  each. A route is the ordered list of customers one vehicle visits; the depot
  is implicit at both ends and never appears inside a route.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Node:
    """One stop. ``demand`` and ``service_time`` are 0 for the depot."""

    demand: int
    ready: int
    due: int
    service_time: int = 0


@dataclass
class VrpInstance:
    """A capacitated VRP with time windows. Validated on construction."""

    nodes: List[Node]
    distance: List[List[int]]
    num_vehicles: int
    vehicle_capacity: int
    travel_time: Optional[List[List[int]]] = None
    name: str = "instance"

    def __post_init__(self) -> None:
        n = len(self.nodes)
        if n < 1:
            raise ValueError("an instance needs at least a depot (node 0)")
        if self.num_vehicles < 1:
            raise ValueError("num_vehicles must be >= 1")
        if self.vehicle_capacity < 0:
            raise ValueError("vehicle_capacity must be >= 0")
        if self.travel_time is None:
            self.travel_time = self.distance
        for label, matrix in (("distance", self.distance), ("travel_time", self.travel_time)):
            if len(matrix) != n or any(len(row) != n for row in matrix):
                raise ValueError(f"{label} must be a {n}x{n} matrix")
            if any(v < 0 for row in matrix for v in row):
                raise ValueError(f"{label} entries must be non-negative")
        for i, node in enumerate(self.nodes):
            if node.ready > node.due:
                raise ValueError(f"node {i}: ready ({node.ready}) is after due ({node.due})")
            if node.demand < 0 or node.service_time < 0:
                raise ValueError(f"node {i}: demand and service_time must be non-negative")
        depot = self.nodes[0]
        if depot.demand != 0 or depot.service_time != 0:
            raise ValueError("the depot (node 0) must have demand 0 and service_time 0")

    @property
    def customers(self) -> range:
        """Customer node indices, ``1..n``."""
        return range(1, len(self.nodes))

    @property
    def num_customers(self) -> int:
        return len(self.nodes) - 1

    @property
    def horizon(self) -> int:
        """Latest time a vehicle may be back at the depot."""
        return self.nodes[0].due

    @classmethod
    def from_coordinates(
        cls,
        coords: Sequence[Tuple[float, float]],
        nodes: List[Node],
        num_vehicles: int,
        vehicle_capacity: int,
        scale: int = 1,
        name: str = "instance",
    ) -> "VrpInstance":
        """Build an instance from planar coordinates (depot first).

        Distance and travel time are the Euclidean distance times ``scale``,
        rounded to the nearest integer. Time windows and service times in
        ``nodes`` must already be in the same scaled units.
        """
        if len(coords) != len(nodes):
            raise ValueError("coords and nodes must have the same length")
        matrix = [
            [int(round(math.dist(a, b) * scale)) for b in coords]
            for a in coords
        ]
        return cls(
            nodes=nodes,
            distance=matrix,
            num_vehicles=num_vehicles,
            vehicle_capacity=vehicle_capacity,
            name=name,
        )


@dataclass
class RouteReport:
    """One route's schedule as the validator computed it."""

    customers: List[int]
    load: int
    distance: int
    #: Service start time at each customer, same order as ``customers``.
    service_starts: List[int]
    #: Time the vehicle leaves the depot (its window's ready time).
    depart: int
    #: Time the vehicle is back at the depot.
    return_time: int


@dataclass
class ValidationReport:
    """Result of checking routes against an instance. ``feasible`` is the verdict."""

    feasible: bool
    violations: List[str]
    total_cost: int
    routes: List[RouteReport]


#: Solution statuses, strongest first.
STATUS_OPTIMAL = "optimal"          # feasible and proven optimal (CP-SAT only)
STATUS_FEASIBLE = "feasible"        # feasible, not proven optimal
STATUS_INFEASIBLE = "infeasible"    # routes returned, but the validator rejects them
STATUS_NO_SOLUTION = "no_solution"  # the solver returned no routes at all


@dataclass
class VrpSolution:
    """What every solver returns."""

    method: str
    status: str
    routes: List[List[int]]
    #: Sum of ``distance`` over every arc driven, depot legs included.
    total_cost: int
    proven_optimal: bool
    wall_seconds: float
    validation: ValidationReport
    #: Solver-specific detail (e.g. CP-SAT's lower bound). Informational only.
    stats: dict = field(default_factory=dict)

    @property
    def feasible(self) -> bool:
        return self.validation.feasible

    @property
    def vehicles_used(self) -> int:
        return len(self.routes)
