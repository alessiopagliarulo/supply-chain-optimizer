# Routing between real places

The Route Plan page's **Real places** tab plans truck routes between real distributor
locations from the catalogue, on the same CVRPTW engine (`backend/app/vrp/`) the Solomon
test cases use. This page says what is real, what is assumed, and how distance is
measured.

## What is real

- **The places.** Every depot and destination is a distributor from the frozen 2024
  catalogue snapshot (`GET /distributors`), at its city-level coordinates. How those
  coordinates were compiled and checked is in [DATA_PROVENANCE.md](DATA_PROVENANCE.md).
  The API reads coordinates from the database by distributor id; a request cannot supply
  its own.
- **The solver and validator.** The same CP-SAT, OR-Tools routing and Clarke-Wright
  paths, and the same validator, as every other plan.

## What is an example

The catalogue has places but no orders, loads, trucks or opening hours. So everything
that is not a location comes from an **example scenario**, labelled "Example scenario"
on the page: an illustrative pickup run where each truck leaves the depot, collects the
same load at each destination, and returns. No destination is a real customer and no
load is a real order.

| Assumption | Default | Why this default |
|---|---|---|
| Load per stop | 3 pallets | A small, uniform load, so no stop looks more important than another. |
| Truck capacity | 12 pallets | Four stops per truck, so the default plan needs more than one truck. |
| Trucks | 4 | Enough for the default UK plan (8 stops, 24 pallets) with room to spare. |
| Average speed | 60 km/h | A blended road average for a loaded truck, motorway and local roads together. |
| Time at each stop | 30 minutes | Loading time at a pickup. |
| Longest route | 14 hours | A long working day, depot to depot. |

The defaults live in `backend/app/vrp/places.py` (`Scenario`) and are served by
`GET /api/v1/routing/places/model`; the page renders them from there. The reader can
change every one of them.

The page opens on an example plan: Farnell's location (Leeds) as the depot and every
other located UK distributor as a destination (`EXAMPLE_DEPOT_NAME` in `places.py`).
"Plan routes from this distributor" on the Map page opens the planner on that depot
instead, with the nearest destinations the example fleet can serve.

There are no time windows on the stops: the catalogue has no opening hours, so every
stop is open for the whole route limit and only the route length is constrained.

## How distance is measured

**Road distance = great-circle distance x 1.3.**

1. The great-circle (haversine) distance between two coordinates, on a sphere of radius
   6371.0088 km (the IUGG mean Earth radius). `backend/app/vrp/geo.py`.
2. Times the **road factor, 1.3**. Roads are never straight, so the driven distance is
   longer than the straight line. Published measurements of this "circuity" or detour
   factor for road travel sit at about 1.2 to 1.4: Ballou, Rahardja and Sakai, *Selected
   country circuity factors for road travel distance estimation*, Transportation Research
   Part A 36(9), 2002; Boscoe, Henry and Zdeb, *A nationwide comparison of driving
   distance versus straight-line distance to hospitals*, The Professional Geographer
   64(2), 2012 (about 1.4 across the United States, higher for short trips). 1.3 is the
   middle of that band.
3. Travel time is that distance at the scenario's average speed, rounded up to whole
   minutes.

The factor is an average, so any single leg can be shorter or longer by road. The lines
on the map are straight connections between stops, not the roads a truck would take. A
real road-distance service could replace `road_km` later without touching the solvers.

The solvers work on integers, so the cost matrix is whole metres and the time matrix is
whole minutes. The response reports kilometres and hours, plus the straight-line total so
the factor is visible.

## Road regions

A truck cannot drive from Shenzhen to Atlanta, so the depot and every destination must be
in one road-connected region (`ROAD_REGIONS` in `places.py`):

| Region | Countries in the catalogue |
|---|---|
| North America | USA, Canada |
| Europe | UK (through the Channel Tunnel), Germany, Netherlands, Norway, Poland |
| Mainland Asia | China (including Hong Kong), Singapore, Thailand |
| Japan | Japan |

A destination outside the depot's region, a stop whose round trip alone exceeds the route
limit, or a fleet too small for the total load is refused with a message naming the
problem, before the solver runs.

## API

- `GET /api/v1/routing/places/model` - road factor and its rationale, road regions,
  example-scenario defaults, request caps, and the example plan.
- `POST /api/v1/routing/places/solve` - `{depot_id, stop_ids, scenario, method,
  time_limit_seconds}` in; routes in km and hours, validated, out.

Tests: `backend/tests/test_real_place_routing.py` checks the distance function on known
city pairs (London-Paris, New York-Los Angeles, Sydney-Melbourne) and solves the example
plan end to end on the committed catalogue database, recomputing every route's length
from the catalogue's own coordinates.
