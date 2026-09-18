"""
RED/GREEN tests for Phase 6 Scenario API endpoints.

Tasks 1-5: Distributor failure, geopolitical risk, delivery-target scenarios.
Follows TDD RED → GREEN → REFACTOR cycle.
"""
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

from app.models.scenario import ScenarioCache
from app.models.distributor import Distributor
from app.models.component import Component, DistributorOffer
from app.main import app
from app.core.database import get_db


# ────────────────────────────────────────────────────────────────────────────
# TASK 1: RED tests for ScenarioCache ORM and Alembic migration
# ────────────────────────────────────────────────────────────────────────────

def _count_monte_carlo(monkeypatch):
    """Count run_monte_carlo calls made through the resilience router.

    Returns a dict whose "n" key is the live call count. Patching the name in
    `app.api.resilience` (not in `app.graph.simulation`) is deliberate: it is
    the binding the endpoint actually calls, so this cannot be fooled by an
    import-alias change.
    """
    import app.api.resilience as _resilience

    calls = {"n": 0}
    real = _resilience.run_monte_carlo

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(_resilience, "run_monte_carlo", counting)
    return calls

def test_scenario_cache_import():
    """Test that ScenarioCache can be imported from app.models.scenario."""
    from app.models.scenario import ScenarioCache
    assert ScenarioCache is not None
    assert hasattr(ScenarioCache, '__tablename__')
    assert ScenarioCache.__tablename__ == 'scenario_cache'


def test_scenario_cache_columns():
    """Test that ScenarioCache has all required columns."""
    from app.models.scenario import ScenarioCache
    required_cols = ['id', 'scenario_type', 'cache_key', 'result_json',
                     'created_at', 'expires_at', 'accessed_at']
    for col in required_cols:
        assert hasattr(ScenarioCache, col), f"Missing column: {col}"


def test_scenario_cache_in_metadata():
    """Test that 'scenario_cache' table is registered in Base.metadata."""
    from app.core.database import Base
    assert 'scenario_cache' in Base.metadata.tables


def test_alembic_migration_0003():
    """Test that Alembic migration file 0003 exists and has correct structure."""
    import importlib.util
    from pathlib import Path
    migration_path = (
        Path(__file__).resolve().parent.parent
        / "migrations" / "versions" / "0003_scenario_cache.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0003",
        str(migration_path),
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == '0003'
    assert migration.down_revision == '0002'
    assert hasattr(migration, 'upgrade')
    assert hasattr(migration, 'downgrade')


def test_models_init_exports_scenario_cache():
    """Test that ScenarioCache is exported from app.models.__init__."""
    from app.models import ScenarioCache
    assert ScenarioCache is not None


# ────────────────────────────────────────────────────────────────────────────
# TASK 2: RED tests for POST /resilience/distributor-failure endpoint
# ────────────────────────────────────────────────────────────────────────────

def test_distributor_failure_accepts_request(db_session):
    """Test POST /api/v1/resilience/distributor-failure accepts valid request."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/distributor-failure",
            json={"distributor_id": 1, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_distributor_failure_response_structure(db_session):
    """Test that distributor-failure response has all required fields."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/distributor-failure",
            json={"distributor_id": 1, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
        data = response.json()

        required_fields = [
            'baseline_cost_usd', 'scenario_cost_usd', 'cost_delta_pct',
            'baseline_eta_days', 'scenario_eta_days', 'eta_delta_days',
            'baseline_risk_score', 'scenario_risk_score', 'risk_delta',
            'baseline_fulfillment_p10', 'baseline_fulfillment_p50', 'baseline_fulfillment_p90',
            'scenario_fulfillment_p10', 'scenario_fulfillment_p50', 'scenario_fulfillment_p90',
            'affected_bom_ids', 'affected_suppliers',
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"
    finally:
        app.dependency_overrides.clear()


def test_distributor_failure_simulation_accuracy(db_session):
    """Test that distributor-failure simulation produces realistic deltas."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/distributor-failure",
            json={"distributor_id": 1, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
        data = response.json()

        assert data['baseline_fulfillment_p10'] <= data['baseline_fulfillment_p50']
        assert data['baseline_fulfillment_p50'] <= data['baseline_fulfillment_p90']
        assert data['scenario_fulfillment_p10'] <= data['scenario_fulfillment_p50']
        assert data['scenario_fulfillment_p50'] <= data['scenario_fulfillment_p90']
    finally:
        app.dependency_overrides.clear()


def test_distributor_failure_caching(db_session, monkeypatch):
    """Test that repeated calls to distributor-failure return cached result."""
    import time
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        _sim_calls = _count_monte_carlo(monkeypatch)
        response1 = client.post(
            "/api/v1/resilience/distributor-failure",
            json={"distributor_id": 1, "bom_component_ids": [1]},
        )
        assert response1.status_code == 200
        data1 = response1.json()

        # Whatever the first (uncached) request cost in simulator calls, the
        # second must add exactly zero. Snapshotting instead of asserting a
        # total keeps this correct even if the endpoint's internal number of
        # simulation passes changes.
        _calls_after_first = _sim_calls["n"]
        assert _calls_after_first > 0, "first request should have run the simulation"

        time.sleep(0.05)

        start = time.time()
        response2 = client.post(
            "/api/v1/resilience/distributor-failure",
            json={"distributor_id": 1, "bom_component_ids": [1]},
        )
        elapsed = (time.time() - start) * 1000
        assert response2.status_code == 200
        data2 = response2.json()

        assert data1 == data2
        # The cache is proven by WORK NOT DONE, not by a stopwatch: a wall-clock
        # budget here measures the CI runner (this assertion read <10ms locally
        # and 196ms on a loaded GitHub runner while the cache was hitting fine).
        # `_sim_calls` counts run_monte_carlo invocations across both requests;
        # a cache miss on the second call would run it again.
        assert _sim_calls["n"] == _calls_after_first, (
            f"second identical request recomputed the scenario: run_monte_carlo ran "
            f"{_sim_calls['n'] - _calls_after_first} extra time(s) — the cache missed"
        )
    finally:
        app.dependency_overrides.clear()


def test_distributor_failure_cache_expired(db_session):
    """Test that expired cache entries are recomputed."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response1 = client.post(
            "/api/v1/resilience/distributor-failure",
            json={"distributor_id": 1, "bom_component_ids": [1]},
        )
        assert response1.status_code == 200
        data1 = response1.json()

        cache_entries = db_session.query(ScenarioCache).all()
        if cache_entries:
            for entry in cache_entries:
                entry.expires_at = datetime.utcnow() - timedelta(hours=1)
            db_session.commit()

        response2 = client.post(
            "/api/v1/resilience/distributor-failure",
            json={"distributor_id": 1, "bom_component_ids": [1]},
        )
        assert response2.status_code == 200
        data2 = response2.json()

        assert data1 == data2
    finally:
        app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────────────
# TASK 3: RED tests for POST /resilience/geopolitical-risk endpoint
# ────────────────────────────────────────────────────────────────────────────

def test_geopolitical_risk_accepts_request(db_session):
    """Test POST /api/v1/resilience/geopolitical-risk accepts valid request."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/geopolitical-risk",
            json={"risk_multiplier": 2.0, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_geopolitical_risk_response_structure(db_session):
    """Test that geopolitical-risk response has all required fields."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/geopolitical-risk",
            json={"risk_multiplier": 2.0, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
        data = response.json()

        required_fields = [
            'baseline_cost_usd', 'scenario_cost_usd', 'cost_delta_pct',
            'baseline_eta_days', 'scenario_eta_days', 'eta_delta_days',
            'baseline_risk_score', 'scenario_risk_score', 'risk_delta',
            'baseline_fulfillment_p10', 'baseline_fulfillment_p50', 'baseline_fulfillment_p90',
            'scenario_fulfillment_p10', 'scenario_fulfillment_p50', 'scenario_fulfillment_p90',
            'affected_bom_ids',
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"
    finally:
        app.dependency_overrides.clear()


def test_geopolitical_risk_feed_override(db_session):
    """Test that risk_multiplier properly overrides live feeds."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/geopolitical-risk",
            json={"risk_multiplier": 2.0, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
        data = response.json()

        assert data['risk_delta'] >= 0
    finally:
        app.dependency_overrides.clear()


def test_geopolitical_risk_tier_migration(db_session):
    """Test that risk_multiplier can cause component tier migrations."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/geopolitical-risk",
            json={"risk_multiplier": 2.0, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
        data = response.json()

        assert isinstance(data['affected_bom_ids'], list)
    finally:
        app.dependency_overrides.clear()


def test_geopolitical_risk_caching(db_session, monkeypatch):
    """Test that repeated geopolitical-risk calls return cached result."""
    import time
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        _sim_calls = _count_monte_carlo(monkeypatch)
        response1 = client.post(
            "/api/v1/resilience/geopolitical-risk",
            json={"risk_multiplier": 2.0, "bom_component_ids": [1]},
        )
        assert response1.status_code == 200
        data1 = response1.json()

        # Whatever the first (uncached) request cost in simulator calls, the
        # second must add exactly zero. Snapshotting instead of asserting a
        # total keeps this correct even if the endpoint's internal number of
        # simulation passes changes.
        _calls_after_first = _sim_calls["n"]
        assert _calls_after_first > 0, "first request should have run the simulation"

        time.sleep(0.05)

        start = time.time()
        response2 = client.post(
            "/api/v1/resilience/geopolitical-risk",
            json={"risk_multiplier": 2.0, "bom_component_ids": [1]},
        )
        elapsed = (time.time() - start) * 1000
        assert response2.status_code == 200
        data2 = response2.json()

        assert data1 == data2
        # The cache is proven by WORK NOT DONE, not by a stopwatch: a wall-clock
        # budget here measures the CI runner (this assertion read <10ms locally
        # and 196ms on a loaded GitHub runner while the cache was hitting fine).
        # `_sim_calls` counts run_monte_carlo invocations across both requests;
        # a cache miss on the second call would run it again.
        assert _sim_calls["n"] == _calls_after_first, (
            f"second identical request recomputed the scenario: run_monte_carlo ran "
            f"{_sim_calls['n'] - _calls_after_first} extra time(s) — the cache missed"
        )
    finally:
        app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────────────
# TASK 4: RED tests for POST /resilience/delivery-target endpoint
# ────────────────────────────────────────────────────────────────────────────

def test_delivery_target_accepts_request(db_session):
    """Test POST /api/v1/resilience/delivery-target accepts valid request."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/delivery-target",
            json={"target_delivery_days": 14, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_delivery_target_response_structure(db_session):
    """Test that delivery-target response has all required fields."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/delivery-target",
            json={"target_delivery_days": 14, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
        data = response.json()

        required_fields = [
            'baseline_cost_usd', 'scenario_cost_usd', 'cost_delta_pct',
            'baseline_eta_days', 'scenario_eta_days', 'eta_delta_days',
            'baseline_risk_score', 'scenario_risk_score', 'risk_delta',
            'baseline_fulfillment_p10', 'baseline_fulfillment_p50', 'baseline_fulfillment_p90',
            'scenario_fulfillment_p10', 'scenario_fulfillment_p50', 'scenario_fulfillment_p90',
            'suppliers_capable', 'suppliers_cannot_meet',
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"
    finally:
        app.dependency_overrides.clear()


def test_delivery_target_tight_constraint(db_session):
    """Test that tight delivery target increases cost."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/delivery-target",
            json={"target_delivery_days": 14, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
        data = response.json()

        assert data['scenario_cost_usd'] >= data['baseline_cost_usd']
        assert isinstance(data['suppliers_capable'], list)
        assert isinstance(data['suppliers_cannot_meet'], list)
    finally:
        app.dependency_overrides.clear()


def test_delivery_target_impossible(db_session):
    """Test that impossible delivery target is handled gracefully."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/resilience/delivery-target",
            json={"target_delivery_days": 1, "bom_component_ids": [1]},
        )
        assert response.status_code == 200
        data = response.json()

        assert isinstance(data, dict)
    finally:
        app.dependency_overrides.clear()


def test_delivery_target_caching(db_session, monkeypatch):
    """Test that repeated delivery-target calls return cached result."""
    import time
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()

    comp = Component(id=1, mpn="TEST-001", manufacturer="TestMfg", category="Test", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    offer = DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1)
    db_session.add(offer)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        _sim_calls = _count_monte_carlo(monkeypatch)
        response1 = client.post(
            "/api/v1/resilience/delivery-target",
            json={"target_delivery_days": 14, "bom_component_ids": [1]},
        )
        assert response1.status_code == 200
        data1 = response1.json()

        # Whatever the first (uncached) request cost in simulator calls, the
        # second must add exactly zero. Snapshotting instead of asserting a
        # total keeps this correct even if the endpoint's internal number of
        # simulation passes changes.
        _calls_after_first = _sim_calls["n"]
        assert _calls_after_first > 0, "first request should have run the simulation"

        time.sleep(0.05)

        start = time.time()
        response2 = client.post(
            "/api/v1/resilience/delivery-target",
            json={"target_delivery_days": 14, "bom_component_ids": [1]},
        )
        elapsed = (time.time() - start) * 1000
        assert response2.status_code == 200
        data2 = response2.json()

        assert data1 == data2
        # The cache is proven by WORK NOT DONE, not by a stopwatch: a wall-clock
        # budget here measures the CI runner (this assertion read <10ms locally
        # and 196ms on a loaded GitHub runner while the cache was hitting fine).
        # `_sim_calls` counts run_monte_carlo invocations across both requests;
        # a cache miss on the second call would run it again.
        assert _sim_calls["n"] == _calls_after_first, (
            f"second identical request recomputed the scenario: run_monte_carlo ran "
            f"{_sim_calls['n'] - _calls_after_first} extra time(s) — the cache missed"
        )
    finally:
        app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────────────
# TASK 5: Integration test for resilience router registration
# ────────────────────────────────────────────────────────────────────────────

def test_resilience_endpoints_registered(db_session):
    """Test that all resilience endpoints are registered in FastAPI app."""
    dist = Distributor(id=1, name="TestDist", latitude=0, longitude=0,
                      city="Test", state="TS", country="USA", is_domestic=True)
    db_session.add(dist)
    db_session.commit()
    # Seed the component the bodies reference. These endpoints now 404 on an unknown
    # component_id (they used to treat it as a supplier-less line and return a
    # confident 200), so a bare `status_code != 404` probe can no longer distinguish
    # "route missing" from "data missing". The route table is checked directly below
    # for that reason, and the POST is given valid data.
    comp = Component(id=1, mpn="REG-1", manufacturer="M", category="T", risk_score=0.3)
    db_session.add(comp)
    db_session.commit()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        endpoints = [
            ("/api/v1/resilience/distributor-failure", {"distributor_id": 1, "bom_component_ids": [1]}),
            ("/api/v1/resilience/geopolitical-risk", {"risk_multiplier": 2.0, "bom_component_ids": [1]}),
            ("/api/v1/resilience/delivery-target", {"target_delivery_days": 14, "bom_component_ids": [1]}),
        ]
        registered = {getattr(r, "path", None) for r in app.routes}
        for endpoint, body in endpoints:
            assert endpoint in registered, f"Endpoint {endpoint} not registered"
            response = client.post(endpoint, json=body)
            assert response.status_code != 404, (
                f"{endpoint} returned 404 with valid data: {response.text}"
            )
    finally:
        app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────────────
# P0 regression: scenario outputs must be DATA-DERIVED (real Monte Carlo +
# real distributor geography), never the old hardcoded constants.
# ────────────────────────────────────────────────────────────────────────────

def _override(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass
    return override_get_db


def test_distributor_failure_is_real_monte_carlo(db_session):
    """Fulfillment percentiles must come from the real Monte Carlo cascade, not the
    old hardcoded baseline (0.7/0.85/0.95) and scenario (0.6/0.75/0.90) placeholders.
    Failing a distributor must be weakly worse than baseline."""
    dists = [
        Distributor(id=i, name=f"D{i}", latitude=35.1 + i, longitude=-90.0 - i,
                    city="C", state="TN", country="USA", is_domestic=True)
        for i in (1, 2, 3)
    ]
    db_session.add_all(dists)
    db_session.commit()
    for cid in range(1, 7):
        db_session.add(Component(id=cid, mpn=f"C{cid}", manufacturer="M", category="Test", risk_score=0.3))
    db_session.commit()
    # d1 is sole/major supplier of several lines so its failure clearly bites.
    pairs = [(1, 1), (2, 1), (3, 1), (3, 2), (4, 2), (4, 3), (5, 2), (6, 3)]
    for oid, (cid, did) in enumerate(pairs, start=1):
        db_session.add(DistributorOffer(id=oid, component_id=cid, distributor_id=did, price=10.0, stock=100, moq=1))
    db_session.commit()

    bom = [1, 2, 3, 4, 5, 6]
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        data = client.post(
            "/api/v1/resilience/distributor-failure",
            json={"distributor_id": 1, "bom_component_ids": bom},
        ).json()
        base = (data["baseline_fulfillment_p10"], data["baseline_fulfillment_p50"], data["baseline_fulfillment_p90"])
        scen = (data["scenario_fulfillment_p10"], data["scenario_fulfillment_p50"], data["scenario_fulfillment_p90"])
        # Real, non-degenerate baseline distribution.
        assert data["baseline_fulfillment_p50"] > 0.0
        assert base[0] <= base[1] <= base[2] and scen[0] <= scen[1] <= scen[2]
        # The old hardcoded placeholder vectors must be gone.
        assert base != (0.7, 0.85, 0.95)
        assert scen != (0.6, 0.75, 0.90)
        # Failing a real supplier is weakly worse than baseline.
        assert data["scenario_fulfillment_p50"] <= data["baseline_fulfillment_p50"]
    finally:
        app.dependency_overrides.clear()


def test_delivery_target_capability_is_geography_derived(db_session):
    """A far international supplier must fail a tight delivery window while a
    hub-local domestic supplier meets it — proving lead times come from real
    haversine geography, not the old hardcoded 10/21 days."""
    near = Distributor(id=1, name="NearHub", latitude=35.15, longitude=-90.05,
                       city="Memphis", state="TN", country="USA", is_domestic=True)
    far = Distributor(id=2, name="FarIntl", latitude=51.5, longitude=0.0,
                      city="London", state="", country="UK", is_domestic=False)
    db_session.add_all([near, far])
    db_session.commit()
    comp = Component(id=1, mpn="CMP-001", manufacturer="Mfg", category="Test", risk_score=0.2)
    db_session.add(comp)
    db_session.commit()
    db_session.add_all([
        DistributorOffer(id=1, component_id=1, distributor_id=1, price=10.0, stock=100, moq=1),
        DistributorOffer(id=2, component_id=1, distributor_id=2, price=9.0, stock=100, moq=1),
    ])
    db_session.commit()

    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        data = client.post(
            "/api/v1/resilience/delivery-target",
            json={"target_delivery_days": 3, "bom_component_ids": [1]},
        ).json()
        capable = {s["name"] for s in data["suppliers_capable"]}
        cannot = {s["name"] for s in data["suppliers_cannot_meet"]}
        assert "NearHub" in capable          # ~2 days, meets 3-day window
        assert "FarIntl" in cannot           # transatlantic + customs, cannot
        # Lead times are real fractional/geography values, not the old 10/21 constants.
        near_lead = next(s["lead_time_days"] for s in data["suppliers_capable"] if s["name"] == "NearHub")
        assert near_lead != 10
    finally:
        app.dependency_overrides.clear()


def test_geopolitical_higher_stress_is_monotonically_worse(db_session):
    """A larger risk multiplier must not improve fulfillment — the elevated-stress
    Monte Carlo should be weakly worse, proving fulfillment responds to the input."""
    # Two distributors both supplying one component so betweenness is non-trivial.
    d1 = Distributor(id=1, name="D1", latitude=35.1, longitude=-90.0, city="A", state="TN", country="USA", is_domestic=True)
    d2 = Distributor(id=2, name="D2", latitude=40.0, longitude=-75.0, city="B", state="PA", country="USA", is_domestic=True)
    db_session.add_all([d1, d2])
    db_session.commit()
    for cid in (1, 2, 3):
        db_session.add(Component(id=cid, mpn=f"C{cid}", manufacturer="M", category="Test", risk_score=0.5))
    db_session.commit()
    oid = 1
    for cid in (1, 2, 3):
        for did in (1, 2):
            db_session.add(DistributorOffer(id=oid, component_id=cid, distributor_id=did, price=5.0, stock=50, moq=1))
            oid += 1
    db_session.commit()

    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        low = client.post("/api/v1/resilience/geopolitical-risk",
                          json={"risk_multiplier": 1.0, "bom_component_ids": [1, 2, 3]}).json()
        high = client.post("/api/v1/resilience/geopolitical-risk",
                           json={"risk_multiplier": 5.0, "bom_component_ids": [1, 2, 3]}).json()
        # Higher geopolitical stress is weakly worse for fulfillment.
        assert high["scenario_fulfillment_p50"] <= low["scenario_fulfillment_p50"]
        # And cost is weakly higher under more stress.
        assert high["scenario_cost_usd"] >= low["scenario_cost_usd"]
    finally:
        app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────────────
# Recommendation-engine endpoints: criticality sweep, dual-sourcing, sensitivity
# ────────────────────────────────────────────────────────────────────────────

def _seed_recommendation_data(db_session):
    """Seed 2 distributors + components with a mix of single/multi-source lines.

    c1: d1 only stocked, d2 alt offer stock 0 (cheaper) → single-source no-regret
    c2: d1 only                                          → single-source supplier-development
    c3: d1 + d2 both stocked                             → multi-source
    """
    d1 = Distributor(id=1, name="D1", latitude=35.15, longitude=-90.05,
                     city="Memphis", state="TN", country="USA", is_domestic=True)
    d2 = Distributor(id=2, name="D2", latitude=40.0, longitude=-75.0,
                     city="Philly", state="PA", country="USA", is_domestic=True)
    db_session.add_all([d1, d2])
    db_session.commit()
    for cid in (1, 2, 3):
        db_session.add(Component(id=cid, mpn=f"C{cid}", manufacturer="M",
                                 category="Test", risk_score=0.3))
    db_session.commit()
    rows = [
        (1, 1, 1, 10.0, 100),
        (2, 1, 2, 8.0, 0),
        (3, 2, 1, 10.0, 100),
        (4, 3, 1, 5.0, 100),
        (5, 3, 2, 5.0, 100),
    ]
    for oid, cid, did, price, stock in rows:
        db_session.add(DistributorOffer(id=oid, component_id=cid, distributor_id=did,
                                        price=price, stock=stock, moq=1))
    db_session.commit()


def test_criticality_sweep_endpoint(db_session):
    """200 + response shape + orphan detection for the criticality sweep."""
    _seed_recommendation_data(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        resp = client.post("/api/v1/resilience/criticality-sweep",
                           json={"top_n": 10})
        assert resp.status_code == 200
        data = resp.json()
        for f in ("entries", "max_spend_at_risk_usd", "network_wide"):
            assert f in data
        assert data["network_wide"] is True  # no bom filter
        assert data["entries"], "expected at least one distributor entry"
        top = data["entries"][0]
        for f in ("distributor_id", "orphan_component_count", "orphan_component_ids",
                  "spend_at_risk_usd", "betweenness", "rei"):
            assert f in top
        # D1 is the only offer for c2 → tops the sweep with rei 1.0.
        # (c1 also has a d2 alt offer, so it is single-source but not orphaned.)
        assert top["distributor_id"] == 1
        assert top["orphan_component_count"] == 1
        assert top["rei"] == 1.0
    finally:
        app.dependency_overrides.clear()


def test_criticality_sweep_cache_hit(db_session):
    _seed_recommendation_data(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        body = {"top_n": 10}
        r1 = client.post("/api/v1/resilience/criticality-sweep", json=body)
        r2 = client.post("/api/v1/resilience/criticality-sweep", json=body)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json() == r2.json()
    finally:
        app.dependency_overrides.clear()


def test_dual_sourcing_plan_endpoint(db_session):
    """200 + response shape + honest tier counts across all single-source parts."""
    _seed_recommendation_data(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        resp = client.post("/api/v1/resilience/dual-sourcing-plan",
                           json={"top_n": 10})
        assert resp.status_code == 200
        data = resp.json()
        for f in ("entries", "no_regret_count", "hedge_count", "supplier_development_count"):
            assert f in data
        # c1 has a cheaper alt → no-regret; c2 has no alt → supplier-development.
        assert data["no_regret_count"] == 1
        assert data["supplier_development_count"] == 1
        entry = next(e for e in data["entries"] if e["component_id"] == 1)
        assert entry["tier"] == "no-regret"
        assert entry["recommended_second_source"] == "D2"
    finally:
        app.dependency_overrides.clear()


def test_dual_sourcing_plan_cache_hit(db_session):
    _seed_recommendation_data(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        body = {"top_n": 10}
        r1 = client.post("/api/v1/resilience/dual-sourcing-plan", json=body)
        r2 = client.post("/api/v1/resilience/dual-sourcing-plan", json=body)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json() == r2.json()
    finally:
        app.dependency_overrides.clear()


def test_sensitivity_endpoint(db_session):
    """200 + tornado shape + bars sorted descending by spread."""
    _seed_recommendation_data(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        resp = client.post("/api/v1/resilience/sensitivity",
                           json={"bom_component_ids": [1, 2, 3], "metric": "cost"})
        assert resp.status_code == 200
        data = resp.json()
        for f in ("baseline_output", "metric", "bars"):
            assert f in data
        assert data["metric"] == "cost"
        bars = data["bars"]
        assert bars, "expected tornado bars"
        for b in bars:
            for f in ("lever", "low_label", "high_label", "low_output", "high_output", "spread"):
                assert f in b
        spreads = [b["spread"] for b in bars]
        assert spreads == sorted(spreads, reverse=True)
    finally:
        app.dependency_overrides.clear()


def test_sensitivity_cache_hit(db_session):
    _seed_recommendation_data(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        body = {"bom_component_ids": [1, 2, 3], "metric": "cost"}
        r1 = client.post("/api/v1/resilience/sensitivity", json=body)
        r2 = client.post("/api/v1/resilience/sensitivity", json=body)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json() == r2.json()
    finally:
        app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────────────
# The published ETA must describe the SAME PLAN as the published cost
#
# `_price_bom` buys the cheapest offer per line. The old `_bom_eta_days` was a
# max-over-lines of MIN-over-suppliers — the fastest distributor in the whole
# catalogue, whether or not the plan bought a single part from it. On the demo
# cart that published 2.8 days for a $166.94 plan whose suppliers actually take
# 26.6 days: a 9.4x understatement, contradicted by the very table underneath it,
# which named the 26.6-day Singapore distributor on 4 of 5 lines.
#
# The seed below is that shape in miniature: the cheapest supplier is the slowest
# one. Every assertion here fails against the old implementation.
# ────────────────────────────────────────────────────────────────────────────

def _seed_cheap_is_slow(db_session):
    """2 lines, 3 suppliers, where cheapest != fastest.

    far_cheap  — Singapore, international: cheapest on BOTH lines, ~26 days out.
    near_dear  — on top of the reference hub: fastest in the catalogue, dearest.
    mid_co     — San Francisco: middling on both price and speed.
    """
    db_session.add_all([
        Distributor(id=1, name="Far Cheap Pte Ltd", latitude=1.3521, longitude=103.8198,
                    city="Singapore", state="", country="Singapore", is_domestic=False),
        Distributor(id=2, name="Near Dear Inc", latitude=35.1495, longitude=-90.0490,
                    city="Memphis", state="TN", country="USA", is_domestic=True),
        Distributor(id=3, name="Mid Co", latitude=37.7749, longitude=-122.4194,
                    city="San Francisco", state="CA", country="USA", is_domestic=True),
    ])
    db_session.add_all([
        Component(id=1, mpn="CHEAP-SLOW-1", manufacturer="M", category="C", risk_score=0.2),
        Component(id=2, mpn="CHEAP-SLOW-2", manufacturer="M", category="C", risk_score=0.2),
    ])
    db_session.commit()
    db_session.add_all([
        DistributorOffer(id=1, component_id=1, distributor_id=1, price=1.00, stock=500, moq=1),
        DistributorOffer(id=2, component_id=1, distributor_id=2, price=5.00, stock=500, moq=1),
        DistributorOffer(id=3, component_id=1, distributor_id=3, price=3.00, stock=500, moq=1),
        DistributorOffer(id=4, component_id=2, distributor_id=1, price=2.00, stock=500, moq=1),
        DistributorOffer(id=5, component_id=2, distributor_id=2, price=9.00, stock=500, moq=1),
        DistributorOffer(id=6, component_id=2, distributor_id=3, price=4.00, stock=500, moq=1),
    ])
    db_session.commit()


def _lead(db_session, distributor_id):
    from app.api.resilience import _distributor_lead_days
    return _distributor_lead_days(
        db_session.query(Distributor).filter(Distributor.id == distributor_id).one()
    )


def _assert_eta_covers_the_priced_plan(db_session, component_ids, reported_eta):
    """The invariant, checked WITHOUT reusing the endpoint's own helpers.

    Every line's cost is set by its cheapest offer. That line cannot arrive before
    the distributor supplying it does, so the BOM ETA must be at least the slowest
    of those distributors. Anything less is an ETA for a plan the reported cost is
    not paying for.
    """
    from app.api.resilience import _distributor_lead_days
    slowest_in_plan = 0.0
    for cid in component_ids:
        offers = [
            o for o in db_session.query(DistributorOffer)
            .filter(DistributorOffer.component_id == cid).all()
            if o.price is not None and float(o.price) > 0
        ]
        assert offers, f"component {cid} has no priced offer"
        bought_from = min(offers, key=lambda o: float(o.price)).distributor_id
        dist = db_session.query(Distributor).filter(Distributor.id == bought_from).one()
        slowest_in_plan = max(slowest_in_plan, _distributor_lead_days(dist))
    assert reported_eta >= round(slowest_in_plan, 1) - 0.05, (
        f"published ETA {reported_eta}d is faster than the plan's own slowest "
        f"supplier ({slowest_in_plan:.1f}d) — cost and ETA describe different plans"
    )


def test_baseline_eta_is_the_plan_eta_not_the_catalogue_minimum(db_session):
    """RED against the old code: it published 2.0 days for a plan sourced at ~26."""
    _seed_cheap_is_slow(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        resp = client.post("/api/v1/resilience/geopolitical-risk", json={
            "risk_multiplier": 2.0,
            "items": [{"component_id": 1, "quantity": 10},
                      {"component_id": 2, "quantity": 10}],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()

        cheapest_slow = _lead(db_session, 1)   # the supplier the plan actually buys
        fastest_dear = _lead(db_session, 2)    # the catalogue-wide minimum
        assert cheapest_slow > fastest_dear + 15, "seed no longer separates the two"

        assert data["baseline_eta_days"] == pytest.approx(cheapest_slow, abs=0.05)
        assert data["baseline_eta_days"] != pytest.approx(fastest_dear, abs=0.05)
        _assert_eta_covers_the_priced_plan(db_session, [1, 2], data["baseline_eta_days"])

        # A risk-index spike removes no supplier, so the plan and its date hold.
        assert data["scenario_eta_days"] == pytest.approx(data["baseline_eta_days"], abs=0.05)
        assert data["eta_delta_days"] == pytest.approx(0.0, abs=0.05)
        assert "slowest" in data["eta_basis"].lower()
    finally:
        app.dependency_overrides.clear()


def test_losing_the_cheap_slow_supplier_speeds_delivery_up(db_session):
    """The trade the bug was hiding: the cheap distant supplier is also the slow one.

    Old code reported eta_delta_days = 0.0 here — both sides collapsed to the
    catalogue-wide fastest supplier, so the outage looked free on time and the
    only visible effect was cost.
    """
    _seed_cheap_is_slow(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        resp = client.post("/api/v1/resilience/distributor-failure", json={
            "distributor_id": 1,
            "items": [{"component_id": 1, "quantity": 10},
                      {"component_id": 2, "quantity": 10}],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["baseline_eta_days"] == pytest.approx(_lead(db_session, 1), abs=0.05)
        # Both surviving suppliers beat the one that went dark; the plan reroutes to
        # the cheaper of them (Mid Co), so the BOM lands EARLIER, not later.
        assert data["scenario_eta_days"] == pytest.approx(_lead(db_session, 3), abs=0.05)
        assert data["eta_delta_days"] < 0, "losing the slowest supplier must not slow the BOM"
        # ...and the speed is paid for.
        assert data["cost_delta_pct"] > 0
        assert data["hedging"]["n_lines_orphaned"] == 0
    finally:
        app.dependency_overrides.clear()


def test_delivery_target_eta_describes_the_constrained_plan(db_session):
    """A window that excludes the cheapest supplier must price AND date the plan
    that replaces it — not the fastest supplier inside the window."""
    _seed_cheap_is_slow(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        target = 6
        resp = client.post("/api/v1/resilience/delivery-target", json={
            "target_delivery_days": target,
            "items": [{"component_id": 1, "quantity": 10},
                      {"component_id": 2, "quantity": 10}],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()

        capable = {s["name"] for s in data["suppliers_capable"]}
        assert capable == {"Near Dear Inc", "Mid Co"}
        assert [s["name"] for s in data["suppliers_cannot_meet"]] == ["Far Cheap Pte Ltd"]

        # Cheapest inside the window is Mid Co on both lines, so the constrained
        # plan's date is Mid Co's — NOT Near Dear's, which nothing is bought from.
        assert data["scenario_eta_days"] == pytest.approx(_lead(db_session, 3), abs=0.05)
        assert data["scenario_eta_days"] != pytest.approx(_lead(db_session, 2), abs=0.05)
        assert data["scenario_eta_days"] <= target
        assert data["target_met"] is True
        assert data["target_is_binding"] is True
        assert data["eta_delta_days"] < 0, "the window is what makes the BOM fast"
        assert data["cost_substitution"]["scenario_component_cost_usd"] > \
            data["cost_substitution"]["baseline_component_cost_usd"]
        assert data["unmet_component_ids"] == []
    finally:
        app.dependency_overrides.clear()


def test_infeasible_window_dates_the_plan_you_are_left_with(db_session):
    """No supplier can hit a 1-day window: the ETA falls back to the BASELINE PLAN's
    supplier for every orphaned line, exactly as its cost does — never to the target,
    and never to a fast supplier the plan cannot use."""
    _seed_cheap_is_slow(db_session)
    app.dependency_overrides[get_db] = _override(db_session)
    try:
        client = TestClient(app)
        resp = client.post("/api/v1/resilience/delivery-target", json={
            "target_delivery_days": 1,
            "items": [{"component_id": 1, "quantity": 10},
                      {"component_id": 2, "quantity": 10}],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["suppliers_capable"] == []
        assert sorted(data["unmet_component_ids"]) == [1, 2]
        assert data["target_met"] is False
        assert "INFEASIBLE" in data["eta_note"]
        assert data["scenario_eta_days"] != 1.0
        assert data["scenario_eta_days"] == pytest.approx(_lead(db_session, 1), abs=0.05)
        _assert_eta_covers_the_priced_plan(db_session, [1, 2], data["scenario_eta_days"])
    finally:
        app.dependency_overrides.clear()
