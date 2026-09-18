"""
Deferred startup warm-up.

WHY THIS EXISTS
---------------
Uvicorn does not accept a single TCP connection until the ASGI lifespan's *startup*
phase has returned. Everything the lifespan did synchronously was therefore on the
critical path of a cold start, and on Render's 0.5-CPU free tier that was expensive:
measured over 8 real cold starts (2026-08-30 .. 09-03, Render platform logs),
`==> Running` -> uvicorn accepting connections took 64.7-73.3 s, of which 30.2-33.7 s
was the lifespan alone (the rest was importing `app.main`).

Locally the same two lifespan steps measure:

    load_ml_state()      2.37 s     <- the biggest single item, not the graph
    build_graph_state()  1.06 s     <- Brandes betweenness over 883 nodes
    fiedler curve        0.20 s
    feed scheduler       0.12 s
    scenario cache purge 0.03 s

So this module moves the two expensive, process-global, idempotent steps — the ML
artifact load and the graph build — off the lifespan and onto a single background
thread that starts the instant the server begins accepting connections. The cheap
steps (scheduler, cache purge, cleanup task) stay in the lifespan because they have
per-lifespan lifecycles that a once-per-process thread would break.

WHAT THIS MUST NOT DO
---------------------
Nothing the API returns may change. Several call sites degrade *silently* when their
process global is still None:

    app/optimization/costs.py      factory lead time -> available=False
    app/api/ml.py                  null metrics
    app/api/graph.py, benchmark.py 503

Before deferral those branches were unreachable in production, because the lifespan
had already populated both globals before the first request could arrive. They must
STAY unreachable. Every one of those call sites therefore waits on the warm-up
(`wait_for_graph()` / `wait_for_ml()`) before reading its global, so a request that
lands mid-warm-up gets the *same* answer it always got — just later. It must never
get a faster, different, degraded one.

The waits are no-ops when no warm-up was ever started (a bare ``TestClient(app)``
that is not entered as a context manager never runs the lifespan), so the existing
"no graph state -> 503" tests keep passing unchanged.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: Warm-up step names.
GRAPH = "graph"
ML = "ml"
STEPS: List[str] = [GRAPH, ML]

#: Upper bound on how long a request will block waiting for a warm-up step.
#:
#: Deliberately far longer than any observed warm-up (worst measured Render lifespan
#: was 33.7 s for BOTH steps). It exists only so a hung build cannot pin a request
#: thread forever. Tripping it leaves the global as None, which lands the caller on
#: exactly the same "not loaded" branch it would have taken if the build had raised —
#: the pre-existing failure behaviour, not a new one.
DEFAULT_WAIT_SECONDS = 180.0


class _Step:
    """One warm-up step: a completion event plus its outcome, for diagnostics."""

    __slots__ = ("name", "done", "error", "seconds")

    def __init__(self, name: str) -> None:
        self.name: str = name
        self.done: threading.Event = threading.Event()
        self.error: Optional[str] = None
        self.seconds: Optional[float] = None


#: How long :func:`start` and :func:`shutdown` will wait for a previous warm-up
#: thread to finish before giving up on it.
JOIN_SECONDS = 120.0

_lock = threading.Lock()
_steps: Dict[str, _Step] = {name: _Step(name) for name in STEPS}
#: The GraphState / MLState assignment counters as they stood when `start()` was
#: called. A step publishes what it produced ONLY if its counter has not moved since
#: — see `_load_ml` / `_build_graph`. Captured at start, not at publish time: a test
#: installs its own state milliseconds after entering the lifespan, long before this
#: thread reaches the assignment, so a counter read at publish time would look
#: unchanged and the warm-up would overwrite the fixture.
_start_epochs: Dict[str, int] = {}
_thread: Optional[threading.Thread] = None
_started = False


def is_started() -> bool:
    """True once :func:`start` has spawned a warm-up thread."""
    return _started


def status() -> Dict[str, object]:
    """Diagnostic snapshot of the warm-up. Not published by any endpoint."""
    return {
        "started": _started,
        "steps": {
            name: {
                "done": step.done.is_set(),
                "error": step.error,
                "seconds": step.seconds,
            }
            for name, step in _steps.items()
        },
    }


def wait(name: str, timeout: Optional[float] = None) -> bool:
    """
    Block until warm-up step ``name`` has finished; return True if it has.

    Returns immediately when no warm-up was ever started in this process — there is
    then nothing to wait for and the caller sees whatever the process global already
    holds, exactly as it did before this module existed.
    """
    if not _started:
        return True
    step = _steps.get(name)
    if step is None:
        return True
    if step.done.is_set():
        return True
    if timeout is None:
        timeout = DEFAULT_WAIT_SECONDS
    t0 = time.perf_counter()
    finished = step.done.wait(timeout)
    waited = time.perf_counter() - t0
    if not finished:
        logger.error(
            "Startup warm-up step %r did not finish within %.0f s — the caller will "
            "see the not-loaded branch. This is the same outcome as a failed build.",
            name, timeout,
        )
    elif waited > 0.5:
        logger.info("Waited %.2f s for startup warm-up step %r", waited, name)
    return finished


def wait_for_graph(timeout: Optional[float] = None) -> bool:
    """Block until the background graph build has finished (or was never started)."""
    return wait(GRAPH, timeout)


def wait_for_ml(timeout: Optional[float] = None) -> bool:
    """Block until the background ML artifact load has finished (or never started)."""
    return wait(ML, timeout)


# ── The warm-up itself ────────────────────────────────────────────────────────


def _load_ml() -> None:
    """Resolve and install the serving model. Body lifted verbatim from the lifespan."""
    from app.ml import install_ml_state_if_unchanged, ml_state_epoch
    from app.ml.serving import load_ml_state

    try:
        # Record the epoch BEFORE the load, and publish only if it has not moved.
        # A load that takes seconds must not land on top of a state that somebody
        # installed while it ran — which, before this guard, is exactly how the
        # background load broke `tests/test_stress_vintage.py`: the test set its own
        # stub MLState right after entering the lifespan and the real artifacts
        # overwrote it a beat later.
        epoch = _start_epochs.get(ML, ml_state_epoch())
        state = load_ml_state()
        if state is not None:
            if not install_ml_state_if_unchanged(state, epoch):
                logger.info(
                    "ML warm-up result discarded: the MLState was replaced while it loaded"
                )
                return
            prov = state.provenance or {}
            logger.info(
                "ML models loaded (source=%s, model=%s, version=%s, stress_prob=%.3f)",
                prov.get("model_source"), prov.get("model_name"),
                prov.get("model_version"), state.current_stress_prob,
            )
    except Exception as exc:
        # This used to be a one-line warning, and that is exactly how a silent
        # production outage shipped: the deployed image pinned scikit-learn 1.3.2
        # while the artifacts had been pickled by 1.8.0, so every boot logged
        # `ML model load skipped: No module named '_loss'` and the API served
        # model_source="none" with null metrics, indistinguishable from "no models
        # trained yet". Log the full traceback and name the most likely cause, so
        # the next unpickle failure is diagnosable from the Render log alone.
        try:
            import sklearn
            import numpy
            env = f"scikit-learn=={sklearn.__version__}, numpy=={numpy.__version__}"
        except Exception:  # noqa: BLE001
            env = "scikit-learn/numpy not importable"
        logger.error(
            "ML MODEL LOAD FAILED — the API will report model_source='none' and null "
            "metrics on /ml/* until this is fixed. %s: %s. Runtime env: %s. If this is "
            "an unpickling error (ModuleNotFoundError, AttributeError, "
            "InconsistentVersionWarning), the runtime versions do not match the ones "
            "that pickled data/ml_models/*.joblib — compare against "
            "metrics.joblib['provenance']['sklearn_version'] and re-pin "
            "backend/requirements.txt to match.",
            type(exc).__name__, exc, env, exc_info=True,
        )


def _build_graph() -> None:
    """
    Build the GraphState and pre-compute the Fiedler curve.

    Goes through :func:`app.graph.ensure_graph_state`, which is single-flight: if a
    request beat the warm-up to it and is already building, this blocks on that one
    build instead of starting a second. 883 nodes of Brandes betweenness twice over
    on a 0.5-CPU worker is a self-inflicted denial of service.
    """
    from app.core.database import SessionLocal
    from app.graph import ensure_graph_state

    try:
        db = SessionLocal()
        try:
            gs = ensure_graph_state(db, only_if_epoch=_start_epochs.get(GRAPH))
            # Phase 4 (BENCH-05): pre-compute sequential-removal λ₂ curve.
            # Inner try/except so a Fiedler failure doesn't kill the whole graph build.
            try:
                import concurrent.futures

                from app.main import compute_fiedler_curve

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(compute_fiedler_curve, gs, db, 5)
                    try:
                        gs.fiedler_curve = future.result(timeout=10)
                        logger.info(
                            "Fiedler curve: %d steps pre-computed", len(gs.fiedler_curve)
                        )
                    except concurrent.futures.TimeoutError:
                        logger.warning(
                            "Fiedler curve pre-compute timed out (>10s) — skipped"
                        )
                        gs.fiedler_curve = []
            except Exception as fiedler_exc:  # noqa: BLE001
                logger.warning("Fiedler curve pre-compute skipped: %s", fiedler_exc)
                gs.fiedler_curve = []
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Graph build skipped: %s", exc)


_RUNNERS = {GRAPH: _build_graph, ML: _load_ml}


def _run(steps: Dict[str, _Step]) -> None:
    """
    Execute every warm-up step, in order, on this one background thread.

    Sequential on purpose: the deployed worker has 0.5 of a CPU, so running the
    betweenness computation and the sklearn import concurrently would only trade
    wall-clock for peak RSS on a 512 MB instance. Graph goes first because it is
    the shorter job and gates more endpoints (/graph, /benchmark, /resilience)
    than the ML load does.
    """
    total = time.perf_counter()
    for name in STEPS:
        step = steps[name]
        t0 = time.perf_counter()
        try:
            _RUNNERS[name]()
        except BaseException as exc:  # noqa: BLE001 — a step must never kill the thread
            step.error = f"{type(exc).__name__}: {exc}"
            logger.error("Startup warm-up step %r failed: %s", name, exc, exc_info=True)
        finally:
            step.seconds = time.perf_counter() - t0
            # Set LAST: a waiter that wakes must find the global already installed.
            step.done.set()
            logger.info("Startup warm-up step %r finished in %.2f s", name, step.seconds)
    logger.info(
        "Startup warm-up complete in %.2f s (%s)",
        time.perf_counter() - total,
        ", ".join(f"{n}={steps[n].seconds:.2f}s" for n in STEPS),
    )


def start() -> bool:
    """
    Spawn the warm-up thread and return immediately.

    ONCE PER LIFESPAN, not once per process — deliberately, because that is exactly
    what the synchronous version it replaces did. In production there is one lifespan,
    so the distinction is invisible. In the test suite it is not: ``conftest``'s autouse
    ``restore_process_globals`` puts the GraphState and MLState back to what they were
    before each test, so a once-per-process warm-up would populate them during the first
    ``with TestClient(app)``, have them reset at that test's teardown, and leave every
    later test looking at a None the old code would have rebuilt. Same work, same
    frequency, same resulting state — just off the critical path.

    Never runs two warm-ups at once: a previous thread is joined first.
    """
    global _thread, _started, _steps

    previous = _thread
    if previous is not None and previous.is_alive():
        previous.join(JOIN_SECONDS)
        if previous.is_alive():
            logger.warning(
                "Previous startup warm-up still running after %.0f s; starting another "
                "would double the work on a 0.5-CPU worker — skipping this one.",
                JOIN_SECONDS,
            )
            return False

    from app.graph import graph_state_epoch
    from app.ml import ml_state_epoch as _ml_epoch

    with _lock:
        steps = {name: _Step(name) for name in STEPS}
        _steps = steps
        _start_epochs.clear()
        _start_epochs[GRAPH] = graph_state_epoch()
        _start_epochs[ML] = _ml_epoch()
        _started = True
        _thread = threading.Thread(
            target=_run, args=(steps,), name="startup-warmup", daemon=True
        )
        _thread.start()
    return True


def join(timeout: Optional[float] = None) -> bool:
    """Block until the warm-up thread has exited. Returns True if it has."""
    thread = _thread
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def shutdown(timeout: float = JOIN_SECONDS) -> bool:
    """
    Join the warm-up at lifespan shutdown.

    Keeps teardown deterministic: no warm-up thread outlives the app that started it,
    so it cannot publish a GraphState into a process that has moved on — which in the
    test suite would mean one test's teardown racing the next test's setup.
    """
    return join(timeout)
