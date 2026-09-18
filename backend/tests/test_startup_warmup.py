"""
The deferred startup warm-up: it must be FAST, SINGLE-FLIGHT, and INVISIBLE.

The ML artifact load and the graph build used to run inside the ASGI lifespan, and
uvicorn accepts no connection until the lifespan's startup phase returns. Measured on
Render's 0.5-CPU free tier across 8 real cold starts, those two steps were 30.2-33.7 s
of a ~70 s wake. They now run on a background thread (``app/startup.py``).

That buys the boot time only if three things hold, and every one of them is a way to
ship a regression rather than a speedup:

  1. the lifespan really does return before the warm-up finishes (else nothing moved);
  2. a request that needs the graph WAITS for the one build — it must not start a
     second one (883 nodes of Brandes betweenness per request on half a CPU is a
     self-inflicted denial of service), and it must not answer without it;
  3. no endpoint's answer changes. Several call sites degrade *silently* to a
     different published number when their process global is still None
     (``macro_stress -> 0.0``, ``model_source="none"``, ``available=false``, 503).
     Those branches were unreachable in production before the deferral and must stay
     unreachable.

Every test here was checked RED before it was checked green — see the commentary on
each one for the mutation that makes it fail.
"""

from __future__ import annotations

import threading
import time

import pytest

import app.graph as graph_mod
import app.graph.builder as builder_mod
from app import startup


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def fresh_warmup():
    """
    Give one test a pristine, un-started ``app.startup`` module.

    ``startup.start()`` is deliberately once-per-process, so by the time this file
    runs in a full-suite pass some earlier ``TestClient`` has already fired it. These
    tests reach into the module's privates to reset it and put everything back
    afterwards, rather than adding a reset hook to production code.
    """
    saved_started = startup._started
    saved_thread = startup._thread
    saved_steps = startup._steps
    saved_runners = dict(startup._RUNNERS)

    startup._started = False
    startup._thread = None
    startup._steps = {name: startup._Step(name) for name in startup.STEPS}
    try:
        yield startup
    finally:
        startup.join(timeout=30)
        startup._RUNNERS.clear()
        startup._RUNNERS.update(saved_runners)
        startup._started = saved_started
        startup._thread = saved_thread
        startup._steps = saved_steps


@pytest.fixture()
def counting_builder(monkeypatch):
    """Wrap the real graph builder in a call counter, patched where it is looked up."""
    calls: list[float] = []
    real = builder_mod.build_graph_state

    def counting(db):
        calls.append(time.perf_counter())
        return real(db)

    # `ensure_graph_state` imports the name from the module at call time, so patching
    # the module attribute is what actually intercepts it.
    monkeypatch.setattr(builder_mod, "build_graph_state", counting)
    return calls


# ── 1. The lifespan must not block on the warm-up ────────────────────────────


def test_the_lifespan_returns_before_the_warmup_finishes(fresh_warmup, monkeypatch):
    """
    The whole point. Entering the app's lifespan must not wait for the graph build.

    RED CHECK: make ``startup.start()`` call ``_run()`` inline instead of spawning a
    thread (i.e. restore the old synchronous lifespan) and this fails on the elapsed
    assertion — 2.0 s instead of ~0 s.
    """
    release = threading.Event()
    entered = threading.Event()

    def slow_step():
        entered.set()
        release.wait(20)

    monkeypatch.setitem(startup._RUNNERS, startup.GRAPH, slow_step)
    monkeypatch.setitem(startup._RUNNERS, startup.ML, lambda: None)

    t0 = time.perf_counter()
    started = startup.start()
    elapsed = time.perf_counter() - t0

    assert started is True
    assert entered.wait(10), "the warm-up thread never ran"
    assert not startup._steps[startup.GRAPH].done.is_set(), \
        "the graph step finished before the test released it — the stub did not apply"
    assert elapsed < 1.0, (
        f"start() took {elapsed:.2f} s while the graph step was still blocked — the "
        "warm-up is running on the caller's thread, so nothing was actually deferred"
    )
    release.set()
    assert startup.join(timeout=20)
    assert startup._steps[startup.GRAPH].done.is_set()
    assert startup._steps[startup.ML].done.is_set()


def test_a_second_lifespan_warms_up_again_but_never_two_at_once(
    fresh_warmup, monkeypatch
):
    """
    The warm-up is per-LIFESPAN, exactly like the synchronous code it replaces, and
    that is load-bearing for the suite: ``conftest.restore_process_globals`` resets the
    GraphState and MLState after every test, so a once-per-process warm-up would leave
    every ``with TestClient(app)`` after the first looking at a None that the old code
    would have rebuilt.

    What must NOT happen is two warm-ups running at once — that is two Brandes
    betweenness passes over 883 nodes in parallel on half a CPU.

    RED CHECK: delete the ``previous.join(JOIN_SECONDS)`` guard in ``start()`` and the
    concurrency peak goes above 1.
    """
    runs: list[str] = []
    live = [0]
    peak = [0]
    lock = threading.Lock()

    def graph_step():
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.25)
        runs.append("graph")
        with lock:
            live[0] -= 1

    monkeypatch.setitem(startup._RUNNERS, startup.GRAPH, graph_step)
    monkeypatch.setitem(startup._RUNNERS, startup.ML, lambda: runs.append("ml"))

    assert startup.start() is True
    assert startup.start() is True   # a second lifespan warms up again
    assert startup.join(timeout=30) is True

    assert runs == ["graph", "ml", "graph", "ml"], runs
    assert peak[0] == 1, f"{peak[0]} warm-ups ran concurrently"


def test_a_failing_step_still_releases_its_waiters(fresh_warmup, monkeypatch):
    """
    A build that raises must not leave every request blocked until the 180 s cap.

    RED CHECK: drop the ``finally:`` around ``step.done.set()`` in ``_run`` and this
    hangs for ``DEFAULT_WAIT_SECONDS`` and then fails.
    """
    def boom():
        raise RuntimeError("deliberate")

    monkeypatch.setitem(startup._RUNNERS, startup.GRAPH, boom)
    monkeypatch.setitem(startup._RUNNERS, startup.ML, lambda: None)
    startup.start()

    assert startup.wait_for_graph(timeout=20) is True
    assert "deliberate" in (startup._steps[startup.GRAPH].error or "")


def test_waiting_is_a_no_op_when_no_warmup_was_ever_started(fresh_warmup):
    """
    Keeps every existing "no graph state -> 503" / "no models -> 503" test honest.

    A bare ``TestClient(app)`` that is not entered as a context manager never runs the
    lifespan, so nothing is warming up and nothing may block.
    """
    t0 = time.perf_counter()
    assert startup.wait_for_graph() is True
    assert startup.wait_for_ml() is True
    assert time.perf_counter() - t0 < 0.5


# ── 2. Single-flight: build once, store it, make everyone else wait ──────────


def test_ensure_graph_state_stores_what_it_builds(graph_db_session, counting_builder):
    """
    RED CHECK: revert ``ensure_graph_state`` to a bare ``return build_graph_state(db)``
    and the ``get_graph_state() is gs`` assertion fails, then the second call rebuilds
    and ``len(counting_builder) == 2``.
    """
    graph_mod.set_graph_state(None)
    gs = graph_mod.ensure_graph_state(graph_db_session)

    assert gs is not None
    assert graph_mod.get_graph_state() is gs, "the build was thrown away, not cached"
    assert graph_mod.ensure_graph_state(graph_db_session) is gs
    assert len(counting_builder) == 1, (
        f"the graph was built {len(counting_builder)} times; every un-cached rebuild is "
        "883 nodes of Brandes betweenness on a 0.5-CPU worker"
    )


def test_concurrent_callers_share_one_build(monkeypatch):
    """
    Eight threads asking for the graph at once must produce ONE build, not eight.

    This is the denial-of-service the deferral would otherwise introduce.

    The builder is a stub: what is under test is the locking, not the graph contents,
    and a real ``sqlite:///:memory:`` session cannot be shared across threads anyway
    (SQLAlchemy's SingletonThreadPool hands each thread its own empty database).

    RED CHECK: drop the ``_build_lock`` from ``ensure_graph_state`` and the call count
    goes to 8.
    """
    calls: list[int] = []
    gate = threading.Barrier(8, timeout=30)

    def slow(db):
        calls.append(1)
        time.sleep(0.3)  # widen the window every racer must survive
        return object()

    monkeypatch.setattr(builder_mod, "build_graph_state", slow)
    graph_mod.set_graph_state(None)

    results: list[object] = []
    errors: list[BaseException] = []

    def worker():
        try:
            gate.wait()
            results.append(graph_mod.ensure_graph_state(None))  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert not errors, errors
    assert len(results) == 8
    assert len(calls) == 1, f"{len(calls)} concurrent graph builds instead of 1"
    assert all(r is results[0] for r in results), "callers got different graph objects"


def test_a_slow_build_does_not_stomp_a_state_installed_while_it_ran(monkeypatch):
    """
    The epoch guard. A test fixture that clears the global mid-build, or any
    deliberate ``set_graph_state``, must win over a background build that started
    earlier — otherwise the warm-up silently swaps a test's graph for production's.

    RED CHECK: remove the ``if _state_epoch == epoch_before`` check and this fails.
    """
    started = threading.Event()
    release = threading.Event()
    built = object()

    def slow(db):
        started.set()
        release.wait(30)
        return built

    monkeypatch.setattr(builder_mod, "build_graph_state", slow)
    graph_mod.set_graph_state(None)

    out: list[object] = []
    t = threading.Thread(target=lambda: out.append(
        graph_mod.ensure_graph_state(None)))  # type: ignore[arg-type]
    t.start()
    assert started.wait(20)

    sentinel = object()
    graph_mod.set_graph_state(sentinel)  # type: ignore[arg-type]
    release.set()
    t.join(60)

    assert out and out[0] is not sentinel, "the builder must still return ITS own build"
    assert graph_mod.get_graph_state() is sentinel, (
        "the background build overwrote a state that was installed after it started"
    )


# ── 3. Nothing an endpoint returns may change ────────────────────────────────


def test_the_resilience_graph_helper_caches_instead_of_rebuilding_per_request(
    graph_db_session, counting_builder
):
    """
    ``api/resilience.py::_graph`` used to call ``build_graph_state(db)`` and discard
    the result — one full rebuild per request the moment the startup build is lazy.

    RED CHECK: restore ``gs = build_graph_state(db)`` there and the count becomes 3.
    """
    from app.api.resilience import _graph

    graph_mod.set_graph_state(None)
    first = _graph(graph_db_session)
    _graph(graph_db_session)
    _graph(graph_db_session)

    assert len(counting_builder) == 1, \
        f"{len(counting_builder)} rebuilds across 3 requests — the result is not cached"
    assert graph_mod.get_graph_state() is first


def test_a_graph_endpoint_waits_for_the_warmup_rather_than_answering_503(
    fresh_warmup, monkeypatch
):
    """
    A request that lands mid-warm-up must BLOCK and then answer correctly. Returning
    503 (or any degraded answer) would be a new, worse behaviour that the old
    synchronous lifespan made impossible.

    RED CHECK: delete the ``wait_for_graph()`` call from
    ``app/api/graph.py::_require_graph_state`` and this raises 503 immediately.
    """
    from fastapi import HTTPException

    from app.api.graph import _require_graph_state

    sentinel = object()
    release = threading.Event()

    def slow_graph():
        release.wait(30)
        graph_mod.set_graph_state(sentinel)  # type: ignore[arg-type]

    monkeypatch.setitem(startup._RUNNERS, startup.GRAPH, slow_graph)
    monkeypatch.setitem(startup._RUNNERS, startup.ML, lambda: None)
    graph_mod.set_graph_state(None)
    startup.start()

    out: dict = {}

    def caller():
        try:
            out["gs"] = _require_graph_state()
        except HTTPException as exc:
            out["http"] = exc.status_code

    t = threading.Thread(target=caller)
    t.start()
    time.sleep(0.4)
    assert t.is_alive(), (
        f"the endpoint answered before the graph was built (result={out}) — it must "
        "wait for the one build, not serve a degraded answer"
    )

    release.set()
    t.join(30)
    assert "http" not in out, f"endpoint returned {out.get('http')} instead of waiting"
    assert out["gs"] is sentinel


def test_an_ml_endpoint_waits_rather_than_publishing_model_source_none(
    fresh_warmup, monkeypatch
):
    """
    ``/ml/model-info`` answers ``model_source="none"`` when the global is unset. During
    warm-up that would be a *published number that is not true*, which is the one thing
    this repo's standing bar forbids.

    RED CHECK: delete ``wait_for_ml()`` from ``app/api/ml.py::_ml_state`` and this
    returns immediately with SOURCE_NONE.
    """
    import app.ml as ml_mod
    from app.api.ml import _ml_state

    sentinel = object()
    release = threading.Event()

    def slow_ml():
        release.wait(30)
        ml_mod.set_ml_state(sentinel)  # type: ignore[arg-type]

    monkeypatch.setitem(startup._RUNNERS, startup.GRAPH, lambda: None)
    monkeypatch.setitem(startup._RUNNERS, startup.ML, slow_ml)
    ml_mod.set_ml_state(None)  # type: ignore[arg-type]
    startup.start()

    out: dict = {}
    t = threading.Thread(target=lambda: out.update(state=_ml_state()))
    t.start()
    time.sleep(0.4)
    assert t.is_alive(), (
        f"the ML endpoint answered before the models were loaded (result={out}) — it "
        "would have published model_source='none' with null metrics"
    )

    release.set()
    t.join(30)
    assert out["state"] is sentinel


# ── 4. The warm-up must not stomp state installed after it started ───────────
#
# This is not a hypothetical. It shipped, and the full suite caught it:
# `tests/test_stress_vintage.py` and `tests/test_model_serving.py` install a stub
# MLState immediately after entering the lifespan and then assert what `/ml/stress`
# says about it. With the artifact load moved onto a background thread, the real
# artifacts landed on top of the stub a beat later and `/ml/stress` answered
# `available: true` for a stub that said false. Four tests went red that had nothing
# to do with startup.
#
# The fix is an assignment counter on each global, sampled when `start()` is called —
# NOT when the step finally publishes. By publish time the test has long since
# installed its state, so a counter read then looks unchanged and the guard does
# nothing at all. Both directions are pinned below, because a guard that simply never
# publishes would satisfy the first test and break production.


def _fake_ml_state(tag: str):
    import types
    return types.SimpleNamespace(provenance={"model_source": tag},
                                 current_stress_prob=0.0, _tag=tag)


def test_the_ml_warmup_does_not_overwrite_a_state_installed_after_it_started(
    fresh_warmup, monkeypatch
):
    """
    The stub is installed while the GRAPH step is still blocked, i.e. BEFORE the ML
    step has begun. That ordering is the whole point: it is what the real suite does
    (the state goes in milliseconds after the lifespan returns, while the warm-up is
    still on its first step), and it is the only ordering that tells the two epoch
    samples apart.

    RED CHECK: sample the epoch inside ``_load_ml`` (``epoch = ml_state_epoch()``)
    instead of from ``_start_epochs``, and this fails — that is the exact bug that
    turned tests/test_stress_vintage.py and tests/test_model_serving.py red.
    """
    import app.ml as ml_mod
    import app.ml.serving as serving_mod

    release = threading.Event()
    entered = threading.Event()

    def blocked_graph():
        entered.set()
        release.wait(30)

    monkeypatch.setitem(startup._RUNNERS, startup.GRAPH, blocked_graph)
    monkeypatch.setattr(serving_mod, "load_ml_state",
                        lambda: _fake_ml_state("from-warmup"))
    ml_mod.set_ml_state(None)  # type: ignore[arg-type]

    startup.start()
    assert entered.wait(20), "the warm-up never started"

    stub = _fake_ml_state("from-test")
    ml_mod.set_ml_state(stub)  # type: ignore[arg-type]
    release.set()
    assert startup.join(timeout=30)

    assert ml_mod.get_ml_state() is stub, (
        "the background ML load overwrote a state installed after it started — this is "
        "what turned tests/test_stress_vintage.py red"
    )


def test_the_ml_warmup_does_install_its_result_when_nothing_interferes(
    fresh_warmup, monkeypatch
):
    """The other direction: a guard that never publishes would break production."""
    import app.ml as ml_mod
    import app.ml.serving as serving_mod

    loaded = _fake_ml_state("from-warmup")
    monkeypatch.setattr(serving_mod, "load_ml_state", lambda: loaded)
    monkeypatch.setitem(startup._RUNNERS, startup.GRAPH, lambda: None)
    ml_mod.set_ml_state(None)  # type: ignore[arg-type]

    startup.start()
    assert startup.join(timeout=30)
    assert ml_mod.get_ml_state() is loaded, "the warm-up never installed what it loaded"


def test_the_graph_warmup_does_not_overwrite_a_state_installed_after_it_started(
    fresh_warmup, monkeypatch
):
    """
    Same race, the graph half — this is what ``tests/test_benchmark_api.py``'s
    ``graph_client`` fixture does when it clears the global after entering TestClient.

    The stub goes in while the warm-up is blocked opening its DB session, i.e. before
    ``ensure_graph_state`` samples anything, so the two epoch samples give different
    answers.

    RED CHECK: drop ``only_if_epoch=_start_epochs.get(GRAPH)`` from the
    ``ensure_graph_state`` call in ``_build_graph`` and this fails.
    """
    import types

    import app.core.database as db_mod

    release = threading.Event()
    entered = threading.Event()
    built = types.SimpleNamespace(fiedler_curve=[], _tag="from-warmup")

    def blocked_session():
        entered.set()
        release.wait(30)
        return types.SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(db_mod, "SessionLocal", blocked_session)
    monkeypatch.setattr(builder_mod, "build_graph_state", lambda db: built)
    monkeypatch.setitem(startup._RUNNERS, startup.ML, lambda: None)
    graph_mod.set_graph_state(None)

    startup.start()
    assert entered.wait(20), "the warm-up never reached the graph build"

    stub = types.SimpleNamespace(fiedler_curve=[], _tag="from-test")
    graph_mod.set_graph_state(stub)  # type: ignore[arg-type]
    release.set()
    assert startup.join(timeout=30)

    assert graph_mod.get_graph_state() is stub, \
        "the background graph build overwrote a state installed after it started"


def test_the_graph_warmup_does_install_its_result_when_nothing_interferes(
    fresh_warmup, monkeypatch
):
    """The other direction, for the graph."""
    import types

    built = types.SimpleNamespace(fiedler_curve=[], _tag="from-warmup")
    monkeypatch.setattr(builder_mod, "build_graph_state", lambda db: built)
    monkeypatch.setitem(startup._RUNNERS, startup.ML, lambda: None)
    graph_mod.set_graph_state(None)

    startup.start()
    assert startup.join(timeout=30)
    assert graph_mod.get_graph_state() is built, \
        "the warm-up never installed the graph it built"
