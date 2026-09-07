"""Shared pytest fixtures and path setup."""
import os
import sys
from pathlib import Path

# Ensure `backend/` is on path so `import app.*` works regardless of invocation
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Set a valid SECRET_KEY before importing app (validator will fire at Settings() instantiation)
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-at-least-32-characters-long-for-testing")
os.environ.setdefault("DEBUG", "true")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.core.database import Base, get_db
from app.models.user import User
from app.core.security import create_access_token, get_password_hash


#: The scratch database is named PER PROCESS.
#:
#: It used to be a fixed ``./test_hardening.db``, which meant two pytest processes
#: silently shared one SQLite file: one would drop and recreate tables while the
#: other was mid-fixture. Observed 2026-08-28 — a targeted run returned
#: ``component_id 5 not found`` / 404 on five stochastic tests purely because a
#: second run was in flight. Nothing was wrong with the code under test.
#:
#: The loop's learnings log records the symptom ("never kill pytest mid-flight — it poisons
#: test_hardening.db"), but the fixed filename was the actual defect: a poisoned
#: shared file is only reachable because the file is shared. A per-process name
#: also makes ``pytest -n auto`` possible, which the fixed name silently forbade.
#:
#: ``.gitignore`` already covers ``*.db``, so no new ignore rule is needed.
TEST_DB_FILE = f"test_hardening_{os.getpid()}.db"
TEST_DB_URL = f"sqlite:///./{TEST_DB_FILE}"
test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = sessionmaker(bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def _remove_scratch_database_when_the_session_ends():
    """Delete this process's scratch DB so runs cannot accumulate stale files.

    Teardown only — the file must exist for the whole session. Best-effort: a
    failure to unlink must never turn a green suite red.
    """
    yield
    test_engine.dispose()
    # -journal is the rollback-mode sidecar; SQLite writes it even when WAL
    # is off, and .gitignore's `*.db` does not match `*.db-journal`.
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(TEST_DB_FILE + suffix)
        except OSError:
            pass


#: MODEL CI STRICT MODE.
#:
#: Every model gate in this suite skips cleanly when the artifacts, the panel or
#: the seeded database are absent, so a fresh checkout is not a wall of red. That
#: same courtesy is a liability in CI: a skipped gate is a *green* gate, and the
#: whole point of `model-ci` is that the gates cannot quietly stop testing. So
#: `.github/workflows/model-ci.yml` sets MODEL_CI_STRICT=1, and in that mode a
#: skip of a `model_ci`-marked test is promoted to a FAILURE. Locally the flag is
#: unset and skips stay skips.
#:
#: This exists because of bug 6: a contract test silently stopped exercising the
#: thing it was written to catch. A gate that no-ops must be loud, not absent.
_MODEL_CI_STRICT = os.environ.get("MODEL_CI_STRICT", "").lower() in ("1", "true", "yes", "on")


#: Node ids of every ``model_ci``-marked test pytest collected THIS session.
#:
#: `tests/test_model_ci_gates.py::test_the_model_ci_gate_census_is_complete` reads
#: this to cross-check its static census against what pytest actually collected,
#: which is how a silent DECOLLECTION is caught. Deleting one `pytestmark` line
#: from a gate file used to drop 21 gates from the run and still report green,
#: because nothing anywhere asserted how many gates there are supposed to be.
COLLECTED_MODEL_CI_NODEIDS: list[str] = []


def pytest_collection_modifyitems(session, config, items):  # noqa: ARG001
    COLLECTED_MODEL_CI_NODEIDS.clear()
    COLLECTED_MODEL_CI_NODEIDS.extend(
        item.nodeid for item in items
        if item.get_closest_marker("model_ci") is not None
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if not _MODEL_CI_STRICT:
        return
    report = outcome.get_result()
    if not report.skipped or item.get_closest_marker("model_ci") is None:
        return
    # xfail is a deliberate expectation, not an absent gate — leave it alone.
    if hasattr(report, "wasxfail"):
        return
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
    report.outcome = "failed"
    report.longrepr = (
        f"MODEL_CI_STRICT: gate {item.nodeid} SKIPPED ({reason}). In model CI a "
        "skipped gate is not a passing gate — the artifacts, the observed panel "
        "and the seeded database are all committed, so this gate must run. "
        "Fix the precondition; do not silence the gate."
    )


@pytest.fixture(scope="session", autouse=True)
def _no_background_feed_refresh():
    """Stop the live-feed scheduler from querying the DB behind the tests.

    ``app/main.py``'s lifespan builds an APScheduler job that fires IMMEDIATELY on
    startup (deliberately — see the comment in ``app/feeds/scheduler.py``), and the
    ``client`` fixture enters ``TestClient`` as a context manager, so EVERY test
    that uses ``client`` starts one. Those jobs run on a background thread and hit
    its per-process scratch DB while the function-scoped fixtures here are dropping and
    recreating its tables — the shared file-backed engine is the same one
    ``tests/test_stochastic_api.py`` builds its network in.

    The result was a nondeterministic wall of
    ``OperationalError: no such table: components`` / ``no such table: users``
    at unrelated tests' setup, drifting between runs: one full-suite run failed
    ``test_serve_coverage.py::test_lead_time_endpoint_returns_a_prediction_for_a_real_part``
    — a MODEL CI GATE — purely from this race, while the same gate passed alone
    and passed under ``-m model_ci``. A gate that goes red for reasons unrelated
    to the model is a gate people learn to re-run rather than read.

    Only the JOB BODY is stubbed, not ``build_scheduler``: the scheduler is still
    built and still registers ``feed_refresh``, so the tests that assert on the
    job's existence and its ``next_run_time`` (``test_feeds.py``,
    ``test_input_sensitivity_regressions.py``) keep testing the real thing.
    """
    import app.feeds.scheduler as scheduler_module

    original = getattr(scheduler_module, "refresh_all_feeds", None)
    if original is None:
        yield
        return

    async def _noop(*args, **kwargs):
        return None

    _noop.__name__ = getattr(original, "__name__", "refresh_all_feeds")
    scheduler_module.refresh_all_feeds = _noop
    try:
        yield
    finally:
        scheduler_module.refresh_all_feeds = original


@pytest.fixture(autouse=True)
def restore_process_globals():
    """
    GraphState and MLState are process-globals populated by the app lifespan (see the
    `client` fixture, which enters TestClient as a context manager). Once set, helpers
    like resilience._graph() prefer the global over building from the test's session,
    so a leaked global silently makes later tests read the real DB. Snapshot/restore
    keeps the suite order-independent.
    """
    import app.graph as graph
    import app.ml as ml

    prev_graph, prev_ml = graph.get_graph_state(), ml.get_ml_state()
    yield
    graph.set_graph_state(prev_graph)
    ml.set_ml_state(prev_ml)


def reset_test_schema() -> None:
    """Bring this process's scratch DB to "all tables present, all tables empty".

    Isolation is done by TRUNCATING at setup rather than DROPPING at teardown, and
    that swap is the fix for a whole class of cross-file flake.

    ``test_engine`` is a single FILE-backed engine shared by every fixture in the
    suite — ``tests/test_stochastic_api.py`` imports it by name and its
    ``frontier_db`` fixture calls ``Base.metadata.drop_all(bind=test_engine)`` on
    it too. With teardown-drops, any interleaving that put one fixture's drop
    between another's create and its insert produced
    ``OperationalError: no such table: components`` / ``no such table: users``,
    and any crashed run left a populated file behind so the NEXT run opened on
    ``IntegrityError: UNIQUE constraint failed: components.id``. Both showed up at
    the setup of tests that had nothing to do with the cause, drifted between runs,
    and in one full-suite run took down
    ``test_serve_coverage.py::test_lead_time_endpoint_returns_a_prediction_for_a_real_part``
    — a MODEL CI GATE — for reasons that had nothing to do with the model.

    Creating-then-emptying makes both impossible: the tables always exist, so a
    stray drop elsewhere is repaired by the next setup instead of being fatal, and
    every test still begins with a genuinely empty database. It is also faster than
    a full drop/create cycle per test.
    """
    # Drop every pooled connection first. A pooled SQLite connection that was
    # opened against a file which has since been unlinked or replaced keeps
    # answering — and answers writes with "attempt to write a readonly database",
    # attributed to whichever unlucky test next checked that connection out.
    # Starting each test from a fresh handle removes that class of ghost entirely.
    test_engine.dispose()
    Base.metadata.create_all(bind=test_engine)
    with test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(scope="function", autouse=True)
def _clean_test_database():
    """Every test starts against a present-and-empty per-process scratch DB.

    Autouse, and at SETUP, so it also covers the fixtures that build on the shared
    engine without going through ``db_session`` — ``tests/test_stochastic_api.py``
    imports ``test_engine`` directly and inserts fixed primary keys
    (``FRONTIER-001`` at ``components.id = 1``). Those used to be cleared as a side
    effect of the previous test's teardown-drop; relying on the *previous* test to
    leave the world tidy is what made the whole suite order-dependent. Doing it at
    setup means each test's precondition is established by that test.
    """
    reset_test_schema()
    yield


@pytest.fixture(scope="function")
def db_session():
    session = TestSession()
    yield session
    session.close()


@pytest.fixture(scope="function")
def client(db_session):
    def _override():
        try:
            yield db_session
        finally:
            pass
    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth_token(db_session):
    user = User(
        email="test@example.com",
        password_hash=get_password_hash("testpass"),
        factory_name="Test Factory",
        latitude=34.85,
        longitude=-82.39,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return create_access_token({"sub": str(user.id)})


# ── Graph test fixtures ───────────────────────────────────────────────────────

from app.models.distributor import Distributor
from app.models.component import Component, DistributorOffer


@pytest.fixture(scope="function")
def graph_db_session():
    """In-memory SQLite DB seeded with 3 distributors, 10 components, 15 offers."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # 3 distributors
    dists = [
        Distributor(id=1, name="DigiKey", latitude=48.1, longitude=-96.2,
                    city="Thief River Falls", state="MN", country="USA", is_domestic=True),
        Distributor(id=2, name="Mouser", latitude=32.2, longitude=-97.1,
                    city="Mansfield", state="TX", country="USA", is_domestic=True),
        Distributor(id=3, name="LCSC", latitude=22.5, longitude=114.1,
                    city="Shenzhen", state=None, country="China", is_domestic=False),
    ]
    for d in dists:
        session.add(d)

    # 10 components across 2 categories
    comps = []
    for i in range(1, 11):
        cat = "Microcontrollers" if i <= 5 else "Op-Amps"
        c = Component(id=i, mpn=f"TEST-{i:03d}", manufacturer="TestCo",
                      manufacturer_country="USA", category=cat,
                      description=f"Test component {i}", risk_score=0.3)
        comps.append(c)
        session.add(c)

    # 15 offers: components 1-5 have 2 offers each (dist 1+2), components 6-10 have 1 offer each (dist 1)
    offer_id = 1
    for comp_id in range(1, 6):
        for dist_id in [1, 2]:
            session.add(DistributorOffer(
                id=offer_id, component_id=comp_id, distributor_id=dist_id,
                price=1.50 + comp_id * 0.1, stock=100, moq=1,
                sku=f"SKU-{comp_id}-{dist_id}", currency="USD",
            ))
            offer_id += 1
    for comp_id in range(6, 11):
        session.add(DistributorOffer(
            id=offer_id, component_id=comp_id, distributor_id=1,
            price=2.00 + comp_id * 0.1, stock=50, moq=1,
            sku=f"SKU-{comp_id}-1", currency="USD",
        ))
        offer_id += 1

    session.commit()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)
