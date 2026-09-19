"""
Drift guard: the committed ARTIFACTS must agree with the CODE that produced them.

Why this file exists (2026-08-29)
---------------------------------
A doc-vs-artifact check has a blind spot the repo hit twice: a code change moved a
published number, the generator was never re-run, and the doc and the artifact were
stale *together*, so the doc-vs-artifact test stayed green while both disagreed with
the code.

The repo's standing bar names exactly this failure: *"twice shipped figures that two documents
agreed on while both disagreed with the code."* Nothing can catch it except a
check that re-runs the real code path and compares the result to the bytes on
disk. That is what this file does.

What is pinned here
-------------------
Sections 1-3 and 5 pinned the sourcing optimizer's artifacts (``volume_sweep.json``,
the ``/benchmark`` page's fallback table, ``diversification_frontier.json`` and
``cvar_frontier.json``). They were removed with that optimizer; the work is archived
at git tag ``archive/sourcing-v1``. The surviving sections keep their numbers so the
cross-references below stay valid.

4. ``docs/newsvendor.json`` — the PRIMARY configuration is re-run through
   ``app.optimization.newsvendor.run_panel_evaluation`` at the artifact's own
   ``n_boot``/``seed``. Measured: 3.3 s. Its existing test file states in its own
   docstring that it "does not re-run the evaluation"; this is the missing half.

6-8. The HEAVY artifacts — see the block at the bottom of this file.
   ``leakage_progression.json`` (panel + one fold per regime),
   ``forecast_backtest.json`` and ``chronos_benchmark.json``.

   The two demand-series artifacts are each pinned by TWO tests, split by whether
   the arm is REPRODUCIBLE rather than by what it costs:

     * the seasonal-naive arm of each is deterministic — it copies observations
       out of a SHA-256-pinned series and does no arithmetic of its own — so it
       is UNMARKED and runs in CI's default suite.
     * the Prophet arms and the Chronos arm stay ``@pytest.mark.slow``: they
       run only on the artifacts' own platform, macOS/arm64 (locally, and in
       the ``python`` job of ``.github/workflows/repo-tests.yml``). Prophet fits
       via Stan (L-BFGS) and is NOT bit-reproducible across platform /
       interpreter / BLAS; promoting it into the Linux CI on 2026-08-30 turned
       it red with 160 differing values against artifacts that were entirely
       current. Both halves keep the SAME strict tolerance — widening it until a
       non-deterministic fit passed would be a check that cannot fail.
     * ``leakage_progression.json`` is ``slow`` too; it re-solves on the exact
       panel bytes the artifact was built from.

Deliberately NOT pinned:
  * ``intermittent_demand.json`` (~22 s) — already cross-pinned by
    ``test_newsvendor.py::test_the_recomputed_mase_reproduces_the_published_
    leaderboard``.
  * ``backend_verification.json`` — HONESTLY UNPINNABLE, reasoned out in full in
    the block at the bottom of this file. No generator for it exists in this repo.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

# Default absolute tolerance for `_compare`: absorbs last-bit float noise across
# platforms on cent-rounded dollar figures, far tighter than any real drift.
MONEY_TOL = 0.011
# Newsvendor carries raw probabilities and per-SKU costs, where 0.011 absolute
# would swallow a real change. Compared on the tighter of absolute/relative.
STAT_ABS_TOL = 1e-9
STAT_REL_TOL = 1e-9


# ── shared helpers ───────────────────────────────────────────────────────────

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
# The pin above runs in the default suite. The artifacts below were left
# unpinned on 2026-08-29 with the sizing "leakage_progression ~215 s, chronos
# torch+weights+network,
# forecast_backtest Prophet per rolling origin, backend_verification 42 live HTTPS
# calls". Every one of those numbers is the cost of REGENERATING THE WHOLE
# ARTIFACT. A pin does not need to do that: re-solving the smallest canonical
# slice through the generator's own function goes red on exactly the same code
# drift. Measured cost of the whole block below: well under two minutes.
#
# WHAT `slow` MEANS IN THIS BLOCK, AND WHAT IT DOES NOT
# ------------------------------------------------------
# `slow` here means MACOS/ARM64-ONLY, not expensive — sections 7 and 8 below are
# sub-second and are still marked. `ci.yml` (Linux) runs `-m "not slow"`; the
# `python` job of `repo-tests.yml` runs the WHOLE suite with no `-m` filter on a
# macOS/arm64 runner, and so does the local standing gate (`pytest tests/ -q`).
# The reasons a pin needs that platform or that setup are properties of the
# environment, never a way to dodge a red test:
#
#   1. THE COMPUTATION IS NOT REPRODUCIBLE ACROSS PLATFORMS — the Prophet arms of
#      sections 7 and 8. Stan's L-BFGS gives platform-dependent results, so a pin
#      that demands exactness can only be honest on the platform that WROTE the
#      artifact. See the block above section 7 for the measurement.
#   2. SETUP THE LINUX CI DOES NOT DO — the Chronos arm reads a Hugging Face
#      weight cache offline; `repo-tests.yml` downloads it before the suite.
#
# REFERENCE PLATFORM. `leakage_progression.json` and `chronos_benchmark.json` are
# generated on GitHub's macOS/arm64 runner (the regenerate-reference-artifacts
# workflow), and the `python` job of `repo-tests.yml` is AUTHORITATIVE for them.
# Their MLP arm and Chronos forecasts use chip-dependent kernels, so on another
# Mac (for example an M5) the leakage-progression and Chronos zero-shot pins can
# differ in the last decimals: a local failure of exactly those two pins is
# expected and is not a reason to regenerate locally or loosen the tolerance.
#
# What is NOT confined here: the deterministic seasonal-naive arm of each demand
# artifact. Those are unmarked and are CI's only artifact-vs-code coverage.
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


def _resolve_repo_python_path() -> None:
    """`seeds.*` imports assume `backend/` is on sys.path, exactly as `-m` does."""
    import sys

    backend = str(REPO_ROOT / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)


# ── 6. leakage_progression.json — panel, folds and one fold per regime ───────

LEAKAGE_JSON = DOCS / "leakage_progression.json"

LEAKAGE_REGENERATE = (
    "Re-run `seeds.run_leakage_progression` through the regenerate-reference-artifacts "
    "workflow (GitHub macOS runner, the reference platform; a local run on another chip "
    "differs in the MLP arm) and commit docs/leakage_progression.json + "
    "docs/LEAKAGE_PROGRESSION.md (README.md and RESEARCH_TECHNIQUES.md quote this artifact)."
)


@pytest.mark.slow
def test_leakage_progression_reproduces_from_the_live_lead_time_model(tmp_path):
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

    # THE PANEL THE ARTIFACT WAS BUILT FROM, NOT THE LIVE FILE. The weekly
    # `collect-lead-times` workflow APPENDS a snapshot to the panel CSV every
    # Monday, while this artifact (and `metrics.joblib`, which
    # `test_the_leakage_artifact_describes_the_served_model_dataset` holds it to)
    # describe the panel the served model was trained on. Comparing against the
    # whole live file therefore went red every week on data growth alone, not on
    # code drift. So the input guard is exact instead: the artifact records the
    # byte length and sha256 of the panel it read, and those bytes must be,
    # byte for byte, the head of today's file. An append passes; any rewrite of
    # an existing row fails here, before anything is computed from it.
    # Anti-vacuity, same shape as the DB row-count guard: assert the INPUT before
    # trusting anything computed from it.
    from app.ml.lead_time_collector import PANEL_PATH
    built_from = artifact["provenance"]["inputs"]["lead_time_panel"]
    assert built_from["sha256"] == meta["panel_sha256"], (
        "the artifact's provenance and meta disagree about which panel it read. "
        f"{LEAKAGE_REGENERATE}")
    live_bytes = PANEL_PATH.read_bytes()
    panel_bytes = live_bytes[: built_from["bytes"]]
    panel_sha = hashlib.sha256(panel_bytes).hexdigest()
    assert len(panel_bytes) == built_from["bytes"] and panel_sha == meta["panel_sha256"], (
        f"the first {built_from['bytes']} bytes of {PANEL_PATH.relative_to(REPO_ROOT)} "
        f"hash to {panel_sha}, but the artifact was built from {meta['panel_sha256']}. "
        "Rows the artifact was measured on were REWRITTEN, not appended to, so a "
        f"mismatch below would not mean the code moved. {LEAKAGE_REGENERATE}"
    )
    panel_file = tmp_path / PANEL_PATH.name
    panel_file.write_bytes(panel_bytes)

    panel = gen.load_observed_panel(panel_file)
    assert panel is not None, (
        "no observed lead-time panel — this test would have checked nothing. Expected "
        f"at {meta['panel_path']}."
    )
    design = gen.build_training_design(panel)
    X, feature_cols = gen.build_design_matrix(design.records, schema=design.schema)
    y = design.y

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
#     suite. BE PRECISE ABOUT THE GAP THIS CLOSES: section 4 of this file is
#     also unmarked and already ran on CI (`backend/supply_chain.db` is committed
#     — see the `!backend/supply_chain.db` un-ignore in `.gitignore` — so the
#     optimizer pins are NOT skipped there; CI run 33318131193 reported 1,114
#     passed and just ONE skip across the whole suite). What CI had zero of was a
#     pin on either DEMAND-SERIES artifact: both were behind `slow` entirely.
#     That is the gap, and it is the one this closes.
#   * the Prophet arms — NOT reproducible off the generating machine. `slow`,
#     i.e. macOS/arm64-only: locally, where the artifact is written, and in the
#     `python` job of `repo-tests.yml`, which runs on a macOS/arm64 runner.
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
# chronos-forecasting + a Hugging Face weight cache, which only `repo-tests.yml`'s
# macOS/arm64 job sets up), so its reproducibility has never been exercised on a
# second platform. An arm
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

    It is NOT CI's first artifact-vs-code pin — section 4 is unmarked too and
    already runs there. It is the first one covering ``forecast_backtest.json``,
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
    is confined to the generating platform, where it is exact and meaningful.

    ``slow`` therefore means MACOS/ARM64-ONLY here, not EXPENSIVE. It runs in the
    local standing gate (`pytest tests/ -q`, no `-m` filter) and in the ``python``
    job of ``repo-tests.yml``, which runs the whole suite on a macOS/arm64 runner.
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
#   * prophet        — Stan/L-BFGS, `slow`, macOS/arm64-only. It failed Linux CI with 61
#                      differing values on 2026-08-30 against a current artifact.
#   * chronos        — `slow` for an independent reason (it reads a Hugging Face
#                      weight cache that only `repo-tests.yml`'s macOS/arm64 job
#                      downloads), so its determinism across platforms is
#                      unproven and it is treated as non-deterministic. See the test below it.

CHRONOS_JSON = DOCS / "chronos_benchmark.json"

CHRONOS_REGENERATE = (
    "Re-run `seeds.run_chronos_benchmark --offline` through the "
    "regenerate-reference-artifacts workflow (GitHub macOS runner, the reference "
    "platform; Chronos forecasts differ by chip) and commit "
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
    The tolerance is therefore left strict and the test confined to macOS/arm64
    (locally and in ``repo-tests.yml``), rather than loosened into a check that could not fail.
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
      * `torch` + `chronos-forecasting` come from `requirements-ml.txt`. The
        ``python`` job of ``repo-tests.yml`` installs them and downloads the
        weights before the suite, so this runs there as well as locally.
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
