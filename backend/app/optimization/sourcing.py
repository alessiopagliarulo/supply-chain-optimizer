"""
Stage 1 — Component sourcing integer program.

Outlier filter + CP-SAT MILP. See spec §3.2 and §5.4.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.optimization.constants import (
    LTL_BASE_FEE_USD as LTL_BASE,
    LTL_RATE_USD_PER_CWT_MILE as LTL_RATE,
    KM_PER_MILE,
    LBS_PER_KG,
    CWT_PER_LB,
    AIR_FREIGHT_BASE_USD,
    AIR_FREIGHT_RATE_USD_PER_KG,
)
from app.optimization.strategies import StrategyWeights

logger = logging.getLogger(__name__)


# ── Data containers ──────────────────────────────────────────────────────────

@dataclass
class BomLine:
    component_id: int
    mpn: str
    quantity: int
    # ── Part attributes the lead-time model may consume ──────────────────────
    # Verbatim from the Component row. All optional so every existing positional
    # construction keeps working; when present they let solve.py ask the model
    # about THIS part instead of assuming "Microcontrollers" for the whole BOM
    # (which is what it used to do). Which of them the model actually uses is
    # resolved at fit time — see app/ml/lead_time_model.resolve_schema_from_records.
    category: Optional[str] = None            # Nexar taxonomy
    dk_category: Optional[str] = None         # DigiKey taxonomy (canonical for lead time)
    dk_subcategory: Optional[str] = None
    manufacturer: Optional[str] = None
    lifecycle_status: Optional[str] = None
    is_normally_stocked: Optional[bool] = None
    # DigiKey catalog attributes persisted by migration 0007. Part-level, because
    # a factory lead time is a property of the part, not of the distributor.
    parameter_count: Optional[int] = None
    package_case: Optional[str] = None
    htsus_code: Optional[str] = None
    rohs_status: Optional[str] = None
    digikey_unit_price: Optional[float] = None
    max_break_qty: Optional[int] = None
    price_break_count: Optional[int] = None


@dataclass
class Offer:
    component_id: int
    distributor_id: int
    distributor_name: str
    price_usd: float
    stock: int
    moq: int
    is_domestic: bool
    dist_km_from_depot: float = 0.0  # precomputed haversine; used for transport penalty
    risk_score: float = 0.5           # component risk (0-1, from Nexar)
    is_chinese_origin: bool = False   # True if manufacturer_country is China
    distributor_country: str = "USA"  # ISO-3166-1 ALPHA-3 (ACLED keys on iso3; "US" can never match)
    # ── Offer attributes the lead-time model may consume (see BomLine) ───────
    packaging: Optional[str] = None
    standard_pack: Optional[int] = None


@dataclass
class OutlierDrop:
    component_id: int
    mpn: str
    dropped_distributor_id: int
    dropped_price_usd: float
    median_price_usd: float
    reason: str


@dataclass
class SourcingAssignment:
    component_id: int
    mpn: str
    distributor_id: int
    distributor_name: str
    quantity: int
    unit_price_usd: float

    @property
    def line_total(self) -> float:
        return self.quantity * self.unit_price_usd


@dataclass
class SourcingResult:
    assignments: List[SourcingAssignment]
    total_component_cost: float
    selected_distributor_ids: List[int]
    outlier_drops: List[OutlierDrop] = field(default_factory=list)
    status: str = "OPTIMAL"
    # The solver's own objective value, converted back to USD (objective units /
    # OBJ_SCALE). None for the greedy baselines, which have no solver objective.
    # greedy.landed_cost_breakdown() must reproduce this to within integer
    # rounding — that agreement is the benchmark's anti-rigging invariant.
    objective_usd: Optional[float] = None


# ── Outlier filter ───────────────────────────────────────────────────────────

OUTLIER_MEDIAN_MULTIPLE = 5.0  # Aberdeen Group 2020


def filter_price_outliers(
    offers: List[Offer],
    bom: List[BomLine],
    k: float = OUTLIER_MEDIAN_MULTIPLE,
) -> Tuple[List[Offer], List[OutlierDrop]]:
    """
    Drop offers where price > k * median(price) for that component.

    One-sided — low prices (real discounts) are kept. See spec §5.4.
    """
    mpn_by_id = {b.component_id: b.mpn for b in bom}
    by_component: Dict[int, List[Offer]] = {}
    for o in offers:
        by_component.setdefault(o.component_id, []).append(o)

    kept: List[Offer] = []
    drops: List[OutlierDrop] = []

    for cid, group in by_component.items():
        prices = [o.price_usd for o in group if o.price_usd > 0]
        if not prices:
            logger.warning(
                "component_id=%s has no offers with price > 0; skipping outlier filter and keeping all offers",
                cid,
            )
            kept.extend(group)
            continue
        median = statistics.median(prices)
        cutoff = k * median
        for o in group:
            if o.price_usd > cutoff:
                drops.append(OutlierDrop(
                    component_id=cid,
                    mpn=mpn_by_id.get(cid, f"component_{cid}"),
                    dropped_distributor_id=o.distributor_id,
                    dropped_price_usd=o.price_usd,
                    median_price_usd=median,
                    reason=f"price {o.price_usd:.2f} > {k}×median {median:.2f}",
                ))
                logger.info("outlier dropped: cid=%s did=%s price=%.2f median=%.2f",
                            cid, o.distributor_id, o.price_usd, median)
            else:
                kept.append(o)
    return kept, drops


# ── CP-SAT sourcing MILP ─────────────────────────────────────────────────────

# Scale factor: CP-SAT wants integer coefficients. Kept because it is the unit
# a "cents" figure is quoted in elsewhere in the codebase — the OBJECTIVE no
# longer uses it. Nothing in the MILP should convert USD with this constant;
# use `to_obj_units` (see below).
PRICE_SCALE = 100

# The objective is built in integer MILLI-CENTS (PRICE_SCALE x OBJ_SUBSCALE).
# Every term is multiplied by the same constant, so the argmin is identical to a
# cents-denominated objective — but the per-unit freight rate is a genuinely
# small number (~$0.029/unit at 100 km, ~$0.003/unit at 10 km). In whole cents it
# would round to ZERO for nearby distributors and the variable freight term would
# silently vanish. Milli-cent resolution keeps it.
OBJ_SUBSCALE = 1000
OBJ_SCALE = PRICE_SCALE * OBJ_SUBSCALE  # objective units per USD

# Objective-coefficient safety ceiling, mirroring the one `stochastic.py` holds
# its own model to (`stochastic.MAX_OBJ_COEFF`). Defined here rather than
# imported because stochastic.py imports FROM this module. Well below int64 max
# (9.22e18) so CP-SAT's internal arithmetic has headroom.
MAX_OBJ_COEFF = 4 * 10**17


def to_obj_units(usd: float) -> int:
    """
    USD -> integer objective units (milli-cents). The ONE conversion every term
    in the sourcing MILP objective goes through.

    WHY THIS EXISTS (fixed 2026-08-28). The objective always carried milli-cent
    resolution and relied on it for the per-unit freight term, but the PRICE
    term was converted in two steps at two different resolutions:
    `int(round(price_usd * PRICE_SCALE)) * OBJ_SUBSCALE` — rounded to whole
    cents FIRST, then padded with three zeros. Those three digits were gone
    before CP-SAT ever saw them. On this catalogue that meant:

      * MLG0603P43NHT000 at $0.0031/unit entered the objective at exactly 0 —
        the solver could take any quantity of it for free;
      * 15 components have sub-$0.10 offers, where whole-cent rounding is a
        quantisation error of up to ~6% of unit price;
      * the greedy baselines (`greedy.landed_cost_breakdown`) score the SAME
        offers on full floats, so the two benchmark arms were not optimising
        at the same price resolution — which is precisely the comparison the
        benchmark exists to make.

    At OBJ_SCALE the residual quantisation is at most $5e-6 per unit (half a
    milli-cent), i.e. below the cent at which every published figure is
    reported. The lowest price in the catalogue, $0.0031, maps to 310 units.
    """
    return int(round(usd * OBJ_SCALE))

# Average shipped mass of one electronic component unit. Used to turn "units
# shipped" into freight-chargeable weight.
AVG_KG_PER_UNIT = 0.05


# Ceiling on the stock-out risk surcharge, as a fraction of unit price. This is
# a CHOSEN CEILING, not a fitted or calibrated quantity — nothing in this repo
# estimates it from data, and the word "calibrated" is reserved here for
# quantities that were fitted (the regime model's P(stress), the lead-time
# model's coefficients, `stochastic.build_failure_probabilities`' hazard rate).
# It is picked so that at maximum stress AND maximum vulnerability the surcharge
# is 15% of unit price: enough to break a tie between comparable offers, never
# enough to overturn a large genuine price difference. Treat it as a policy
# knob with a stated bound, not as an estimate of anything.
RISK_PREMIUM_RATE = 0.15

# Weights of the stock-out vulnerability index. They sum to 1.0 by construction
# and each multiplies a term already in [0, 1], so `vulnerability` is in [0, 1]
# and RISK_PREMIUM_RATE keeps its stated meaning as the surcharge at the maximum.
# 3:2 origin:stock is the ratio the original formula's stated weights implied
# (0.3 / 0.2); see _stockout_risk_premium_obj_units for why the third term went.
VULN_W_CHINESE_ORIGIN = 0.6
VULN_W_STOCK_COVERAGE = 0.4

# Stock coverage (stock / MOQ) at or above which a line is treated as fully
# covered and contributes nothing to vulnerability.
VULN_STOCK_COVERAGE_CAP = 50.0


def _stockout_risk_premium_obj_units(
    offer: "Offer",
    bom_line: "BomLine",
    macro_stress: float,
) -> int:
    """
    Stock-out risk surcharge, in objective units (milli-cents), added to the
    MILP's effective price for selecting this offer.

    Formula:
        vulnerability = 0.6×is_chinese_origin
                      + 0.4×(1 - min(stock/moq, 50)/50)
        stockout_risk = macro_stress × vulnerability          # both in [0, 1]
        surcharge     = unit_price × stockout_risk × RISK_PREMIUM_RATE

    WHY risk_score IS NOT IN THIS FORMULA (changed 2026-08-28).

    The previous formula was
        0.3×is_chinese_origin + 0.2×(stock term) + 0.5×risk_score
    and it counted one binary attribute twice. `Component.risk_score` is a
    verbatim passthrough of a third-party HuggingFace column
    (`seeds/seed_db.py:248`); on this catalogue it takes six values and is an
    additive hand-weighted flag sum:

        risk_score = 0.60·chinese_origin + 0.25·critical_category
                                         + 0.10·limited_suppliers

    verified against the shipped DB: risk_score ∈ {0.60, 0.70} is EXACTLY the
    14 rows with manufacturer_country == "China", i.e. exactly the rows where
    `is_chinese_origin` (sourcing.py's own predicate, derived from the same
    `risk_factors` list) fires. So the old expression expanded to

        0.60·is_chinese + 0.20·stock + 0.125·critical + 0.05·limited

    — 60% of maximum vulnerability from one flag, 0.30 of it arriving twice.

    The remaining content of risk_score could not be salvaged without lying
    about its resolution: 387 of 791 components (48.9%) carry a flat 0.20 with
    an EMPTY risk_factors list, and 0.20 is not a sum of any subset of those
    weights. It is a placeholder, not a measurement, and under the old formula
    it silently added 0.10 of vulnerability to half the catalogue on no
    evidence. The two attributes that survive here — manufacturer origin and
    stock-to-MOQ coverage — are both checkable facts on the offer.

    The two flags that are lost with it (`critical_category`, 170 parts;
    `limited_suppliers`, 31 parts) are not exposed on `Offer` and cannot be
    read here without plumbing new fields through every producer
    (api/optimize.py, api/stochastic.py, seeds/run_benchmark.py). Adding them
    back as their own named terms would be a strict improvement over reading
    them out of an opaque index, and is left as separate work.

    NET EFFECT on the surcharge: unchanged for a Chinese-origin offer with full
    stock coverage (0.60 either way); removes the flat 0.10 that a flagless,
    fully-stocked domestic offer used to pay; and raises the weight on genuinely
    thin stock from 0.20 to 0.40.
    """
    is_chinese = getattr(offer, "is_chinese_origin", False)
    stock = offer.stock or 0
    moq = offer.moq or 1
    stock_coverage = min(stock / max(moq, 1), VULN_STOCK_COVERAGE_CAP)

    vulnerability = (
        VULN_W_CHINESE_ORIGIN * int(bool(is_chinese))
        + VULN_W_STOCK_COVERAGE * (1.0 - stock_coverage / VULN_STOCK_COVERAGE_CAP)
    )
    stockout_risk = macro_stress * vulnerability
    surcharge_usd = offer.price_usd * stockout_risk * RISK_PREMIUM_RATE
    return to_obj_units(surcharge_usd)


# Snyder & Daskin (2005), "Reliable Location Models" — a disrupted
# single-sourced component has no cheaper fallback offer to price a delta
# against, so its expected recourse cost is approximated as a large-but-
# finite multiple of unit price (stand-in for expediting/respin cost).
STOCKOUT_PENALTY_MULTIPLE = 3.0

# Emergency-reprocurement premium: even when a substitute exists, recovering a
# disrupted line means expediting the replacement units at a premium. Mirrors the
# Monte Carlo model's EMERGENCY_COST_PREMIUM (0.15). Defined locally so the
# optimization layer stays independent of the graph/simulation layer.
EMERGENCY_REPROCURE_PREMIUM = 0.15


def _graph_surcharge_obj_units(
    offer: "Offer",
    betweenness_score: float,
    component_offers: List["Offer"],
) -> int:
    """
    Graph-concentration surcharge in objective units (milli-cents), shaped like
    a Snyder & Daskin (2005)
    reliable-facility-location expected-loss term (weight x recourse loss) but
    NOT a calibrated expected-disruption-loss: `betweenness_score` is used
    directly as a risk WEIGHT here, not as a probability.

    HONEST CAVEAT. `betweenness_score` is the raw (algorithm-normalized,
    NOT min-max rescaled) bipartite betweenness centrality from
    `graph/builder.py` — a structural concentration proxy, not a fitted
    disruption probability. For the median distributor in this catalogue that
    raw value is roughly two orders of magnitude below the ~4.4%
    (`DEFAULT_BASE_ANNUAL_PROB` over a 60-day horizon) calibrated hazard rate
    that `app/optimization/stochastic.py::build_failure_probabilities` derives
    from a cited base rate plus a bounded rank transform of the same
    centrality. Read `stochastic.py`'s module-level comment for the full
    argument against using raw betweenness as a probability — the same
    argument applies here. This function does not use that calibration; it
    multiplies the raw centrality score directly into the recourse cost as a
    weighting factor, so the resulting surcharge is a directional risk
    WEIGHT (higher centrality -> higher surcharge) rather than a properly
    scaled expected loss. `seeds/run_benchmark.py`'s `graph_aware=True` /
    "milp_graph" arm calls this function, so any published cost delta
    attributed to that arm reflects this uncalibrated weight, not the
    calibrated hazard used in the Monte Carlo / CVaR path.

    UNITS (changed 2026-08-28). This used to compute in whole cents and be
    multiplied up by OBJ_SUBSCALE at the call site, which meant the surcharge on
    any offer whose betweenness-weighted recourse cost was under half a cent
    rounded to exactly zero — on cheap parts, the entire graph-aware signal
    vanished while the price term still counted. It now computes at the
    objective's own milli-cent resolution throughout (`to_obj_units`), so the
    "milp_graph" arm is not blind on the cheap end of the catalogue.

    recourse_cost_units = the expected per-unit loss if this source is disrupted:
      - the price gap to switch to the next-cheapest alternative offer, PLUS
      - an emergency-reprocurement premium on the unit (EMERGENCY_REPROCURE_PREMIUM)
        for expediting the replacement — incurred even when a cheap substitute
        exists, because recovery is never free.
      If no alternative offer exists (single-source component), the recourse cost
      is a large-but-finite STOCKOUT_PENALTY_MULTIPLE x unit price (expedite/respin
      stand-in), which dominates the substitutable case as it should.

    surcharge_units = round(p_d * recourse_cost_units)

    Near-zero for low-centrality suppliers, and materially larger for
    high-centrality ones — so graph-aware sourcing is biased away from
    concentrated hubs toward lower-centrality alternatives (diversification),
    while a low-centrality plan pays essentially nothing. This is the true
    "insurance" shape, with no arbitrary flat-rate cap.
    """
    unit_price_units = to_obj_units(offer.price_usd)

    alt_prices_units = [
        to_obj_units(o.price_usd)
        for o in component_offers
        if o.distributor_id != offer.distributor_id
    ]
    if alt_prices_units:
        next_cheapest_units = min(alt_prices_units)
        switch_gap_units = max(0, next_cheapest_units - unit_price_units)
        expedite_units = int(round(EMERGENCY_REPROCURE_PREMIUM * unit_price_units))
        recourse_cost_units = switch_gap_units + expedite_units
    else:
        recourse_cost_units = int(round(STOCKOUT_PENALTY_MULTIPLE * unit_price_units))

    return int(round(betweenness_score * recourse_cost_units))


def _feed_risk_obj_units(
    offer: "Offer",
    distributor_country: str,
    is_chinese_origin: bool,
    cache: "object | None",
) -> int:
    """
    Feed-driven risk surcharge in objective units (milli-cents). Per D-01 from
    CONTEXT.md.

    GPR: Chinese-origin component risk scaled by geopolitical tension.
    ACLED: distributor-country risk scaled by 90-day conflict count.

    Ceiling: 15% of unit price (matching graph surcharge ceiling).
    Returns 0 when cache is None or feed data unavailable.

    UNITS (changed 2026-08-28): computed in whole cents before, then scaled up
    by OBJ_SUBSCALE at the call site — so on any offer under ~$0.07 the whole
    live-feed signal floored to zero while the price term still counted. Now
    computed at the objective's own resolution via `to_obj_units`.
    """
    import math
    if cache is None:
        return 0

    unit_price_units = to_obj_units(offer.price_usd)
    ceiling = int(math.floor(0.15 * unit_price_units))

    gpr_surcharge = 0
    acled_surcharge = 0

    # GPR: Chinese-origin risk
    #
    # KNOWN AND DELIBERATELY UNCHANGED (2026-09-07): the value read here is the
    # newest row of the published GPR file, and that row is dated **September
    # 2021**. `/feeds/status` now says so — it reports the feed `stale` with the
    # observation date rather than `live` — but this surcharge is computed from
    # the value regardless of its age, so a 2021 reading still enters the CP-SAT
    # objective on every solve.
    #
    # Gating it on `cache.gpr.observed_at` would be a one-line change and it is
    # NOT made here on purpose: it would silently alter every published
    # optimization figure and the committed benchmark artifacts they are diffed
    # against, which is a re-benchmark, not a bug fix. Disclosed instead, and
    # left as an explicit decision for whoever does that re-run. What must not
    # happen in the meantime is the app describing this input as current — that
    # part is fixed.
    if is_chinese_origin and getattr(cache, 'gpr', None) is not None and cache.gpr.data is not None:
        gpr_value = float(cache.gpr.data)  # typically 50-500
        gpr_normalized = max(0.0, min((gpr_value - 100) / 400, 1.0))
        gpr_surcharge = int(math.floor(gpr_normalized * 0.15 * unit_price_units))

    # ACLED: distributor country conflict risk
    if getattr(cache, 'acled', None) is not None and cache.acled.data is not None:
        country_counts = cache.acled.data
        # distributor_country might be "US", "CN", etc. — use as-is for lookup
        conflict_count = country_counts.get(distributor_country, 0)
        acled_normalized = min(conflict_count / 500, 1.0)
        acled_surcharge = int(math.floor(acled_normalized * 0.15 * unit_price_units))

    total = gpr_surcharge + acled_surcharge
    return min(total, ceiling)


@dataclass(frozen=True)
class FreightModel:
    """
    Freight decomposed into the two parts a fixed-charge network model needs.

    fixed_by_did[d]     USD charged ONCE for opening distributor d — the LTL base
                        fee (domestic) or the air consignment minimum
                        (international). Does NOT depend on how much d ships.
                        This is the genuine fixed charge whose trade-off against
                        component price is the entire reason the MILP exists.

    per_unit_by_did[d]  USD per UNIT actually shipped from d — weight x distance
                        for domestic LTL, weight x $/kg for air. Scales with the
                        quantity d really ships, so splitting a BOM across N
                        suppliers ALLOCATES one BOM's variable freight among them
                        instead of charging a full BOM's freight N times over.

    Both components are already multiplied by penalty_scale
    (StrategyWeights.transport_penalty_scale), so they can be dropped straight
    into a cost objective (MILP or greedy baseline) with no further scaling:
      cheapest  = 1.0  → full transport cost in objective (landed cost)
      fastest   = 1.0  → full transport cost; us_only + consolidation drive speed
      greenest  = 2.5  → strong proximity preference to cut tonne-miles CO2
      balanced  = 1.5  → moderate distance penalty

    Total freight paid to distributor d:
        fixed_by_did[d] * opened(d) + per_unit_by_did[d] * units_shipped_from(d)

    which is linear in the CP-SAT decision variables (y[d] and sum_c q[c,d]), so
    the MILP models it exactly rather than approximating it.
    """
    fixed_by_did: Dict[int, float]
    per_unit_by_did: Dict[int, float]


def _freight_model_by_did(
    offers: List["Offer"],
    penalty_scale: float,
) -> FreightModel:
    """
    Build the per-distributor freight model (see FreightModel).

    Domestic: LTL tariff (FreightWaves SONAR Q4 2023 / Old Dominion) —
      fixed    = LTL_BASE_FEE_USD
      per unit = AVG_KG_PER_UNIT x LBS_PER_KG x CWT_PER_LB x miles x LTL_RATE
    International: IATA 2023 airfreight (flat consignment base + $/kg) —
      fixed    = AIR_FREIGHT_BASE_USD
      per unit = AVG_KG_PER_UNIT x AIR_FREIGHT_RATE_USD_PER_KG
    (LTL_RATE_USD_PER_CWT_MILE is domestic trucking only and produces absurd
    values over 6,000+ km international distances, hence the split.)

    This replaces an earlier model that computed ONE representative shipment
    weight for the whole BOM and then charged EVERY opened distributor that full
    weight. That replicated variable freight per supplier instead of allocating
    it, systematically over-penalising split orders and inflating the MILP's
    apparent consolidation advantage.
    """
    all_distributors = {o.distributor_id for o in offers}
    dist_km_by_did = {o.distributor_id: o.dist_km_from_depot for o in offers}
    is_domestic_by_did = {o.distributor_id: o.is_domestic for o in offers}

    fixed_by_did: Dict[int, float] = {}
    per_unit_by_did: Dict[int, float] = {}
    for did in all_distributors:
        km = dist_km_by_did.get(did, 0.0)
        if is_domestic_by_did.get(did, True):
            miles = km / KM_PER_MILE
            fixed = LTL_BASE
            per_unit = AVG_KG_PER_UNIT * LBS_PER_KG * CWT_PER_LB * miles * LTL_RATE
        else:
            fixed = AIR_FREIGHT_BASE_USD
            per_unit = AVG_KG_PER_UNIT * AIR_FREIGHT_RATE_USD_PER_KG
        fixed_by_did[did] = fixed * penalty_scale
        per_unit_by_did[did] = per_unit * penalty_scale

    return FreightModel(fixed_by_did=fixed_by_did, per_unit_by_did=per_unit_by_did)


def solve_sourcing(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    us_only: bool = True,
    graph_aware: bool = False,
    require_dual_source: bool = False,
    min_distributors: Optional[int] = None,
) -> SourcingResult:
    """
    Pick which distributor fills each BOM line (and how much) to minimize
    cost, subject to demand/stock/MOQ/domestic constraints.

    WHAT THE STAGE 1 OBJECTIVE ACTUALLY MINIMIZES. It is a landed-cost model,
    not a component-price model — an earlier revision of this docstring said
    "only component cost", which stopped being true once freight was decomposed
    into a fixed-charge model. Every term below is built in integer milli-cents
    through `to_obj_units`, and the sum is what `model.Minimize` receives:

      component cost      price[c,d]      x q[c,d]     per unit
      fixed freight       fixed[d]        x y[d]       per opened distributor
      variable freight    per_unit[d]     x q[c,d]     per unit
      consolidation       bonus_usd       x y[d]       per opened distributor
      stock-out risk      premium[c,d]    x q[c,d]     per unit
      graph concentration surcharge[c,d]  x q[c,d]     per unit, graph_aware only
      live-feed risk      surcharge[c,d]  x q[c,d]     per unit

    The two freight terms come from `_freight_model_by_did` (LTL tariff domestic,
    IATA airfreight international) and both arrive already multiplied by
    ``StrategyWeights.transport_penalty_scale``. The consolidation term is a
    positive charge per distributor opened — it is called a "bonus" because it
    rewards consolidation, but it enters the minimization as a cost. The
    stock-out premium is P(macro stress) x vulnerability x RISK_PREMIUM_RATE of
    unit price (`_stockout_risk_premium_obj_units`); the graph term is present
    only when ``graph_aware=True`` AND a GraphState is loaded; the feed term is
    zero when no LiveDataCache is loaded. So the objective is genuinely
    per-strategy: two StrategyWeights knobs move three of its coefficients (both
    freight terms and the consolidation charge), and one whole term switches on a
    caller flag.

    WHERE TIME AND CARBON ENTER — and it is NOT here. Neither lead time nor
    CO2e appears in the objective above, because both are properties of the
    ROUTE, and the route does not exist until Stage 1 has chosen which
    distributors to visit. They are computed in Stage 2 (`routing.py`'s TSP over
    the selected distributors, plus the cross-dock evaluation) and composed with
    the Stage 1 result by the orchestrator, `solve.py`: it min-max normalizes
    cost/time/carbon ACROSS the four alternatives (`strategies.normalize_objectives`)
    and reports the weighted sum with ``w_cost``/``w_time``/``w_carbon`` as each
    alternative's ``StrategyMath.weighted_total``. Note what that scalarization
    does and does not do — it SCORES the four plans for display; it does not pick
    among them (all four are returned, and ``recommended_id`` is fixed to
    "balanced"). No weight in `StrategyWeights.as_tuple` reaches this MILP.

    That leaves a time- or carbon-preferring strategy exactly two PROXY levers
    over Stage 1, both of them cost-denominated and both documented as proxies
    in `strategies.py`: ``transport_penalty_scale`` (a distance penalty, standing
    in for transit days and tonne-miles) and ``consolidation_bonus_usd`` (a
    per-supplier charge, standing in for one more handling window and at least
    one more transit day, since leg transit is ceil(km / 800)). A third lever,
    ``us_only_sourcing``, restricts the pool before the model is built. These are
    calibrated stand-ins, not a lead-time term: Stage 1 still cannot price a
    supplier's actual lead time, and adding such a term is the clean fix.

    require_dual_source: when True (and the BOM has ≥2 lines), a HARD
    diversification constraint caps how many BOM lines any single distributor
    may source, forcing the plan to spread across ≥2 distributors so a targeted
    outage of the cheapest hub cannot orphan the whole BOM. The solver escalates
    the cap from the tightest that forces diversification (ceil(N/2)) upward and
    takes the first feasible plan; if no cap is feasible (a genuinely
    single-source BOM where every line is offered by one hub) it falls back to
    the unconstrained blind plan.

    min_distributors (k): HARD lower bound on the number of DISTINCT
    distributors the plan opens — ``sum_d y[d] >= k`` — used to trace the
    price-of-resilience frontier (``seeds/run_diversification_sweep.py``).
    ``None`` (the default) adds no variable, no constraint and no objective
    term, so the model handed to CP-SAT is exactly the one this function built
    before the parameter existed; every existing caller is on that path.

    WHY PER-BOM AND NOT PER-LINE. The defensible unit of diversification here
    is the BOM, for two reasons, both empirical rather than aesthetic:

      1. It is the unit the risk measure actually scores. The cascade
         simulation (`app/graph/simulation.run_monte_carlo`) fails whole
         DISTRIBUTORS and then asks how many BOM lines lost every one of their
         suppliers. What protects a plan is therefore the number of distinct
         distributors it depends on, which is exactly ``sum_d y[d]``.
      2. A per-line rule (every line sourced from >= k distributors) is a
         different and much stronger requirement: it needs k offers for EVERY
         component and it must split each line's quantity, which collides with
         the MOQ floor (``q >= moq * x``) — a quantity-1 line cannot be split
         at all. On this catalogue it is infeasible for most BOMs at k = 2,
         which would produce an empty frontier rather than a null one.

    A per-BOM bound is the weaker, purchasable constraint: it says "keep k
    doors open", not "buy every part twice". It is also the one that maps onto
    the metric the benchmark already publishes (`n_distinct_suppliers`, which
    the cost-optimal MILP drives from 3.22 to 1.33 per BOM in run 5).

    ``y[d]`` is only forced UP by the linking constraint ``y[d] >= x[c,d]``, so
    on its own ``sum_d y[d] >= k`` could be satisfied by opening empty
    distributors. When ``min_distributors`` is supplied we therefore also add
    the reverse link ``y[d] <= sum_c x[c,d]``, pinning ``y`` to genuine use.
    (At the optimum ``y`` is already tight whenever a distributor carries a
    positive fixed freight fee or consolidation charge — both are positive
    costs — so this changes the optimal VALUE for no strategy in
    `strategies.py`; it changes the feasible set, which is what the bound
    needs.)

    Raises RuntimeError when no plan with k distinct distributors exists (for
    example k exceeds the number of distributors offering any BOM line, or MOQ
    floors exceed demand on the extra lines). Callers sweeping k should catch
    it and record the k as infeasible rather than treating it as an error.
    """
    # Deferred import: CP-SAT (and the pandas/pyarrow it pulls in transitively)
    # costs ~830 ms to import and is needed only when a solve actually runs, not
    # at boot. Keeping it here takes OR-Tools off the `import app.main` path.
    from ortools.sat.python import cp_model

    if not bom:
        raise ValueError("BOM is empty — cannot solve sourcing with zero components")

    # Pre-filter outliers
    offers, drops = filter_price_outliers(offers, bom)

    # Pre-filter by us_only
    if us_only:
        offers = [o for o in offers if o.is_domestic]

    # Collapse duplicate (component_id, distributor_id) rows to one offer.
    #
    # The offer table carries one row per price-break tier, so the same
    # (component, distributor) pair can appear up to 6 times. The CP-SAT model
    # below keys its x/q variables on (component_id, distributor_id): duplicate
    # rows silently overwrite each other's variable, but every duplicate is still
    # summed into the demand constraint and priced into the objective. That made
    # the demand constraint read k*q == demand (spuriously INFEASIBLE unless
    # demand % k == 0) and charged the unit price as the SUM of the k tier prices
    # -- STM32F103C8T6 at Verical was costed at $30.03/unit instead of $2.86, so
    # the solver systematically avoided multi-tier distributors.
    #
    # We keep the cheapest tier, which is what solve_sourcing_greedy already does
    # implicitly via min(feasible, key=price). Ties break toward more stock. This
    # under-states availability when a pricier tier holds more stock -- a
    # conservative direction, and preferable to modelling a price the distributor
    # does not charge. Proper quantity-dependent price breaks are a separate,
    # larger change.
    deduped: Dict[tuple, Offer] = {}
    for o in offers:
        key = (o.component_id, o.distributor_id)
        best = deduped.get(key)
        if best is None or (o.price_usd, -o.stock) < (best.price_usd, -best.stock):
            deduped[key] = o
    offers = list(deduped.values())

    # Group by component
    offers_by_component: Dict[int, List[Offer]] = {}
    for o in offers:
        offers_by_component.setdefault(o.component_id, []).append(o)

    # Validate every BOM line has at least one offer after filtering
    missing = [b.mpn for b in bom if not offers_by_component.get(b.component_id)]
    if missing:
        raise ValueError(
            f"No valid offers for components after filtering: {missing}"
        )

    # Validate every BOM line has enough STOCK across its surviving offers.
    #
    # The demand row in _build_and_solve is an equality (sum(q) == b.quantity)
    # and every q is capped at its own offer's stock (`upper = min(o.stock,
    # b.quantity)` plus `q <= o.stock * x`). So a line whose surviving offers
    # hold less stock than the build quantity pins every q below the demand it
    # must meet, and CP-SAT returns INFEASIBLE for the whole model.
    #
    # That is a statement about the DATA, not a solver failure: the catalogue
    # simply does not stock enough of this part in the pool being searched. It
    # must surface the same way the missing-offer case above does — a ValueError
    # that /optimize/vrp maps to a 400 naming the part — not as the RuntimeError
    # below, which becomes a 500 reading "Solver failed" and tells the user the
    # server broke.
    #
    # Real cases this catches (verified against the served DB, 2026-08-30):
    #   - component 11 (SIPY RCZ1 & RCZ3): one offer, DigiKey, stock 0. Any
    #     quantity 500'd.
    #   - component 24 (140G-G2C3-C50): domestic stock is 3, so a build quantity
    #     of 4 or more 500'd. Three of the four strategies hard-code
    #     us_only_sourcing=True, so this fired even for requests sent with
    #     us_only=False.
    # 31 catalogue components have a priced domestic offer with zero domestic
    # stock; 18 have zero stock across every offer.
    short = [
        (b.mpn, b.quantity, sum(max(o.stock, 0) for o in offers_by_component[b.component_id]))
        for b in bom
        if sum(max(o.stock, 0) for o in offers_by_component[b.component_id]) < b.quantity
    ]
    if short:
        pool = "domestic distributors only" if us_only else "all distributors"
        raise ValueError(
            "Insufficient stock to fill the BOM from " + pool + ": "
            + "; ".join(
                f"{mpn} needs {qty} but only {avail} in stock" for mpn, qty, avail in short
            )
        )

    all_distributors = {o.distributor_id for o in offers}

    # ── Cap-independent inputs — computed ONCE and captured by _build_and_solve.
    # These do not depend on the diversification cap, so we avoid recomputing
    # them (and re-hitting ML/graph/feed state) on every escalation iteration.
    penalty_scale = getattr(weights, "transport_penalty_scale", 1.0)
    freight = _freight_model_by_did(offers, penalty_scale)
    consolidation_bonus = getattr(weights, "consolidation_bonus_usd", 1.0)

    # Stock-out risk premium from the macro regime model.
    #
    # HONESTY NOTE (updated 2026-08-26): this used to be driven by a scalar
    # replayed out of metrics.joblib (0.9967, baked 2026-07-10) because
    # regime.joblib was not git-tracked and therefore absent in production — a
    # months-old constant was pricing a real surcharge into every solve.
    # regime.joblib / regime_features.joblib are now git-tracked and
    # app/ml/serving.resolve_regime_signal recomputes P(stress) from that
    # model when its ship gate passes, or reports the signal as UNAVAILABLE
    # and returns the documented default 0.0 when it does not.
    #
    # As of this writing the ship gate PASSES (Brier beats both persistence and
    # climatology; see app/ml/regime_model.evaluate_ship_gate), so the regime
    # model is NOT gated off: /ml/stress reports `regime_active: true`,
    # `ship_gate_passed: true`, `stress_probability: ~0.83`, and macro_stress
    # below is that same non-zero value — it DOES contribute a real premium
    # through _stockout_risk_premium_obj_units, not "exactly nothing". The
    # gated-off, zero-contribution state described in earlier revisions of
    # this comment is not the current behaviour; verify against `/ml/stress`
    # before trusting either claim.
    #
    # This value is also NOT continuously live: get_current_stress_prob()
    # (app/ml/regime_model.py) reads `features_df.tail(1)` off the checked-in
    # `regime_features.joblib` artifact, so macro_stress is frozen at whatever
    # row was baked in at last training time. There is no scheduled workflow
    # that refreshes or retrains the regime model (only the weekly lead-time
    # collector in .github/workflows/collect-lead-times.yml is scheduled;
    # model-ci.yml runs on push/PR/dispatch only, not on a cron). The number
    # will not move again until someone manually reruns
    # seeds/train_ml_models.py (or equivalent) and commits new artifacts.
    # As of 2026-08-28 the VINTAGE of that frozen row is published — GET /ml/stress
    # returns `observation_date` / `observation_age_days` / `vintage_is_stale`, and
    # `_ml.regime_status["observation_date"]` carries the same ISO date here (see
    # app/ml/serving.resolve_regime_signal). Publishing it is all that changed:
    # `macro_stress` below is UNCONDITIONAL, exactly as before, and a frame past
    # `regime_model.STRESS_FRAME_MAX_AGE_DAYS` still prices a full surcharge.
    #
    # Whether a stale reading should be decayed or gated off is an OWNER DECISION
    # and is deliberately not taken here. If it is ever taken, this is the single
    # line to change — the date is already in scope on `_ml.regime_status`, so no
    # new plumbing is needed, and `backend/tests/test_stress_vintage.py` already
    # pins the tolerance constant that such a rule would key off.
    from app.ml import get_ml_state  # local import to avoid circular dep at module load
    from app.startup import wait_for_graph, wait_for_ml

    # The ML load and the graph build moved off the ASGI lifespan onto a background
    # thread (app/startup.py) to cut a ~70 s cold start. Both globals used to be
    # guaranteed populated before the first request could arrive; now they are not, and
    # the two `if ... is None` fallbacks below are no longer unreachable. Left alone
    # they would silently price a BOM at macro_stress = 0.0 and solve `graph_aware`
    # with no graph — a DIFFERENT published plan, not a slower one. So wait for the one
    # warm-up. Both waits are no-ops once it has finished, and when none is running.
    wait_for_ml()
    _ml = get_ml_state()
    macro_stress = _ml.current_stress_prob if _ml is not None else 0.0

    # Graph state (graph_aware mode only); feed cache (live macro signals).
    _gs = None
    if graph_aware:
        from app.graph import get_graph_state  # local import
        wait_for_graph()
        _gs = get_graph_state()
    from app.feeds import get_live_data_cache  # local import to avoid circular dep
    _ldc = get_live_data_cache()

    def _build_and_solve(max_lines_cap: Optional[int], min_dists: Optional[int] = None):
        """
        Build the full sourcing MILP and solve it. When ``max_lines_cap`` and
        ``min_dists`` are both None the model is byte-identical in behavior to
        the original (no diversification constraint). When ``max_lines_cap`` is
        an int, each distributor is capped to source at most that many BOM
        lines. When ``min_dists`` is an int, the plan must open at least that
        many distinct distributors (see solve_sourcing's docstring).

        Returns (status, solver, x, q, y).
        """
        model = cp_model.CpModel()

        # x[cid, did] ∈ {0,1} — select this offer
        # q[cid, did] ∈ [0, stock] — quantity ordered
        # y[did] ∈ {0,1} — visit this distributor
        x: Dict[Tuple[int, int], cp_model.IntVar] = {}
        q: Dict[Tuple[int, int], cp_model.IntVar] = {}
        y: Dict[int, cp_model.IntVar] = {}

        for did in all_distributors:
            y[did] = model.NewBoolVar(f"y_{did}")

        for b in bom:
            for o in offers_by_component[b.component_id]:
                key = (b.component_id, o.distributor_id)
                x[key] = model.NewBoolVar(f"x_c{b.component_id}_d{o.distributor_id}")
                # Quantity bounded by stock and demand
                upper = min(o.stock, b.quantity)
                q[key] = model.NewIntVar(0, max(upper, 0), f"q_c{b.component_id}_d{o.distributor_id}")

        for b in bom:
            # Demand coverage: sum of quantities over offers == demand
            model.Add(
                sum(q[(b.component_id, o.distributor_id)]
                    for o in offers_by_component[b.component_id]) == b.quantity
            )
            for o in offers_by_component[b.component_id]:
                key = (b.component_id, o.distributor_id)
                # Stock cap: q ≤ stock * x
                model.Add(q[key] <= o.stock * x[key])
                # MOQ floor: if x=1, q ≥ moq; if x=0, q=0 (already enforced by stock cap)
                if o.moq > 1:
                    model.Add(q[key] >= o.moq * x[key])
                else:
                    model.Add(q[key] >= x[key])  # q ≥ 1 if selected
                # Distributor linking: y ≥ x
                model.Add(y[o.distributor_id] >= x[key])

        # ── Diversification constraint (require_dual_source escalation only) ──
        # Cap how many BOM lines any single distributor may source, forcing the
        # plan to spread across ≥2 distributors instead of consolidating the
        # whole BOM onto one cheapest hub (fixed-charge economics).
        if max_lines_cap is not None:
            for did in all_distributors:
                lines_on_did = [
                    x[(b.component_id, did)]
                    for b in bom
                    if (b.component_id, did) in x
                ]
                if lines_on_did:
                    model.Add(sum(lines_on_did) <= max_lines_cap)

        # ── Minimum-distributor constraint (price-of-resilience frontier) ────
        # sum_d y[d] >= k, with y pinned to genuine use so an empty distributor
        # cannot satisfy the bound. Argued in solve_sourcing's docstring.
        if min_dists is not None:
            for did in all_distributors:
                lines_on_did = [
                    x[(b.component_id, did)]
                    for b in bom
                    if (b.component_id, did) in x
                ]
                if lines_on_did:
                    model.Add(y[did] <= sum(lines_on_did))
                else:
                    model.Add(y[did] == 0)
            model.Add(sum(y[did] for did in all_distributors) >= min_dists)

        # Objective: minimize total component cost + freight + consolidation charge.
        # Built in integer milli-cents (OBJ_SCALE = PRICE_SCALE x OBJ_SUBSCALE).
        # EVERY term converts USD -> objective units exactly once, through
        # `to_obj_units`. Nothing rounds to a coarser unit first and scales up
        # afterwards — that two-step conversion is what silently priced sub-cent
        # offers at zero and left the greedy baseline optimising at a different
        # resolution from the MILP (see `to_obj_units`).
        #
        # Freight is a genuine FIXED-CHARGE model (Balinski 1965 / Kuehn & Hamburger
        # 1963), decomposed by _freight_model_by_did:
        #   fixed[d]    x y[d]              — pay once to open distributor d
        #   per_unit[d] x sum_c q[c,d]      — pay per unit d ACTUALLY ships
        # The second term is what makes splitting a BOM across suppliers divide one
        # BOM's variable freight among them, instead of charging a full BOM's
        # freight to each of them (the old bug).
        #
        # penalty_scale (from StrategyWeights.transport_penalty_scale) is already
        # baked into both components by _freight_model_by_did.
        cost_terms = []
        for b in bom:
            for o in offers_by_component[b.component_id]:
                key = (b.component_id, o.distributor_id)
                price_units = to_obj_units(o.price_usd)
                if price_units == 0 and o.price_usd > 0.0:
                    # Same loud-not-silent rule the freight rate below already
                    # follows. A priced offer must never cost the solver zero:
                    # that is what made MLG0603P43NHT000 ($0.0031) free before
                    # prices were carried at OBJ_SCALE. Needs < $5e-6/unit to
                    # fire now; no offer in this catalogue is close.
                    logger.warning(
                        "unit price for component %s at distributor %s (%.3e USD) "
                        "rounds to 0 at OBJ_SCALE=%d — this offer is FREE to the "
                        "objective",
                        b.component_id, o.distributor_id, o.price_usd, OBJ_SCALE,
                    )
                cost_terms.append(price_units * q[key])

        transport_terms = []
        for did in all_distributors:
            fixed_units = to_obj_units(freight.fixed_by_did[did])
            if fixed_units:
                transport_terms.append(fixed_units * y[did])

            per_unit_usd = freight.per_unit_by_did[did]
            rate_units = to_obj_units(per_unit_usd)
            if rate_units == 0 and per_unit_usd > 0.0:
                # Loud rather than silent: the per-unit rate is real but below the
                # objective's milli-cent resolution (needs < $1e-5/unit, i.e. a
                # domestic distributor ~0.03 km from the depot). Dropping it is
                # numerically harmless but must never happen quietly.
                logger.warning(
                    "freight per-unit rate for distributor %s (%.3e USD/unit) rounds "
                    "to 0 at OBJ_SCALE=%d — variable freight term dropped for this "
                    "distributor",
                    did, per_unit_usd, OBJ_SCALE,
                )
            if rate_units:
                for b in bom:
                    key = (b.component_id, did)
                    if key in q:
                        transport_terms.append(rate_units * q[key])

        consolidation_units = to_obj_units(consolidation_bonus)
        consolidation_terms = [
            consolidation_units * y[did]
            for did in all_distributors
        ]

        # ── Risk surcharge terms (already in objective units) ────────────────
        # PER UNIT, on q[key] — not once per line, on x[key] (fixed 2026-09-03).
        #
        # UNITS. `_stockout_risk_premium_obj_units` returns
        #     to_obj_units(unit_price x stockout_risk x RISK_PREMIUM_RATE)
        # — a surcharge on the UNIT PRICE, carried in the same milli-cents PER
        # UNIT as `price_units` above. Its docstring calls it an addition to "the
        # MILP's effective price"; RISK_PREMIUM_RATE is documented as "15% of unit
        # price" at maximum stress and vulnerability. `premium * x[key]` therefore
        # charged one single unit's surcharge for a line of any size: on a
        # 100-unit line the plan paid 0.15% of what the model says it charges.
        #
        # WHY THIS IS WORSE THAN A SCALE ERROR. On the binary the coefficient is
        # constant in quantity, so the term could only ever influence WHETHER an
        # offer is opened — never HOW MUCH is bought from it. Every split-line
        # allocation was priced as if risk were free. "Take less from the exposed
        # source" is the one thing a stock-out surcharge exists to say, and on x
        # it was not expressible in the model at all.
        #
        # MEASURED (demo BOM, 5 lines / 225 units). Sweeping macro_stress 0.0 ->
        # 1.0 moved the objective by $0.14 on x, and by $7.07 on q (greenest:
        # $908.39 -> $915.45). The demo BOM's own plan does NOT flip either way,
        # and that is not evidence the surcharge is still inert: its lines'
        # vulnerabilities are near-constant ACROSS the offers competing for each
        # line (origin is a property of the part, so it cancels within a line, and
        # the stock-coverage term separates only thin-stock offers the solver
        # rejects on stock anyway), so the surcharge scales those offers almost
        # uniformly. Where vulnerability does differ, the fix bites: on the BOM
        # {120, 504, 509, 595} at qty 100, `fastest`/`greenest`/`balanced` all
        # source TMS320C6678ACYPA 60/40 across distributors 28 and 56 at
        # macro_stress 0.0 and move to 98/2 from 0.25 upward — 38 units pulled off
        # the thinner-stocked source. On x that same sweep returns one identical
        # plan at every stress value, because a per-line charge cannot move a
        # quantity split.
        #
        # Every other per-unit term here is already on q: `price_units * q[key]`,
        # `rate_units * q[key]` (variable freight), and the graph and feed
        # surcharges below. Only the two genuine fixed charges — freight
        # `fixed_units * y[did]` and the consolidation charge — sit on a binary,
        # because they are per-opened-distributor costs that do not scale with
        # quantity. This term is not one of those.
        risk_terms = []
        for b in bom:
            for o in offers_by_component[b.component_id]:
                key = (b.component_id, o.distributor_id)
                premium = _stockout_risk_premium_obj_units(o, b, macro_stress)
                if premium > 0:
                    risk_terms.append(premium * q[key])

        # ── Graph surcharge terms (graph_aware mode only) ────────────────────
        # Additive node-weight surcharge on q[key] (betweenness concentration risk)
        # plus single-source component risk. Falls back silently to zero
        # surcharge if GraphState not loaded.
        graph_surcharge_terms = []
        if graph_aware and _gs is not None:
            for b in bom:
                component_offers = offers_by_component[b.component_id]
                for o in component_offers:
                    key = (b.component_id, o.distributor_id)
                    btwn = _gs.betweenness.get(o.distributor_id, 0.0)
                    surcharge = _graph_surcharge_obj_units(o, btwn, component_offers)
                    if surcharge > 0:
                        graph_surcharge_terms.append(surcharge * q[key])

        # ── Feed risk surcharge terms (live macro signals) ───────────────────
        # Additive surcharge from GPR + ACLED live feeds. Per D-01.
        # Falls back to 0 when LiveDataCache not loaded or feeds unavailable.
        feed_surcharge_terms = []
        if _ldc is not None:
            for b in bom:
                for o in offers_by_component[b.component_id]:
                    key = (b.component_id, o.distributor_id)
                    f_surcharge = _feed_risk_obj_units(
                        o,
                        distributor_country=getattr(o, 'distributor_country', 'US'),
                        is_chinese_origin=getattr(o, 'is_chinese_origin', False),
                        cache=_ldc,
                    )
                    if f_surcharge > 0:
                        feed_surcharge_terms.append(f_surcharge * q[key])

        model.Minimize(
            sum(cost_terms)
            + sum(transport_terms)
            + sum(consolidation_terms)
            + sum(risk_terms)
            + sum(graph_surcharge_terms)
            + sum(feed_surcharge_terms)
        )

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 5.0
        # Single worker: these models are tiny (solve in ~ms), and a single
        # deterministic worker keeps results reproducible (seed=42 narrative)
        # and avoids an OR-Tools multi-worker deadlock seen under bare-python
        # invocation on macOS.
        solver.parameters.num_search_workers = 1
        status = solver.Solve(model)
        return status, solver, x, q, y

    # ── Solve blind first, then diversify ONLY if consolidated onto one hub ──
    # Policy: "mandate a second source for BOMs the cost-optimizer consolidated
    # onto a single hub." We never reshuffle an already-diversified plan —
    # that can only make its concentration WORSE, never better.
    status, solver, x, q, y = _build_and_solve(None, min_distributors)

    if require_dual_source and len(bom) >= 2 and status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        blind_dids = {
            did for (cid, did), qv in q.items() if solver.Value(qv) > 0
        }
        if len(blind_dids) == 1:
            # Blind plan puts the whole BOM on ONE hub — force a second source.
            # Escalate the cap from the tightest that forces spreading
            # (ceil(N/2)) up to N-1; take the FIRST feasible plan. A cap of N
            # would not force any diversification, so we stop below it. If NO
            # cap is feasible (genuinely single-source BOM), keep the blind plan.
            n = len(bom)
            for cap in range(math.ceil(n / 2), n):
                d_status, d_solver, d_x, d_q, d_y = _build_and_solve(cap, min_distributors)
                if d_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    status, solver, x, q, y = d_status, d_solver, d_x, d_q, d_y
                    break
        # else: already diversified — keep the blind result exactly as-is.

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        detail = ""
        if min_distributors is not None:
            detail = (
                f" with min_distributors={min_distributors} over "
                f"{len(all_distributors)} candidate distributors"
            )
        raise RuntimeError(
            f"Sourcing MILP infeasible (status={solver.StatusName(status)}){detail}"
        )

    # Extract assignments
    assignments: List[SourcingAssignment] = []
    for b in bom:
        for o in offers_by_component[b.component_id]:
            key = (b.component_id, o.distributor_id)
            qty = solver.Value(q[key])
            if qty > 0:
                assignments.append(SourcingAssignment(
                    component_id=b.component_id,
                    mpn=b.mpn,
                    distributor_id=o.distributor_id,
                    distributor_name=o.distributor_name,
                    quantity=qty,
                    unit_price_usd=o.price_usd,
                ))

    total_cost = sum(a.line_total for a in assignments)
    selected = sorted({a.distributor_id for a in assignments})

    return SourcingResult(
        assignments=assignments,
        total_component_cost=total_cost,
        selected_distributor_ids=selected,
        outlier_drops=drops,
        status=solver.StatusName(status),
        objective_usd=solver.ObjectiveValue() / OBJ_SCALE,
    )
