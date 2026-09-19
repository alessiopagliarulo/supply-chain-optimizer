"""
Route planning between real places (latitude/longitude), on the same CVRPTW engine.

A place is a real distributor location from the catalogue. The routing engine
itself is unchanged: this module turns a depot, a list of stops and an
**example scenario** into a :class:`VrpInstance` whose

* ``distance`` is the estimated driving distance in whole metres
  (great-circle distance times :data:`app.vrp.geo.ROAD_FACTOR`), and
* ``travel_time`` is whole minutes at the scenario's average speed,

solves it with :func:`app.vrp.solve`, and reports each route in kilometres and
hours.

THE SCENARIO IS AN EXAMPLE. The catalogue holds real places but no orders,
loads, trucks or opening hours. Everything that is not a location (load per
stop, truck capacity, fleet size, speed, time at each stop, the route-length
limit) comes from :class:`Scenario`, whose defaults are documented in
``docs/REAL_PLACE_ROUTING.md`` and are never presented as real customers or
real demand.

ROAD REGIONS. A truck cannot drive from Shenzhen to Atlanta, so a plan's depot
and stops must share one :data:`ROAD_REGIONS` region: a set of countries that
are connected by road (Great Britain counts as connected to the continent
through the Channel Tunnel).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from app.vrp.geo import ROAD_FACTOR, great_circle_km, road_distance_matrix_m, travel_time_matrix_min
from app.vrp.model import Node, VrpInstance, VrpSolution

#: Country (as the catalogue spells it) -> the road-connected region it belongs to.
ROAD_REGIONS: Dict[str, str] = {
    "USA": "North America",
    "Canada": "North America",
    "UK": "Europe",
    "Germany": "Europe",
    "Netherlands": "Europe",
    "Norway": "Europe",
    "Poland": "Europe",
    # Hong Kong is stored as country "China" in the catalogue; it is road-connected
    # to Shenzhen, and mainland China to Singapore and Thailand through Laos/Malaysia.
    "China": "Mainland Asia",
    "Singapore": "Mainland Asia",
    "Thailand": "Mainland Asia",
    "Japan": "Japan",
}


def road_region(country: Optional[str]) -> Optional[str]:
    """The road region of ``country``; an unmapped country is a region of its own."""
    if not country:
        return None
    return ROAD_REGIONS.get(country, country)


@dataclass(frozen=True)
class Place:
    """A real location from the catalogue."""

    id: int
    name: str
    latitude: float
    longitude: float
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None

    @property
    def point(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


@dataclass(frozen=True)
class Scenario:
    """The example assumptions around the real places. None of these are real data."""

    #: Pallets collected at every stop.
    stop_load: int = 3
    #: Pallets one truck holds.
    vehicle_capacity: int = 12
    num_vehicles: int = 4
    #: Average driving speed, km/h, used to turn distance into travel time.
    speed_kmh: float = 60.0
    #: Minutes spent at each stop.
    service_minutes: int = 30
    #: Longest a route may take, depot to depot, in hours.
    max_route_hours: float = 14.0


DEFAULT_SCENARIO = Scenario()

#: The example plan a reader sees first: this distributor's location as the depot and
#: every other located distributor in its country as a destination. Farnell (Leeds)
#: gives a one-country plan whose round trips all fit the default route limit.
EXAMPLE_DEPOT_NAME = "Farnell"


class PlacesError(ValueError):
    """The request cannot be planned; the message says why in the reader's terms."""


@dataclass
class PlacedRoute:
    """One truck's route, in real units."""

    #: Indices into the plan's ``stops`` list, in visiting order.
    stops: List[int]
    load: int
    distance_km: float
    straight_line_km: float
    duration_hours: float
    #: Arrival-to-service start at each stop, hours after leaving the depot.
    service_start_hours: List[float]


@dataclass
class PlacedPlan:
    solution: VrpSolution
    routes: List[PlacedRoute]
    total_km: float
    total_straight_line_km: float
    road_factor: float
    scenario: Scenario
    violations: List[str] = field(default_factory=list)


def check_plannable(depot: Place, stops: Sequence[Place], scenario: Scenario) -> None:
    """Raise :class:`PlacesError` for any request no fleet could serve."""
    if not stops:
        raise PlacesError("choose at least one destination")
    region = road_region(depot.country)
    off_region = [s for s in stops if road_region(s.country) != region]
    if off_region:
        names = ", ".join(f"{s.name} ({s.city}, {s.country})" for s in off_region[:5])
        more = f" and {len(off_region) - 5} more" if len(off_region) > 5 else ""
        raise PlacesError(
            f"the depot is in {region}; these destinations cannot be reached by road from it: {names}{more}"
        )
    if scenario.stop_load > scenario.vehicle_capacity:
        raise PlacesError(
            f"each stop's load ({scenario.stop_load}) is more than one truck holds ({scenario.vehicle_capacity})"
        )
    total = scenario.stop_load * len(stops)
    fleet = scenario.vehicle_capacity * scenario.num_vehicles
    if total > fleet:
        raise PlacesError(
            f"{len(stops)} stops x {scenario.stop_load} = {total} pallets, but {scenario.num_vehicles} trucks "
            f"x {scenario.vehicle_capacity} hold only {fleet}; add trucks or capacity"
        )


def build_instance(depot: Place, stops: Sequence[Place], scenario: Scenario) -> VrpInstance:
    """The CVRPTW instance: metres for cost, minutes for time, depot first."""
    points = [depot.point] + [s.point for s in stops]
    distance = road_distance_matrix_m(points)
    travel = travel_time_matrix_min(distance, scenario.speed_kmh)
    horizon = int(round(scenario.max_route_hours * 60))
    nodes = [Node(demand=0, ready=0, due=horizon)] + [
        Node(demand=scenario.stop_load, ready=0, due=horizon, service_time=scenario.service_minutes) for _ in stops
    ]
    instance = VrpInstance(
        nodes=nodes,
        distance=distance,
        travel_time=travel,
        num_vehicles=scenario.num_vehicles,
        vehicle_capacity=scenario.vehicle_capacity,
        name=f"{depot.name} + {len(stops)} stops",
    )
    too_far = [(s, travel[0][i + 1] + scenario.service_minutes + travel[i + 1][0]) for i, s in enumerate(stops)]
    too_far = [(s, m) for s, m in too_far if m > horizon]
    if too_far:
        names = ", ".join(f"{s.name} ({s.city}, {m / 60:.1f} h)" for s, m in too_far[:5])
        more = f" and {len(too_far) - 5} more" if len(too_far) > 5 else ""
        raise PlacesError(
            f"a round trip to these stops takes longer than the {scenario.max_route_hours:g} h route limit even "
            f"alone: {names}{more}; raise the limit or drop them"
        )
    return instance


def plan_routes(
    depot: Place,
    stops: Sequence[Place],
    scenario: Scenario,
    solve_fn: Callable[[VrpInstance], VrpSolution],
) -> PlacedPlan:
    """Check, build, solve (with ``solve_fn(instance)``) and report in km and hours."""
    check_plannable(depot, stops, scenario)
    instance = build_instance(depot, stops, scenario)
    solution: VrpSolution = solve_fn(instance)

    points = [depot.point] + [s.point for s in stops]
    routes: List[PlacedRoute] = []
    for report in solution.validation.routes:
        path = [0] + report.customers + [0]
        straight = sum(great_circle_km(points[a], points[b]) for a, b in zip(path, path[1:], strict=False))
        routes.append(
            PlacedRoute(
                stops=[c - 1 for c in report.customers],
                load=report.load,
                distance_km=report.distance / 1000,
                straight_line_km=straight,
                duration_hours=(report.return_time - report.depart) / 60,
                service_start_hours=[(s - report.depart) / 60 for s in report.service_starts],
            )
        )
    return PlacedPlan(
        solution=solution,
        routes=routes,
        total_km=solution.total_cost / 1000,
        total_straight_line_km=sum(r.straight_line_km for r in routes),
        road_factor=ROAD_FACTOR,
        scenario=scenario,
        violations=[_name_stops(v, stops) for v in solution.validation.violations],
    )


def _name_stops(violation: str, stops: Sequence[Place]) -> str:
    """The validator says "customer 3"; a reader of a real-place plan wants the place."""
    return re.sub(
        r"customer (\d+)",
        lambda m: stops[int(m.group(1)) - 1].name if 0 < int(m.group(1)) <= len(stops) else m.group(0),
        violation,
    )
