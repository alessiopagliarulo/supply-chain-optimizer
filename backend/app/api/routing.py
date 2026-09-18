"""
CVRPTW solve endpoint over ``app/vrp``.

  POST /routing/solve   One instance in, one validated route plan out.

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
from typing import Annotated, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.vrp import Node, VrpInstance, solve, tune_buffers

router = APIRouter(prefix="/routing", tags=["routing"])

MAX_CUSTOMERS = 200
MAX_EXACT_CUSTOMERS = 25
MAX_TIME_LIMIT_SECONDS = 10.0
MAX_VALUE = 10**9
MAX_COORDINATE = 10**5

Value = Annotated[int, Field(ge=0, le=MAX_VALUE)]


class NodeIn(BaseModel):
    x: Optional[float] = Field(None, ge=-MAX_COORDINATE, le=MAX_COORDINATE)
    y: Optional[float] = Field(None, ge=-MAX_COORDINATE, le=MAX_COORDINATE)
    demand: Value = 0
    ready: Value = 0
    due: Value
    service_time: Value = 0


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
    nodes = [Node(demand=n.demand, ready=n.ready, due=n.due, service_time=n.service_time) for n in req.nodes]
    try:
        if req.distance_matrix is not None:
            instance = VrpInstance(
                nodes=nodes,
                distance=req.distance_matrix,
                num_vehicles=req.num_vehicles,
                vehicle_capacity=req.vehicle_capacity,
            )
        else:
            instance = VrpInstance.from_coordinates(
                [(float(n.x), float(n.y)) for n in req.nodes],  # type: ignore[arg-type]
                nodes,
                num_vehicles=req.num_vehicles,
                vehicle_capacity=req.vehicle_capacity,
                scale=req.scale,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    nodes = [Node(demand=n.demand, ready=n.ready, due=n.due, service_time=n.service_time) for n in req.nodes]
    try:
        if req.distance_matrix is not None:
            instance = VrpInstance(
                nodes=nodes,
                distance=req.distance_matrix,
                num_vehicles=req.num_vehicles,
                vehicle_capacity=req.vehicle_capacity,
            )
        else:
            instance = VrpInstance.from_coordinates(
                [(float(n.x), float(n.y)) for n in req.nodes],  # type: ignore[arg-type]
                nodes,
                num_vehicles=req.num_vehicles,
                vehicle_capacity=req.vehicle_capacity,
                scale=req.scale,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        result = tune_buffers(
            instance,
            solve,
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
