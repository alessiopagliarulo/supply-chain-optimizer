"""
Route planning between real places: the distance function and an end-to-end solve.

The distance model is great-circle (haversine) distance times ``ROAD_FACTOR``
(``app/vrp/geo.py``, docs/REAL_PLACE_ROUTING.md). The end-to-end tests run
``POST /routing/places/solve`` against the committed catalogue database - the file
Render serves - so the coordinates are the real distributor locations, read by id.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.main import app
from app.vrp.geo import (
    EARTH_RADIUS_KM,
    ROAD_FACTOR,
    great_circle_km,
    road_distance_matrix_m,
    road_km,
    travel_time_matrix_min,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_ROOT / "supply_chain.db"
SOLVE = "/api/v1/routing/places/solve"
MODEL = "/api/v1/routing/places/model"
CANDIDATES = "/api/v1/routing/places/candidates"

# ── The distance function on known city pairs ────────────────────────────────

LONDON = (51.5074, -0.1278)
PARIS = (48.8566, 2.3522)
NEW_YORK = (40.7128, -74.0060)
LOS_ANGELES = (34.0522, -118.2437)
SYDNEY = (-33.8688, 151.2093)
MELBOURNE = (-37.8136, 144.9631)

#: Published great-circle distances between city centres, in km. Each is the figure
#: quoted by standard great-circle calculators for these city-centre coordinates.
KNOWN_PAIRS = [
    ("London-Paris", LONDON, PARIS, 343.5),
    ("New York-Los Angeles", NEW_YORK, LOS_ANGELES, 3935.7),
    ("Sydney-Melbourne", SYDNEY, MELBOURNE, 713.4),
]


@pytest.mark.parametrize("name,a,b,km", KNOWN_PAIRS, ids=[p[0] for p in KNOWN_PAIRS])
def test_great_circle_matches_known_city_pairs(name, a, b, km):
    assert great_circle_km(a, b) == pytest.approx(km, rel=0.002), name
    assert great_circle_km(b, a) == pytest.approx(great_circle_km(a, b))


def test_great_circle_edge_cases():
    assert great_circle_km(LONDON, LONDON) == 0.0
    # Antipodal points are half the circumference apart.
    assert great_circle_km((0.0, 0.0), (0.0, 180.0)) == pytest.approx(math.pi * EARTH_RADIUS_KM)
    # One degree of latitude is about 111.2 km everywhere.
    assert great_circle_km((10.0, 20.0), (11.0, 20.0)) == pytest.approx(111.19, abs=0.01)


def test_road_distance_is_great_circle_times_the_documented_factor():
    assert ROAD_FACTOR == 1.3, "the factor is documented in docs/REAL_PLACE_ROUTING.md and on the Route Plan page"
    for _, a, b, _ in KNOWN_PAIRS:
        assert road_km(a, b) == pytest.approx(great_circle_km(a, b) * 1.3)
    # London-Paris: 343.6 km straight, so about 446.6 km by the road model.
    assert road_km(LONDON, PARIS) == pytest.approx(446.6, abs=0.3)


def test_matrices_are_integer_metres_and_whole_minutes():
    m = road_distance_matrix_m([LONDON, PARIS, LONDON])
    assert m[0][0] == 0 and m[0][2] == 0
    assert m[0][1] == m[1][0] == round(road_km(LONDON, PARIS) * 1000)
    assert all(isinstance(v, int) for row in m for v in row)
    t = travel_time_matrix_min(m, speed_kmh=60)
    # 446.6 km at 60 km/h is 446.6 minutes, rounded up to whole minutes.
    assert t[0][1] == math.ceil(m[0][1] / 1000)
    with pytest.raises(ValueError):
        travel_time_matrix_min(m, speed_kmh=0)


# ── End to end on the real catalogue ─────────────────────────────────────────


@pytest.fixture(scope="module")
def real_client():
    assert DB_PATH.exists(), f"{DB_PATH} is tracked in git and must exist"
    engine = create_engine(f"sqlite:///file:{DB_PATH}?mode=ro&uri=true", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)

    def _override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app), engine
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        engine.dispose()


def _located(engine, country=None):
    q = "SELECT id, name, latitude, longitude, country FROM distributors WHERE latitude IS NOT NULL"
    params = {}
    if country:
        q += " AND country = :c"
        params["c"] = country
    with engine.connect() as conn:
        return {r.id: r for r in conn.execute(text(q + " ORDER BY id"), params)}


def test_the_model_endpoint_states_the_factor_and_an_example_from_the_catalogue(real_client):
    client, engine = real_client
    body = client.get(MODEL).json()
    assert body["road_factor"] == ROAD_FACTOR
    assert "1.3" in body["road_factor_rationale"]
    # Facts about the world, typed here rather than read back from the constant the API
    # serves: a table that put the UK and China in one region must fail.
    regions = body["road_regions"]
    assert regions["USA"] == regions["Canada"] == "North America"
    assert regions["UK"] == regions["Germany"] == regions["Poland"] == "Europe"
    assert regions["China"] == regions["Singapore"] == "Mainland Asia"
    assert len({regions["USA"], regions["UK"], regions["China"], regions["Japan"]}) == 4
    example = body["example"]
    uk = _located(engine, "UK")
    assert example["depot_id"] in uk and uk[example["depot_id"]].name == "Farnell"
    assert set(example["stop_ids"]) == set(uk) - {example["depot_id"]}


@pytest.mark.parametrize("method", ["auto", "ortools", "clarke_wright"])
def test_a_real_place_plan_solves_end_to_end(real_client, method):
    """The example plan (Farnell, Leeds, to every other UK distributor) on real coordinates."""
    client, engine = real_client
    example = client.get(MODEL).json()["example"]
    r = client.post(SOLVE, json={**example, "method": method, "time_limit_seconds": 1})
    assert r.status_code == 200, r.text
    plan = r.json()

    assert plan["feasible"] is True and plan["violations"] == []
    assert plan["depot"]["id"] == example["depot_id"]
    assert [s["id"] for s in plan["stops"]] == example["stop_ids"]

    # Every stop is visited exactly once, and every route respects the example scenario.
    visited = sorted(i for route in plan["routes"] for i in route["stops"])
    assert visited == list(range(len(plan["stops"])))
    scenario = plan["scenario"]
    assert plan["vehicles_used"] == len(plan["routes"]) <= scenario["num_vehicles"]
    for route in plan["routes"]:
        assert route["load"] == scenario["stop_load"] * len(route["stops"]) <= scenario["vehicle_capacity"]
        assert route["duration_hours"] <= scenario["max_route_hours"]

    # Distances are recomputed here from the catalogue's own coordinates: the response
    # must be the great-circle length of each drawn route times the road factor.
    places = _located(engine)
    depot = places[example["depot_id"]]
    for route in plan["routes"]:
        path = [depot] + [places[plan["stops"][i]["id"]] for i in route["stops"]] + [depot]
        straight = sum(
            great_circle_km((a.latitude, a.longitude), (b.latitude, b.longitude)) for a, b in zip(path, path[1:], strict=False)
        )
        assert route["straight_line_km"] == pytest.approx(straight, rel=1e-9)
        # Legs are rounded to whole metres before summing.
        assert route["distance_km"] == pytest.approx(straight * ROAD_FACTOR, abs=0.001 * len(path))
    assert plan["total_km"] == pytest.approx(sum(r["distance_km"] for r in plan["routes"]))

    _assert_timeline_matches_the_catalogue(plan, places)


def _assert_timeline_matches_the_catalogue(plan, places):
    """Replay every truck in its REPORTED visiting order from the catalogue's coordinates.

    Each leg is road distance in whole metres, driven at the scenario speed and rounded
    up to whole minutes (docs/REAL_PLACE_ROUTING.md); every stop is open all day, so a
    truck never waits. The replayed arrival at each stop and the return to the depot must
    equal what the API reports, in hours. A reversed stop order, a duration in the wrong
    unit, or service starts in minutes instead of hours all break this equality.
    """
    sc = plan["scenario"]
    metres_per_minute = sc["speed_kmh"] * 1000 / 60
    depot = places[plan["depot"]["id"]]
    at = lambda r: (r.latitude, r.longitude)  # noqa: E731
    leg = lambda a, b: math.ceil(round(road_km(at(a), at(b)) * 1000) / metres_per_minute)  # noqa: E731
    for route in plan["routes"]:
        assert len(route["service_start_hours"]) == len(route["stops"])
        t, prev = 0, depot
        for k, i in enumerate(route["stops"]):
            here = places[plan["stops"][i]["id"]]
            t += leg(prev, here)
            assert route["service_start_hours"][k] == pytest.approx(t / 60, abs=1e-9), (route, k)
            t += sc["service_minutes"]
            prev = here
        t += leg(prev, depot)
        assert route["duration_hours"] == pytest.approx(t / 60, abs=1e-9), route
        # And the bounds a reader can check without replaying anything.
        starts = route["service_start_hours"]
        assert starts == sorted(starts)
        assert route["duration_hours"] >= route["distance_km"] / sc["speed_kmh"]
        assert route["duration_hours"] >= starts[-1] + sc["service_minutes"] / 60


def test_a_continental_plan_uses_the_longer_route_limit(real_client):
    """North America with a multi-day limit: many stops, the OR-Tools path."""
    client, engine = real_client
    depot = next(r for r in _located(engine, "USA").values() if r.name == "Mouser")
    stops = [i for i, r in _located(engine, "USA").items() if i != depot.id][:20]
    body = {
        "depot_id": depot.id,
        "stop_ids": stops,
        "scenario": {"stop_load": 2, "vehicle_capacity": 10, "num_vehicles": 6, "max_route_hours": 120},
        "method": "ortools",
        "time_limit_seconds": 1,
    }
    r = client.post(SOLVE, json=body)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["feasible"] is True
    assert sorted(i for route in plan["routes"] for i in route["stops"]) == list(range(20))
    _assert_timeline_matches_the_catalogue(plan, _located(engine))


def test_destinations_off_the_depots_road_region_are_refused(real_client):
    client, engine = real_client
    farnell = next(r for r in _located(engine, "UK").values() if r.name == "Farnell")
    worldway = next(r for r in _located(engine, "China").values() if r.name == "Worldway Electronics")
    r = client.post(SOLVE, json={"depot_id": farnell.id, "stop_ids": [worldway.id]})
    assert r.status_code == 422
    assert "Worldway Electronics" in r.json()["detail"] and "by road" in r.json()["detail"]


def test_a_stop_beyond_the_route_limit_is_named(real_client):
    client, engine = real_client
    usa = _located(engine, "USA")
    digikey = next(r for r in usa.values() if r.name == "DigiKey")
    jameco = next(r for r in usa.values() if r.name == "Jameco")
    r = client.post(SOLVE, json={"depot_id": digikey.id, "stop_ids": [jameco.id]})
    assert r.status_code == 422
    assert "Jameco" in r.json()["detail"] and "route limit" in r.json()["detail"]


def test_unlocated_unknown_and_repeated_places_are_refused(real_client):
    client, engine = real_client
    with engine.connect() as conn:
        unlocated = conn.execute(text("SELECT id FROM distributors WHERE latitude IS NULL")).scalar_one()
    farnell = next(r for r in _located(engine, "UK").values() if r.name == "Farnell")
    assert client.post(SOLVE, json={"depot_id": farnell.id, "stop_ids": [unlocated]}).status_code == 422
    assert client.post(SOLVE, json={"depot_id": farnell.id, "stop_ids": [999999]}).status_code == 404
    assert client.post(SOLVE, json={"depot_id": farnell.id, "stop_ids": [farnell.id]}).status_code == 422
    others = [i for i in _located(engine, "UK") if i != farnell.id]
    assert client.post(SOLVE, json={"depot_id": farnell.id, "stop_ids": [others[0], others[0]]}).status_code == 422


def test_a_fleet_that_cannot_split_loads_is_refused_before_solving(real_client):
    """35 pallets fit in 3 trucks of 12 on paper, but a 7-pallet stop cannot be split, so a
    truck takes one stop and 3 trucks cannot visit 5. The audit reproduced a silent empty
    plan here; it must be a 422 naming the problem."""
    client, engine = real_client
    uk = _located(engine, "UK")
    farnell = next(i for i, r in uk.items() if r.name == "Farnell")
    stops = [i for i in uk if i != farnell][:5]
    body = {
        "depot_id": farnell,
        "stop_ids": stops,
        "scenario": {"stop_load": 7, "vehicle_capacity": 12, "num_vehicles": 3},
        "method": "ortools",
        "time_limit_seconds": 1,
    }
    r = client.post(SOLVE, json=body)
    assert r.status_code == 422, r.text
    assert "at most 3" in r.json()["detail"] and "add trucks or capacity" in r.json()["detail"]
    # One more truck makes it plannable.
    body["scenario"]["num_vehicles"] = 5
    assert client.post(SOLVE, json=body).json()["feasible"] is True


def test_the_solve_does_not_hold_a_database_connection(real_client, monkeypatch):
    """The solve can run for the whole time limit; holding a pooled connection through it
    lets a burst of solves starve every catalogue read. None may be checked out."""
    import app.api.routing_places as routing_places

    client, engine = real_client
    seen = []
    real_solve = routing_places.solve

    def spy(instance, **kw):
        seen.append(engine.pool.checkedout())
        return real_solve(instance, **kw)

    monkeypatch.setattr(routing_places, "solve", spy)
    example = client.get(MODEL).json()["example"]
    assert client.post(SOLVE, json=example).status_code == 200
    assert seen == [0]


def test_out_of_range_ids_are_a_422_not_a_crash(real_client):
    client, _ = real_client
    assert client.post(SOLVE, json={"depot_id": 10**20, "stop_ids": [1]}).status_code == 422
    assert client.post(SOLVE, json={"depot_id": 1, "stop_ids": [10**20]}).status_code == 422
    assert client.post(CANDIDATES, json={"depot_id": 10**20}).status_code == 422


def test_candidates_are_the_depots_region_measured_like_a_plan(real_client):
    client, engine = real_client
    uk = _located(engine, "UK")
    farnell = next(r for r in uk.values() if r.name == "Farnell")
    body = client.post(CANDIDATES, json={"depot_id": farnell.id}).json()
    assert body["region"] == "Europe"
    europe = {i for i, r in _located(engine).items() if r.country in ("UK", "Germany", "Netherlands", "Norway", "Poland")}
    assert {c["place"]["id"] for c in body["candidates"]} == europe - {farnell.id}
    places = _located(engine)
    kms = [c["road_km"] for c in body["candidates"]]
    assert kms == sorted(kms)
    for c in body["candidates"]:
        p = places[c["place"]["id"]]
        assert c["road_km"] == pytest.approx(road_km((farnell.latitude, farnell.longitude), (p.latitude, p.longitude)))
        assert c["fits_route_limit"] == (c["round_trip_hours"] <= 14)
    # The suggestion fits the default fleet (4 trucks x 12 pallets / 3 per stop = 16 stops)
    # and the route limit, and every one of them solves.
    fits = {c["place"]["id"] for c in body["candidates"] if c["fits_route_limit"]}
    assert body["suggested"] and set(body["suggested"]) <= fits and len(body["suggested"]) <= 16
    r = client.post(SOLVE, json={"depot_id": farnell.id, "stop_ids": body["suggested"]})
    assert r.status_code == 200 and r.json()["feasible"] is True


def test_a_depot_with_nothing_in_range_gets_no_suggestion_but_keeps_its_candidates(real_client):
    """Arrow (Centennial, Colorado) has road-reachable distributors but none within the
    default 14 h round trip. The planner must be told that, not handed an empty page."""
    client, engine = real_client
    arrow = next(r for r in _located(engine, "USA").values() if r.name == "Arrow Electronics")
    body = client.post(CANDIDATES, json={"depot_id": arrow.id}).json()
    assert body["candidates"] and body["suggested"] == []
    assert not any(c["fits_route_limit"] for c in body["candidates"])
    longer = client.post(CANDIDATES, json={"depot_id": arrow.id, "scenario": {"max_route_hours": 48}}).json()
    assert longer["suggested"]


def test_a_fleet_too_small_for_the_load_is_refused_before_solving(real_client):
    client, engine = real_client
    example = client.get(MODEL).json()["example"]
    body = {**example, "scenario": {"stop_load": 5, "vehicle_capacity": 5, "num_vehicles": 1}}
    r = client.post(SOLVE, json=body)
    assert r.status_code == 422 and "add trucks or capacity" in r.json()["detail"]
