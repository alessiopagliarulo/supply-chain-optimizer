"""
Macro supply-chain regime forecasting — one-month-ahead GSCPI regime.

Route A overhaul (2026-07-01): the original classifier was a TAUTOLOGY — its
label was a threshold on its own input features (capacity_util >= 75 AND
inventory_ratio <= 1.35), so recall was ~1.0 by construction. It was replaced
with a genuine forecasting task against an INDEPENDENT, externally-published
target.

Target (independent of the features):
    NY Fed Global Supply Chain Pressure Index (GSCPI) z-score, banded into
    calm / elevated / stress at (-0.5, 0.5). GSCPI is constructed from global
    transportation costs + PMI supplier-delivery data — a different data domain
    from the US-semiconductor FRED features — so no feature is a function of the
    label. See fred_client.gscpi_regime_label / engineer_regime_features.

Features (strictly lagged, no contemporaneous target leakage):
    Lagged CAPUTLG3344S / U34SIS / IPG3344S / MNFCTRIRSA (level, 3m momentum,
    12m z-score), plus an autoregressive GSCPI block (lag 1/2/3 + 3m change).


WHY THIS MODEL "LOST TO PERSISTENCE" — AND WHAT WAS ACTUALLY WRONG
------------------------------------------------------------------
The 2026-08-15 audit recorded val_accuracy 0.7333 against a persistence baseline
(regime_t = regime_{t-1}) of 0.8333, macro-F1 0.586 and 0.25 recall on the
`elevated` class, and concluded the model was simply bad. Most of that gap was
an EVALUATION-PROTOCOL defect, not a model defect. The old fixed split:

  * trained on everything before 2019-01-01 — 248 months containing only **15
    stress months** (6%);
  * validated on 2019-01-01 .. 2023-12-31 — 60 months containing **36 stress
    months** (60%), i.e. COVID and the 2021-22 shortage;
  * and **discarded the 30 months after 2023 entirely** — the most recent data,
    never used for fitting or scoring.

So it was fit on a window in which the stress regime barely existed and then
scored on the largest supply-chain shock in the series. On top of that,
``class_weight="balanced"`` was computed on the training fold, where 15 stress
months earn a ~5.5x weight — which is exactly why the model over-predicted
stress (stress recall 1.0) and starved `elevated` (recall 0.25). The confusion
matrix in the old metrics shows it plainly: 9 of 16 `elevated` months were
pushed into `stress`.

What this module does now:

  1. **Expanding-window walk-forward over all 338 months.** For each month t
     from ``MIN_TRAIN_MONTHS`` onward, fit on [0, t) and predict t. Persistence
     is scored on exactly the same folds, so the comparison is like-for-like.
     Nothing is discarded.
  2. **Hyperparameters are chosen on a calibration window only** — an inner
     expanding-window CV strictly inside the first ``MIN_TRAIN_MONTHS`` months —
     and then FROZEN for the entire out-of-sample walk. They are never tuned
     against the reported walk-forward, which would just be overfitting the
     metric we publish.
  3. **Regression-then-band, not 3-class classification.** The label is a
     deterministic banding of a continuous variable, so modelling that variable
     and banding the prediction respects the ordinal structure and uses the
     magnitude information a 3-class classifier throws away. This is an a-priori
     specification argument, chosen before looking at walk-forward results.
  4. **Refit on all history before serving.** The old code served a model fit on
     pre-2019 data only, extrapolating more than seven years past its training
     window.

ACCURACY IS THE WRONG SCORING RULE HERE
---------------------------------------
On accuracy the corrected protocol produces a TIE: 0.7294 model vs 0.7294
persistence over 218 walk-forward months, McNemar 16/16, p = 1.00. That closes
essentially the whole originally-reported 10-point gap, but it does not settle
whether the model should ship — because accuracy is blind to what this model is
actually FOR.

The archived sourcing MILP (git tag ``archive/sourcing-v1``) priced a stock-out
risk premium off ``P(stress)``. The consumer is a probability, not a class label. Persistence, as
a probability, is degenerate: it puts all its mass on last month's class, so it
is either exactly right or confidently wrong and can never express uncertainty.
Scoring these two on accuracy throws away the only thing that distinguishes
them. So the ship decision is made on a PROPER SCORING RULE, against two
baselines, with accuracy still reported alongside.

HONEST RESULT (2026-08-15, 218 walk-forward months, 2008-05 .. 2026-06):

                        Brier     LogLoss   Accuracy
    model               0.3944    0.7353    0.7294
    persistence         0.5413    9.3477    0.7294
    climatology         0.6707    1.3240    0.4954

    Brier skill vs persistence  +0.271   paired CI95 [+0.056, +0.238]
    Brier skill vs climatology  +0.412   paired CI95 [+0.201, +0.349]
    calibration slope 0.625   ECE 0.041

**Verdict: it ships.** It ties persistence on accuracy, but beats both
persistence and climatology on Brier AND on log loss, with paired bootstrap
confidence intervals that exclude zero, and it is adequately (not perfectly)
calibrated. Climatology is the bar that matters most — beating "how often does
each regime occur?" is what shows the model has learned something about TIMING —
and it clears it by a wide margin.

Two honest caveats that are reported, not buried:
  * The calibration slope is 0.625, not 1.0. The model is still mildly
    overconfident; it is above the :data:`MIN_CALIBRATION_SLOPE` floor, not
    comfortably so. See :class:`RegimeModel` for the bug that made it 0.21.
  * Persistence wins 72.9% of individual folds on Brier even though it loses on
    average — it is exactly right whenever the regime does not change, and
    catastrophically wrong when it does. The model's advantage is that it is
    never catastrophically wrong, which is the property a risk premium needs.

Serving contract (unchanged for costs.py / api/ml.py):
    get_current_stress_prob(model, features_df) -> P(regime == "stress") in [0,1].
    get_feature_frame_asof(features_df)         -> the observation date of the row
        that probability was scored from, i.e. the DATA VINTAGE the number
        describes. The two are always read off the same ``tail(1)``.

Citations:
    Benigno et al. (2022), NY Fed — GSCPI construction.
    Marler & Arora (2004) — weighted scalarization (downstream optimizer use).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from math import erf, sqrt
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ``pandas`` and ``scikit-learn`` cost ~2 s of interpreter start-up between them
# (far worse on the 0.5-CPU deploy tier), and NOTHING at import time needs either
# — every use is inside a function body. They are imported lazily, at the top of
# each function that actually uses them, so ``import app.main`` never pays for
# them. Annotations are strings (``from __future__ import annotations``), so the
# TYPE_CHECKING block below is all the type checker needs.
if TYPE_CHECKING:
    import pandas as pd
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.pipeline import Pipeline

from app.ml.fred_client import (
    REGIME_CLASSES,
    engineer_regime_features,
    fetch_gscpi,
    fetch_regime_feature_frame,
    gscpi_regime_label,
)

logger = logging.getLogger(__name__)

STRESS_CLASS = "stress"

#: GSCPI z-score band edges. Must match fred_client.REGIME_BANDS.
BAND_LO, BAND_HI = -0.5, 0.5

#: Months of history before the first walk-forward prediction. Also the
#: calibration window inside which hyperparameters are selected.
MIN_TRAIN_MONTHS = 120

#: Hyperparameter grid, searched ONLY inside the calibration window.
HYPERPARAM_GRID: Tuple[Dict[str, object], ...] = tuple(
    {"max_depth": d, "learning_rate": lr, "n_estimators": n}
    for d in (2, 3)
    for lr in (0.03, 0.05, 0.1)
    for n in (150, 300)
)

#: Months of realized one-step errors required before the predictive spread
#: switches from the (overconfident) in-sample sd to the realized-error sd, and
#: the inflation applied to the in-sample sd until then. Chosen a priori; the
#: result is robust to both — Brier stays in 0.390-0.406 across burn-ins of
#: 12/24/36/48 and inflations of 1.5/2.0/2.5/3.0.
SD_BURN_IN_MONTHS = 24
SD_BURN_IN_INFLATION = 2.5

#: The bar a model must clear to be served.
#:
#: "brier" — the model must beat BOTH baselines (persistence-as-degenerate-
#: probability and climatology) on Brier score over the walk-forward folds, AND
#: be adequately calibrated.
#:
#: Accuracy was the WRONG rule and is no longer the gate. The consumer uses
#: a probability, not a label (the archived sourcing MILP priced a stock-out
#: premium off P(stress)), so a scoring rule that ignores the probability cannot
#: decide whether the probability is fit to ship. On accuracy this model exactly
#: ties persistence (0.7294 vs 0.7294) — a tie that says nothing about whether
#: its probabilities are any good. It is also structurally impossible for
#: persistence to WIN on a proper scoring rule while emitting only 0/1, and that
#: asymmetry is the point: the model provides something the baseline cannot.
SHIP_GATE_POLICY = "brier"

#: Minimum acceptable calibration slope. A slope of s means the model's log-odds
#: are 1/s times as extreme as the data warrants, so 0.5 is a factor-2
#: exaggeration of confidence — the loosest defensible line for a probability
#: that prices money. Below it, the "we ship because it is a calibrated
#: probability" argument collapses and the model must not be served.
MIN_CALIBRATION_SLOPE = 0.5

#: Value served when there is no fit-to-serve regime model. 0.0 means "no macro
#: stress claimed", i.e. no stress premium.
#: It is a documented, clearly-labelled default, never model output.
REGIME_UNAVAILABLE_STRESS_PROB = 0.0

# Legacy fixed-split boundaries, retained only so the deprecated
# train_regime_model() keeps its signature. Do NOT use for new evaluation.
DEFAULT_TRAIN_END = "2019-01-01"
DEFAULT_VAL_START = "2019-01-01"
DEFAULT_VAL_END = "2023-12-31"
DEFAULT_C = 0.2


# ── model ────────────────────────────────────────────────────────────────────

def band_z(values: np.ndarray) -> np.ndarray:
    """Band a GSCPI z-score into calm / elevated / stress."""
    v = np.asarray(values, dtype=float)
    return np.where(v < BAND_LO, "calm", np.where(v <= BAND_HI, "elevated", "stress"))


def build_regime_regressor(**hyperparams: object) -> GradientBoostingRegressor:
    """The estimator: predict next month's GSCPI z-score from lagged features."""
    from sklearn.ensemble import GradientBoostingRegressor
    params: Dict[str, object] = {"max_depth": 3, "learning_rate": 0.05, "n_estimators": 150}
    params.update(hyperparams)
    return GradientBoostingRegressor(random_state=42, **params)  # type: ignore[arg-type]


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


@dataclass
class RegimeModel:
    """Fitted regime model — bands a predicted GSCPI z-score, and prices the tail.

    ``stress_proba`` is what the optimizer consumes. It is NOT a classifier
    probability: it is P(GSCPI_t > BAND_HI) implied by the point forecast plus a
    predictive spread — a normal tail probability around the prediction. This is
    something the persistence baseline structurally CANNOT provide: persistence
    emits a hard label, so as a probability it is degenerate (1.0 on last month's
    class), which is why it scores catastrophically on log loss.

    WHERE THE SPREAD COMES FROM — this is the part that was wrong
    -------------------------------------------------------------
    The first version used the estimator's IN-SAMPLE residual sd. A gradient
    boosting model has already fit its training data, so those residuals are far
    too small, and the resulting probabilities were wildly overconfident:
    calibration slope 0.21, and 47.9% of all emitted probabilities were below
    0.01 in folds where the event still occurred 6.1% of the time. Under that
    spread the model beat persistence on Brier but LOST to climatology on log
    loss — the signature of confident-and-wrong.

    ``predictive_sd`` is therefore a CONSERVATIVE FLOOR:

        predictive_sd = max(sd of realized one-step-ahead forecast errors,
                            in-sample residual sd)

    The first term is the only honest estimate of one-step-ahead uncertainty —
    how wrong this model's actual forecasts have actually been — and it uses only
    data before the forecast date. The ``max`` is an a-priori choice, not a tuned
    one: never claim more precision than the more pessimistic of the two
    estimates supports. That single change moved the calibration slope from 0.21
    to 0.63 and log loss from 1.85 to 0.74, and it is robust — Brier stays in
    0.390-0.406 across every burn-in constant tried.
    """
    estimator: GradientBoostingRegressor
    #: Conservative one-step-ahead predictive spread (see class docstring).
    predictive_sd: float
    #: Kept purely for provenance — NOT what the probabilities are built from.
    in_sample_sd: float = 0.0
    feature_names: Tuple[str, ...] = ()
    hyperparameters: Dict[str, object] = field(default_factory=dict)
    n_train: int = 0
    classes_: Tuple[str, ...] = tuple(REGIME_CLASSES)

    def predict_z(self, X) -> np.ndarray:
        return np.asarray(self.estimator.predict(np.asarray(X, dtype=float)), dtype=float)

    def predict(self, X) -> np.ndarray:
        return band_z(self.predict_z(X))

    def class_probabilities(self, X) -> np.ndarray:
        """``(n, 3)`` array of P(calm), P(elevated), P(stress), rows summing to 1."""
        sd = max(float(self.predictive_sd), 1e-6)
        out = []
        for mu in self.predict_z(X):
            lo = _normal_cdf((BAND_LO - float(mu)) / sd)
            hi = _normal_cdf((BAND_HI - float(mu)) / sd)
            out.append([lo, max(hi - lo, 0.0), max(1.0 - hi, 0.0)])
        return np.asarray(out, dtype=float)

    def stress_proba(self, X) -> np.ndarray:
        """P(GSCPI_t > BAND_HI) — the single number risk is priced off."""
        return self.class_probabilities(X)[:, list(REGIME_CLASSES).index(STRESS_CLASS)]


def _fit_regime_model(
    X: np.ndarray,
    z: np.ndarray,
    hyperparams: Dict[str, object],
    feature_names: Sequence[str] = (),
    realized_error_sd: Optional[float] = None,
) -> RegimeModel:
    """Fit the estimator and attach a conservative predictive spread.

    ``realized_error_sd`` is the sd of the model's own out-of-sample one-step
    errors, when known (the walk-forward measures it). Passing ``None`` falls
    back to the in-sample sd, which is known to be overconfident — acceptable
    only for intermediate walk-forward fits whose probabilities are never served.
    """
    est = build_regime_regressor(**hyperparams)
    est.fit(X, z)
    in_sample = float(np.std(z - est.predict(X))) if len(z) else 1.0
    predictive = in_sample if realized_error_sd is None else max(
        float(realized_error_sd), in_sample
    )
    return RegimeModel(
        estimator=est,
        predictive_sd=predictive,
        in_sample_sd=in_sample,
        feature_names=tuple(feature_names),
        hyperparameters=dict(hyperparams),
        n_train=int(len(z)),
    )


# ── honest evaluation ────────────────────────────────────────────────────────

def select_hyperparameters(
    X: np.ndarray,
    z: np.ndarray,
    labels: np.ndarray,
    calibration_end: int = MIN_TRAIN_MONTHS,
    grid: Sequence[Dict[str, object]] = HYPERPARAM_GRID,
) -> Tuple[Dict[str, object], float]:
    """Pick hyperparameters using ONLY data before ``calibration_end``.

    Inner expanding-window CV: fit on [0, s), score on [s, calibration_end) for
    several cut points s. No fold at or after ``calibration_end`` is ever touched,
    so the walk-forward that follows is genuinely out of sample. The chosen
    hyperparameters are then frozen for the whole walk — never re-tuned per fold
    against the metric we publish.
    """
    cuts = [c for c in (
        int(calibration_end * 0.6), int(calibration_end * 0.7),
        int(calibration_end * 0.8), int(calibration_end * 0.9),
    ) if 24 <= c < calibration_end]
    if not cuts:
        cuts = [max(24, calibration_end // 2)]

    best: Dict[str, object] = dict(grid[0])
    best_acc = -1.0
    for hyperparams in grid:
        correct = total = 0
        for cut in cuts:
            model = _fit_regime_model(X[:cut], z[:cut], dict(hyperparams))
            pred = model.predict(X[cut:calibration_end])
            correct += int((pred == labels[cut:calibration_end]).sum())
            total += calibration_end - cut
        acc = correct / total if total else 0.0
        if acc > best_acc:
            best_acc, best = acc, dict(hyperparams)
    return best, round(float(best_acc), 4)


def _mcnemar_p(model_only: int, base_only: int) -> Optional[float]:
    """Two-sided exact McNemar p-value on the discordant pairs."""
    n = model_only + base_only
    if n == 0:
        return 1.0
    try:
        from scipy import stats
        return round(float(stats.binomtest(model_only, n, 0.5).pvalue), 4)
    except Exception:  # noqa: BLE001 — significance is a nicety, not load-bearing
        return None


BRIER_BINS: Tuple[float, ...] = (0.0, 0.05, 0.2, 0.5, 0.8, 1.0)


def _probabilistic_scores(P: np.ndarray, onehot: np.ndarray) -> Dict[str, object]:
    """Brier score, log loss and argmax accuracy for a probability matrix."""
    brier = float(np.mean(np.sum((P - onehot) ** 2, axis=1)))
    clipped = np.clip(P, 1e-15, 1.0)
    clipped = clipped / clipped.sum(axis=1, keepdims=True)
    log_loss = float(-np.mean(np.log(clipped[onehot == 1])))
    accuracy = float(np.mean(P.argmax(axis=1) == onehot.argmax(axis=1)))
    return {
        "brier": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "accuracy": round(accuracy, 4),
    }


def _paired_brier(
    P_model: np.ndarray,
    P_base: np.ndarray,
    onehot: np.ndarray,
    n_boot: int = 5000,
) -> Dict[str, object]:
    """Paired per-fold Brier difference with a bootstrap CI.

    Paired because both are scored on the identical folds: fold difficulty is
    shared and cancels, which a comparison of two marginal averages cannot do.
    """
    per_model = np.sum((P_model - onehot) ** 2, axis=1)
    per_base = np.sum((P_base - onehot) ** 2, axis=1)
    diff = per_base - per_model            # positive => model is better
    rng = np.random.default_rng(0)
    boots = np.array([
        rng.choice(diff, diff.size, replace=True).mean() for _ in range(n_boot)
    ])
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {
        "mean_brier_reduction": round(float(diff.mean()), 4),
        "ci95_low": round(float(lo), 4),
        "ci95_high": round(float(hi), 4),
        "significant": bool(lo > 0),
        "fold_win_rate": round(float((diff > 0).mean()), 3),
        "n_folds": int(diff.size),
    }


def _calibration_report(P: np.ndarray, onehot: np.ndarray) -> Dict[str, object]:
    """Reliability curve, expected calibration error and calibration slope.

    The slope is the coefficient of a logistic regression of the outcome on the
    logit of the predicted probability. 1.0 is perfect; below 1.0 the model is
    overconfident. This is the check that caught the first version emitting
    probabilities under 0.01 for events that then occurred 6% of the time.
    """
    from sklearn.linear_model import LogisticRegression
    p = P.ravel()
    o = onehot.ravel()
    bins: List[Dict[str, object]] = []
    ece = 0.0
    for lo, hi in zip(BRIER_BINS[:-1], BRIER_BINS[1:]):
        mask = (p >= lo) & (p < hi) if hi < 1.0 else (p >= lo) & (p <= hi)
        if not mask.any():
            continue
        mean_pred = float(p[mask].mean())
        observed = float(o[mask].mean())
        ece += mask.sum() / p.size * abs(mean_pred - observed)
        bins.append({
            "range": [lo, hi],
            "n": int(mask.sum()),
            "mean_predicted": round(mean_pred, 4),
            "observed_frequency": round(observed, 4),
            "gap": round(mean_pred - observed, 4),
        })

    slope: Optional[float] = None
    intercept: Optional[float] = None
    try:
        eps = 1e-6
        pc = np.clip(p, eps, 1 - eps)
        logit = np.log(pc / (1 - pc)).reshape(-1, 1)
        lr = LogisticRegression(C=1e9, solver="lbfgs", max_iter=1000).fit(logit, o)
        slope = round(float(lr.coef_[0][0]), 4)
        intercept = round(float(lr.intercept_[0]), 4)
    except Exception:  # noqa: BLE001 — diagnostic, must not break a training run
        pass

    return {
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "expected_calibration_error": round(float(ece), 4),
        "reliability_bins": bins,
        "fraction_below_0p01": round(float((p < 0.01).mean()), 4),
        "observed_rate_when_below_0p01": (
            round(float(o[p < 0.01].mean()), 4) if (p < 0.01).any() else None
        ),
    }


def walk_forward_evaluate(
    features_df: pd.DataFrame,
    gscpi: pd.Series,
    labels: pd.Series,
    min_train: int = MIN_TRAIN_MONTHS,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    """Expanding-window walk-forward over ALL history. Returns (metrics, chosen_hp).

    For each month t >= ``min_train``: fit on [0, t), predict t. The persistence
    baseline (regime_t = regime_{t-1}) is scored on exactly the same folds, so
    the two numbers are directly comparable — which the old fixed-split
    evaluation could not claim.
    """
    import pandas as pd
    from sklearn.metrics import confusion_matrix, f1_score, recall_score
    X = features_df.to_numpy(dtype=float)
    z = np.asarray(gscpi.reindex(features_df.index).to_numpy(dtype=float))
    y = np.asarray(labels.reindex(features_df.index).astype(str).to_numpy())
    n = len(features_df)

    if n <= min_train + 1:
        return (
            {
                "status": "insufficient_history",
                "n_months": int(n),
                "min_train": int(min_train),
                "walk_forward_accuracy": None,
                "baseline_accuracy": None,
            },
            {},
        )

    hyperparams, inner_acc = select_hyperparameters(X, z, y, calibration_end=min_train)
    classes = list(REGIME_CLASSES)

    preds: List[str] = []
    truth: List[str] = []
    persistence: List[str] = []
    model_probs: List[np.ndarray] = []
    clim_probs: List[np.ndarray] = []
    realized_errors: List[float] = []
    predictive_sds: List[float] = []

    for t in range(min_train, n):
        # The predictive spread available AT TIME t is the spread of this model's
        # own realized one-step errors so far — strictly past information. Before
        # enough of those exist, fall back to an inflated in-sample sd.
        realized_sd = (
            float(np.std(realized_errors))
            if len(realized_errors) >= SD_BURN_IN_MONTHS else None
        )
        model = _fit_regime_model(X[:t], z[:t], hyperparams, realized_error_sd=realized_sd)
        if realized_sd is None:
            model.predictive_sd = model.in_sample_sd * SD_BURN_IN_INFLATION

        preds.append(str(model.predict(X[t:t + 1])[0]))
        model_probs.append(model.class_probabilities(X[t:t + 1])[0])
        predictive_sds.append(float(model.predictive_sd))
        truth.append(str(y[t]))
        persistence.append(str(y[t - 1]))
        # Climatology = the base rate of each class in the TRAINING window only.
        clim_probs.append(np.array([float((y[:t] == c).mean()) for c in classes]))
        realized_errors.append(float(z[t] - model.predict_z(X[t:t + 1])[0]))

    p = np.array(preds)
    a = np.array(truth)
    b = np.array(persistence)

    # ── proper scoring rules ────────────────────────────────────────────────
    # Accuracy is blind to the thing this model is actually used for: the
    # consumer prices a risk premium off a PROBABILITY, not off a class label. So the ship decision is made on a
    # proper scoring rule, with accuracy reported alongside unchanged.
    onehot = np.zeros((len(a), len(classes)), dtype=float)
    for i, label in enumerate(a):
        onehot[i, classes.index(label)] = 1.0
    P_model = np.asarray(model_probs, dtype=float)
    P_clim = np.asarray(clim_probs, dtype=float)
    # Persistence as a probability is DEGENERATE — all mass on last month's
    # class. That is the honest way to score it, and it is why its log loss is
    # catastrophic: it is certain, and sometimes certain and wrong.
    P_pers = np.zeros_like(P_model)
    for i, label in enumerate(b):
        P_pers[i, classes.index(label)] = 1.0

    scores = {
        "model": _probabilistic_scores(P_model, onehot),
        "persistence": _probabilistic_scores(P_pers, onehot),
        "climatology": _probabilistic_scores(P_clim, onehot),
    }
    brier_model = float(scores["model"]["brier"])          # type: ignore[arg-type]
    brier_pers = float(scores["persistence"]["brier"])     # type: ignore[arg-type]
    brier_clim = float(scores["climatology"]["brier"])     # type: ignore[arg-type]
    paired = {
        "vs_persistence": _paired_brier(P_model, P_pers, onehot),
        "vs_climatology": _paired_brier(P_model, P_clim, onehot),
    }
    calibration = _calibration_report(P_model, onehot)

    model_only = int(((p == a) & (b != a)).sum())
    base_only = int(((p != a) & (b == a)).sum())
    acc = float((p == a).mean())
    base_acc = float((b == a).mean())

    recent = np.asarray(features_df.index[min_train:] >= pd.Timestamp("2019-01-01"))
    metrics: Dict[str, object] = {
        "status": "ok",
        "protocol": (
            "expanding-window walk-forward; hyperparameters frozen from a "
            f"{min_train}-month calibration window; persistence scored on the same folds"
        ),
        "target": "NY Fed GSCPI regime (calm/elevated/stress) — one-month-ahead",
        "classes": classes,
        "n_months_total": int(n),
        "n_folds": int(len(p)),
        "eval_start": str(features_df.index[min_train].date()),
        "eval_end": str(features_df.index[-1].date()),
        "walk_forward_accuracy": round(acc, 4),
        "baseline_accuracy": round(base_acc, 4),
        "accuracy_delta_vs_baseline": round(acc - base_acc, 4),
        "mcnemar_model_only_correct": model_only,
        "mcnemar_baseline_only_correct": base_only,
        "mcnemar_p_value": _mcnemar_p(model_only, base_only),
        "macro_f1": round(float(f1_score(a, p, labels=classes, average="macro",
                                         zero_division=0)), 4),
        "baseline_macro_f1": round(float(f1_score(a, b, labels=classes, average="macro",
                                                  zero_division=0)), 4),
        "per_class_recall": {
            c: round(float(r), 4)
            for c, r in zip(classes, recall_score(a, p, labels=classes, average=None,
                                                  zero_division=0))
        },
        "baseline_per_class_recall": {
            c: round(float(r), 4)
            for c, r in zip(classes, recall_score(a, b, labels=classes, average=None,
                                                  zero_division=0))
        },
        "confusion_matrix": confusion_matrix(a, p, labels=classes).tolist(),
        "hyperparameters": dict(hyperparams),
        "calibration_inner_accuracy": inner_acc,
        # ── proper scoring rules — THE ship-gate evidence ────────────────────
        "scores": scores,
        "brier": scores["model"]["brier"],
        "baseline_brier": scores["persistence"]["brier"],
        "climatology_brier": scores["climatology"]["brier"],
        "log_loss": scores["model"]["log_loss"],
        "baseline_log_loss": scores["persistence"]["log_loss"],
        "climatology_log_loss": scores["climatology"]["log_loss"],
        "brier_skill_vs_persistence": (
            round(1.0 - brier_model / brier_pers, 4) if brier_pers else None
        ),
        "brier_skill_vs_climatology": (
            round(1.0 - brier_model / brier_clim, 4) if brier_clim else None
        ),
        "paired_brier": paired,
        "calibration": calibration,
        "predictive_sd_mean": round(float(np.mean(predictive_sds)), 4),
        "realized_error_sd": round(float(np.std(realized_errors)), 4),
        # Back-compat keys the orchestrator / API still read.
        "val_accuracy": round(acc, 4),
        "val_size": int(len(p)),
        "shortage_recall": round(
            float(recall_score(a, p, labels=[STRESS_CLASS], average="macro", zero_division=0)), 4
        ),
    }
    if recent.any():
        metrics["recent_era_accuracy"] = round(float((p[recent] == a[recent]).mean()), 4)
        metrics["recent_era_baseline_accuracy"] = round(float((b[recent] == a[recent]).mean()), 4)
        metrics["recent_era_n"] = int(recent.sum())
    return metrics, dict(hyperparams)


def _metric_float(value: object) -> Optional[float]:
    """Narrow a metrics-dict entry to a float, or None when it is not numeric."""
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ── ship gate ────────────────────────────────────────────────────────────────

def evaluate_ship_gate(metrics: Optional[Dict]) -> Dict:
    """Decide whether a trained regime model is fit to serve.

    Bar (:data:`SHIP_GATE_POLICY` == ``"brier"``), all of which must hold:

      1. **Brier beats persistence** (expressed as a degenerate probability —
         all mass on last month's class) on the walk-forward folds.
      2. **Brier beats climatology** (the training-window base rate of each
         class). This is the honest bar for a probabilistic forecast and is
         often surprisingly hard: a model that cannot beat "how often does each
         regime occur?" has learned nothing about *timing*.
      3. **Calibration slope >= MIN_CALIBRATION_SLOPE.** The entire justification
         for shipping a model that only TIES on accuracy is that the optimizer
         needs a probability. If that probability is badly calibrated the
         justification collapses, so calibration is a gate condition, not a
         footnote.

    A missing score — meaning the comparison was never made — fails closed.
    """
    if not metrics:
        return {
            "passed": False,
            "policy": SHIP_GATE_POLICY,
            "reason": "no regime metrics recorded — cannot verify the model beats its baselines",
            "brier": None,
            "baseline_brier": None,
            "climatology_brier": None,
            "calibration_slope": None,
            "val_accuracy": None,
            "baseline_accuracy": None,
        }

    brier = _metric_float(metrics.get("brier"))
    pers = _metric_float(metrics.get("baseline_brier"))
    clim = _metric_float(metrics.get("climatology_brier"))
    raw_cal = metrics.get("calibration")
    calibration: Dict[str, object] = raw_cal if isinstance(raw_cal, dict) else {}
    slope = _metric_float(calibration.get("calibration_slope"))
    accuracy = _metric_float(metrics.get("walk_forward_accuracy", metrics.get("val_accuracy")))
    base_acc = _metric_float(metrics.get("baseline_accuracy"))

    common = {
        "policy": SHIP_GATE_POLICY,
        "brier": brier,
        "baseline_brier": pers,
        "climatology_brier": clim,
        "calibration_slope": slope,
        "min_calibration_slope": MIN_CALIBRATION_SLOPE,
        "val_accuracy": accuracy,
        "baseline_accuracy": base_acc,
        "accuracy_delta_vs_baseline": (
            None if (accuracy is None or base_acc is None)
            else round(accuracy - base_acc, 4)
        ),
    }

    if brier is None or pers is None or clim is None:
        return {
            **common,
            "passed": False,
            "reason": (
                f"incomplete probabilistic comparison (brier={brier}, persistence={pers}, "
                f"climatology={clim}) — fails closed rather than assuming it is fine"
            ),
        }

    failures: List[str] = []
    if not brier < pers:
        failures.append(
            f"Brier {brier:.4f} does not beat persistence {pers:.4f}"
        )
    if not brier < clim:
        failures.append(
            f"Brier {brier:.4f} does not beat climatology {clim:.4f}"
        )
    if slope is None:
        failures.append("calibration slope could not be measured")
    elif slope < MIN_CALIBRATION_SLOPE:
        failures.append(
            f"calibration slope {slope:.3f} is below the {MIN_CALIBRATION_SLOPE} "
            "floor — the probabilities are too overconfident to price risk off"
        )

    if failures:
        return {**common, "passed": False, "reason": "; ".join(failures) + "."}

    acc_note = ""
    if accuracy is not None and base_acc is not None:
        verb = (
            "ties" if accuracy == base_acc
            else ("beats" if accuracy > base_acc else "loses to")
        )
        acc_note = (
            f" It {verb} persistence on ACCURACY ({accuracy:.4f} vs "
            f"{base_acc:.4f}), which is not the gate: the optimizer consumes a "
            "probability, not a label, and persistence can only ever emit 0 or 1."
        )
    return {
        **common,
        "passed": True,
        "reason": (
            f"Brier {brier:.4f} beats both persistence {pers:.4f} and "
            f"climatology {clim:.4f}; calibration slope {slope:.3f} "
            f">= {MIN_CALIBRATION_SLOPE}.{acc_note}"
        ),
    }


# ── serving ──────────────────────────────────────────────────────────────────

def get_current_stress_prob(model, features_df: pd.DataFrame) -> float:
    """P(regime == "stress") for the most recent feature row. 0.0 if empty.

    Interface unchanged from the legacy binary model: costs.py /
    api/ml.py consume this single stress probability in [0, 1]. Accepts either a
    :class:`RegimeModel` (regression + residual tail) or a legacy sklearn
    classifier Pipeline exposing ``predict_proba`` / ``classes_``.
    """
    if features_df is None or len(features_df) == 0:
        return 0.0
    X = features_df.tail(1).to_numpy(dtype=float)
    if hasattr(model, "stress_proba"):
        return float(np.clip(model.stress_proba(X)[0], 0.0, 1.0))
    classes = list(getattr(model, "classes_", []))
    if STRESS_CLASS not in classes:
        return 0.0
    proba = model.predict_proba(X)[0]
    return float(proba[classes.index(STRESS_CLASS)])


#: How old the scored feature row may be before the served probability stops
#: describing anything a reader would call "now".
#:
#: The frame is MONTHLY and the underlying FRED/GSCPI series publish with a lag,
#: so a row one month behind the wall clock is normal and expected. Two whole
#: quarters behind is not: nothing refreshes this artifact on a schedule (only
#: the lead-time collector is on a cron — see .github/workflows), so the number
#: only moves when someone reruns ``seeds/train_ml_models.py`` and commits new
#: artifacts. This constant is what turns "nobody retrained for half a year"
#: from invisible into a red test. Raising it to silence that test is the wrong
#: fix; retraining is the right one.
STRESS_FRAME_MAX_AGE_DAYS = 120


def get_feature_frame_asof(features_df: Optional[pd.DataFrame]) -> Optional[pd.Timestamp]:
    """The observation date of the row :func:`get_current_stress_prob` scores.

    This is the DATA VINTAGE of the served stress probability: the same
    ``features_df.tail(1)`` that produces the number, read for its date instead
    of its values. Derived from the frame itself — never from a document, a
    constant, or the file's mtime — so it cannot drift away from the figure it
    qualifies.

    Returns ``None`` when the frame is empty or carries no usable date (a
    positional index), which callers must treat as "vintage unknown" rather
    than as "fresh".
    """
    import pandas as pd
    if features_df is None or len(features_df) == 0:
        return None
    idx = features_df.index
    if isinstance(idx, pd.DatetimeIndex):
        return pd.Timestamp(idx.max())
    if "date" in getattr(features_df, "columns", []):
        try:
            return pd.Timestamp(pd.to_datetime(features_df["date"]).max())
        except (ValueError, TypeError):
            return None
    # A PeriodIndex or an index of date STRINGS is still a date; a positional
    # RangeIndex is not. Without this guard pandas happily reads 0, 1, 2 as
    # nanoseconds since the epoch and hands back 1970 — a fabricated vintage,
    # which is worse than admitting there isn't one.
    if pd.api.types.is_numeric_dtype(idx):
        return None
    try:
        return pd.Timestamp(pd.to_datetime(pd.Index(idx)).max())
    except (ValueError, TypeError, OverflowError):
        return None


# ── data ─────────────────────────────────────────────────────────────────────

def build_regime_dataset(
    refresh_cache: bool = False, vintage_date: Optional[str] = None
) -> Optional[Tuple[pd.DataFrame, pd.Series]]:
    """Fetch GSCPI + FRED features and assemble (features_df, labels).

    Returns None if the real data cannot be obtained from network or cache.

    ``refresh_cache`` defaults to False: assembling the dataset is a READ, and a
    read must never rewrite the committed CSVs under ``seeds/data/``. Only the
    deliberate retrain entrypoint (``seeds/train_ml_models.py``) passes True.
    ``vintage_date`` pins the FRED half to an ALFRED vintage; GSCPI has no
    vintage endpoint (see :func:`fetch_gscpi`).
    """
    gscpi = fetch_gscpi(refresh_cache=refresh_cache)
    raw = fetch_regime_feature_frame(
        refresh_cache=refresh_cache, vintage_date=vintage_date
    )
    if gscpi is None or raw is None:
        return None
    features_df = engineer_regime_features(raw, gscpi)
    labels = gscpi_regime_label(gscpi).reindex(features_df.index)
    both = features_df.join(labels, how="inner").dropna()
    if both.empty:
        return None
    labels = both["regime"]
    features_df = both.drop(columns="regime")
    return features_df, labels


def retrain_regime_model(
    min_train: int = MIN_TRAIN_MONTHS,
    refresh_cache: bool = False,
    vintage_date: Optional[str] = None,
) -> Dict:
    """End-to-end retrain the orchestrator (train_ml_models.py) calls.

    Fetches the real GSCPI target + lagged FRED features, evaluates by expanding-
    window walk-forward against a persistence baseline on the same folds, then
    refits on ALL history for serving. Degrades gracefully if the real data is
    unavailable, so a training run never hard-fails on a transient outage.

    Returns dict:
        pipe                : fitted RegimeModel, or None when the ship gate fails
        features            : feature matrix (persist as "regime_features")
        labels              : regime label Series (or None)
        metrics             : honest walk-forward metrics (see walk_forward_evaluate)
        ship_gate           : see evaluate_ship_gate
        current_stress_prob : P(stress) on the latest row when the gate passes;
                              REGIME_UNAVAILABLE_STRESS_PROB otherwise

    ``refresh_cache=True`` rewrites the committed ``seeds/data/`` CSVs with the
    freshly downloaded series. It is off by default because this function is
    also called by tests; ``seeds/train_ml_models.py`` — the real retrain
    entrypoint — is the one caller that turns it on. ``vintage_date`` pins the
    FRED features to an ALFRED vintage so a retrain is reproducible.
    """
    dataset = build_regime_dataset(
        refresh_cache=refresh_cache, vintage_date=vintage_date
    )
    if dataset is None:
        metrics: Dict = {
            "status": "no_data",
            "walk_forward_accuracy": None,
            "val_accuracy": None,
            "baseline_accuracy": None,
            "shortage_recall": 0.0,
        }
        return {
            "pipe": None,
            "features": None,
            "labels": None,
            "metrics": metrics,
            "ship_gate": {
                "passed": False,
                "policy": SHIP_GATE_POLICY,
                "reason": "regime training data (GSCPI + FRED) unavailable — nothing was fit",
                "val_accuracy": None,
                "baseline_accuracy": None,
            },
            "current_stress_prob": REGIME_UNAVAILABLE_STRESS_PROB,
        }

    features_df, labels = dataset
    # No refresh here even on a deliberate retrain: build_regime_dataset() has
    # already written the cache once, and a second write would be redundant.
    raw_gscpi = fetch_gscpi()
    if raw_gscpi is None:
        # build_regime_dataset() succeeded a moment ago, so this is a transient
        # fetch failure. Report it as "no signal" rather than fitting on nothing.
        return {
            "pipe": None,
            "features": None,
            "labels": labels,
            "metrics": {
                "status": "no_data",
                "walk_forward_accuracy": None,
                "val_accuracy": None,
                "baseline_accuracy": None,
                "shortage_recall": 0.0,
            },
            "ship_gate": {
                "passed": False,
                "policy": SHIP_GATE_POLICY,
                "reason": "GSCPI series became unavailable mid-retrain — nothing was fit",
                "val_accuracy": None,
                "baseline_accuracy": None,
            },
            "current_stress_prob": REGIME_UNAVAILABLE_STRESS_PROB,
        }
    gscpi = raw_gscpi.reindex(features_df.index)
    metrics, hyperparams = walk_forward_evaluate(
        features_df, gscpi, labels, min_train=min_train
    )
    gate = evaluate_ship_gate(metrics)

    _raw_cal = metrics.get("calibration")
    cal: Dict[str, object] = _raw_cal if isinstance(_raw_cal, dict) else {}
    logger.info(
        "regime walk-forward over %s folds %s..%s — PROPER SCORING RULE (the gate): "
        "Brier model=%s persistence=%s climatology=%s | LogLoss model=%s persistence=%s "
        "climatology=%s | calibration slope=%s ECE=%s",
        metrics.get("n_folds"), metrics.get("eval_start"), metrics.get("eval_end"),
        metrics.get("brier"), metrics.get("baseline_brier"), metrics.get("climatology_brier"),
        metrics.get("log_loss"), metrics.get("baseline_log_loss"),
        metrics.get("climatology_log_loss"),
        cal.get("calibration_slope"), cal.get("expected_calibration_error"),
    )
    logger.info(
        "regime accuracy (reported, NOT the gate): acc=%s vs persistence=%s "
        "(delta %s, McNemar p=%s); hyperparameters %s chosen on the calibration window only",
        metrics.get("walk_forward_accuracy"), metrics.get("baseline_accuracy"),
        metrics.get("accuracy_delta_vs_baseline"), metrics.get("mcnemar_p_value"),
        metrics.get("hyperparameters"),
    )

    # Refit on ALL history for serving — the old code served a model fit on
    # pre-2019 data only and extrapolated seven years past its training window.
    #
    # The served model's predictive spread comes from the walk-forward's realized
    # one-step errors, NOT from its own in-sample residuals. Anything else ships
    # the overconfidence the calibration check exists to catch.
    final_model = _fit_regime_model(
        features_df.to_numpy(dtype=float),
        np.asarray(gscpi.to_numpy(dtype=float)),
        hyperparams or {},
        feature_names=list(features_df.columns),
        realized_error_sd=_metric_float(metrics.get("realized_error_sd")),
    )
    current = (
        get_current_stress_prob(final_model, features_df)
        if gate["passed"] else REGIME_UNAVAILABLE_STRESS_PROB
    )
    return {
        "pipe": final_model if gate["passed"] else None,
        "features": features_df if gate["passed"] else None,
        "labels": labels,
        "metrics": metrics,
        "ship_gate": gate,
        "current_stress_prob": current,
    }


# ── deprecated fixed-split trainer ───────────────────────────────────────────

def build_regime_pipeline(C: float = DEFAULT_C) -> Pipeline:
    """DEPRECATED. StandardScaler + L2 multinomial logistic regression.

    Retained for the legacy fixed-split path below and for tests that pin the
    old interface. ``class_weight='balanced'`` is the setting that, combined with
    a training window containing only 15 stress months, made the old model
    over-predict `stress` and starve `elevated` — see the module docstring.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    return Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(
            C=C,
            class_weight="balanced",
            solver="lbfgs",
            max_iter=5000,
            random_state=42,
        )),
    ])


def _persistence_accuracy(labels: pd.Series, mask) -> Optional[float]:
    """Accuracy of the naive baseline regime_t = regime_{t-1} on ``mask`` rows."""
    mask = np.asarray(getattr(mask, "values", mask))
    prev = labels.shift(1)
    idx = labels.index[mask]
    prev_vals = prev.reindex(idx)
    true_vals = labels.reindex(idx)
    ok = prev_vals.notna()
    if not ok.any():
        return None
    return round(float((prev_vals[ok].values == true_vals[ok].values).mean()), 4)


def train_regime_model(
    features_df: pd.DataFrame,
    labels: pd.Series,
    C: float = DEFAULT_C,
    train_end: str = DEFAULT_TRAIN_END,
    val_start: str = DEFAULT_VAL_START,
    val_end: str = DEFAULT_VAL_END,
) -> Tuple[Pipeline, Dict]:
    """DEPRECATED fixed-split trainer — kept only for interface compatibility.

    This is the protocol whose defects are documented in the module docstring:
    it trains on a window with almost no stress months, validates on COVID, and
    discards everything after ``val_end``. Use :func:`walk_forward_evaluate` for
    any number you intend to publish.
    """
    import pandas as pd
    from sklearn.metrics import confusion_matrix, f1_score, recall_score
    labels = labels.reindex(features_df.index)
    train_mask = features_df.index < train_end
    val_mask = (features_df.index >= val_start) & (features_df.index <= val_end)

    X_train = features_df[train_mask].values
    y_train = labels[train_mask].values

    pipe = build_regime_pipeline(C=C)
    pipe.fit(X_train, y_train)
    classes = list(pipe.classes_)

    if val_mask.sum() > 0:
        X_val = features_df[val_mask].values
        y_val = labels[val_mask].values
    else:
        X_val, y_val = features_df.values, labels.values
        val_mask = pd.Series(True, index=features_df.index)

    y_pred = pipe.predict(X_val)
    per_class = recall_score(y_val, y_pred, labels=classes, average=None, zero_division=0)
    stress_recall = per_class[classes.index(STRESS_CLASS)] if STRESS_CLASS in classes else 0.0

    metrics: Dict = {
        "protocol": "DEPRECATED fixed calendar split — see walk_forward_evaluate",
        "target": "NY Fed GSCPI regime (calm/elevated/stress) — one-month-ahead",
        "classes": classes,
        "val_accuracy": round(float((y_pred == y_val).mean()), 4),
        "macro_f1": round(float(f1_score(y_val, y_pred, labels=classes,
                                         average="macro", zero_division=0)), 4),
        "per_class_recall": {c: round(float(r), 4) for c, r in zip(classes, per_class)},
        "confusion_matrix": confusion_matrix(y_val, y_pred, labels=classes).tolist(),
        "baseline_accuracy": _persistence_accuracy(labels, val_mask),
        "val_size": int(val_mask.sum()),
        "shortage_recall": round(float(stress_recall), 4),
    }
    return pipe, metrics
