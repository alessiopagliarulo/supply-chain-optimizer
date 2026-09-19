"""
CVRPTW endpoints over ``app/vrp``.

  GET  /routing/instances        The named instances (built-in samples, Solomon files) and the caps below.
  GET  /routing/instances/{id}   One named instance: coordinates, nodes, fleet.
  POST /routing/solve            One instance in, one validated route plan out.
  POST /routing/simulate         One route plan in, its discrete-event simulation KPIs out.
  POST /routing/tune-buffers     Schedule/capacity buffer search against the simulation.
  GET  /routing/benchmarks       The committed Solomon benchmark artifact, if generated.

The request carries the nodes (depot first) with planar coordinates, or an
explicit distance matrix, plus the fleet. The response is the solver's
``VrpSolution``: routes, total cost, status, and the shared validator's
per-route schedule and violations, so a caller never has to trust a solver's
own claim of feasibility.

DoS POSTURE. The deployed API is one uvicorn worker on 0.5 CPU
(``render.yaml``), and a solve is CPU-bound for its whole time limit (OR-Tools
guided local search never stops early). So the time limit is capped at
``MAX_TIME_LIMIT_SECONDS``, the instance size at ``MAX_CUSTOMERS``, and the
exact CP-SAT path at ``MAX_EXACT_CUSTOMERS`` customers. The handler is a plain
``def`` so FastAPI runs it in its thread pool instead of on the event loop.

Every integer in the request is capped at ``MAX_VALUE`` and every coordinate at
``MAX_COORDINATE``, so route sums and time cumuls stay far inside the 64-bit
range CP-SAT and OR-Tools routing work in; an oversized value is a 422.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Annotated, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.vrp import EXACT_MAX_CUSTOMERS, Node, VrpInstance, build_solution, simulate, solve, tune_buffers
from app.vrp.instances import list_instances, load_instance

router = APIRouter(prefix="/routing", tags=["routing"])

MAX_CUSTOMERS = 200
MAX_EXACT_CUSTOMERS = 25
MAX_TIME_LIMIT_SECONDS = 10.0
MAX_VALUE = 10**9
MAX_COORDINATE = 10**5
MAX_REPLICATIONS = 1000

#: Written by the Solomon benchmark script; served as-is, never computed here.
BENCHMARK_ARTIFACT_PATH = Path(__file__).resolve().parents[3] / "docs" / "benchmark_results.json"

Value = Annotated[int, Field(ge=0, le=MAX_VALUE)]


class NodeIn(BaseModel):
    x: Optional[float] = Field(None, ge=-MAX_COORDINATE, le=MAX_COORDINATE)
    y: Optional[float] = Field(None, ge=-MAX_COORDINATE, le=MAX_COORDINATE)
    demand: Value = 0
    ready: Value = 0
    due: Value
    service_time: Value = 0


def _build_instance(
    nodes_in: List[NodeIn],
    distance_matrix: Optional[List[List[int]]],
    num_vehicles: int,
    vehicle_capacity: int,
    scale: int,
) -> VrpInstance:
    """The request's instance, or a 422 naming what is wrong with it."""
    nodes = [Node(demand=n.demand, ready=n.ready, due=n.due, service_time=n.service_time) for n in nodes_in]
    try:
        if distance_matrix is not None:
            return VrpInstance(
                nodes=nodes,
                distance=distance_matrix,
                num_vehicles=num_vehicles,
                vehicle_capacity=vehicle_capacity,
            )
        return VrpInstance.from_coordinates(
            [(float(n.x), float(n.y)) for n in nodes_in],  # type: ignore[arg-type]
            nodes,
            num_vehicles=num_vehicles,
            vehicle_capacity=vehicle_capacity,
            scale=scale,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class SolveRequest(BaseModel):
    nodes: List[NodeIn] = Field(..., min_length=1, max_length=MAX_CUSTOMERS + 1, description="Depot first.")
    distance_matrix: Optional[List[List[Value]]] = Field(
        None, description="Integer distances; also the travel times. Omit to use scaled Euclidean distance of x/y."
    )
    scale: int = Field(1, ge=1, le=1000, description="Multiplier applied to Euclidean distances before rounding.")
    num_vehicles: int = Field(..., ge=1, le=MAX_CUSTOMERS)
    vehicle_capacity: Value
    method: Literal["auto", "cpsat", "clarke_wright", "ortools"] = "auto"
    time_limit_seconds: float = Field(2.0, gt=0, le=MAX_TIME_LIMIT_SECONDS)

    @model_validator(mode="after")
    def _distances_given(self) -> "SolveRequest":
        if self.distance_matrix is None and any(n.x is None or n.y is None for n in self.nodes):
            raise ValueError("give every node x and y, or give distance_matrix")
        return self


@router.post("/solve")
def solve_instance(req: SolveRequest) -> dict:
    """Solve one CVRPTW instance and return the validated route plan."""
    customers = len(req.nodes) - 1
    if req.method == "cpsat" and customers > MAX_EXACT_CUSTOMERS:
        raise HTTPException(
            status_code=422,
            detail=f"cpsat is the exact path and is capped at {MAX_EXACT_CUSTOMERS} customers; "
            f"use clarke_wright or ortools for {customers}",
        )
    instance = _build_instance(req.nodes, req.distance_matrix, req.num_vehicles, req.vehicle_capacity, req.scale)

    solution = solve(instance, method=req.method, time_limit_seconds=req.time_limit_seconds)
    out = dataclasses.asdict(solution)
    out["feasible"] = solution.feasible
    out["vehicles_used"] = solution.vehicles_used
    return out


class BufferTuningRequest(BaseModel):
    nodes: List[NodeIn] = Field(..., min_length=1, max_length=MAX_CUSTOMERS + 1, description="Depot first.")
    distance_matrix: Optional[List[List[Value]]] = Field(
        None, description="Integer distances; also the travel times. Omit to use scaled Euclidean distance of x/y."
    )
    scale: int = Field(1, ge=1, le=1000, description="Multiplier applied to Euclidean distances before rounding.")
    num_vehicles: int = Field(..., ge=1, le=MAX_CUSTOMERS)
    vehicle_capacity: Value
    schedule_buffer_max: float = Field(10, ge=0, le=100, description="Max schedule buffer percentage.")
    capacity_buffer_max: float = Field(5, ge=0, le=50, description="Max capacity buffer percentage.")
    schedule_buffer_step: float = Field(2, gt=0, le=100, description="Schedule buffer grid step.")
    capacity_buffer_step: float = Field(1, gt=0, le=50, description="Capacity buffer grid step.")
    num_replications: int = Field(50, ge=1, le=1000, description="Simulation replications per candidate.")
    variability: float = Field(0.1, ge=0, le=2, description="Lognormal CV or triangular range.")
    distribution: Literal["lognormal", "triangular"] = "lognormal"
    target_on_time_rate: float = Field(0.95, ge=0, le=1, description="Target on-time rate.")
    objective: Literal["maximize_on_time", "minimize_cost_for_target"] = "minimize_cost_for_target"
    base_seed: int = Field(42, description="Random seed for reproducibility.")
    method: Literal["auto", "cpsat", "clarke_wright", "ortools"] = Field(
        "auto", description="Solver path used for every candidate plan."
    )
    time_limit_seconds: float = Field(
        5.0, gt=0, le=MAX_TIME_LIMIT_SECONDS, description="Solver time limit per candidate plan."
    )

    @model_validator(mode="after")
    def _distances_given(self) -> "BufferTuningRequest":
        if self.distance_matrix is None and any(n.x is None or n.y is None for n in self.nodes):
            raise ValueError("give every node x and y, or give distance_matrix")
        return self


class CandidateOut(BaseModel):
    schedule_buffer_pct: float
    capacity_buffer_pct: float
    on_time_rate: float
    mean_lateness: float
    p95_lateness: float
    total_distance: int
    vehicles_used: int


class BufferTuningResponse(BaseModel):
    chosen_buffer: CandidateOut
    frontier: List[CandidateOut]
    num_replications: int
    variability: float
    distribution: str
    objective: str
    target_on_time_rate: float


@router.post("/tune-buffers")
def tune_instance_buffers(req: BufferTuningRequest) -> BufferTuningResponse:
    """Tune schedule/capacity buffers against DES simulation results."""
    customers = len(req.nodes) - 1
    if customers > MAX_EXACT_CUSTOMERS:
        raise HTTPException(
            status_code=422,
            detail=f"Buffer tuning is capped at {MAX_EXACT_CUSTOMERS} customers for practical runtime; "
            f"got {customers}",
        )

    instance = _build_instance(req.nodes, req.distance_matrix, req.num_vehicles, req.vehicle_capacity, req.scale)

    try:
        result = tune_buffers(
            instance,
            lambda inst: solve(inst, method=req.method, time_limit_seconds=req.time_limit_seconds),
            schedule_buffer_range=(0, req.schedule_buffer_max, req.schedule_buffer_step),
            capacity_buffer_range=(0, req.capacity_buffer_max, req.capacity_buffer_step),
            num_replications=req.num_replications,
            variability=req.variability,
            distribution=req.distribution,
            base_seed=req.base_seed,
            target_on_time_rate=req.target_on_time_rate,
            objective=req.objective,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    chosen = CandidateOut(
        schedule_buffer_pct=result.chosen_buffer.schedule_buffer_pct,
        capacity_buffer_pct=result.chosen_buffer.capacity_buffer_pct,
        on_time_rate=result.chosen_buffer.simulation.on_time_rate,
        mean_lateness=result.chosen_buffer.simulation.mean_lateness,
        p95_lateness=result.chosen_buffer.simulation.p95_lateness,
        total_distance=result.chosen_buffer.total_distance,
        vehicles_used=result.chosen_buffer.vehicles_used,
    )

    frontier = [
        CandidateOut(
            schedule_buffer_pct=c.schedule_buffer_pct,
            capacity_buffer_pct=c.capacity_buffer_pct,
            on_time_rate=c.simulation.on_time_rate,
            mean_lateness=c.simulation.mean_lateness,
            p95_lateness=c.simulation.p95_lateness,
            total_distance=c.total_distance,
            vehicles_used=c.vehicles_used,
        )
        for c in result.frontier
    ]

    return BufferTuningResponse(
        chosen_buffer=chosen,
        frontier=frontier,
        num_replications=result.num_replications,
        variability=result.variability,
        distribution=result.distribution,
        objective=result.objective,
        target_on_time_rate=result.target_on_time_rate,
    )


# ── Named instances ──────────────────────────────────────────────────────────


class InstanceSummary(BaseModel):
    id: str
    name: str
    source: str
    num_customers: int
    num_vehicles: int
    vehicle_capacity: int


class RoutingLimits(BaseModel):
    """The request caps above, so a client can state them instead of retyping them."""

    max_customers: int
    max_exact_customers: int
    # method="auto" sends an instance to CP-SAT only up to this size, OR-Tools above it.
    # Smaller than max_exact_customers, which caps an explicit method="cpsat" request.
    auto_exact_max_customers: int
    max_tuning_customers: int
    max_time_limit_seconds: float
    max_replications: int


class InstanceListResponse(BaseModel):
    instances: List[InstanceSummary]
    limits: RoutingLimits


class InstanceDetail(InstanceSummary):
    nodes: List[NodeIn] = Field(..., description="Depot first, with x/y coordinates.")


@router.get("/instances")
def get_instances() -> InstanceListResponse:
    """The named instances the app can load, and the request caps."""
    return InstanceListResponse(
        instances=[
            InstanceSummary(
                id=inst.id,
                name=inst.name,
                source=inst.source,
                num_customers=inst.num_customers,
                num_vehicles=inst.num_vehicles,
                vehicle_capacity=inst.vehicle_capacity,
            )
            for inst in list_instances()
        ],
        limits=RoutingLimits(
            max_customers=MAX_CUSTOMERS,
            max_exact_customers=MAX_EXACT_CUSTOMERS,
            auto_exact_max_customers=EXACT_MAX_CUSTOMERS,
            max_tuning_customers=MAX_EXACT_CUSTOMERS,
            max_time_limit_seconds=MAX_TIME_LIMIT_SECONDS,
            max_replications=MAX_REPLICATIONS,
        ),
    )


@router.get("/instances/{prefix}/{stem}")
def get_instance(prefix: str, stem: str) -> InstanceDetail:
    """One named instance, ready to post to /solve."""
    instance_id = f"{prefix}/{stem}"
    try:
        inst = load_instance(instance_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no instance {instance_id!r}") from exc
    return InstanceDetail(
        id=inst.id,
        name=inst.name,
        source=inst.source,
        num_customers=inst.num_customers,
        num_vehicles=inst.num_vehicles,
        vehicle_capacity=inst.vehicle_capacity,
        nodes=[
            NodeIn(x=x, y=y, demand=n.demand, ready=n.ready, due=n.due, service_time=n.service_time)
            for (x, y), n in zip(inst.coords, inst.nodes, strict=True)
        ],
    )


# ── Simulation ───────────────────────────────────────────────────────────────


class SimulateRequest(BaseModel):
    nodes: List[NodeIn] = Field(..., min_length=1, max_length=MAX_CUSTOMERS + 1, description="Depot first.")
    distance_matrix: Optional[List[List[Value]]] = Field(
        None, description="Integer distances; also the travel times. Omit to use scaled Euclidean distance of x/y."
    )
    scale: int = Field(1, ge=1, le=1000, description="Multiplier applied to Euclidean distances before rounding.")
    num_vehicles: int = Field(..., ge=1, le=MAX_CUSTOMERS)
    vehicle_capacity: Value
    routes: List[List[int]] = Field(..., max_length=MAX_CUSTOMERS, description="The plan: customer indices per vehicle.")
    num_replications: int = Field(100, ge=1, le=MAX_REPLICATIONS)
    variability: float = Field(0.1, ge=0, le=2, description="Lognormal CV or triangular range.")
    distribution: Literal["lognormal", "triangular"] = "lognormal"
    seed: int = Field(42, description="Replication i uses seed + i.")

    @model_validator(mode="after")
    def _distances_given(self) -> "SimulateRequest":
        if self.distance_matrix is None and any(n.x is None or n.y is None for n in self.nodes):
            raise ValueError("give every node x and y, or give distance_matrix")
        return self


class SimulateResponse(BaseModel):
    num_replications: int
    variability: float
    distribution: str
    seed: int
    on_time_rate: float
    mean_lateness: float
    p95_lateness: float
    mean_route_completion_time: float
    max_route_completion_time: float
    #: The depot's due time: every vehicle is planned to be back by then.
    depot_close: int
    vehicle_utilization_mean: float
    vehicle_utilization_min: float
    #: The deterministic plan's own verdict, from the shared validator.
    plan_feasible: bool
    plan_violations: List[str]


@router.post("/simulate")
def simulate_plan(req: SimulateRequest) -> SimulateResponse:
    """Run a route plan through the discrete-event simulation with random travel and service times."""
    instance = _build_instance(req.nodes, req.distance_matrix, req.num_vehicles, req.vehicle_capacity, req.scale)
    customers = set(instance.customers)
    stray = sorted({c for route in req.routes for c in route if c not in customers})
    if stray:
        raise HTTPException(status_code=422, detail=f"routes name nodes that are not customers: {stray}")

    solution = build_solution(instance, method="submitted", routes=req.routes, wall_seconds=0.0)
    results = simulate(
        instance,
        solution,
        num_replications=req.num_replications,
        variability=req.variability,
        distribution=req.distribution,
        seed=req.seed,
    )
    return SimulateResponse(
        num_replications=results.num_replications,
        variability=req.variability,
        distribution=req.distribution,
        seed=req.seed,
        on_time_rate=results.on_time_rate,
        mean_lateness=results.mean_lateness,
        p95_lateness=float(results.p95_lateness),
        mean_route_completion_time=results.mean_route_completion_time,
        max_route_completion_time=results.max_route_completion_time,
        depot_close=instance.horizon,
        vehicle_utilization_mean=results.vehicle_utilization_mean,
        vehicle_utilization_min=results.vehicle_utilization_min,
        plan_feasible=solution.feasible,
        plan_violations=solution.validation.violations,
    )


# ── Benchmarks ───────────────────────────────────────────────────────────────


@router.get("/benchmarks")
def get_benchmarks() -> dict:
    """The committed Solomon benchmark artifact, served as written.

    ``available`` is false when the artifact has not been generated in this
    checkout; nothing is ever computed or filled in here.
    """
    path = BENCHMARK_ARTIFACT_PATH
    if not path.is_file():
        return {"available": False, "artifact": "docs/benchmark_results.json"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"docs/benchmark_results.json is unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise HTTPException(status_code=500, detail="docs/benchmark_results.json has no results list")
    return {"available": True, "artifact": "docs/benchmark_results.json", **data}
