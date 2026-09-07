"""
Benchmark API endpoints (04-02).

Four public endpoints for the benchmark dashboard:
  GET /benchmark/summary               — Aggregate A/B delta metrics for latest run_id
  GET /benchmark/fiedler-curve         — Sequential-removal λ₂ curve from GraphState
  GET /benchmark/cascade-heatmap       — Per-distributor unfulfilled-BOM-line exposure for maplibre
  GET /benchmark/single-source-components — Real component MPN+manufacturer+sole-source distributor

All endpoints are unauthenticated — public aggregate analytics, no user data (T-04-02-03/04).
"""
from __future__ import annotations

import json
import logging
import random
from functools import lru_cache
from pathlib import Path
from statistics import fmean, mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/benchmark", tags=["benchmark"])

# ── Named, disclosed modelling assumption ─────────────────────────────────────
# The benchmark scores ONE representative reorder per BOM. To annualize the
# per-BOM MILP savings we assume each BOM is re-ordered this many times per
# year. This is an openly-stated assumption surfaced in the API response
# (`annual_reorders`), not a measured procurement cadence.
ANNUAL_REORDERS = 12

# ── The retracted headline ────────────────────────────────────────────────────
# `docs/BENCHMARK_VOLUME_CURVE.md` retracted "the optimizer is 44.7% cheaper" in
# July 2026. The retraction landed in every document but never in
# this API, which kept serving `savings_pct: 48.09` as a bare headline off a run of
# 4-line, 5-to-9-unit prototype BOMs. This endpoint now serves the volume curve and
# the decomposition instead, with the prototype-volume figure explicitly labelled as
# an artifact of a per-supplier fixed fee.
RETRACTED_HEADLINE = "the CP-SAT optimizer is ~44.7% cheaper than a greedy baseline"

# Repo root: app/api/benchmark.py -> app -> backend -> <repo>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VOLUME_SWEEP_PATH = _REPO_ROOT / "docs" / "volume_sweep.json"
_DIVERSIFICATION_FRONTIER_PATH = _REPO_ROOT / "docs" / "diversification_frontier.json"

# Production volume, for the honest headline range. The sweep's own caveat: the
# high-multiplier rows are a smaller BOM cohort than the low ones (stock ceilings knock
# BOMs out as volume rises), so the trustworthy statement is a RANGE over this band,
# never a single number.
_PRODUCTION_MULTIPLIER_MIN = 500

# ── Materiality threshold — an ASSUMPTION, not a measurement ──────────────────
# This used to be served as `noise_floor_pct` and rendered as "this run's 2.0%
# noise floor", which invited the question "how did you establish it?" and had no
# answer: it was never derived from solver tolerance or replicate variance.
#
# It is not derivable here, and that is a property of the benchmark rather than an
# oversight. The run is a single deterministic solve — seed 42, CP-SAT with
# `num_search_workers = 1` and no relative-gap limit — so re-running it reproduces
# every figure bit-for-bit. There are no replicates, so the measured run-to-run
# variance is exactly 0.0%; a "noise floor" derived from it would be 0.0 and would
# declare every difference, however tiny, material.
#
# So it is published as what it actually is: a reporting convention, fixed before
# the run and held constant across runs so it cannot be tuned toward a result.
MATERIALITY_THRESHOLD_PCT = 2.0
_MATERIALITY_BASIS = (
    "ASSUMED THRESHOLD, NOT A MEASURED NOISE FLOOR. 2.0% of landed cost is a "
    "reporting convention chosen a priori and held fixed across runs, so it cannot "
    "be tuned toward a result. Nothing in this pipeline measures it: the benchmark "
    "is a single deterministic solve (seed 42, CP-SAT num_search_workers=1, no "
    "relative-gap limit), so re-running reproduces every figure exactly and there "
    "are no replicates from which run-to-run variance could be estimated. Read it "
    "as 'differences smaller than this are not worth acting on', never as "
    "'differences smaller than this are indistinguishable from noise'."
)


# ── Paired bootstrap over BOM clusters — the ship standard, applied here too ──
# The lead-time model may only ship by beating its baselines with a PAIRED
# BOOTSTRAP CI THAT EXCLUDES ZERO, and the Model Card says so prominently. Until
# 2026-08-28 this endpoint published every resilience delta as a bare
# uninterval'd mean over 9 BOMs — no CI, no standard error, no replicate, one
# fixed seed — while the ML page two clicks away was held to the harder bar. A
# reviewer who reads both pages is entitled to ask why. There is no good answer,
# so the benchmark is now held to the same bar.
#
# NOTHING IS RE-SOLVED. Every per-BOM delta is already stored in
# `optimization_runs`; the bootstrap resamples the BOM CLUSTERS with replacement
# over those stored values. The BOM is the independent unit — both arms of a
# delta come from the SAME BOM under the SAME simulation seed, so the BOM is what
# gets resampled — and resampling it leaves run 5 byte-identical.
#
# Percentile interval, not BCa: with n <= 9 clusters the acceleration term would
# be estimated from at most 9 jackknife points, which is false precision. `n` and
# `n_effective` are published beside every interval for the same reason — an
# interval over 7 BOMs is a description of these BOMs, not a population claim.
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 42
CI_ALPHA = 0.05

_BOOTSTRAP_METHOD = (
    "paired percentile bootstrap over BOM clusters, 10,000 resamples, seed 42, "
    "computed from the per-BOM deltas already stored for this run — no re-solve"
)

# Why a BOM can be "structurally zero". Two of run 5's nine BOMs
# (`drone_flight_controller`, `rf_transceiver_module`) select the SAME plan in
# both arms, so their delta is an exact 0.0 on every metric in every scenario.
# They are not evidence of a null effect; they are BOMs the treatment never
# touched. They drag every mean toward zero AND shrink the apparent spread, which
# is the worst of both. `n_effective` counts only the BOMs whose plan actually
# differs, and both the full-panel and effective-panel intervals are published so
# nobody has to take the distinction on trust.
_N_EFFECTIVE_DEFINITION = (
    "n = BOMs in the paired panel. n_effective = BOMs whose graph-aware plan "
    "actually DIFFERS from the blind plan (different distributor set, or the same "
    "set at a different landed cost). A BOM whose plan is identical in both arms "
    "contributes an exact 0.0 to every delta and carries no information about the "
    "treatment; it is counted, not hidden, and the effective-panel interval is "
    "published beside the full-panel one."
)


# ── Pydantic Response Schemas ─────────────────────────────────────────────────

class MonteCarloSummary(BaseModel):
    baseline_p10: float
    baseline_p50: float
    baseline_p90: float
    graph_aware_p10: float
    graph_aware_p50: float
    graph_aware_p90: float
    baseline_cvar_95: Optional[float] = None
    graph_aware_cvar_95: Optional[float] = None


class TradeoffEntry(BaseModel):
    """
    The BOM on which the graph-aware arm does WORST against the blind MILP.

    `narrative` is DERIVED FROM THE STORED PLANS, never templated. Until
    2026-09-03 it was a hardcoded sentence — "the cheapest distributor carries a
    high-centrality component and graph-aware routes around it" — printed under a
    heading that says HONEST TRADEOFF. It was false on the run it was serving:
    on run 7 the tradeoff BOM is iot_sensor_node, where the blind plan is
    {DigiKey} and the graph-aware plan is {DigiKey, Verical}. Nothing was routed
    around; a supplier was ADDED and the extra landed cost is one more fixed
    per-supplier freight fee. The fields below now publish the two plans so the
    sentence can be checked against them without leaving the response.
    """
    bom_name: str
    losing_axis: str          # "cost" | "eta" | "risk"
    baseline_value: float
    graph_aware_value: float
    delta_pct: float
    narrative: str            # pre-formatted string for UI tradeoff card body
    # The plans behind the sentence. `blind_only` = distributors the blind MILP
    # used that graph-aware dropped (a genuine "routed around"); `added` =
    # distributors graph-aware opened on top; `kept` = the intersection.
    blind_distributors: List[str] = []
    graph_aware_distributors: List[str] = []
    distributors_kept: List[str] = []
    distributors_dropped: List[str] = []
    distributors_added: List[str] = []
    # Panel-wide attribution: which of the graph-aware arm's two ingredients —
    # the SOFT betweenness-centrality surcharge or the HARD dual-source
    # constraint — actually moved the plans. Computed from the stored rows.
    mechanism: str = ""


class BomDelta(BaseModel):
    bom_name: str
    cost_delta_pct: float
    eta_delta_pct: float
    co2_delta_pct: float
    cascade_risk_delta_pct: float


class VolumeCurvePoint(BaseModel):
    """One multiplier on the greedy-vs-MILP volume sweep.

    Costs are POOLED — sum(greedy) / sum(MILP) across the BOMs feasible at that
    multiplier — not a mean of per-BOM percentages. Mixing the two aggregations is
    how the original inconsistency happened.

    Several fields carry a short alias as well as the descriptive name (e.g.
    `fixed_fee_usd` beside `saving_from_fixed_fees_usd`). That is deliberate: the
    Benchmark page's curve normalizer reads the short names, and serving both means
    the UI renders THIS endpoint's live numbers rather than a checked-in fallback
    copy of the same artifact that can silently drift.
    """
    multiplier: int
    boms_feasible: int
    # Units per BOM at this multiplier — the "these are toy orders" evidence.
    units_min: int
    units_max: int
    greedy_cost_usd: float
    milp_cost_usd: float
    pooled_savings_pct: float
    savings_pct: float                       # alias of pooled_savings_pct
    saving_from_fixed_fees_usd: float
    fixed_fee_usd: float                     # alias
    saving_from_component_cost_usd: float
    component_usd: float                     # alias
    saving_from_variable_freight_usd: float
    variable_freight_usd: float              # alias
    fixed_fee_share_of_saving_pct: Optional[float] = None
    fee_share_of_saving_pct: Optional[float] = None   # alias
    # Fixed per-supplier fees as a share of the GREEDY baseline's total landed cost.
    # At 1x this is ~80% — i.e. four fifths of the cost being "optimized" is a fee
    # for opening a supplier account, which is the whole retraction in one number.
    greedy_fixed_share_of_cost_pct: Optional[float] = None
    suppliers_greedy: int
    suppliers_milp: int


class VolumeCurve(BaseModel):
    """The savings-vs-volume decay curve, with the caveats that make it readable."""
    available: bool
    source: str
    generated_utc: Optional[str] = None
    aggregate_definition: str
    points: List[VolumeCurvePoint] = []
    cohort_caveat: str = ""
    unavailable_reason: Optional[str] = None
    # The single constant the retracted headline was really measuring: the fixed
    # per-supplier freight fee (LTL base x the strategy's transport penalty scale).
    fixed_fee_per_supplier_usd: Optional[float] = None
    # The same fee for an INTERNATIONAL supplier (air-freight base x the same
    # scale). It is what makes the greedy arm's wider offer pool expensive rather
    # than cheap, and `pool_asymmetry` needs both numbers to be readable.
    international_fixed_fee_per_supplier_usd: Optional[float] = None


class SavingsDecomposition(BaseModel):
    """Where a saving actually comes from, in dollars, at one volume."""
    multiplier: int
    total_saving_usd: float
    from_fixed_supplier_fees_usd: float
    from_component_cost_usd: float
    from_variable_freight_usd: float
    dominant_term: str
    note: str


class Headline(BaseModel):
    """
    The honest answer to 'how much does the optimizer save?', which is a RANGE that
    depends on order volume — not one percentage.
    """
    statement: str
    retracted_claim: str
    retraction_reason: str
    savings_pct_at_prototype_volume: Optional[float] = None
    prototype_volume_units: Optional[int] = None
    savings_pct_at_production_volume_low: Optional[float] = None
    savings_pct_at_production_volume_high: Optional[float] = None
    production_volume_units_low: Optional[int] = None
    production_volume_units_high: Optional[int] = None
    dominant_mechanism_at_prototype_volume: str
    dominant_mechanism_at_production_volume: str
    do_not_quote_a_single_percentage: bool = True


class BaselineComparison(BaseModel):
    """
    One heuristic baseline scored against the SAME blind-MILP plans.

    FOUR baselines are solved by `seeds/run_benchmark.py` and stored on every run
    from run_id=8 onward, along two independent axes — the heuristic, and the
    OFFER POOL it was allowed to shop:

      heuristic:  `greedy`      myopic per-line cheapest offer, no consolidation
                  `greedy_add`  Kuehn & Hamburger (1963) ADD / local search
      pool:       (none)        the FULL international catalogue, us_only=False
                  `_dom`        DOMESTIC only, us_only=True — the same catalogue
                                the MILP's `balanced` strategy restricts it to

    Both axes are handicaps that inflate the optimizer's apparent edge, and until
    2026-09-03 the published number carried both: this API served ONLY `greedy`
    (weakest heuristic) and the pipeline solved it on the full international pool
    (wider catalogue) while the MILP it was compared against never left the US.

    `greedy_add_dom` is therefore the ONLY like-for-like baseline — same
    catalogue, same cost function, competent heuristic — and it is the one
    flagged `is_primary`. The other three are published beside it because the gap
    between them is the finding; a reader who sees only the biggest number learns
    the wrong thing, and a reader who sees only the smallest cannot tell why.

    `pooled_savings_pct` is the headline statistic — see `_POOLED_DEFINITION`.
    `mean_of_boms_savings_pct` is published beside it so the gap between the two
    aggregations is visible rather than latent, and it is NEVER the headline.
    """
    # the `arm` column value: "greedy" | "greedy_add" | "greedy_dom" | "greedy_add_dom"
    arm: str
    label: str
    description: str
    # The catalogue this baseline shopped, in words, and whether it is the same
    # one the MILP was restricted to. A saving quoted against `matched_pool=False`
    # is not an optimization result on its own.
    pool: str = ""
    matched_pool: bool = False
    # Exactly one baseline carries this: the apples-to-apples comparison.
    is_primary: bool = False
    n_boms: int
    total_cost_usd: float
    milp_total_cost_usd: float
    pooled_savings_pct: float
    mean_of_boms_savings_pct: float
    savings_usd_per_bom: float
    savings_usd_annualized: float
    avg_suppliers: float
    suppliers_opened: int
    international_suppliers_opened: int


class PoolAsymmetry(BaseModel):
    """
    The published benchmark's two arms do NOT shop the same catalogue.

    `seeds/run_benchmark.py` solves the greedy arms with `us_only=False` (the full
    international offer pool) but takes the MILP's plan from the `balanced`
    strategy, whose `us_only_sourcing=True` restricts it to domestic distributors.
    So the greedy baseline can — and does — open Chinese and Singaporean suppliers
    at the international fixed freight fee, while the MILP never sees them. That
    is a shipping-policy difference sitting inside a number presented as an
    optimization result, and it was disclosed nowhere on the page.

    `matched_pool_*` is the control, read from the COMMITTED `docs/volume_sweep.json`,
    which solves the same BOMs with a `milp_matched` arm (`us_only=False`, same pool
    as greedy) beside a `milp_bench` arm (`us_only=True`, reproducing the published
    benchmark MILP). It answers "how much of the headline is the pool?" for the
    MILP side without re-running anything.

    RESOLVED 2026-09-03 (run_id >= 8). Both directions are now measured, and they
    do not carry equal weight:

      * MILP side — NON-BINDING. Re-solving the benchmark's MILP on greedy's full
        global pool returns the identical plan and the identical landed cost to
        the cent. 0.00 points of the headline come from restricting the optimizer.
      * GREEDY side — THE WHOLE ASYMMETRY. `seeds/run_benchmark.py` now solves
        `greedy_dom` and `greedy_add_dom`, the two heuristics re-solved on the
        MILP's own domestic-only pool, and persists them beside the global-pool
        arms. `matched_*` below carries that result.

    `matched` is True only when this run actually carries the matched arms. On an
    earlier run it stays False and every `matched_*` field is null — "we did not
    measure this" must never be served as a number.
    """
    matched: bool
    statement: str
    greedy_pool: str
    milp_pool: str
    greedy_suppliers_opened: int
    greedy_international_suppliers_opened: int
    milp_suppliers_opened: int
    milp_international_suppliers_opened: int
    domestic_fixed_fee_usd: Optional[float] = None
    international_fixed_fee_usd: Optional[float] = None
    # The control, from docs/volume_sweep.json at multiplier 1.
    control_source: Optional[str] = None
    control_n_boms: Optional[int] = None
    control_greedy_cost_usd: Optional[float] = None
    control_milp_domestic_pool_cost_usd: Optional[float] = None
    control_milp_full_pool_cost_usd: Optional[float] = None
    control_savings_pct_domestic_pool: Optional[float] = None
    control_savings_pct_full_pool: Optional[float] = None
    control_finding: Optional[str] = None
    unmatched_side: str = ""
    # ── The greedy side of the match, MEASURED (run_id >= 8) ──────────────────
    # Solved by seeds/run_benchmark.py's `greedy_dom` / `greedy_add_dom` arms and
    # read out of this run's own rows — not from a scratch script, not from prose.
    matched_baseline_arm: Optional[str] = None
    matched_n_boms: Optional[int] = None
    matched_greedy_cost_usd: Optional[float] = None
    matched_greedy_add_cost_usd: Optional[float] = None
    matched_milp_cost_usd: Optional[float] = None
    matched_savings_pct_vs_greedy: Optional[float] = None
    matched_savings_pct_vs_greedy_add: Optional[float] = None
    # Percentage points of the unmatched `greedy` headline explained by each
    # handicap. They sum to it by construction.
    points_from_weaker_heuristic: Optional[float] = None
    points_from_wider_baseline_catalogue: Optional[float] = None
    points_from_optimizer: Optional[float] = None
    matched_finding: Optional[str] = None


class PairedBootstrapCI(BaseModel):
    """
    A 95% paired percentile-bootstrap interval around one published delta.

    The resample unit is the BOM CLUSTER, not the row: both arms of every
    per-BOM delta come from the same BOM under the same simulation seed, so the
    BOM is the independent unit. 10,000 resamples, seed 42, computed from the
    per-BOM values already stored for the run — the benchmark is NOT re-solved
    and no published mean moves.

    `significant` is True only when the interval EXCLUDES zero. A delta whose
    interval covers zero is not a result and must not be rendered as one.

    `n_effective` and the `*_effective` fields describe the sub-panel of BOMs
    whose plan actually differs between arms. See `n_effective_definition` on the
    parent section.
    """
    metric: str
    units: str                      # "share_0_1" | "cost_multiplier" | "percent"
    mean: float
    ci95_low: Optional[float] = None
    ci95_high: Optional[float] = None
    significant: bool = False
    n: int = 0
    n_effective: int = 0
    mean_effective: Optional[float] = None
    ci95_low_effective: Optional[float] = None
    ci95_high_effective: Optional[float] = None
    significant_effective: bool = False
    # BOMs that select an identical plan in both arms and therefore contribute an
    # exact 0.0 to this delta. Named, not just counted.
    zero_plan_boms: List[str] = []
    n_boot: int = BOOTSTRAP_N
    seed: int = BOOTSTRAP_SEED
    method: str = ""


class ResilienceSection(BaseModel):
    """
    Value of resilience: graph-aware MILP vs blind MILP.

    nominal_cost_premium_pct — mean per-BOM (graph_aware - blind) / blind * 100 of
      nominal landed cost. Negative = graph-aware is cheaper; expected ~0 (the
      graph-aware plan buys tail protection at near-zero nominal premium).

    *_cascade_risk_reduction — mean (blind.plan_cascade_risk - graph.plan_cascade_risk)
      under each disruption scenario. Positive = graph-aware LOWERS the share of
      BOM lines left unfulfilled. plan_cascade_risk is 1 - the MEDIAN fraction of
      the BOM's lines that stay fulfillable across the Monte Carlo trials — a
      share on 0-1, NOT a probability: it has no base rate and no exposure
      window, and on 4-line BOMs it can only take the values 0, .25, .5, .75, 1.
    *_cvar95_reduction — mean (blind.mc_cvar_95 - graph.mc_cvar_95). Positive =
      graph-aware LOWERS the worst-5% emergency-cost multiplier. READ
      `*_cvar95_saturated` FIRST: where it is True the metric is pinned at its
      structural ceiling and a reduction of 0.0 is arithmetic, not a finding.
    """
    nominal_cost_premium_pct: float
    stress_cascade_risk_reduction: float
    stress_cvar95_reduction: float
    targeted_cascade_risk_reduction: float
    targeted_cvar95_reduction: float
    # ── CVaR-95 saturation (item 13) ─────────────────────────────────────────
    # cvar_95 is a mean over the worst-5% tail of
    #   cost_inflation = 1 + unfulfillable_share * EMERGENCY_COST_PREMIUM,
    # which is BOUNDED, so cvar_95 tops out at 1 + premium and stops moving.
    # Under stress_factor=3 most plans sit ON that ceiling, and two arms then
    # print the identical number while being very differently exposed. These
    # measures were computed by graph/simulation.run_monte_carlo and persisted
    # NOWHERE until 2026-08-28, so 18 published CVaR cells tied unflagged.
    #
    # `*_cvar95_saturated` is True when AT LEAST ONE BOM PAIR in that scenario has
    # BOTH arms on the ceiling — that pair's delta is then 0.0 by arithmetic and
    # dilutes the scenario mean above. The pair, not the row, is the unit: one
    # saturated arm still leaves a measurable gap. `*_cvar95_ceiling_tied_boms`
    # names them, because a count alone cannot be checked.
    #
    # None (not False) means the run that produced these rows predates the
    # columns, so the question is UNANSWERED. A client must not collapse the two.
    cvar95_ceiling: Optional[float] = None
    stress_cvar95_saturated: Optional[bool] = None
    targeted_cvar95_saturated: Optional[bool] = None
    stress_cvar95_ceiling_tied_boms: Optional[List[str]] = None
    targeted_cvar95_ceiling_tied_boms: Optional[List[str]] = None
    cvar95_saturated_rows: Optional[int] = None
    cvar95_rows_measured: Optional[int] = None
    # The measure that keeps discriminating where cvar_95 has stopped:
    # P(EVERY BOM line unfulfillable), a mean over ALL scenarios rather than the
    # tail. Same sign convention: positive = the graph-aware arm is lower.
    # Never published bare — `p_total_shortfall_intervals` carries the same paired
    # percentile bootstrap the five published deltas carry, keyed by the exact
    # field name it qualifies. It is a SEPARATE dict from `intervals` so the
    # significant/non-significant partition over the published deltas is unchanged.
    stress_p_total_shortfall_reduction: Optional[float] = None
    targeted_p_total_shortfall_reduction: Optional[float] = None
    p_total_shortfall_intervals: Dict[str, PairedBootstrapCI] = {}
    # Composed from the flags above, never hardcoded, and explicit when the
    # columns are absent from this run's rows.
    saturation_note: str = ""
    # A reduction of exactly 0.0 means the two arms scored IDENTICALLY on that metric
    # under that scenario — a real measurement, not a missing one. The raw arm values
    # are published beside it so nobody has to take that on trust, and the
    # interpretation names any metric that came out flat.
    measured_values: Dict[str, Optional[float]] = {}
    flat_metrics: List[str] = []
    interpretation: str = ""
    # ── Paired inference (2026-08-28) ─────────────────────────────────────────
    # Every scalar above is a MEAN OVER 9 BOMs and was published for months with
    # no interval of any kind, while the lead-time model on the ML page may only
    # ship by beating its baselines with a paired bootstrap CI excluding zero.
    # `intervals` closes that gap: one PairedBootstrapCI per published delta,
    # keyed by the exact field name it qualifies, so a reader can map an interval
    # to its number without guessing. The scalars are unchanged — this ADDS an
    # interval around figures that already existed, it does not restate them.
    #
    # Read `significant_metrics` / `non_significant_metrics` before quoting any
    # scalar above. A delta in `non_significant_metrics` has an interval that
    # covers zero and IS NOT A RESULT.
    intervals: Dict[str, PairedBootstrapCI] = {}
    n_boms: int = 0
    n_effective_boms: int = 0
    n_effective_definition: str = ""
    significant_metrics: List[str] = []
    non_significant_metrics: List[str] = []
    inference_note: str = ""


class BenchmarkSummaryResponse(BaseModel):
    run_id: int
    run_tag: str
    # `run_tag` is opaque on its own — the deployed instance serves
    # "static_fallback" with no indication of what that implies about the numbers.
    run_tag_meaning: str = ""
    timestamp: str            # ISO-8601 of created_at for the run_id
    n_boms: int
    # ── The honest headline: a volume-dependent range, not a number ───────────
    headline: Headline
    volume_curve: VolumeCurve
    decomposition_at_prototype_volume: Optional[SavingsDecomposition] = None
    decomposition_at_production_volume: Optional[SavingsDecomposition] = None
    caveats: List[str] = []
    # ── Flat scalars the Benchmark page reads directly ────────────────────────
    # Same numbers as `headline` / `volume_curve`, promoted to the top level and to
    # the names the UI already looks for, so the page renders THIS endpoint's live
    # values instead of a checked-in copy of the artifact that can drift from it.
    headline_retracted: bool = True
    retraction_note: str = ""
    realistic_savings_pct_low: Optional[float] = None
    realistic_savings_pct_high: Optional[float] = None
    fixed_fee_share_of_savings_pct: Optional[float] = None
    fixed_fee_share_of_cost_pct: Optional[float] = None
    fixed_fee_per_supplier_usd: Optional[float] = None
    mean_units_per_bom: Optional[int] = None
    # ── Value of optimization: MILP (arm='milp', blind) vs greedy baselines ────
    # savings_pct — POOLED (Σ greedy − Σ milp) / Σ greedy over this run's BOMs.
    #   Until 2026-09-03 this field was the MEAN OF PER-BOM PERCENTAGES while
    #   `volume_curve` served the pooled figure, so one response carried two
    #   different averages of the same quantity (50.67 and 47.25 on run 7) with
    #   nothing distinguishing them. See `_POOLED_DEFINITION` for why pooled is
    #   the right one for a money claim; the old statistic is still published, as
    #   `savings_pct_mean_of_boms`, explicitly labelled and never as the headline.
    #
    #   MEASURED ON THIS RUN ONLY. This run's BOMs are 4 lines / 5–9 units, so this
    #   is the PROTOTYPE-volume number and it is dominated by a per-supplier fixed
    #   fee. It is NOT the optimizer's saving at any volume a buyer would actually
    #   order — read `headline` and `volume_curve` for that. `savings_units` names
    #   the unit so it cannot be confused with the USD fields below (the two have
    #   coincidentally equal values on run_id=4: 48.09 % and $48.09).
    #
    #   AND IT IS NOT A LIKE-FOR-LIKE COMPARISON: the greedy arm shops the full
    #   international offer pool while the MILP arm is domestic-only. Read
    #   `pool_asymmetry` before quoting this number anywhere.
    # savings_usd_per_bom — mean per-BOM (greedy - milp) landed cost, one reorder.
    # savings_usd_annualized — savings_usd_per_bom * annual_reorders (disclosed).
    savings_pct: float
    savings_units: str = "percent"
    savings_pct_aggregation: str = ""
    savings_pct_mean_of_boms: float = 0.0
    savings_pct_mean_of_boms_note: str = ""
    savings_pct_is_prototype_volume_only: bool = True
    # Display-ready label for `savings_pct`. A UI that renders the bare number will
    # reproduce the retracted headline; render this string instead. It now also
    # carries the offer-pool disclosure, so the caveat travels with the number
    # instead of sitting in a `caveats` array the page never rendered.
    savings_pct_display_label: str = ""
    savings_usd_per_bom: float
    savings_usd_annualized: float
    annual_reorders: int
    avg_suppliers_greedy: float
    avg_suppliers_milp: float
    benchmark_volume_note: str = ""
    # ── Every baseline in the database, not just the weakest one ──────────────
    # All four arms — {naive, ADD} x {international pool, MATCHED domestic pool} —
    # each pooled and mean-of-BOMs, so the reader can see what the optimizer adds
    # over a competent heuristic on the same catalogue, and not only over the
    # worst heuristic shopping a catalogue the optimizer was never allowed.
    # Exactly one carries `is_primary`.
    baselines: List[BaselineComparison] = []
    # ── The comparison's biggest known flaw, published beside the number ───────
    pool_asymmetry: Optional[PoolAsymmetry] = None
    # ── The like-for-like figure (run_id >= 8) ────────────────────────────────
    # `savings_pct` above is deliberately NOT re-pointed at this: it stays the
    # withdrawn, naive-baseline number the page renders struck through, and the
    # defensible one is served under its own name so a reader can never mistake
    # which is which. Null on a run that carries no pool-matched arms — an
    # unmeasured quantity is served as null, never as a zero.
    savings_pct_matched_pool: Optional[float] = None
    savings_pct_matched_pool_arm: Optional[str] = None
    savings_pct_matched_pool_note: str = ""
    primary_claim: str = ""
    # ── Value of resilience: graph-aware vs blind MILP (nominal + disruption) ──
    resilience: ResilienceSection
    # ── Legacy graph-aware-vs-blind A/B fields (now filtered to arm='milp',
    #    scenario='nominal'). Negative = graph-aware cheaper/faster/less risky. ──
    cost_delta_pct: float
    # Dollar-denominated framing (P3). cost_delta_usd is the mean absolute USD
    # difference (graph-aware - baseline) in total landed cost per BOM run;
    # negative => graph-aware saves money. baseline_spend_at_risk_usd is the mean
    # CVaR-95 emergency-procurement premium exposed per baseline BOM
    # (= total_cost_usd * (mc_cvar_95 - 1)).
    #
    # ON THE 48.09 COINCIDENCE (2026-08 audit): the audit flagged
    # `cost_delta_usd: 48.09` sitting beside `savings_pct: 48.09` as a percentage in a
    # USD field. It is not — the two are computed from different arms by different
    # formulas and happen to agree to two decimals on run_id=4:
    #   savings_pct    = mean over BOMs of (greedy - blind_milp) / greedy * 100
    #                  = 48.09 PERCENT
    #   cost_delta_usd = mean over BOMs of (graph_aware_milp - blind_milp)
    #                  = 433.64 - 385.54 = 48.09 US DOLLARS
    # `cost_delta_units` and `savings_units` are published so no reader has to take
    # that on trust, and `cost_delta_pct` (23.68) is the percentage partner of
    # cost_delta_usd — it is cost_delta_pct, not savings_pct, that shares its arms.
    cost_delta_usd: float
    cost_delta_units: str = "usd"
    baseline_spend_at_risk_usd: float
    eta_delta_pct: float
    co2_delta_pct: float
    # Sourced from `plan_cascade_risk`, NOT from the dead `cascade_risk_score` column.
    # See `cascade_risk_metric` for why.
    cascade_risk_delta_pct: float
    cascade_risk_metric: str = ""
    monte_carlo: MonteCarloSummary
    tradeoff: TradeoffEntry   # BOM with the worst graph-aware axis
    bom_deltas: List[BomDelta]
    feeds_fallback: bool
    # Renamed from `noise_floor_pct` (2026-08): it was never a noise floor.
    # See MATERIALITY_THRESHOLD_PCT / _MATERIALITY_BASIS above — the basis
    # string is served alongside the number so no reader has to ask where the
    # 2.0 came from.
    materiality_threshold_pct: float
    materiality_threshold_basis: str = ""


class FiedlerPoint(BaseModel):
    step: int
    removed: Optional[int] = None
    removed_name: Optional[str] = None
    lambda2: float
    delta_pct: float
    # Reference BOMs with at least one line that has NO supplier left in the graph
    # once the distributors up to and including this step have been removed.
    # Cumulative, and computed on the same graph λ₂ is computed on. Empty means
    # "checked, none collapsed" — `boms_checked` on the response says how many
    # BOMs stood behind that check, so an empty list is never ambiguous.
    collapsed_boms: List[str] = []


class FiedlerCurveResponse(BaseModel):
    points: List[FiedlerPoint]
    baseline_lambda2: float
    # How many reference BOMs the collapse check covered, and where they came
    # from. `boms_checked == 0` means the check could not run (no benchmarked
    # BOMs in the database) — the UI must then say so rather than rendering
    # "all BOMs remain fulfillable".
    boms_checked: int = 0
    bom_source: str = ""


class HeatmapPoint(BaseModel):
    lat: float
    lng: float
    # Mean `plan_cascade_risk` over the runs that selected this distributor:
    # the median SHARE of BOM lines left unfulfilled, on 0.0-1.0. A share, not
    # a probability — see _CASCADE_RISK_METRIC.
    weight: float
    distributor_id: int
    distributor_name: str


class CascadeHeatmapResponse(BaseModel):
    points: List[HeatmapPoint]
    # A heatmap that renders nothing must say why. Previously this returned
    # `{"points": []}` on a database with 234 perfectly good rows, because the weight
    # column it read was structurally 0.0 and a `normalized_weight > 0` guard then
    # dropped every point.
    metric: str = ""
    run_id: Optional[int] = None
    n_distributors_scored: int = 0
    max_raw_weight: float = 0.0
    note: str = ""


class SingleSourceComponent(BaseModel):
    component_id: int
    mpn: str
    manufacturer: str
    distributor_id: int
    distributor_name: str


class SingleSourceComponentsResponse(BaseModel):
    components: List[SingleSourceComponent]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_graph_state():
    from app.graph import get_graph_state
    from app.startup import wait_for_graph

    # The graph build moved off the lifespan onto a background thread (app/startup.py),
    # so a request can now arrive before it has finished. Wait for that ONE build
    # rather than starting another or answering from a half-built graph. Returns
    # immediately when no warm-up is running — which is what keeps the "no graph
    # state -> 503" tests meaningful.
    wait_for_graph()
    gs = get_graph_state()
    if gs is None:
        raise HTTPException(
            status_code=503,
            detail="Graph not loaded — server starting up or graph build failed",
        )
    return gs


def _safe_mean(values: list) -> float:
    """Return mean of a list, or 0.0 if empty."""
    if not values:
        return 0.0
    return mean(values)


def _pct_delta(baseline: Optional[float], graph_aware: Optional[float]) -> float:
    """Compute (graph_aware - baseline) / baseline * 100. Returns 0.0 if baseline is 0."""
    if baseline is None or graph_aware is None or baseline == 0.0:
        return 0.0
    return (graph_aware - baseline) / abs(baseline) * 100.0


# ── Paired inference over BOM clusters ────────────────────────────────────────

def paired_bootstrap_ci(
    deltas: Sequence[float],
    n_boot: int = BOOTSTRAP_N,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = CI_ALPHA,
) -> Dict[str, Any]:
    """
    Percentile bootstrap CI for the mean of a PAIRED per-BOM difference.

    Paired because both numbers behind each difference come from the same BOM,
    the same offer pool and the same simulation seed — the only thing that
    changes is the arm. The BOM is therefore the independent unit, and the BOM is
    what gets resampled with replacement.

    Returns ``significant = True`` only when the interval EXCLUDES zero. An
    interval that touches zero on either side is reported as not significant:
    `lo > 0 or hi < 0` is deliberately strict, so an all-zero panel (lo = hi = 0)
    can never be called a result.

    ``mean`` is NOT rounded to display precision here — the caller rounds. The
    served figures for run 5 are unchanged by this function; it only puts an
    interval around numbers that already exist.
    """
    vals = [float(d) for d in deltas]
    n = len(vals)
    if n == 0:
        return {
            "n": 0, "mean": 0.0, "ci95_low": None, "ci95_high": None,
            "significant": False,
            "note": "empty panel — no interval is estimable",
        }
    m = fmean(vals)
    if n == 1:
        return {
            "n": 1, "mean": m, "ci95_low": None, "ci95_high": None,
            "significant": False,
            "note": "n=1 — no interval is estimable from a single cluster",
        }

    rng = random.Random(seed)
    means: List[float] = []
    for _ in range(n_boot):
        means.append(fmean(rng.choices(vals, k=n)))
    means.sort()
    lo = means[max(0, int((alpha / 2) * n_boot))]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return {
        "n": n,
        "mean": m,
        "ci95_low": lo,
        "ci95_high": hi,
        "significant": (lo > 0.0) or (hi < 0.0),
    }


def _plan_differs(blind_row: Any, graph_row: Any) -> bool:
    """
    Did the graph-aware arm actually choose a DIFFERENT plan for this BOM?

    `optimization_runs` stores no line-level assignment snapshot, so plan
    identity is inferred from the two things it does store: the set of selected
    distributors and the landed cost. Same distributor set AND the same landed
    cost to the cent = the same plan, and such a BOM contributes an exact 0.0 to
    every delta. A same-set/different-cost pair means the quantities moved, which
    IS a different plan.
    """
    b_ids = sorted(blind_row.selected_distributor_ids or [])
    g_ids = sorted(graph_row.selected_distributor_ids or [])
    if b_ids != g_ids:
        return True
    b_cost = blind_row.total_cost_usd
    g_cost = graph_row.total_cost_usd
    if b_cost is None or g_cost is None:
        return False
    return abs(b_cost - g_cost) > 1e-9


def _paired_panel(
    rows: List[Any],
    value: Any,
) -> Tuple[List[str], List[float], List[bool]]:
    """
    Build the paired per-BOM panel for one scenario.

    Returns (bom_names, deltas, plan_changed) aligned by index, sorted by BOM
    name so the panel is deterministic. `value(blind_row, graph_row)` returns the
    per-BOM delta, or None when either side is missing the field.
    """
    blind = {r.bom_name: r for r in rows if not r.graph_aware}
    graph = {r.bom_name: r for r in rows if r.graph_aware}
    names: List[str] = []
    deltas: List[float] = []
    changed: List[bool] = []
    for name in sorted(set(blind) & set(graph)):
        b, g = blind[name], graph[name]
        d = value(b, g)
        if d is None:
            continue
        names.append(name)
        deltas.append(float(d))
        changed.append(_plan_differs(b, g))
    return names, deltas, changed


def _interval(
    metric: str,
    units: str,
    names: List[str],
    deltas: List[float],
    changed: List[bool],
    digits: int = 6,
) -> "PairedBootstrapCI":
    """Full-panel and effective-panel intervals for one published delta."""
    full = paired_bootstrap_ci(deltas)
    eff_idx = [i for i, c in enumerate(changed) if c]
    eff = paired_bootstrap_ci([deltas[i] for i in eff_idx])

    def _r(x: Optional[float]) -> Optional[float]:
        return None if x is None else round(x, digits)

    return PairedBootstrapCI(
        metric=metric,
        units=units,
        mean=round(float(full["mean"]), digits),
        ci95_low=_r(full["ci95_low"]),
        ci95_high=_r(full["ci95_high"]),
        significant=bool(full["significant"]),
        n=full["n"],
        n_effective=len(eff_idx),
        mean_effective=_r(eff["mean"]),
        ci95_low_effective=_r(eff["ci95_low"]),
        ci95_high_effective=_r(eff["ci95_high"]),
        significant_effective=bool(eff["significant"]),
        zero_plan_boms=[names[i] for i, c in enumerate(changed) if not c],
        n_boot=BOOTSTRAP_N,
        seed=BOOTSTRAP_SEED,
        method=_BOOTSTRAP_METHOD,
    )


# ── The volume curve: the honest replacement for the retracted headline ───────

_CASCADE_RISK_METRIC = (
    "plan_cascade_risk = 1 - the MEDIAN FRACTION OF THE BOM'S LINES that stay "
    "fulfillable under the Monte Carlo, with the supplying pool restricted to the "
    "distributors the plan actually chose. It is a SHARE on 0-1, not a probability: "
    "there is no base rate and no exposure window behind it, and on the 4-line "
    "reference BOMs it can only take the values 0, 0.25, 0.5, 0.75 and 1.0. "
    "The sibling column `cascade_risk_score` is DEAD and is no longer "
    "read by this API: the benchmark seed pipeline computes it as 1 - median "
    "fulfillment over the WHOLE distributor network, and with 97.7% of components "
    "carried by two or more distributors that median is 1.0 in essentially every "
    "trial, so the column is exactly 0.0 in all 234 rows ever written. A metric that "
    "is structurally incapable of being non-zero cannot express a delta, which is why "
    "cascade_risk_delta_pct used to be 0.0 for every BOM and /benchmark/cascade-heatmap "
    "returned an empty points list."
)

_AGGREGATE_DEFINITION = (
    "POOLED: sum(greedy costs) / sum(MILP costs) across the BOMs feasible at that "
    "multiplier — NOT a mean of per-BOM percentages. Mixing the two aggregations is "
    "how the original 44.7% inconsistency arose. Arm = `milp_matched`: greedy and "
    "MILP see the SAME offer pool (us_only=False for both), which the published "
    "benchmark run does not do. Points where greedy's plan orders more units than "
    "exist are excluded — greedy cannot be allowed to win with an unexecutable plan."
)

# ── The ONE aggregation the savings headline is allowed to use ────────────────
# Until 2026-09-03 `/benchmark/summary` served `savings_pct` as a MEAN OF PER-BOM
# PERCENTAGES (50.67 on run 7) while `volume_curve` served the POOLED figure
# (47.25 at 1x) — two different statistics of the same quantity, side by side, in
# one response, with nothing on the page distinguishing them. `_AGGREGATE_DEFINITION`
# directly above already forbade exactly this ("Mixing the two aggregations is how
# the original 44.7% inconsistency arose") and the endpoint did it anyway.
#
# POOLED wins, and not by convention. A cost-savings headline is a claim about
# money: it must be the share of the panel's spend that was saved, so that
# savings_pct x total greedy spend returns the dollars actually saved. Mean-of-BOMs
# does not have that property — it weights the $132 iot_sensor_node exactly like
# the $1,089 robotics_servo_driver, so it answers "the average BOM's percentage",
# which is not a budget line and cannot be multiplied by one. It also reads HIGHER
# here (50.67 vs 47.25) precisely because the biggest saving percentages land on
# the smallest BOMs, which is the flattering direction — one more reason it is not
# the number to lead with.
_POOLED_DEFINITION = (
    "POOLED: (sum of the greedy arm's landed costs - sum of the blind MILP's) / "
    "sum of the greedy arm's landed costs, over the BOMs both arms solved. This is "
    "the share of the panel's total spend that was saved, so it can be multiplied "
    "by a budget. It is the SAME aggregation `volume_curve` uses, so the headline "
    "and the curve are now one statistic measured at different order volumes."
)
_MEAN_OF_BOMS_DEFINITION = (
    "MEAN OF PER-BOM PERCENTAGES: the unweighted average of (greedy - MILP) / greedy "
    "across BOMs. NOT the headline and NOT comparable to `volume_curve`: it weights a "
    "$132 BOM the same as a $1,089 one, so it is not a share of spend and must not be "
    "multiplied by a budget. Published only so the difference from the pooled figure "
    "is visible instead of latent — this endpoint served this number AS the headline "
    "until 2026-09-03."
)

_COHORT_CAVEAT = (
    "The high-volume rows are a DIFFERENT, SMALLER BOM cohort than the low-volume "
    "ones — stock ceilings knock BOMs out as volume rises (10 BOMs feasible at 1x, "
    "2 at 10,000x). This curve is not a like-for-like cohort and must not be read as "
    "one. The trustworthy statement is the RANGE at production volume, not any single "
    "point on it."
)


@lru_cache(maxsize=1)
def _load_volume_curve() -> VolumeCurve:
    """
    Build the savings-vs-volume curve from `docs/volume_sweep.json`.

    The numbers are POOLED here rather than copied from the markdown, so the API can
    never drift from the published data the way the retracted 44.7% headline did.
    If the sweep artifact is missing the curve is returned as UNAVAILABLE with a
    reason — it is never replaced by a single headline percentage.
    """
    if not _VOLUME_SWEEP_PATH.exists():
        logger.warning("volume_sweep.json not found at %s", _VOLUME_SWEEP_PATH)
        return VolumeCurve(
            available=False,
            source=str(_VOLUME_SWEEP_PATH.relative_to(_REPO_ROOT)),
            aggregate_definition=_AGGREGATE_DEFINITION,
            unavailable_reason=(
                "docs/volume_sweep.json is missing. Regenerate it with "
                "`cd backend && python -m seeds.run_volume_sweep` (~1s). Until then "
                "this API cannot state how the saving varies with order volume, and "
                "no single savings percentage should be quoted."
            ),
        )

    try:
        raw: Dict[str, Any] = json.loads(_VOLUME_SWEEP_PATH.read_text())
    except Exception as exc:  # noqa: BLE001 — a bad artifact must not 500 the endpoint
        logger.warning("volume_sweep.json unreadable: %s", exc)
        return VolumeCurve(
            available=False,
            source=str(_VOLUME_SWEEP_PATH.relative_to(_REPO_ROOT)),
            aggregate_definition=_AGGREGATE_DEFINITION,
            unavailable_reason=f"docs/volume_sweep.json could not be parsed: {exc}",
        )

    meta = raw.get("meta", {})
    boms: Dict[str, Any] = raw.get("boms", {})
    points: List[VolumeCurvePoint] = []

    for multiplier in meta.get("multiplier_grid", []):
        greedy_total = milp_total = 0.0
        greedy_fixed_total = 0.0
        fees = comp = freight = 0.0
        sup_g = sup_m = 0
        n_feasible = 0
        units: List[int] = []
        for bom in boms.values():
            match = [p for p in bom.get("points", []) if p.get("multiplier") == multiplier]
            if not match:
                continue
            point = match[0]
            arms = point.get("arms", {})
            greedy_arm = arms.get("greedy", {})
            milp_arm = arms.get("milp_matched", {})
            if not greedy_arm.get("feasible") or not milp_arm.get("feasible"):
                continue
            # Greedy's fallback can order more units than an offer holds. Such a plan
            # cannot be executed, so it must not be allowed to inflate greedy's cost.
            if greedy_arm.get("stock_violations"):
                continue
            versus = point.get("vs_milp_matched", {})
            greedy_total += greedy_arm.get("total_cost", 0.0)
            greedy_fixed_total += greedy_arm.get("fixed_fee_usd", 0.0)
            milp_total += milp_arm.get("total_cost", 0.0)
            fees += versus.get("saving_from_fixed_fees_usd", 0.0)
            comp += versus.get("saving_from_component_cost_usd", 0.0)
            freight += versus.get("saving_from_variable_freight_usd", 0.0)
            sup_g += versus.get("suppliers_greedy", 0)
            sup_m += versus.get("suppliers_milp", 0)
            units.append(int(point.get("total_units", 0)))
            n_feasible += 1

        if not n_feasible or greedy_total <= 0:
            continue
        saving = greedy_total - milp_total
        pooled_pct = round(saving / greedy_total * 100.0, 2)
        fee_share = round(fees / saving * 100.0, 1) if abs(saving) > 1e-9 else None
        points.append(VolumeCurvePoint(
            multiplier=int(multiplier),
            boms_feasible=n_feasible,
            units_min=min(units) if units else 0,
            units_max=max(units) if units else 0,
            greedy_cost_usd=round(greedy_total, 2),
            milp_cost_usd=round(milp_total, 2),
            pooled_savings_pct=pooled_pct,
            savings_pct=pooled_pct,
            saving_from_fixed_fees_usd=round(fees, 2),
            fixed_fee_usd=round(fees, 2),
            saving_from_component_cost_usd=round(comp, 2),
            component_usd=round(comp, 2),
            saving_from_variable_freight_usd=round(freight, 2),
            variable_freight_usd=round(freight, 2),
            fixed_fee_share_of_saving_pct=fee_share,
            fee_share_of_saving_pct=fee_share,
            greedy_fixed_share_of_cost_pct=round(
                greedy_fixed_total / greedy_total * 100.0, 1
            ),
            suppliers_greedy=sup_g,
            suppliers_milp=sup_m,
        ))

    # The fixed per-supplier fee the cost model actually charges: the domestic LTL
    # base fee scaled by the strategy's transport penalty. This single constant is
    # what the retracted headline was measuring.
    try:
        fee = float(meta["cost_constants"]["LTL_BASE_FEE_USD"]) * float(
            meta["strategy_weights"]["transport_penalty_scale"]
        )
    except Exception:  # noqa: BLE001 — an older artifact simply omits it
        fee = 0.0
    try:
        air_fee = float(meta["cost_constants"]["AIR_FREIGHT_BASE_USD"]) * float(
            meta["strategy_weights"]["transport_penalty_scale"]
        )
    except Exception:  # noqa: BLE001 — ditto
        air_fee = 0.0

    return VolumeCurve(
        available=bool(points),
        source=str(_VOLUME_SWEEP_PATH.relative_to(_REPO_ROOT)),
        generated_utc=meta.get("generated_utc"),
        fixed_fee_per_supplier_usd=round(fee, 2) if fee else None,
        international_fixed_fee_per_supplier_usd=round(air_fee, 2) if air_fee else None,
        aggregate_definition=_AGGREGATE_DEFINITION,
        points=points,
        cohort_caveat=_COHORT_CAVEAT,
        unavailable_reason=None if points else "The sweep contains no feasible points.",
    )


@lru_cache(maxsize=1)
def _load_pool_control() -> Optional[Dict[str, Any]]:
    """
    The matched-offer-pool CONTROL for the published benchmark headline.

    `docs/volume_sweep.json` solves every BOM at 1x with three arms:
      greedy       — us_only=False (exactly what the published benchmark runs)
      milp_bench   — us_only=True  (exactly the published benchmark's MILP arm)
      milp_matched — us_only=False (the same MILP given greedy's full pool)

    So the artifact already contains the counterfactual the headline needs: hold
    greedy fixed, lift the MILP's domestic-only restriction, and see how much of
    the gap was the shipping policy rather than the optimization.

    Restricted to the LIKE-FOR-LIKE cohort — BOMs where all three arms are
    feasible and greedy's plan does not order above stock — because comparing a
    10-BOM cohort against a 9-BOM one is the cohort error `_COHORT_CAVEAT`
    already warns about.

    Returns None (never a fabricated number) if the artifact is missing or
    carries no usable point.
    """
    if not _VOLUME_SWEEP_PATH.exists():
        return None
    try:
        raw: Dict[str, Any] = json.loads(_VOLUME_SWEEP_PATH.read_text())
    except Exception:  # noqa: BLE001 — a bad artifact must not 500 the endpoint
        return None

    greedy_total = bench_total = matched_total = 0.0
    n = 0
    for bom in (raw.get("boms") or {}).values():
        match = [p for p in bom.get("points", []) if p.get("multiplier") == 1]
        if not match:
            continue
        arms = match[0].get("arms", {})
        g = arms.get("greedy", {})
        bench = arms.get("milp_bench", {})
        matched = arms.get("milp_matched", {})
        if not (g.get("feasible") and bench.get("feasible") and matched.get("feasible")):
            continue
        if g.get("stock_violations"):
            continue
        greedy_total += float(g.get("total_cost", 0.0))
        bench_total += float(bench.get("total_cost", 0.0))
        matched_total += float(matched.get("total_cost", 0.0))
        n += 1

    if not n or greedy_total <= 0:
        return None

    pct_bench = (greedy_total - bench_total) / greedy_total * 100.0
    pct_matched = (greedy_total - matched_total) / greedy_total * 100.0
    gap = pct_bench - pct_matched
    if abs(matched_total - bench_total) < 0.01:
        finding = (
            f"NON-BINDING on the MILP side. Re-solving the same {n} BOMs with the "
            "domestic-only restriction LIFTED — the MILP free to buy from the same "
            "Chinese and Singaporean distributors greedy uses — produces the same "
            f"total landed cost to the cent (${matched_total:,.2f} vs "
            f"${bench_total:,.2f}) and the same chosen distributors on every BOM. "
            "The MILP declines the international offers on its own: opening one "
            "costs the air-freight fixed fee, and at these quantities the cheaper "
            "parts do not pay it back. So 0.0 points of the headline percentage "
            "come from restricting the MILP's pool."
        )
    else:
        finding = (
            f"On the same {n} BOMs, lifting the MILP's domestic-only restriction "
            f"moves the pooled saving from {pct_bench:.2f}% to {pct_matched:.2f}% — "
            f"{gap:+.2f} points of the published headline are the MILP's supplier "
            "pool, not its optimization."
        )
    return {
        "source": str(_VOLUME_SWEEP_PATH.relative_to(_REPO_ROOT)),
        "n_boms": n,
        "greedy_cost_usd": round(greedy_total, 2),
        "milp_domestic_pool_cost_usd": round(bench_total, 2),
        "milp_full_pool_cost_usd": round(matched_total, 2),
        "savings_pct_domestic_pool": round(pct_bench, 2),
        "savings_pct_full_pool": round(pct_matched, 2),
        "finding": finding,
    }


def _decompose(point: VolumeCurvePoint, label: str) -> SavingsDecomposition:
    """Name the term that actually produced the saving at one volume."""
    terms = {
        "fixed per-supplier fees": point.saving_from_fixed_fees_usd,
        "component cost": point.saving_from_component_cost_usd,
        "variable freight": point.saving_from_variable_freight_usd,
    }
    dominant = max(terms, key=lambda k: abs(terms[k]))
    total = point.greedy_cost_usd - point.milp_cost_usd
    if dominant == "fixed per-supplier fees":
        note = (
            f"At {label} the saving is fee arithmetic, not optimization. The greedy "
            f"baseline picks min(price) per line, so it is the component-cost minimum "
            f"BY CONSTRUCTION — the MILP cannot beat it on parts and here pays "
            f"${abs(point.saving_from_component_cost_usd):,.0f} MORE for them. What it "
            f"avoids is a $75 LTL / $150 air fee charged per opened supplier (x1.5 "
            f"transport_penalty_scale), by consolidating "
            f"{point.suppliers_greedy} suppliers into {point.suppliers_milp}. That fee "
            "is roughly constant in volume while component cost grows linearly, so the "
            "percentage must decay — and it does."
        )
    elif dominant == "variable freight":
        note = (
            f"At {label} the saving is real optimization and it SURVIVES volume: the "
            f"MILP routes each line's units to whichever distributor minimizes price + "
            f"freight, which greedy structurally cannot see. It now opens MORE "
            f"suppliers than greedy ({point.suppliers_greedy} -> "
            f"{point.suppliers_milp}) and pays more in per-visit fees on purpose, to "
            "buy down freight and stay inside stock caps."
        )
    else:
        note = f"At {label} the dominant term is component cost."
    return SavingsDecomposition(
        multiplier=point.multiplier,
        total_saving_usd=round(total, 2),
        from_fixed_supplier_fees_usd=point.saving_from_fixed_fees_usd,
        from_component_cost_usd=point.saving_from_component_cost_usd,
        from_variable_freight_usd=point.saving_from_variable_freight_usd,
        dominant_term=dominant,
        note=note,
    )


def _build_headline(curve: VolumeCurve) -> Headline:
    """The volume-dependent truth, phrased so it cannot be quoted as one number."""
    retraction_reason = (
        "Retracted 2026-07-13 (docs/BENCHMARK_VOLUME_CURVE.md, commit 4b4c5b2). The "
        "benchmark scores 4-line BOMs of 5-9 TOTAL UNITS. At that size a fixed "
        "$75-per-supplier LTL fee (x1.5) is larger than the parts, so consolidating "
        "suppliers dominates everything else. The fee does not grow with volume and "
        "component cost does, so the percentage decays. The single-number claim "
        "\"the optimizer is 44.7% cheaper\" is retracted and must not be requoted; "
        "use the volume curve below instead."
    )
    common = {
        "retracted_claim": RETRACTED_HEADLINE,
        "retraction_reason": retraction_reason,
        "dominant_mechanism_at_prototype_volume":
            "avoided fixed per-supplier onboarding fees (an artifact of toy quantities)",
        "dominant_mechanism_at_production_volume":
            "variable freight — routing units by landed cost rather than unit price "
            "(genuine, volume-scaling optimization)",
    }

    if not curve.available or not curve.points:
        return Headline(
            statement=(
                "No savings figure can be stated: the volume sweep that grounds it is "
                "unavailable. A single savings percentage is not a valid answer for "
                "this optimizer at any volume."
            ),
            **common,
        )

    by_mult = {p.multiplier: p for p in curve.points}
    prototype = by_mult.get(1) or curve.points[0]
    production = [p for p in curve.points if p.multiplier >= _PRODUCTION_MULTIPLIER_MIN]
    if production:
        lows = min(p.pooled_savings_pct for p in production)
        highs = max(p.pooled_savings_pct for p in production)
        lo_mult = min(p.multiplier for p in production)
        hi_mult = max(p.multiplier for p in production)
        statement = (
            f"The MILP's cost edge over a greedy baseline is VOLUME-DEPENDENT: "
            f"{prototype.pooled_savings_pct:.1f}% on the benchmark's "
            f"{prototype.multiplier}x prototype BOMs, decaying to "
            f"{lows:.1f}%-{highs:.1f}% at production volume "
            f"({lo_mult}x-{hi_mult}x). The prototype figure is a fixed per-supplier fee "
            f"(${prototype.saving_from_fixed_fees_usd:,.0f} of a "
            f"${prototype.greedy_cost_usd - prototype.milp_cost_usd:,.0f} saving — the "
            f"MILP actually pays "
            f"${abs(prototype.saving_from_component_cost_usd):,.0f} MORE for the parts). "
            "The production figure is genuine freight optimization. Quote the range, "
            "not a number."
        )
    else:
        lows = highs = None
        lo_mult = hi_mult = None
        statement = (
            f"The sweep only reaches {max(by_mult)}x, below the "
            f"{_PRODUCTION_MULTIPLIER_MIN}x production band, so no production-volume "
            "range can be stated."
        )

    return Headline(
        statement=statement,
        savings_pct_at_prototype_volume=prototype.pooled_savings_pct,
        prototype_volume_units=None,
        savings_pct_at_production_volume_low=lows,
        savings_pct_at_production_volume_high=highs,
        production_volume_units_low=lo_mult,
        production_volume_units_high=hi_mult,
        **common,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/summary", response_model=BenchmarkSummaryResponse)
def get_benchmark_summary(
    run_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    """
    Return two stories for the latest (or specified) run_id.

    (1) VALUE OF OPTIMIZATION — partitions nominal rows by arm (greedy vs milp)
        and reports savings_pct / savings_usd_* and supplier consolidation.
    (2) VALUE OF RESILIENCE — the two MILP arms (blind vs graph-aware): ~0 nominal
        cost premium, but cascade-risk and CVaR-95 reduction under stress/targeted
        disruption.

    Legacy graph-aware-vs-blind delta fields are retained (now filtered to
    arm='milp', scenario='nominal'). Delta sign convention preserved:
    negative = graph-aware is cheaper/faster/less risky.
    """
    from app.models.optimization_run import OptimizationRun

    # Determine target run_id
    if run_id is None:
        latest = (
            db.query(OptimizationRun.run_id)
            .order_by(OptimizationRun.run_id.desc())
            .first()
        )
        if latest is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No benchmark rows found. "
                    "Run the benchmark pipeline: python -m seeds.benchmark_pipeline"
                ),
            )
        target_run_id = latest[0]
    else:
        target_run_id = run_id

    # Load all rows for this run_id
    all_rows = (
        db.query(OptimizationRun)
        .filter(OptimizationRun.run_id == target_run_id)
        .all()
    )
    if not all_rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No benchmark rows found for run_id={target_run_id}. "
                "Run the benchmark pipeline: python -m seeds.benchmark_pipeline"
            ),
        )

    # Partition rows by arm/scenario (benchmark 2.0 schema). Legacy rows written
    # before arm/scenario existed (both NULL) are treated as milp/nominal so the
    # graph-aware A/B still works on older run_ids.
    def _arm(r) -> str:
        return r.arm or "milp"

    def _scen(r) -> str:
        return r.scenario or "nominal"

    # Value-of-resilience A/B compares the two MILP arms in the NOMINAL world.
    milp_nominal = [r for r in all_rows if _arm(r) == "milp" and _scen(r) == "nominal"]
    baseline_rows = [r for r in milp_nominal if not r.graph_aware]   # blind MILP
    graph_aware_rows = [r for r in milp_nominal if r.graph_aware]    # graph-aware MILP
    # Value-of-optimization compares greedy baseline vs (blind) MILP, nominal.
    greedy_nominal = [r for r in all_rows if _arm(r) == "greedy" and _scen(r) == "nominal"]
    # The ADD-heuristic baseline. It has been written on every run since the
    # benchmark-2.0 schema landed and this endpoint never surfaced it, so the
    # published figure was the MILP's edge over the WEAKEST baseline in the table
    # while a stronger one sat one row away.
    greedy_add_nominal = [
        r for r in all_rows if _arm(r) == "greedy_add" and _scen(r) == "nominal"
    ]
    # The POOL-MATCHED baselines (run_id >= 8): the same two heuristics re-solved
    # on the MILP's own domestic-only catalogue. Matched by EXACT arm equality, so
    # a `_dom` row can never be counted into a global-pool partition and quietly
    # double the cohort. On older runs these lists are empty and every matched-pool
    # field below is served as null rather than as a zero.
    greedy_dom_nominal = [
        r for r in all_rows if _arm(r) == "greedy_dom" and _scen(r) == "nominal"
    ]
    greedy_add_dom_nominal = [
        r for r in all_rows if _arm(r) == "greedy_add_dom" and _scen(r) == "nominal"
    ]

    if not baseline_rows or not graph_aware_rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Incomplete benchmark data for run_id={target_run_id}. "
                "Run the benchmark pipeline: python -m seeds.benchmark_pipeline"
            ),
        )

    # Gather timestamp from first row
    timestamp_str = (
        all_rows[0].created_at.isoformat()
        if all_rows[0].created_at is not None
        else ""
    )

    # Build per-BOM lookup
    baseline_by_bom: Dict[str, object] = {r.bom_name: r for r in baseline_rows}
    graph_aware_by_bom: Dict[str, object] = {r.bom_name: r for r in graph_aware_rows}

    # Compute per-BOM deltas
    bom_deltas: List[BomDelta] = []
    common_boms = sorted(set(baseline_by_bom.keys()) & set(graph_aware_by_bom.keys()))
    n_boms = len(common_boms)

    for bom in common_boms:
        b = baseline_by_bom[bom]
        g = graph_aware_by_bom[bom]
        bom_deltas.append(BomDelta(
            bom_name=bom,
            cost_delta_pct=_pct_delta(b.total_cost_usd, g.total_cost_usd),
            eta_delta_pct=_pct_delta(b.eta_p50_days, g.eta_p50_days),
            co2_delta_pct=_pct_delta(b.co2_kg, g.co2_kg),
            # `plan_cascade_risk`, NOT `cascade_risk_score` — see _CASCADE_RISK_METRIC.
            cascade_risk_delta_pct=_pct_delta(b.plan_cascade_risk, g.plan_cascade_risk),
        ))

    # Aggregate mean deltas
    cost_delta_pct = _safe_mean([d.cost_delta_pct for d in bom_deltas])

    # Absolute dollar deltas (P3): mean USD saved per BOM run, and the mean
    # CVaR-95 emergency-procurement premium exposed on each baseline BOM.
    cost_delta_usd = _safe_mean([
        graph_aware_by_bom[bom].total_cost_usd - baseline_by_bom[bom].total_cost_usd
        for bom in common_boms
    ])
    baseline_spend_at_risk_usd = _safe_mean([
        baseline_by_bom[bom].total_cost_usd * max(0.0, (baseline_by_bom[bom].mc_cvar_95 or 1.0) - 1.0)
        for bom in common_boms
        if baseline_by_bom[bom].total_cost_usd is not None
    ])

    eta_delta_pct = _safe_mean([d.eta_delta_pct for d in bom_deltas])
    co2_delta_pct = _safe_mean([d.co2_delta_pct for d in bom_deltas])
    cascade_risk_delta_pct = _safe_mean([d.cascade_risk_delta_pct for d in bom_deltas])

    # Monte Carlo summary
    def _safe_list_mean(rows, attr: str) -> float:
        vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
        return _safe_mean(vals)

    monte_carlo = MonteCarloSummary(
        baseline_p10=_safe_list_mean(baseline_rows, "eta_p10_days"),
        baseline_p50=_safe_list_mean(baseline_rows, "eta_p50_days"),
        baseline_p90=_safe_list_mean(baseline_rows, "eta_p90_days"),
        graph_aware_p10=_safe_list_mean(graph_aware_rows, "eta_p10_days"),
        graph_aware_p50=_safe_list_mean(graph_aware_rows, "eta_p50_days"),
        graph_aware_p90=_safe_list_mean(graph_aware_rows, "eta_p90_days"),
        baseline_cvar_95=_safe_list_mean(baseline_rows, "mc_cvar_95") or None,
        graph_aware_cvar_95=_safe_list_mean(graph_aware_rows, "mc_cvar_95") or None,
    )

    # ── Value of optimization: heuristic baselines vs (blind) MILP, nominal ────
    #
    # Domesticity of every distributor either arm opened, so the offer-pool
    # asymmetry can be QUANTIFIED from the served database rather than asserted.
    # One query, keyed by id; a distributor missing from the table is counted as
    # domestic (the conservative direction — it cannot inflate the disclosure).
    from app.models import Distributor

    _is_domestic: Dict[int, bool] = {
        int(did): bool(dom)
        for did, dom in db.query(Distributor.id, Distributor.is_domestic).all()
    }

    def _supplier_counts(rows_by_bom: Dict[str, object], boms: List[str]) -> Tuple[int, int]:
        """(distributors opened, of which international) summed across `boms`."""
        opened = intl = 0
        for b in boms:
            ids = getattr(rows_by_bom[b], "selected_distributor_ids", None) or []
            if not isinstance(ids, list):
                continue
            opened += len(ids)
            intl += sum(1 for i in ids if not _is_domestic.get(int(i), True))
        return opened, intl

    def _compare(
        rows_by_bom: Dict[str, object], boms: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Score one baseline arm against the blind MILP on the BOMs both solved.

        Both aggregations are computed here and BOTH are returned, named. The
        pooled one is the headline (`_POOLED_DEFINITION`); the mean-of-BOMs one
        is carried alongside so the two can never again be confused for each
        other by a reader who only sees one of them.
        """
        if not boms:
            return None
        base_total = sum(rows_by_bom[b].total_cost_usd for b in boms)
        milp_total = sum(baseline_by_bom[b].total_cost_usd for b in boms)
        if base_total <= 0:
            return None
        per_bom_pct = [
            (rows_by_bom[b].total_cost_usd - baseline_by_bom[b].total_cost_usd)
            / abs(rows_by_bom[b].total_cost_usd) * 100.0
            for b in boms if rows_by_bom[b].total_cost_usd
        ]
        per_bom_usd = [
            rows_by_bom[b].total_cost_usd - baseline_by_bom[b].total_cost_usd
            for b in boms
        ]
        opened, intl = _supplier_counts(rows_by_bom, boms)
        return {
            "n_boms": len(boms),
            "total_cost_usd": round(base_total, 2),
            "milp_total_cost_usd": round(milp_total, 2),
            "pooled_savings_pct": round((base_total - milp_total) / base_total * 100.0, 2),
            "mean_of_boms_savings_pct": round(_safe_mean(per_bom_pct), 2),
            "savings_usd_per_bom": round(_safe_mean(per_bom_usd), 2),
            "savings_usd_annualized": round(_safe_mean(per_bom_usd) * ANNUAL_REORDERS, 2),
            "avg_suppliers": round(_safe_mean([
                rows_by_bom[b].n_distinct_suppliers for b in boms
                if rows_by_bom[b].n_distinct_suppliers is not None
            ]), 2),
            "suppliers_opened": opened,
            "international_suppliers_opened": intl,
        }

    greedy_by_bom: Dict[str, object] = {r.bom_name: r for r in greedy_nominal}
    greedy_add_by_bom: Dict[str, object] = {r.bom_name: r for r in greedy_add_nominal}
    greedy_dom_by_bom: Dict[str, object] = {r.bom_name: r for r in greedy_dom_nominal}
    greedy_add_dom_by_bom: Dict[str, object] = {
        r.bom_name: r for r in greedy_add_dom_nominal
    }
    opt_boms = sorted(set(greedy_by_bom.keys()) & set(baseline_by_bom.keys()))
    add_boms = sorted(set(greedy_add_by_bom.keys()) & set(baseline_by_bom.keys()))
    dom_boms = sorted(set(greedy_dom_by_bom.keys()) & set(baseline_by_bom.keys()))
    add_dom_boms = sorted(set(greedy_add_dom_by_bom.keys()) & set(baseline_by_bom.keys()))

    greedy_stats = _compare(greedy_by_bom, opt_boms)
    greedy_add_stats = _compare(greedy_add_by_bom, add_boms)
    greedy_dom_stats = _compare(greedy_dom_by_bom, dom_boms)
    greedy_add_dom_stats = _compare(greedy_add_dom_by_bom, add_dom_boms)

    _GLOBAL_POOL = (
        "the FULL international catalogue (us_only=False) — a wider catalogue "
        "than the MILP was allowed"
    )
    _DOMESTIC_POOL = (
        "domestic (US) distributors only (us_only=True) — the SAME catalogue the "
        "MILP's balanced strategy restricts it to"
    )

    # Ordered weakest-baseline-first so the table reads as a descent from the
    # retracted number to the defensible one, ending on the primary arm.
    _baseline_specs = (
        (
            "greedy", greedy_stats, _GLOBAL_POOL, False, False,
            "Naive greedy (per-line cheapest), international pool",
            "Sources every BOM line independently at the cheapest in-stock offer "
            "anywhere in the catalogue. No awareness of the per-supplier fixed "
            "freight fee, so it opens a supplier account per line — and it may open "
            "them abroad, at the international air-freight fee the MILP never pays. "
            "This is the WEAKEST baseline in the database on BOTH axes, and the one "
            "the retracted headline was measured against.",
        ),
        (
            "greedy_add", greedy_add_stats, _GLOBAL_POOL, False, False,
            "ADD heuristic (Kuehn & Hamburger 1963), international pool",
            "Starts from the naive greedy plan, then repeatedly moves a BOM line "
            "onto an already-opened distributor while that strictly lowers landed "
            "cost. A competent heuristic, but still shopping a wider catalogue than "
            "the optimizer: the drop from the row above is the heuristic handicap "
            "alone, with the pool handicap still in place.",
        ),
        (
            "greedy_dom", greedy_dom_stats, _DOMESTIC_POOL, True, False,
            "Naive greedy (per-line cheapest), MATCHED domestic pool",
            "The naive heuristic re-solved on the optimizer's own domestic-only "
            "catalogue. Pool matched, heuristic still naive — so the drop from the "
            "first row is the pool handicap alone.",
        ),
        (
            "greedy_add_dom", greedy_add_dom_stats, _DOMESTIC_POOL, True, True,
            "ADD heuristic, MATCHED domestic pool — the like-for-like baseline",
            "A competent heuristic solving the same problem, on the same catalogue, "
            "scored by the same landed-cost function. Nothing differs but the "
            "algorithm, so this is the only one of the four that measures the "
            "optimizer rather than the baseline's handicaps. THIS is the number to "
            "quote.",
        ),
    )

    baselines: List[BaselineComparison] = []
    for _arm_id, _stats, _pool, _matched, _primary, _label, _desc in _baseline_specs:
        if _stats is None:
            continue
        baselines.append(BaselineComparison(
            arm=_arm_id,
            label=_label,
            description=_desc,
            pool=_pool,
            matched_pool=_matched,
            is_primary=_primary,
            **_stats,
        ))

    # ── THE PRIMARY CLAIM: like-for-like, pools matched ───────────────────────
    # `savings_pct` below stays bound to the `greedy` arm because it is the field
    # the page renders struck through as the WITHDRAWN figure, and silently
    # re-pointing a published field at a different statistic is how this repo
    # produced two documents that agreed with each other and with nothing else.
    # The defensible number gets its own name instead.
    _primary = next((b for b in baselines if b.is_primary), None)
    savings_pct_matched_pool = _primary.pooled_savings_pct if _primary else None
    savings_pct_matched_pool_arm = _primary.arm if _primary else None
    savings_pct_matched_pool_note = (
        (
            f"POOLED landed-cost advantage of the blind MILP over `{_primary.arm}` "
            f"across {_primary.n_boms} BOMs: (Σ baseline − Σ MILP) / Σ baseline. "
            f"Both arms shop the domestic-only catalogue and are scored by the same "
            f"`landed_cost_breakdown`, so only the algorithm differs. This is the "
            f"figure to quote; `savings_pct` beside it is measured against a naive "
            f"baseline shopping a wider catalogue and is kept for contrast only."
        )
        if _primary else
        (
            "No pool-matched baseline exists on this run. The matched arms "
            "(`greedy_dom`, `greedy_add_dom`) were added to the benchmark seed "
            "pipeline on 2026-09-03 and are only present from run_id=8 onward; on an "
            "run there is no like-for-like number and none is invented here."
        )
    )
    primary_claim = (
        (
            f"The CP-SAT optimizer is {abs(_primary.pooled_savings_pct):.2f}% cheaper "
            f"on pooled landed cost than a competent ADD heuristic solving the same "
            f"{_primary.n_boms} BOMs on the same domestic-only offer pool. At this "
            f"order size (see the volume curve) even that edge is mostly avoided "
            f"per-supplier freight fees and it decays as volume grows."
        )
        if _primary else ""
    )

    # The headline statistic. POOLED, matching `volume_curve` — see
    # `_POOLED_DEFINITION` for why this and not the mean of per-BOM percentages.
    savings_pct = greedy_stats["pooled_savings_pct"] if greedy_stats else 0.0
    savings_pct_mean_of_boms = (
        greedy_stats["mean_of_boms_savings_pct"] if greedy_stats else 0.0
    )
    savings_usd_per_bom = greedy_stats["savings_usd_per_bom"] if greedy_stats else 0.0
    savings_usd_annualized = savings_usd_per_bom * ANNUAL_REORDERS
    avg_suppliers_greedy = greedy_stats["avg_suppliers"] if greedy_stats else 0.0
    avg_suppliers_milp = _safe_mean([
        baseline_by_bom[b].n_distinct_suppliers for b in opt_boms
        if baseline_by_bom[b].n_distinct_suppliers is not None
    ])

    # ── The offer-pool asymmetry, quantified from this run's own rows ──────────
    milp_opened, milp_intl = _supplier_counts(baseline_by_bom, opt_boms)
    greedy_opened = greedy_stats["suppliers_opened"] if greedy_stats else 0
    greedy_intl = greedy_stats["international_suppliers_opened"] if greedy_stats else 0
    _control = _load_pool_control()

    # ── Value of resilience: graph-aware vs blind MILP under disruption ────────
    milp_stress = [r for r in all_rows if _arm(r) == "milp" and _scen(r) == "stress"]
    milp_targeted = [r for r in all_rows if _arm(r) == "milp" and _scen(r) == "targeted"]

    def _mean_reduction(rows, attr: str) -> float:
        """mean(blind - graph) per BOM: positive = graph-aware lowers the metric."""
        blind = {r.bom_name: r for r in rows if not r.graph_aware}
        graph = {r.bom_name: r for r in rows if r.graph_aware}
        vals = []
        for b in set(blind) & set(graph):
            bv = getattr(blind[b], attr)
            gv = getattr(graph[b], attr)
            if bv is not None and gv is not None:
                vals.append(bv - gv)
        return _safe_mean(vals)

    def _arm_mean(rows, attr: str, graph_aware: bool) -> Optional[float]:
        vals = [
            getattr(r, attr) for r in rows
            if bool(r.graph_aware) is graph_aware and getattr(r, attr) is not None
        ]
        return round(_safe_mean(vals), 4) if vals else None

    reductions = {
        "stress_cascade_risk_reduction": _mean_reduction(milp_stress, "plan_cascade_risk"),
        "stress_cvar95_reduction": _mean_reduction(milp_stress, "mc_cvar_95"),
        "targeted_cascade_risk_reduction": _mean_reduction(milp_targeted, "plan_cascade_risk"),
        "targeted_cvar95_reduction": _mean_reduction(milp_targeted, "mc_cvar_95"),
    }
    flat = [name for name, value in reductions.items() if abs(value) < 1e-9]
    measured = {
        "stress_blind_plan_cascade_risk": _arm_mean(milp_stress, "plan_cascade_risk", False),
        "stress_graph_plan_cascade_risk": _arm_mean(milp_stress, "plan_cascade_risk", True),
        "stress_blind_mc_cvar_95": _arm_mean(milp_stress, "mc_cvar_95", False),
        "stress_graph_mc_cvar_95": _arm_mean(milp_stress, "mc_cvar_95", True),
        "targeted_blind_plan_cascade_risk": _arm_mean(milp_targeted, "plan_cascade_risk", False),
        "targeted_graph_plan_cascade_risk": _arm_mean(milp_targeted, "plan_cascade_risk", True),
        "targeted_blind_mc_cvar_95": _arm_mean(milp_targeted, "mc_cvar_95", False),
        "targeted_graph_mc_cvar_95": _arm_mean(milp_targeted, "mc_cvar_95", True),
        # The measures that keep resolving after mc_cvar_95 has saturated (item 13).
        "stress_blind_p_total_shortfall": _arm_mean(
            milp_stress, "mc_p_total_shortfall", False),
        "stress_graph_p_total_shortfall": _arm_mean(
            milp_stress, "mc_p_total_shortfall", True),
        "targeted_blind_p_total_shortfall": _arm_mean(
            milp_targeted, "mc_p_total_shortfall", False),
        "targeted_graph_p_total_shortfall": _arm_mean(
            milp_targeted, "mc_p_total_shortfall", True),
        "stress_blind_p_shortfall": _arm_mean(milp_stress, "mc_p_shortfall", False),
        "stress_graph_p_shortfall": _arm_mean(milp_stress, "mc_p_shortfall", True),
        "targeted_blind_p_shortfall": _arm_mean(milp_targeted, "mc_p_shortfall", False),
        "targeted_graph_p_shortfall": _arm_mean(milp_targeted, "mc_p_shortfall", True),
    }

    # ── CVaR-95 saturation, read off the stored rows (item 13) ────────────────
    # mc_cvar_95 is bounded above by mc_cvar_95_ceiling = 1 + EMERGENCY_COST_PREMIUM.
    # THE UNIT THAT MATTERS IS THE PAIR, NOT THE ROW: a per-BOM cvar95 delta is
    # forced to exactly 0.0 when BOTH arms of that BOM are at the ceiling, and a
    # scenario mean built partly out of such pairs is contaminated by zeros the
    # metric could not avoid producing. Counting saturated ROWS would say "8 of 9",
    # which sounds alarming but is not the thing that biases the mean; counting
    # ceiling-TIED PAIRS says how many of the panel's deltas are arithmetic.
    #
    # Nothing here is asserted. Every flag is read from the stored columns, and
    # rows written before those columns existed carry None, which propagates to
    # None ("not measured on this run") rather than to a False nobody measured.
    _measured_rows = [
        r for r in (milp_stress + milp_targeted) if r.mc_cvar_95_saturated is not None
    ]

    def _ceiling_tied_boms(rows: List[Any]) -> Optional[List[str]]:
        """BOMs whose cvar95 delta is 0.0 because BOTH arms hit the ceiling.

        None when the rows predate the column — an unanswerable question, which
        is not the same answer as "none".
        """
        blind = {r.bom_name: r for r in rows if not r.graph_aware}
        graph = {r.bom_name: r for r in rows if r.graph_aware}
        paired = sorted(set(blind) & set(graph))
        if not paired or any(
            blind[b].mc_cvar_95_saturated is None or graph[b].mc_cvar_95_saturated is None
            for b in paired
        ):
            return None
        return [
            b for b in paired
            if blind[b].mc_cvar_95_saturated and graph[b].mc_cvar_95_saturated
        ]

    _ceilings = [
        float(r.mc_cvar_95_ceiling) for r in (milp_stress + milp_targeted)
        if r.mc_cvar_95_ceiling is not None
    ]
    cvar95_ceiling = max(_ceilings) if _ceilings else None
    stress_tied = _ceiling_tied_boms(milp_stress)
    targeted_tied = _ceiling_tied_boms(milp_targeted)
    stress_saturated = None if stress_tied is None else bool(stress_tied)
    targeted_saturated = None if targeted_tied is None else bool(targeted_tied)

    # The measure that keeps resolving past the ceiling, with the SAME paired
    # bootstrap the published deltas carry — a bare mean here would repeat the
    # exact defect item 12 fixed. Kept in its own dict so `intervals` stays the
    # set of intervals for the five PUBLISHED deltas and its partition contract
    # (significant_metrics | non_significant_metrics) is unchanged.
    def _p_total(b: Any, g: Any) -> Optional[float]:
        bv, gv = b.mc_p_total_shortfall, g.mc_p_total_shortfall
        return None if (bv is None or gv is None) else bv - gv

    shortfall_intervals: Dict[str, PairedBootstrapCI] = {}
    for _metric, _rows in (
        ("stress_p_total_shortfall_reduction", milp_stress),
        ("targeted_p_total_shortfall_reduction", milp_targeted),
    ):
        _names, _deltas, _changed = _paired_panel(_rows, _p_total)
        if _deltas:
            shortfall_intervals[_metric] = _interval(
                _metric, "probability_0_1", _names, _deltas, _changed
            )
    stress_pts_reduction = (
        shortfall_intervals["stress_p_total_shortfall_reduction"].mean
        if "stress_p_total_shortfall_reduction" in shortfall_intervals else None
    )
    targeted_pts_reduction = (
        shortfall_intervals["targeted_p_total_shortfall_reduction"].mean
        if "targeted_p_total_shortfall_reduction" in shortfall_intervals else None
    )

    def _tied_clause(scenario: str, tied: Optional[List[str]], panel: List[Any]) -> str:
        n_pairs = len({r.bom_name for r in panel if not r.graph_aware}
                      & {r.bom_name for r in panel if r.graph_aware})
        if not tied:
            return ""
        return (
            f"{len(tied)} of {n_pairs} {scenario} BOM pairs ({', '.join(tied)})"
        )

    _clauses = [
        c for c in (
            _tied_clause("stress", stress_tied, milp_stress),
            _tied_clause("targeted", targeted_tied, milp_targeted),
        ) if c
    ]
    if not _measured_rows:
        saturation_note = (
            "CVaR-95 saturation is NOT KNOWN for this run: its rows predate the "
            "mc_cvar_95_saturated / mc_p_total_shortfall columns. Re-run the "
            "benchmark seed pipeline to measure it. Until then do not read a 0.0 "
            "cvar95 reduction as evidence that the two arms are equally exposed "
            "-- it may be the ceiling."
        )
    elif _clauses:
        saturation_note = (
            f"CVaR-95 IS PINNED AT ITS CEILING on part of this panel. mc_cvar_95 is "
            f"a mean over the worst-5% tail of a BOUNDED quantity, so it cannot "
            f"exceed {cvar95_ceiling:.4f} = 1 + EMERGENCY_COST_PREMIUM. "
            f"{'; '.join(_clauses)} have BOTH arms on that ceiling, so their cvar95 "
            f"delta is exactly 0.0 BY ARITHMETIC and the scenario mean is diluted by "
            f"zeros the metric had no room to avoid. A tie there is NOT evidence of "
            f"equal exposure. The measure that keeps resolving is "
            f"p_total_shortfall = P(every BOM line unfulfillable), a mean over ALL "
            f"scenarios rather than over the tail; its reduction and interval are "
            f"served beside the cvar95 figures."
        )
    else:
        saturation_note = (
            f"No BOM pair is ceiling-tied: on every pair at least one arm sits below "
            f"the CVaR-95 ceiling of {cvar95_ceiling:.4f}, so the cvar95 reductions "
            f"above are measurements rather than arithmetic. p_total_shortfall is "
            f"published beside them anyway, because it is the measure that would "
            f"survive if the panel ever did saturate."
        )
    # ── Paired bootstrap CIs over the BOM clusters (item 12) ──────────────────
    # No re-solve: every per-BOM delta below is read straight out of the rows the
    # run already wrote. The BOM is the cluster and the BOM is what is resampled.
    def _risk(b, g) -> Optional[float]:
        bv, gv = b.plan_cascade_risk, g.plan_cascade_risk
        return None if (bv is None or gv is None) else bv - gv

    def _cvar(b, g) -> Optional[float]:
        bv, gv = b.mc_cvar_95, g.mc_cvar_95
        return None if (bv is None or gv is None) else bv - gv

    def _premium(b, g) -> Optional[float]:
        return _pct_delta(b.total_cost_usd, g.total_cost_usd)

    _panels = [
        ("stress_cascade_risk_reduction", "share_0_1", milp_stress, _risk),
        ("stress_cvar95_reduction", "cost_multiplier", milp_stress, _cvar),
        ("targeted_cascade_risk_reduction", "share_0_1", milp_targeted, _risk),
        ("targeted_cvar95_reduction", "cost_multiplier", milp_targeted, _cvar),
        ("nominal_cost_premium_pct", "percent", milp_nominal, _premium),
    ]
    intervals: Dict[str, PairedBootstrapCI] = {}
    for metric, units, panel_rows, fn in _panels:
        names, deltas, changed = _paired_panel(panel_rows, fn)
        intervals[metric] = _interval(metric, units, names, deltas, changed)

    sig = [k for k, v in intervals.items() if v.significant]
    nonsig = [k for k, v in intervals.items() if not v.significant]

    # ── Interpretation, COMPOSED from the four reductions and their intervals ──
    # Built here, after the bootstrap, so it can consult significance. Every clause
    # is generated from the value it describes. A hardcoded sentence used to sit
    # above this and published "the graph-aware arm lowered both plan cascade risk
    # and the CVaR-95 tail" whenever no reduction was exactly 0.0 -- while
    # stress_cascade_risk_reduction was -0.0833 (the arm had RAISED it) and that
    # metric's own interval covered zero. The branch tested only for exact zero and
    # never looked at sign.
    # Sign convention: a reduction is mean(blind - graph), so POSITIVE means the
    # graph-aware arm scored LOWER, i.e. a genuine reduction.
    def _verdict(name: str, value: float) -> str:
        if abs(value) < 1e-9:
            return f"{name} is exactly 0.0 -- the two arms scored identically"
        direction = "lowered" if value > 0 else "RAISED"
        ci = intervals.get(name)
        if ci is not None and ci.significant:
            return f"{name} {direction} it by {abs(value):.4g}, interval excludes zero"
        return (
            f"{name} {direction} it by {abs(value):.4g}, but its interval covers zero "
            f"-- not quotable as a result"
        )

    _wrong_way = [k for k, v in reductions.items() if v < -1e-9]
    _survives = [
        k for k, v in reductions.items()
        if v > 1e-9 and (intervals.get(k) is not None and intervals[k].significant)
    ]
    resil_interpretation = (
        f"{len(_survives)} of {len(reductions)} reductions are both positive and "
        + (f"survive their interval ({', '.join(_survives)}). " if _survives
           else "none survive their interval. ")
        + (f"{len(_wrong_way)} went the WRONG WAY -- the graph-aware arm scored WORSE "
           f"than blind on {', '.join(_wrong_way)}. " if _wrong_way else "")
        + (f"{len(flat)} are exactly 0.0 ({', '.join(flat)}), which is a MEASUREMENT, "
           f"not a gap. " if flat else "")
        + "Per metric: " + "; ".join(_verdict(k, v) for k, v in reductions.items()) + ". "
        + "Check measured_values for the two arm means behind each. "
        + saturation_note
    )
    _nominal_ci = intervals["nominal_cost_premium_pct"]
    n_panel = _nominal_ci.n
    n_eff = _nominal_ci.n_effective
    inference_note = (
        f"Every delta above is a mean over {n_panel} BOMs and now carries a 95% "
        f"paired percentile-bootstrap CI ({BOOTSTRAP_N:,} resamples, seed "
        f"{BOOTSTRAP_SEED}) that resamples the BOM CLUSTERS over the per-BOM "
        f"values this run already stored — nothing was re-solved and no published "
        f"mean moved. {n_eff} of {n_panel} BOMs select a different plan in the two "
        f"arms; the rest contribute an exact 0.0 to every delta, so both the "
        f"full-panel and effective-panel intervals are published. "
        + (
            f"{len(sig)} of {len(intervals)} deltas have an interval that excludes "
            f"zero ({', '.join(sig)}). "
            if sig else
            f"NONE of the {len(intervals)} deltas has an interval excluding zero. "
        )
        + (
            f"{len(nonsig)} do not ({', '.join(nonsig)}) and MUST NOT be quoted as "
            f"results: on {n_panel} clusters they are not distinguishable from zero. "
            if nonsig else
            "All of them exclude zero. "
        )
        + "This is the same bar the lead-time model is held to on the ML page, "
        "which is the point: a benchmark published without intervals beside a "
        "model card that requires them is a double standard, not a finding."
    )

    resilience = ResilienceSection(
        # graph-aware vs blind NOMINAL premium — same figure as cost_delta_pct
        nominal_cost_premium_pct=cost_delta_pct,
        **reductions,
        cvar95_ceiling=cvar95_ceiling,
        stress_cvar95_saturated=stress_saturated,
        targeted_cvar95_saturated=targeted_saturated,
        stress_cvar95_ceiling_tied_boms=stress_tied,
        targeted_cvar95_ceiling_tied_boms=targeted_tied,
        cvar95_saturated_rows=(
            sum(1 for r in _measured_rows if r.mc_cvar_95_saturated)
            if _measured_rows else None
        ),
        cvar95_rows_measured=(len(_measured_rows) or None),
        stress_p_total_shortfall_reduction=stress_pts_reduction,
        targeted_p_total_shortfall_reduction=targeted_pts_reduction,
        p_total_shortfall_intervals=shortfall_intervals,
        saturation_note=saturation_note,
        measured_values=measured,
        flat_metrics=flat,
        interpretation=resil_interpretation,
        intervals=intervals,
        n_boms=n_panel,
        n_effective_boms=n_eff,
        n_effective_definition=_N_EFFECTIVE_DEFINITION,
        significant_metrics=sig,
        non_significant_metrics=nonsig,
        inference_note=inference_note,
    )

    # Tradeoff: find BOM where graph-aware is WORST (highest positive delta on any axis)
    # If all negative, pick closest-to-neutral (smallest absolute negative delta)
    best_bom = bom_deltas[0] if bom_deltas else None
    best_axis = "cost"
    best_delta: Optional[float] = None
    best_baseline = 0.0
    best_ga = 0.0

    for bd in bom_deltas:
        axis_vals = [
            ("cost", bd.cost_delta_pct,
             baseline_by_bom[bd.bom_name].total_cost_usd,
             graph_aware_by_bom[bd.bom_name].total_cost_usd),
            ("eta", bd.eta_delta_pct,
             baseline_by_bom[bd.bom_name].eta_p50_days,
             graph_aware_by_bom[bd.bom_name].eta_p50_days),
            ("risk", bd.cascade_risk_delta_pct,
             baseline_by_bom[bd.bom_name].plan_cascade_risk or 0.0,
             graph_aware_by_bom[bd.bom_name].plan_cascade_risk or 0.0),
        ]
        for axis, delta, b_val, g_val in axis_vals:
            if best_delta is None:
                best_bom = bd
                best_axis = axis
                best_delta = delta
                best_baseline = b_val
                best_ga = g_val
            else:
                # Prefer highest positive delta (worst outcome for graph-aware)
                if delta > best_delta:
                    best_bom = bd
                    best_axis = axis
                    best_delta = delta
                    best_baseline = b_val
                    best_ga = g_val

    # ── The tradeoff sentence, DERIVED from the two stored plans ───────────────
    # It used to be a hardcoded template asserting that graph-aware "routes around"
    # the cheapest distributor. On run 7 that is false: the tradeoff BOM is
    # iot_sensor_node, the blind plan is {DigiKey}, and the graph-aware plan is
    # {DigiKey, Verical} — the distributor is still there and a second one was
    # added. Nothing in the old sentence was computed, so it could not have been
    # right except by luck. It is now read off the plans, and the plans ship with it.
    _t_name = best_bom.bom_name if best_bom else "unknown"
    _blind_row = baseline_by_bom.get(_t_name)
    _graph_row = graph_aware_by_bom.get(_t_name)

    def _dist_names(row) -> List[str]:
        names = getattr(row, "selected_distributor_names", None) or []
        return sorted(str(n) for n in names) if isinstance(names, list) else []

    _blind_names = _dist_names(_blind_row) if _blind_row is not None else []
    _graph_names = _dist_names(_graph_row) if _graph_row is not None else []
    _kept = sorted(set(_blind_names) & set(_graph_names))
    _dropped = sorted(set(_blind_names) - set(_graph_names))
    _added = sorted(set(_graph_names) - set(_blind_names))

    def _join(names: Sequence[str]) -> str:
        names = list(names)
        if not names:
            return "none"
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " and " + names[-1]

    _axis_word = {"cost": "more expensive", "eta": "slower", "risk": "riskier"}.get(
        best_axis, best_axis
    )
    _delta_txt = f"+{best_delta:.1f}%" if best_delta is not None else "unchanged"

    if not _blind_names or not _graph_names:
        _plan_story = (
            "This run did not store the chosen distributors for one of the two arms, "
            "so what changed between the plans cannot be stated and is not guessed at."
        )
    elif _dropped and not _kept:
        _plan_story = (
            f"The graph-aware arm REPLACED the blind plan outright: it dropped "
            f"{_join(_dropped)} and sourced from {_join(_added)} instead. This one is "
            "a genuine re-route."
        )
    elif _dropped:
        _plan_story = (
            f"The graph-aware arm kept {_join(_kept)}, dropped {_join(_dropped)} and "
            f"added {_join(_added)}."
        )
    elif _added:
        _plan_story = (
            f"NOTHING WAS ROUTED AROUND. The graph-aware arm kept every distributor "
            f"the blind plan used ({_join(_kept)}) and ADDED {_join(_added)} on top. "
            "The blind plan put the whole BOM on a single distributor, which is "
            "exactly the case the hard dual-source constraint forbids, so a second "
            "supplier was opened — and the extra landed cost is that supplier's "
            "fixed freight fee, not a different sourcing decision."
        )
    else:
        _plan_story = (
            f"Both arms chose the identical plan ({_join(_kept)}); the difference on "
            "this axis does not come from a change of supplier."
        )

    # ── Panel-wide attribution of the graph-aware arm's two ingredients ────────
    # `seeds/run_benchmark.py` solves the graph-aware arm with BOTH a soft
    # betweenness-centrality surcharge in the objective AND a hard dual-source
    # constraint (`require_dual_source=True`). The constraint only fires when the
    # blind plan consolidated onto ONE distributor. So the BOMs where the blind
    # plan already used two or more distributors are the natural control: there the
    # surcharge acts alone. If those BOMs come back with an identical plan, the
    # surcharge changed nothing it was free to change — and that is a stronger,
    # checkable claim than the one this field used to make.
    _solo, _multi, _multi_same = 0, 0, 0
    _opened_blind = _opened_graph = 0
    for _b in sorted(set(baseline_by_bom) & set(graph_aware_by_bom)):
        _bn = _dist_names(baseline_by_bom[_b])
        _gn = _dist_names(graph_aware_by_bom[_b])
        _opened_blind += len(_bn)
        _opened_graph += len(_gn)
        if len(_bn) <= 1:
            _solo += 1
        else:
            _multi += 1
            if _bn == _gn:
                _multi_same += 1
    if _multi and _multi_same == _multi:
        _mechanism = (
            f"ATTRIBUTION: on the {_multi} of {_solo + _multi} BOMs where the blind "
            "MILP already used two or more distributors — so the HARD dual-source "
            "constraint could not fire and the SOFT centrality surcharge was acting "
            "alone — the graph-aware plan is identical to the blind one, distributor "
            "for distributor. Every measurable difference in this arm therefore comes "
            "from the hard constraint. Across the panel the arm never removes a "
            f"supplier, it adds them: {_opened_blind} distributor-selections blind "
            f"vs {_opened_graph} graph-aware. The centrality surcharge is real code "
            "in the objective, but on this catalogue it did not change a single plan "
            "it was free to change, and it is not what the cost premium is buying."
        )
    elif _multi:
        _mechanism = (
            f"ATTRIBUTION: {_multi} of {_solo + _multi} BOMs were already "
            "multi-sourced by the blind MILP, so the hard dual-source constraint "
            f"could not fire on them; the soft centrality surcharge changed the plan "
            f"on {_multi - _multi_same} of those. Across the panel the arm opens "
            f"{_opened_graph} distributor-selections against the blind arm's "
            f"{_opened_blind}."
        )
    else:
        _mechanism = (
            f"ATTRIBUTION: the blind MILP consolidated all {_solo} BOMs onto a single "
            "distributor, so the hard dual-source constraint fires on every one and "
            "there is no BOM on which the soft centrality surcharge acts alone. This "
            "run cannot separate the two ingredients, and no separation is claimed."
        )

    tradeoff = TradeoffEntry(
        bom_name=_t_name,
        losing_axis=best_axis,
        baseline_value=best_baseline,
        graph_aware_value=best_ga,
        delta_pct=best_delta if best_delta is not None else 0.0,
        narrative=(
            f"{_t_name}: graph-aware is {_delta_txt} {_axis_word} than the blind "
            f"MILP — the worst it does on any BOM or axis in this run. {_plan_story}"
        ),
        blind_distributors=_blind_names,
        graph_aware_distributors=_graph_names,
        distributors_kept=_kept,
        distributors_dropped=_dropped,
        distributors_added=_added,
        mechanism=_mechanism,
    )

    # feeds_fallback: True if any row has a False feed value
    feeds_fallback = False
    for row in all_rows:
        if row.feeds_available:
            if isinstance(row.feeds_available, dict):
                if any(v is False for v in row.feeds_available.values()):
                    feeds_fallback = True
                    break

    # ── The honest, volume-dependent headline (replaces the retracted number) ──
    curve = _load_volume_curve()
    headline = _build_headline(curve)
    by_mult = {p.multiplier: p for p in curve.points}
    proto_point = by_mult.get(1)
    prod_points = [p for p in curve.points if p.multiplier >= _PRODUCTION_MULTIPLIER_MIN]
    # Pick the deepest production point that still has a real cohort behind it.
    prod_point = max(prod_points, key=lambda p: p.boms_feasible) if prod_points else None

    # Mean units per BOM across this run — the reason its savings_pct is a prototype
    # figure and not a procurement number.
    run_units: Optional[int] = None
    try:
        per_bom_units = []
        seen_boms = set()
        for row in all_rows:
            if row.bom_name in seen_boms:
                continue
            seen_boms.add(row.bom_name)
            items = row.bom_items_json or []
            if isinstance(items, list):
                per_bom_units.append(
                    sum(int(i.get("quantity", 0)) for i in items if isinstance(i, dict))
                )
        run_units = round(_safe_mean(per_bom_units)) if per_bom_units else None
    except Exception:  # noqa: BLE001 — a malformed items blob must not 500 the endpoint
        run_units = None

    # ── The offer-pool disclosure, assembled from this run + the sweep control ──
    # This is the single biggest known flaw in the headline percentage and it was
    # published nowhere the reader could see it: the greedy arm shops the full
    # international pool and pays the AIR fixed fee for every Chinese or
    # Singaporean supplier it opens, while the MILP arm is confined to domestic
    # distributors. Every number below is counted off this run's own stored plans
    # or read from the committed sweep artifact — none is asserted.
    _dom_fee = curve.fixed_fee_per_supplier_usd
    _intl_fee = curve.international_fixed_fee_per_supplier_usd
    _fee_clause = (
        f" Opening an international supplier costs the air-freight fixed fee of "
        f"${_intl_fee:,.2f} against ${_dom_fee:,.2f} for a domestic one, so part of "
        "the gap is a shipping policy, not an optimization."
        if _dom_fee and _intl_fee else ""
    )
    # ── The greedy side of the match, read out of THIS run's own rows ─────────
    _matched_pts: Dict[str, Optional[float]] = {
        "weaker": None, "catalogue": None, "optimizer": None,
    }
    if greedy_add_dom_stats is not None and greedy_stats is not None:
        # Both handicaps expressed as percentage points of the unmatched headline,
        # so they sum to it: naive-global = heuristic + catalogue + optimizer.
        _matched_pts = {
            "weaker": round(
                greedy_stats["pooled_savings_pct"]
                - greedy_add_stats["pooled_savings_pct"], 2,
            ) if greedy_add_stats is not None else None,
            "catalogue": round(
                greedy_add_stats["pooled_savings_pct"]
                - greedy_add_dom_stats["pooled_savings_pct"], 2,
            ) if greedy_add_stats is not None else None,
            "optimizer": round(greedy_add_dom_stats["pooled_savings_pct"], 2),
        }
        _matched_finding = (
            "MEASURED, both directions. Re-solving the two greedy heuristics on the "
            "MILP's own domestic-only pool takes the pooled saving from "
            f"{greedy_stats['pooled_savings_pct']:.2f}% (naive heuristic, full "
            f"international catalogue) to "
            f"{greedy_add_dom_stats['pooled_savings_pct']:.2f}% (ADD heuristic, "
            f"matched catalogue) over {greedy_add_dom_stats['n_boms']} BOMs. Of the "
            f"{greedy_stats['pooled_savings_pct']:.2f} points, "
            f"{_matched_pts['weaker']:.2f} were the baseline being the naive "
            f"heuristic and {_matched_pts['catalogue']:.2f} were it shopping a "
            f"catalogue the optimizer was not allowed; "
            f"{_matched_pts['optimizer']:.2f} points are the optimizer's. The other "
            "direction contributes nothing: re-solved on greedy's full global pool "
            "the MILP returns an identical plan at an identical cost, so the "
            "optimizer's restriction was never binding."
        )
        _resolved_side = (
            "RESOLVED. This run carries `greedy_dom` and `greedy_add_dom` — the same "
            "two heuristics re-solved on the MILP's domestic-only catalogue — so the "
            "greedy side of the match is measured rather than argued. The like-for-"
            f"like figure is {abs(greedy_add_dom_stats['pooled_savings_pct']):.2f}%; "
            "see `savings_pct_matched_pool` and the `is_primary` baseline. The "
            "unmatched figure is retained for contrast and must always be labelled "
            "'vs a naive, globally-shopping baseline'."
        )
        _statement = (
            "POOLS NOW MATCHED — but `savings_pct` itself is still NOT A LIKE-FOR-LIKE "
            "COMPARISON. It remains measured against a baseline solved on the FULL "
            "INTERNATIONAL offer pool "
            "(us_only=False) while the MILP's plan comes from the `balanced` "
            "strategy, which is DOMESTIC-ONLY (us_only_sourcing=True): on this run "
            f"that baseline opened {greedy_intl} international supplier(s) out of "
            f"{greedy_opened} across {len(opt_boms)} BOMs, against {milp_intl} out of "
            f"{milp_opened} for the MILP." + _fee_clause + " The like-for-like "
            f"comparison is now solved beside it and is "
            f"{abs(greedy_add_dom_stats['pooled_savings_pct']):.2f}%, not "
            f"{abs(greedy_stats['pooled_savings_pct']):.2f}%."
        )
    else:
        _matched_finding = None
        _resolved_side = (
            "The control above matches the pools by widening the MILP's. The other "
            "direction — re-solving the GREEDY arm on the MILP's domestic-only pool "
            "— is NOT computed on this run, so no number for it is served here. The "
            "`greedy_dom` / `greedy_add_dom` arms exist in the benchmark seed pipeline "
            "from 2026-09-03 and are present from run_id=8 onward; until this run is "
            "re-solved, treat greedy's access to cheap international parts (which it "
            "buys at the air-freight fixed fee, and which it handles badly) as an "
            "unquantified part of this headline."
        )
        _statement = (
            "NOT A LIKE-FOR-LIKE COMPARISON. The greedy baseline is solved on the "
            "FULL INTERNATIONAL offer pool (us_only=False) while the MILP's plan is "
            "taken from the `balanced` strategy, which is DOMESTIC-ONLY "
            "(us_only_sourcing=True). On this run the greedy arm opened "
            f"{greedy_intl} international supplier(s) out of {greedy_opened} across "
            f"{len(opt_boms)} BOMs; the MILP opened {milp_intl} out of "
            f"{milp_opened}." + _fee_clause
        )

    pool_asymmetry = PoolAsymmetry(
        matched=greedy_add_dom_stats is not None,
        statement=_statement,
        greedy_pool="all distributors, domestic and international (us_only=False)",
        milp_pool="domestic (US) distributors only (balanced.us_only_sourcing=True)",
        greedy_suppliers_opened=greedy_opened,
        greedy_international_suppliers_opened=greedy_intl,
        milp_suppliers_opened=milp_opened,
        milp_international_suppliers_opened=milp_intl,
        domestic_fixed_fee_usd=_dom_fee,
        international_fixed_fee_usd=_intl_fee,
        control_source=_control["source"] if _control else None,
        control_n_boms=_control["n_boms"] if _control else None,
        control_greedy_cost_usd=_control["greedy_cost_usd"] if _control else None,
        control_milp_domestic_pool_cost_usd=(
            _control["milp_domestic_pool_cost_usd"] if _control else None
        ),
        control_milp_full_pool_cost_usd=(
            _control["milp_full_pool_cost_usd"] if _control else None
        ),
        control_savings_pct_domestic_pool=(
            _control["savings_pct_domestic_pool"] if _control else None
        ),
        control_savings_pct_full_pool=(
            _control["savings_pct_full_pool"] if _control else None
        ),
        control_finding=_control["finding"] if _control else None,
        unmatched_side=_resolved_side,
        matched_baseline_arm=(
            greedy_add_dom_stats and "greedy_add_dom"
        ) or None,
        matched_n_boms=(
            greedy_add_dom_stats["n_boms"] if greedy_add_dom_stats else None
        ),
        matched_greedy_cost_usd=(
            greedy_dom_stats["total_cost_usd"] if greedy_dom_stats else None
        ),
        matched_greedy_add_cost_usd=(
            greedy_add_dom_stats["total_cost_usd"] if greedy_add_dom_stats else None
        ),
        matched_milp_cost_usd=(
            greedy_add_dom_stats["milp_total_cost_usd"] if greedy_add_dom_stats else None
        ),
        matched_savings_pct_vs_greedy=(
            greedy_dom_stats["pooled_savings_pct"] if greedy_dom_stats else None
        ),
        matched_savings_pct_vs_greedy_add=(
            greedy_add_dom_stats["pooled_savings_pct"] if greedy_add_dom_stats else None
        ),
        points_from_weaker_heuristic=_matched_pts["weaker"],
        points_from_wider_baseline_catalogue=_matched_pts["catalogue"],
        points_from_optimizer=_matched_pts["optimizer"],
        matched_finding=_matched_finding,
    )

    run_tag = all_rows[0].run_tag
    run_tag_meaning = {
        "benchmark": (
            "Every live risk feed was available when this run was scored, so the "
            "feed-derived inputs are real."
        ),
        "static_fallback": (
            "At least one live risk feed was UNAVAILABLE when this run was scored, so "
            "its feed-derived inputs fell back to static values. Cost and supplier "
            "consolidation are unaffected (they come from the offer table); the "
            "geopolitical-risk inputs are not live. See feeds_fallback."
        ),
    }.get(run_tag, f"Unrecognised run_tag '{run_tag}'.")

    return BenchmarkSummaryResponse(
        run_id=target_run_id,
        run_tag=run_tag,
        run_tag_meaning=run_tag_meaning,
        timestamp=timestamp_str,
        n_boms=n_boms,
        headline=headline,
        volume_curve=curve,
        decomposition_at_prototype_volume=(
            _decompose(proto_point, "prototype volume (1x)") if proto_point else None
        ),
        decomposition_at_production_volume=(
            _decompose(prod_point, f"production volume ({prod_point.multiplier}x)")
            if prod_point else None
        ),
        headline_retracted=True,
        retraction_note=headline.retraction_reason,
        realistic_savings_pct_low=headline.savings_pct_at_production_volume_low,
        realistic_savings_pct_high=headline.savings_pct_at_production_volume_high,
        fixed_fee_share_of_savings_pct=(
            proto_point.fixed_fee_share_of_saving_pct if proto_point else None
        ),
        fixed_fee_share_of_cost_pct=(
            proto_point.greedy_fixed_share_of_cost_pct if proto_point else None
        ),
        fixed_fee_per_supplier_usd=curve.fixed_fee_per_supplier_usd,
        mean_units_per_bom=run_units,
        caveats=[
            "savings_pct on this response is measured on THIS RUN ONLY, whose BOMs are "
            f"4 lines and {run_units if run_units is not None else '5-9'} total units. "
            "It is a prototype-volume figure dominated by a fixed per-supplier fee. Do "
            "not quote it as the optimizer's saving — quote headline.statement.",
            RETRACTED_HEADLINE.capitalize() + " is RETRACTED and must not be repeated.",
            pool_asymmetry.statement,
            "savings_pct is POOLED. " + _POOLED_DEFINITION + " The unweighted "
            "mean of per-BOM percentages is a DIFFERENT statistic and is published "
            "separately as savings_pct_mean_of_boms; this endpoint served THAT one as "
            "the headline until 2026-09-03, beside the pooled volume curve.",
            "savings_pct compares the MILP against the WEAKEST baseline in the "
            "database on both axes — the naive heuristic, shopping a wider "
            "catalogue than the optimizer was allowed. `baselines` carries all "
            "four arms; the one flagged is_primary (greedy_add_dom: ADD heuristic, "
            "matched domestic pool) is the like-for-like comparison and a "
            "materially smaller number, served as savings_pct_matched_pool.",
            *(
                [
                    "THE LIKE-FOR-LIKE FIGURE IS "
                    f"{abs(savings_pct_matched_pool):.2f}%, not "
                    f"{abs(savings_pct):.2f}%. " + savings_pct_matched_pool_note
                ]
                if savings_pct_matched_pool is not None else []
            ),
            _CASCADE_RISK_METRIC,
            "The resilience section (graph-aware vs blind MILP under disruption) is a "
            "separate story on a separate axis and is NOT affected by anything in the "
            "volume curve.",
        ],
        savings_pct=round(savings_pct, 2),
        savings_units="percent",
        savings_pct_aggregation=_POOLED_DEFINITION,
        savings_pct_mean_of_boms=round(savings_pct_mean_of_boms, 2),
        savings_pct_mean_of_boms_note=_MEAN_OF_BOMS_DEFINITION,
        savings_pct_is_prototype_volume_only=True,
        savings_pct_display_label=(
            f"{savings_pct:.1f}% pooled at "
            f"{f'{run_units}-unit' if run_units is not None else 'prototype'} "
            "prototype volume, against the NAIVE greedy baseline shopping a wider "
            "supplier pool than the optimizer was allowed — a per-supplier fee "
            "artifact, not the optimizer's saving. See headline, baselines and "
            "pool_asymmetry."
            + (
                f" Like-for-like (ADD heuristic, matched supplier pool): "
                f"{savings_pct_matched_pool:.1f}%."
                if savings_pct_matched_pool is not None else ""
            )
        ),
        baselines=baselines,
        pool_asymmetry=pool_asymmetry,
        savings_pct_matched_pool=savings_pct_matched_pool,
        savings_pct_matched_pool_arm=savings_pct_matched_pool_arm,
        savings_pct_matched_pool_note=savings_pct_matched_pool_note,
        primary_claim=primary_claim,
        savings_usd_per_bom=round(savings_usd_per_bom, 2),
        savings_usd_annualized=round(savings_usd_annualized, 2),
        annual_reorders=ANNUAL_REORDERS,
        avg_suppliers_greedy=round(avg_suppliers_greedy, 2),
        avg_suppliers_milp=round(avg_suppliers_milp, 2),
        benchmark_volume_note=(
            f"This run's BOMs total "
            f"{run_units if run_units is not None else 'a handful of'} units per BOM. "
            "Every percentage below is measured at that volume."
        ),
        resilience=resilience,
        cost_delta_pct=cost_delta_pct,
        cost_delta_usd=round(cost_delta_usd, 2),
        cost_delta_units="usd",
        baseline_spend_at_risk_usd=round(baseline_spend_at_risk_usd, 2),
        eta_delta_pct=eta_delta_pct,
        co2_delta_pct=co2_delta_pct,
        cascade_risk_delta_pct=cascade_risk_delta_pct,
        cascade_risk_metric=_CASCADE_RISK_METRIC,
        monte_carlo=monte_carlo,
        tradeoff=tradeoff,
        bom_deltas=bom_deltas,
        feeds_fallback=feeds_fallback,
        materiality_threshold_pct=MATERIALITY_THRESHOLD_PCT,
        materiality_threshold_basis=_MATERIALITY_BASIS,
    )


@router.get("/fiedler-curve", response_model=FiedlerCurveResponse)
def get_fiedler_curve():
    """
    Return the sequential-removal Fiedler λ₂ curve from pre-computed GraphState.

    Step 0 is the baseline (no removal). Subsequent steps show λ₂ after removing
    the most-central distributor.

    `collapsed_boms` lists the reference BOMs that have at least one line with no
    remaining supplier once every distributor up to that step is gone. It is
    computed in `main.compute_fiedler_curve` against the SAME graph λ₂ is computed
    on (the 80% training partition of the offer table), so the two columns of the
    chart describe one network, not two. `boms_checked` and `bom_source` disclose
    how many BOMs the check covered and where they came from; when `boms_checked`
    is 0 the check did not run and an empty `collapsed_boms` means nothing.
    """
    gs = _require_graph_state()

    if not gs.fiedler_curve:
        raise HTTPException(
            status_code=503,
            detail="Fiedler curve not computed — check server startup logs",
        )

    points = []
    for entry in gs.fiedler_curve:
        points.append(FiedlerPoint(
            step=entry["step"],
            removed=entry.get("removed"),
            removed_name=entry.get("removed_name"),
            lambda2=entry["lambda2"],
            delta_pct=entry["delta_pct"],
            collapsed_boms=entry.get("collapsed_boms", []),
        ))

    head = gs.fiedler_curve[0]
    baseline_lambda2 = head["lambda2"]
    return FiedlerCurveResponse(
        points=points,
        baseline_lambda2=baseline_lambda2,
        boms_checked=int(head.get("boms_checked", 0) or 0),
        bom_source=str(head.get("bom_source", "") or ""),
    )


@router.get("/cascade-heatmap", response_model=CascadeHeatmapResponse)
def get_cascade_heatmap(db: Session = Depends(get_db)):
    """
    Return per-distributor unfulfilled-BOM-line exposure for maplibre heatmap-layer
    rendering.

    If no optimization_runs rows exist, returns an empty points list (not 404).
    Weight is the mean `plan_cascade_risk` across the runs in which each distributor
    was selected, normalized to [0, 1] against the most-exposed distributor.

    2026-08 audit fixes:
      * reads `plan_cascade_risk` (populated, 0.0–1.0 with real variance) instead of
        `cascade_risk_score`, which is exactly 0.0 in all 234 rows ever written — see
        _CASCADE_RISK_METRIC;
      * drops the `normalized_weight > 0` filter. A distributor whose plans never
        collapse has weight 0.0, which is a RESULT and belongs on the map; discarding
        it is how a fully-populated database produced `{"points": []}`;
      * when nothing scores above zero, says so in `note` instead of returning a
        structurally-empty success.
    """
    _require_graph_state()

    from app.models.optimization_run import OptimizationRun
    from app.models.distributor import Distributor

    # Find latest run_id
    latest = (
        db.query(OptimizationRun.run_id)
        .order_by(OptimizationRun.run_id.desc())
        .first()
    )
    if latest is None:
        return CascadeHeatmapResponse(
            points=[], metric=_CASCADE_RISK_METRIC,
            note=(
                "No optimization_runs rows exist. Run the benchmark seed pipeline "
                "(see backend/seeds/) to populate them."
            ),
        )

    target_run_id = latest[0]
    all_rows = (
        db.query(OptimizationRun)
        .filter(OptimizationRun.run_id == target_run_id)
        .all()
    )

    if not all_rows:
        return CascadeHeatmapResponse(
            points=[], metric=_CASCADE_RISK_METRIC, run_id=target_run_id,
            note=f"run_id={target_run_id} has no rows.",
        )

    # Compute per-distributor mean plan_cascade_risk
    dist_risk_accumulator: Dict[int, List[float]] = {}
    for row in all_rows:
        if row.plan_cascade_risk is None:
            continue
        dist_ids = row.selected_distributor_ids or []
        if isinstance(dist_ids, list):
            for did in dist_ids:
                if isinstance(did, int):
                    dist_risk_accumulator.setdefault(did, []).append(
                        float(row.plan_cascade_risk)
                    )

    if not dist_risk_accumulator:
        return CascadeHeatmapResponse(
            points=[], metric=_CASCADE_RISK_METRIC, run_id=target_run_id,
            note=(
                f"run_id={target_run_id} has {len(all_rows)} rows but none carry both a "
                "plan_cascade_risk value and a selected_distributor_ids list, so no "
                "distributor can be scored. Re-run the benchmark pipeline."
            ),
        )

    # Compute raw weights
    raw_weights: Dict[int, float] = {
        did: _safe_mean(scores)
        for did, scores in dist_risk_accumulator.items()
    }
    max_raw = max(raw_weights.values())
    # Divisor only — a zero max means every distributor scores 0.0, which is reported
    # rather than hidden.
    denom = max_raw if max_raw > 0 else 1.0

    # Fetch distributor details
    dist_ids_list = list(raw_weights.keys())
    distributors = (
        db.query(Distributor)
        .filter(Distributor.id.in_(dist_ids_list))
        .all()
    )
    dist_map = {d.id: d for d in distributors}

    points = []
    missing_geo = 0
    for did, raw_w in raw_weights.items():
        dist = dist_map.get(did)
        if dist is None or dist.latitude is None or dist.longitude is None:
            missing_geo += 1
            continue
        points.append(HeatmapPoint(
            lat=dist.latitude,
            lng=dist.longitude,
            weight=round(raw_w / denom, 4),
            distributor_id=dist.id,
            distributor_name=dist.name,
        ))
    points.sort(key=lambda p: -p.weight)

    if max_raw <= 0:
        note = (
            f"All {len(points)} distributors score 0.0: no selected sourcing plan in "
            f"run_id={target_run_id} collapsed under the Monte Carlo. Points are still "
            "returned (weight 0.0) so the map shows the network rather than nothing."
        )
    else:
        note = (
            f"{sum(1 for p in points if p.weight > 0)} of {len(points)} distributors "
            f"carry non-zero collapse exposure; weights are normalized against the "
            f"most-exposed (raw max {max_raw:.4f})."
        )
    if missing_geo:
        note += f" {missing_geo} distributor(s) omitted for missing coordinates."

    return CascadeHeatmapResponse(
        points=points,
        metric=_CASCADE_RISK_METRIC,
        run_id=target_run_id,
        n_distributors_scored=len(points),
        max_raw_weight=round(max_raw, 4),
        note=note,
    )


@router.get("/single-source-components", response_model=SingleSourceComponentsResponse)
def get_single_source_components(db: Session = Depends(get_db)):
    """
    Return real component MPN + manufacturer + sole-source distributor from ORM joins.

    Reads GraphState.single_source_component_ids (frozenset[int]) then joins to
    Component and DistributorOffer tables to return authoritative catalog data.

    CRITICAL: mpn and manufacturer come ONLY from the Component ORM row — no fabricated
    strings, no distributor fields used as manufacturer values (VIZ-02, D-05).
    """
    gs = _require_graph_state()

    component_ids = list(gs.single_source_component_ids)
    if not component_ids:
        return SingleSourceComponentsResponse(components=[])

    from app.models.component import Component, DistributorOffer
    from app.models.distributor import Distributor

    components = (
        db.query(Component)
        .filter(Component.id.in_(component_ids))
        .all()
    )

    results = []
    for comp in components:
        # Find stocked offers for this component
        offers = (
            db.query(DistributorOffer)
            .filter(
                DistributorOffer.component_id == comp.id,
                DistributorOffer.stock > 0,
            )
            .all()
        )
        if not offers:
            # Fallback: any offer
            offers = (
                db.query(DistributorOffer)
                .filter(DistributorOffer.component_id == comp.id)
                .limit(1)
                .all()
            )
        if not offers:
            continue

        offer = offers[0]
        dist = (
            db.query(Distributor)
            .filter(Distributor.id == offer.distributor_id)
            .first()
        )
        if not dist:
            continue

        results.append(SingleSourceComponent(
            component_id=comp.id,
            mpn=comp.mpn,                              # REAL catalog MPN
            manufacturer=comp.manufacturer or "Unknown",   # REAL manufacturer name
            distributor_id=dist.id,
            distributor_name=dist.name,
        ))

    return SingleSourceComponentsResponse(components=results)


# ══════════════════════════════════════════════════════════════════════════════
# THE PRICE OF RESILIENCE — the diversification frontier
# ══════════════════════════════════════════════════════════════════════════════
# `/benchmark/summary` reports that graph-aware sourcing is measurably better
# against a TARGETED outage and shows no measurable effect under broad systemic
# stress. On its own that reads as a shrug. This endpoint serves the sweep that
# explains it and turns it into a number.
#
# The same sourcing MILP is re-solved subject to a hard "open at least k distinct
# distributors" constraint, each plan is costed, and each is simulated under the
# benchmark's OWN scenarios. k = 1 is the frontier's control arm: the constraint
# binds on nothing, so the plan IS the unconstrained cost-optimal plan — and it
# reproduces run 5's published `milp_blind` landed cost on 9 of 9 BOMs to the
# cent, which is what makes the sweep comparable to the published benchmark.
#
# This is a LOADER, exactly like `_load_volume_curve`. It pools and reshapes a
# checked-in artifact so the API can never drift from the published data; it does
# not re-run the sweep, does not touch the database, and cannot move a published
# number. A missing or unparseable artifact returns `available: false` with a
# reason, never a bare headline.

_FRONTIER_AGGREGATE_DEFINITION = (
    "Costs are the MEAN landed cost per BOM at that k. Deltas are PAIRED against "
    "the same BOM's own k=1 plan, then summarised with a 95% percentile bootstrap "
    "that resamples BOMs (10,000 resamples, seed 42). An interval that covers zero "
    "is not a result and is labelled as such. Risk deltas are risk REMOVED: "
    "positive means safer than k=1."
)

# The four caveats a reader needs before they quote anything off this frontier.
# They are served as data, not baked into the page, so the UI cannot soften them.
_FRONTIER_COST_AXIS_CAVEAT = (
    "THE COST AXIS IS LARGELY A FIXED-CHARGE ARTIFACT. Every opened distributor "
    "pays the same fixed freight charge (LTL_BASE_FEE_USD scaled by the balanced "
    "strategy's transport_penalty_scale of 1.5), so the marginal cost of the k-th "
    "supplier settles at $115-120 and the cost side of this frontier is close to "
    "linear in k almost by construction. The interesting axis is RISK, not cost."
)
_FRONTIER_SEED_CAVEAT = (
    "ONE SEED. Every point on the frontier uses the same 1,000 Monte Carlo "
    "scenarios at seed 42 — that is exactly what makes the comparison paired, and "
    "it also means these CIs contain BOM-level variation ONLY, with no "
    "Monte-Carlo error term. A second seed would move these numbers by an amount "
    "this study does not measure."
)
_FRONTIER_QUANTISATION_CAVEAT = (
    "cascade_risk IS QUANTISED. It is 1 - p50(fulfillment) over a 4-line BOM, so "
    "it can only take the values {0, 0.25, 0.5, 0.75, 1} and cannot resolve a "
    "change smaller than a quarter of a BOM. expected_shortfall "
    "(1 - mean(fulfillment)) is reported beside it precisely because it can. Where "
    "the two disagree on significance, the coarse one is the LESS informative "
    "measure, not the more conservative one."
)
_FRONTIER_INDEPENDENCE_CAVEAT = (
    "FAILURES ARE INDEPENDENT beyond a shared stress multiplier. run_monte_carlo "
    "fails distributors independently at a calibrated hazard; there is no "
    "propagation, no recovery, and no correlation between distributor failures "
    "except the common stress_factor. Diversification protects most against "
    "CORRELATED shocks, so an independent-failure model is the conservative place "
    "to measure its value, not a flattering one."
)
_FRONTIER_NESTING_CAVEAT = (
    "THE CONSTRAINT BOUNDS A COUNT AND THE OBJECTIVE IS STILL PURE COST. "
    "min_distributors = k says how many doors stay open, never WHICH doors, so "
    "the cheapest way to satisfy it is often to ABANDON the incumbent and buy a "
    "different, cheaper set. Because the k-supplier plan is not required to "
    "contain the 1-supplier plan, risk NEED NOT BE MONOTONE in k under broad "
    "stress: a BOM can be forced onto two suppliers and end up more exposed than "
    "it was on one, if the supplier it left had the lower hazard. That is a "
    "property of the CONSTRAINT. Whether this particular sweep actually exhibits "
    "it — in which measure, and on how many BOMs — is not asserted here: it is "
    "read off the artifact and served in `non_monotone_status`, beside the worst "
    "counter-example the scan found. Under a TARGETED outage the effect is "
    "one-directional — spreading always shrinks the blast radius of losing a "
    "single named hub — and that asymmetry is exactly the split the benchmark "
    "reports."
)

_FRONTIER_RECOMMENDED_K_BASIS = (
    "recommended_k is the step that removes targeted cascade risk MOST CHEAPLY: "
    "the lowest USD per unit of risk removed among the steps that carry a price "
    "at all. A step is only priced when its paired 95% bootstrap interval "
    "excludes zero, so an unmeasurable step can never be recommended — and "
    "neither can a step that removes real risk at multiples of the best price, "
    "which is the whole point of publishing the frontier rather than a single "
    "number. It is deliberately NOT 'the largest k that is still significant' — "
    "significance says a step does something, never that the something is worth "
    "its price — and the two rules have disagreed on this very frontier before, "
    "so the priced one is the one that ships. How far the price column actually "
    "reaches on the CURRENT artifact is served in `price_coverage`, not asserted "
    "here. The same call composes `finding` and "
    "`verdict`, so a client that highlights recommended_k highlights the row the "
    "sentence is about, by construction. None = no step is priced, and "
    "`finding` / `verdict` are empty."
)


class FrontierInterval(BaseModel):
    """One paired percentile-bootstrap interval on the frontier.

    Mirrors `PairedBootstrapCI`'s contract — `significant` is True ONLY when the
    interval EXCLUDES zero — but is a separate model because the resample unit and
    provenance differ: these come from the sweep artifact, not from the run's
    stored per-BOM deltas.
    """
    n: int
    mean: float
    ci95_low: Optional[float] = None
    ci95_high: Optional[float] = None
    significant: bool = False
    n_boot: int = 0
    seed: int = 0
    method: str = ""


class FrontierPoint(BaseModel):
    """One value of k on the price-of-resilience frontier.

    `n_boms_feasible` is how many BOMs the MILP could solve at this k.
    `n_effective` is how many of those had their PLAN ACTUALLY CHANGE. A BOM the
    constraint does not bind on contributes an exact zero to every delta;
    counting it inflates n and shrinks the interval without adding evidence. Both
    are published because quoting only the larger one is the dishonest option.
    """
    k: int
    n_boms_feasible: int
    n_effective: int
    boms_infeasible: List[str] = []
    # How often the k-supplier plan still contains every k=1 supplier. This single
    # column is the mechanism: where it is low, the plan is more diversified
    # without being nested, and risk need not fall.
    n_keeps_k1_suppliers: int = 0
    mean_total_cost_usd: float
    mean_suppliers: float
    mean_stress_cascade_risk: Optional[float] = None
    mean_stress_expected_shortfall: Optional[float] = None
    mean_targeted_cascade_risk: Optional[float] = None
    mean_targeted_expected_shortfall: Optional[float] = None
    # Cumulative, paired against each BOM's own k = 1 plan.
    delta_cost_usd: Optional[FrontierInterval] = None
    delta_stress_cascade_risk: Optional[FrontierInterval] = None
    delta_stress_expected_shortfall: Optional[FrontierInterval] = None
    delta_targeted_cascade_risk: Optional[FrontierInterval] = None
    delta_targeted_expected_shortfall: Optional[FrontierInterval] = None
    # Cumulative price of risk removed vs k = 1, printed ONLY where the risk
    # change has a paired 95% CI excluding zero AND the change is a REDUCTION.
    # Where the CI covers zero the denominator is indistinguishable from zero and
    # the ratio would be an artifact of division; where it excludes zero on the
    # OTHER side, diversification added risk and there is no price of protection
    # to quote at all. The `_note` says which of the two it is, and `_added`
    # carries the dollars paid per unit of risk ADDED in the second case — the
    # same magnitude, under its true name, instead of a negative number sitting
    # under a heading that says "removed".
    usd_per_unit_targeted_cascade_risk: Optional[float] = None
    usd_per_unit_targeted_cascade_risk_note: Optional[str] = None
    usd_per_unit_targeted_cascade_risk_added: Optional[float] = None
    usd_per_unit_stress_cascade_risk: Optional[float] = None
    usd_per_unit_stress_cascade_risk_note: Optional[str] = None
    usd_per_unit_stress_cascade_risk_added: Optional[float] = None


class FrontierStep(BaseModel):
    """The step from k-1 to k — what the k-th supplier ALONE buys.

    This is the column that says where to stop. `usd_per_unit_*` is None with a
    `_note` wherever the marginal risk removed at that step has an interval
    covering zero.
    """
    label: str                     # "1 → 2"
    from_k: int
    to_k: int
    marginal_cost_usd: Optional[FrontierInterval] = None
    marginal_targeted_cascade_risk_removed: Optional[FrontierInterval] = None
    marginal_stress_cascade_risk_removed: Optional[FrontierInterval] = None
    marginal_targeted_expected_shortfall_removed: Optional[FrontierInterval] = None
    marginal_stress_expected_shortfall_removed: Optional[FrontierInterval] = None
    usd_per_unit_targeted_cascade_risk: Optional[float] = None
    usd_per_unit_targeted_cascade_risk_note: Optional[str] = None
    usd_per_unit_targeted_cascade_risk_added: Optional[float] = None
    usd_per_unit_stress_expected_shortfall: Optional[float] = None
    usd_per_unit_stress_expected_shortfall_note: Optional[str] = None
    usd_per_unit_stress_expected_shortfall_added: Optional[float] = None
    # How much more this step costs per unit of TARGETED cascade risk removed than
    # the first PRICED step did — the collapse, as a multiple. It exists only
    # where a second priced step exists; with one priced step the frontier has no
    # multiple to quote and this is 1.0 on that step and None everywhere else.
    # `price_coverage` on the response says which of the two cases is live.
    cost_multiple_vs_first_step: Optional[float] = None


class FrontierNonMonotoneExample(BaseModel):
    """The single worst counter-example to "more suppliers is safer".

    Read straight off the artifact — the BOM whose broad-stress risk RISES most
    at a consecutive step in k.

    `measure` NAMES WHICH RISK. This used to be expected shortfall and nothing
    else, and the field pair was called `expected_shortfall_before/after`. On the
    corrected supply graph (all 8,176 supplier-part links, not the 80% a dead
    holdout carve left behind) stress expected shortfall falls monotonically in k
    on every included BOM, so that example no longer exists. Non-monotonicity is
    still present in the coarser p50 measure, `cascade_risk` — a different fact
    that must not be printed under the old label. The values are therefore
    generic and the measure travels with them.
    """
    bom: str
    from_k: int
    to_k: int
    measure: str                   # "expected_shortfall" | "cascade_risk"
    measure_label: str             # words for the page, e.g. "cascade risk"
    scenario: str = "stress"
    value_before: float
    value_after: float
    n_suppliers_before: int
    n_suppliers_after: int
    # The artifact records nesting against the k = 1 plan specifically, so this
    # is named for what it actually is rather than "keeps the previous set".
    keeps_k1_suppliers: bool


class DiversificationFrontierResponse(BaseModel):
    """`GET /benchmark/diversification-frontier`.

    Serves `docs/diversification_frontier.json`, reshaped. Every basis string is
    self-documenting: a client that renders this payload and nothing else still
    tells the reader what the numbers mean and where they break down.
    """
    available: bool
    source: str
    unavailable_reason: Optional[str] = None
    generated_utc: Optional[str] = None
    # ── The one sentence ─────────────────────────────────────────────────────
    finding: str = ""
    verdict: str = ""
    # ── Where to stop, as a number the client can read ───────────────────────
    # The k the finding and the verdict are ABOUT. Served because the Benchmark
    # page used to hardcode `k === 2` to highlight the recommended row: nothing
    # on screen was false, but a bare numeral in the client cannot follow the
    # frontier if the frontier moves. `_recommended_k()` is the single place the
    # rule lives — `_frontier_finding()` composes its sentence from the same
    # call, so the highlighted row and the sentence cannot disagree.
    # None means no k is recommended (the first step's interval covers zero),
    # which is exactly when `finding` and `verdict` are empty too.
    recommended_k: Optional[int] = None
    recommended_k_basis: str = _FRONTIER_RECOMMENDED_K_BASIS
    # ── Provenance of the sweep itself ───────────────────────────────────────
    strategy: Optional[str] = None
    mc_scenarios: Optional[int] = None
    mc_seed: Optional[int] = None
    stress_factor: Optional[float] = None
    bootstrap_n: Optional[int] = None
    bootstrap_seed: Optional[int] = None
    n_boms_in_catalog: Optional[int] = None
    n_boms_included: Optional[int] = None
    boms_excluded: Dict[str, str] = {}
    # k = 1 reproduces run 5's blind-MILP landed cost — the sentence that makes
    # this sweep comparable to the published benchmark instead of a parallel
    # universe with its own baseline.
    baseline_check: str = ""
    baseline_check_passed: bool = False
    # ── The frontier ─────────────────────────────────────────────────────────
    aggregate_definition: str = _FRONTIER_AGGREGATE_DEFINITION
    points: List[FrontierPoint] = []
    steps: List[FrontierStep] = []
    # ── How far the price column reaches ─────────────────────────────────────
    # A step carries a price only when its marginal targeted-risk interval
    # excludes zero. `n_priced_steps` counts them and `price_coverage` says, in
    # words derived from those counts, what can and cannot be claimed — because
    # the "cheap second supplier, expensive third" collapse this section used to
    # publish needs TWO priced steps to exist at all, and on the corrected supply
    # graph it has one. A client that renders `price_coverage` publishes the
    # retraction; one that renders only `cost_multiple_vs_first_step` would
    # silently print nothing and leave the old story standing in the reader's
    # head.
    n_steps_total: int = 0
    n_priced_steps: int = 0
    price_coverage: str = ""
    # Suppliers per BOM at the unconstrained optimum — the consolidation this
    # sweep prices the reversal of.
    mean_suppliers_at_k1: Optional[float] = None
    # ── The mechanism ────────────────────────────────────────────────────────
    nesting_caveat: str = _FRONTIER_NESTING_CAVEAT
    non_monotone_example: Optional[FrontierNonMonotoneExample] = None
    # ALWAYS non-empty when the artifact loads: what the scan found, INCLUDING
    # the measure in which it found nothing. An absent counter-example must be
    # reported as an absence, not served as a bare null the page skips over.
    non_monotone_status: str = ""
    # ── Honesty ──────────────────────────────────────────────────────────────
    cost_axis_caveat: str = _FRONTIER_COST_AXIS_CAVEAT
    seed_caveat: str = _FRONTIER_SEED_CAVEAT
    quantisation_caveat: str = _FRONTIER_QUANTISATION_CAVEAT
    independence_caveat: str = _FRONTIER_INDEPENDENCE_CAVEAT
    n_effective_definition: str = ""
    caveats: List[str] = []


_FRONTIER_N_EFFECTIVE_DEFINITION = (
    "n = BOMs whose MILP is feasible at this k. n_effective = BOMs whose SOURCING "
    "PLAN actually changes at this k. Two of the nine BOMs are structurally "
    "identical between k=1 and k=2 — the unconstrained optimum already opened two "
    "or more distributors, so the constraint binds on nothing — and they "
    "contribute hard zeros to every delta. That is why the headline quotes "
    "n_effective = 7, not 9."
)

_NOT_REPORTED_NOTE = (
    "not reported: the risk change here has a paired 95% CI covering zero, so the "
    "denominator is indistinguishable from zero and the ratio would be an artifact "
    "of division, not a price"
)


def _frontier_interval(raw: Any) -> Optional[FrontierInterval]:
    """Reshape one bootstrap block from the artifact. Returns None if absent."""
    if not isinstance(raw, dict) or "mean" not in raw:
        return None
    return FrontierInterval(
        n=int(raw.get("n", 0)),
        mean=float(raw.get("mean", 0.0)),
        ci95_low=raw.get("ci_low"),
        ci95_high=raw.get("ci_high"),
        significant=bool(raw.get("excludes_zero", False)),
        n_boot=int(raw.get("n_boot", 0)),
        seed=int(raw.get("seed", 0)),
        method=str(raw.get("method", "")),
    )


def _frontier_source() -> str:
    """Repo-relative path when it is under the repo, absolute otherwise.

    `Path.relative_to` RAISES on a path outside the root, and an artifact path
    pointed somewhere else (a test tmpdir, a mounted volume) must degrade to a
    readable string rather than a 500.
    """
    try:
        return str(_DIVERSIFICATION_FRONTIER_PATH.relative_to(_REPO_ROOT))
    except ValueError:
        return str(_DIVERSIFICATION_FRONTIER_PATH)


def _frontier_unavailable(reason: str) -> DiversificationFrontierResponse:
    return DiversificationFrontierResponse(
        available=False,
        source=_frontier_source(),
        unavailable_reason=reason,
        n_effective_definition=_FRONTIER_N_EFFECTIVE_DEFINITION,
    )


@lru_cache(maxsize=1)
def _load_diversification_frontier() -> DiversificationFrontierResponse:
    """
    Build the price-of-resilience frontier from `docs/diversification_frontier.json`.

    Cached for the process lifetime, like `_load_volume_curve`: the artifact is a
    committed file that only changes on redeploy, and re-reading it per request
    would buy nothing.
    """
    if not _DIVERSIFICATION_FRONTIER_PATH.exists():
        logger.warning(
            "diversification_frontier.json not found at %s",
            _DIVERSIFICATION_FRONTIER_PATH,
        )
        return _frontier_unavailable(
            "docs/diversification_frontier.json is missing. Regenerate it with "
            "`cd backend && python -m seeds.run_diversification_sweep`. Until then "
            "this API cannot price a second supplier, and no cost-per-unit-of-risk "
            "figure should be quoted."
        )

    try:
        raw: Dict[str, Any] = json.loads(_DIVERSIFICATION_FRONTIER_PATH.read_text())
    except Exception as exc:  # noqa: BLE001 — a bad artifact must not 500 the endpoint
        logger.warning("diversification_frontier.json unreadable: %s", exc)
        return _frontier_unavailable(
            f"docs/diversification_frontier.json could not be parsed: {exc}"
        )

    meta: Dict[str, Any] = raw.get("meta", {}) or {}
    provenance: Dict[str, Any] = raw.get("provenance", {}) or {}
    check: Dict[str, Any] = raw.get("run5_reproduction_check", {}) or {}
    rows: List[Dict[str, Any]] = list(raw.get("frontier", []) or [])

    if not rows:
        return _frontier_unavailable(
            "docs/diversification_frontier.json contains no frontier rows."
        )

    points: List[FrontierPoint] = []
    for row in rows:
        d_cost = _frontier_interval(row.get("delta_cost_vs_k1"))
        d_t_casc = _frontier_interval(row.get("delta_targeted_cascade_risk_vs_k1"))
        d_s_casc = _frontier_interval(row.get("delta_stress_cascade_risk_vs_k1"))
        points.append(FrontierPoint(
            k=int(row.get("k", 0)),
            n_boms_feasible=int(row.get("n_boms_feasible", 0)),
            n_effective=int(row.get("n_effective", 0)),
            boms_infeasible=[str(b) for b in (row.get("boms_infeasible") or [])],
            n_keeps_k1_suppliers=int(row.get("n_keeps_k1_suppliers", 0)),
            mean_total_cost_usd=float(row.get("mean_total_cost_usd", 0.0)),
            mean_suppliers=float(row.get("mean_suppliers", 0.0)),
            mean_stress_cascade_risk=row.get("mean_stress_cascade_risk"),
            mean_stress_expected_shortfall=row.get("mean_stress_expected_shortfall"),
            mean_targeted_cascade_risk=row.get("mean_targeted_cascade_risk"),
            mean_targeted_expected_shortfall=row.get(
                "mean_targeted_expected_shortfall"
            ),
            delta_cost_usd=d_cost,
            delta_stress_cascade_risk=d_s_casc,
            delta_stress_expected_shortfall=_frontier_interval(
                row.get("delta_stress_expected_shortfall_vs_k1")
            ),
            delta_targeted_cascade_risk=d_t_casc,
            delta_targeted_expected_shortfall=_frontier_interval(
                row.get("delta_targeted_expected_shortfall_vs_k1")
            ),
            usd_per_unit_targeted_cascade_risk=row.get(
                "usd_per_unit_targeted_cascade_risk_removed"
            ),
            usd_per_unit_targeted_cascade_risk_note=row.get(
                "usd_per_unit_targeted_cascade_risk_removed_note"
            ),
            usd_per_unit_targeted_cascade_risk_added=row.get(
                "usd_per_unit_targeted_cascade_risk_added"
            ),
            usd_per_unit_stress_cascade_risk=row.get(
                "usd_per_unit_stress_cascade_risk_removed"
            ),
            usd_per_unit_stress_cascade_risk_note=row.get(
                "usd_per_unit_stress_cascade_risk_removed_note"
            ),
            usd_per_unit_stress_cascade_risk_added=row.get(
                "usd_per_unit_stress_cascade_risk_added"
            ),
        ))

    # ── Marginal returns: what the k-th supplier ALONE buys ───────────────────
    steps: List[FrontierStep] = []
    first_step_price: Optional[float] = None
    for row in rows:
        marginal_cost = _frontier_interval(row.get("marginal_cost_usd_vs_prev_k"))
        if marginal_cost is None:
            continue                                  # k = 1 has no previous step
        k = int(row.get("k", 0))
        removed: Dict[str, Any] = row.get("marginal_risk_removed_vs_prev_k", {}) or {}
        ratios: Dict[str, Any] = row.get("marginal_usd_per_unit_risk_removed", {}) or {}
        added: Dict[str, Any] = row.get("marginal_usd_per_unit_risk_added", {}) or {}
        notes: Dict[str, Any] = (
            row.get("marginal_usd_per_unit_risk_removed_note", {}) or {}
        )
        t_casc_price = ratios.get("targeted_cascade_risk")
        s_es_price = ratios.get("stress_expected_shortfall")
        if first_step_price is None and isinstance(t_casc_price, (int, float)):
            first_step_price = float(t_casc_price)
        multiple = (
            round(float(t_casc_price) / first_step_price, 1)
            if isinstance(t_casc_price, (int, float))
            and first_step_price
            and first_step_price > 0
            else None
        )
        steps.append(FrontierStep(
            label=f"{k - 1} → {k}",
            from_k=k - 1,
            to_k=k,
            marginal_cost_usd=marginal_cost,
            marginal_targeted_cascade_risk_removed=_frontier_interval(
                removed.get("targeted_cascade_risk")
            ),
            marginal_stress_cascade_risk_removed=_frontier_interval(
                removed.get("stress_cascade_risk")
            ),
            marginal_targeted_expected_shortfall_removed=_frontier_interval(
                removed.get("targeted_expected_shortfall")
            ),
            marginal_stress_expected_shortfall_removed=_frontier_interval(
                removed.get("stress_expected_shortfall")
            ),
            usd_per_unit_targeted_cascade_risk=t_casc_price,
            # The artifact now distinguishes the TWO reasons a price is withheld
            # (interval covers zero / risk was added), so its own note is
            # preferred; `_NOT_REPORTED_NOTE` remains the fallback for an older
            # artifact that only knows the first reason.
            usd_per_unit_targeted_cascade_risk_note=(
                None if t_casc_price is not None
                else (notes.get("targeted_cascade_risk") or _NOT_REPORTED_NOTE)
            ),
            usd_per_unit_targeted_cascade_risk_added=added.get(
                "targeted_cascade_risk"
            ),
            usd_per_unit_stress_expected_shortfall=s_es_price,
            usd_per_unit_stress_expected_shortfall_note=(
                None if s_es_price is not None
                else (notes.get("stress_expected_shortfall") or _NOT_REPORTED_NOTE)
            ),
            usd_per_unit_stress_expected_shortfall_added=added.get(
                "stress_expected_shortfall"
            ),
            cost_multiple_vs_first_step=multiple,
        ))

    # ── The mechanism, as the worst single counter-example ────────────────────
    # Not asserted from memory: the artifact is scanned for the BOM whose broad-
    # stress risk RISES most at a step in k — and the scan reports which MEASURE
    # it found that in, plus an explicit statement when a measure yields nothing.
    example, non_monotone_status = _non_monotone(raw.get("boms", []) or [])

    # ── How far the price column reaches ──────────────────────────────────────
    price_coverage = _price_coverage(points, steps)

    # ── The one sentence ──────────────────────────────────────────────────────
    finding, verdict = _frontier_finding(points, steps)

    excluded = {
        str(b.get("bom")): str(b.get("reason") or "excluded, reason not recorded")
        for b in (raw.get("boms") or [])
        if isinstance(b, dict) and not b.get("included", True)
    }

    matched = int(check.get("matched", 0))
    checked = int(check.get("checked", 0))
    baseline_check = (
        f"k = 1 reproduces benchmark run {check.get('benchmark_run_id')}'s "
        f"milp_blind landed cost on {matched} of {checked} BOMs to within "
        f"${float(check.get('tolerance_usd', 0.01)):.2f}. The frontier's baseline "
        f"IS the published baseline, which is what makes this sweep comparable to "
        f"the benchmark rather than a parallel study with its own control arm."
        if checked
        else ""
    )

    return DiversificationFrontierResponse(
        available=True,
        source=_frontier_source(),
        generated_utc=provenance.get("generated_at_utc"),
        finding=finding,
        verdict=verdict,
        recommended_k=_recommended_k(steps),
        strategy=meta.get("strategy"),
        mc_scenarios=meta.get("mc_scenarios"),
        mc_seed=meta.get("mc_seed"),
        stress_factor=meta.get("stress_factor"),
        bootstrap_n=meta.get("bootstrap_n"),
        bootstrap_seed=meta.get("bootstrap_seed"),
        n_boms_in_catalog=meta.get("n_boms_in_catalog"),
        n_boms_included=meta.get("n_boms_included"),
        boms_excluded=excluded,
        baseline_check=baseline_check,
        baseline_check_passed=bool(check.get("all_match", False)),
        points=points,
        steps=steps,
        n_steps_total=len(steps),
        n_priced_steps=sum(
            1 for s in steps if s.usd_per_unit_targeted_cascade_risk is not None
        ),
        price_coverage=price_coverage,
        mean_suppliers_at_k1=points[0].mean_suppliers if points else None,
        non_monotone_example=example,
        non_monotone_status=non_monotone_status,
        n_effective_definition=_FRONTIER_N_EFFECTIVE_DEFINITION,
        caveats=[str(c) for c in (raw.get("caveats") or [])],
    )


_MEASURE_LABELS = {
    "expected_shortfall": "expected shortfall",
    "cascade_risk": "cascade risk",
}


def _worst_rise(
    boms: Sequence[Any], measure: str
) -> Tuple[Optional[FrontierNonMonotoneExample], int, int]:
    """
    The largest consecutive-k RISE in broad-stress `measure`, if any.

    Returns `(worst, n_boms_with_a_rise, n_boms_included)`. `worst is None`
    means the measure falls monotonically in k on every included BOM.
    """
    worst: Optional[FrontierNonMonotoneExample] = None
    worst_gap = 0.0
    with_rise: set[str] = set()
    n_included = 0
    for bom in boms:
        if not isinstance(bom, dict) or not bom.get("included", False):
            continue
        n_included += 1
        pts = [p for p in (bom.get("points") or []) if p.get("feasible")]
        for prev, cur in zip(pts, pts[1:], strict=False):
            before = (prev.get("scenarios", {}).get("stress", {}) or {}).get(measure)
            after = (cur.get("scenarios", {}).get("stress", {}) or {}).get(measure)
            if not isinstance(before, (int, float)) or not isinstance(
                after, (int, float)
            ):
                continue
            gap = float(after) - float(before)
            if gap <= 0:
                continue
            with_rise.add(str(bom.get("bom", "?")))
            if gap > worst_gap:
                worst_gap = gap
                worst = FrontierNonMonotoneExample(
                    bom=str(bom.get("bom", "?")),
                    from_k=int(prev.get("k", 0)),
                    to_k=int(cur.get("k", 0)),
                    measure=measure,
                    measure_label=_MEASURE_LABELS.get(measure, measure),
                    scenario="stress",
                    value_before=round(float(before), 4),
                    value_after=round(float(after), 4),
                    n_suppliers_before=int(prev.get("n_distinct_suppliers", 0)),
                    n_suppliers_after=int(cur.get("n_distinct_suppliers", 0)),
                    keeps_k1_suppliers=bool(cur.get("keeps_k1_suppliers", False)),
                )
    return worst, len(with_rise), n_included


def _non_monotone(
    boms: Sequence[Any],
) -> Tuple[Optional[FrontierNonMonotoneExample], str]:
    """
    Does this sweep actually get WORSE under broad stress when forced to spread?

    Scans every consecutive (k-1, k) pair on every included BOM, in TWO measures,
    and returns the worst counter-example together with a sentence that states
    what was found in each — including where nothing was found.

    THE ORDER IS NOT ARBITRARY. expected_shortfall is `1 - mean(fulfillment)` and
    resolves any change; cascade_risk is `1 - p50(fulfillment)` over a 4-line BOM
    and can only move in quarters. The finer measure is therefore checked first,
    and the coarse one is a FALLBACK reported under its own name — never printed
    as if it were the other.

    This section used to name an expected-shortfall counter-example
    unconditionally. On the corrected supply graph (all 8,176 supplier-part
    links, not the 80% a dead holdout carve left behind) that example does not
    exist: the fuller graph is more redundant and stress expected shortfall falls
    at every step on every BOM. Returning a bare None there would have deleted
    the claim silently, so the absence is published as a sentence.
    """
    es, es_boms, n_included = _worst_rise(boms, "expected_shortfall")
    cr, cr_boms, _ = _worst_rise(boms, "cascade_risk")

    def _how(ex: FrontierNonMonotoneExample) -> str:
        return (
            "it keeps its whole k=1 supplier set and is still more exposed"
            if ex.keeps_k1_suppliers
            else f"it drops a lower-hazard incumbent for a cheaper set of "
                 f"{ex.n_suppliers_after}"
        )

    if es is not None:
        return es, (
            f"NOT MONOTONE in the finer measure: broad-stress expected shortfall "
            f"RISES at some step in k on {es_boms} of the {n_included} included "
            f"BOMs. Worst case {es.bom}, {es.value_before:.4f} to "
            f"{es.value_after:.4f} between k={es.from_k} and k={es.to_k} — "
            f"{_how(es)}."
        )
    if cr is not None:
        return cr, (
            f"RETRACTED IN ONE MEASURE, STILL TRUE IN THE OTHER. Broad-stress "
            f"expected shortfall FALLS at every step on all {n_included} included "
            f"BOMs on this sweep, so the expected-shortfall counter-example this "
            f"section used to name no longer exists and is withdrawn. The "
            f"non-monotonicity survives in the coarser p50 measure: broad-stress "
            f"cascade risk rises on {cr_boms} of the {n_included} BOMs. Worst "
            f"case {cr.bom}, {cr.value_before:.4f} to {cr.value_after:.4f} "
            f"between k={cr.from_k} and k={cr.to_k} — {_how(cr)}."
        )
    return None, (
        f"RETRACTED. Neither broad-stress expected shortfall nor broad-stress "
        f"cascade risk rises at any step in k on any of the {n_included} included "
        f"BOMs, so this sweep shows NO counter-example to 'more suppliers is "
        f"safer' and none is claimed. Non-monotonicity remains something the "
        f"constraint PERMITS — see the nesting caveat — not something measured "
        f"here."
    )


def _price_coverage(
    points: Sequence[FrontierPoint], steps: Sequence[FrontierStep]
) -> str:
    """
    How far the price column reaches, in words composed from the counts.

    The section's original claim was a COLLAPSE — "the second supplier is cheap
    per unit of risk and the third is not" — and a collapse needs two priced
    steps to be a claim at all. Whether there are two is a property of the
    artifact, so the sentence is built from it rather than written down once.
    """
    priced = [s for s in steps if s.usd_per_unit_targeted_cascade_risk is not None]
    n = len(steps)
    if not steps:
        return ""
    if not priced:
        return (
            f"NO STEP on this frontier carries a price. All {n} steps have a "
            "marginal targeted-cascade-risk interval covering zero, so no "
            "dollars-per-unit-of-risk figure is quotable and none is published."
        )
    if len(priced) == 1:
        only = priced[0]
        k1 = next((p for p in points if p.k == 1), None)
        last = points[-1] if points else None
        after = next((p for p in points if p.k == only.to_k), None)
        trail = ""
        if k1 is not None and after is not None and last is not None:
            trail = (
                f" The reason is not that later suppliers are expensive — it is "
                f"that there is nothing measurable left for them to remove: mean "
                f"targeted cascade risk goes from "
                f"{k1.mean_targeted_cascade_risk:.3f} at k=1 to "
                f"{after.mean_targeted_cascade_risk:.3f} at k={after.k}, and "
                f"{last.mean_targeted_cascade_risk:.3f} at k={last.k}."
            )
        return (
            f"ONLY 1 OF {n} STEPS CARRIES A PRICE, so there is no "
            f"cheap-then-expensive collapse on this frontier and no multiple is "
            f"quoted. The step to k={only.to_k} removes targeted cascade risk at "
            f"${only.usd_per_unit_targeted_cascade_risk:,.2f} per unit; every "
            f"later step's marginal targeted-risk interval covers zero.{trail}"
        )
    first, second = priced[0], priced[1]
    return (
        f"{len(priced)} OF {n} STEPS CARRY A PRICE, and they collapse. The step "
        f"to k={first.to_k} removes targeted cascade risk at "
        f"${first.usd_per_unit_targeted_cascade_risk:,.2f} per unit; the step to "
        f"k={second.to_k} costs "
        f"${second.usd_per_unit_targeted_cascade_risk:,.2f} per unit, "
        f"{second.cost_multiple_vs_first_step}× more for the same unit of risk. "
        f"Past the priced steps no price is quotable at all."
    )


_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
    6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
}


def _ordinal(k: int) -> str:
    """"second" / "third" / … for the k-th supplier; falls back to "k = N"."""
    return _ORDINALS.get(k, f"k = {k}")


def _recommended_k(steps: Sequence[FrontierStep]) -> Optional[int]:
    """
    THE ONE PLACE the "where to stop" rule lives.

    The recommendation is the step that buys targeted cascade risk MOST CHEAPLY:
    the priced step with the lowest `usd_per_unit_targeted_cascade_risk`, ties
    broken toward the smaller k. "Priced" already means the step's risk interval
    excluded zero — `_load_diversification_frontier` refuses to divide by a
    denominator that covers zero — so an unmeasurable step can never be the
    recommendation, and a step that removes risk at multiples of the best price
    is not one either.

    WHY NOT "the largest k whose interval still excludes zero". Because that is a
    different rule with a different answer, and it is the wrong one. It has
    already differed on this frontier: before the supply graph was corrected to
    use all 8,176 supplier-part links, 1→2 removed 0.44 of risk at $132/unit and
    2→3 removed a further 0.11 at $903/unit — BOTH intervals excluded zero, so a
    "largest significant k" rule returned 3 and flipped the published verdict to
    its opposite on a frontier that had not moved. Significance says the third
    supplier does something; it does not say the something is worth 6.8× the
    price. The diminishing-returns question is a PRICE question, so the rule is
    priced. (On the corrected graph only one step is priced at all — see
    `_price_coverage` — which is why the count is served rather than described.)

    Returns None when no step is priced at all — exactly the case in which
    `_frontier_finding()` returns ("", ""), because there is no finding to state.
    Both the served `recommended_k` field and the headline sentence call this, so
    the row the page highlights and the row the sentence describes cannot drift.
    """
    priced = [
        s for s in steps
        if s.usd_per_unit_targeted_cascade_risk is not None
        and s.marginal_cost_usd is not None
        and s.marginal_targeted_cascade_risk_removed is not None
        and s.marginal_targeted_cascade_risk_removed.significant
    ]
    if not priced:
        return None
    best = min(
        priced,
        key=lambda s: (float(s.usd_per_unit_targeted_cascade_risk or 0.0), s.to_k),
    )
    return best.to_k


def _frontier_finding(
    points: Sequence[FrontierPoint], steps: Sequence[FrontierStep]
) -> Tuple[str, str]:
    """
    Compose the headline and the verdict FROM THE DATA, never from memory.

    Returns ("", "") rather than a half-sentence if the first step is missing or
    its risk interval covers zero — there is no finding to state in that case.
    The step it describes is `_recommended_k()`'s, not a hardcoded k = 2.
    """
    rec_k = _recommended_k(steps)
    if rec_k is None:
        return "", ""
    first = next((s for s in steps if s.to_k == rec_k), None)
    if first is None:  # pragma: no cover - _recommended_k only returns a real step
        return "", ""
    cost = first.marginal_cost_usd
    risk = first.marginal_targeted_cascade_risk_removed
    if cost is None or risk is None or not risk.significant:  # pragma: no cover
        return "", ""
    k2 = next((p for p in points if p.k == rec_k), None)
    n_eff = k2.n_effective if k2 else risk.n
    finding = (
        f"The {_ordinal(rec_k)} supplier removes {risk.mean:.2f} of targeted cascade "
        f"risk for ${cost.mean:,.2f} per BOM (95% CI {risk.ci95_low:.2f} to "
        f"{risk.ci95_high:.2f}, n={risk.n} BOMs, n_effective={n_eff})."
    )
    nxt = next((s for s in steps if s.to_k == rec_k + 1), None)
    if nxt is not None and nxt.cost_multiple_vs_first_step:
        finding += (
            f" The {_ordinal(rec_k + 1)} costs {nxt.cost_multiple_vs_first_step:g}× "
            f"more per unit of risk removed, and past it the interval covers zero."
        )
    elif sum(1 for s in steps if s.usd_per_unit_targeted_cascade_risk is not None) == 1:
        # The sentence used to END on "the third costs 6.8× more". With one
        # priced step there is no multiple, and silence would leave the reader
        # with the old story — so the finding states the absence itself.
        finding += (
            " It is the ONLY step on this frontier that carries a price: every "
            "later step's targeted-risk interval covers zero, so no "
            "cheap-then-expensive collapse is claimed."
        )
    verdict = (
        f"Buy the {_ordinal(rec_k)} supplier. "
        f"Do not buy the {_ordinal(rec_k + 1)}."
    )
    return finding, verdict


@router.get(
    "/diversification-frontier",
    response_model=DiversificationFrontierResponse,
)
def get_diversification_frontier() -> DiversificationFrontierResponse:
    """
    The price of resilience: cost and cascade risk against a hard minimum
    supplier count k, with paired bootstrap CIs.

    Serves the checked-in sweep artifact. Nothing is solved or simulated here and
    no database is touched — if the artifact is missing the response is
    `available: false` with a regeneration command, never a bare headline.
    """
    return _load_diversification_frontier()
