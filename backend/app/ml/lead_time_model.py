"""
Multi-model factory lead time predictor.

Trains four scikit-learn regressors (Ridge, RandomForest, GBM, MLP) on REAL
observed lead times collected from the DigiKey catalog and stored in the panel
written by ``app.ml.lead_time_collector``.

Target variable (Route A — real, no leakage):
    target_days = observed lead_time_weeks × 7
where ``lead_time_weeks`` is the FACTORY lead time a distributor published for
the part on the snapshot date — i.e. how long it takes to *replenish* that part,
not how long it takes to ship one that is already on the shelf.

ONE AUTHORITATIVE FEATURE BUILDER (fixed 2026-08-15)
----------------------------------------------------
Before this rewrite there were two feature paths that had silently diverged:

  * training  (``build_observed_matrix``) emitted
    ``['is_active','log_stock','macro_stress','cat_<5>','src_digikey']``
  * serving   (``build_feature_row`` + ``_align_row``) emitted
    ``['category','is_domestic','dist_km','tier','macro_stress','risk_score',
       'stock_coverage','is_chinese_origin']`` one-hot-encoded with the prefix
    ``category_`` instead of ``cat_``

``_align_row`` then reindexed onto the training columns and ZERO-FILLED every
name that did not match — which was all of them except ``macro_stress``. The
served vector was constant, so **every served prediction was the same 62.1085
days**, regardless of input. Nothing in the test suite caught it.

There is now exactly one place where a record becomes a row of numbers —
``_fill`` — and exactly one schema object, :class:`ResolvedSchema`. Training
derives that schema from the data (:func:`resolve_schema_from_records`); serving
recovers the *same* object by parsing the persisted column names
(:func:`parse_feature_cols`). Neither path can invent, drop or reorder a column.
``tests/test_lead_time_schema_contract.py`` pins the invariant.

DECLARATIVE FEATURE SPEC
------------------------
The candidate feature set is declared once, in :data:`NUMERIC_SPECS` and
:data:`CATEGORICAL_SPECS`. At fit time each candidate is admitted only if BOTH
hold:

  1. **It exists, varies, and is populated in the training panel.** A column
     that is constant across every row carries zero information; a column missing
     from more than ``max_missing_fraction`` of rows costs more rows than it is
     worth. This is what silently rotted before: the panel was a single snapshot
     from a single distributor, so ``macro_stress`` and ``src_digikey`` were
     constant — and ``macro_stress`` was nevertheless the only feature the
     serving path filled in, which is precisely why the prediction never moved.
     Nothing is ever imputed: rows that cannot be encoded are dropped, and the
     count is reported.
  2. **It is resolvable at prediction time.** :func:`serve_availability`
     introspects the ORM models the optimizer actually reads
     (``Component`` / ``DistributorOffer``). A feature that exists in the panel
     but has nowhere to come from in production would force serving to guess.

Nothing is dropped silently: :func:`resolve_schema_from_records` returns an
``exclusions`` list naming every rejected candidate and why, and that list is
persisted into ``metrics.joblib`` and published by ``GET /ml/model-comparison``.
Because admission is data-driven, the schema grows on its own the moment the
collector starts persisting new columns and the migration adds the matching ORM
attributes — no code change, and the contract test still guarantees parity.

Unseen categorical levels are handled per-spec, not uniformly:
  * ``dk_category`` uses ``unseen_policy="refuse"`` — DigiKey's taxonomy is
    canonical for a target that is DigiKey's own quote, and answering for a
    category with no training support would be a confident guess.
    :class:`UnknownCategoryError` is raised and the caller falls back to a
    documented deterministic estimate.
  * secondary categoricals use ``unseen_policy="other"``: rare levels are folded
    into an explicit ``__other__`` bucket AT FIT TIME, so that bucket is trained
    and an unseen level at serve time maps somewhere the model has actually seen.

EVALUATION — THE GROUPED SPLIT IS THE WHOLE BALLGAME
----------------------------------------------------
The panel contains large near-duplicate part FAMILIES: 456 STM32F103 rows,
147 ATMEGA328, 93 TMS320. ``base_product`` alone explains **R² = 0.856 of the
target IN SAMPLE** (361 levels over 2,615 rows; a per-level mean fitted and scored
on the same rows — an ANOVA statistic, NOT a model score and NOT cross-validated).
So a random split — or even an MPN-level split — puts siblings on both sides and
measures the model's ability to RECOGNISE a part family, not to predict the lead
time of a family it has never seen. Any R² from such a split is a memorisation
score.

How much: measured over 50 folds with the SAME estimator and rows and only the
grouping varying, R² goes **+0.825 random → +0.073 grouped by family → −0.697
holding out whole manufacturers** (medians +0.826 / +0.140 / −0.104). The
negative figure means the squared error on an unseen vendor exceeds that vendor's
entire label variance. ``docs/leakage_progression.json`` is the artifact;
``python -m seeds.run_leakage_progression`` regenerates it without retraining.
Every figure in this paragraph is transcribed from that artifact and pinned by
``tests/test_docs_match_artifacts.py`` — an earlier revision quoted an 810-row,
27-manufacturer vintage for two retrains after it stopped being true.

Every split here is therefore **grouped on the part family** (:func:`_group_key`,
:func:`make_splits`): the single 80/20 holdout, all repeated CV folds, and the
baselines, which are scored on the *identical* folds so the comparison is paired
(:func:`_paired_skill`). Quoting two marginal standard deviations and saying "the
error bars overlap" is the wrong test when both are scored on the same splits;
most of that spread is fold difficulty, which cancels in the pairing.

Baselines are deliberately strong: train-mean, always-210d, a category-mean
lookup table, and a **manufacturer-mean** lookup table — the last because vendor
identity is a powerful single predictor here (see below). A model that cannot
beat "what does this vendor usually quote?" has not earned its complexity.

WHAT THE PANEL ACTUALLY CAPTURED — A REAL LEAD-TIME EXTENSION
-------------------------------------------------------------
Between the 2026-07-01 and 2026-08-15 snapshots, 75 MPNs were observed twice:

  * the 19 that were NOT quoting 30 weeks in July barely moved (6→6, 9→9, 14→14);
  * **all 56 that quoted exactly 30 weeks in July re-quoted longer in August —
    14 to 40 weeks and 42 to 52 weeks** — nearly all STMicroelectronics parts.

That matters twice over. First, it retires an earlier assumption: the old 75-row
panel had 56/75 labels pinned at exactly 30 weeks and looked right-censored at a
publication ceiling, which argued for a Tobit/censored model. It was not a
ceiling — it was a real ST-wide 30-week quote, and it moved. In the current panel
only 5 of the 742 new rows sit at 30 weeks (median 12, mean 19.8, sd 15.2, range
2–99, 41 distinct values), so **a censored model would now be the wrong tool.**
There IS residual quantisation: ~22% of rows land on 26 / 40 / 52 weeks, i.e.
half-year, 40-week and one-year quotes.

Second, it means a weekly collector caught a genuine supply-chain event rather
than sampling noise — and it explains why ``manufacturer`` is such a strong
baseline: for a stretch of this panel, "which vendor?" really did determine the
quote.

THE THIRD SNAPSHOT (2026-08-17) — AND WHY IT IS THE WEAKEST OF THE THREE
------------------------------------------------------------------------
A scheduled run added a third cross-section on 2026-08-17: 363 rows, 1,180 in
the panel across three dates (75 / 742 / 363). 357 of its MPNs were also quoted
on 2026-08-15, so for the first time the panel contains a genuine 2-day
repeat measurement. It behaves exactly as two days should:

  * 324 of 357 (90.8%) re-quoted the IDENTICAL lead time;
  * all 100 STMicroelectronics parts held (23 at 40 weeks, 77 at 52) — the
    July→August escalation above did not revert;
  * of the 75 MPNs present at all three dates, 61 moved Jul→Aug-15 (mean
    +13.8 weeks) and only 4 moved Aug-15→Aug-17 (mean +0.37 weeks).

Read that as "nothing happened in two days", which is the correct reading, not
as a third independent observation of the cross-section. It adds almost no new
temporal signal; its value is that it CONFIRMS the July→August move was real
and persistent rather than a one-week artefact.

It also carries a defect that must not be laundered. This snapshot was produced
by the PRE-REWRITE collector, whose DigiKey lookup was a bare keyword search
returning ``Products[0]`` with no MPN verification and no ``match_type``
recorded. Of the 33 MPNs whose lead time did move Aug-15→Aug-17, 20 also moved
their unit price by more than 3x in those two days (Fisher exact p = 4e-23
against the 323/324 stable-price non-movers). A >3x two-day repricing is not a
repricing — it is a different DigiKey product wearing our MPN, and it clusters
on short catalogue-number MPNs ("2491", "4065", "3210") where a keyword search
has nothing to disambiguate on. Those rows are label noise. They are retained
rather than deleted because they are real API responses and deleting them
silently would be worse, but ~5-9% of the 2026-08-17 rows should be assumed
mis-resolved, they cannot be audited individually (no ``matched_mpn`` was
stored), and no claim about Aug-15→Aug-17 movement should rest on them.

Metrics are exposed via GET /api/v1/ml/model-comparison, which ties them to the
served estimator by object identity.
"""
from __future__ import annotations

import copy
import logging
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

# ``pandas`` and ``scikit-learn`` cost ~2 s of interpreter start-up between them
# (far worse on the 0.5-CPU deploy tier), and NOTHING at import time needs either
# — every use is inside a function body. They are imported lazily, at the top of
# each function that actually uses them, so ``import app.main`` never pays for
# them. Annotations are strings (``from __future__ import annotations``), so the
# TYPE_CHECKING block below is all the type checker needs.
if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

from app.ml.lead_time_labels import get_base_days


# Four competing models
#
# Built on first access rather than at import time — instantiating them imports
# scikit-learn. ``MODELS`` is still a module attribute (PEP 562 ``__getattr__``
# below), so ``from app.ml.lead_time_model import MODELS`` and
# ``module.MODELS`` behave exactly as before; the estimators, hyperparameters
# and key order are unchanged.
def _build_models() -> Dict:
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import Ridge
    from sklearn.neural_network import MLPRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return {
        "ridge": Pipeline([
            ("scaler", StandardScaler()),
            ("model", Ridge(alpha=1.0)),
        ]),
        "random_forest": RandomForestRegressor(
            n_estimators=100, min_samples_leaf=3, random_state=42
        ),
        "gradient_boosting": GradientBoostingRegressor(
            n_estimators=100, learning_rate=0.1, max_depth=4, random_state=42
        ),
        "mlp": Pipeline([
            ("scaler", StandardScaler()),
            ("model", MLPRegressor(
                hidden_layer_sizes=(64, 32),
                activation="relu",
                max_iter=500,
                random_state=42,
            )),
        ]),
    }


def _models() -> Dict:
    """The module-level ``MODELS`` dict, built once, for use *inside* this module.

    A module-level ``__getattr__`` is not consulted for global-name lookup from
    within the module itself, so internal callers must go through this.
    """
    models = globals().get("MODELS")
    if models is None:
        models = _build_models()
        globals()["MODELS"] = models
    return models


def __getattr__(name: str) -> Any:
    """PEP 562 — materialise ``MODELS`` on first external access."""
    if name == "MODELS":
        return _models()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:  # the type a reader of ``MODELS`` still sees
    MODELS: Dict


# ── THE feature contract ─────────────────────────────────────────────────────
#
# Bump FEATURE_SCHEMA_VERSION whenever the ENCODING changes (not when the
# resolved feature set changes — that is data-driven by design). It is persisted
# in metrics.joblib and app/ml/serving.py refuses to serve an artifact whose
# column names this code cannot parse, so a stale joblib can never again be
# silently zero-filled into a constant predictor.
FEATURE_SCHEMA_VERSION = 3

#: Column-name prefixes. Deliberately unlike the old ``cat_``/``category_``
#: names so a pre-fix artifact is rejected rather than misread.
NUMERIC_PREFIX = "n="
CATEGORICAL_PREFIX = "c="

#: Explicit bucket for rare / unseen categorical levels. Created AT FIT TIME so
#: it always has training support behind it.
OTHER_LEVEL = "__other__"

UNKNOWN_LEVEL = "Unknown"

#: Minimum fraction of production rows that must carry a value for a feature to
#: be admitted.
#:
#: "Resolvable in principle" is NOT the same as servable. Migration 0006 put
#: ``standard_pack`` / ``packaging`` on ``DistributorOffer``, where they are
#: populated only for DigiKey's own offers — **571 of 8,176 rows, 7.0%**. Both
#: columns existed, so the old ``hasattr`` check admitted them, and then every
#: non-DigiKey offer hit :class:`MissingFeatureError` at serve time. The
#: optimizer's ``supply_risk.model_available`` came back false on 6 of 6 sampled
#: runs: the model declined in the PRIMARY case, not an edge case.
#:
#: A feature present on 7% of rows is not servable in any useful sense. The
#: availability check therefore measures real coverage in the database and
#: excludes anything below this bar, with the measured percentage in the reason.
MIN_SERVE_COVERAGE = 0.50


class FeatureSchemaMismatch(ValueError):
    """Raised when ``feature_cols`` do not describe a schema this code can build.

    This is the guard that would have caught the 2026-08 constant-predictor bug:
    the persisted column names simply did not belong to the schema the serving
    code was building, and the old ``_align_row`` papered over that with zeros.
    """


class UnknownCategoryError(ValueError):
    """Raised when a prediction is requested for a category the model never saw.

    Encoding an unseen category as an all-zero one-hot block would place the row
    in a region of feature space with no training support, and the model would
    return a confident number for it anyway. We decline instead; callers fall
    back to a documented deterministic estimate.
    """


class MissingFeatureError(ValueError):
    """Raised when a record lacks a value for a feature the schema requires."""


@dataclass(frozen=True)
class NumericSpec:
    """A scalar feature. ``transform`` is part of the contract, not a detail."""
    name: str               # logical name; becomes ``n=<name>``
    panel_column: str       # column in the observed panel CSV
    record_key: str         # key in a serve-time record dict
    transform: str          # "log1p" | "identity" | "binary"
    serve_source: str       # ORM location, for the exclusion report
    min_unique: int = 2     # a column with fewer distinct values carries nothing
    #: Minimum fraction of PRODUCTION rows that must actually carry a value for
    #: this feature to be worth admitting. See MIN_SERVE_COVERAGE.
    min_serve_coverage: float = MIN_SERVE_COVERAGE
    # A feature missing from a SMALL fraction of training rows is kept and those
    # rows are dropped (see _drop_unfillable); above this fraction the feature
    # itself is excluded, because paying more rows than that for one column is a
    # bad trade. Never imputed either way.
    max_missing_fraction: float = 0.10


@dataclass(frozen=True)
class CategoricalSpec:
    """A one-hot feature. ``unseen_policy`` decides what an unknown level means."""
    name: str
    panel_column: str
    record_key: str
    serve_source: str
    unseen_policy: str = "other"   # "other" -> __other__ bucket; "refuse" -> raise
    min_level_count: int = 3       # rarer levels are folded into __other__
    max_levels: int = 60           # guard against memorising a high-cardinality id
    min_unique: int = 2
    max_missing_fraction: float = 0.10
    min_serve_coverage: float = MIN_SERVE_COVERAGE


#: Every candidate scalar feature. Admission is decided at fit time.
#:
#: NOT declared, deliberately:
#:  * ``stock`` — measured INERT against this target (Spearman -0.015 with
#:    lead-time weeks over the 736-row panel). It was a feature in schema v2; it
#:    is removed rather than demoted, because a feature that carries no signal
#:    still costs rows whenever it is missing.
#:  * ``product_status_id`` (-0.16) — redundant with the ``lifecycle_status``
#:    string, which IS persisted.
#:  * ``discontinued`` / ``end_of_life`` / ``ncnr`` / ``digireel_fee`` /
#:    ``manufacturer_public_quantity`` / ``marketplace`` — near-constant.
NUMERIC_SPECS: Dict[str, NumericSpec] = {
    s.name: s for s in (
        # TRAIN/SERVE SKEW CLOSED (migration 0007). This trains on the panel's
        # `dk_unit_price` and now serves `Component.digikey_unit_price` — the SAME
        # DigiKey figure, persisted per part. It previously served
        # `DistributorOffer.price`, which is the same QUANTITY from a possibly
        # different vendor: a documented approximation, but a skew nonetheless,
        # and this module exists to remove exactly that.
        NumericSpec("log_unit_price", "dk_unit_price", "unit_price", "log1p",
                    "Component.digikey_unit_price"),
        NumericSpec("log_moq", "moq", "moq", "log1p",
                    "DistributorOffer.moq"),
        NumericSpec("log_standard_pack", "standard_package", "standard_pack", "log1p",
                    "DistributorOffer.standard_pack"),
        NumericSpec("is_normally_stocked", "normally_stocking", "is_normally_stocked",
                    "binary", "Component.normally_stocked"),

        # ── Declared but currently UNSERVABLE ────────────────────────────────
        # These are the strongest numeric correlates in the panel, and they are
        # declared here on purpose even though no ORM column persists them yet.
        # Declaring them means serve_availability() reports them as unavailable,
        # they appear in `feature_exclusions` on GET /ml/model-comparison with
        # the reason, and they switch themselves ON the moment a migration adds
        # the column — no code change here. A comment would have hidden the gap;
        # this publishes it.
        NumericSpec("parameter_count", "parameter_count", "parameter_count", "log1p",
                    "Component.parameter_count"),          # Spearman +0.37
        NumericSpec("max_break_qty", "max_break_qty", "max_break_qty", "log1p",
                    "Component.max_break_qty"),            # Spearman +0.22
        NumericSpec("price_break_count", "price_break_count", "price_break_count", "log1p",
                    "Component.price_break_count"),        # Spearman +0.17
    )
}

#: Every candidate one-hot feature.
#:
#: ``tariff_active`` and ``dk_manufacturer`` are NOT declared: the first is a
#: shipping-cost attribute with no lead-time mechanism, the second duplicates
#: ``manufacturer``.
CATEGORICAL_SPECS: Dict[str, CategoricalSpec] = {
    s.name: s for s in (
        # DigiKey's taxonomy is CANONICAL for this target — the model is trained
        # on DigiKey's own quoted lead times. The Nexar `category` on the DB
        # disagrees with it (52 levels vs 17), so both are offered and the
        # DigiKey one carries the refusal policy: an unseen DigiKey category
        # means the part is outside the panel's support, and an __other__ bucket
        # would be answering a question we cannot answer.
        CategoricalSpec("dk_category", "dk_category", "dk_category",
                        "Component.digikey_category",
                        unseen_policy="refuse", min_level_count=1, max_levels=80),
        CategoricalSpec("dk_subcategory", "dk_subcategory", "dk_subcategory",
                        "Component.digikey_subcategory", unseen_policy="other"),
        # Nexar taxonomy — always populated on the DB, so it keeps some signal
        # available for parts DigiKey never categorised.
        CategoricalSpec("category", "category", "category", "Component.category",
                        unseen_policy="other", max_levels=80),
        CategoricalSpec("manufacturer", "manufacturer", "manufacturer",
                        "Component.manufacturer", unseen_policy="other", max_levels=60),
        # 88% "Active", but the minority levels separate cleanly:
        # Obsolete ≈ 14.5 wk vs Active ≈ 20.5 wk.
        CategoricalSpec("lifecycle_status", "lifecycle_status", "lifecycle_status",
                        "Component.lifecycle_status", unseen_policy="other"),
        CategoricalSpec("packaging", "packaging", "packaging",
                        "DistributorOffer.packaging", unseen_policy="other"),

        # ── Declared but currently UNSERVABLE (see the numeric block) ────────
        CategoricalSpec("package_case", "package_case", "package_case",
                        "Component.package_case", unseen_policy="other", max_levels=130),
        CategoricalSpec("htsus_code", "htsus_code", "htsus_code",
                        "Component.htsus_code", unseen_policy="other"),
        CategoricalSpec("rohs_status", "rohs_status", "rohs_status",
                        "Component.rohs_status", unseen_policy="other"),
    )
}

#: Deterministic evaluation order — numerics first, then categoricals.
CANDIDATE_NUMERICS: Tuple[str, ...] = tuple(NUMERIC_SPECS)
CANDIDATE_CATEGORICALS: Tuple[str, ...] = tuple(CATEGORICAL_SPECS)

#: Where each candidate comes from at prediction time: (ORM model, attribute).
#: :func:`serve_availability` introspects these, so a feature switches on by
#: itself once a migration adds the column — no edit here required.
SERVE_SOURCES: Dict[str, Tuple[str, str]] = {
    "log_unit_price": ("Component", "digikey_unit_price"),
    "parameter_count": ("Component", "parameter_count"),
    "max_break_qty": ("Component", "max_break_qty"),
    "price_break_count": ("Component", "price_break_count"),
    "package_case": ("Component", "package_case"),
    "htsus_code": ("Component", "htsus_code"),
    "rohs_status": ("Component", "rohs_status"),
    "log_moq": ("DistributorOffer", "moq"),
    "log_standard_pack": ("DistributorOffer", "standard_pack"),
    "is_normally_stocked": ("Component", "normally_stocked"),
    "dk_category": ("Component", "digikey_category"),
    "dk_subcategory": ("Component", "digikey_subcategory"),
    "category": ("Component", "category"),
    "manufacturer": ("Component", "manufacturer"),
    "lifecycle_status": ("Component", "lifecycle_status"),
    "packaging": ("DistributorOffer", "packaging"),
}

#: HARD LEAKAGE GUARD. ``Component.observed_lead_time_weeks`` is the TARGET,
#: persisted so the optimizer can prefer a real quote over a prediction. Using it
#: (or its snapshot date) as a feature would make the model trivially "perfect"
#: and completely worthless. No spec may point at these, and no panel column
#: derived from them may become a record key. Enforced by
#: :func:`_assert_no_label_leakage`, which runs at import time.
FORBIDDEN_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("Component", "observed_lead_time_weeks"),
    ("Component", "lead_time_observed_at"),
)
FORBIDDEN_PANEL_COLUMNS: Tuple[str, ...] = (
    "lead_time_weeks",
    "lead_time_weeks_raw",
    "observed_lead_time_weeks",
)


def _assert_no_label_leakage() -> None:
    """Fail at import if any declared feature reads the label. No exceptions."""
    for name, source in SERVE_SOURCES.items():
        if source in FORBIDDEN_SOURCES:
            raise RuntimeError(
                f"feature {name!r} reads {source[0]}.{source[1]}, which is the "
                "lead-time TARGET — that is label leakage, not a feature"
            )
    declared: List[Tuple[str, str]] = [
        (s.name, s.panel_column) for s in NUMERIC_SPECS.values()
    ] + [
        (c.name, c.panel_column) for c in CATEGORICAL_SPECS.values()
    ]
    for feature_name, panel_column in declared:
        if panel_column in FORBIDDEN_PANEL_COLUMNS:
            raise RuntimeError(
                f"feature {feature_name!r} reads panel column {panel_column!r}, "
                "which is the lead-time TARGET — that is label leakage"
            )


_assert_no_label_leakage()


def _spec_min_coverage(name: str) -> float:
    spec = NUMERIC_SPECS.get(name) or CATEGORICAL_SPECS.get(name)
    return spec.min_serve_coverage if spec is not None else MIN_SERVE_COVERAGE


def measure_serve_coverage() -> Dict[str, Optional[float]]:
    """Fraction of PRODUCTION rows that actually carry each candidate feature.

    Returns ``{feature: fraction}``, or ``{feature: None}`` when the database
    cannot be reached — coverage unknown is reported as unknown, never assumed.
    The denominator is the table the feature lives on: all components for
    ``Component.*``, all offers for ``DistributorOffer.*``.
    """
    unknown: Dict[str, Optional[float]] = {name: None for name in SERVE_SOURCES}
    try:
        from sqlalchemy import func, select

        from app.core.database import SessionLocal
        from app.models.component import Component, DistributorOffer
    except Exception:  # noqa: BLE001 — no DB layer => coverage simply unknown
        return unknown

    models = {"Component": Component, "DistributorOffer": DistributorOffer}
    out: Dict[str, Optional[float]] = {}
    try:
        with SessionLocal() as session:
            totals: Dict[str, int] = {}
            for model_name, model in models.items():
                try:
                    totals[model_name] = int(
                        session.execute(select(func.count()).select_from(model)).scalar() or 0
                    )
                except Exception:  # noqa: BLE001 — table may not exist yet
                    totals[model_name] = 0

            for name, (model_name, attr) in SERVE_SOURCES.items():
                target = models.get(model_name)
                total = totals.get(model_name, 0)
                column = getattr(target, attr, None) if target is not None else None
                if target is None or column is None or total == 0:
                    out[name] = None
                    continue
                try:
                    filled = int(session.execute(
                        select(func.count()).select_from(target).where(column.isnot(None))
                    ).scalar() or 0)
                    out[name] = filled / total
                except Exception:  # noqa: BLE001
                    out[name] = None
    except Exception:  # noqa: BLE001 — DB unreachable => coverage unknown
        return unknown
    return out


def serve_availability(
    coverage: Optional[Mapping[str, Optional[float]]] = None,
) -> Dict[str, Tuple[bool, str]]:
    """Which candidate features production can actually supply.

    Two independent conditions, both required:

      1. **The column exists** on the ORM model the optimizer reads.
      2. **It is actually populated** on at least ``min_serve_coverage`` of rows.

    Condition 2 is the one added after ``standard_pack`` / ``packaging`` — real
    columns, populated on 7.0% of offers — were admitted by an ``hasattr`` check
    and then made the model decline on 6 of 6 sampled optimizer runs. Existing
    but empty is not the same as available.

    Coverage that cannot be measured (no database) is reported as unverified and
    does NOT block admission — a training run must not require a live DB — but
    the reason string says so, so it is never mistaken for a measurement.
    """
    out: Dict[str, Tuple[bool, str]] = {}
    try:
        from app.models.component import Component, DistributorOffer
        models = {"Component": Component, "DistributorOffer": DistributorOffer}
    except Exception as exc:  # noqa: BLE001 — no ORM => nothing is serve-resolvable
        return {
            name: (False, f"ORM not importable ({type(exc).__name__}: {exc})")
            for name in SERVE_SOURCES
        }

    measured = dict(coverage) if coverage is not None else measure_serve_coverage()

    for name, (model_name, attr) in SERVE_SOURCES.items():
        model = models.get(model_name)
        if model is None:
            out[name] = (False, f"unknown ORM model {model_name!r}")
            continue
        if not hasattr(model, attr):
            out[name] = (
                False,
                f"{model_name}.{attr} does not exist — the column is not persisted yet, "
                "so serving could only guess it",
            )
            continue

        frac = measured.get(name)
        floor = _spec_min_coverage(name)
        if frac is None:
            out[name] = (True, f"{model_name}.{attr} (serve coverage unverified — no database)")
        elif frac < floor:
            out[name] = (
                False,
                f"{model_name}.{attr} is populated on only {frac:.1%} of rows "
                f"(floor {floor:.0%}) — the column exists but is empty in production, so "
                "admitting it would make the model decline on most real inputs",
            )
        else:
            out[name] = (True, f"{model_name}.{attr} ({frac:.1%} of rows populated)")
    return out


@dataclass(frozen=True)
class ResolvedSchema:
    """The admitted feature set, in column order. The single schema object.

    ``numerics`` are logical names from :data:`NUMERIC_SPECS`; ``categoricals``
    maps a logical name from :data:`CATEGORICAL_SPECS` to its ordered level list.
    """
    numerics: Tuple[str, ...] = ()
    categoricals: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()

    @property
    def columns(self) -> List[str]:
        cols = [f"{NUMERIC_PREFIX}{n}" for n in self.numerics]
        for feature, levels in self.categoricals:
            cols.extend(f"{CATEGORICAL_PREFIX}{feature}={lvl}" for lvl in levels)
        return cols

    def levels(self, feature: str) -> Tuple[str, ...]:
        for name, levels in self.categoricals:
            if name == feature:
                return levels
        return ()


# ── column-name encoding / decoding ──────────────────────────────────────────

def parse_feature_cols(feature_cols: Sequence[str]) -> ResolvedSchema:
    """Recover the :class:`ResolvedSchema` from persisted column names.

    Raises :class:`FeatureSchemaMismatch` on anything this code cannot build —
    an unknown prefix, an unknown logical feature name, numerics appearing after
    categoricals, or a categorical's levels not being contiguous. Failing here is
    the point: a schema we cannot parse is a schema we must not serve.
    """
    cols = list(feature_cols)
    if not cols:
        raise FeatureSchemaMismatch("feature_cols is empty — no schema to serve")

    numerics: List[str] = []
    cats: List[Tuple[str, List[str]]] = []
    seen_categorical = False

    for col in cols:
        if col.startswith(NUMERIC_PREFIX):
            if seen_categorical:
                raise FeatureSchemaMismatch(
                    f"numeric column {col!r} appears after the categorical block; "
                    "column order is part of the contract"
                )
            name = col[len(NUMERIC_PREFIX):]
            if name not in NUMERIC_SPECS:
                raise FeatureSchemaMismatch(
                    f"unknown numeric feature {name!r} in feature_cols — this build "
                    f"declares {sorted(NUMERIC_SPECS)!r}"
                )
            numerics.append(name)
        elif col.startswith(CATEGORICAL_PREFIX):
            seen_categorical = True
            body = col[len(CATEGORICAL_PREFIX):]
            feature, sep, level = body.partition("=")
            if not sep:
                raise FeatureSchemaMismatch(
                    f"malformed categorical column {col!r} — expected "
                    f"{CATEGORICAL_PREFIX}<feature>=<level>"
                )
            if feature not in CATEGORICAL_SPECS:
                raise FeatureSchemaMismatch(
                    f"unknown categorical feature {feature!r} in feature_cols — this "
                    f"build declares {sorted(CATEGORICAL_SPECS)!r}"
                )
            if cats and cats[-1][0] == feature:
                cats[-1][1].append(level)
            elif any(f == feature for f, _ in cats):
                raise FeatureSchemaMismatch(
                    f"levels of categorical {feature!r} are not contiguous in feature_cols"
                )
            else:
                cats.append((feature, [level]))
        else:
            raise FeatureSchemaMismatch(
                f"unrecognised feature column {col!r} — not a v{FEATURE_SCHEMA_VERSION} "
                f"schema (expected a {NUMERIC_PREFIX!r} or {CATEGORICAL_PREFIX!r} prefix). "
                "The artifact was trained on a different feature schema; retrain with "
                "`python -m seeds.train_ml_models`."
            )

    if not numerics and not cats:
        raise FeatureSchemaMismatch("feature_cols contains no usable features")
    return ResolvedSchema(
        numerics=tuple(numerics),
        categoricals=tuple((f, tuple(lv)) for f, lv in cats),
    )


def validate_feature_cols(feature_cols: Sequence[str]) -> ResolvedSchema:
    """Parse and return the schema, raising :class:`FeatureSchemaMismatch` if invalid."""
    return parse_feature_cols(feature_cols)


#: The categorical whose vocabulary bounds what the model will answer for.
#: DigiKey's taxonomy is canonical (the target is DigiKey's own quote); the Nexar
#: `category` is the fallback when DigiKey never categorised the part.
PRIMARY_CATEGORY_FEATURES: Tuple[str, ...] = ("dk_category", "category")


def primary_category_feature(feature_cols: Sequence[str]) -> Optional[str]:
    """Which categorical actually carries the refusal policy in this schema."""
    try:
        schema = parse_feature_cols(feature_cols)
    except FeatureSchemaMismatch:
        return None
    present = {f for f, _ in schema.categoricals}
    for name in PRIMARY_CATEGORY_FEATURES:
        if name in present and CATEGORICAL_SPECS[name].unseen_policy == "refuse":
            return name
    for name in PRIMARY_CATEGORY_FEATURES:
        if name in present:
            return name
    return None


def known_categories(feature_cols: Sequence[str]) -> List[str]:
    """The trained category vocabulary encoded in a persisted ``feature_cols``."""
    try:
        schema = parse_feature_cols(feature_cols)
    except FeatureSchemaMismatch:
        return []
    feature = primary_category_feature(feature_cols)
    if feature is None:
        return []
    return [lvl for lvl in schema.levels(feature) if lvl != OTHER_LEVEL]


# ── records ──────────────────────────────────────────────────────────────────

def required_record_keys(feature_cols: Sequence[str]) -> List[str]:
    """The record keys a schema will REFUSE to predict without.

    Only features that raise on a missing value are listed: numerics, which have
    no defensible default, and any categorical whose policy is ``refuse``. A
    categorical with an ``__other__`` bucket is satisfied by an absent value, so
    it is genuinely optional and is not reported here.
    """
    schema = parse_feature_cols(feature_cols)
    keys = [NUMERIC_SPECS[name].record_key for name in schema.numerics]
    for feature, _levels in schema.categoricals:
        spec = CATEGORICAL_SPECS[feature]
        if spec.unseen_policy == "refuse":
            keys.append(spec.record_key)
    return sorted(dict.fromkeys(keys))


def optional_record_keys(feature_cols: Sequence[str]) -> List[str]:
    """Record keys the schema uses but can fall back to ``__other__`` for."""
    schema = parse_feature_cols(feature_cols)
    required = set(required_record_keys(feature_cols))
    keys = [
        CATEGORICAL_SPECS[f].record_key
        for f, _ in schema.categoricals
        if CATEGORICAL_SPECS[f].record_key not in required
    ]
    return sorted(dict.fromkeys(keys))


def build_feature_row(**values: object) -> Dict[str, object]:
    """Build ONE raw record. Training rows and serving rows have this same shape.

    Keys are the ``record_key`` of a declared spec — ``category``, ``stock``,
    ``unit_price``, ``manufacturer``, ``lifecycle_status``, ``moq``,
    ``standard_pack``, ``packaging``, ``is_normally_stocked``. Unknown keys are
    kept and ignored; keys the resolved schema needs but that are absent cause an
    honest :class:`MissingFeatureError` at fill time rather than an imputed zero.

    There is no imputation anywhere in this module. The training-time median is
    not persisted, so inventing one at serve time would make training and serving
    disagree — which is the entire bug class this rewrite exists to remove.
    """
    return dict(values)


def _coerce_numeric(value: object, spec: NumericSpec) -> float:
    import pandas as pd
    number = pd.to_numeric(value, errors="coerce")
    if number is None or (isinstance(number, float) and np.isnan(number)):
        raise MissingFeatureError(
            f"record has no usable {spec.record_key!r} (needed for feature {spec.name!r})"
        )
    x = float(number)
    if spec.transform == "log1p":
        return float(np.log1p(max(x, 0.0)))
    if spec.transform == "binary":
        return 1.0 if x != 0.0 else 0.0
    return x


def _level_of(value: object) -> str:
    """Normalise a categorical value to a level name.

    None / NaN / blank all collapse to a single explicit ``Unknown`` level. They
    must NOT become the literal string ``"nan"`` — that silently creates a level
    whose meaning is "pandas parsed a float NaN here", which is not a category.
    """
    if value is None:
        return UNKNOWN_LEVEL
    if isinstance(value, float) and np.isnan(value):
        return UNKNOWN_LEVEL
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "<na>"):
        return UNKNOWN_LEVEL
    return text


def _fill(records: Sequence[Mapping[str, object]], schema: ResolvedSchema) -> np.ndarray:
    """THE encoder. Every row of numbers this project ever builds comes from here.

    Training and serving both reach this function — training via a schema
    resolved from data, serving via a schema parsed from persisted column names.
    """
    cols = schema.columns
    index = {c: i for i, c in enumerate(cols)}
    X = np.zeros((len(records), len(cols)), dtype=float)

    for i, record in enumerate(records):
        for name in schema.numerics:
            spec = NUMERIC_SPECS[name]
            X[i, index[f"{NUMERIC_PREFIX}{name}"]] = _coerce_numeric(
                record.get(spec.record_key), spec
            )
        for feature, levels in schema.categoricals:
            spec_c = CATEGORICAL_SPECS[feature]
            if spec_c.record_key not in record:
                raise MissingFeatureError(
                    f"record has no {spec_c.record_key!r} (needed for feature {feature!r})"
                )
            level = _level_of(record.get(spec_c.record_key))
            if level not in levels:
                if spec_c.unseen_policy == "refuse":
                    raise UnknownCategoryError(
                        f"{feature}={level!r} is not in the trained vocabulary "
                        f"{sorted(lv for lv in levels if lv != OTHER_LEVEL)!r}. The observed "
                        "lead-time panel has never seen it, so the model has no basis for a "
                        "prediction."
                    )
                if OTHER_LEVEL not in levels:
                    raise UnknownCategoryError(
                        f"{feature}={level!r} is unseen and this schema has no "
                        f"{OTHER_LEVEL!r} bucket to fall back on"
                    )
                level = OTHER_LEVEL
            X[i, index[f"{CATEGORICAL_PREFIX}{feature}={level}"]] = 1.0
    return X


# ── schema resolution (training) ─────────────────────────────────────────────

def _would_lose_a_snapshot(
    records: Sequence[Mapping[str, object]],
    snapshot_dates: Optional[Sequence[str]],
    record_key: str,
) -> Optional[str]:
    """Would REQUIRING ``record_key`` delete an entire cross-section?

    A feature missing from every row of one snapshot date is not merely sparse —
    admitting it removes that whole date from training, because rows the schema
    cannot encode are dropped. That destroys the panel's time dimension, which is
    worth more than any single weak feature: it is the only thing that lets the
    model see the same part quoted differently at different times.

    So the rule is: **no feature may cost the dataset an entire cross-section.**
    Returns a reason string when it would, else ``None``.
    """
    if not snapshot_dates or len(snapshot_dates) != len(records):
        return None
    all_dates = sorted(set(snapshot_dates))
    if len(all_dates) < 2:
        return None
    def _has_value(record: Mapping[str, object]) -> bool:
        value = record.get(record_key)
        if value is None:
            return False
        return not (isinstance(value, float) and np.isnan(value))

    present = {
        date for date, record in zip(snapshot_dates, records, strict=True)
        if _has_value(record)
    }
    lost = [d for d in all_dates if d not in present]
    if not lost:
        return None
    return (
        f"absent from EVERY row of snapshot date(s) {lost} — requiring it would drop "
        f"{len(lost)} of {len(all_dates)} cross-sections from training, and the panel's "
        "time dimension is worth more than one weak feature"
    )


def resolve_schema_from_records(
    records: Sequence[Mapping[str, object]],
    serve_caps: Optional[Mapping[str, Tuple[bool, str]]] = None,
    snapshot_dates: Optional[Sequence[str]] = None,
) -> Tuple[ResolvedSchema, List[Dict[str, object]]]:
    """Decide which declared candidates are admitted, and say why the rest are not.

    A candidate is admitted only when it is (a) present and varying in the
    training records and (b) resolvable at prediction time. Returns
    ``(schema, exclusions)``; ``exclusions`` is a list of
    ``{"feature", "kind", "reason", "serve_source"}`` dicts and is reported by
    ``GET /ml/model-comparison`` — a declared feature is never dropped silently.
    """
    import pandas as pd
    caps = dict(serve_caps if serve_caps is not None else serve_availability())
    exclusions: List[Dict[str, object]] = []

    def _reject(name: str, kind: str, reason: str, source: str) -> None:
        exclusions.append(
            {"feature": name, "kind": kind, "reason": reason, "serve_source": source}
        )

    numerics: List[str] = []
    for name in CANDIDATE_NUMERICS:
        spec = NUMERIC_SPECS[name]
        available, why = caps.get(name, (False, "no serve source declared"))
        if not available:
            _reject(name, "numeric", f"not resolvable at prediction time: {why}",
                    spec.serve_source)
            continue
        lost = _would_lose_a_snapshot(records, snapshot_dates, spec.record_key)
        if lost:
            _reject(name, "numeric", lost, spec.serve_source)
            continue
        raw = [r.get(spec.record_key) for r in records if spec.record_key in r]
        values = pd.to_numeric(pd.Series(raw, dtype="object"), errors="coerce").dropna()
        missing = len(records) - len(values)
        frac = missing / len(records) if records else 1.0
        if frac > spec.max_missing_fraction:
            _reject(name, "numeric",
                    f"absent or unparseable in {missing} of {len(records)} training rows "
                    f"({frac:.1%} > the {spec.max_missing_fraction:.0%} ceiling) — keeping it "
                    "would cost more rows than the column is worth, and it is never imputed",
                    spec.serve_source)
            continue
        if values.nunique() < spec.min_unique:
            _reject(name, "numeric",
                    f"constant in the training panel ({values.nunique()} distinct value(s)) "
                    "— carries no information",
                    spec.serve_source)
            continue
        numerics.append(name)

    cats: List[Tuple[str, Tuple[str, ...]]] = []
    for name in CANDIDATE_CATEGORICALS:
        spec_c = CATEGORICAL_SPECS[name]
        available, why = caps.get(name, (False, "no serve source declared"))
        if not available:
            _reject(name, "categorical", f"not resolvable at prediction time: {why}",
                    spec_c.serve_source)
            continue
        if spec_c.unseen_policy == "refuse":
            lost_c = _would_lose_a_snapshot(records, snapshot_dates, spec_c.record_key)
            if lost_c:
                _reject(name, "categorical", lost_c, spec_c.serve_source)
                continue
        raw_levels = [_level_of(r.get(spec_c.record_key))
                      for r in records if spec_c.record_key in r]
        missing_c = len(records) - len(raw_levels)
        frac_c = missing_c / len(records) if records else 1.0
        if frac_c > spec_c.max_missing_fraction:
            _reject(name, "categorical",
                    f"absent in {missing_c} of {len(records)} training rows "
                    f"({frac_c:.1%} > the {spec_c.max_missing_fraction:.0%} ceiling)",
                    spec_c.serve_source)
            continue
        counts = pd.Series(raw_levels).value_counts()
        if len(counts) < spec_c.min_unique:
            _reject(name, "categorical",
                    f"constant in the training panel ({len(counts)} distinct level(s)) "
                    "— carries no information",
                    spec_c.serve_source)
            continue

        kept = [str(lvl) for lvl, n in counts.items() if n >= spec_c.min_level_count]
        kept = sorted(kept)[: spec_c.max_levels]
        folded = [str(lvl) for lvl in counts.index if str(lvl) not in kept]
        levels = list(kept)
        if spec_c.unseen_policy == "other" or folded:
            if spec_c.unseen_policy == "refuse" and folded:
                # A refusing feature must not lose levels; keep them all.
                levels = sorted(str(lvl) for lvl in counts.index)[: spec_c.max_levels]
            elif OTHER_LEVEL not in levels:
                levels.append(OTHER_LEVEL)
        if len([lv for lv in levels if lv != OTHER_LEVEL]) < 1 or len(levels) < spec_c.min_unique:
            _reject(name, "categorical",
                    "no level survives the rare-level fold — nothing to encode",
                    spec_c.serve_source)
            continue
        cats.append((name, tuple(levels)))

    return ResolvedSchema(numerics=tuple(numerics), categoricals=tuple(cats)), exclusions


def build_design_matrix(
    records: Sequence[Mapping[str, object]],
    feature_cols: Optional[Sequence[str]] = None,
    schema: Optional[ResolvedSchema] = None,
) -> Tuple[np.ndarray, List[str]]:
    """THE feature builder. Training and serving both go through this function.

    Args:
        records:      raw records shaped like :func:`build_feature_row`.
        feature_cols: SERVING — those exact columns, in that exact order, are
                      produced. The data cannot add, drop or reorder a column.
        schema:       TRAINING — a pre-resolved schema. When both are ``None``
                      the schema is resolved from ``records``.

    Raises:
        FeatureSchemaMismatch: ``feature_cols`` is not a schema this build knows.
        UnknownCategoryError:  a refusing categorical met an unseen level.
        MissingFeatureError:   a record lacks a value the schema requires.
    """
    if feature_cols is not None:
        resolved = parse_feature_cols(feature_cols)
    elif schema is not None:
        resolved = schema
    else:
        resolved, _ = resolve_schema_from_records(records)
    return _fill(records, resolved), resolved.columns


def align_row(record: Mapping[str, object], feature_cols: Sequence[str]) -> np.ndarray:
    """Serving-side 1×N vector, built by the same code path as training."""
    X, _ = build_design_matrix([record], feature_cols=feature_cols)
    return X


# ── evaluation ───────────────────────────────────────────────────────────────

def make_splits(
    n: int,
    groups: Optional[Sequence[object]] = None,
    n_splits: int = 1,
    test_size: float = 0.2,
    seed: int = 42,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Train/test index pairs, GROUPED so one part cannot straddle a split.

    The panel now carries the same MPN at more than one snapshot date. A plain
    random split can therefore put one observation of a part in train and another
    in test, and the model gets credit for recognising a part rather than for
    predicting a lead time. Splitting on the MPN group closes that leak. Falls
    back to an ungrouped split only when no groups are supplied.
    """
    from sklearn.model_selection import GroupShuffleSplit, train_test_split
    if groups is None:
        return [
            train_test_split(np.arange(n), test_size=test_size, random_state=seed + i)
            for i in range(n_splits)
        ]
    g = np.asarray(groups)
    out: List[Tuple[np.ndarray, np.ndarray]] = []
    for i in range(n_splits):
        gss = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed + i)
        tr, te = next(gss.split(np.zeros(n), groups=g))
        out.append((tr, te))
    return out


def _split_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, float]:
    from sklearn.metrics import mean_absolute_error, r2_score
    return {
        "rmse": round(float(np.sqrt(np.mean((y_pred - y_true) ** 2))), 2),
        "mae": round(float(mean_absolute_error(y_true, y_pred)), 2),
        "r2": round(float(r2_score(y_true, y_pred)), 4),
    }


N_CV_SPLITS = 20


def train_all_models(
    X: np.ndarray,
    y: np.ndarray,
    n_cv_splits: int = N_CV_SPLITS,
    groups: Optional[Sequence[object]] = None,
) -> Dict[str, Dict]:
    """Train all four models on 80% of the data and evaluate on the 20% holdout.

    A single 20% test split is noise, so each model additionally gets
    ``n_cv_splits`` repeated 80/20 splits. Quote the CV numbers, not the single
    split.

    Splits are GROUPED by ``groups`` (the part MPN) when supplied, so the same
    part observed at two snapshot dates cannot appear on both sides.

    ``cv_rmse_mean`` is the champion-selection metric and the number to quote.
    R² is reported too, but it is fragile whenever a large share of labels sits
    on DigiKey's 30-week publication ceiling: a test fold that is nearly constant
    makes R² divide by ~0 variance and blow up negative, dragging the mean far
    below the median. That is a property of the label distribution, not of the
    model — hence ``cv_r2_median`` alongside ``cv_r2_mean``. RMSE has no such
    pathology.

    Returns ``{model_name: {"model", "rmse", "mae", "r2",
                            "cv_rmse_mean", "cv_rmse_std",
                            "cv_r2_mean", "cv_r2_std", "cv_r2_median",
                            "cv_splits"}}``.
    """
    from sklearn.metrics import r2_score
    (tr0, te0), = make_splits(len(y), groups, n_splits=1)
    X_train, X_test, y_train, y_test = X[tr0], X[te0], y[tr0], y[te0]
    cv_folds = make_splits(len(y), groups, n_splits=n_cv_splits, seed=0)

    results: Dict[str, Dict] = {}
    for name, blueprint in _models().items():
        m = copy.deepcopy(blueprint)
        m.fit(X_train, y_train)
        info: Dict = {"model": m}
        info.update(_split_metrics(y_test, m.predict(X_test)))

        cv_rmse: List[float] = []
        cv_r2: List[float] = []
        for tr, te in cv_folds:
            cm = copy.deepcopy(blueprint)
            cm.fit(X[tr], y[tr])
            pb = cm.predict(X[te])
            cv_rmse.append(float(np.sqrt(np.mean((pb - y[te]) ** 2))))
            cv_r2.append(float(r2_score(y[te], pb)))
        info["cv_rmse_per_split"] = [round(v, 4) for v in cv_rmse]
        info["cv_splits"] = int(n_cv_splits)
        info["cv_rmse_mean"] = round(float(np.mean(cv_rmse)), 2)
        info["cv_rmse_std"] = round(float(np.std(cv_rmse)), 2)
        info["cv_r2_mean"] = round(float(np.mean(cv_r2)), 4)
        info["cv_r2_std"] = round(float(np.std(cv_r2)), 4)
        info["cv_r2_median"] = round(float(np.median(cv_r2)), 4)
        results[name] = info
    return results


#: DigiKey caps its published factory lead time at 30 weeks. Most panel rows sit
#: exactly on that ceiling, so "always predict the ceiling" is a genuinely
#: competitive naive baseline and has to be beaten, not ignored.
CENSORING_CEILING_DAYS = 210.0


def baseline_predictors(
    feature_cols: Sequence[str],
) -> Dict[str, Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]]:
    """THE naive baselines, as ``(X_train, y_train, X_test) -> y_pred`` callables.

    Exposed as a module-level factory rather than hidden inside
    :func:`compute_baselines` so that any *other* evaluation — notably
    ``seeds/run_leakage_progression.py``, which scores the same baselines under
    three different split regimes — reuses these exact definitions instead of
    reimplementing them. A "baseline" that differs between two reports is not a
    baseline, it is a second model.

    * ``train_mean``    — predict the training mean.
    * ``always_210d``   — predict DigiKey's 30-week publication ceiling.
    * ``category_mean`` — a groupby-category lookup table (train-mean for a
      category with no training rows), using DigiKey's canonical taxonomy when
      it is in the schema. This is the honest apples-to-apples comparison: if a
      lookup table matches the GBM, the "model" is a lookup table.
    * ``manufacturer_mean`` — the same idea on the manufacturer, which is a
      strong single predictor here (an ST-wide quote change drives much of the
      panel's variance). A model that cannot beat "what does this vendor
      usually quote?" has not earned its complexity.
    """
    def _block(feature: str) -> List[int]:
        prefix = f"{CATEGORICAL_PREFIX}{feature}="
        return [i for i, c in enumerate(feature_cols) if c.startswith(prefix)]

    # Prefer DigiKey's taxonomy (canonical for this target); fall back to Nexar's.
    cat_idx = _block("dk_category") or _block("category")
    man_idx = _block("manufacturer")

    def _group_mean_predict(
        idx: List[int], Xa: np.ndarray, ya: np.ndarray, Xb: np.ndarray
    ) -> np.ndarray:
        overall = float(ya.mean())
        if not idx:
            return np.full(Xb.shape[0], overall)
        keys_a = Xa[:, idx].argmax(axis=1)
        table = {int(k): float(ya[keys_a == k].mean()) for k in np.unique(keys_a)}
        return np.array([table.get(int(k), overall) for k in Xb[:, idx].argmax(axis=1)])

    def _train_mean(Xa: np.ndarray, ya: np.ndarray, Xb: np.ndarray) -> np.ndarray:
        return np.full(Xb.shape[0], float(ya.mean()))

    def _always_ceiling(Xa: np.ndarray, ya: np.ndarray, Xb: np.ndarray) -> np.ndarray:
        return np.full(Xb.shape[0], CENSORING_CEILING_DAYS)

    def _category_mean_predict(Xa: np.ndarray, ya: np.ndarray, Xb: np.ndarray) -> np.ndarray:
        return _group_mean_predict(cat_idx, Xa, ya, Xb)

    def _manufacturer_mean_predict(Xa: np.ndarray, ya: np.ndarray, Xb: np.ndarray) -> np.ndarray:
        return _group_mean_predict(man_idx, Xa, ya, Xb)

    return {
        "train_mean": _train_mean,
        "always_210d": _always_ceiling,
        "category_mean": _category_mean_predict,
        "manufacturer_mean": _manufacturer_mean_predict,
    }


def compute_baselines(
    X: np.ndarray,
    y: np.ndarray,
    feature_cols: Sequence[str],
    n_cv_splits: int = N_CV_SPLITS,
    groups: Optional[Sequence[object]] = None,
) -> Dict[str, Dict]:
    """Naive baselines the learned models must beat to be worth serving.

    The baseline definitions live in :func:`baseline_predictors`; this function
    only scores them. All of them are scored on the SAME grouped folds as the
    models, so the comparison is paired and family-level leakage is excluded from
    both sides.
    """
    from sklearn.metrics import r2_score
    predictors = baseline_predictors(feature_cols)
    _category_mean_predict = predictors["category_mean"]
    _manufacturer_mean_predict = predictors["manufacturer_mean"]

    (tr0, te0), = make_splits(len(y), groups, n_splits=1)
    X_train, X_test, y_train, y_test = X[tr0], X[te0], y[tr0], y[te0]
    preds = {
        "train_mean": np.full_like(y_test, float(y_train.mean())),
        "always_210d": np.full_like(y_test, CENSORING_CEILING_DAYS),
        "category_mean": _category_mean_predict(X_train, y_train, X_test),
        "manufacturer_mean": _manufacturer_mean_predict(X_train, y_train, X_test),
    }
    out: Dict[str, Dict] = {name: _split_metrics(y_test, p) for name, p in preds.items()}

    # EXACTLY the same folds the models were scored on, so the comparison is
    # paired and a per-split difference is meaningful.
    cv: Dict[str, Dict[str, List[float]]] = {k: {"rmse": [], "r2": []} for k in preds}
    for tr, te in make_splits(len(y), groups, n_splits=n_cv_splits, seed=0):
        ya, yb = y[tr], y[te]
        rounds = {
            "train_mean": np.full_like(yb, float(ya.mean())),
            "always_210d": np.full_like(yb, CENSORING_CEILING_DAYS),
            "category_mean": _category_mean_predict(X[tr], ya, X[te]),
            "manufacturer_mean": _manufacturer_mean_predict(X[tr], ya, X[te]),
        }
        for name, p in rounds.items():
            cv[name]["rmse"].append(float(np.sqrt(np.mean((p - yb) ** 2))))
            cv[name]["r2"].append(float(r2_score(yb, p)))
    for name in out:
        out[name]["cv_rmse_per_split"] = [round(v, 4) for v in cv[name]["rmse"]]
        out[name]["cv_splits"] = int(n_cv_splits)
        out[name]["cv_rmse_mean"] = round(float(np.mean(cv[name]["rmse"])), 2)
        out[name]["cv_rmse_std"] = round(float(np.std(cv[name]["rmse"])), 2)
        out[name]["cv_r2_mean"] = round(float(np.mean(cv[name]["r2"])), 4)
        out[name]["cv_r2_std"] = round(float(np.std(cv[name]["r2"])), 4)
        out[name]["cv_r2_median"] = round(float(np.median(cv[name]["r2"])), 4)
    return out


def _paired_skill(
    model_per_split: Optional[Sequence[float]],
    baseline_per_split: Optional[Sequence[float]],
) -> Dict[str, object]:
    """Paired per-fold comparison of two RMSE vectors scored on the SAME folds.

    Returns the mean paired difference, its standard error, an approximate
    two-sided p-value, and the fraction of folds the model won. This is the
    statistic to quote — not "the marginal error bars overlap", which conflates
    split-to-split difficulty (shared by both, and cancelled by pairing) with the
    difference actually being tested.
    """
    if not model_per_split or not baseline_per_split:
        return {"available": False, "reason": "per-split RMSE not recorded"}
    a = np.asarray(model_per_split, dtype=float)
    b = np.asarray(baseline_per_split, dtype=float)
    if a.shape != b.shape or a.size < 2:
        return {"available": False, "reason": "per-split RMSE vectors do not align"}
    diff = b - a                     # positive => the model has the lower RMSE
    mean_diff = float(diff.mean())
    se = float(diff.std(ddof=1) / np.sqrt(diff.size))
    rng = np.random.default_rng(0)
    boots = np.array([
        rng.choice(diff, diff.size, replace=True).mean() for _ in range(5000)
    ])
    ci_low, ci_high = (float(v) for v in np.percentile(boots, [2.5, 97.5]))
    out: Dict[str, object] = {
        "available": True,
        "n_folds": int(diff.size),
        "mean_rmse_reduction_days": round(mean_diff, 3),
        "std_error": round(se, 3),
        "ci95_low": round(ci_low, 3),
        "ci95_high": round(ci_high, 3),
        "significant_ci": bool(ci_low > 0),
        "folds_model_won": int((diff > 0).sum()),
        "fold_win_rate": round(float((diff > 0).mean()), 3),
    }
    try:
        from scipy import stats
        out["paired_t_p_value"] = round(float(stats.ttest_rel(b, a).pvalue), 5)
        out["wilcoxon_p_value"] = round(
            float(stats.wilcoxon(b, a).pvalue), 5
        ) if not np.allclose(b, a) else 1.0
    except Exception:  # noqa: BLE001 — significance is a nicety, not load-bearing
        out["paired_t_p_value"] = None
        out["wilcoxon_p_value"] = None
    # NOTE: folds share training rows, so these p-values are optimistic
    # (Dietterich 1998). They are reported as a sanity check, not as proof.
    out["caveat"] = (
        "repeated-split folds overlap in their training data, so the p-value is "
        "optimistic (Dietterich 1998); treat the fold win rate and the mean "
        "reduction as the primary evidence"
    )
    return out


def leakage_audit(
    X: np.ndarray,
    y: np.ndarray,
    family_groups: Sequence[str],
    manufacturer_groups: Sequence[str],
    model_name: str = "random_forest",
    n_splits: int = 20,
) -> Dict[str, object]:
    """Reproduce the leakage collapse: the single most important number here.

    The SAME estimator on the SAME data, scored under three progressively
    stricter split regimes:

      1. ``random``       — rows split at random. Sibling variants of one part
         family land on both sides, so this scores RECOGNISING a part family.
      2. ``family``       — grouped on ``base_product``. Siblings can no longer
         straddle the split.
      3. ``manufacturer`` — whole vendors held out. This asks the question the
         model is actually deployed to answer: a part from a vendor we have
         never quoted.

    The gap between (1) and (3) is not a modelling failure, it is the honest
    measurement of how little independent information the panel contains: the
    effective sample size for generalisation is the number of MANUFACTURERS, not
    the number of rows. Publishing (1) alone — which any default
    ``train_test_split`` produces — would be the single most misleading number
    this project could report, so all three are computed on every retrain and
    persisted together.
    """
    from sklearn.metrics import r2_score
    from sklearn.model_selection import GroupShuffleSplit, train_test_split

    blueprint = _models()[model_name]
    out: Dict[str, object] = {
        "model": model_name,
        "n_rows": int(len(y)),
        "n_families": int(len(set(family_groups))),
        "n_manufacturers": int(len(set(manufacturer_groups))),
        "n_splits": int(n_splits),
    }

    regimes = {
        "random": None,
        "family": list(family_groups),
        "manufacturer": list(manufacturer_groups),
    }
    for regime, groups in regimes.items():
        scores: List[float] = []
        if groups is not None and len(set(groups)) < 3:
            out[regime] = None
            continue
        for seed in range(n_splits):
            if groups is None:
                tr, te = train_test_split(
                    np.arange(len(y)), test_size=0.2, random_state=seed
                )
            else:
                gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=seed)
                tr, te = next(gss.split(np.zeros(len(y)), groups=np.asarray(groups)))
            if len(te) < 2 or len(np.unique(y[te])) < 2:
                continue
            m = copy.deepcopy(blueprint)
            m.fit(X[tr], y[tr])
            scores.append(float(r2_score(y[te], m.predict(X[te]))))
        out[regime] = round(float(np.mean(scores)), 4) if scores else None
        out[f"{regime}_median"] = round(float(np.median(scores)), 4) if scores else None

    out["headline"] = (
        f"R2 {out.get('random')} random split -> {out.get('family')} grouped by part "
        f"family -> {out.get('manufacturer')} holding out whole manufacturers "
        f"({out['n_manufacturers']} manufacturers, {out['n_families']} families, "
        f"{out['n_rows']} rows). The effective sample size for generalisation is the "
        "manufacturer count, not the row count."
    )
    return out


#: The bar the lead-time champion must clear to be persisted, mirroring the
#: regime model's gate. Two conditions:
#:   1. beat EVERY naive baseline on mean grouped-CV RMSE;
#:   2. beat the TOUGHEST one by a margin whose paired bootstrap CI excludes zero.
#:
#: Condition 2 matters because the baselines here are strong: a category-mean or
#: manufacturer-mean lookup table is a real competitor when effective n is 27
#: manufacturers. Beating one on a point estimate while the fold-to-fold spread
#: swamps the difference is not evidence, and this project does not ship models
#: on point estimates alone.
LEAD_TIME_SHIP_GATE_POLICY = "beats_all_baselines_significantly"


def evaluate_lead_time_ship_gate(result: Optional[Mapping[str, object]]) -> Dict[str, object]:
    """Decide whether a trained lead-time model is fit to persist and serve.

    Fails CLOSED on missing evidence: a run that cannot show its baselines is a
    run that has not earned deployment.
    """
    if not result or result.get("status") != "trained":
        return {
            "passed": False,
            "policy": LEAD_TIME_SHIP_GATE_POLICY,
            "reason": (
                f"no trained model to gate (status={(result or {}).get('status')!r})"
            ),
        }

    raw_beaten = result.get("baselines_beaten")
    beaten: Dict[str, object] = dict(raw_beaten) if isinstance(raw_beaten, dict) else {}
    toughest = result.get("toughest_baseline")
    raw_paired = result.get("paired_vs_toughest_baseline")
    paired: Dict[str, object] = dict(raw_paired) if isinstance(raw_paired, dict) else {}
    skill = result.get("skill_vs_toughest_baseline")
    common = {
        "policy": LEAD_TIME_SHIP_GATE_POLICY,
        "best": result.get("best"),
        "toughest_baseline": toughest,
        "skill_vs_toughest_baseline": skill,
        "baselines_beaten": beaten,
        "paired": paired,
    }

    if not beaten:
        return {**common, "passed": False,
                "reason": "no baseline comparison recorded — fails closed"}

    lost_to = sorted(name for name, won in beaten.items() if not won)
    if lost_to:
        return {
            **common,
            "passed": False,
            "reason": (
                f"champion {result.get('best')!r} does not beat naive baseline(s) "
                f"{lost_to} on mean grouped-CV RMSE"
            ),
        }

    if not paired.get("available"):
        return {
            **common,
            "passed": False,
            "reason": (
                f"no paired comparison against {toughest!r} "
                f"({paired.get('reason', 'not computed')}) — fails closed"
            ),
        }
    if not paired.get("significant_ci"):
        return {
            **common,
            "passed": False,
            "reason": (
                f"the margin over {toughest!r} is not separated from zero: mean RMSE "
                f"reduction {paired.get('mean_rmse_reduction_days')} d, 95% CI "
                f"[{paired.get('ci95_low')}, {paired.get('ci95_high')}], winning "
                f"{paired.get('folds_model_won')}/{paired.get('n_folds')} folds"
            ),
        }

    return {
        **common,
        "passed": True,
        "reason": (
            f"champion {result.get('best')!r} beats all {len(beaten)} naive baselines; "
            f"vs toughest ({toughest}) mean RMSE reduction "
            f"{paired.get('mean_rmse_reduction_days')} d, 95% CI "
            f"[{paired.get('ci95_low')}, {paired.get('ci95_high')}], winning "
            f"{paired.get('folds_model_won')}/{paired.get('n_folds')} folds"
        ),
    }


def predict_lead_time(
    model,
    feature_row: Mapping[str, object],
    feature_cols: Sequence[str],
) -> float:
    """Predict FACTORY lead time in days for a single (component, offer) record.

    Raises :class:`UnknownCategoryError` when a refusing categorical meets an
    unseen level, :class:`MissingFeatureError` when the record lacks a required
    value, and :class:`FeatureSchemaMismatch` when ``feature_cols`` belong to a
    schema this build cannot construct. All three are the honest answer; none is
    papered over with a zero-filled vector.
    """
    X = align_row(feature_row, feature_cols)
    return max(float(model.predict(X)[0]), 1.0)


def compute_target(
    category: str,
    dist_km: float,
    macro_stress: float,
) -> float:
    """
    DEPRECATED / QUARANTINED — DO NOT use to train the lead-time model.

    This is the old *synthetic* target (base_days × stress_multiplier ×
    distance_modifier). Because it is a deterministic function of the model's
    own inputs, a model trained on it merely memorises this equation (R²≈1.0,
    pure leakage) — it learns nothing about real lead times. Route A replaced it
    with real observed lead times; see ``retrain_lead_time``.

    Retained only so the legacy orchestrator import does not hard-crash during
    migration. It emits a DeprecationWarning and is not called by any real
    training path.
    """
    warnings.warn(
        "compute_target is the deprecated synthetic lead-time formula and must "
        "not be used for training — use retrain_lead_time() on the observed "
        "collector panel instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    base = get_base_days(category)
    stress_mult = 1.0 + 1.5 * macro_stress
    dist_mod = 1.0 + (dist_km / 20_000.0)
    return base * stress_mult * dist_mod


# ── REAL observed-panel training path (Route A) ──────────────────────────────


def load_observed_panel(panel_path: Optional[Path] = None) -> Optional[pd.DataFrame]:
    """
    Load the accumulated observed lead-time panel written by the collector.
    Returns the DataFrame, or None if the panel does not exist / is empty.
    """
    import pandas as pd
    if panel_path is None:
        from app.ml.lead_time_collector import PANEL_PATH
        panel_path = PANEL_PATH
    panel_path = Path(panel_path)
    if not panel_path.exists():
        return None
    df = pd.read_csv(panel_path)
    return df if len(df) else None


#: Match qualities that count as a real observation of the requested part.
#: Anything else (fuzzy / no match) is a different part's lead time wearing our
#: MPN, which would be a label error, not a hard example.
#:
#: ``unverified`` is the bucket for rows collected BEFORE the collector recorded
#: a match type at all. Excluding them was a silent, expensive bug: every row in
#: the 2026-07-01 cross-section has a NULL ``match_type`` because that column did
#: not exist in July, so the filter deleted the entire first snapshot and the
#: model trained on ONE calendar date while believing it had two. A NULL here
#: means "not recorded", not "bad match".
#:
#: RETRACTION (2026-08-17). This comment used to claim the concession was
#: "self-limiting and cannot re-admit genuinely bad future matches, because every
#: current collector run writes the column". That was wrong, and reality falsified
#: it within two days: the weekly GitHub Action was still checked out at the
#: pre-rewrite collector and appended 363 NULL-``match_type`` rows dated
#: 2026-08-17 — rows that this filter therefore waved through as "unverified"
#: even though ~5-9% of them are provably a different DigiKey product (see the
#: module docstring). The concession is NOT self-limiting: it is only as safe as
#: the collector that happens to be running. What actually bounds it is the
#: workflow now pinning the verifying collector, not this tuple.
#:
#: The concession is still the right call — dropping a whole cross-section to
#: avoid a few percent of label noise costs more than it saves — but it is a
#: known-noise admission, not a clean one, and anything that reports panel
#: quality must say so rather than counting these rows as verified.
ACCEPTED_MATCH_TYPES: Tuple[str, ...] = ("exact", "contains", "unverified")

#: Columns describing the PART itself. These do not depend on when the part was
#: observed, so a snapshot that predates their collection can legitimately
#: inherit them from another snapshot of the SAME MPN.
#:
#: ASSUMPTION, stated explicitly: a part's category, package, parametric count,
#: tariff code and RoHS status did not change across 2026-07-01, 2026-08-15 and
#: 2026-08-17 — a 47-day span, and 2 days for the last pair. That is safe for
#: these fields and unsafe for anything price-, stock- or lifecycle-linked, which
#: is why the two lists below are separate and why nothing outside this tuple is
#: ever carried across dates.
PART_STATIC_PANEL_COLUMNS: Tuple[str, ...] = (
    "base_product",
    "manufacturer",
    "dk_manufacturer",
    "category",
    "dk_category",
    "dk_subcategory",
    "parameter_count",
    "package_case",
    "mounting_type",
    "htsus_code",
    "rohs_status",
    "series",
)

#: Columns that are properties of the OBSERVATION, not of the part. These are
#: NEVER carried across snapshot dates — back-propagating an August price, stock
#: level, stocking flag or lifecycle status onto a July row would fabricate the
#: very within-part variation the second cross-section exists to measure.
TIME_VARYING_PANEL_COLUMNS: Tuple[str, ...] = (
    "lead_time_weeks",
    "lead_time_weeks_raw",
    "stock",
    "quantity_available",
    "unit_price",
    "dk_unit_price",
    "lifecycle_status",
    "product_status_id",
    "normally_stocking",
    "discontinued",
    "end_of_life",
    "moq",
    "max_break_qty",
    "price_break_count",
    "packaging",
    "standard_package",
)


def enrich_static_attributes(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """Fill PART-STATIC columns across snapshots of the same MPN.

    The panel is not a flat table of independent rows — it is *mostly-static part
    attributes* x *dated observations of the target*. Two of the three
    cross-sections were collected by the 9-column pre-rewrite collector
    (2026-07-01, 75 rows; 2026-08-17, 363 rows) and one by the 56-column current
    collector (2026-08-15, 742 rows). Treating the 9-column rows as unusable threw
    away the only within-part temporal variation in the dataset, including the 56
    STMicroelectronics parts whose quote moved 30 -> 40/52 weeks.

    Concretely, of the 363 rows dated 2026-08-17, 357 share an MPN with a
    56-column observation and so arrive complete; ``base_product`` — the
    cross-validation group key — is recovered for 255 of them, and the remaining
    108 fall back to ``mpn:`` / ``row:`` group keys, which can only ever make a
    fold group smaller, never merge two real families.

    So each MPN's static attributes are shared across its observations, while
    every time-varying column — the target above all — stays strictly per-row.

    What this DOES buy: those 56 real quote changes enter the model as genuine
    within-part variation, and the schema is now correct for every future weekly
    snapshot.

    What it does NOT buy, and must not be claimed: this is not temporal
    validation. There are 3 dates but they are not three independent ones — 75 of
    748 parts appear at all three, 357 at the two August dates, and those two
    dates are 2 days apart and agree on 90.8% of the shared parts. No split here
    holds out a time period. See ``docs/`` and the module docstring.

    Returns ``(enriched_df, counts)``.
    """
    if "mpn" not in df.columns:
        return df, {"static_cells_filled": 0, "parts_enriched": 0}

    out = df.copy()
    cols = [c for c in PART_STATIC_PANEL_COLUMNS if c in out.columns]
    if not cols:
        return out, {"static_cells_filled": 0, "parts_enriched": 0}

    before = out[cols].notna().sum().sum()
    key = out["mpn"].astype("object").map(_level_of)
    # ffill+bfill within each MPN: a value observed at ANY date fills the dates
    # where it was never collected. Only these columns; only within one part.
    out[cols] = out.groupby(key)[cols].transform(lambda g: g.ffill().bfill())
    after = out[cols].notna().sum().sum()

    filled = int(after - before)
    parts = int((out.groupby(key)[cols].transform("count").gt(0).any(axis=1)).sum()) if filled else 0
    return out, {"static_cells_filled": filled, "parts_enriched": parts}


def _group_key(d: pd.DataFrame) -> List[str]:
    """Leakage-free split key: the PART FAMILY, not the individual MPN.

    This is the difference between a defensible score and a fake one. The panel
    contains 456 STM32F103 rows, 147 ATMEGA328 and 93 TMS320 — siblings that
    share a factory lead time. ``base_product`` alone explains R² = 0.856 of the
    target IN SAMPLE (an ANOVA statistic, not a model score — see the module
    docstring), so an MPN-level (or random) split puts siblings on both sides and
    scores the model's ability to RECOGNISE a part family, not to predict a lead
    time for an unseen one.

    Grouping on ``base_product`` collapses 742 MPNs into 361 base_product levels
    (360 real families plus the ``Unknown`` bucket) — 472 group keys once the
    MPN/row fallbacks below are counted, i.e. 360 real families + 112 MPNs split
    out of ``Unknown`` — and forces every fold to generalise across families.
    This key itself explains R² = 0.908 in sample.
    Falls back to the MPN when DigiKey returned no base product, and to
    the row index as a last resort — a fallback can only ever make a group
    SMALLER, never merge two real families.
    """
    import pandas as pd
    if "base_product" in d.columns:
        base = d["base_product"].astype("object").map(_level_of)
    else:
        base = pd.Series([UNKNOWN_LEVEL] * len(d), index=d.index)
    mpn = (
        d["mpn"].astype("object").map(_level_of)
        if "mpn" in d.columns
        else pd.Series([UNKNOWN_LEVEL] * len(d), index=d.index)
    )
    out: List[str] = []
    for i, (b, m) in enumerate(zip(base.tolist(), mpn.tolist(), strict=True)):
        if b != UNKNOWN_LEVEL:
            out.append(f"family:{b}")
        elif m != UNKNOWN_LEVEL:
            out.append(f"mpn:{m}")
        else:
            out.append(f"row:{i}")
    return out


#: Panel columns that IDENTIFY a part rather than describe it. They are never
#: features — one-hot-encoding an MPN is memorisation, which is the entire failure
#: mode this module is built to measure rather than commit.
#:
#: They are carried onto each record under :data:`IDENTITY_PREFIX` purely so an
#: evaluation can quantify how much of the target a bare identity column explains
#: IN SAMPLE (see ``seeds/run_leakage_progression.py``). Riding along on the
#: record keeps them row-aligned with ``y`` through every filter and drop, which a
#: parallel array recomputed from the panel would not be.
#:
#: ``_fill`` reads only the keys the resolved schema names, and schema resolution
#: only inspects declared specs' ``record_key``s, so these keys are inert for
#: training and serving. ``_assert_no_identity_feature_leakage`` pins that.
IDENTITY_PANEL_COLUMNS: Tuple[str, ...] = (
    "mpn",
    "base_product",
    "series",
    "manufacturer",
    "package_case",
    "dk_category",
    "category",
)

#: Namespace for the columns above. Deliberately distinct from every ``record_key``.
IDENTITY_PREFIX = "id="


def _assert_no_identity_feature_leakage() -> None:
    """Fail at import if an identity key could ever be read as a feature."""
    record_keys = {s.record_key for s in NUMERIC_SPECS.values()}
    record_keys |= {c.record_key for c in CATEGORICAL_SPECS.values()}
    for column in IDENTITY_PANEL_COLUMNS:
        key = f"{IDENTITY_PREFIX}{column}"
        if key in record_keys:
            raise RuntimeError(
                f"identity key {key!r} collides with a declared feature record_key — "
                "an identity column must never reach the encoder"
            )


_assert_no_identity_feature_leakage()


def panel_to_records(
    df: pd.DataFrame,
) -> Tuple[List[Dict[str, object]], np.ndarray, List[str], List[str], Dict[str, int]]:
    """Turn the observed panel into ``(records, y, groups, snapshot_dates, counts)``.

    The panel is treated as *mostly-static part attributes* x *dated observations
    of the target* (see :func:`enrich_static_attributes`), so a snapshot that
    predates a column's collection inherits that column from another observation
    of the same MPN — but only for genuinely static attributes, never for the
    target or anything price/stock/lifecycle-linked.

    Every declared spec whose ``panel_column`` is present is copied onto the
    record under its ``record_key``; absent columns are simply absent, and
    :func:`resolve_schema_from_records` excludes the corresponding feature with a
    stated reason. Rows are dropped — never imputed — when the label is missing,
    non-positive, or when the collector's MPN match was not good enough to
    believe the quote belongs to the part we asked for.
    """
    import pandas as pd
    d = df.copy()
    n0 = len(d)
    d["lead_time_weeks"] = pd.to_numeric(d["lead_time_weeks"], errors="coerce")
    d = d[d["lead_time_weeks"].notna() & (d["lead_time_weeks"] > 0)]
    n_no_label = n0 - len(d)

    # Share static part attributes across this part's observations BEFORE the
    # match filter, so a pre-match_type snapshot arrives complete.
    d, enrich_counts = enrich_static_attributes(d)

    # Only trust quotes the collector matched confidently to the requested MPN.
    # A row with no recorded match_type predates the column and is "unverified",
    # not "bad" — see ACCEPTED_MATCH_TYPES.
    n_bad_match = 0
    if "match_type" in d.columns:
        before = len(d)
        match = d["match_type"].astype("object").map(_level_of).str.lower()
        match = match.where(match != UNKNOWN_LEVEL.lower(), "unverified")
        d = d[match.isin(ACCEPTED_MATCH_TYPES)]
        n_bad_match = before - len(d)

    columns: Dict[str, str] = {}
    for nspec in NUMERIC_SPECS.values():
        if nspec.panel_column in d.columns:
            columns[nspec.record_key] = nspec.panel_column
    for cspec in CATEGORICAL_SPECS.values():
        if cspec.panel_column in d.columns:
            columns[cspec.record_key] = cspec.panel_column

    records: List[Dict[str, object]] = []
    for _, row in d.iterrows():
        record = {key: row[col] for key, col in columns.items()}
        # `unit_price` is DigiKey's own figure where the collector recorded one,
        # otherwise this row's OWN observed price. Both are the part's USD unit
        # price AT THIS SNAPSHOT; what must never happen is a later date's price
        # being carried backwards, which is why dk_unit_price is in
        # TIME_VARYING_PANEL_COLUMNS.
        if record.get("unit_price") is None or (
            isinstance(record.get("unit_price"), float) and np.isnan(record["unit_price"])
        ):
            fallback = row.get("unit_price") if "unit_price" in d.columns else None
            if fallback is not None and not (
                isinstance(fallback, float) and np.isnan(fallback)
            ):
                record["unit_price"] = fallback
        # Identity columns ride along, namespaced and inert (see
        # IDENTITY_PANEL_COLUMNS). They are never encoded.
        for identity_column in IDENTITY_PANEL_COLUMNS:
            if identity_column in d.columns:
                record[f"{IDENTITY_PREFIX}{identity_column}"] = _level_of(row[identity_column])
        records.append(record)

    groups = _group_key(d)
    if "snapshot_date" in d.columns:
        dates = [_level_of(v) for v in d["snapshot_date"].tolist()]
    else:
        dates = [UNKNOWN_LEVEL] * len(d)

    y = d["lead_time_weeks"].to_numpy(float) * 7.0
    counts = {
        "rows_in": n0,
        "dropped_no_label": n_no_label,
        "dropped_bad_match": n_bad_match,
        "rows_used": len(d),
        "distinct_families": len(set(groups)),
        "distinct_snapshot_dates": len(set(dates)),
        **enrich_counts,
    }
    return records, y, groups, dates, counts


def build_observed_matrix(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """Build ``(X, y, feature_cols)`` from the observed panel.

    Target y = observed factory ``lead_time_weeks`` × 7 (calendar days).
    The design matrix comes from :func:`build_design_matrix` — the SAME function
    serving calls — so the two schemas cannot diverge.
    """
    records, y, groups, dates, _ = panel_to_records(df)
    if not records:
        return np.empty((0, 0)), np.empty((0,)), []
    schema, _ = resolve_schema_from_records(records, snapshot_dates=dates)
    # Rows the resolved schema cannot encode are DROPPED, exactly as
    # retrain_lead_time does — never imputed, and never left in to explode
    # inside _fill. X and y must stay row-aligned after the drop.
    records, y, _groups, _dropped = _drop_unfillable(records, y, groups, schema)
    if not records:
        return np.empty((0, 0)), np.empty((0,)), []
    X, cols = build_design_matrix(records, schema=schema)
    return X, y, cols


def _drop_unfillable(
    records: List[Dict[str, object]],
    y: np.ndarray,
    groups: Sequence[str],
    schema: ResolvedSchema,
) -> Tuple[List[Dict[str, object]], np.ndarray, List[str], int]:
    """Drop training rows the resolved schema cannot encode (never impute them)."""
    keep_records: List[Dict[str, object]] = []
    keep_idx: List[int] = []
    for i, record in enumerate(records):
        try:
            _fill([record], schema)
        except (MissingFeatureError, UnknownCategoryError):
            continue
        keep_records.append(record)
        keep_idx.append(i)
    kept_groups = [groups[i] for i in keep_idx]
    return keep_records, y[keep_idx], kept_groups, len(records) - len(keep_records)


@dataclass(frozen=True)
class TrainingDesign:
    """Everything the panel becomes before an estimator ever sees it.

    This is the object that guarantees an *evaluation* script and the *training*
    path are looking at the same 2,615 rows. ``seeds/run_leakage_progression.py``
    exists to publish a number about how this data generalises; if it filtered
    the panel even slightly differently from :func:`retrain_lead_time`, the
    number would describe a dataset that is not the one being trained on. So both
    go through :func:`build_training_design` and neither owns a private copy of
    the filtering rules.
    """
    records: List[Dict[str, object]]
    y: np.ndarray
    family_groups: List[str]
    manufacturer_groups: List[str]
    schema: ResolvedSchema
    exclusions: List[Dict[str, object]]
    snapshot_dates: List[str]
    counts: Dict[str, int]
    #: ``{panel column: per-row values}`` for :data:`IDENTITY_PANEL_COLUMNS`,
    #: row-aligned with ``y``. Diagnostics only — never encoded as features.
    identity_columns: Dict[str, List[str]]


def build_training_design(df: pd.DataFrame) -> TrainingDesign:
    """Panel DataFrame -> the exact rows, labels and group keys training uses.

    Applies, in order: label filtering and the match-quality filter
    (:func:`panel_to_records`), data-driven schema resolution
    (:func:`resolve_schema_from_records`), and the drop of rows the resolved
    schema cannot encode (:func:`_drop_unfillable`). Nothing is imputed at any
    step; every drop is counted in ``counts``.

    The manufacturer group key is read off the *records* rather than the raw
    panel, so it stays row-aligned with ``y`` through every drop above.
    """
    records, y, groups, dates, counts = panel_to_records(df)
    schema, exclusions = resolve_schema_from_records(records, snapshot_dates=dates)
    records, y, groups, n_unfillable = _drop_unfillable(records, y, groups, schema)
    counts = dict(counts)
    counts["dropped_unfillable"] = n_unfillable
    counts["rows_trained"] = len(records)
    counts["distinct_families_trained"] = len(set(groups))
    manufacturers = [str(r.get("manufacturer") or UNKNOWN_LEVEL) for r in records]
    counts["distinct_manufacturers_trained"] = len(set(manufacturers))
    identity: Dict[str, List[str]] = {}
    for column in IDENTITY_PANEL_COLUMNS:
        key = f"{IDENTITY_PREFIX}{column}"
        if any(key in r for r in records):
            identity[column] = [str(r.get(key, UNKNOWN_LEVEL)) for r in records]
    return TrainingDesign(
        records=records,
        y=y,
        family_groups=list(groups),
        manufacturer_groups=manufacturers,
        schema=schema,
        exclusions=exclusions,
        snapshot_dates=list(dates),
        counts=counts,
        identity_columns=identity,
    )


def retrain_lead_time(
    panel_path: Optional[Path] = None,
    min_samples: int = 30,
) -> Dict:
    """
    REAL entrypoint: train the lead-time regressors on observed data only.

    Reads the collector panel. If it is missing / empty / too small, SKIPS
    training with an honest status (never falls back to the synthetic formula).
    Otherwise resolves the feature schema from the data, trains all four models,
    computes the naive baselines, and returns metrics + fitted models.

    Returns:
        {"status": "skipped", "reason": ..., "n_samples": int}
      or
        {"status": "trained", "n_samples", "n_features", "models", "baselines",
         "feature_cols", "feature_schema_version", "feature_exclusions",
         "best", "beats_baselines", "baselines_beaten", "toughest_baseline",
         "skill_vs_toughest_baseline", "panel_rows"}
    """
    df = load_observed_panel(panel_path)
    if df is None:
        logger.warning(
            "no observed lead times yet — collector must run first; "
            "SKIPPING lead-time training (no synthetic fallback)."
        )
        return {"status": "skipped", "reason": "no_observed_panel", "n_samples": 0}

    if not panel_to_records(df)[0]:
        return {"status": "skipped", "reason": "no_usable_rows", "n_samples": 0}

    # SAME object seeds/run_leakage_progression.py evaluates, by construction.
    design = build_training_design(df)
    records, y, groups = design.records, design.y, design.family_groups
    schema, exclusions, counts = design.schema, design.exclusions, design.counts

    if len(y) < min_samples:
        logger.warning(
            "only %d usable observed lead-time rows (< %d needed) — SKIPPING training. "
            "Let the collector accumulate more weekly snapshots first.",
            len(y), min_samples,
        )
        return {"status": "skipped", "reason": "insufficient_observations", "n_samples": int(len(y))}

    X, feature_cols = build_design_matrix(records, schema=schema)
    results = train_all_models(X, y, groups=groups)
    baselines = compute_baselines(X, y, feature_cols, groups=groups)
    best = min(results, key=lambda k: results[k]["cv_rmse_mean"])

    best_cv = results[best]["cv_rmse_mean"]
    beaten = {b: best_cv < info["cv_rmse_mean"] for b, info in baselines.items()}
    beats_baselines = all(beaten.values())

    # Skill against the TOUGHEST baseline, not the easiest. The category-mean
    # lookup table is the fair comparison for a mostly-one-hot feature set.
    toughest = min(baselines, key=lambda b: baselines[b]["cv_rmse_mean"])
    tough_rmse = baselines[toughest]["cv_rmse_mean"]
    skill = round(1.0 - (best_cv / tough_rmse), 3) if tough_rmse > 0 else None

    # PAIRED comparison on the identical folds. Comparing two marginal standard
    # deviations ("the error bars overlap") is the wrong test when both models
    # are scored on the same splits — most of that spread is split-to-split
    # difficulty, which cancels in the pairing. The per-fold difference is the
    # honest statistic, and it is reported with the fraction of folds won.
    paired = _paired_skill(
        results[best].get("cv_rmse_per_split"),
        baselines[toughest].get("cv_rmse_per_split"),
    )

    manufacturers = design.manufacturer_groups
    audit = leakage_audit(X, y, groups, manufacturers, model_name=best)
    logger.warning("LEAKAGE AUDIT — %s", audit["headline"])

    logger.info(
        "lead-time retrain on %d REAL observations (%d features, schema v%d) — best=%s "
        "cv_rmse=%.2f±%.2f  cv_r2_median=%.3f  beats_all_baselines=%s",
        len(y), X.shape[1], FEATURE_SCHEMA_VERSION, best,
        best_cv, results[best]["cv_rmse_std"], results[best]["cv_r2_median"], beats_baselines,
    )
    logger.info("  resolved features: %s", feature_cols)
    for exc in exclusions:
        logger.info("  EXCLUDED %-20s (%s): %s", exc["feature"], exc["kind"], exc["reason"])
    for bname, binfo in baselines.items():
        logger.info("  baseline %-14s cv_rmse=%6.2f  beaten=%s",
                    bname, binfo["cv_rmse_mean"], beaten[bname])
    if paired.get("available"):
        logger.info(
            "  vs toughest baseline (%s): skill=%s | PAIRED on the same %s folds — mean RMSE "
            "reduction %.2f ± %.2f d, model wins %s/%s folds (%.0f%%), paired-t p=%s. "
            "Champion cv_rmse %.2f±%.2f vs baseline %.2f±%.2f (n=%d rows, %d distinct families).",
            toughest, skill, paired["n_folds"],
            paired["mean_rmse_reduction_days"], paired["std_error"],
            paired["folds_model_won"], paired["n_folds"],
            100 * float(paired["fold_win_rate"]),  # type: ignore[arg-type]
            paired.get("paired_t_p_value"),
            best_cv, results[best]["cv_rmse_std"],
            tough_rmse, baselines[toughest]["cv_rmse_std"],
            len(y), counts.get("distinct_families_trained", 0),
        )
    else:
        logger.info("  vs toughest baseline (%s): skill=%s (paired comparison unavailable: %s)",
                    toughest, skill, paired.get("reason"))

    return {
        "status": "trained",
        "n_samples": int(len(y)),
        "n_features": int(X.shape[1]),
        "models": results,
        "baselines": baselines,
        "feature_cols": feature_cols,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_exclusions": exclusions,
        "best": best,
        "beats_baselines": beats_baselines,
        "baselines_beaten": beaten,
        "toughest_baseline": toughest,
        "skill_vs_toughest_baseline": skill,
        "paired_vs_toughest_baseline": paired,
        "leakage_audit": audit,
        "n_manufacturers": int(len(set(manufacturers))),
        "panel_rows": counts,
    }
