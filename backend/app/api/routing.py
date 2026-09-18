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
"""
from __future__ import annotations

import dataclasses
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.vrp import Node, VrpInstance, solve

router = APIRouter(prefix="/routing", tags=["routing"])

MAX_CUSTOMERS = 200
MAX_EXACT_CUSTOMERS = 25
MAX_TIME_LIMIT_SECONDS = 10.0


class NodeIn(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    demand: int = Field(0, ge=0)
    ready: int = Field(0, ge=0)
    due: int = Field(..., ge=0)
    service_time: int = Field(0, ge=0)


class SolveRequest(BaseModel):
    nodes: List[NodeIn] = Field(..., min_length=1, max_length=MAX_CUSTOMERS + 1, description="Depot first.")
    distance_matrix: Optional[List[List[int]]] = Field(
        None, description="Integer distances; also the travel times. Omit to use scaled Euclidean distance of x/y."
    )
    scale: int = Field(1, ge=1, le=1000, description="Multiplier applied to Euclidean distances before rounding.")
    num_vehicles: int = Field(..., ge=1, le=MAX_CUSTOMERS)
    vehicle_capacity: int = Field(..., ge=0)
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
