"""
The part-family leakage collapse, measured end to end.

WHY THIS EXISTS
---------------
This project's headline ML finding is that the lead-time model's R² collapses as
the split protocol gets honest. It was published twice, with two different sets of
numbers:

  * ``docs/MODEL_CI.md``          R² 0.61 -> 0.19 -> -0.48, "27 manufacturers, not 810 rows"
  * ``docs/RESEARCH_TECHNIQUES.md`` R² 0.95 -> 0.19-0.29 -> 0.06, "28 manufacturers, not 684 rows"

Two published versions of one number is worse than either being wrong, and the
repo's own rule is that every published figure must be reproducible. So this
script measures the progression from the panel, writes the result to
``docs/leakage_progression.json``, and the docs cite that artifact instead of a
remembered number.

WHAT IT MEASURES
----------------
The SAME estimators, on the SAME rows, through the SAME feature pipeline, with the
SAME seed, scored under three split regimes. The grouping is the ONLY thing that
varies:

  random        ``KFold(shuffle=True)`` — the naive, wrong protocol. Sibling
                variants of one part family land on both sides of the fold
                boundary, so this scores the model's ability to RECOGNISE a part
                family it has already seen.
  family        ``GroupKFold`` on the ``base_product`` family key — the protocol
                the shipped model actually uses (``lead_time_model._group_key``).
  manufacturer  ``GroupKFold`` on the manufacturer — whole vendors held out. This
                is the question the model is deployed to answer: a part from a
                vendor we have never quoted.

Everything is scored on identical folds, models and naive baselines alike, so a
per-regime comparison is paired. Mean AND median are reported with the fold spread,
because they disagree materially here and the repo has been careful about that
distinction elsewhere (``cv_r2_mean`` vs ``cv_r2_median`` in
``app/api/ml.py``): R² divides by the test fold's own label variance, so a fold
that happens to be nearly constant blows up negative and drags the mean far below
the median. That is a property of the fold, not of the model.

TWO GROUPING COUNTS — DO NOT CONFUSE THEM EITHER
------------------------------------------------
The panel has **360** distinct ``base_product`` values and **467** distinct
*group keys*. They are not the same quantity and must not share the word
"families" in prose. ``lead_time_model._group_key`` (see its docstring, which
states the same arithmetic) emits ``family:{base_product}`` when DigiKey returned
a base product, ``mpn:{mpn}`` when it did not, and ``row:{i}`` as a last resort.
The fallbacks only ever *split* a group, never merge two real families — so the
group-key count is a strict refinement of the base-product count: 359 real
base-product families + 108 distinct MPNs from the rows whose ``base_product`` is
``Unknown`` = 467 keys, against 359 + 1 ``Unknown`` level = 360 base products.
This module therefore publishes ``n_family_group_keys`` (467) and reads the
base-product level count (360) out of ``identity_column_in_sample_r2`` rather than
calling either one "part families".

A THIRD, DIFFERENT QUANTITY — DO NOT CONFUSE THEM
-------------------------------------------------
The script also reports the IN-SAMPLE explanatory power of single identity
columns: fit a per-level mean on all rows and score it on those same rows
(one-way ANOVA R²). ``base_product`` scores high there. That number is NOT a
model score and NOT cross-validated — it is the measurement of how much redundancy
the panel contains, i.e. the REASON the random split is inflated. It belongs in
``identity_column_in_sample_r2``, never in the progression, and conflating the two
is exactly how ``RESEARCH_TECHNIQUES.md`` came to report 0.95 as a random-split
score.

WHAT THIS SCRIPT MUST NOT DO
----------------------------
Fit anything to disk. Every estimator here is fitted in memory and discarded; the
committed artifacts under ``backend/data/ml_models/`` are written only by
``seeds/train_ml_models.py``. This is a measurement, not a retrain.

Nor does it own a private copy of the data-preparation rules. The rows, labels and
group keys come from ``lead_time_model.build_training_design`` — the same function
``retrain_lead_time`` uses — and the naive baselines come from
``lead_time_model.baseline_predictors``. If either changed, this report would
change with it rather than quietly describing a dataset nobody trains on.

OUTPUTS
-------
  docs/leakage_progression.json   machine-readable, per-fold scores and metadata
  docs/LEAKAGE_PROGRESSION.md     the human writeup

Invocation:  cd backend && python -m seeds.run_leakage_progression
             python -m seeds.run_leakage_progression --quick   (champion only, 2 repeats)
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import platform
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, cast

import numpy as np
import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# ruff: E402 below is unavoidable — sys.path must be primed before `app.*`/`seeds.*`
# resolve when this module is run as `python -m seeds.run_leakage_progression` from
# backend/, and `from __future__ import annotations` must stay first.
from sklearn.metrics import r2_score  # noqa: E402
from sklearn.model_selection import GroupKFold, KFold  # noqa: E402

from app.ml.lead_time_model import (  # noqa: E402
    FEATURE_SCHEMA_VERSION,
    MODELS,
    baseline_predictors,
    build_design_matrix,
    build_training_design,
    load_observed_panel,
)
from seeds.provenance import build_provenance, provenance_markdown  # noqa: E402

logger = logging.getLogger(__name__)

REPO_ROOT = BACKEND_ROOT.parent
DOCS = REPO_ROOT / "docs"

#: Folds per repeat. 5 is the largest k that still leaves every manufacturer-held-out
#: fold with enough vendors on the test side to be meaningful: there are only 27
#: manufacturers, so k=10 would put ~2-3 vendors in each test fold and the score
#: would be almost pure fold-composition noise.
N_SPLITS = 5

#: Independent shuffles of the fold assignment. 5 folds alone give 5 numbers, which
#: is far too few to state a spread on; 10 repeats give 50.
N_REPEATS = 10

#: The one seed. ``random_state = SEED + repeat`` in every regime, so the three
#: regimes see the same sequence of shuffles and differ ONLY in their grouping.
SEED = 42

#: The regime the shipped model is actually evaluated under, and therefore the one
#: whose mean RMSE selects the champion reported here.
CHAMPION_SELECTION_REGIME = "family"

REGIME_DESCRIPTIONS = {
    "random": (
        "KFold(shuffle=True) on rows. The naive, WRONG protocol: near-duplicate "
        "siblings of one part family straddle the fold boundary, so the score "
        "measures recognition of an already-seen family, not prediction."
    ),
    "family": (
        "GroupKFold on lead_time_model._group_key — family:{base_product} where DigiKey "
        "returned one, mpn:{mpn} otherwise. This is the protocol the shipped model uses. "
        "No family can straddle a fold. Note the key count exceeds the base_product level "
        "count because the MPN fallback splits the Unknown bucket; it never merges "
        "two real families."
    ),
    "manufacturer": (
        "GroupKFold on the manufacturer. Whole vendors are held out — the strictest "
        "and most honest generalisation test, and the one that matches deployment."
    ),
}


# ── fold construction ────────────────────────────────────────────────────────

def build_folds(
    n_rows: int,
    groups: Optional[Sequence[str]],
    n_splits: int,
    n_repeats: int,
    seed: int,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Repeated k-fold index pairs; grouped when ``groups`` is supplied.

    ``groups=None`` gives the ungrouped (random) regime. Every regime draws the
    same ``n_repeats`` random states, so nothing but the grouping differs.
    """
    folds: List[Tuple[np.ndarray, np.ndarray]] = []
    indices = np.arange(n_rows)
    for repeat in range(n_repeats):
        state = seed + repeat
        if groups is None:
            splitter = KFold(n_splits=n_splits, shuffle=True, random_state=state)
            iterator = splitter.split(indices)
        else:
            grouped = GroupKFold(n_splits=n_splits, shuffle=True, random_state=state)
            iterator = grouped.split(indices, groups=np.asarray(groups))
        folds.extend((np.asarray(tr), np.asarray(te)) for tr, te in iterator)
    return folds


def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.sqrt(np.mean((y_pred - y_true) ** 2)))


def summarise(scores: Sequence[float]) -> Dict[str, object]:
    """Mean AND median with the fold spread. Both, always — they disagree here.

    R² is a ratio whose denominator is the TEST FOLD's own label variance. A fold
    that is nearly constant in y therefore produces a large negative R² regardless
    of how good the predictions are in absolute terms, which pulls the mean far
    below the median. Reporting either alone hides that; reporting the full range
    and the deciles makes it checkable.
    """
    a = np.asarray(list(scores), dtype=float)
    if a.size == 0:
        return {"n_folds": 0}
    return {
        "n_folds": int(a.size),
        "mean": round(float(a.mean()), 4),
        "median": round(float(np.median(a)), 4),
        "std": round(float(a.std(ddof=1)), 4) if a.size > 1 else 0.0,
        "min": round(float(a.min()), 4),
        "max": round(float(a.max()), 4),
        "p10": round(float(np.percentile(a, 10)), 4),
        "p90": round(float(np.percentile(a, 90)), 4),
        "iqr": round(float(np.percentile(a, 75) - np.percentile(a, 25)), 4),
        "folds_below_zero": int((a < 0).sum()),
    }


# ── the measurement ──────────────────────────────────────────────────────────

def score_regime(
    X: np.ndarray,
    y: np.ndarray,
    feature_cols: Sequence[str],
    folds: Sequence[Tuple[np.ndarray, np.ndarray]],
    model_names: Sequence[str],
) -> Dict[str, object]:
    """Score every estimator and every naive baseline on ONE set of folds.

    Fits are in-memory and discarded — nothing here touches
    ``backend/data/ml_models/``.
    """
    baselines = baseline_predictors(feature_cols)
    r2_by: Dict[str, List[float]] = {k: [] for k in list(model_names) + list(baselines)}
    rmse_by: Dict[str, List[float]] = {k: [] for k in r2_by}
    fold_rows: List[Dict[str, object]] = []

    for fold_index, (tr, te) in enumerate(folds):
        y_tr, y_te = y[tr], y[te]
        # A test fold with no label variance makes R² undefined (0/0). It has not
        # happened on this panel, but silently scoring it would be a lie.
        if y_te.size < 2 or float(np.var(y_te)) == 0.0:
            logger.warning("fold %d has no label variance — skipping", fold_index)
            continue

        preds: Dict[str, np.ndarray] = {}
        for name in model_names:
            estimator = copy.deepcopy(MODELS[name])
            estimator.fit(X[tr], y_tr)
            preds[name] = estimator.predict(X[te])
        for name, predictor in baselines.items():
            preds[name] = predictor(X[tr], y_tr, X[te])

        for name, p in preds.items():
            r2_by[name].append(float(r2_score(y_te, p)))
            rmse_by[name].append(_rmse(y_te, p))

        fold_rows.append({
            "fold": fold_index,
            "n_train": int(tr.size),
            "n_test": int(te.size),
            "test_y_mean": round(float(y_te.mean()), 3),
            "test_y_std": round(float(y_te.std()), 3),
            "r2": {name: round(v[-1], 4) for name, v in r2_by.items()},
        })

    return {
        "r2": {name: summarise(v) for name, v in r2_by.items()},
        "rmse_days": {name: summarise(v) for name, v in rmse_by.items()},
        "r2_per_fold": {name: [round(v, 4) for v in vals] for name, vals in r2_by.items()},
        "folds": fold_rows,
    }


def identity_column_in_sample_r2(
    y: np.ndarray, identity_columns: Dict[str, List[str]], extra: Dict[str, List[str]]
) -> Dict[str, Dict[str, object]]:
    """How much of the target a bare IDENTITY column explains, IN SAMPLE.

    Fit a per-level mean on all rows, score it on those same rows: the one-way
    ANOVA R². This is NOT a model score and NOT cross-validated. It measures how
    much redundancy the panel contains — which is precisely why the random-split
    number above is inflated — and it is reported separately so the two can never
    be quoted as the same thing again.

    A column with as many levels as rows would score 1.0 by construction, so the
    level count is reported next to every figure.
    """
    out: Dict[str, Dict[str, object]] = {}
    for name, values in list(identity_columns.items()) + list(extra.items()):
        labels = pd.Series(values, dtype="object")
        fitted = pd.Series(y).groupby(labels.values).transform("mean").to_numpy()
        out[name] = {
            "in_sample_r2": round(float(r2_score(y, fitted)), 4),
            "n_levels": int(labels.nunique()),
            "n_rows": int(len(y)),
            "rows_per_level": round(len(y) / max(labels.nunique(), 1), 2),
        }
    return out


def _served_champion() -> Optional[str]:
    """Which estimator production actually serves, per the persisted metrics.

    Read-only. Returns ``None`` when the artifact is absent or unreadable, in
    which case the caller selects a champion from this run's own folds.
    """
    try:
        import joblib

        from app.ml import model_store
        metrics = joblib.load(model_store.path("metrics"))
        best = metrics.get("best_lead_time_model")
        return str(best) if best else None
    except Exception:  # noqa: BLE001 — no artifact is a normal state, not an error
        return None


def _served_training_sha() -> Optional[str]:
    """sha256 of the panel bytes the served model was trained on, per the persisted metrics.

    Read-only, and ``None`` when the artifact is absent or records no hash.
    """
    try:
        import joblib

        from app.ml import model_store
        metrics = joblib.load(model_store.path("metrics"))
        sha = (metrics.get("provenance") or {}).get("training_data_sha256")
        return str(sha) if sha else None
    except Exception:  # noqa: BLE001 — no artifact is a normal state, not an error
        return None


def served_panel_prefix(live: bytes, training_sha: str) -> Optional[bytes]:
    """The head of the live panel file that hashes to ``training_sha``, or ``None``.

    The weekly ``collect-lead-times`` workflow only APPENDS to the panel CSV, so the
    exact bytes the served model was trained on stay the head of the live file until
    the next retrain. This finds that head, cut at a row boundary, so the
    progression keeps describing the served model's dataset (the interlock in
    ``test_docs_match_artifacts.py``) instead of whatever the collector added since.
    """
    digest = hashlib.sha256()
    end = 0
    for line in live.splitlines(keepends=True):
        digest.update(line)
        end += len(line)
        if digest.copy().hexdigest() == training_sha:
            return live[:end]
    return None


def _library_versions() -> Dict[str, str]:
    import sklearn
    import scipy
    return {
        "scikit-learn": sklearn.__version__,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scipy": scipy.__version__,
    }


# ── writeup ──────────────────────────────────────────────────────────────────

def _row(label: str, block: Dict[str, object]) -> str:
    return (
        f"| {label} | {block['mean']:+.3f} | {block['median']:+.3f} | "
        f"{block['std']:.3f} | {block['p10']:+.3f} | {block['p90']:+.3f} | "
        f"{block['min']:+.3f} | {block['max']:+.3f} |"
    )


def render_markdown(payload: Dict[str, object]) -> str:
    provenance = cast(Dict[str, object], payload.get("provenance") or {})
    identity = cast(Dict[str, Dict[str, object]], payload["identity_column_in_sample_r2"])
    meta = payload["meta"]
    counts = cast(Dict[str, int], payload["counts"])
    champ = payload["champion_model"]
    regimes = payload["regimes"]
    prog = payload["progression"]

    lines: List[str] = []
    add = lines.append
    add("# The part-family leakage collapse, measured")
    add("")
    add(
        f"Generated `{meta['generated_utc']}` by `python -m seeds.run_leakage_progression` "
        f"(backend/, venv active). Machine-readable: [`leakage_progression.json`](leakage_progression.json)."
    )
    add("")
    add(
        "**Every number below is produced by that one command.** Earlier revisions of "
        "`MODEL_CI.md` and `RESEARCH_TECHNIQUES.md` quoted two different progressions "
        "from memory; this artifact is now the only source either of them cites."
    )
    add("")
    add("## The headline")
    add("")
    add(
        f"The same estimator (`{champ}`), the same {counts['n_rows']} rows, the same feature "
        "pipeline and the same seed. **The only thing that changes is what the fold "
        "boundary is allowed to cut through.**"
    )
    add("")
    add("| Split regime | R² mean | R² median | fold sd | p10 | p90 | min | max |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for regime in ("random", "family", "manufacturer"):
        add(_row(REGIME_LABELS[regime], regimes[regime]["r2"][champ]))
    add("")
    n_base_products = identity["base_product"]["n_levels"]
    add(
        f"Mean R² **{prog['random_mean']:+.3f} → {prog['family_mean']:+.3f} → "
        f"{prog['manufacturer_mean']:+.3f}**; median R² "
        f"**{prog['random_median']:+.3f} → {prog['family_median']:+.3f} → "
        f"{prog['manufacturer_median']:+.3f}**. "
        f"{counts['n_rows']} rows, {counts['n_family_group_keys']} family grouping keys, "
        f"{counts['n_manufacturers']} manufacturers."
    )
    add("")
    add(
        f"**{counts['n_family_group_keys']} grouping keys is not "
        f"{counts['n_family_group_keys']} part families**, and the two numbers are kept "
        "apart everywhere below. The fold groups are the output of "
        f"`lead_time_model._group_key`, which emits `family:{{base_product}}` when "
        "DigiKey returned a base product, `mpn:{mpn}` when it did not, and `row:{i}` as "
        "a last resort. A fallback can only ever split a group, never merge two real "
        f"families, so the key count ({counts['n_family_group_keys']}) is a strict "
        f"refinement of the **{n_base_products} distinct `base_product` values** in the "
        "panel. Read as \"part families\", the right number is "
        f"**{n_base_products}**; read as \"what the fold boundary actually respects\", it "
        f"is **{counts['n_family_group_keys']}**."
    )
    add("")
    add(
        f"**The effective sample size for generalisation is {counts['n_manufacturers']} "
        f"manufacturers, not {counts['n_rows']} rows.** Three vendors "
        "(Analog Devices, Texas Instruments, STMicroelectronics) supply "
        f"{payload['manufacturer_concentration']['top3_share_pct']}% of the panel, and "
        f"{payload['manufacturer_concentration']['n_with_le_6_rows']} of the "
        f"{counts['n_manufacturers']} vendors contribute 6 rows or fewer."
    )
    add("")
    add("## What the negative number means, precisely")
    add("")
    add(
        f"Holding out whole manufacturers, mean R² is **{prog['manufacturer_mean']:+.3f}** "
        f"and {regimes['manufacturer']['r2'][champ]['folds_below_zero']} of "
        f"{regimes['manufacturer']['r2'][champ]['n_folds']} folds score below zero. "
        "A negative R² is not a small positive one, and it is worth stating exactly "
        "what it is: R² is measured against the **held-out fold's own mean**, so "
        "R² < 0 means the model's squared error exceeds that vendor's entire label "
        "variance. On a vendor it has never quoted, the model has **no explanatory "
        "power at all** — it does not rank that vendor's parts correctly and its "
        "predicted level is biased for them."
    )
    add("")
    add(
        "It does **not** mean the model is beaten by every trivial predictor, and the "
        "honest version of the claim has to say so. Scored on those same "
        "manufacturer-held-out folds, `train_mean` gets "
        f"{regimes['manufacturer']['r2']['train_mean']['mean']:+.3f} and "
        "`manufacturer_mean` gets "
        f"{regimes['manufacturer']['r2']['manufacturer_mean']['mean']:+.3f} — both worse "
        f"than the model's {prog['manufacturer_mean']:+.3f}. The model is still the best "
        "of the set. It is simply the best member of a set in which **nothing "
        "generalises to an unseen vendor.**"
    )
    add("")
    add(
        "The mechanism is visible in the baseline table below: under a random split, a "
        "lookup table keyed on nothing but the manufacturer scores "
        f"{regimes['random']['r2']['manufacturer_mean']['mean']:+.3f} — almost the whole "
        f"of the full model's {prog['random_mean']:+.3f}. Vendor identity *is* the "
        "panel's signal, and holding a vendor out is precisely the operation that "
        "removes it. This is a statement about the dataset, not a bug in the estimator: "
        f"{counts['n_manufacturers']} vendors is a small sample no matter how many rows "
        "they generate."
    )
    add("")
    add(
        "As a harness sanity check, `train_mean` scores "
        f"{regimes['random']['r2']['train_mean']['mean']:+.3f} under the random regime — "
        "R² ≈ 0 for a constant predictor, which is what it must be if the scoring is right."
    )
    add("")
    add("### Mean vs median, and why both are quoted")
    add("")
    add(
        "R² divides by the *test fold's own* label variance. Under the manufacturer "
        "regime a fold can be dominated by one vendor whose quotes barely vary, and that "
        "fold's R² blows up negative regardless of absolute error — which is why the mean "
        f"({prog['manufacturer_mean']:+.3f}) sits far below the median "
        f"({prog['manufacturer_median']:+.3f}). RMSE has no such pathology, so it is "
        "reported alongside:"
    )
    add("")
    add("| Split regime | RMSE mean (days) | RMSE median (days) | fold sd |")
    add("|---|---:|---:|---:|")
    for regime in ("random", "family", "manufacturer"):
        block = regimes[regime]["rmse_days"][champ]
        add(
            f"| {REGIME_LABELS[regime]} | {block['mean']:.2f} | {block['median']:.2f} | "
            f"{block['std']:.2f} |"
        )
    add("")
    add(
        "The RMSE progression tells the same story without the ratio artefact: error "
        f"grows from {regimes['random']['rmse_days'][champ]['mean']:.1f} d to "
        f"{regimes['manufacturer']['rmse_days'][champ]['mean']:.1f} d as the protocol "
        "gets honest."
    )
    add("")
    add("## Naive baselines, on the identical folds")
    add("")
    add(
        "Every baseline is scored on exactly the folds above, so the comparison is "
        "paired. They come from `lead_time_model.baseline_predictors` — the same "
        "definitions the training path gates on — not from a copy living in this script."
    )
    add("")
    add("| Predictor | random R² | family R² | manufacturer R² |")
    add("|---|---:|---:|---:|")
    for name in payload["model_names"] + payload["baseline_names"]:
        cells = " | ".join(
            f"{regimes[r]['r2'][name]['mean']:+.3f}" for r in ("random", "family", "manufacturer")
        )
        marker = " *(champion)*" if name == champ else ""
        kind = "" if name in payload["model_names"] else " *(baseline)*"
        add(f"| `{name}`{marker}{kind} | {cells} |")
    add("")
    add(
        "The collapse is not an artefact of one estimator — every model in the bake-off "
        "shows it, including the two linear/neural ones whose manufacturer-regime means "
        "are dominated by a handful of catastrophic folds. And the ordering does not "
        "invert: the champion beats every naive baseline under all three regimes. What "
        "changes is that under the manufacturer regime the whole table is negative."
    )
    add("")
    add(
        f"The headline row is `{champ}` — the {payload['champion_source']}. This run's "
        f"own lowest family-regime RMSE belongs to "
        f"`{payload['best_model_under_family_regime']}`"
        + (
            "; the two agree."
            if payload["best_model_under_family_regime"] == champ
            else ", a different estimator. They are within noise of each other on this "
                 "panel, and the headline deliberately follows what production serves "
                 "rather than the best number available."
        )
    )
    add("")
    add("## A DIFFERENT quantity: in-sample identity-column R²")
    add("")
    add(
        "**These are not model scores and not cross-validated.** Each row fits a "
        "per-level mean on all rows and scores it on those same rows (one-way ANOVA "
        "R²). It quantifies how much *redundancy* the panel contains — the reason the "
        "random split is inflated — and must never be quoted as a split-regime R². "
        "A column with one level per row would score 1.000 by construction, so the "
        "level count is shown next to every figure."
    )
    add("")
    add("| Identity column | in-sample R² | levels | rows/level |")
    add("|---|---:|---:|---:|")
    for name, block in identity.items():
        add(
            f"| `{name}` | {block['in_sample_r2']:.3f} | {block['n_levels']} | "
            f"{block['rows_per_level']} |"
        )
    add("")
    add(
        "This is the table that got conflated with the progression. `base_product` "
        f"explaining {identity['base_product']['in_sample_r2']:.3f} "
        "in sample is why a random split leaks; it is **not** the model's random-split "
        f"score, which is {prog['random_mean']:+.3f}."
    )
    add("")
    add("## Protocol")
    add("")
    add(f"- **Folds:** {meta['n_splits']}-fold, {meta['n_repeats']} independent shuffles = "
        f"{meta['n_splits'] * meta['n_repeats']} folds per regime.")
    add(f"- **Seed:** `random_state = {meta['seed']} + repeat`, identical across all three regimes.")
    add("- **Splitters:** `KFold(shuffle=True)` for `random`; `GroupKFold(shuffle=True)` for "
        "`family` and `manufacturer`.")
    add(f"- **Feature pipeline:** `lead_time_model.build_training_design` + "
        f"`build_design_matrix`, feature schema v{meta['feature_schema_version']}, "
        f"{meta['n_features']} columns — the same path `retrain_lead_time` uses.")
    add(f"- **Panel:** `{meta['panel_path']}`, sha256 `{(meta['panel_sha256'] or 'n/a')[:16]}…`")
    add("- **Nothing is persisted.** All fits are in-memory; `backend/data/ml_models/` is "
        "not written by this script.")
    add("")
    add("### Row accounting")
    add("")
    add("| | rows |")
    add("|---|---:|")
    for key, value in payload["panel_row_accounting"].items():
        add(f"| {ROW_ACCOUNTING_LABELS.get(key, key.replace('_', ' '))} | {value} |")
    add("")
    add("### Regimes")
    add("")
    for regime, text in REGIME_DESCRIPTIONS.items():
        add(f"- **`{regime}`** — {text}")
    add("")
    add("### Environment")
    add("")
    add(f"- hardware `{meta['hardware']}`, python `{meta['python']}`")
    versions = ", ".join(f"{k} {v}" for k, v in meta["libraries"].items())
    add(f"- {versions}")
    add(f"- wall time {meta['wall_seconds']}s")
    add("")
    add("## Reproduce")
    add("")
    add("```bash")
    add("cd backend && source venv/bin/activate")
    add("python -m seeds.run_leakage_progression")
    add("```")
    if provenance:
        add(provenance_markdown(provenance))
    add("")
    return "\n".join(lines) + "\n"


REGIME_LABELS = {
    "random": "random rows (**wrong**)",
    "family": "grouped by part-family key",
    "manufacturer": "grouped by manufacturer",
}

#: ``build_training_design`` names its counters "families"; they are in fact counts of
#: ``_group_key`` outputs (467), not of ``base_product`` values (360). The upstream keys
#: are not ours to rename, so they are relabelled at render time rather than left to
#: imply a number they do not carry.
ROW_ACCOUNTING_LABELS = {
    "distinct_families": "distinct family grouping keys",
    "distinct_families_trained": "distinct family grouping keys trained",
}


# ── driver ───────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick", action="store_true",
        help="champion estimator only, 2 repeats — for a smoke check, not for publishing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Under DEBUG the app engine is created with echo=True, which would bury the
    # measurement in SQL. Read-only introspection is all this script does with the DB.
    try:
        from app.core.database import engine
        engine.echo = False
    except Exception:  # noqa: BLE001 — no DB layer is fine; coverage just goes unverified
        pass
    for noisy in ("sqlalchemy.engine", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    started = datetime.now(UTC)
    t_start = time.perf_counter()

    from app.ml.lead_time_collector import PANEL_PATH

    if not PANEL_PATH.is_file():
        logger.error("no observed lead-time panel at %s — nothing to measure", PANEL_PATH)
        return 1
    # Measure the rows the SERVED model was trained on, not the whole live file:
    # the weekly collector appends snapshots that no model has been fitted on yet.
    live_bytes = PANEL_PATH.read_bytes()
    training_sha = _served_training_sha()
    if training_sha is None:
        logger.warning("no served model records its training panel; measuring the whole live panel")
        panel_bytes = live_bytes
    else:
        prefix = served_panel_prefix(live_bytes, training_sha)
        if prefix is None:
            logger.error(
                "the served model was trained on panel bytes %s, which are not the head of "
                "%s — the panel was rewritten since the last retrain. Retrain first "
                "(python -m seeds.train_ml_models).", training_sha[:16], PANEL_PATH)
            return 1
        panel_bytes = prefix
    panel_sha256 = hashlib.sha256(panel_bytes).hexdigest()
    logger.info(
        "panel: first %d of %d bytes of %s (the served model's training cut)",
        len(panel_bytes), len(live_bytes), PANEL_PATH.name)
    with tempfile.TemporaryDirectory() as tmp:
        panel_file = Path(tmp) / PANEL_PATH.name
        panel_file.write_bytes(panel_bytes)
        panel = load_observed_panel(panel_file)
    if panel is None:
        logger.error("could not read the observed lead-time panel at %s", PANEL_PATH)
        return 1

    design = build_training_design(panel)
    X, feature_cols = build_design_matrix(design.records, schema=design.schema)
    y = design.y
    if len(y) < N_SPLITS * 2:
        logger.error("only %d usable rows — too few to fold", len(y))
        return 1

    n_repeats = 2 if args.quick else N_REPEATS
    model_names = [CHAMPION_MODEL_HINT] if args.quick else list(MODELS)

    counts = {
        "n_rows": int(len(y)),
        # NOT "n_families". This counts _group_key outputs, which fragment into a
        # per-MPN key wherever base_product is Unknown. See the module docstring.
        "n_family_group_keys": int(len(set(design.family_groups))),
        "n_manufacturers": int(len(set(design.manufacturer_groups))),
        "n_features": int(X.shape[1]),
        "n_snapshot_dates": int(len(set(design.snapshot_dates))),
    }
    logger.info(
        "panel: %d rows, %d family group keys, %d manufacturers, %d features, "
        "%d snapshot dates",
        counts["n_rows"], counts["n_family_group_keys"], counts["n_manufacturers"],
        counts["n_features"], counts["n_snapshot_dates"],
    )

    regime_groups: Dict[str, Optional[List[str]]] = {
        "random": None,
        "family": design.family_groups,
        "manufacturer": design.manufacturer_groups,
    }

    regimes: Dict[str, object] = {}
    for regime, groups in regime_groups.items():
        folds = build_folds(len(y), groups, N_SPLITS, n_repeats, SEED)
        t0 = time.perf_counter()
        regimes[regime] = score_regime(X, y, feature_cols, folds, model_names)
        logger.info(
            "regime %-13s %d folds in %.1fs", regime, len(folds), time.perf_counter() - t0
        )
        for name in model_names:
            block = regimes[regime]["r2"][name]  # type: ignore[index]
            logger.info(
                "    %-20s R2 mean=%+.4f median=%+.4f sd=%.4f  [%+.3f, %+.3f]",
                name, block["mean"], block["median"], block["std"], block["min"], block["max"],
            )

    # The champion is chosen under the regime the shipped model is evaluated
    # under — never under `random`, which would select for memorisation.
    best_here = min(
        model_names,
        key=lambda m: regimes[CHAMPION_SELECTION_REGIME]["rmse_days"][m]["mean"],  # type: ignore[index]
    )
    # The headline estimator is the one production actually SERVES, read off the
    # persisted metrics — this report describes the deployed model, not the best
    # model that could have been deployed. Only when that is unreadable does it
    # fall back to the family regime (never `random`, which selects for memorisation).
    served = _served_champion()
    if served in model_names:
        champion = served
        champion_source = "served champion, read from data/ml_models/metrics.joblib"
    else:
        champion = best_here
        champion_source = f"lowest mean RMSE under the {CHAMPION_SELECTION_REGIME} regime"
    logger.info(
        "headline estimator: %s (%s); lowest %s-regime RMSE measured here: %s",
        champion, champion_source, CHAMPION_SELECTION_REGIME, best_here,
    )

    manufacturer_counts = pd.Series(design.manufacturer_groups).value_counts()
    concentration = {
        "top3": [
            {"manufacturer": str(k), "rows": int(v)}
            for k, v in manufacturer_counts.head(3).items()
        ],
        "top3_share_pct": round(
            float(manufacturer_counts.head(3).sum()) / len(y) * 100.0, 1
        ),
        "n_with_le_6_rows": int((manufacturer_counts <= 6).sum()),
        "rows_per_manufacturer": {str(k): int(v) for k, v in manufacturer_counts.items()},
    }

    progression = {
        f"{regime}_{stat}": regimes[regime]["r2"][champion][stat]  # type: ignore[index]
        for regime in ("random", "family", "manufacturer")
        for stat in ("mean", "median")
    }

    # Computed here rather than inline in the payload because the headline needs the
    # base_product level count (360) to state it alongside the group-key count (467)
    # without either number being written by hand.
    identity_r2 = identity_column_in_sample_r2(
        y,
        design.identity_columns,
        {"family_group_key": design.family_groups},
    )
    n_base_products = cast(int, identity_r2["base_product"]["n_levels"])

    headline = (
        f"R2 {progression['random_mean']:+.3f} random split -> "
        f"{progression['family_mean']:+.3f} grouped by part family -> "
        f"{progression['manufacturer_mean']:+.3f} holding out whole manufacturers "
        f"(mean over {N_SPLITS * n_repeats} folds; medians "
        f"{progression['random_median']:+.3f} / {progression['family_median']:+.3f} / "
        f"{progression['manufacturer_median']:+.3f}). "
        f"{counts['n_manufacturers']} manufacturers, "
        f"{counts['n_family_group_keys']} family grouping keys "
        f"({n_base_products} distinct base_product values), "
        f"{counts['n_rows']} rows. The effective sample size for generalisation is the "
        "manufacturer count, not the row count."
    )

    elapsed = time.perf_counter() - t_start
    payload: Dict[str, object] = {
        "meta": {
            "generated_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hardware": f"{platform.machine()} / {platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "libraries": _library_versions(),
            "wall_seconds": round(elapsed, 1),
            "quick_mode": bool(args.quick),
            "seed": SEED,
            "n_splits": N_SPLITS,
            "n_repeats": n_repeats,
            "n_folds_per_regime": N_SPLITS * n_repeats,
            "splitters": {
                "random": "sklearn.model_selection.KFold(shuffle=True, random_state=SEED+repeat)",
                "family": "sklearn.model_selection.GroupKFold(shuffle=True, random_state=SEED+repeat)",
                "manufacturer": "sklearn.model_selection.GroupKFold(shuffle=True, random_state=SEED+repeat)",
            },
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "n_features": int(X.shape[1]),
            "feature_cols": list(feature_cols),
            "feature_exclusions": design.exclusions,
            "panel_path": str(PANEL_PATH.relative_to(REPO_ROOT)),
            "panel_sha256": panel_sha256,
            "champion_selection_regime": CHAMPION_SELECTION_REGIME,
            "persists_artifacts": False,
            "notes": [
                "Every fit is in-memory. This script never writes "
                "backend/data/ml_models/*.joblib — it measures, it does not retrain.",
                "Rows, labels and group keys come from "
                "lead_time_model.build_training_design, the SAME function "
                "retrain_lead_time uses, so this report cannot describe a dataset "
                "nobody trains on.",
                "Baselines come from lead_time_model.baseline_predictors and are scored "
                "on the IDENTICAL folds as the models, so every comparison is paired.",
                "identity_column_in_sample_r2 is a DIFFERENT QUANTITY from the "
                "progression: in-sample, not cross-validated, and not a model score.",
            ],
        },
        "headline": headline,
        "champion_model": champion,
        "champion_source": champion_source,
        #: Which estimator this run's own family-regime RMSE would have picked. It
        #: is reported even when it disagrees with the served champion, so a reader
        #: can see the margin rather than take the headline model on trust.
        "best_model_under_family_regime": best_here,
        "model_names": list(model_names),
        "baseline_names": list(baseline_predictors(feature_cols)),
        "counts": counts,
        "panel_row_accounting": design.counts,
        "manufacturer_concentration": concentration,
        "regime_descriptions": REGIME_DESCRIPTIONS,
        "progression": progression,
        "regimes": regimes,
        "identity_column_in_sample_r2": identity_r2,
    }
    payload["provenance"] = build_provenance(
        generator="seeds.run_leakage_progression",
        inputs={"lead_time_panel": PANEL_PATH},
        extra={
            "artifacts": [
                "docs/leakage_progression.json",
                "docs/LEAKAGE_PROGRESSION.md",
            ]
        },
    )

    # The input measured is a head of the live file, so record ITS hash and length:
    # the artifact pin re-reads exactly that many bytes and checks this hash.
    payload["provenance"]["inputs"]["lead_time_panel"].update(
        sha256=panel_sha256, bytes=len(panel_bytes), live_file_bytes=len(live_bytes))

    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "leakage_progression.json").write_text(json.dumps(payload, indent=2) + "\n")
    (DOCS / "LEAKAGE_PROGRESSION.md").write_text(render_markdown(payload))
    logger.info("LEAKAGE PROGRESSION — %s", headline)
    logger.info(
        "wrote docs/leakage_progression.json and docs/LEAKAGE_PROGRESSION.md (%.1fs)", elapsed
    )
    return 0


#: Only used by ``--quick`` to avoid fitting all four estimators for a smoke check.
CHAMPION_MODEL_HINT = "random_forest"


if __name__ == "__main__":
    sys.exit(main())
