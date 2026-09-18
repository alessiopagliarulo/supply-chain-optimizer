from contextlib import asynccontextmanager
import asyncio
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api import api_router
from app.core.config import settings
from app.core.database import Base, engine
from app import models  # noqa: F401 — ensure all ORM models are registered before create_all

# Configure OpenTelemetry SDK.
# Tracing is opt-in: Jaeger is optional for the local demo, and exporting to a
# Jaeger agent that isn't running spams stderr with "OSError: Message too long"
# (oversized UDP batches with no listener). Enable explicitly with OTEL_ENABLED=true.
logger = logging.getLogger(__name__)
_OTEL_ENABLED = os.getenv("OTEL_ENABLED", "").lower() in ("1", "true", "yes")
if _OTEL_ENABLED:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.jaeger.thrift import JaegerExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        jaeger_exporter = JaegerExporter(
            agent_host_name=os.getenv("JAEGER_AGENT_HOST", "localhost"),
            agent_port=int(os.getenv("JAEGER_AGENT_PORT", "6831")),
            udp_split_oversized_batches=True,  # avoid OSError [Errno 40] on large batches
        )
        _tracer_provider = TracerProvider()
        _tracer_provider.add_span_processor(BatchSpanProcessor(jaeger_exporter))
        trace.set_tracer_provider(_tracer_provider)
        logger.info("OpenTelemetry configured with Jaeger exporter (localhost:6831)")
    except ImportError:
        logger.warning("OpenTelemetry not available — tracing disabled")
    except Exception as e:
        logger.warning(f"OpenTelemetry initialization failed: {e}")
else:
    logger.info("OpenTelemetry tracing disabled (set OTEL_ENABLED=true to enable)")

# Create tables
Base.metadata.create_all(bind=engine)


def _reference_boms(db):
    """
    Load the benchmarked reference BOMs as {bom_name: [component_id, ...]}.

    Source is the latest `optimization_runs` run_id — the rows written by the
    sourcing benchmark archived at git tag `archive/sourcing-v1`. Reading them from
    the database keeps the app layer independent of the seed scripts, which are not
    on the path in production.

    Returns (boms, source_note). `boms` is empty when no benchmark has ever been run
    or when an MPN cannot be resolved to a Component — never a silent partial answer.
    """
    from app.models.component import Component
    from app.models.optimization_run import OptimizationRun

    latest = (
        db.query(OptimizationRun.run_id)
        .order_by(OptimizationRun.run_id.desc())
        .first()
    )
    if latest is None:
        return {}, "no optimization_runs rows — collapse check did not run"
    run_id = int(latest[0])

    rows = (
        db.query(OptimizationRun.bom_name, OptimizationRun.bom_items_json)
        .filter(OptimizationRun.run_id == run_id)
        .all()
    )
    mpns_by_bom = {}
    for name, items in rows:
        if name in mpns_by_bom:
            continue
        mpns_by_bom[name] = [
            str(i.get("mpn")) for i in (items or []) if isinstance(i, dict) and i.get("mpn")
        ]
    mpns_by_bom = {n: m for n, m in mpns_by_bom.items() if m}
    if not mpns_by_bom:
        return {}, f"run_id={run_id} recorded no BOM lines — collapse check did not run"

    wanted = sorted({m for mpns in mpns_by_bom.values() for m in mpns})
    id_by_mpn = {
        c.mpn: c.id
        for c in db.query(Component).filter(Component.mpn.in_(wanted)).all()
    }

    boms = {}
    dropped = []
    for name, mpns in mpns_by_bom.items():
        ids = [id_by_mpn.get(m) for m in mpns]
        if any(cid is None for cid in ids):
            # A line we cannot resolve is a catalogue problem, not a removal effect.
            # Dropping the BOM is honest; scoring it would attribute a data gap to
            # the distributor removal.
            dropped.append(name)
            continue
        boms[name] = [int(cid) for cid in ids]

    # "the 80% training partition" until 2026-09-05. That partition no longer exists:
    # `graph/builder.py` carved out a random 20% of supplier-part links for a
    # train/test split that had outlived the evaluation it was built for, and it was
    # removed once found (5,789 -> 7,363 edges). The label survived the code it
    # described by about four months, which is the failure this comment now guards.
    note = (
        f"benchmark run_id={run_id} ({len(boms)} BOMs), checked against the full "
        f"offer graph — the same graph λ₂ is computed on"
    )
    if dropped:
        note += f"; {len(dropped)} BOM(s) skipped, unresolved MPNs: {', '.join(sorted(dropped))}"
    return boms, note


def compute_fiedler_curve(gs, db, top_k: int = 5):
    """
    Pre-compute sequential-removal λ₂ curve for the top-k highest-betweenness distributors.

    Uses method="lanczos" on the UNWEIGHTED laplacian of the largest connected component.
    Guards against Pitfall #1 (tracemin_pcg hang on stock-weighted bipartite graphs —
    returned λ₂=0 in 146s on 847-node LCC in Phase 2).

    Returns a list of exactly `top_k + 1` dicts:
        [
          {"step": 0, ..., "lambda2": base, "delta_pct": 0.0, "collapsed_boms": [...]},
          {"step": 1, "removed": did, "removed_name": name, "lambda2": ..., ...},
          ...
        ]

    Each step removes the distributor node with the next-highest betweenness and
    recomputes λ₂ on the remaining largest connected component.

    `collapsed_boms` is the cumulative list of reference BOMs that have at least
    one line with no supplier left in the graph after the removals up to and
    including that step. It used to be documented here and never written, so the
    API always served `[]` and the Benchmark page invited a click that could not
    reveal anything. It is now computed on the same graph as λ₂. The step-0 entry
    also carries `boms_checked` and `bom_source` so the API can tell an empty list
    ("checked, nothing collapsed") apart from an absent check.
    """
    import logging
    import time

    import networkx as nx

    from app.models.distributor import Distributor

    logger = logging.getLogger(__name__)
    dist_name_by_id = {d.id: d.name for d in db.query(Distributor).all()}

    Gu = gs.graph.to_undirected()
    curve = []

    def _lambda2(G):
        if G.number_of_nodes() <= 2:
            return 0.0
        ccs = list(nx.connected_components(G))
        if not ccs:
            return 0.0
        cc = max(ccs, key=len)
        Gsub = G.subgraph(cc).copy()
        # Strip edge weights — Pitfall #1 mitigation (lanczos on unweighted laplacian)
        for u, v in Gsub.edges():
            Gsub[u][v]["weight"] = 1.0
        try:
            t0 = time.time()
            lam = nx.algebraic_connectivity(Gsub, method="lanczos", normalized=False)
            elapsed = time.time() - t0
            if elapsed > 5.0:
                logger.warning(
                    "Fiedler lanczos slow (%.1fs) on %d nodes",
                    elapsed, Gsub.number_of_nodes(),
                )
            return float(lam) if lam > 0 else 0.0
        except Exception as exc:
            logger.warning("Fiedler lanczos failed: %s — returning 0.0", exc)
            return 0.0

    # Reference BOMs for the fulfillability column. A BOM "collapses" when one of
    # its lines has no distributor left in `Gtmp` — degree 0, or the component node
    # is absent from the graph entirely.
    try:
        bom_components, bom_source = _reference_boms(db)
    except Exception as exc:  # noqa: BLE001 — never let this kill the λ₂ curve
        logger.warning("Fiedler collapse check skipped: %s", exc)
        bom_components, bom_source = {}, f"collapse check failed: {exc}"

    def _collapsed(G) -> list:
        out = []
        for name, cids in bom_components.items():
            for cid in cids:
                node = f"c_{cid}"
                if not G.has_node(node) or G.degree(node) == 0:
                    out.append(name)
                    break
        return sorted(out)

    base_lambda = _lambda2(Gu)
    curve.append({
        "step": 0, "removed": None, "removed_name": None,
        "lambda2": base_lambda, "delta_pct": 0.0,
        "collapsed_boms": _collapsed(Gu),
        "boms_checked": len(bom_components),
        "bom_source": bom_source,
    })

    top_dists = sorted(gs.betweenness.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    Gtmp = Gu.copy()
    for step, (did, _btwn) in enumerate(top_dists, start=1):
        node = f"d_{did}"
        if Gtmp.has_node(node):
            Gtmp.remove_node(node)
        lam = _lambda2(Gtmp)
        delta_pct = (
            (lam - base_lambda) / max(base_lambda, 1e-9) * 100
            if base_lambda > 0 else 0.0
        )
        curve.append({
            "step": step,
            "removed": int(did),
            "removed_name": dist_name_by_id.get(did, f"distributor-{did}"),
            "lambda2": lam,
            "delta_pct": round(delta_pct, 1),
            "collapsed_boms": _collapsed(Gtmp),
        })

    # Pad to exactly top_k+1 entries even if fewer distributors were available
    while len(curve) < top_k + 1:
        # No further removal happened on these padded steps, so the collapse set is
        # whatever it already was — reporting [] here would make the column
        # non-monotone and read as "the BOMs came back".
        curve.append({
            "step": len(curve), "removed": None, "removed_name": None,
            "lambda2": 0.0, "delta_pct": -100.0,
            "collapsed_boms": _collapsed(Gtmp),
        })
    return curve


@asynccontextmanager
async def lifespan(app):
    # ── Live data feeds ────────────────────────────────────────────────────────
    _scheduler = None
    try:
        from app.feeds.scheduler import build_scheduler
        from app.feeds import set_live_data_cache, LiveDataCache
        _ldc = LiveDataCache()
        set_live_data_cache(_ldc)
        _scheduler = build_scheduler(_ldc)
        _scheduler.start()
        import logging
        logging.getLogger(__name__).info("Feed scheduler started (15-min interval)")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Feed scheduler start skipped: %s", exc)

    # ── Scenario cache: drop everything a previous build wrote ────────────────
    # Cache keys carry code_version(), so an older build's entries can never be
    # read back — this is purely so the tracked DB does not accumulate rows that
    # are dead on arrival. It also warms the source fingerprint before the first
    # request needs it.
    try:
        from app.cache import CacheManager
        from app.core.database import SessionLocal as _SessionLocal
        from app.core.version import build_commit, code_version

        _cache_db = _SessionLocal()
        try:
            _purged = CacheManager.purge_foreign_versions(_cache_db)
            logging.getLogger(__name__).info(
                "Scenario cache pinned to build %s (%s); purged %d entry(ies) "
                "written by other builds",
                code_version(), build_commit()[:12], _purged,
            )
        finally:
            _cache_db.close()
    except Exception as exc:
        logging.getLogger(__name__).warning("Scenario cache version purge skipped: %s", exc)

    # ── Background cache cleanup job (Phase 6 - performance) ──────────────────
    _cleanup_task = None
    try:
        import logging as _log_module
        _logger = _log_module.getLogger(__name__)

        async def cleanup_loop():
            """Run cache cleanup every 10 minutes."""
            from app.cache import CacheManager
            # Imported HERE on purpose. This name used to be a closure over a local
            # that the (now-deferred) graph-build block happened to import into
            # `lifespan`'s scope — an accidental binding that would have become a
            # NameError ten minutes after boot the moment that block moved.
            from app.core.database import SessionLocal

            while True:
                try:
                    await asyncio.sleep(600)  # 10 minutes
                    db = SessionLocal()
                    try:
                        deleted = CacheManager.cleanup_expired(db)
                        if deleted > 0:
                            _logger.info(f"Cache cleanup: deleted {deleted} expired entries")
                        stale = CacheManager.purge_foreign_versions(db)
                        if stale > 0:
                            _logger.info(
                                f"Cache cleanup: deleted {stale} entries from other builds"
                            )
                    except Exception as e:
                        _logger.error(f"Cache cleanup failed: {e}")
                    finally:
                        db.close()
                except asyncio.CancelledError:
                    _logger.info("Cache cleanup task cancelled")
                    break
                except Exception as e:
                    _logger.error(f"Cache cleanup loop error: {e}")

        _cleanup_task = asyncio.create_task(cleanup_loop())
        _logger.info("Background cache cleanup scheduled (10-min interval)")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Cache cleanup job setup skipped: %s", exc)

    # ── Deferred warm-up: the ML artifact load and the graph build ────────────
    #
    # Uvicorn accepts no connection until this coroutine reaches its `yield`, so
    # anything done here is on the critical path of a cold start. On Render's
    # 0.5-CPU free tier these two steps measured 30.2-33.7 s across 8 real cold
    # starts, out of a ~70 s wake — a recruiter opening the live link waited for
    # every second of it.
    #
    # They now run on a background thread (app/startup.py) and the server begins
    # serving immediately. Nothing the API returns changes: every call site that
    # reads either process global first waits on the corresponding warm-up step,
    # so a request landing mid-warm-up gets the same answer it always got, just
    # later — never a faster, degraded, different one.
    #
    # STARTED LAST, deliberately. The warm-up thread is CPU-bound and holds the GIL,
    # so kicking it off at the top of the lifespan made the three cheap steps above
    # take 1.35 s instead of 0.15 s while they fought it for the interpreter — and
    # every one of those seconds is still pre-`yield`, i.e. still a second before the
    # first connection is accepted. Measured locally; on half a CPU it is worse.
    from app import startup as _startup

    _startup.start()

    yield

    # Join the warm-up thread so it cannot outlive the app that started it.
    _startup.shutdown()

    # Cleanup: shut down scheduler and cache cleanup task
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass

    if _cleanup_task is not None:
        try:
            _cleanup_task.cancel()
        except Exception:
            pass


app = FastAPI(
    title=settings.PROJECT_NAME,
    debug=settings.DEBUG,
    lifespan=lifespan,
)

# Parse CORS origins from settings (D-05)
origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
if settings.DEBUG:
    # D-06: Always include localhost origins in dev mode
    dev_origins = {"http://localhost:5173", "http://localhost:3000"}
    origins = list(set(origins) | dev_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(api_router, prefix=settings.API_V1_STR)


class HealthResponse(BaseModel):
    """Liveness probe payload. Declared so generated clients are not untyped here."""
    status: str


class VersionResponse(BaseModel):
    """Deployed build identity, read by ./launch and the UI build badge."""
    commit: str
    service: str


@app.get("/health", response_model=HealthResponse)
def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/version", response_model=VersionResponse)
def version_info():
    """Deployed build info — used by ./launch and the UI build badge.

    The commit comes from `app.core.version.build_commit()`, the same helper the
    scenario cache keys on, so "which build is live" and "which build wrote this
    cached response" can never be two different answers.
    """
    from app.core.version import build_commit

    return {
        "commit": build_commit(),
        "service": os.getenv("RENDER_SERVICE_NAME", "local"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
