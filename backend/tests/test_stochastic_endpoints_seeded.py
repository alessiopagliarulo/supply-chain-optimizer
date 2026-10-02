"""
The two sourcing-model endpoints against the committed, seeded database - the file
Render ships - opened READ-ONLY so the suite can never write to it (the frontier
endpoint's result cache is best-effort and simply misses here).

  POST /api/v1/stochastic/frontier     the HTTP contract the Sourcing Risk page reads:
                                       the documented field names on success, and the
                                       documented status codes for a BOM that cannot be
                                       sourced, a component that does not exist and a
                                       request that breaks the schema.
  GET  /api/v1/stochastic/calibration  the disruption probabilities the model was given,
                                       so the page can state its own assumptions.

`tests/test_stochastic_api.py` (restored from archive/sourcing-v1) covers the same two
endpoints in depth on a synthetic six-distributor network, including the over-budget
503. This file is the one that proves they work on the real catalogue.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.graph as graph_state_module
from app.core.database import get_db
from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_ROOT / "supply_chain.db"

FRONTIER = "/api/v1/stochastic/frontier"
CALIBRATION = "/api/v1/stochastic/calibration"

# pcb_power_supply from the archived benchmark catalogue, at 100 boards: four real parts
# (component ids in the seeded catalogue) served by a six-distributor pool.
PCB_POWER_SUPPLY = [
    {"component_id": 429, "quantity": 200},   # LM317DCY
    {"component_id": 431, "quantity": 100},   # TPS767D325PWP
    {"component_id": 457, "quantity": 200},   # UA78M33CDCY
    {"component_id": 442, "quantity": 100},   # OPA861ID
]

# DFR1063: every offer for it in the seeded catalogue together holds 232 units.
SHORT_STOCK_COMPONENT = 4

POINT_FIELDS = {
    "lambda", "expected_cost_usd", "cvar_95_usd", "var_95_usd", "tail_premium_usd",
    "first_stage_cost_usd", "expected_recourse_usd", "n_suppliers", "supplier_ids",
    "solver_status", "mip_gap_pct", "solve_seconds", "n_variables", "dominated",
    "n_atoms_in_tail",
}
TOP_LEVEL_FIELDS = {
    "cached", "frontier", "partial", "unsolved_points", "frontier_shape",
    "recommendation", "instance", "calibration", "scenarios", "solver", "caveats",
}


@pytest.fixture(scope="module")
def seeded_client():
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

    previous_override = app.dependency_overrides.get(get_db)
    previous_graph = graph_state_module.get_graph_state()
    app.dependency_overrides[get_db] = _override
    # Build the betweenness from THIS database, not from whatever an earlier test left.
    graph_state_module.set_graph_state(None)
    try:
        yield TestClient(app)
    finally:
        graph_state_module.set_graph_state(previous_graph)
        if previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_override
        engine.dispose()


@pytest.fixture(scope="module")
def pcb_frontier(seeded_client):
    r = seeded_client.post(FRONTIER, json={"items": PCB_POWER_SUPPLY})
    assert r.status_code == 200, r.text
    return r.json()


def test_frontier_on_the_seeded_catalogue_has_the_documented_shape(pcb_frontier):
    assert TOP_LEVEL_FIELDS <= set(pcb_frontier)
    assert pcb_frontier["frontier"], "a solved request returns at least one point"
    for point in pcb_frontier["frontier"]:
        assert POINT_FIELDS <= set(point), POINT_FIELDS - set(point)
        assert point["solver_status"] in ("OPTIMAL", "FEASIBLE")
        assert point["n_suppliers"] == len(point["supplier_ids"]) >= 1
        assert point["cvar_95_usd"] >= point["expected_cost_usd"] > 0


def test_frontier_reports_what_it_solved_and_how(pcb_frontier):
    solver = pcb_frontier["solver"]
    assert solver["engine"] == "OR-Tools CP-SAT"
    assert solver["points_solved"] + solver["points_unsolved"] == solver["points_requested"]
    assert solver["max_time_in_seconds_per_point"] > 0
    assert solver["sweep_time_budget_s"] > 0
    assert pcb_frontier["calibration"]["n_distributors_in_pool"] >= 2
    assert pcb_frontier["instance"]["n_lines"] == len(PCB_POWER_SUPPLY)
    assert pcb_frontier["instance"]["total_units"] == sum(i["quantity"] for i in PCB_POWER_SUPPLY)
    assert pcb_frontier["scenarios"]["kind"] in ("exact", "saa")
    assert pcb_frontier["caveats"]


def test_a_bom_the_catalogue_cannot_supply_is_a_422_not_a_500(seeded_client):
    r = seeded_client.post(FRONTIER, json={
        "items": [{"component_id": SHORT_STOCK_COMPONENT, "quantity": 5_000}],
    })
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]


def test_an_unknown_component_is_a_404(seeded_client):
    r = seeded_client.post(FRONTIER, json={
        "items": [{"component_id": 10_000_000, "quantity": 10}],
    })
    assert r.status_code == 404, r.text
    assert "10000000" in r.json()["detail"]


@pytest.mark.parametrize("body", [
    {"items": []},
    {"items": [{"component_id": 429, "quantity": 0}]},
    {"items": [{"component_id": 429, "quantity": 100_001}]},
    {"items": [{"component_id": 429, "quantity": 10}], "horizon_days": 0},
])
def test_a_request_outside_the_schema_is_a_422(seeded_client, body):
    assert seeded_client.post(FRONTIER, json=body).status_code == 422


def test_an_unknown_strategy_is_a_400(seeded_client):
    r = seeded_client.post(FRONTIER, json={
        "items": PCB_POWER_SUPPLY, "strategy": "no-such-strategy",
    })
    assert r.status_code == 400, r.text


def test_calibration_publishes_every_probability_with_its_source(seeded_client):
    r = seeded_client.get(CALIBRATION)
    assert r.status_code == 200, r.text
    body = r.json()

    params = body["parameters"]
    assert 0 < params["base_annual_prob"] < 1
    assert 0 < params["base_horizon_prob"] < params["base_annual_prob"]
    assert params["horizon_days"] == 60
    assert "McKinsey" in body["base_rate_source"]["citation"]

    dists = body["distributors"]
    assert dists, "the seeded catalogue has distributors"
    for d in dists:
        assert 0 < d["p_disruption_over_horizon"] <= params["max_failure_prob"]
        assert d["distributor_name"]


def test_calibration_rejects_parameters_outside_their_range(seeded_client):
    assert seeded_client.get(CALIBRATION, params={"base_annual_prob": 1.5}).status_code == 400
    assert seeded_client.get(CALIBRATION, params={"horizon_days": 0}).status_code == 400
    assert seeded_client.get(CALIBRATION, params={"centrality_spread": 0.5}).status_code == 400
