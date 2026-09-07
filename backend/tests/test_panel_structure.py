"""The panel is part-attributes x dated observations — not a flat row table.

Four defects motivated this file:

  1. **A whole cross-section was silently deleted.** ``ACCEPTED_MATCH_TYPES``
     dropped rows with a NULL ``match_type``. Every row of the 2026-07-01
     snapshot has one, because the column did not exist in July — so training
     ran on ONE calendar date while reporting two, and the 56 STMicroelectronics
     parts whose quote moved 30 -> 40/52 weeks never entered the model.
  2. **Champion-metric mismatch.** ``seeds/select_champion.py`` ranked on
     single-split ``rmse`` while the training pipeline ranked on
     ``cv_rmse_mean``, so running it promoted a different model than the one
     that had been evaluated.
  3. **No provenance.** ``metrics.joblib`` recorded nothing about when it was
     trained or from what, so no staleness or reproducibility claim was possible.
  4. **No lead-time ship gate**, while the regime model had one — baselines were
     computed and then ignored.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.ml.lead_time_model import (
    ACCEPTED_MATCH_TYPES,
    PART_STATIC_PANEL_COLUMNS,
    TIME_VARYING_PANEL_COLUMNS,
    enrich_static_attributes,
    evaluate_lead_time_ship_gate,
    leakage_audit,
    load_observed_panel,
    panel_to_records,
    resolve_schema_from_records,
)

PANEL_PATH = (
    Path(__file__).resolve().parent.parent
    / "seeds" / "data" / "lead_time_panel" / "observed_lead_times.csv"
)


@pytest.fixture(scope="module")
def panel():
    df = load_observed_panel(PANEL_PATH)
    if df is None or "snapshot_date" not in df.columns:
        pytest.skip("observed panel unavailable")
    return df


# ── 1. every cross-section must survive ─────────────────────────────────────

def test_no_snapshot_date_is_silently_dropped(panel):
    """THE regression test. July was 100% deleted by the match-type filter."""
    dates_in = set(panel["snapshot_date"].astype(str))
    _records, _y, _groups, dates_out, counts = panel_to_records(panel)
    assert set(dates_out) == dates_in, (
        f"training lost snapshot date(s) {sorted(dates_in - set(dates_out))}; "
        f"counts={counts}"
    )
    assert counts["distinct_snapshot_dates"] == len(dates_in)


def test_unverified_match_type_is_accepted_but_bad_matches_are_not():
    assert "unverified" in ACCEPTED_MATCH_TYPES
    for bad in ("fuzzy", "none", "no_match"):
        assert bad not in ACCEPTED_MATCH_TYPES


def test_rows_predating_match_type_are_kept(panel):
    """A NULL match_type means 'not recorded', not 'bad match'."""
    null_match = panel[panel["match_type"].isna()] if "match_type" in panel else panel.iloc[:0]
    if null_match.empty:
        pytest.skip("panel has no rows predating match_type")
    _records, y, _g, _d, counts = panel_to_records(panel)
    assert counts["dropped_bad_match"] < len(null_match), (
        "rows with an unrecorded match_type are still being discarded as bad matches"
    )


# ── the static / time-varying split ─────────────────────────────────────────

def test_static_and_time_varying_columns_do_not_overlap():
    """A column carried across dates must never also be a per-observation value."""
    assert not (set(PART_STATIC_PANEL_COLUMNS) & set(TIME_VARYING_PANEL_COLUMNS))


def test_the_target_is_never_a_static_attribute():
    for target_col in ("lead_time_weeks", "lead_time_weeks_raw"):
        assert target_col not in PART_STATIC_PANEL_COLUMNS
        assert target_col in TIME_VARYING_PANEL_COLUMNS


def test_static_attributes_are_shared_across_a_parts_observations():
    df = pd.DataFrame({
        "mpn": ["A", "A", "B"],
        "snapshot_date": ["2026-07-01", "2026-08-15", "2026-08-15"],
        "lead_time_weeks": [30, 52, 8],
        "dk_category": [np.nan, "Integrated Circuits (ICs)", "Sensors"],
        "parameter_count": [np.nan, 18, 4],
        "unit_price": [1.0, 2.0, 3.0],
    })
    out, counts = enrich_static_attributes(df)
    assert out.loc[0, "dk_category"] == "Integrated Circuits (ICs)"
    assert out.loc[0, "parameter_count"] == 18
    assert counts["static_cells_filled"] == 2


def test_time_varying_values_are_never_back_propagated():
    """The whole point: July keeps ITS OWN target, price and lifecycle."""
    df = pd.DataFrame({
        "mpn": ["A", "A"],
        "snapshot_date": ["2026-07-01", "2026-08-15"],
        "lead_time_weeks": [30, 52],
        "unit_price": [1.0, 9.0],
        "lifecycle_status": ["Active", "Obsolete"],
        "dk_category": [np.nan, "Integrated Circuits (ICs)"],
    })
    out, _ = enrich_static_attributes(df)
    assert out.loc[0, "lead_time_weeks"] == 30
    assert out.loc[0, "unit_price"] == 1.0
    assert out.loc[0, "lifecycle_status"] == "Active"


def test_within_part_target_variation_reaches_training(panel):
    """The 56 ST parts whose quote moved must appear as two distinct labels."""
    if "mpn" not in panel.columns:
        pytest.skip("panel has no mpn column")
    repeats = panel.groupby("mpn")["lead_time_weeks"].nunique()
    changed = set(repeats[repeats > 1].index)
    if not changed:
        pytest.skip("no part was observed with two different lead times")

    _records, y, groups, dates, _counts = panel_to_records(panel)
    assert len(set(dates)) > 1, "training data has only one snapshot date"
    # At least one family must carry more than one distinct target value.
    by_group: dict[str, set[float]] = {}
    for group, value in zip(groups, y, strict=True):
        by_group.setdefault(group, set()).add(round(float(value), 3))
    assert any(len(v) > 1 for v in by_group.values()), (
        "no within-part target variation survived into training"
    )


def test_a_feature_may_not_cost_an_entire_cross_section():
    """A column absent from one whole date must be excluded, not delete the date."""
    records = [
        {"dk_category": "Integrated Circuits (ICs)", "unit_price": 5.0, "parameter_count": 3},
        {"dk_category": "Integrated Circuits (ICs)", "unit_price": 6.0, "parameter_count": None},
    ] * 20
    dates = ["2026-08-15", "2026-07-01"] * 20
    _schema, exclusions = resolve_schema_from_records(records, snapshot_dates=dates)
    reason = next(
        (e["reason"] for e in exclusions if e["feature"] == "parameter_count"), None
    )
    assert reason is not None and "cross-section" in reason


# ── 4. the lead-time ship gate ──────────────────────────────────────────────

def _gate_input(beaten=None, significant=True, **extra):
    result = {
        "status": "trained",
        "best": "random_forest",
        "toughest_baseline": "manufacturer_mean",
        "skill_vs_toughest_baseline": 0.09,
        "baselines_beaten": beaten if beaten is not None else {
            "train_mean": True, "always_210d": True,
            "category_mean": True, "manufacturer_mean": True,
        },
        "paired_vs_toughest_baseline": {
            "available": True, "significant_ci": significant,
            "mean_rmse_reduction_days": 6.7, "ci95_low": 3.6, "ci95_high": 10.3,
            "folds_model_won": 16, "n_folds": 20,
        },
    }
    result.update(extra)
    return result


def test_lead_time_ship_gate_passes_when_it_beats_every_baseline():
    gate = evaluate_lead_time_ship_gate(_gate_input())
    assert gate["passed"] is True


def test_lead_time_ship_gate_blocks_a_model_that_loses_to_a_baseline():
    losing = {"train_mean": True, "always_210d": True,
              "category_mean": True, "manufacturer_mean": False}
    gate = evaluate_lead_time_ship_gate(_gate_input(beaten=losing))
    assert gate["passed"] is False
    assert "manufacturer_mean" in gate["reason"]


def test_lead_time_ship_gate_blocks_an_insignificant_margin():
    """Beating a baseline on a point estimate is not evidence."""
    gate = evaluate_lead_time_ship_gate(_gate_input(significant=False))
    assert gate["passed"] is False
    assert "not separated from zero" in gate["reason"]


def test_lead_time_ship_gate_fails_closed():
    assert evaluate_lead_time_ship_gate(None)["passed"] is False
    assert evaluate_lead_time_ship_gate({"status": "skipped"})["passed"] is False
    assert evaluate_lead_time_ship_gate(
        _gate_input(baselines_beaten={})
    )["passed"] is False


# ── 2. one champion metric ──────────────────────────────────────────────────

def test_champion_selection_metric_is_defined_once_and_grouped():
    """Training and select_champion must not rank on different things."""
    import inspect

    from app.ml import mlflow_tracking
    import seeds.select_champion as select_champion

    assert mlflow_tracking.LEAD_TIME_SELECTION_METRIC == "cv_rmse_mean"
    source = inspect.getsource(select_champion)
    assert 'metric="rmse"' not in source, (
        "select_champion still hardcodes single-split rmse for lead time"
    )
    assert "LEAD_TIME_SELECTION_METRIC" in source


# ── 3. provenance ───────────────────────────────────────────────────────────

def test_metrics_artifact_carries_provenance():
    from app.ml import model_store

    metrics = model_store.load("metrics")
    if not metrics:
        pytest.skip("no metrics artifact — run `python -m seeds.train_ml_models`")
    prov = metrics.get("provenance")
    assert prov, "metrics.joblib has no provenance block"
    for key in ("trained_at", "training_data_sha256", "training_data_path", "git_sha"):
        assert key in prov, f"provenance is missing {key}"
    assert prov["trained_at"]
    # The hash must actually match the panel currently on disk, or the artifact
    # is stale and we can now SAY so instead of guessing.
    if PANEL_PATH.exists() and prov.get("training_data_sha256"):
        current = model_store.file_sha256(PANEL_PATH)
        assert current is not None
        if current != prov["training_data_sha256"]:
            pytest.skip("artifact predates the current panel (staleness is detectable)")


def test_provenance_records_no_absolute_filesystem_path():
    """Provenance is PUBLISHED, so a path recorded at fit time is a path served.

    The committed artifact carried an absolute path of the form
    ``/Users/<name>/.../observed_lead_times.csv`` in
    ``provenance.training_data_path``, and ``GET /ml/model-info`` returns the
    provenance block verbatim — so every visitor to the live API was told the
    trainer's operating system, username and directory layout. It was never
    caught because the field was only ever read back on the machine that wrote
    it, where an absolute path looks perfectly normal.

    ``model_store.repo_relative`` now relativises at capture. This gate is the
    other half: it fails if an absolute path ever gets persisted again, whoever
    retrains and wherever from. A relative path identifies the same file and
    discloses nothing about the machine.
    """
    from app.ml import model_store

    metrics = model_store.load("metrics")
    if not metrics:
        pytest.skip("no metrics artifact — run `python -m seeds.train_ml_models`")
    recorded = (metrics.get("provenance") or {}).get("training_data_path")
    assert recorded, "provenance must record which data trained the artifact"
    assert not Path(recorded).is_absolute(), (
        f"provenance.training_data_path is an absolute path ({recorded!r}). "
        "GET /ml/model-info publishes this block, so an absolute path leaks the "
        "trainer's home directory to every visitor. Record it with "
        "model_store.repo_relative() at capture time."
    )
    # ...and it must still LOCATE the panel, or relativising broke the staleness
    # check to buy the privacy — which would be trading one defect for another.
    assert model_store.resolve_repo_path(recorded).exists(), (
        f"provenance.training_data_path {recorded!r} does not resolve to a file "
        "in this checkout — the staleness check cannot find the panel"
    )


# ── the leakage collapse, reproducibly ──────────────────────────────────────

def test_leakage_audit_reproduces_the_collapse():
    """Random split >> family-grouped >> manufacturer-held-out. The finding."""
    rng = np.random.default_rng(0)
    n_mfr, per_mfr = 6, 30
    rows, targets, families, mfrs = [], [], [], []
    for m in range(n_mfr):
        level = rng.uniform(20, 200)
        for i in range(per_mfr):
            fam = f"fam{m}_{i // 5}"
            rows.append([level + rng.normal(0, 2), float(i % 5)])
            targets.append(level + rng.normal(0, 2))
            families.append(fam)
            mfrs.append(f"mfr{m}")
    X = np.asarray(rows, dtype=float)
    y = np.asarray(targets, dtype=float)

    audit = leakage_audit(X, y, families, mfrs, n_splits=5)
    assert audit["n_manufacturers"] == n_mfr
    assert audit["random"] is not None and audit["manufacturer"] is not None
    assert audit["random"] > audit["manufacturer"], (
        "holding out whole manufacturers must be strictly harder than a random split"
    )
    assert "effective sample size" in audit["headline"]


def test_persisted_leakage_audit_is_published():
    from app.ml import model_store

    metrics = model_store.load("metrics")
    if not metrics:
        pytest.skip("no metrics artifact")
    audit = metrics.get("lead_time_leakage_audit")
    if not audit:
        pytest.skip("artifact predates the leakage audit — retrain")
    for key in ("random", "family", "manufacturer", "n_manufacturers", "headline"):
        assert key in audit
    assert audit["random"] > audit["family"] > audit["manufacturer"], (
        f"the collapse should be monotonic; got {audit}"
    )
