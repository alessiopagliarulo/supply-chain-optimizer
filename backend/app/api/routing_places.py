"""
Route planning between real distributor locations.

  GET  /routing/places/model   The distance model (road factor and why), the road regions,
                               the example scenario's defaults and the request caps.
  POST /routing/places/solve   A depot and destinations (distributor ids) plus the example
                               scenario in, a validated route plan in km and hours out.

Coordinates are read from the catalogue by id, never taken from the request, so a
plan is always drawn between the places the catalogue says the distributors are.
Distances are great-circle distance times ``app.vrp.geo.ROAD_FACTOR``
(docs/REAL_PLACE_ROUTING.md). Loads, trucks, speed and hours are an EXAMPLE
scenario: the catalogue has no orders or fleet.

Same DoS posture as ``app/api/routing.py``: capped stop count and time limit, a
plain ``def`` handler so the CPU-bound solve runs in the thread pool.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.api.routing import MAX_EXACT_CUSTOMERS, MAX_TIME_LIMIT_SECONDS
from app.core.database import get_db
from app.models.distributor import Distributor
from app.vrp import EXACT_MAX_CUSTOMERS, solve
from app.vrp.geo import EARTH_RADIUS_KM, ROAD_FACTOR, ROAD_FACTOR_RATIONALE
from app.vrp.places import DEFAULT_SCENARIO, EXAMPLE_DEPOT_NAME, ROAD_REGIONS, Place, PlacesError, Scenario, plan_routes

router = APIRouter(prefix="/routing/places", tags=["routing"])

#: Destinations per plan. The catalogue's largest road region (North America) has
#: 45 located distributors, so every one of them fits.
MAX_PLACE_STOPS = 60
MAX_VEHICLES = 30
MAX_CAPACITY = 1000
MAX_ROUTE_HOURS = 240.0


class ScenarioIn(BaseModel):
    """The example assumptions. Defaults are ``app.vrp.places.DEFAULT_SCENARIO``."""

    stop_load: int = Field(default=DEFAULT_SCENARIO.stop_load, ge=1, le=MAX_CAPACITY, description="Pallets per stop.")
    vehicle_capacity: int = Field(
        default=DEFAULT_SCENARIO.vehicle_capacity, ge=1, le=MAX_CAPACITY, description="Pallets per truck."
    )
    num_vehicles: int = Field(default=DEFAULT_SCENARIO.num_vehicles, ge=1, le=MAX_VEHICLES)
    speed_kmh: float = Field(default=DEFAULT_SCENARIO.speed_kmh, ge=10, le=120, description="Average driving speed.")
    service_minutes: int = Field(
        default=DEFAULT_SCENARIO.service_minutes, ge=0, le=600, description="Minutes at each stop."
    )
    max_route_hours: float = Field(
        default=DEFAULT_SCENARIO.max_route_hours, gt=0, le=MAX_ROUTE_HOURS, description="Longest route, depot to depot."
    )


class PlacesSolveRequest(BaseModel):
    depot_id: int = Field(..., description="Distributor id of the depot.")
    stop_ids: List[int] = Field(..., min_length=1, max_length=MAX_PLACE_STOPS, description="Distributor ids to visit.")
    scenario: ScenarioIn = Field(default_factory=lambda: ScenarioIn.model_validate({}))
    method: Literal["auto", "cpsat", "clarke_wright", "ortools"] = "auto"
    time_limit_seconds: float = Field(2.0, gt=0, le=MAX_TIME_LIMIT_SECONDS)

    @model_validator(mode="after")
    def _distinct(self) -> "PlacesSolveRequest":
        if len(set(self.stop_ids)) != len(self.stop_ids):
            raise ValueError("stop_ids must not repeat a distributor")
        if self.depot_id in self.stop_ids:
            raise ValueError("the depot cannot also be a destination")
        return self


class PlaceOut(BaseModel):
    id: int
    name: str
    latitude: float
    longitude: float
    city: Optional[str]
    state: Optional[str]
    country: Optional[str]


class PlacedRouteOut(BaseModel):
    stops: List[int] = Field(..., description="Indices into `stops`, in visiting order.")
    load: int
    distance_km: float
    straight_line_km: float
    duration_hours: float
    service_start_hours: List[float]


class PlacesSolveResponse(BaseModel):
    depot: PlaceOut
    stops: List[PlaceOut]
    scenario: ScenarioIn
    road_factor: float
    method: str
    status: str
    feasible: bool
    proven_optimal: bool
    wall_seconds: float
    vehicles_used: int
    total_km: float
    total_straight_line_km: float
    routes: List[PlacedRouteOut]
    violations: List[str]


class PlacesLimits(BaseModel):
    max_stops: int
    max_vehicles: int
    max_capacity: int
    max_route_hours: float
    max_time_limit_seconds: float
    max_exact_customers: int
    auto_exact_max_customers: int


class ExamplePlan(BaseModel):
    depot_id: int
    stop_ids: List[int]


class PlacesModel(BaseModel):
    """How a real-place plan measures distance and what it assumes."""

    road_factor: float
    road_factor_rationale: str
    earth_radius_km: float
    road_regions: Dict[str, str]
    default_scenario: ScenarioIn
    limits: PlacesLimits
    #: The depot and destinations the page starts with; null if the catalogue lacks them.
    example: Optional[ExamplePlan]
    documentation: str


def _example(db: Session) -> Optional[ExamplePlan]:
    depot = db.query(Distributor).filter(Distributor.name == EXAMPLE_DEPOT_NAME).first()
    if depot is None or depot.latitude is None or depot.longitude is None:
        return None
    others = (
        db.query(Distributor.id)
        .filter(
            Distributor.country == depot.country,
            Distributor.id != depot.id,
            Distributor.latitude.isnot(None),
            Distributor.longitude.isnot(None),
        )
        .order_by(Distributor.id)
        .limit(MAX_PLACE_STOPS)
        .all()
    )
    return ExamplePlan(depot_id=cast(int, depot.id), stop_ids=[i for (i,) in others])


@router.get("/model", response_model=PlacesModel)
def places_model(db: Session = Depends(get_db)) -> PlacesModel:
    return PlacesModel(
        road_factor=ROAD_FACTOR,
        road_factor_rationale=ROAD_FACTOR_RATIONALE,
        earth_radius_km=EARTH_RADIUS_KM,
        road_regions=ROAD_REGIONS,
        default_scenario=ScenarioIn.model_validate({}),
        limits=PlacesLimits(
            max_stops=MAX_PLACE_STOPS,
            max_vehicles=MAX_VEHICLES,
            max_capacity=MAX_CAPACITY,
            max_route_hours=MAX_ROUTE_HOURS,
            max_time_limit_seconds=MAX_TIME_LIMIT_SECONDS,
            max_exact_customers=MAX_EXACT_CUSTOMERS,
            auto_exact_max_customers=EXACT_MAX_CUSTOMERS,
        ),
        example=_example(db),
        documentation="docs/REAL_PLACE_ROUTING.md",
    )


def _place(d: Distributor) -> Place:
    """Only called once ``d`` is known to have coordinates."""
    return Place(
        id=cast(int, d.id),
        name=str(d.name),
        latitude=float(d.latitude),  # type: ignore[arg-type]
        longitude=float(d.longitude),  # type: ignore[arg-type]
        city=d.city,
        state=d.state,
        country=d.country,
    )


def _out(p: Place) -> PlaceOut:
    return PlaceOut(
        id=p.id, name=p.name, latitude=p.latitude, longitude=p.longitude, city=p.city, state=p.state, country=p.country
    )


@router.post("/solve", response_model=PlacesSolveResponse)
def solve_places(req: PlacesSolveRequest, db: Session = Depends(get_db)) -> PlacesSolveResponse:
    """Plan truck routes from a real depot to real destinations under the example scenario."""
    if req.method == "cpsat" and len(req.stop_ids) > MAX_EXACT_CUSTOMERS:
        raise HTTPException(
            status_code=422,
            detail=f"cpsat is the exact path and is capped at {MAX_EXACT_CUSTOMERS} destinations; "
            f"use clarke_wright or ortools for {len(req.stop_ids)}",
        )
    wanted = [req.depot_id, *req.stop_ids]
    rows = {d.id: d for d in db.query(Distributor).filter(Distributor.id.in_(wanted)).all()}
    missing = [i for i in wanted if i not in rows]
    if missing:
        raise HTTPException(status_code=404, detail=f"no distributor with id {missing}")
    unlocated = [rows[i].name for i in wanted if rows[i].latitude is None or rows[i].longitude is None]
    if unlocated:
        raise HTTPException(
            status_code=422, detail=f"these distributors have no known location and cannot be routed: {unlocated}"
        )

    depot = _place(rows[req.depot_id])
    stops = [_place(rows[i]) for i in req.stop_ids]
    scenario = Scenario(**req.scenario.model_dump())
    try:
        plan = plan_routes(
            depot,
            stops,
            scenario,
            lambda inst: solve(inst, method=req.method, time_limit_seconds=req.time_limit_seconds),
        )
    except PlacesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    s = plan.solution
    return PlacesSolveResponse(
        depot=_out(depot),
        stops=[_out(p) for p in stops],
        scenario=req.scenario,
        road_factor=plan.road_factor,
        method=s.method,
        status=s.status,
        feasible=s.feasible,
        proven_optimal=s.proven_optimal,
        wall_seconds=s.wall_seconds,
        vehicles_used=s.vehicles_used,
        total_km=plan.total_km,
        total_straight_line_km=plan.total_straight_line_km,
        routes=[PlacedRouteOut(**r.__dict__) for r in plan.routes],
        violations=plan.violations,
    )
