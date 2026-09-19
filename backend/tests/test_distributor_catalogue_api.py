"""
The real component / distributor / offer catalogue, served read-only.

Runs against the committed `backend/supply_chain.db` (the file Render ships), not a
fixture: the point is that the data the live app serves loads, has the documented
counts and shapes, and puts every distributor where its own label says it is.

The database is tracked in git, so a missing file is a broken checkout and FAILS
here; it is never a reason to skip.
"""

import json
import math
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import catalogue_provenance as prov
from app.core.database import get_db
from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_ROOT / "supply_chain.db"
CENTROIDS_PATH = BACKEND_ROOT / "seeds" / "data" / "distributor_city_centroids.json"
REPO_ROOT = BACKEND_ROOT.parent

# The documented snapshot (docs/DATA_PROVENANCE.md section 1).
N_COMPONENTS = 791
N_DISTRIBUTORS = 92
N_OFFERS = 8176
N_DOMESTIC = 35
N_INTERNATIONAL = 56
# One distributor in the snapshot has no verifiable location and is served with
# NULL coordinates (seeds/seed_db.py::UNLOCATED_DISTRIBUTORS).
UNLOCATED = {"VNN Services"}


def _haversine_km(lat1, lng1, lat2, lng2):
    r = math.radians
    h = (
        math.sin(r(lat2 - lat1) / 2) ** 2
        + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lng2 - lng1) / 2) ** 2
    )
    return 6371.0 * 2 * math.asin(math.sqrt(h))


@pytest.fixture(scope="module")
def real_client():
    assert DB_PATH.exists(), f"{DB_PATH} is tracked in git and must exist"
    engine = create_engine(
        f"sqlite:///file:{DB_PATH}?mode=ro&uri=true",
        connect_args={"check_same_thread": False},
    )
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
        # Not entered as a context manager: the lifespan (feeds, warm-up) is not
        # needed to read the catalogue.
        yield TestClient(app)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        engine.dispose()


@pytest.fixture(scope="module")
def all_distributors(real_client):
    resp = real_client.get("/api/v1/distributors")
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture(scope="module")
def distributors(all_distributors):
    """The located ones: what a map plots."""
    return [d for d in all_distributors if d["name"] not in UNLOCATED]


# ── Seed loads with the documented counts ─────────────────────────────────────


def test_seed_database_has_the_documented_counts():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("components", "distributors", "distributor_offers")
        }
        orphans = conn.execute(
            "SELECT COUNT(*) FROM distributor_offers o "
            "LEFT JOIN components c ON c.id = o.component_id "
            "LEFT JOIN distributors d ON d.id = o.distributor_id "
            "WHERE c.id IS NULL OR d.id IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert counts == {
        "components": N_COMPONENTS,
        "distributors": N_DISTRIBUTORS,
        "distributor_offers": N_OFFERS,
    }
    assert orphans == 0, "every offer must point at a real component and distributor"


def test_stats_endpoint_matches_the_seed(real_client):
    stats = real_client.get("/api/v1/components/stats").json()
    assert stats["total_components"] == N_COMPONENTS
    assert stats["total_distributors"] == N_DISTRIBUTORS
    assert stats["total_offers"] == N_OFFERS
    assert stats["domestic_distributors"] == N_DOMESTIC
    assert stats["international_distributors"] == N_INTERNATIONAL
    assert stats["unlocated_distributors"] == len(UNLOCATED)


# ── Coordinates are real ──────────────────────────────────────────────────────


def test_unknown_locations_are_null_never_invented(all_distributors):
    """The seeder used to plot an unmappable distributor in San Francisco as city "Unknown"."""
    from seeds.seed_db import DISTRIBUTOR_LOCATIONS, UNLOCATED_DISTRIBUTORS

    assert set(UNLOCATED_DISTRIBUTORS) == UNLOCATED
    assert not UNLOCATED & set(DISTRIBUTOR_LOCATIONS)
    unlocated = [d for d in all_distributors if d["latitude"] is None]
    assert {d["name"] for d in unlocated} == UNLOCATED
    for d in unlocated:
        assert d["longitude"] is None and d["city"] is None and d["state"] is None
        assert d["country"] is None and d["is_domestic"] is None
        assert d["total_offers"] == 0, "an unlocated distributor must not carry offers a route would need"


def test_every_distributor_has_valid_coordinates(distributors):
    assert len(distributors) == N_DISTRIBUTORS - len(UNLOCATED)
    for d in distributors:
        assert -90 <= d["latitude"] <= 90, d
        assert -180 <= d["longitude"] <= 180, d
        # (0, 0) is in the Gulf of Guinea: the classic "no location" placeholder.
        assert (d["latitude"], d["longitude"]) != (0.0, 0.0), d
        assert d["city"] and d["country"], d
        assert d["city"] != "Unknown", d
        assert d["is_domestic"] == (d["country"] == "USA"), d


def test_every_distributor_sits_in_its_own_labelled_city(distributors):
    """Each coordinate is held to an independent OpenStreetMap point for its label.

    Seven distributors failed this on 2026-09-19 by 33-460 km (Schukat was plotted in
    Bavaria while headquartered in Monheim am Rhein, NRW). The reference file is
    generated by seeds/geocode_distributor_cities.py and committed.
    """
    ref = json.loads(CENTROIDS_PATH.read_text())
    assert "OpenStreetMap" in ref["source"]
    centroids = ref["centroids"]
    off = []
    for d in distributors:
        key = f"{d['city']}|{d['state']}|{d['country']}"
        assert key in centroids, f"no OpenStreetMap reference for {d['name']} ({key})"
        c = centroids[key]
        km = _haversine_km(d["latitude"], d["longitude"], c["lat"], c["lng"])
        if km > prov.COORDINATE_TOLERANCE_KM:
            off.append(f"{d['name']}: {km:.0f} km from {key}")
    assert not off, "coordinates do not match their city label:\n" + "\n".join(off)


def test_seed_dictionary_and_database_agree(distributors):
    from seeds.seed_db import DISTRIBUTOR_LOCATIONS

    for d in distributors:
        loc = DISTRIBUTOR_LOCATIONS[d["name"]]
        assert (d["latitude"], d["longitude"]) == (loc["lat"], loc["lng"]), d["name"]
        assert (d["city"], d["state"], d["country"]) == (loc["city"], loc["state"], loc["country"]), d["name"]


def test_shares_location_with_counts_stacked_markers(distributors):
    by_point = {}
    for d in distributors:
        by_point.setdefault((d["latitude"], d["longitude"]), []).append(d)
    for group in by_point.values():
        for d in group:
            assert d["shares_location_with"] == len(group) - 1, d["name"]


# ── Distributor endpoints ─────────────────────────────────────────────────────


def test_distributor_list_shape_and_order(all_distributors):
    distributors = all_distributors
    assert len(distributors) == N_DISTRIBUTORS
    keys = {
        "id", "name", "latitude", "longitude", "city", "state", "country",
        "is_domestic", "total_offers", "total_stock", "shares_location_with",
    }
    assert all(set(d) == keys for d in distributors)
    assert sum(d["total_offers"] for d in distributors) == N_OFFERS
    offers = [d["total_offers"] for d in distributors]
    assert offers == sorted(offers, reverse=True)
    assert distributors[0]["name"] == "DigiKey"


def test_distributor_filters(real_client, distributors):
    get = lambda **p: real_client.get("/api/v1/distributors", params=p)  # noqa: E731

    located = get(located_only=True).json()
    assert {d["name"] for d in located} == {d["name"] for d in distributors}

    domestic = get(domestic_only=True).json()
    assert len(domestic) == N_DOMESTIC and all(d["country"] == "USA" for d in domestic)

    china = get(country="China").json()
    assert china and all(d["country"] == "China" for d in china)

    with_offers = get(has_offers=True).json()
    assert with_offers and all(d["total_offers"] > 0 for d in with_offers)
    assert len(with_offers) == sum(1 for d in distributors if d["total_offers"] > 0)

    # Continental US viewport.
    box = get(min_lat=24, max_lat=50, min_lng=-125, max_lng=-66).json()
    assert box and all(24 <= d["latitude"] <= 50 and -125 <= d["longitude"] <= -66 for d in box)
    assert {d["name"] for d in box} >= {"DigiKey", "Mouser", "Arrow Electronics"}
    assert get(min_lat=10, max_lat=5).status_code == 422
    assert get(min_lat=91).status_code == 422


def test_distributors_filtered_by_component_are_exactly_its_offers(real_client):
    comp = real_client.get("/api/v1/components/1").json()
    expected = {o["distributor_id"] for o in comp["offers"]}
    got = real_client.get("/api/v1/distributors", params={"component_id": 1}).json()
    assert expected and {d["id"] for d in got} == expected
    assert real_client.get("/api/v1/distributors", params={"component_id": 999999}).status_code == 404


def test_distributors_filtered_by_category(real_client):
    category = real_client.get("/api/v1/components/categories").json()[0]["name"]
    got = real_client.get("/api/v1/distributors", params={"category": category}).json()
    assert got
    for d in got[:3]:
        carried = real_client.get(
            f"/api/v1/distributors/{d['id']}/components", params={"category": category}
        ).json()
        assert carried and all(c["category"] == category for c in carried)


def test_distributor_detail_and_carried_components(real_client, distributors):
    top = distributors[0]
    detail = real_client.get(f"/api/v1/distributors/{top['id']}").json()
    assert detail["name"] == top["name"]
    assert (detail["latitude"], detail["longitude"]) == (top["latitude"], top["longitude"])
    assert 0 < len(detail["top_components"]) <= 20

    carried = real_client.get(f"/api/v1/distributors/{top['id']}/components").json()
    assert len(carried) == top["total_offers"]
    assert sum(c["stock"] for c in carried) == top["total_stock"]
    assert all(c["price"] > 0 for c in carried)
    stocks = [c["stock"] for c in carried]
    assert stocks == sorted(stocks, reverse=True)

    by_price = real_client.get(
        f"/api/v1/distributors/{top['id']}/components", params={"sort_by": "price", "limit": 5}
    ).json()
    assert len(by_price) == 5
    assert [c["price"] for c in by_price] == sorted(c["price"] for c in by_price)

    assert real_client.get("/api/v1/distributors/999999").status_code == 404
    assert real_client.get("/api/v1/distributors/999999/components").status_code == 404


# ── Component endpoints ───────────────────────────────────────────────────────


def test_component_list_with_offers_covers_every_offer(real_client):
    rows = []
    for skip in range(0, N_COMPONENTS, 1000):
        rows += real_client.get(
            "/api/v1/components", params={"include_offers": True, "skip": skip, "limit": 1000}
        ).json()
    assert len(rows) == N_COMPONENTS
    assert len({r["id"] for r in rows}) == N_COMPONENTS
    assert sum(len(r["offers"]) for r in rows) == N_OFFERS
    for r in rows:
        assert r["num_offers"] == len(r["offers"])
        prices = [o["price"] for o in r["offers"]]
        assert prices == sorted(prices)
        for o in r["offers"]:
            assert -90 <= o["distributor_latitude"] <= 90
            assert -180 <= o["distributor_longitude"] <= 180


def test_component_list_without_offers_stays_lean(real_client):
    rows = real_client.get("/api/v1/components", params={"limit": 5}).json()
    assert len(rows) == 5 and all(r["offers"] is None for r in rows)


def test_components_filtered_by_distributor(real_client, distributors):
    top = distributors[0]
    rows = real_client.get("/api/v1/components", params={"distributor_id": top["id"]}).json()
    carried = real_client.get(f"/api/v1/distributors/{top['id']}/components").json()
    assert rows and {r["id"] for r in rows} == {c["component_id"] for c in carried}
    assert real_client.get("/api/v1/components", params={"distributor_id": 999999}).status_code == 404


def test_component_offers_carry_distributor_coordinates(real_client, distributors):
    by_id = {d["id"]: d for d in distributors}
    offers = real_client.get("/api/v1/components/1/offers").json()
    assert offers
    for o in offers:
        d = by_id[o["distributor_id"]]
        assert (o["distributor_latitude"], o["distributor_longitude"]) == (d["latitude"], d["longitude"])


# ── Provenance is served, and honest ──────────────────────────────────────────


def test_catalogue_provenance_says_frozen_2024_snapshot(real_client):
    p = real_client.get("/api/v1/catalogue/provenance").json()
    assert p["is_live"] is False
    assert p["snapshot_year"] == 2024
    assert "Not a live feed" in p["summary"] and "2024" in p["summary"]
    assert p["counts"] == {"components": N_COMPONENTS, "distributors": N_DISTRIBUTORS, "offers": N_OFFERS}
    assert p["license"] == "CC-BY-4.0"
    assert p["coordinates"]["precision"] == "city"
    assert p["coordinates"]["tolerance_km"] == prov.COORDINATE_TOLERANCE_KM
    assert p["coordinates"]["unlocated_distributors"] == len(UNLOCATED)
    assert (REPO_ROOT / p["documentation"]["path"]).exists()


def test_provenance_doc_matches_the_served_coordinate_facts(real_client):
    """The doc must quote the shared-point numbers the API computes, not stale ones."""
    p = real_client.get("/api/v1/catalogue/provenance").json()
    doc = (REPO_ROOT / p["documentation"]["path"]).read_text()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        shenzhen = conn.execute("SELECT COUNT(*) FROM distributors WHERE city = 'Shenzhen'").fetchone()[0]
    finally:
        conn.close()
    assert f"{shenzhen} distributors share one Shenzhen city point" in doc
    assert f"{p['coordinates']['distinct_points']} distinct points" in doc
    assert "GET /api/v1/catalogue/provenance" in doc
    for name in UNLOCATED:
        assert name in doc
