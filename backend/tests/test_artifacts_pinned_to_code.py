"""
Drift guard: the committed ARTIFACTS must agree with the CODE that produced them.

Why this file exists (2026-08-29)
---------------------------------
``tests/test_benchmark_docs_match_artifacts.py`` compares the published markdown
to the committed JSON. That is a *doc-vs-artifact* check, and it has a blind spot
the repo has now hit twice:

    commit 6a33ad0 changed ``app/optimization/sourcing.py`` (milli-cent
    quantisation, risk surcharges, an ``is_chinese_origin`` double-count) and
    hand-edited only the ``meta`` prose of ``docs/volume_sweep.json``. The
    generator was never re-run. The doc and the artifact were stale *together*,
    so ``test_volume_curve_pooled_table_matches_sweep_json`` stayed green while
    both disagreed with the optimizer. A later real re-run of
    ``python -m seeds.run_volume_sweep`` moved a cell: $181,919.39 → $181,908.01,
    5 → 6 suppliers, and the pooled 10,000x row 7.96% → 7.97%.

The repo's standing bar names exactly this failure: *"twice shipped figures that two documents
agreed on while both disagreed with the code."* Nothing can catch it except a
check that re-runs the real code path and compares the result to the bytes on
disk. That is what this file does.

What is pinned here
-------------------
1. ``docs/volume_sweep.json`` — every point of the PRIMARY (deduped) grid is
   re-solved through ``seeds.run_volume_sweep._run_point``, i.e. the generator's
   own function calling the real ``solve_sourcing`` / ``solve_sourcing_greedy``.
   Nothing is reimplemented here. Measured cost: **0.39 s for all 80 points**
   (10 BOMs x 13 multipliers, trimmed to each BOM's stock ceiling, 3 arms each),
   plus ~0.02 s of offer loading. Cheap enough to pin the whole grid rather than
   one token point.

2. ``frontend/src/lib/volumeDecayCurveData.ts`` — the hardcoded fallback table
   the ``/benchmark`` page renders whenever the API is unreachable. It is a
   hand-copied projection of ``docs/volume_sweep.json`` and had already drifted
   once. Here it is re-derived from the committed artifact using the generator's
   own ``_pooled_rows`` aggregation and compared row by row.

3. ``docs/diversification_frontier.json`` — the whole k = 1..K sweep is re-solved
   through ``seeds.run_diversification_sweep.sweep_bom``. Measured: 1.0 s to build
   the graph state + 0.15 s for all 10 BOMs. It previously had NO test of any kind
   tying it to anything — not even a doc comparison.

4. ``docs/newsvendor.json`` — the PRIMARY configuration is re-run through
   ``app.optimization.newsvendor.run_panel_evaluation`` at the artifact's own
   ``n_boot``/``seed``. Measured: 3.3 s. Its existing test file states in its own
   docstring that it "does not re-run the evaluation"; this is the missing half.

5-8. The four HEAVY artifacts — see the block at the bottom of this file.
   ``cvar_frontier.json`` (primary arm), ``leakage_progression.json`` (panel +
   one fold per regime), ``forecast_backtest.json`` and
   ``chronos_benchmark.json``. Added 2026-08-30, together ~50 s. The 2026-08-29
   sizing that left them unpinned had costed REGENERATING each artifact, not
   re-solving a canonical slice of it. ``cvar_frontier.json`` was ALREADY STALE
   when its pin was written.

   The two demand-series artifacts are each pinned by TWO tests, split by whether
   the arm is REPRODUCIBLE rather than by what it costs:

     * the seasonal-naive arm of each is deterministic — it copies observations
       out of a SHA-256-pinned series and does no arithmetic of its own — so it
       is UNMARKED and runs in CI's default suite. Sections 1-4 above were
       already unmarked and already ran there, so this is NOT CI's first
       artifact-vs-code pin; it is the first for the two DEMAND-SERIES
       artifacts, which were behind `slow` in their entirety.
     * the Prophet arms and the Chronos arm stay ``@pytest.mark.slow``, i.e.
       local-only. Prophet fits via Stan (L-BFGS) and is NOT bit-reproducible
       across platform / interpreter / BLAS; promoting it into CI on 2026-08-30
       turned CI red with 160 differing values against artifacts that were
       entirely current. Both halves keep the SAME strict tolerance — widening it
       until a non-deterministic fit passed would be a check that cannot fail.
     * ``cvar_frontier.json`` and ``leakage_progression.json`` stay ``slow`` for
       the older reason: they need machine-local state CI does not have.

Deliberately NOT pinned, with sizing (see the survey in this task's report):
  * ``points_raw_pool`` of the volume sweep — the declared CONTROL arm, already
    tied to the primary pool by ``test_raw_and_deduped_pools_agree``.
  * ``benchmark_results.json`` (~137 s) — already has a real pin in
    ``tests/test_diversification_frontier.py::test_unconstrained_solve_still_
    reproduces_published_run5_costs``.
  * ``intermittent_demand.json`` (~22 s) — already cross-pinned by
    ``test_newsvendor.py::test_the_recomputed_mase_reproduces_the_published_
    leaderboard``.
  * ``backend_verification.json`` — HONESTLY UNPINNABLE, reasoned out in full in
    the block at the bottom of this file. No generator for it exists in this repo.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
SWEEP_JSON = DOCS / "volume_sweep.json"
FRONTEND_FALLBACK_TS = REPO_ROOT / "frontend" / "src" / "lib" / "volumeDecayCurveData.ts"
DB_PATH = REPO_ROOT / "backend" / "supply_chain.db"

REGENERATE = (
    "Re-run `cd backend && ./venv/bin/python -m seeds.run_volume_sweep` "
    "(~1 s) and commit the regenerated docs/volume_sweep.json + "
    "docs/BENCHMARK_VOLUME_CURVE.md."
)

# `_decompose` rounds every dollar figure to cents before it is written out, so
# an exact match is what a reproduction should produce; the tolerances below only
# absorb last-bit float noise across platforms. They are deliberately far tighter
# than the drift they exist to catch (the real 2026-08-29 drift was $11.38 on a
# cost and 0.01pp on a percentage).
MONEY_TOL = 0.011
# Newsvendor carries raw probabilities and per-SKU costs, where 0.011 absolute
# would swallow a real change. Compared on the tighter of absolute/relative.
STAT_ABS_TOL = 1e-9
STAT_REL_TOL = 1e-9

# Wall-clock fields cannot reproduce and are the ONLY ones allowed to differ.
SWEEP_SKIP_FIELDS = frozenset({"solve_seconds"})

# The seeded snapshot every published number is computed from. Asserted rather
# than assumed: DATABASE_URL is CWD-relative and SQLite CREATES rather than
# fails, so running from the wrong directory yields a silently empty database
# (loop learnings log, 2026-08-28).
EXPECTED_ROW_COUNTS = {
    "components": 791,
    "distributors": 92,
    "distributor_offers": 8176,
}


# ── shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sweep() -> Dict[str, Any]:
    if not SWEEP_JSON.is_file():
        pytest.fail(f"docs/volume_sweep.json is missing. {REGENERATE}")
    return json.loads(SWEEP_JSON.read_text())


# ── 1. volume_sweep.json vs the live optimizer ───────────────────────────────

def _compare(
    path: str,
    expected: Any,
    actual: Any,
    problems: List[str],
    *,
    abs_tol: float = MONEY_TOL,
    rel_tol: float = 0.0,
    skip_fields: frozenset = frozenset(),
) -> None:
    """Structural comparison that reports EVERY differing leaf, not just the first."""
    kw = {"abs_tol": abs_tol, "rel_tol": rel_tol, "skip_fields": skip_fields}
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            problems.append(f"{path}: expected an object, re-solve produced {type(actual).__name__}")
            return
        for key in expected:
            if key in skip_fields:
                continue
            if key not in actual:
                problems.append(f"{path}.{key}: missing from the re-solve")
                continue
            _compare(f"{path}.{key}", expected[key], actual[key], problems, **kw)
        for key in actual:
            if key not in expected and key not in skip_fields:
                problems.append(f"{path}.{key}: the re-solve produced a field the artifact lacks")
        return

    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            problems.append(
                f"{path}: artifact has {len(expected)} entries, re-solve produced "
                f"{len(actual) if isinstance(actual, list) else type(actual).__name__}"
            )
            return
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            _compare(f"{path}[{i}]", e, a, problems, **kw)
        return

    if isinstance(expected, bool) or isinstance(actual, bool):
        if expected != actual:
            problems.append(f"{path}: artifact={expected!r} re-solve={actual!r}")
        return

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        tol = max(abs_tol, rel_tol * abs(float(expected)))
        if abs(float(expected) - float(actual)) > tol:
            problems.append(f"{path}: artifact={expected!r} re-solve={actual!r}")
        return

    if expected != actual:
        problems.append(f"{path}: artifact={expected!r} re-solve={actual!r}")


def test_volume_sweep_artifact_reproduces_from_the_live_optimizer(sweep):
    """
    THE pin: every committed point of the primary sweep grid must still fall out
    of the real optimizer.

    This is the check that ``6a33ad0`` needed and did not have. It calls
    ``seeds.run_volume_sweep._run_point`` — the exact function ``main()`` calls —
    so a change anywhere under ``app/optimization/`` that moves a published cost
    shows up here immediately, whatever the markdown says.
    """
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("ortools")
    if not DB_PATH.exists():
        pytest.skip("backend/supply_chain.db not present")

    from sqlalchemy import text

    from app.core.database import SessionLocal
    from app.optimization.strategies import get_strategy
    from seeds.run_benchmark import BOM_CATALOG, _load_offers_for_bom
    from seeds.run_volume_sweep import STRATEGY_ID, _dedupe_offers, _run_point

    assert sweep["meta"]["strategy"] == STRATEGY_ID, (
        f"the artifact was generated with strategy {sweep['meta']['strategy']!r} but the "
        f"generator now uses {STRATEGY_ID!r}. {REGENERATE}"
    )

    weights = get_strategy(STRATEGY_ID)
    db = SessionLocal()
    problems: List[str] = []
    points_checked = 0
    started = time.perf_counter()
    try:
        for name, counted in EXPECTED_ROW_COUNTS.items():
            got = db.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
            assert got == counted, (
                f"the database this test read has {got} rows in {name}, not {counted}. "
                "Either the seed drifted or pytest was launched from the wrong "
                "directory (DATABASE_URL is CWD-relative and SQLite creates an empty "
                "file rather than failing). Run pytest from backend/."
            )

        for bom_name, entry in sweep["boms"].items():
            expected_points = entry.get("points")
            if not expected_points:
                continue
            assert bom_name in BOM_CATALOG, (
                f"docs/volume_sweep.json carries results for {bom_name!r}, which is no "
                f"longer in run_benchmark.BOM_CATALOG. {REGENERATE}"
            )
            items = BOM_CATALOG[bom_name]
            bom, raw_offers, _meta = _load_offers_for_bom(db, items)
            assert bom and raw_offers, (
                f"{bom_name}: the database returned no BOM lines or no offers, so this "
                "test would have checked nothing."
            )
            deduped, _dups = _dedupe_offers(raw_offers)

            for expected in expected_points:
                actual = _run_point(
                    bom_name, items, bom, deduped, weights, expected["multiplier"]
                )
                points_checked += 1
                for arm_id in ("greedy", "milp_matched", "milp_bench"):
                    arm = actual["arms"].get(arm_id) or {}
                    if arm.get("hit_time_limit"):
                        problems.append(
                            f"boms.{bom_name}.m={expected['multiplier']}.{arm_id}: the "
                            "re-solve hit CP-SAT's 5 s limit and returned FEASIBLE rather "
                            "than OPTIMAL, so this machine cannot reproduce the artifact "
                            "deterministically. This is a solver-environment problem, NOT "
                            "artifact staleness."
                        )
                _compare(
                    f"boms.{bom_name}.points[m={expected['multiplier']}]",
                    expected,
                    actual,
                    problems,
                    skip_fields=SWEEP_SKIP_FIELDS,
                )
    finally:
        db.close()
    elapsed = time.perf_counter() - started

    # Anti-vacuity: this test must actually have solved something. The sweep grid
    # is 10 BOMs trimmed to their stock ceilings; it has never been under 70 points.
    assert points_checked >= 70, (
        f"only {points_checked} sweep points were re-solved — the pin has gone quiet, "
        "which is exactly how a published number drifts unnoticed."
    )
    assert elapsed < 60.0, (
        f"the re-solve took {elapsed:.1f}s; it is budgeted at well under a second and "
        "something has changed materially in the solver configuration."
    )

    assert not problems, (
        f"docs/volume_sweep.json no longer matches what the optimizer produces "
        f"({len(problems)} differing values across {points_checked} re-solved points).\n"
        f"The ARTIFACT is stale, not this test: the code under app/optimization/ has "
        f"moved and the artifact was not regenerated.\n{REGENERATE}\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


# ── 2. the frontend's hardcoded fallback vs the artifact ─────────────────────
#
# WHY A PYTHON TEST AND NOT A VITEST ONE
# --------------------------------------
# The frontend has no unit-test runner at all: frontend/package.json has no
# vitest/jest dependency, no `test` script and no runner config; the only
# automated frontend check is `npm run ui-gate`, a Playwright + axe-core visual
# gate that never reads docs/*.json. Adding a runner would mean a new dependency,
# a config file and new CI wiring for a single assertion.
#
# Meanwhile the aggregation that turns the artifact into these rows ALREADY EXISTS
# in Python — `seeds.run_volume_sweep._pooled_rows`, the same function that writes
# the markdown table. Deriving the expected rows from the generator's own helper
# means nothing is reimplemented, and backend/tests is the suite the standing gate
# runs. Reading a source file as text is also an established convention here (see
# tests/test_run_benchmark.py and tests/test_is_chinese_origin_propagation.py).

_TS_ROW_RE = re.compile(r"\{([^{}]*)\}")
_TS_FIELD_RE = re.compile(r"(\w+)\s*:\s*(-?\d+(?:\.\d+)?)")


def _parse_ts_fallback(source: str) -> List[Dict[str, float]]:
    """Extract VOLUME_SWEEP_FALLBACK's object literals as plain dicts."""
    m = re.search(
        r"export const VOLUME_SWEEP_FALLBACK\s*:\s*VolumeCurvePoint\[\]\s*=\s*\[(.*?)\n\];",
        source,
        re.S,
    )
    assert m, (
        "could not find `export const VOLUME_SWEEP_FALLBACK: VolumeCurvePoint[] = [...]` "
        f"in {FRONTEND_FALLBACK_TS.relative_to(REPO_ROOT)}. If the export was renamed or "
        "restructured, this pin must be updated with it — do not delete it."
    )
    rows = []
    for body in _TS_ROW_RE.findall(m.group(1)):
        fields = {k: float(v) for k, v in _TS_FIELD_RE.findall(body)}
        if fields:
            rows.append(fields)
    return rows


def _expected_fallback_rows(sweep: Dict[str, Any]) -> List[Dict[str, float]]:
    """
    Derive the fallback table from the committed artifact using the GENERATOR's
    own pooled aggregation, so this test cannot disagree with the markdown for
    reasons of arithmetic.
    """
    from seeds.run_volume_sweep import _points_at, _pooled_rows

    boms = sweep["boms"]
    out = []
    for r in _pooled_rows(boms):
        pts = _points_at(boms, r["m"])
        greedy_fixed = sum(float(p["greedy"]["fixed_fee_usd"]) for p in pts)
        out.append({
            "multiplier": float(r["m"]),
            "savings_pct": r["pct"],
            "n_boms": float(r["n"]),
            "units_min": float(r["units_min"]),
            "units_max": float(r["units_max"]),
            "fixed_fee_usd": r["fee"],
            "component_usd": r["comp"],
            "variable_freight_usd": r["var"],
            "fee_share_of_saving_pct": (r["fee"] / r["delta"] * 100.0) if r["delta"] else 0.0,
            "greedy_fixed_share_of_cost_pct": (
                greedy_fixed / r["greedy"] * 100.0 if r["greedy"] else 0.0
            ),
        })
    return out


# field -> (decimal places the TS literal carries, human label)
_FALLBACK_FIELDS: Tuple[Tuple[str, int], ...] = (
    ("savings_pct", 2),
    ("n_boms", 0),
    ("units_min", 0),
    ("units_max", 0),
    ("fixed_fee_usd", 0),
    ("component_usd", 0),
    ("variable_freight_usd", 0),
    ("fee_share_of_saving_pct", 0),
    ("greedy_fixed_share_of_cost_pct", 1),
)


def test_frontend_volume_fallback_matches_sweep_artifact(sweep):
    """
    ``VOLUME_SWEEP_FALLBACK`` is what a visitor sees on /benchmark whenever the
    API is unreachable — i.e. it is PUBLISHED. It is a hand-copied projection of
    docs/volume_sweep.json with nothing tying it to the source, and it had already
    drifted once. Every cell must be the correct rounding of the artifact's value.
    """
    assert FRONTEND_FALLBACK_TS.is_file(), (
        f"{FRONTEND_FALLBACK_TS.relative_to(REPO_ROOT)} is missing — the /benchmark "
        "page's offline fallback table."
    )
    actual = _parse_ts_fallback(FRONTEND_FALLBACK_TS.read_text())
    expected = _expected_fallback_rows(sweep)

    fix = (
        "Regenerate docs/volume_sweep.json if the OPTIMIZER moved "
        "(`cd backend && ./venv/bin/python -m seeds.run_volume_sweep`), then hand-update "
        f"VOLUME_SWEEP_FALLBACK in {FRONTEND_FALLBACK_TS.relative_to(REPO_ROOT)} to match "
        "the artifact. The artifact is the source of truth; the TS table is a copy."
    )

    assert [r["multiplier"] for r in actual] == [r["multiplier"] for r in expected], (
        "the multipliers in VOLUME_SWEEP_FALLBACK no longer match the pooled rows of "
        f"docs/volume_sweep.json.\n  TS       : {[int(r['multiplier']) for r in actual]}\n"
        f"  artifact : {[int(r['multiplier']) for r in expected]}\n{fix}"
    )

    problems: List[str] = []
    for got, want in zip(actual, expected, strict=True):
        m = int(want["multiplier"])
        for field, places in _FALLBACK_FIELDS:
            if field not in got:
                problems.append(f"m={m}: TS row is missing `{field}`")
                continue
            # The TS literal carries the artifact value rounded to `places`; anything
            # further away than half a unit in the last place is real drift.
            tol = 0.5 * (10 ** -places) + 1e-6
            if abs(got[field] - want[field]) > tol:
                problems.append(
                    f"m={m}.{field}: TS={got[field]!r} but the artifact gives "
                    f"{round(want[field], places)!r} (exact {want[field]:.4f})"
                )

    assert not problems, (
        f"{FRONTEND_FALLBACK_TS.relative_to(REPO_ROOT)} disagrees with "
        f"docs/volume_sweep.json in {len(problems)} place(s) — the /benchmark page "
        f"publishes these numbers when the API is down.\n{fix}\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


def test_frontend_production_floor_matches_generator():
    """
    The frontend labels everything at/above ``PRODUCTION_VOLUME_MIN_MULTIPLIER``
    "production volume" and quotes a saving range over that tail. The generator
    defines the same threshold as ``PRODUCTION_FLOOR`` and uses it for the
    equivalent sentence in the markdown. Two constants, one meaning.
    """
    from seeds.run_volume_sweep import PRODUCTION_FLOOR

    src = FRONTEND_FALLBACK_TS.read_text()
    m = re.search(r"export const PRODUCTION_VOLUME_MIN_MULTIPLIER\s*=\s*(\d+)", src)
    assert m, "PRODUCTION_VOLUME_MIN_MULTIPLIER not found in volumeDecayCurveData.ts"
    assert int(m.group(1)) == PRODUCTION_FLOOR, (
        f"the frontend calls >= {m.group(1)}x 'production volume' while "
        f"seeds/run_volume_sweep.PRODUCTION_FLOOR says {PRODUCTION_FLOOR}x. The page and "
        "the published markdown would describe different cohorts."
    )


# ── 3. diversification_frontier.json vs the live optimizer ───────────────────

DIVERSIFICATION_JSON = DOCS / "diversification_frontier.json"


def test_diversification_frontier_reproduces_from_the_live_optimizer():
    """
    ``docs/diversification_frontier.json`` had NO test tying it to anything — no
    doc comparison, no recompute. Its numbers reach the public
    ``/benchmark/diversification`` endpoint, and it was last generated at
    ``0a1aecab`` (2026-08-27), i.e. before the sourcing.py change that moved the
    volume sweep.

    This re-solves the entire k = 1..K frontier through the generator's own
    ``sweep_bom`` — the same function ``main()`` calls, with the same GraphState.
    Measured: ~1.0 s graph build + ~0.15 s for all 10 BOMs.
    """
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("ortools")
    if not DIVERSIFICATION_JSON.is_file():
        pytest.skip("docs/diversification_frontier.json not present")
    if not DB_PATH.exists():
        pytest.skip("backend/supply_chain.db not present")

    from sqlalchemy import text

    from app.core.database import SessionLocal
    from app.graph import get_graph_state
    from app.graph.builder import build_graph_state
    from app.optimization.strategies import get_strategy
    from seeds.run_benchmark import BOM_CATALOG
    from seeds.run_diversification_sweep import STRATEGY_ID, sweep_bom

    artifact = json.loads(DIVERSIFICATION_JSON.read_text())
    expected_by_bom = {r["bom"]: r for r in artifact["boms"]}

    regenerate = (
        "Re-run `cd backend && ./venv/bin/python -m seeds.run_diversification_sweep` "
        "(~2 s) and commit docs/diversification_frontier.json + "
        "docs/DIVERSIFICATION_FRONTIER.md."
    )

    db = SessionLocal()
    problems: List[str] = []
    boms_checked = 0
    try:
        for name, counted in EXPECTED_ROW_COUNTS.items():
            got = db.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
            assert got == counted, (
                f"the database this test read has {got} rows in {name}, not {counted}. "
                "Run pytest from backend/ (DATABASE_URL is CWD-relative)."
            )

        graph_state = get_graph_state() or build_graph_state(db)
        weights = get_strategy(STRATEGY_ID)

        for bom_name, items in BOM_CATALOG.items():
            expected = expected_by_bom.get(bom_name)
            if expected is None:
                problems.append(
                    f"{bom_name}: in BOM_CATALOG but absent from the committed frontier"
                )
                continue
            actual = sweep_bom(db, graph_state, bom_name, items, weights)
            boms_checked += 1
            _compare(f"boms.{bom_name}", expected, actual, problems)
    finally:
        db.close()

    assert boms_checked >= 10, (
        f"only {boms_checked} BOMs were re-solved — the pin has gone quiet."
    )
    assert not problems, (
        f"docs/diversification_frontier.json no longer matches what the optimizer "
        f"produces ({len(problems)} differing values across {boms_checked} BOMs).\n"
        f"{regenerate}\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


# ── 4. newsvendor.json vs the live evaluation ────────────────────────────────

NEWSVENDOR_JSON = DOCS / "newsvendor.json"

# `_run` stamps its own wall-clock onto each configuration; the evaluation itself
# does not produce it.
NEWSVENDOR_SKIP_FIELDS = frozenset({"wall_seconds"})


def test_newsvendor_primary_reproduces_from_the_live_evaluation():
    """
    ``tests/test_newsvendor_docs_match_artifact.py`` says so in its own docstring:
    *"It does not re-run the evaluation ... and does not import
    app.optimization.newsvendor. It reads two committed files and compares them."*
    So nothing tied the published newsvendor numbers to the code that computes
    them. This does.

    Re-runs the PRIMARY configuration only, at the artifact's own ``n_boot`` and
    ``seed``, through the same ``run_panel_evaluation`` the generator calls.
    Measured: 3.3 s. The four sensitivity arms are the same call with one argument
    changed and would cost ~14 s more for no additional coverage of the code path.
    """
    if not NEWSVENDOR_JSON.is_file():
        pytest.skip("docs/newsvendor.json not present")

    from app.optimization.newsvendor import run_panel_evaluation
    from seeds.run_newsvendor import N_BOOT, SEED

    artifact = json.loads(NEWSVENDOR_JSON.read_text())
    meta = artifact.get("meta", {})
    assert meta.get("n_boot") == N_BOOT and meta.get("bootstrap_seed") == SEED, (
        f"the artifact was generated with n_boot={meta.get('n_boot')} "
        f"seed={meta.get('bootstrap_seed')} but seeds/run_newsvendor now uses "
        f"n_boot={N_BOOT} seed={SEED}; the two are not comparable. "
        "Re-run `cd backend && ./venv/bin/python -m seeds.run_newsvendor`."
    )

    started = time.perf_counter()
    actual = run_panel_evaluation(n_boot=N_BOOT, seed=SEED)
    elapsed = time.perf_counter() - started

    expected = artifact["primary"]
    problems: List[str] = []
    _compare(
        "primary", expected, actual, problems,
        abs_tol=STAT_ABS_TOL, rel_tol=STAT_REL_TOL,
        skip_fields=NEWSVENDOR_SKIP_FIELDS,
    )

    assert elapsed < 90.0, (
        f"the newsvendor re-run took {elapsed:.1f}s against a 3.3s measurement — "
        "the evaluation has changed shape and this pin needs re-budgeting."
    )
    assert not problems, (
        f"docs/newsvendor.json's `primary` block no longer matches what "
        f"app.optimization.newsvendor produces ({len(problems)} differing values).\n"
        "The ARTIFACT is stale: re-run `cd backend && ./venv/bin/python -m "
        "seeds.run_newsvendor` and commit docs/newsvendor.json + the "
        "RESEARCH_TECHNIQUES.md section it feeds.\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


# ═════════════════════════════════════════════════════════════════════════════
# THE HEAVY ARTIFACTS — pinned behind `-m slow`
# ═════════════════════════════════════════════════════════════════════════════
#
# WHY THESE ARE SEPARATE
# ----------------------
# The four pins above cost ~5 s in total and run in the default suite. The five
# artifacts below were left unpinned on 2026-08-29 with the sizing "cvar_frontier
# ~1,316 s, leakage_progression ~215 s, chronos torch+weights+network,
# forecast_backtest Prophet per rolling origin, backend_verification 42 live HTTPS
# calls". Every one of those numbers is the cost of REGENERATING THE WHOLE
# ARTIFACT. A pin does not need to do that: re-solving the smallest canonical
# slice through the generator's own function goes red on exactly the same code
# drift. Measured cost of the whole block below: well under two minutes.
#
# WHAT `slow` MEANS IN THIS BLOCK, AND WHAT IT DOES NOT
# ------------------------------------------------------
# CI runs `-m "not slow"`, so `slow` here means LOCAL-ONLY. It does NOT mean
# expensive — sections 7 and 8 below are sub-second and are still marked. There
# are exactly two reasons a pin in this block is confined to the local machine,
# and both are properties of the environment, never a way to dodge a red test:
#
#   1. MACHINE-LOCAL STATE CI DOES NOT HAVE — a seeded SQLite file
#      (`cvar_frontier`, `leakage_progression`), or torch + a Hugging Face weight
#      cache from `requirements-ml.txt`, which the CI workflow never installs
#      (the Chronos arm).
#   2. THE COMPUTATION IS NOT REPRODUCIBLE ACROSS PLATFORMS — the Prophet arms of
#      sections 7 and 8. Stan's L-BFGS gives platform-dependent results, so a pin
#      that demands exactness can only be honest on the machine that WROTE the
#      artifact. See the block above section 7 for the measurement.
#
# In both cases the LOCAL standing gate (`pytest tests/ -q`) has no `-m` filter,
# so these DO run on the machine where artifacts are generated — which is the
# only machine where an artifact can go stale.
#
# What is NOT confined here: the deterministic seasonal-naive arm of each demand
# artifact. Those are unmarked and are CI's only artifact-vs-code coverage.
#
# WHAT THEY CAUGHT ON THE DAY THEY WERE WRITTEN
# ---------------------------------------------
# `docs/cvar_frontier.json` was stale. Commit `6a33ad0` changed
# `app/optimization/sourcing.py` (milli-cent quantisation, risk surcharges, the
# `is_chinese_origin` double-count). `docs/volume_sweep.json` was regenerated for
# it on 2026-08-29; `docs/cvar_frontier.json` was not, and it carries the SAME
# deterministic MILP as a baseline. Its `primary.x10000.baselines` published
# $181,919.39 / 5 suppliers where the optimizer now returns $181,908.01 / 6 —
# the identical cell that moved in the volume sweep — and
# `docs/CVAR_EFFICIENT_FRONTIER.md:534-535` published the derived
# $183,171 / $219,128 / 5 suppliers to a reader. Nothing in the suite could see
# it: the artifact's own tests are doc-vs-artifact.
#
# NOT PINNED, AND WHY — `docs/backend_verification.json`
# ------------------------------------------------------
# HONESTLY UNPINNABLE. There is no generator for it anywhere in this repo: it is
# a hand-run snapshot from the 2026-08-19 production repair (see
# the 2026-08-19 production repair and verification pass),
# so there is no function to call — pinning it would mean writing the very
# reimplementation the loop's learnings log forbids. Its content is 42 live HTTPS responses
# from Render, and it stores a `seconds` field per check that cannot reproduce by
# construction. A test that re-issued those calls would assert that a free-tier
# service is awake, not that this repo's code is unchanged: it would go red on a
# cold start and green on a broken build. `docs/README.md` lists it beside the
# generated data files, but it is the only one of them with neither a generator
# nor a companion `.md` — it is a log, not a result. The correct guard for it is
# the live-endpoint audit, not a code pin.
#
# NOT PINNED, AND WHY — the breadth / sensitivity / SAA arms of `cvar_frontier`
# -----------------------------------------------------------------------------
# Those arms solve under a 15-unit deterministic CP-SAT budget and the committed
# artifact records `by_arm.breadth.n_time_limit_hits: 44` (46 run-wide), i.e. 44
# solves that returned FEASIBLE with an open bound. Since 2026-09-01 a truncated
# solve is REPRODUCIBLE — the budget is work, not clock — so the historical
# "goes red for reasons that are not drift" objection is weaker than it was. What
# still argues against pinning them is cost: those arms are the bulk of a
# ~27-minute sweep, and a truncated re-solve on a machine whose CP-SAT build
# differs would still move. The PRIMARY arm runs at an 80-unit budget and closed
# every one of its 27 solves to OPTIMAL at a 0.000% gap, which is why it is the
# arm that is pinned — and `_assert_all_optimal` below still refuses to read a
# truncated re-solve as staleness.

_MISSING = object()


def _resolve_repo_python_path() -> None:
    """`seeds.*` imports assume `backend/` is on sys.path, exactly as `-m` does."""
    import sys

    backend = str(REPO_ROOT / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)


def _assert_row_counts(db) -> None:
    """The CWD-relative-SQLite guard, shared by every DB-backed pin above."""
    from sqlalchemy import text

    for name, counted in EXPECTED_ROW_COUNTS.items():
        got = db.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
        assert got == counted, (
            f"the database this test read has {got} rows in {name}, not {counted}. "
            "Either the seed drifted or pytest was launched from the wrong directory "
            "(DATABASE_URL is CWD-relative and SQLite creates an empty file rather "
            "than failing). Run pytest from backend/."
        )


# ── 5. cvar_frontier.json — the PRIMARY arm vs the live solvers ──────────────

CVAR_JSON = DOCS / "cvar_frontier.json"

# `_run_primary` stamps wall-clock onto every point it emits. Those are the only
# fields a reproduction is not expected to match.
CVAR_SKIP_FIELDS = frozenset({"sweep_wall_seconds", "solve_seconds", "evaluate_seconds"})

CVAR_REGENERATE = (
    "Re-run `cd backend && ./venv/bin/python -m seeds.run_cvar_frontier` (~22 min, "
    "it is the full non-quick sweep the artifact asserts) and commit the regenerated "
    "docs/cvar_frontier.json + docs/CVAR_EFFICIENT_FRONTIER.md."
)


def _assert_all_optimal(primary: Dict[str, Any]) -> None:
    """A truncated solve is an ENVIRONMENT problem, never artifact staleness.

    CP-SAT returning FEASIBLE means the per-solve budget was exhausted with the
    bound still open, so the numbers below it are whatever the search had
    reached — they say nothing about whether the committed artifact is current.
    Reporting that as "the artifact is stale" would send the next reader to
    regenerate a 22-minute sweep for no reason, so it gets its own message.
    """
    truncated = [
        f"x{block['multiplier']} lambda={pt['lambda']} status={pt['solver_status']} "
        f"gap={pt['mip_gap_pct']}%"
        for block in primary.values()
        for pt in block["frontier"]
        if pt["solver_status"] != "OPTIMAL" or pt["hit_time_limit"]
    ]
    assert not truncated, (
        "the re-solve did not close the bound on "
        f"{len(truncated)} of the primary frontier points — CP-SAT returned FEASIBLE "
        "inside its 80-unit deterministic budget rather than OPTIMAL. This machine "
        "cannot reproduce the "
        "primary arm deterministically, which is a SOLVER-ENVIRONMENT problem and NOT "
        "evidence that docs/cvar_frontier.json is stale. Do not regenerate the artifact "
        "on the strength of this failure.\n"
        + "\n".join(f"  - {t}" for t in truncated[:20])
    )


@pytest.mark.slow
def test_cvar_frontier_primary_reproduces_from_the_live_solvers():
    """
    The PRIMARY arm of ``docs/cvar_frontier.json`` — all three volumes, all nine
    lambdas, both baselines and the VSS — re-solved through
    ``seeds.run_cvar_frontier._run_primary``, the generator's own function.

    This arm is where the artifact touches the deterministic optimizer: its
    ``shipped_milp_graph_aware=*`` baselines call ``app.optimization.sourcing.
    solve_sourcing`` directly, so any change under ``app/optimization/`` that moves
    a published cost shows up here. It is also the arm that reaches the reader:
    ``docs/CVAR_EFFICIENT_FRONTIER.md`` renders the frontier table, the knee, the
    baselines table and the headline pitch out of exactly this block.

    Measured: ~5 s of setup (graph build + offer load + ML state) and ~40 s of
    solving. That is 3% of the 1,316 s full regeneration and it covers every line
    of code the full run would exercise on this arm.

    The ML state is loaded exactly as ``main()`` loads it — the shipped-MILP
    baseline is scored against the optimizer AS PRODUCTION RUNS IT, with the macro
    stress premium live. Without that the baseline would be a different plan and
    this pin would compare two different things and call it agreement.
    """
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("ortools")
    if not CVAR_JSON.is_file():
        pytest.skip("docs/cvar_frontier.json not present")
    if not DB_PATH.exists():
        pytest.skip("backend/supply_chain.db not present")

    _resolve_repo_python_path()

    import app.ml as ml_pkg
    import app.optimization.stochastic as st
    import seeds.run_cvar_frontier as gen
    from app.core.database import SessionLocal
    from app.graph.builder import build_graph_state
    from app.optimization.strategies import get_strategy
    from seeds.run_benchmark import BOM_CATALOG, _load_offers_for_bom

    artifact = json.loads(CVAR_JSON.read_text())
    assert artifact["provenance"]["quick_mode"] is False, (
        "the committed cvar_frontier.json declares quick_mode=True, i.e. it was not "
        f"generated by the full sweep it is published as. {CVAR_REGENERATE}"
    )
    assert artifact["meta"]["strategy"] == gen.STRATEGY_ID, (
        f"the artifact was generated with strategy {artifact['meta']['strategy']!r} "
        f"but the generator now uses {gen.STRATEGY_ID!r}. {CVAR_REGENERATE}"
    )
    assert artifact["meta"]["primary_bom"] == gen.PRIMARY_BOM, (
        f"the artifact's primary BOM is {artifact['meta']['primary_bom']!r}; the "
        f"generator now uses {gen.PRIMARY_BOM!r}. {CVAR_REGENERATE}"
    )

    previous_ml_state = ml_pkg.get_ml_state()
    db = SessionLocal()
    try:
        _assert_row_counts(db)

        # Same three lines main() runs, in the same order, for the same reason.
        from app.ml.serving import load_ml_state
        loaded_state = load_ml_state()
        if loaded_state is not None:
            ml_pkg.set_ml_state(loaded_state)

        graph_state = build_graph_state(db)
        bom0, offers, _meta = _load_offers_for_bom(db, BOM_CATALOG[gen.PRIMARY_BOM])
        assert bom0 and offers, (
            f"{gen.PRIMARY_BOM}: the database returned no BOM lines or no offers, so "
            "this test would have checked nothing."
        )

        weights = get_strategy(gen.STRATEGY_ID)
        distributor_ids = sorted({o.distributor_id for o in offers})
        failure_probs = st.build_failure_probabilities(
            distributor_ids, graph_state.betweenness)
        scenario_set = st.sample_scenarios(failure_probs, gen.N_DRAWS, gen.SEED)
        exact_set = (
            st.enumerate_scenarios(failure_probs)
            if len(distributor_ids) <= st.MAX_ENUMERABLE_DISTRIBUTORS else None
        )

        # The calibration block is free to check and pins the probability model the
        # whole artifact rests on.
        calibration_problems: List[str] = []
        _compare(
            "calibration.primary_bom_distributors",
            artifact["calibration"]["primary_bom_distributors"],
            [
                {
                    "distributor_id": d,
                    "betweenness_normalized": round(
                        graph_state.betweenness.get(d, 0.0), 6),
                    "p_disruption_over_horizon": round(failure_probs[d], 5),
                }
                for d in distributor_ids
            ],
            calibration_problems,
            abs_tol=STAT_ABS_TOL,
            rel_tol=STAT_REL_TOL,
        )
        assert not calibration_problems, (
            "the disruption calibration in docs/cvar_frontier.json no longer matches "
            f"app.optimization.stochastic.build_failure_probabilities.\n"
            f"{CVAR_REGENERATE}\n\n"
            + "\n".join(f"  - {p}" for p in calibration_problems[:20])
        )

        gen._SOLVE_LOG.clear()
        started = time.perf_counter()
        actual = gen._run_primary(bom0, offers, weights, scenario_set, exact_set)
        elapsed = time.perf_counter() - started
    finally:
        db.close()
        if previous_ml_state is not None:
            ml_pkg.set_ml_state(previous_ml_state)
        else:
            # Nothing was loaded before this test; leave the process as it was found.
            ml_pkg._ml_state = None  # noqa: SLF001

    _assert_all_optimal(actual)

    # Anti-vacuity: the pin must actually have solved the grid it claims to cover.
    points_resolved = sum(len(block["frontier"]) for block in actual.values())
    assert len(actual) == len(gen.PRIMARY_MULTIPLIERS) >= 3, (
        f"only {len(actual)} volume blocks were re-solved against "
        f"{len(gen.PRIMARY_MULTIPLIERS)} configured multipliers — the pin has gone quiet."
    )
    assert points_resolved >= len(gen.PRIMARY_MULTIPLIERS) * len(gen.LAMBDA_GRID), (
        f"only {points_resolved} frontier points were re-solved; the primary arm is "
        f"{len(gen.PRIMARY_MULTIPLIERS)} volumes x {len(gen.LAMBDA_GRID)} lambdas."
    )
    assert elapsed < 300.0, (
        f"the primary re-solve took {elapsed:.1f}s against a ~40 s measurement — the "
        "solver configuration has changed materially and this pin needs re-budgeting."
    )

    problems: List[str] = []
    for volume_key, expected_block in artifact["primary"].items():
        got = actual.get(volume_key, _MISSING)
        if got is _MISSING:
            problems.append(
                f"primary.{volume_key}: the artifact carries this volume but the "
                f"generator's PRIMARY_MULTIPLIERS no longer produce it")
            continue
        _compare(
            f"primary.{volume_key}", expected_block, got, problems,
            skip_fields=CVAR_SKIP_FIELDS,
        )

    assert not problems, (
        "docs/cvar_frontier.json's PRIMARY arm no longer matches what the solvers "
        f"produce ({len(problems)} differing values across {points_resolved} re-solved "
        "frontier points).\n"
        "The ARTIFACT is stale, not this test: code under app/optimization/ has moved "
        "and the artifact was not regenerated. docs/CVAR_EFFICIENT_FRONTIER.md renders "
        f"these numbers, so a reader is being shown them.\n{CVAR_REGENERATE}\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


# ── 6. leakage_progression.json — panel, folds and one fold per regime ───────

LEAKAGE_JSON = DOCS / "leakage_progression.json"

LEAKAGE_REGENERATE = (
    "Re-run `cd backend && ./venv/bin/python -m seeds.run_leakage_progression` "
    "(~215 s) and commit docs/leakage_progression.json + docs/LEAKAGE_PROGRESSION.md "
    "(README.md and RESEARCH_TECHNIQUES.md quote this artifact)."
)


@pytest.mark.slow
def test_leakage_progression_reproduces_from_the_live_lead_time_model():
    """
    ``docs/leakage_progression.json`` publishes the headline every reviewer looks
    at first — R2 +0.825 random -> +0.073 by family -> -0.697 by manufacturer —
    and README.md, PROJECT_OVERVIEW.md and RESEARCH_TECHNIQUES.md all quote it.
    Until now nothing tied it to `app/ml/lead_time_model.py`.

    THE SMALLEST RE-SOLVE THAT STILL GOES RED. The full run is 3 regimes x 50
    folds x 8 predictors = 1,200 fits (~215 s). Fold 0 of each regime is 24 fits
    (~4.5 s) and exercises exactly the same code: the panel loader, the training
    design, the design matrix, `_group_key`, the fold splitters, `MODELS` and
    `baseline_predictors`. Anything that moves a published number moves fold 0 too.
    The three cheap whole-artifact blocks — the row accounting, the feature
    columns and the in-sample identity R2 table — are checked in full because
    they cost ~0.5 s.

    NOTE the fold indices must align: `score_regime` skips a fold with no label
    variance without appending to `r2_per_fold`, so this asserts `folds[0]["fold"]
    == 0` before trusting the positional comparison.
    """
    pytest.importorskip("sklearn")
    pytest.importorskip("pandas")
    if not LEAKAGE_JSON.is_file():
        pytest.skip("docs/leakage_progression.json not present")

    _resolve_repo_python_path()

    import seeds.run_leakage_progression as gen

    artifact = json.loads(LEAKAGE_JSON.read_text())
    meta = artifact["meta"]
    assert meta["quick_mode"] is False, (
        f"the committed artifact was generated with --quick. {LEAKAGE_REGENERATE}")
    assert (meta["seed"], meta["n_splits"], meta["n_repeats"]) == (
        gen.SEED, gen.N_SPLITS, gen.N_REPEATS), (
        f"the artifact was generated at seed={meta['seed']} n_splits={meta['n_splits']} "
        f"n_repeats={meta['n_repeats']} but the generator now uses seed={gen.SEED} "
        f"n_splits={gen.N_SPLITS} n_repeats={gen.N_REPEATS}; the two are not "
        f"comparable. {LEAKAGE_REGENERATE}"
    )
    assert meta["feature_schema_version"] == gen.FEATURE_SCHEMA_VERSION, (
        f"the artifact carries feature schema v{meta['feature_schema_version']}; "
        f"lead_time_model is now at v{gen.FEATURE_SCHEMA_VERSION}. {LEAKAGE_REGENERATE}"
    )

    panel = gen.load_observed_panel()
    assert panel is not None, (
        "no observed lead-time panel — this test would have checked nothing. Expected "
        f"at {meta['panel_path']}."
    )
    design = gen.build_training_design(panel)
    X, feature_cols = gen.build_design_matrix(design.records, schema=design.schema)
    y = design.y

    # Anti-vacuity, same shape as the DB row-count guard: assert the INPUT before
    # trusting anything computed from it. An empty or truncated panel would
    # otherwise produce a small, silently meaningless set of folds.
    from app.ml.lead_time_collector import PANEL_PATH
    panel_sha = _sha256_of(PANEL_PATH)
    assert panel_sha == meta["panel_sha256"], (
        f"{PANEL_PATH.relative_to(REPO_ROOT)} hashes to {panel_sha}, but the artifact "
        f"was built from {meta['panel_sha256']}. The INPUT DATA changed, so a mismatch "
        f"below would not mean the code moved. {LEAKAGE_REGENERATE}"
    )
    assert len(y) == artifact["counts"]["n_rows"] >= 1000, (
        f"the design matrix has {len(y)} rows against the artifact's "
        f"{artifact['counts']['n_rows']} — the panel this test read is not the panel "
        "the artifact was built from."
    )

    problems: List[str] = []
    _compare(
        "counts",
        artifact["counts"],
        {
            "n_rows": int(len(y)),
            "n_family_group_keys": int(len(set(design.family_groups))),
            "n_manufacturers": int(len(set(design.manufacturer_groups))),
            "n_features": int(X.shape[1]),
            "n_snapshot_dates": int(len(set(design.snapshot_dates))),
        },
        problems, abs_tol=0.0,
    )
    _compare("panel_row_accounting", artifact["panel_row_accounting"], design.counts,
             problems, abs_tol=0.0)
    _compare("meta.feature_cols", meta["feature_cols"], list(feature_cols), problems)
    _compare("meta.feature_exclusions", meta["feature_exclusions"], design.exclusions,
             problems)
    _compare("model_names", artifact["model_names"], list(gen.MODELS), problems)
    _compare("baseline_names", artifact["baseline_names"],
             list(gen.baseline_predictors(feature_cols)), problems)
    _compare(
        "identity_column_in_sample_r2",
        artifact["identity_column_in_sample_r2"],
        gen.identity_column_in_sample_r2(
            y, design.identity_columns, {"family_group_key": design.family_groups}),
        problems, abs_tol=STAT_ABS_TOL, rel_tol=STAT_REL_TOL,
    )

    regime_groups = {
        "random": None,
        "family": design.family_groups,
        "manufacturer": design.manufacturer_groups,
    }
    folds_rescored = 0
    fits = 0
    started = time.perf_counter()
    for regime, groups in regime_groups.items():
        expected_regime = artifact["regimes"][regime]
        folds = gen.build_folds(len(y), groups, gen.N_SPLITS, gen.N_REPEATS, gen.SEED)
        assert len(folds) == meta["n_folds_per_regime"], (
            f"{regime}: build_folds produced {len(folds)} folds, but the artifact "
            f"records {meta['n_folds_per_regime']} per regime. {LEAKAGE_REGENERATE}"
        )
        assert expected_regime["folds"][0]["fold"] == 0, (
            f"{regime}: the artifact's first recorded fold is index "
            f"{expected_regime['folds'][0]['fold']}, not 0 — a fold was skipped for "
            "want of label variance, so this positional comparison is not valid."
        )
        got = gen.score_regime(X, y, feature_cols, folds[:1], list(gen.MODELS))
        folds_rescored += 1
        fits += len(got["r2_per_fold"])
        _compare(f"regimes.{regime}.folds[0]", expected_regime["folds"][0],
                 got["folds"][0], problems, abs_tol=STAT_ABS_TOL, rel_tol=STAT_REL_TOL)
        for name, values in got["r2_per_fold"].items():
            _compare(
                f"regimes.{regime}.r2_per_fold[{name}][0]",
                expected_regime["r2_per_fold"][name][0],
                values[0], problems, abs_tol=STAT_ABS_TOL, rel_tol=STAT_REL_TOL,
            )
    elapsed = time.perf_counter() - started

    assert folds_rescored == 3 and fits >= 24, (
        f"only {folds_rescored} folds / {fits} predictor scores were re-fitted — the "
        "pin has gone quiet, which is exactly how a published number drifts unnoticed."
    )
    assert elapsed < 180.0, (
        f"re-fitting one fold per regime took {elapsed:.1f}s against a ~4.5 s "
        "measurement; the estimator configuration has changed materially."
    )

    assert not problems, (
        f"docs/leakage_progression.json no longer matches what app/ml/lead_time_model.py "
        f"produces ({len(problems)} differing values).\nThe ARTIFACT is stale, not this "
        f"test.\n{LEAKAGE_REGENERATE}\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


def _sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 7. forecast_backtest.json — SPLIT BY DETERMINISM, not by cost ────────────
#
# WHY THIS ARTIFACT IS PINNED BY TWO TESTS AND NOT ONE
# ----------------------------------------------------
# On 2026-08-30 a single pin covering all three arms was promoted out of `slow`
# into CI's default suite. It passed locally and went RED on CI (run
# 33318131193): 135 differing values here and 61 in `chronos_benchmark.json`.
# EVERY one of the 160 was a `prophet.*` key — not a single `seasonal_naive.*`
# key moved — and the magnitudes were ~0.2-0.3% relative
# (`prophet.overall.wape` 0.0313 vs 0.0312, `prophet.overall.rmse` 1413.3469 vs
# 1410.4055). The artifacts were current. The pin's SCOPE was wrong.
#
# Prophet fits via Stan's L-BFGS, which is not bit-reproducible across platform,
# interpreter or BLAS build: this repo's artifacts are generated on macOS/arm64 +
# Python 3.13 and CI runs Linux/x86_64 + Python 3.11. That is a genuine platform
# limitation, not laziness — there is no flag, seed or option that makes a Stan
# fit reproduce across architectures.
#
# The tempting fix — widen the tolerance until Prophet passes on both machines —
# is forbidden here. A tolerance loose enough to absorb a non-deterministic fit
# is a check that cannot reliably fail, which the loop's learnings log (2026-08-28) names
# as worse than no check at all. So the arms are split by whether they are
# reproducible, and BOTH halves keep the SAME strict tolerance:
#
#   * seasonal-naive — DETERMINISTIC. Unmarked, so it runs in CI's default
#     suite. BE PRECISE ABOUT THE GAP THIS CLOSES: sections 1-4 of this file are
#     also unmarked and already ran on CI (`backend/supply_chain.db` is committed
#     — see the `!backend/supply_chain.db` un-ignore in `.gitignore` — so the
#     optimizer pins are NOT skipped there; CI run 33318131193 reported 1,114
#     passed and just ONE skip across the whole suite). What CI had zero of was a
#     pin on either DEMAND-SERIES artifact: both were behind `slow` entirely.
#     That is the gap, and it is the one this closes.
#   * the Prophet arms — NOT reproducible off the generating machine. `slow`,
#     i.e. local-only, which is where the artifact is written and therefore the
#     only place it can go stale.
#
# EVIDENCE FOR THE CLASSIFICATION (2026-08-30 — measured, not assumed)
# --------------------------------------------------------------------
# `seasonal_naive_fit_predict` performs NO arithmetic at all: it indexes the
# training list and returns copies of observed values. The values it copies come
# from a SHA-256-pinned vintage that `_assert_series_matches_artifact` asserts
# byte-for-byte before any comparison runs. The only floating-point work on the
# path is the shared metric code in `app/ml/forecast_metrics.py`, and every one
# of its outputs passes through `round(x, 4)` in `HorizonMetrics.as_dict()`
# before it is written — so a last-bit summation difference cannot reach a
# compared leaf.
#
# That reasoning was then CONFIRMED rather than trusted: both artifacts'
# `seasonal_naive` blocks were re-scored inside a `linux/amd64` container on
# CI's exact stack (Python 3.11.16, numpy 2.4.4, pandas 2.3.3) and produced ZERO
# differing leaves under literal `!=` equality — stricter than the 1e-9
# tolerance used below.
#
# NOT CLASSIFIED, AND THEREFORE TREATED AS NON-DETERMINISTIC
# ----------------------------------------------------------
# The Chronos arm. It is behind `slow` for an independent reason (torch +
# chronos-forecasting come from `requirements-ml.txt`, which CI never installs),
# so its reproducibility has never been exercised on a second platform. An arm
# whose determinism cannot be shown is left in `slow` — the safe side.

FORECAST_JSON = DOCS / "forecast_backtest.json"

FORECAST_REGENERATE = (
    "Re-run `cd backend && ./venv/bin/python -m seeds.run_forecast_backtest --offline` "
    "and commit docs/forecast_backtest.json + docs/FORECAST_BACKTEST.md."
)


def _assert_series_matches_artifact(load, meta: Dict[str, Any], regenerate: str) -> None:
    """The input-integrity guard for the two demand-series artifacts.

    Same failure mode as the CWD-relative SQLite trap: if the series this test read
    is not the series the artifact was built from, every comparison below it is
    meaningless. The vintage is PINNED and committed precisely so this can be
    asserted rather than assumed.
    """
    got = load.meta()
    for field in ("series_id", "vintage", "n_obs", "start", "end",
                  "series_values_sha256", "vintage_file_sha256"):
        assert got.get(field) == meta.get(field), (
            f"the loaded series has {field}={got.get(field)!r} but the artifact was "
            f"built from {field}={meta.get(field)!r}. The INPUT DATA differs, so a "
            f"mismatch below would not mean the code moved. {regenerate}"
        )
    assert meta["n_obs"] >= 100, (
        f"the artifact records only {meta['n_obs']} observations — too short for a "
        "3-window x 12-month rolling-origin backtest to mean anything."
    )


def _rescore_arms(gen, artifact: Dict[str, Any], arms: Dict[str, Any],
                  problems: List[str], regenerate: str) -> int:
    """Re-run the named arms through the generator's own harness; diff every leaf.

    Shared by the deterministic pin and the Prophet pin of BOTH demand artifacts,
    so the four cannot drift apart: same loader, same input-integrity assert, same
    ``walk_forward_backtest``, same tolerance. The only thing a caller varies is
    WHICH arms it scores — and, consequently, what a mismatch is allowed to mean.
    """
    from app.ml.backtest import walk_forward_backtest

    load = gen._load_series(None, offline=True)
    _assert_series_matches_artifact(load, artifact["meta"], regenerate)
    values = [float(v) for v in load.series.to_numpy()]

    windows_scored = 0
    for arm, fit_predict in arms.items():
        report = walk_forward_backtest(
            values, fit_predict, horizon=gen.HORIZON, n_windows=gen.N_WINDOWS
        ).as_dict()
        windows_scored += len(report["per_window"])
        _compare(arm, artifact[arm], report, problems,
                 abs_tol=STAT_ABS_TOL, rel_tol=STAT_REL_TOL)
    return windows_scored


def _prophet_mismatch_message(artifact_name: str, n_problems: int,
                              deterministic_pin: str, regenerate: str) -> str:
    """The failure text for a PROPHET-arm mismatch, which is genuinely ambiguous.

    The message this replaced asserted "The ARTIFACT is stale, not this test." On
    2026-08-30 CI printed exactly that sentence about an artifact that was
    perfectly current, which is its own defect: a check that misdiagnoses sends
    the next reader to regenerate the wrong thing. Platform non-reproducibility
    is named FIRST because it is the likelier cause and the cheapest to rule out.
    """
    return (
        f"docs/{artifact_name}'s PROPHET arms no longer match what "
        f"app/ml/backtest.py and Prophet produce ({n_problems} differing values).\n"
        "\n"
        "THIS IS NOT NECESSARILY A STALE ARTIFACT. Prophet fits via Stan (L-BFGS), "
        "which is NOT bit-reproducible across machines. Rule PLATFORM "
        "NON-REPRODUCIBILITY out FIRST, before regenerating anything:\n"
        "  1. IS THIS THE MACHINE THAT WROTE THE ARTIFACT? Compare the OS and CPU "
        "architecture, the Python interpreter version, the BLAS/LAPACK build numpy "
        "is linked against, and the prophet / cmdstanpy / numpy / pandas versions "
        "against the artifact's own provenance. A wobble of ~0.1-0.5% relative "
        "across such a change is EXPECTED and is not drift.\n"
        f"  2. IS THE DETERMINISTIC PIN GREEN? `{deterministic_pin}` re-scores this "
        "same artifact through this same harness on an arm that IS bit-reproducible "
        "everywhere. If it passes and only the Prophet arms differ, the cause is the "
        "platform — do NOT regenerate.\n"
        "  3. Only if that deterministic pin is ALSO red, or something under "
        "app/ml/backtest.py or the generator genuinely changed, is the artifact "
        f"stale. Then, on the generating machine: {regenerate}\n\n"
    )


def _load_forecast_artifact():
    """The generator-vs-artifact shape asserts that both forecast pins share."""
    _resolve_repo_python_path()

    import seeds.run_forecast_backtest as gen

    artifact = json.loads(FORECAST_JSON.read_text())
    meta = artifact["meta"]
    assert meta["reproducible"] is True, (
        "the committed forecast_backtest.json was generated with --latest (an "
        f"unpinned vintage) and cannot be reproduced. {FORECAST_REGENERATE}")
    assert (meta["horizon"], meta["n_windows"]) == (gen.HORIZON, gen.N_WINDOWS), (
        f"the artifact was generated at horizon={meta['horizon']} "
        f"n_windows={meta['n_windows']}; the generator now uses horizon={gen.HORIZON} "
        f"n_windows={gen.N_WINDOWS}. {FORECAST_REGENERATE}"
    )
    return gen, artifact


def test_forecast_backtest_deterministic_arm_reproduces_from_the_live_harness():
    """
    The seasonal-naive arm of ``docs/forecast_backtest.json``, re-run through
    ``app.ml.backtest.walk_forward_backtest`` with the generator's own
    ``seasonal_naive_fit_predict``. Nothing is reimplemented here.

    THIS IS THE HALF CI RUNS. It is deliberately unmarked: it needs no database,
    no network, no ``requirements-ml.txt`` dependency and not even Prophet — only
    pandas, to read the committed vintage.

    It is NOT CI's first artifact-vs-code pin — sections 1-4 are unmarked too and
    already run there. It is the first one covering ``forecast_backtest.json``,
    which was behind `slow` in full, so the failure this file exists for — code
    moving while the artifact and its document stayed agreed with each other —
    was invisible on CI *for the demand-series artifacts specifically*.

    It is a REAL check on the whole path, not a token one. The arm shares the
    loader, the rolling-origin split, the horizon bucketing, the metric code and
    the rounding with the Prophet arms, so any change under ``app/ml/backtest.py``
    or ``app/ml/forecast_metrics.py``, any change to the vintage pin, and any
    change to ``seeds.run_forecast_backtest``'s split parameters lands here.
    VERIFIED RED on 2026-08-30 by moving ``SEASONAL_PERIOD`` 12 -> 11 in
    ``seeds/run_forecast_backtest.py``: this pin failed with 69 differing values
    and the chronos one with 61, while both Prophet pins stayed green (Prophet
    does not read that constant). The generator was then restored and confirmed
    byte-identical by sha256. A pin nobody has watched go red is not a check.

    Measured: **0.01 s** of call time (0.09 s including collection/import). The two
    deterministic pins together add 0.12 s wall to CI's suite.

    The series is loaded OFFLINE from the committed ALFRED vintage pin, so this
    test cannot reach the network and cannot be made to pass or fail by a Census
    revision — the exact defect the vintage pin was introduced to kill.
    """
    # No `importorskip` and no `skip` on this one, deliberately. Both are ways for
    # CI's only pin on THIS artifact to disappear without turning anything red,
    # which is the exact gap this test exists to close. `docs/forecast_backtest.json`
    # is committed, and pandas is pinned in `requirements.txt` — if either is
    # missing that is a defect and this must say so, not shrug.
    assert FORECAST_JSON.is_file(), (
        f"docs/forecast_backtest.json is missing, though it is committed. {FORECAST_REGENERATE}")

    gen, artifact = _load_forecast_artifact()

    problems: List[str] = []
    started = time.perf_counter()
    windows_scored = _rescore_arms(
        gen, artifact, {"seasonal_naive": gen.seasonal_naive_fit_predict},
        problems, FORECAST_REGENERATE,
    )
    elapsed = time.perf_counter() - started

    assert windows_scored == gen.N_WINDOWS >= 3, (
        f"only {windows_scored} rolling origins were scored — the pin has gone quiet.")
    # 10 s against a 0.01 s measurement: loose enough that a slow CI runner never
    # flakes, tight enough that "this arm quietly started fitting something" shows up.
    assert elapsed < 10.0, (
        f"the seasonal-naive re-score took {elapsed:.2f}s against a 0.01 s measurement; "
        "the harness has changed shape and this pin needs re-budgeting.")

    assert not problems, (
        "docs/forecast_backtest.json's DETERMINISTIC seasonal-naive arm no longer "
        f"matches what app/ml/backtest.py produces ({len(problems)} differing values).\n"
        "This arm does no arithmetic of its own — it copies observations out of a "
        "SHA-256-pinned series — and it was verified to reproduce with ZERO differing "
        "leaves on CI's own linux/amd64 + Python 3.11 stack. So this is NOT platform "
        "noise: the ARTIFACT is stale, not this test.\n"
        f"{FORECAST_REGENERATE}\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


@pytest.mark.slow
def test_forecast_backtest_prophet_arms_reproduce_from_the_live_harness():
    """
    The two Prophet arms of ``docs/forecast_backtest.json`` — ``prophet``
    (yearly seasonality) and ``prophet_served_config`` (the trend-only ablation)
    — re-run through the generator's own ``make_prophet_fit_predict``.

    Measured: ~0.5 s for both arms. Prophet is fitted with
    ``uncertainty_samples=0`` on 198 monthly points, three times per arm, so
    there is no posterior sampling to pay for. The 2026-08-29 sizing that read
    "Prophet per rolling origin" as expensive was simply wrong.

    WHY THIS ONE STAYS `slow` WHEN IT IS FAST — A REAL PLATFORM LIMIT, NOT LAZINESS
    ------------------------------------------------------------------------------
    Cost is not the reason. Prophet fits via Stan's L-BFGS optimiser, whose
    result depends on the platform, the interpreter and the BLAS/LAPACK build.
    The committed artifact is generated on ONE machine (macOS/arm64, Python 3.13);
    CI is Linux/x86_64 on Python 3.11. When this arm was briefly promoted into
    CI's default suite on 2026-08-30 it failed there with 135 differing values,
    every one of them a ~0.2-0.3% relative wobble on a `prophet.*` key, against
    an artifact that was entirely current.

    There is no seed or flag that fixes that; it is a property of the fit. The
    only two honest options are (a) run the pin only where the artifact is
    written, or (b) widen the tolerance until a non-deterministic fit passes
    anywhere — and (b) produces a check that cannot reliably fail, which this
    repo forbids outright. So the tolerance here is the SAME strict
    ``STAT_ABS_TOL`` / ``STAT_REL_TOL`` the deterministic pin uses, and the test
    is confined to the generating machine, where it is exact and meaningful.

    ``slow`` therefore means LOCAL-ONLY here, not EXPENSIVE. The local standing
    gate (`pytest tests/ -q`) has no `-m` filter, so this does run before every
    push, on the only machine where this artifact can actually go stale.
    """
    pytest.importorskip("prophet")
    if not FORECAST_JSON.is_file():
        pytest.skip("docs/forecast_backtest.json not present")

    gen, artifact = _load_forecast_artifact()

    problems: List[str] = []
    started = time.perf_counter()
    windows_scored = _rescore_arms(
        gen, artifact,
        {
            "prophet": gen.make_prophet_fit_predict(yearly_seasonality=True),
            "prophet_served_config": gen.make_prophet_fit_predict(yearly_seasonality=False),
        },
        problems, FORECAST_REGENERATE,
    )
    elapsed = time.perf_counter() - started

    assert windows_scored == 2 * gen.N_WINDOWS >= 6, (
        f"only {windows_scored} rolling origins were scored across 2 arms — "
        "the pin has gone quiet.")
    assert elapsed < 120.0, (
        f"the backtest took {elapsed:.1f}s against a 0.5 s measurement; the harness "
        "has changed shape and this pin needs re-budgeting.")

    assert not problems, (
        _prophet_mismatch_message(
            "forecast_backtest.json", len(problems),
            "test_forecast_backtest_deterministic_arm_reproduces_from_the_live_harness",
            FORECAST_REGENERATE,
        )
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


# ── 8. chronos_benchmark.json — deterministic arm in CI, the rest local ──────
#
# Same split, same reason as section 7, and the two artifacts share the arm:
# `seeds.run_chronos_benchmark` IMPORTS `seasonal_naive_fit_predict` and
# `SEASONAL_PERIOD` from `seeds.run_forecast_backtest` rather than restating
# them, so one baseline change moves both pins and neither can drift alone.
#
#   * seasonal_naive — deterministic, unmarked, runs in CI. First pin on this
#                      artifact; not CI's first in this file — see section 7.
#   * prophet        — Stan/L-BFGS, `slow`, local-only. It failed CI with 61
#                      differing values on 2026-08-30 against a current artifact.
#   * chronos        — `slow` for an independent reason (torch +
#                      chronos-forecasting are in `requirements-ml.txt`, which CI
#                      does not install), so its determinism is unproven and it
#                      is treated as non-deterministic. See the test below it.

CHRONOS_JSON = DOCS / "chronos_benchmark.json"

CHRONOS_REGENERATE = (
    "Re-run `cd backend && ./venv/bin/python -m seeds.run_chronos_benchmark --offline` "
    "(needs requirements-ml.txt: torch + chronos-forecasting) and commit "
    "docs/chronos_benchmark.json + docs/CHRONOS_BENCHMARK.md."
)

# Everything in the chronos block that is a timing, a hardware fact or an
# environment fact rather than a forecast. `weights_cached` and `torch_version`
# describe the machine, not the model.
CHRONOS_ENV_FIELDS = frozenset({
    "import_seconds", "load_seconds", "warmup_seconds", "walk_forward_wall_seconds",
    "steady_state", "weights_cached", "torch_version",
})


def _load_chronos_artifact():
    """The generator-vs-artifact shape asserts that both classical chronos pins share."""
    _resolve_repo_python_path()

    import seeds.run_chronos_benchmark as gen

    artifact = json.loads(CHRONOS_JSON.read_text())
    assert artifact["meta"]["reproducible"] is True, (
        f"the committed chronos_benchmark.json is not vintage-pinned. {CHRONOS_REGENERATE}")
    return gen, artifact


def test_chronos_benchmark_deterministic_arm_reproduces_from_the_live_harness():
    """
    The seasonal-naive arm of ``docs/chronos_benchmark.json`` — one of the two
    baselines the Chronos verdict is stated against — re-scored through the
    generator's own callable.

    THIS PIN RUNS IN CI. It guards the denominator of the claim
    ``docs/CHRONOS_BENCHMARK.md`` publishes ("Chronos beats / loses to the
    baseline by X"): if the baseline the document was written against stopped
    falling out of the code, the published comparison would be describing a
    number that no longer exists.

    Deterministic for exactly the reasons given in section 7, and verified there
    on CI's own linux/amd64 + Python 3.11 stack with zero differing leaves.

    Measured: <0.01 s.
    """
    # Neither `importorskip` nor `skip` — see the note in the forecast pin above.
    # A CI pin that can vanish quietly is not a pin.
    assert CHRONOS_JSON.is_file(), (
        f"docs/chronos_benchmark.json is missing, though it is committed. {CHRONOS_REGENERATE}")

    gen, artifact = _load_chronos_artifact()

    problems: List[str] = []
    windows_scored = _rescore_arms(
        gen, artifact, {"seasonal_naive": gen.seasonal_naive_fit_predict},
        problems, CHRONOS_REGENERATE,
    )

    assert windows_scored == gen.N_WINDOWS >= 3, (
        f"only {windows_scored} rolling origins were scored — the pin has gone quiet.")
    assert not problems, (
        "docs/chronos_benchmark.json's DETERMINISTIC seasonal-naive arm no longer "
        f"matches what app/ml/backtest.py produces ({len(problems)} differing values). "
        "This is the baseline the Chronos verdict is stated against.\n"
        "The arm does no arithmetic of its own — it copies observations out of a "
        "SHA-256-pinned series — and it was verified to reproduce with ZERO differing "
        "leaves on CI's own linux/amd64 + Python 3.11 stack. So this is NOT platform "
        "noise: the ARTIFACT is stale, not this test.\n"
        f"{CHRONOS_REGENERATE}\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


@pytest.mark.slow
def test_chronos_benchmark_prophet_arm_reproduces_from_the_live_harness():
    """
    The Prophet arm of ``docs/chronos_benchmark.json`` — the other baseline the
    Chronos verdict is measured against.

    Measured: ~0.3 s. It is `slow` for the SAME platform reason as the Prophet
    arms in section 7, not for cost: Prophet fits via Stan (L-BFGS) and does not
    reproduce bit-for-bit off the machine that wrote the artifact. Promoted into
    CI on 2026-08-30, it failed there with 61 differing values — every one a
    ~0.2-0.3% relative wobble — against an artifact that was entirely current.
    The tolerance is therefore left strict and the test left local-only, rather
    than loosened into a check that could not fail.
    """
    pytest.importorskip("prophet")
    if not CHRONOS_JSON.is_file():
        pytest.skip("docs/chronos_benchmark.json not present")

    gen, artifact = _load_chronos_artifact()

    problems: List[str] = []
    windows_scored = _rescore_arms(
        gen, artifact,
        {"prophet": gen.make_prophet_fit_predict(yearly_seasonality=True)},
        problems, CHRONOS_REGENERATE,
    )

    assert windows_scored == gen.N_WINDOWS >= 3, (
        f"only {windows_scored} rolling origins were scored — the pin has gone quiet.")
    assert not problems, (
        _prophet_mismatch_message(
            "chronos_benchmark.json", len(problems),
            "test_chronos_benchmark_deterministic_arm_reproduces_from_the_live_harness",
            CHRONOS_REGENERATE,
        )
        + "These are the baselines the Chronos verdict is stated against.\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )


@pytest.mark.slow
def test_chronos_zero_shot_forecasts_reproduce_from_the_cached_weights():
    """
    The Chronos arm itself, plus the cold-start table.

    The 2026-08-29 sizing recorded this as unpinnable — "torch + HF weights +
    network". Two of those three are wrong on the machine that generates the
    artifact: `chronos-bolt-tiny` is 8.65 M parameters and already sits in the
    local HF cache, and `HF_HUB_OFFLINE` makes `from_pretrained` read that cache
    without touching the network. MEASURED: 2.6 s to load the weights, 0.06 s for
    the whole walk-forward, 1.0 s for the cold-start table. The forward pass is
    deterministic (Chronos-Bolt is direct quantile regression — no sampling), and
    it reproduces the committed artifact to the last decimal.

    HONEST LIMITS, stated because they are the reason this stays behind `slow`:
      * `torch` + `chronos-forecasting` come from `requirements-ml.txt`, which CI
        does not install. This test SKIPS there. It runs on the machine where the
        artifact is generated, which is the only machine where it can go stale.
      * It skips rather than fails when the weight cache is cold, because
        downloading 8.65 M parameters mid-test would make the suite depend on
        Hugging Face being up. A skip is honest; a network fetch would not be.
      * The timing fields (`import_seconds`, `load_seconds`, `steady_state`, ...)
        and `hardware` are machine facts and are NOT compared. Only the forecasts
        and the parameter count are.
    """
    pytest.importorskip("torch")
    pytest.importorskip("chronos")
    pytest.importorskip("prophet")
    if not CHRONOS_JSON.is_file():
        pytest.skip("docs/chronos_benchmark.json not present")

    _resolve_repo_python_path()

    import seeds.run_chronos_benchmark as gen
    from app.ml.backtest import walk_forward_backtest

    artifact = json.loads(CHRONOS_JSON.read_text())
    meta = artifact["meta"]
    model_name = artifact["chronos"]["model"]
    assert model_name == gen.DEFAULT_CHRONOS_MODEL, (
        f"the artifact was built with {model_name!r} but the generator now defaults to "
        f"{gen.DEFAULT_CHRONOS_MODEL!r}. {CHRONOS_REGENERATE}"
    )
    if not gen._weights_cached(model_name):
        pytest.skip(
            f"{model_name} is not in the local Hugging Face cache. This pin reads "
            "cached weights offline by design and will not download 8.65M parameters "
            "mid-suite; run `python -m seeds.run_chronos_benchmark` once to populate "
            "the cache."
        )

    # Belt and braces: the env var for a cold huggingface_hub import, the module
    # constant for one that is already imported. Either way this cannot reach the
    # network, so a green result here can never mean "HF was up today".
    import os
    previous_env = {k: os.environ.get(k) for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    hub_constants = None
    previous_offline = None
    try:
        import huggingface_hub.constants as hub_constants  # noqa: PLC0415
        previous_offline = hub_constants.HF_HUB_OFFLINE
        hub_constants.HF_HUB_OFFLINE = True
    except ImportError:  # pragma: no cover - chronos always brings the hub with it
        pass

    try:
        load = gen._load_series(None, offline=True)
        _assert_series_matches_artifact(load, meta, CHRONOS_REGENERATE)
        values = [float(v) for v in load.series.to_numpy()]

        fit_predict, chronos_meta = gen.make_chronos_fit_predict(model_name)

        started = time.perf_counter()
        report = walk_forward_backtest(
            values, fit_predict, horizon=gen.HORIZON, n_windows=gen.N_WINDOWS
        ).as_dict()
        cold_start = {
            "prophet": gen.cold_start_eval(
                values, gen.make_prophet_fit_predict(yearly_seasonality=True),
                gen.COLD_START_CONTEXT),
            "prophet_trend_only": gen.cold_start_eval(
                values, gen.make_prophet_fit_predict(yearly_seasonality=False),
                gen.COLD_START_CONTEXT),
            "seasonal_naive": gen.cold_start_eval(
                values, gen.seasonal_naive_fit_predict, gen.COLD_START_CONTEXT),
            "chronos": gen.cold_start_eval(
                values, fit_predict, gen.COLD_START_CONTEXT),
        }
        elapsed = time.perf_counter() - started
    finally:
        if hub_constants is not None and previous_offline is not None:
            hub_constants.HF_HUB_OFFLINE = previous_offline
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert chronos_meta["n_parameters"] == artifact["chronos"]["n_parameters"], (
        f"the loaded checkpoint has {chronos_meta['n_parameters']} parameters; the "
        f"artifact was built from one with {artifact['chronos']['n_parameters']}. A "
        f"DIFFERENT MODEL is in the cache under the same name. {CHRONOS_REGENERATE}"
    )

    # Anti-vacuity: cold_start_eval swallows its own exceptions and returns None.
    # Without this, a Chronos that raised on every call would produce four Nones
    # and a silently green test.
    missing = [name for name, block in cold_start.items() if not block]
    assert not missing, (
        f"cold_start_eval returned nothing for {missing} — it catches its own "
        "exceptions and returns None, so this pin would have compared nothing. That is "
        "a broken model or harness, NOT a stale artifact."
    )
    assert len(report["per_window"]) == gen.N_WINDOWS >= 3, (
        f"only {len(report['per_window'])} rolling origins were scored — the pin has "
        "gone quiet.")
    assert elapsed < 120.0, (
        f"the Chronos re-score took {elapsed:.1f}s against a ~1 s measurement.")

    problems: List[str] = []
    _compare(
        "chronos",
        {k: v for k, v in artifact["chronos"].items() if k not in CHRONOS_ENV_FIELDS},
        {**report, "model": chronos_meta["model"],
         "n_parameters": chronos_meta["n_parameters"]},
        problems, abs_tol=STAT_ABS_TOL, rel_tol=STAT_REL_TOL,
    )
    for name, block in cold_start.items():
        _compare(f"cold_start.{name}", artifact["cold_start"][name], block, problems,
                 abs_tol=STAT_ABS_TOL, rel_tol=STAT_REL_TOL)
    _compare("cold_start.context_len", artifact["cold_start"]["context_len"],
             gen.COLD_START_CONTEXT, problems, abs_tol=0.0)

    assert not problems, (
        "docs/chronos_benchmark.json's Chronos forecasts no longer match what the "
        f"cached checkpoint produces ({len(problems)} differing values).\n"
        f"The ARTIFACT is stale, not this test.\n{CHRONOS_REGENERATE}\n\n"
        + "\n".join(f"  - {p}" for p in problems[:40])
        + (f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else "")
    )
