"""Newsvendor decision layer -- a demand DISTRIBUTION turned into an order quantity.

WHAT THIS CLOSES
----------------
`app/ml/intermittent.py` emits a genuine predictive distribution for every method on the
car-parts leaderboard, and `app/ml/proper_scoring.py` scores those distributions with CRPS
and the pinball loss. Until this module nothing CONSUMED them. The repo could say which
forecast was better calibrated; it could not say what to do about it. A quantile no
decision reads is a number, not a result -- and `docs/INTERMITTENT_DEMAND.md` says as much
in its own words: the planner's question is "how much do I stock", which is a quantile,
and a point forecast does not have one.

The newsvendor is the smallest honest bridge from that distribution to that decision.

THE DECISION
------------
One review period. Demand D over the period is random with predictive cdf F. We commit an
order-up-to level q BEFORE D is observed. Two asymmetric per-unit costs:

    Cu   underage -- cost of being one unit SHORT for the period
    Co   overage  -- cost of holding one unit that was not demanded, for the period

    C(q) = Cu * E[(D - q)+] + Co * E[(q - D)+]

For continuous D, dC/dq = -Cu * P(D > q) + Co * P(D <= q), which vanishes at

    F(q*) = Cu / (Cu + Co) =: tau                        (the CRITICAL FRACTILE)

For integer-valued D -- which is what spare-parts demand is -- the derivative argument is
replaced by the first difference C(q+1) - C(q) = (Cu + Co) * F(q) - Cu, which first turns
non-negative at the smallest integer q with F(q) >= tau. That is exactly the generalised
inverse cdf `proper_scoring.quantile_from_pmf(pmf, tau)`. No solver, no approximation, no
continuity assumption: the optimum of this decision is a lookup in the predictive cdf.

The dual identity is worth stating because it is what makes the whole forecasting track
load-bearing rather than decorative:

    realized newsvendor cost at q  ==  (Cu + Co) * pinball_loss(q, y, tau)

exactly, for every q and y (asserted in `tests/test_newsvendor.py`). The scaled pinball
loss already on the leaderboard IS a decision cost up to a constant, so a method that wins
under pinball at level tau is the method that minimises expected newsvendor cost at that
tau. Ban & Rudin (2019) make the same observation the basis of their "big data newsvendor":
because newsvendor cost is the check loss, fitting a quantile predictor at tau is
decision-optimal by construction -- there is no differentiable-solver layer to add.

WHERE Cu AND Co COME FROM -- every input is a constant already cited in this repo
--------------------------------------------------------------------------------
Nothing here is invented for the occasion. Both costs are per unit, per review period, and
both are proportional to the unit price:

  Co  =  app.optimization.costs.holding_cost_usd(unit_price, review_period_days)
      =  unit_price * 0.25 * (review_period_days / 365)
     The 25%/yr electronics holding rate is `costs.ANNUAL_HOLDING_RATE`, cited to Gartner
     IT Supply Chain Benchmarks 2022 (capital + obsolescence + warehousing + insurance) and
     already used by the freight/holding cost model. This module calls that same function
     rather than re-deriving the number, so the two cannot drift.

  Cu  =  unit_price * EXPEDITE_PREMIUM                        (shortage_mode="expedite")
     0.15, the emergency-reprocurement premium. It is the same number as
     `graph.simulation.EMERGENCY_COST_PREMIUM`; a test asserts the two agree.

     JUSTIFICATION, because a stockout penalty is exactly the kind of number that gets
     invented. A spare part that is out of stock is not a lost sale: the demand does not
     evaporate, the unit is re-procured on an emergency footing. The cost of the shortage
     is therefore the PREMIUM paid to recover it, not the margin on it -- which is why Cu
     here is a fraction of the unit price rather than a multiple of it. That is the
     conservative reading, and it is the one this module publishes.

  Cu  =  unit_price * STOCKOUT_ESCALATION_MULTIPLE            (shortage_mode="line_down")
     3.0, after Snyder & Daskin (2005): when there is
     no substitutable source the recourse is a line-down / respin event, priced as a
     large-but-finite multiple of unit price. Offered as a SENSITIVITY, not as the default.
     Read the warning on `NewsvendorCosts.resolution_warning` before quoting it: at
     shortage_mode="line_down" tau is 0.993, and a 99.3rd percentile of a count law
     estimated from at most 45 monthly observations is an extrapolation of the assumed
     parametric form, not an empirical quantile. The number exists; the evidence for it
     does not.

  DELIBERATELY EXCLUDED, with the direction of the bias stated: the fixed air-freight
  consignment charge (`constants.AIR_FREIGHT_BASE_USD` = $150) is per CONSIGNMENT, not per
  unit, so it cannot enter a linear per-unit Cu without an assumption about how many short
  units share a consignment. The variable air-freight uplift
  (AIR_FREIGHT_RATE_USD_PER_KG * AVG_COMPONENT_KG = $0.25/unit) is available via
  `expedite_freight_usd_per_unit` and defaults to 0.0. Both omissions push Cu DOWN, hence
  tau down, hence q* down: the published order quantities are conservative (they understock
  relative to the true asymmetry), and the measured saving is a lower bound.

THE CRITICAL FRACTILE DOES NOT DEPEND ON THE UNIT PRICE
-------------------------------------------------------
Both costs are proportional to price, so the price cancels out of tau:

    tau = pi_e / (pi_e + r_h * L / 12)      with pi_e = 0.15, r_h = 0.25, L in months
        = 0.15 / (0.15 + 0.020833)  =  0.8780        at a one-month review period

This is not a convenience, it is what makes the evaluation below possible at all. The
Monash car-parts panel is real intermittent spare-parts demand with NO prices attached, and
this repo does not fabricate data. Because tau is price-free, the order quantity is
computable on that panel with nothing invented, and every dollar figure is reported per
$1.00 of unit price and scales linearly. Multiply by the price of your part; do not read
the default of $1.00 as a claim about what a car part costs.

WHICH DISTRIBUTION DRIVES THE DECISION -- say it, never substitute silently
---------------------------------------------------------------------------
The decision is driven by the DEMAND predictive distribution from `app.ml.intermittent`:
the compound Bernoulli(p) x zero-truncated-NegBin(mean z) law that the CRPS / pinball
leaderboard already scores, evaluated on the Monash car-parts panel. It is not the
lead-time model's predictive distribution and it is not a fitted per-part forecast for the
electronic components this app sells -- no public per-SKU demand series exists for those,
`docs/INTERMITTENT_DEMAND.md` explains why the synthetic one was deleted, and this module
does not resurrect it under a new name.

Three distinct paths, and `NewsvendorDecision.distribution_source` always records which one
ran:

  "empirical"   `climatology_dist` -- the in-sample empirical pmf of the training window.
                q* is then a genuine empirical quantile with no parametric assumption
                beyond exchangeability of train and test.
  "parametric"  `croston_dist` / `sba_dist` / `tsb_dist` -- the compound-Bernoulli law.
                Preferred, and the default (`tsb`), because the empirical path cannot
                resolve a quantile above 1 - 1/n_train and cannot place mass above the
                largest observed order.
  "normal"      the FALLBACK, used only when no pmf is available and the caller has just a
                mean and a standard deviation. q* = mu + sigma * Phi^-1(tau). It is a
                continuous, symmetric, unbounded law fitted to a count variable that is 76%
                zeros, so it is wrong here in a specific and measurable way -- and the
                evaluation below measures it, as the `normal_safety_stock` baseline. It is
                the fallback, never the default.
                `scarf_order_quantity` is the distribution-free alternative on the same
                two moments (Scarf 1958): the min-max optimal order over EVERY law with
                that mean and variance, so it is the DRO counterpart of the normal
                fallback rather than another guess at a shape.

WHAT IS NOT MODELLED (read before quoting any number)
------------------------------------------------------
Modelled:  a single review period; asymmetric per-unit underage/overage costs; an integer
           order-up-to level; the full count predictive distribution; multi-period demand
           by exact convolution when the review period spans several months.
NOT modelled: inventory CARRY-OVER between periods (each period starts from zero on-hand,
           which is the newsvendor's defining simplification -- the multi-period version is
           an (s, S) policy, a different and larger problem); a fixed cost per order (that
           is what forces (s, S) in the first place); lead-time uncertainty, although the
           lead-time model has a predictive distribution that could supply it; MOQ and
           price breaks, both of which exist in this repo's offer data and both of which
           make the true feasible set non-convex; salvage value (Co is a carrying charge,
           not a write-off -- see below); backorder vs lost-sale (the expedite framing
           assumes backorder); correlation of demand across periods (the compound-Bernoulli
           law is i.i.d. by construction, which is what makes the convolution exact WITHIN
           the model and only within it).

THIS IS A CARRYING-CHARGE NEWSVENDOR, NOT A PERISHABLE ONE
-----------------------------------------------------------
The textbook newsvendor sells newspapers: unsold stock is worthless at midnight and
Co = c - salvage, close to the full unit cost. Electronic spare parts do not perish at
midnight. An unsold unit carries forward and the loss is one period of carrying charge, so
Co is ~2% of unit price per month, not ~100%. That single modelling choice is what makes
tau 0.88 rather than 0.13, and it is the choice a reader should attack first. It is stated
here rather than buried because the answer changes completely if the part is genuinely
perishable or obsolescence-prone -- pass a larger `holding_rate_annual`, or a
`review_period_months` long enough to represent the write-off, and re-read tau.

REFERENCES
----------
- Arrow, K.J., Harris, T. & Marschak, J. (1951). "Optimal Inventory Policy."
  Econometrica 19(3):250-272. -- the critical-fractile result.
- Scarf, H. (1958). "A Min-Max Solution of an Inventory Problem", in Arrow, Karlin & Scarf,
  Studies in the Mathematical Theory of Inventory and Production, Stanford UP, ch. 12.
  Originally RAND P-910, https://www.rand.org/pubs/papers/P910.html
- Ban, G-Y. & Rudin, C. (2019). "The Big Data Newsvendor: Practical Insights from Machine
  Learning." Operations Research 67(1):90-108.
  https://pubsonline.informs.org/doi/10.1287/opre.2018.1757
- Bertsimas, D. & Kallus, N. (2020). "From Predictive to Prescriptive Analytics."
  Management Science 66(3):1025-1044.
- Snyder, L.V. & Daskin, M.S. (2005). "Reliable Facility Location Models."
  Transportation Science 39(3):400-416. -- the shortage-escalation multiple.
- Gneiting, T. & Raftery, A.E. (2007). "Strictly Proper Scoring Rules, Prediction, and
  Estimation." JASA 102(477):359-378.
- Syntetos, A.A. & Boylan, J.E. (2005). "The accuracy of intermittent demand estimates."
  IJF 21(2):303-314.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, cast

import numpy as np

from app.ml.backtest import rolling_origins
from app.ml.intermittent import (
    climatology_dist,
    croston,
    croston_dist,
    mase,
    mase_denominator,
    naive_last,
    naive_last_dist,
    sba,
    sba_dist,
    tsb,
    tsb_dist,
    zero_dist,
)
from app.ml.proper_scoring import quantile_from_pmf
from app.optimization.costs import ANNUAL_HOLDING_RATE, holding_cost_usd

logger = logging.getLogger(__name__)

#: Anything this module will accept as a probability mass function over the counts
#: 0..K, or as a demand history. numpy arrays are not `Sequence` to a type checker but
#: are what every caller actually passes, so the alias says so once instead of at
#: twelve call sites.
PmfLike = Sequence[float] | np.ndarray


# ── Cost inputs ──────────────────────────────────────────────────────────────

#: Emergency-reprocurement premium on the unit price when a needed unit is not on hand.
#: The same 0.15 as `graph.simulation.EMERGENCY_COST_PREMIUM`, pinned equal to it by
#: `tests/test_newsvendor.py::test_expedite_premium_matches_the_simulation_constant`.
EXPEDITE_PREMIUM: float = 0.15

#: Shortage escalation for a line-down / respin event with no substitutable source, after
#: Snyder & Daskin (2005). A SENSITIVITY, not
#: the default -- see `NewsvendorCosts.resolution_warning`.
STOCKOUT_ESCALATION_MULTIPLE: float = 3.0

#: Variable air-freight uplift per expedited unit: AIR_FREIGHT_RATE_USD_PER_KG (5.0,
#: IATA Cargo Market Report 2023) x AVG_COMPONENT_KG (0.05). Offered, not defaulted --
#: including it makes Cu price-dependent and therefore tau price-dependent, which would
#: forfeit the price-invariance the panel evaluation relies on.
EXPEDITE_FREIGHT_USD_PER_UNIT: float = 0.25

DAYS_PER_MONTH: float = 365.0 / 12.0

#: Shortage cost models this module knows how to price, and the multiple each applies to
#: the unit price.
SHORTAGE_MODES: Dict[str, float] = {
    "expedite": EXPEDITE_PREMIUM,
    "line_down": STOCKOUT_ESCALATION_MULTIPLE,
}

#: Above this critical fractile, q* is an extrapolation of the assumed parametric tail
#: rather than a quantity the training window can resolve. 45 monthly observations is the
#: longest training window in the panel protocol, so the finest empirical resolution
#: available is 1/45 = 0.022 -- anything past 1 - 1/45 = 0.978 is the model talking, not
#: the data. Rounded down to 0.97 so the warning fires before the boundary, not after it.
TAU_RESOLUTION_CEILING: float = 0.97


@dataclass(frozen=True)
class NewsvendorCosts:
    """The two costs and the critical fractile they imply, with their provenance.

    Frozen and self-describing on purpose: every published order quantity has to be able to
    name the cost asymmetry behind it, and a bare `tau=0.878` in a response body is not an
    explanation.
    """

    unit_price_usd: float
    review_period_months: float
    holding_rate_annual: float
    shortage_mode: str
    shortage_multiple: float
    expedite_freight_usd_per_unit: float
    underage_usd: float
    overage_usd: float
    critical_ratio: float
    #: Non-None when tau sits past what the training window can resolve empirically.
    resolution_warning: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "unit_price_usd": round(self.unit_price_usd, 6),
            "review_period_months": self.review_period_months,
            "holding_rate_annual": self.holding_rate_annual,
            "shortage_mode": self.shortage_mode,
            "shortage_multiple": self.shortage_multiple,
            "expedite_freight_usd_per_unit": round(self.expedite_freight_usd_per_unit, 6),
            "underage_usd_per_unit": round(self.underage_usd, 6),
            "overage_usd_per_unit": round(self.overage_usd, 6),
            "critical_ratio": round(self.critical_ratio, 6),
            "cost_asymmetry": round(self.underage_usd / self.overage_usd, 4) if self.overage_usd > 0 else None,
            "resolution_warning": self.resolution_warning,
            "derivation": (
                f"Cu = unit_price x {self.shortage_multiple} ({self.shortage_mode}) "
                f"+ ${self.expedite_freight_usd_per_unit:.2f}/unit expedite freight; "
                f"Co = unit_price x {self.holding_rate_annual} x "
                f"{self.review_period_months}/12 (Gartner 2022 electronics holding rate, "
                f"via app.optimization.costs.holding_cost_usd); "
                f"tau = Cu / (Cu + Co) = {self.critical_ratio:.4f}."
            ),
        }


def newsvendor_costs(
    unit_price_usd: float = 1.0,
    review_period_months: float = 1.0,
    shortage_mode: str = "expedite",
    holding_rate_annual: float = ANNUAL_HOLDING_RATE,
    expedite_freight_usd_per_unit: float = 0.0,
) -> NewsvendorCosts:
    """Build (Cu, Co, tau) from this repo's own cited cost constants.

    Args:
        unit_price_usd: price of one unit. Defaults to $1.00, which makes every cost read
            as "USD per dollar of unit price"; tau does not depend on it (both costs are
            proportional to price) unless `expedite_freight_usd_per_unit` is non-zero.
        review_period_months: length of the period the order has to cover.
        shortage_mode: "expedite" (default, 0.15 x price -- the shortage is an emergency
            re-procurement) or "line_down" (3.0 x price -- no substitutable source; a
            sensitivity, and beyond the resolution of the data, see the module docstring).
        holding_rate_annual: annual carrying rate. Defaults to the repo's Gartner-cited
            0.25 for electronics.
        expedite_freight_usd_per_unit: variable air-freight uplift on an expedited unit.
            Defaults to 0.0, which understates Cu -- see the module docstring for why the
            fixed consignment charge cannot be included and what that biases.

    Raises:
        ValueError: on a non-positive price or period, an unknown shortage mode, or costs
            that do not admit a fractile (both zero).
    """
    if unit_price_usd <= 0:
        raise ValueError(f"unit_price_usd must be positive, got {unit_price_usd}")
    if review_period_months <= 0:
        raise ValueError(f"review_period_months must be positive, got {review_period_months}")
    if shortage_mode not in SHORTAGE_MODES:
        raise ValueError(f"unknown shortage_mode {shortage_mode!r}; expected one of {sorted(SHORTAGE_MODES)}")
    if holding_rate_annual < 0 or expedite_freight_usd_per_unit < 0:
        raise ValueError("holding_rate_annual and expedite_freight_usd_per_unit must be non-negative")

    multiple = SHORTAGE_MODES[shortage_mode]
    underage = unit_price_usd * multiple + expedite_freight_usd_per_unit
    period_days = review_period_months * DAYS_PER_MONTH
    if holding_rate_annual == ANNUAL_HOLDING_RATE:
        # Delegated, not re-derived: the SAME function the freight/holding cost model uses,
        # so the newsvendor's overage cost and the optimizer's holding cost cannot drift.
        overage = holding_cost_usd(unit_price_usd, period_days)
    else:
        overage = unit_price_usd * holding_rate_annual * (period_days / 365.0)

    tau = critical_ratio(underage, overage)
    warning = None
    if tau > TAU_RESOLUTION_CEILING:
        warning = (
            f"tau = {tau:.4f} sits above {TAU_RESOLUTION_CEILING}. The longest training "
            "window in this panel is 45 monthly observations, so the finest empirical "
            "quantile resolution available is 1/45 = 0.022. An order quantity at this "
            "fractile is an EXTRAPOLATION of the assumed compound-Bernoulli tail, not a "
            "quantile the data can resolve. Report it as a sensitivity, never as a "
            "measured service level."
        )
    return NewsvendorCosts(
        unit_price_usd=float(unit_price_usd),
        review_period_months=float(review_period_months),
        holding_rate_annual=float(holding_rate_annual),
        shortage_mode=shortage_mode,
        shortage_multiple=float(multiple),
        expedite_freight_usd_per_unit=float(expedite_freight_usd_per_unit),
        underage_usd=float(underage),
        overage_usd=float(overage),
        critical_ratio=float(tau),
        resolution_warning=warning,
    )


def critical_ratio(underage_usd: float, overage_usd: float) -> float:
    """tau = Cu / (Cu + Co), the fractile of demand to stock up to.

    Strictly between 0 and 1 for two positive costs. Co = 0 would mean holding is free,
    whose optimum is "order everything" and has no interior fractile; Cu = 0 would mean a
    shortage is free, whose optimum is "order nothing". Both are rejected rather than
    clipped, because a tau of exactly 0 or 1 silently turns a decision into a degenerate
    policy and `quantile_from_pmf` would reject it one frame later with a worse message.
    """
    if underage_usd < 0 or overage_usd < 0:
        raise ValueError(f"costs must be non-negative, got Cu={underage_usd}, Co={overage_usd}")
    total = underage_usd + overage_usd
    if total <= 0:
        raise ValueError("Cu and Co cannot both be zero -- there is no trade-off to solve")
    if underage_usd == 0 or overage_usd == 0:
        raise ValueError(
            f"a zero cost gives a degenerate fractile (Cu={underage_usd}, Co={overage_usd}); "
            "order-nothing and order-everything are not newsvendor solutions"
        )
    return float(underage_usd / total)


# ── The decision, on a discrete predictive distribution ──────────────────────


def order_quantity_from_pmf(pmf: PmfLike, tau: float) -> float:
    """The newsvendor optimum for an integer-valued demand: min{q : F(q) >= tau}.

    A thin, deliberate alias of `proper_scoring.quantile_from_pmf`. It exists so the call
    site reads as a decision rather than as a statistic, and so this docstring can say the
    thing that matters: for a discrete demand law this is not an approximation of the
    critical-fractile solution, it IS the exact minimiser of
    C(q) = Cu E[(D-q)+] + Co E[(q-D)+] over the integers, because C is convex on Z with
    first difference (Cu + Co) F(q) - Cu.

    One inherited edge: `quantile_from_pmf` clamps to the last index of the support. The
    compound-Bernoulli supports are built out to a tail mass of 1e-10, so the clamp only
    binds for tau within 1e-10 of 1 -- but `climatology_dist` supports end at the largest
    order ever observed, where a high tau can legitimately hit the cap. That is the
    empirical path honestly refusing to invent a tail, and it is one of the reasons the
    parametric path is the default.
    """
    # cast, not a conversion: `quantile_from_pmf` annotates Sequence[float] but does
    # `np.asarray` on the way in, so an ndarray is what it wants at runtime.
    return quantile_from_pmf(cast(Sequence[float], _normalised(pmf)), tau)


def _normalised(pmf: PmfLike) -> np.ndarray:
    p = np.asarray(pmf, dtype=float)
    if p.ndim != 1 or p.size == 0:
        raise ValueError("pmf must be a non-empty 1-D array over counts 0..K")
    if np.any(p < -1e-12):
        raise ValueError("pmf has negative mass")
    total = float(p.sum())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("pmf does not sum to a positive finite number")
    return p / total


def pmf_moments(pmf: PmfLike) -> Tuple[float, float]:
    """(mean, standard deviation) of a pmf over the counts 0..K.

    Exposed because the normal-approximation baseline has to be fed the SAME first two
    moments as the distributional policy -- otherwise the comparison measures forecast
    quality instead of the thing under test, which is whether assuming normality on a
    76%-zero count law costs you money.
    """
    p = _normalised(pmf)
    k = np.arange(p.size, dtype=float)
    mean = float(np.sum(k * p))
    var = float(np.sum((k - mean) ** 2 * p))
    return mean, math.sqrt(max(var, 0.0))


def aggregate_pmf(pmf: PmfLike, periods: int) -> np.ndarray:
    """pmf of demand summed over `periods` consecutive periods, by exact convolution.

    EXACT WITHIN THE MODEL AND ONLY THERE. The compound-Bernoulli predictive law is
    i.i.d. across periods by construction (every `*_dist` function returns the same pmf for
    every step of the horizon), so the convolution is the exact law of the sum under that
    model. It is NOT robust to serial correlation in the real series, which intermittent
    demand plausibly has -- an obsoleting part's zeros cluster. If the real demand is
    positively autocorrelated this understates the variance of the total and therefore
    understates q*.
    """
    if periods < 1:
        raise ValueError(f"periods must be >= 1, got {periods}")
    base = _normalised(pmf)
    out = base
    for _ in range(periods - 1):
        out = np.convolve(out, base)
    return out


def expected_cost_from_pmf(
    pmf: PmfLike, q: float, underage_usd: float, overage_usd: float
) -> Dict[str, float]:
    """Exact expected cost of ordering `q`, decomposed into its two halves.

    Exact, not simulated: the support is finite and enumerated, so the expectation is a
    dot product. Also returns the two service measures a planner actually asks for, each
    with the exposure window and unit that makes them readable -- `fill_rate` is a fraction
    of UNITS met from stock, `cycle_service_level` is P(no shortage in the period). They are
    different numbers and are routinely confused; both are reported so neither can be
    quoted as the other.
    """
    p = _normalised(pmf)
    k = np.arange(p.size, dtype=float)
    short = np.maximum(k - q, 0.0)
    excess = np.maximum(q - k, 0.0)
    expected_short = float(np.sum(short * p))
    expected_excess = float(np.sum(excess * p))
    mean = float(np.sum(k * p))
    return {
        "expected_units_short": expected_short,
        "expected_units_held": expected_excess,
        "expected_underage_usd": expected_short * underage_usd,
        "expected_overage_usd": expected_excess * overage_usd,
        "expected_total_usd": expected_short * underage_usd + expected_excess * overage_usd,
        "cycle_service_level": float(np.sum(p[: int(math.floor(q)) + 1])) if q >= 0 else 0.0,
        "fill_rate": 1.0 if mean <= 0 else float(1.0 - expected_short / mean),
        "expected_demand": mean,
    }


def realized_cost(q: float, y: float, underage_usd: float, overage_usd: float) -> float:
    """Cost actually paid: Cu * (y - q)+ + Co * (q - y)+.

    Equals (Cu + Co) * pinball_loss(q, y, tau) exactly. That identity is the reason the
    scaled pinball loss on the demand leaderboard is a decision cost in disguise, and it is
    asserted in the tests rather than merely asserted here.
    """
    diff = float(y) - float(q)
    return float(underage_usd * diff) if diff >= 0 else float(-overage_usd * diff)


# ── Parametric fallbacks -- used only when there is no pmf ────────────────────


def normal_order_quantity(mean: float, sd: float, tau: float) -> float:
    """q* = mu + sigma * Phi^-1(tau). The FALLBACK, not the default.

    HEURISTIC ON THIS DATA, and knowingly so. A normal law is continuous, symmetric and
    unbounded; car-parts demand is an integer count that is 75.9% exactly zero. The
    approximation cannot represent the atom at zero and will return a negative order
    quantity for any series whose mean is small relative to its spread at tau < 0.5 (it is
    clipped at 0 here, which is itself a distortion of the optimum rather than a fix).

    It is implemented because it is what the textbook safety-stock formula
    `mu + z * sigma` does, it is what most planning systems do, and the only honest way to
    say "using the count distribution correctly is worth money" is to MEASURE the gap
    against it. It is the `normal_safety_stock` baseline in `run_panel_evaluation`.

    `statistics.NormalDist` rather than scipy: scipy reaches this container only
    transitively via scikit-learn and is not pinned in requirements.txt.
    """
    if sd < 0:
        raise ValueError(f"sd must be non-negative, got {sd}")
    if not 0.0 < tau < 1.0:
        raise ValueError("tau must lie strictly between 0 and 1")
    return float(max(0.0, mean + sd * NormalDist().inv_cdf(tau)))


def scarf_order_quantity(mean: float, sd: float, underage_usd: float, overage_usd: float) -> float:
    """Scarf's (1958) min-max newsvendor: distribution-free on the first two moments.

        q = mu + (sigma / 2) * ( sqrt(Cu/Co) - sqrt(Co/Cu) )

    The order that maximises the worst-case profit over EVERY demand law with mean mu and
    standard deviation sigma. This is the distributionally robust counterpart of
    `normal_order_quantity` -- same two moments in, but no shape assumed, an ambiguity set
    rather than a guess. It is the same move a CVaR objective makes, in closed
    form and one dimension.

    Its own limitation, stated: the min-max criterion is pessimistic by design, so on a
    demand law that is NOT adversarial it deliberately leaves expected cost on the table.
    It is reported alongside the fractile solution as a robustness reference, never as a
    replacement for it.
    """
    if sd < 0:
        raise ValueError(f"sd must be non-negative, got {sd}")
    ratio = critical_ratio(underage_usd, overage_usd)  # validates both costs
    del ratio
    root = math.sqrt(underage_usd / overage_usd)
    return float(max(0.0, mean + 0.5 * sd * (root - 1.0 / root)))


def uniform_order_quantity(low: float, high: float, tau: float) -> float:
    """q* = low + tau * (high - low) for D ~ Uniform[low, high]. Closed form.

    Present because it is the one newsvendor instance with an optimum that can be written
    down and checked by hand, which is what `tests/test_newsvendor.py` does with it.
    """
    if high < low:
        raise ValueError(f"high must be >= low, got low={low}, high={high}")
    if not 0.0 < tau < 1.0:
        raise ValueError("tau must lie strictly between 0 and 1")
    return float(low + tau * (high - low))


# ── One decision, fully described ────────────────────────────────────────────


@dataclass(frozen=True)
class NewsvendorDecision:
    """An order quantity plus everything a reader needs to argue with it."""

    order_quantity: float
    costs: NewsvendorCosts
    distribution_source: str
    method: str
    expected: Dict[str, float]
    comparisons: Dict[str, float]
    caveats: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "order_quantity": self.order_quantity,
            "costs": self.costs.as_dict(),
            "distribution_source": self.distribution_source,
            "forecast_method": self.method,
            "expected": {k: round(v, 6) for k, v in self.expected.items()},
            "comparisons": {k: round(v, 6) for k, v in self.comparisons.items()},
            "caveats": list(self.caveats),
        }


#: Forecast method -> (distribution builder, whether the law is empirical or parametric).
#: `zero` and `naive_last` are degenerate point forecasts lifted to distributions; they are
#: on the list because they are on the demand leaderboard, and because a decision layer
#: that cannot be pointed at the MASE winner cannot demonstrate that the MASE winner is a
#: bad decision.
DIST_BUILDERS: Dict[str, Tuple[Callable[..., List[np.ndarray]], str]] = {
    "tsb": (tsb_dist, "parametric"),
    "sba": (sba_dist, "parametric"),
    "croston": (croston_dist, "parametric"),
    "climatology": (climatology_dist, "empirical"),
    "naive_last": (naive_last_dist, "degenerate"),
    "zero": (zero_dist, "degenerate"),
}

DEFAULT_METHOD = "tsb"

#: The POINT forecast each method emits, identical to `seeds/run_carparts_backtest.py`'s
#: `METHODS` table. Kept alongside the distributions for two reasons: the MASE column of
#: the decision-cost leaderboard has to be the SAME number the published leaderboard
#: reports, and the invariant below needs something to check the distribution against.
POINT_BUILDERS: Dict[str, Callable[..., List[float]]] = {
    "tsb": tsb,
    "sba": sba,
    "croston": croston,
    "climatology": lambda tr, h: [float(np.mean(tr)) if len(tr) else 0.0] * h,
    "naive_last": naive_last,
    "zero": lambda tr, h: [0.0] * h,
}

#: Relative tolerance on the documented invariant E[predictive pmf] == the method's own
#: point forecast. `docs/INTERMITTENT_DEMAND.md` states this holds "by construction" -- the
#: compound-Bernoulli mean p*z IS the flat rate the point method emits -- and it is what
#: makes the point and distributional leaderboards comparable rather than merely adjacent.
#:
#: IT DOES NOT ALWAYS HOLD, and this decision layer is where that stops being harmless.
#: When the non-zero order sizes are overdispersed by a hair (sample variance a few parts
#: in 1e16 above the mean), the method-of-moments shape r = m^2/(v - m) evaluates to ~1e16
#: instead of collapsing to the Poisson limit, and `_size_pmf`'s mean-matching search then
#: returns a size law with a mean tens of times too large. On the committed panel this
#: fires at 3 (series, origin) pairs out of 8,022 -- 0.04% -- but a scoring rule only
#: notices it as a slightly worse CRPS whereas a DECISION reads the 0.878 quantile of that
#: broken law and orders 70 units where the right answer is 2. Two such series were worth
#: ~24% of the margin over the toughest baseline before this guard existed.
#:
#: The bug is in `app/ml/intermittent.py`, which this module does not own and must not
#: edit. So the guard here is conservative and loud: any series whose predictive law
#: violates its own stated invariant at any origin is dropped from the evaluation and
#: counted in `panel.n_series_dropped_pmf_invariant`, and a single decision on such a law
#: raises. Silently ordering 70 units is the failure mode this exists to prevent.
PMF_MEAN_RTOL: float = 1e-6


class PredictiveLawError(ValueError):
    """A predictive distribution failed the invariant that makes it usable for a decision."""


def predictive_distribution(
    train: PmfLike, method: str = DEFAULT_METHOD, horizon: int = 1
) -> Tuple[np.ndarray, str]:
    """(pmf, distribution_source) for one training window, invariant-checked.

    The ONLY sanctioned way into this module's decision functions from a raw series. It
    builds the method's predictive law and its point forecast and refuses to return the
    law if the two disagree, because `E[pmf] == point forecast` is the property the demand
    leaderboard is built on and a violated invariant means the pmf's tail is not the tail
    anyone measured. See `PMF_MEAN_RTOL` for the specific upstream defect this catches.
    """
    if method not in DIST_BUILDERS:
        raise ValueError(f"unknown method {method!r}; expected one of {sorted(DIST_BUILDERS)}")
    builder, source = DIST_BUILDERS[method]
    pmf = _normalised(np.asarray(builder(train, max(1, horizon))[0], dtype=float))
    point = float(POINT_BUILDERS[method](train, max(1, horizon))[0])
    mean, _ = pmf_moments(pmf)
    if abs(mean - point) > PMF_MEAN_RTOL * max(1.0, abs(point)):
        raise PredictiveLawError(
            f"method {method!r} produced a predictive law whose mean ({mean:.6g}) does not "
            f"match its own point forecast ({point:.6g}). The compound-Bernoulli lift in "
            "app/ml/intermittent.py is numerically broken on this window; a decision must "
            "not be taken on it. See app/optimization/newsvendor.py::PMF_MEAN_RTOL."
        )
    return pmf, source


def decide_from_pmf(
    pmf: PmfLike,
    costs: NewsvendorCosts,
    method: str = DEFAULT_METHOD,
    distribution_source: str = "parametric",
) -> NewsvendorDecision:
    """Turn one predictive pmf and one cost pair into a described decision."""
    p = _normalised(pmf)
    tau = costs.critical_ratio
    q = order_quantity_from_pmf(p, tau)
    mean, sd = pmf_moments(p)
    expected = expected_cost_from_pmf(p, q, costs.underage_usd, costs.overage_usd)

    comparisons = {
        "order_point_forecast": float(round(mean)),
        "cost_of_ordering_point_forecast": expected_cost_from_pmf(
            p, float(round(mean)), costs.underage_usd, costs.overage_usd
        )["expected_total_usd"],
        "order_normal_approximation": normal_order_quantity(mean, sd, tau),
        "cost_of_normal_approximation": expected_cost_from_pmf(
            p, normal_order_quantity(mean, sd, tau), costs.underage_usd, costs.overage_usd
        )["expected_total_usd"],
        "order_scarf_minmax": scarf_order_quantity(mean, sd, costs.underage_usd, costs.overage_usd),
        "predictive_mean": mean,
        "predictive_sd": sd,
    }

    caveats = [
        "The DEMAND distribution driving this decision is the compound Bernoulli x "
        "zero-truncated NegBin predictive law from app/ml/intermittent.py, fitted to the "
        "Monash car-parts panel -- real intermittent spare-parts demand used as a STAND-IN. "
        "It is not a demand forecast for the electronic components this app sells; no "
        "public per-SKU demand series exists for those (docs/INTERMITTENT_DEMAND.md).",
        "cycle_service_level is P(demand <= q) over ONE review period of "
        f"{costs.review_period_months} month(s) under the predictive law -- a model "
        "probability with a stated exposure window, not a measured achieved service rate. "
        "fill_rate is a fraction of UNITS met from stock over the same window; the two are "
        "different numbers and must not be quoted for each other.",
        "Every dollar figure is per unit at a unit price of "
        f"${costs.unit_price_usd:.2f} and scales linearly in price. tau itself does not "
        "depend on price at all -- both costs are proportional to it, so it cancels.",
        "SINGLE PERIOD. No inventory carries over from the previous period and there is no "
        "fixed cost per order; with either of those the optimal policy is (s, S), not a "
        "newsvendor fractile, and q* here would be an order-up-to level rather than an "
        "order quantity.",
    ]
    if costs.resolution_warning:
        caveats.insert(0, costs.resolution_warning)
    if distribution_source == "empirical":
        caveats.append(
            "EMPIRICAL distribution: the support ends at the largest order ever observed "
            "in the training window, so q* cannot exceed it however large tau gets. The "
            "empirical path does not invent a tail, which is the honest behaviour and also "
            "the reason the parametric path is the default."
        )
    if distribution_source == "degenerate":
        caveats.append(
            "DEGENERATE distribution: this method emits a point forecast with zero spread, "
            "so its 'quantile' is that point for every tau and the cost asymmetry has no "
            "effect on the order. It is on the list because it is on the demand "
            "leaderboard, not because it is a defensible policy."
        )
    return NewsvendorDecision(
        order_quantity=q,
        costs=costs,
        distribution_source=distribution_source,
        method=method,
        expected=expected,
        comparisons=comparisons,
        caveats=caveats,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation -- a policy ships only by beating stated baselines on held-out data
#
# House rule, and the reason this section is longer than the decision itself: an
# absolute cost number means nothing. "$0.04 per SKU-month" is not a result; "$0.04
# per SKU-month, which is 41% less than what the textbook safety-stock rule pays on
# the same forecast, on 2,646 held-out series, with a paired bootstrap CI that
# excludes zero" is a result. Everything below exists to produce the second sentence.
# ─────────────────────────────────────────────────────────────────────────────

#: The Monash car-parts panel, committed. Read-only, always: nothing in `app/` writes to
#: `seeds/data/`. `seeds/monash_loader.py` owns the download and the cache refresh; this is
#: a deliberate read of the same bytes rather than an import, because `app/` does not
#: depend on `seeds/` anywhere else in this codebase and a decision endpoint should not be
#: able to trigger a HuggingFace download.
PANEL_PATH = Path(__file__).resolve().parents[2] / "seeds" / "data" / "car_parts_monthly.npz"

#: The published protocol, taken from `seeds/run_carparts_backtest.py`'s primary config so
#: the newsvendor evaluation runs on the SAME rolling origins as the CRPS/pinball
#: leaderboard it is meant to complete. Origins are [33, 39, 45] on a 51-month panel.
PANEL_HORIZON = 6
PANEL_N_WINDOWS = 3
PANEL_MIN_TRAIN = 33
PANEL_SEASONALITY = 12

#: The fixed safety multiple baseline: order twice the point forecast. A real practitioner
#: rule of thumb, and the most common thing that happens when someone knows the mean
#: understocks but has no distribution to say by how much.
SAFETY_MULTIPLE = 2.0


@lru_cache(maxsize=1)
def load_panel() -> Tuple[Tuple[str, ...], np.ndarray]:
    """(series names, N x T matrix) for the committed Monash car-parts panel.

    Raises FileNotFoundError rather than falling back to anything synthetic. There is no
    substitute panel; a missing file is a deployment fact to surface, not a gap to fill.
    """
    if not PANEL_PATH.is_file():
        raise FileNotFoundError(
            f"Monash car-parts panel not found at {PANEL_PATH}. It is committed to the "
            "repo; regenerate with `cd backend && python -m seeds.monash_loader`."
        )
    blob = np.load(PANEL_PATH, allow_pickle=False)
    names = tuple(str(n) for n in blob["names"])
    values = np.asarray(blob["values"], dtype=float)
    mat = np.ascontiguousarray(values)
    mat.setflags(write=False)
    return names, mat


@dataclass(frozen=True)
class PolicyInput:
    """Everything a stocking policy is allowed to see. Identical for every policy.

    The point of holding this fixed is that the comparison below isolates the DECISION
    RULE. Every policy gets the same forecast, the same predictive distribution, the same
    two moments of it, the same costs and the same critical fractile. Whatever separates
    them is what they do with that, not what they knew.
    """

    train: np.ndarray
    pmf: np.ndarray
    tau: float
    mean: float
    sd: float
    underage_usd: float
    overage_usd: float


PolicyFn = Callable[[PolicyInput], float]


def _policy_newsvendor(ctx: PolicyInput) -> float:
    return order_quantity_from_pmf(ctx.pmf, ctx.tau)


def _policy_point_forecast(ctx: PolicyInput) -> float:
    return float(round(ctx.mean))


def _policy_naive_last(ctx: PolicyInput) -> float:
    return float(max(0.0, round(float(ctx.train[-1])))) if ctx.train.size else 0.0


def _policy_safety_multiple(ctx: PolicyInput) -> float:
    return float(math.ceil(SAFETY_MULTIPLE * ctx.mean))


def _policy_normal_safety_stock(ctx: PolicyInput) -> float:
    return float(round(normal_order_quantity(ctx.mean, ctx.sd, ctx.tau)))


def _policy_scarf(ctx: PolicyInput) -> float:
    return float(round(scarf_order_quantity(ctx.mean, ctx.sd, ctx.underage_usd, ctx.overage_usd)))


def _policy_zero(ctx: PolicyInput) -> float:
    del ctx
    return 0.0


#: The policy under test.
NEWSVENDOR_POLICY = "newsvendor_fractile"

#: name -> (rule, what it is and why it is a fair thing to have to beat).
POLICIES: Dict[str, Tuple[PolicyFn, str]] = {
    NEWSVENDOR_POLICY: (
        _policy_newsvendor,
        "Order the tau-quantile of the predictive distribution, tau = Cu/(Cu+Co). The "
        "policy under test.",
    ),
    "point_forecast": (
        _policy_point_forecast,
        "Order the point forecast, rounded. The predict-then-order default, and wrong "
        "whenever tau != F(mean) -- which under an asymmetric cost is always.",
    ),
    "naive_last": (
        _policy_naive_last,
        "Order what was demanded last period. The oldest planning heuristic there is, and "
        "the point forecast whose distributional lift is on the demand leaderboard.",
    ),
    "safety_multiple_2x": (
        _policy_safety_multiple,
        "Order ceil(2 x point forecast). A fixed safety multiple -- what a planner does "
        "when they know the mean understocks but have no distribution to say by how much.",
    ),
    "normal_safety_stock": (
        _policy_normal_safety_stock,
        "Order mu + z_tau * sigma from the SAME forecast at the SAME service level, "
        "assuming demand is normal. The textbook safety-stock formula and the toughest "
        "baseline here: it already has the cost asymmetry, so the only thing it gets wrong "
        "is the SHAPE of a count law that is 76% zeros. The gap to it is the measured "
        "value of using the distribution correctly rather than approximating it.",
    ),
    "scarf_minmax": (
        _policy_scarf,
        "Scarf (1958) min-max order on the same two moments -- distribution-free, "
        "worst-case optimal over every law with that mean and variance. Pessimistic by "
        "construction, so on non-adversarial demand it should cost more than the fractile.",
    ),
    "order_nothing": (
        _policy_zero,
        "Order zero, every period, forever. The NEGATIVE CONTROL -- and not a straw man: "
        "`zero` wins both point-error leaderboards on this panel (docs/INTERMITTENT_DEMAND"
        ".md), so this is what optimising MASE actually recommends you stock.",
    ),
}

BASELINE_POLICIES: Tuple[str, ...] = tuple(k for k in POLICIES if k != NEWSVENDOR_POLICY)


def paired_bootstrap(diff: np.ndarray, n_boot: int = 5000, seed: int = 0) -> Dict[str, Any]:
    """Paired per-series cost difference with a bootstrap CI, positive => policy is better.

    PAIRED because every policy is scored on the identical series at the identical origins:
    series difficulty is shared and cancels. A comparison of two marginal averages cannot
    do that, and on a panel whose per-series costs span three orders of magnitude it would
    be dominated by a handful of high-volume SKUs.

    Resampling is over SERIES, which is the replication unit `docs/INTERMITTENT_DEMAND.md`
    fixes for this panel -- six months of one SKU are not six draws. Same shape as
    `app/ml/regime_model.py::_paired_brier`, deliberately, so the two read alike.
    """
    d = np.asarray(diff, dtype=float)
    d = d[np.isfinite(d)]
    if d.size == 0:
        return {"n": 0, "mean_difference": None, "ci95_low": None, "ci95_high": None, "significant": False}
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, dtype=float)
    chunk = 500
    for start in range(0, n_boot, chunk):
        size = min(chunk, n_boot - start)
        idx = rng.integers(0, d.size, size=(size, d.size))
        means[start:start + size] = d[idx].mean(axis=1)
    lo, hi = (float(x) for x in np.percentile(means, [2.5, 97.5]))
    tol = 1e-12
    return {
        "n": int(d.size),
        "n_boot": int(n_boot),
        "mean_difference": float(d.mean()),
        "ci95_low": lo,
        "ci95_high": hi,
        "significant": bool(lo > 0),
        "win_rate": float(np.mean(d > tol)),
        "tie_rate": float(np.mean(np.abs(d) <= tol)),
        "loss_rate": float(np.mean(d < -tol)),
    }


def _subsample_index(n: int, max_series: Optional[int]) -> np.ndarray:
    """Deterministic evenly-spaced subsample. Used by tests; never by a published number.

    Evenly spaced rather than the first N, because the panel is ordered and the first N
    series are not representative of it -- taking `mat[:N]` would quietly evaluate a
    different population from the one the headline describes.
    """
    if max_series is None or max_series >= n:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, int(max_series)).astype(int))


def run_panel_evaluation(
    unit_price_usd: float = 1.0,
    review_period_months: int = 1,
    shortage_mode: str = "expedite",
    forecast_method: str = DEFAULT_METHOD,
    horizon: int = PANEL_HORIZON,
    n_windows: int = PANEL_N_WINDOWS,
    min_train: int = PANEL_MIN_TRAIN,
    seasonality: int = PANEL_SEASONALITY,
    n_boot: int = 5000,
    seed: int = 0,
    max_series: Optional[int] = None,
    permute_forecasts_seed: Optional[int] = None,
    panel: Optional[Tuple[Sequence[str], np.ndarray]] = None,
) -> Dict[str, Any]:
    """Score the newsvendor policy against every baseline on held-out car-parts demand.

    PROTOCOL. The same rolling origins as the published demand leaderboard, from the same
    `app.ml.backtest.rolling_origins` -- horizon 6, three origins, training windows
    33/39/45 months. The predictive distribution at each origin is fitted on
    `series[:cut]` only; the order quantity is chosen from it; the cost is paid against
    `series[cut:cut+horizon]`, which the fit never saw. One order quantity per origin (the
    predictive law is flat over the horizon), one cost per period.

    THE PANEL IS THE PUBLISHED BALANCED ONE. Series enter only if the seasonal-naive MASE
    denominator is finite at every origin -- the same 2,646-of-2,674 balance rule the
    leaderboard uses. Newsvendor cost needs no scaling denominator, so all 2,674 series are
    *scoreable* here and dropping 28 of them is a concession rather than a necessity; it is
    made anyway so the decision-cost ranking and the MASE ranking are computed on one
    identical population and can be compared without an asterisk.

    Args:
        unit_price_usd: price per unit. Costs scale linearly in it; tau does not depend on
            it. The default of $1.00 makes every figure read as "per dollar of unit price".
        review_period_months: periods per decision. >1 aggregates demand by exact
            convolution (see `aggregate_pmf`) and lengthens the holding charge; the horizon
            is split into floor(horizon / L) non-overlapping blocks and the remainder is
            dropped.
        forecast_method: which predictive distribution the policy comparison runs on.
        permute_forecasts_seed: PERMUTATION CONTROL. When set, each series is scored
            against another series' predictive distribution. If the real pairing does not
            beat the permuted one, this harness is measuring the shape of the cost function
            rather than any information in the forecast, and none of its numbers mean
            anything. Not a published configuration -- a falsification test for the harness.
        panel: inject (names, matrix) instead of reading the committed npz. Tests only.

    Returns a dict with `costs`, `protocol`, `panel`, `policies`, `paired_vs_newsvendor`,
    `toughest_baseline`, `ship_gate`, `method_leaderboard` and `caveats`.
    """
    if review_period_months < 1:
        raise ValueError(f"review_period_months must be a positive integer, got {review_period_months}")
    if forecast_method not in DIST_BUILDERS:
        raise ValueError(f"unknown forecast_method {forecast_method!r}; expected one of {sorted(DIST_BUILDERS)}")

    costs = newsvendor_costs(
        unit_price_usd=unit_price_usd,
        review_period_months=float(review_period_months),
        shortage_mode=shortage_mode,
    )
    cu, co, tau = costs.underage_usd, costs.overage_usd, costs.critical_ratio

    names_seq, mat = panel if panel is not None else load_panel()
    names = tuple(str(n) for n in names_seq)
    mat = np.asarray(mat, dtype=float)
    rows = _subsample_index(mat.shape[0], max_series)
    cuts = rolling_origins(int(mat.shape[1]), horizon, n_windows, min_train)

    block = int(review_period_months)
    n_blocks = horizon // block
    if n_blocks < 1:
        raise ValueError(f"review_period_months={block} exceeds the horizon of {horizon}")

    method_names = list(DIST_BUILDERS)
    policy_names = list(POLICIES)

    # Pass 1: build every predictive distribution once, keyed (row, cut, method).
    # Memory is bounded: len(rows) x 3 x 6 small float arrays.
    pmfs: Dict[Tuple[int, int, str], np.ndarray] = {}
    points: Dict[Tuple[int, int, str], float] = {}
    balanced: List[int] = []
    n_unbalanced = 0
    n_invariant = 0
    for i in rows:
        series = mat[i]
        undefined_denominator = False
        broken_law = False
        for cut in cuts:
            train = series[:cut]
            if not math.isfinite(mase_denominator(train, seasonality)):
                undefined_denominator = True
            for name in method_names:
                builder = DIST_BUILDERS[name][0]
                pmf = np.asarray(builder(train, horizon)[0], dtype=float)
                point = float(POINT_BUILDERS[name](train, horizon)[0])
                pmfs[(int(i), cut, name)] = pmf
                points[(int(i), cut, name)] = point
                mean, _ = pmf_moments(pmf)
                if abs(mean - point) > PMF_MEAN_RTOL * max(1.0, abs(point)):
                    broken_law = True
        if undefined_denominator:
            n_unbalanced += 1
        elif broken_law:
            n_invariant += 1
            logger.warning(
                "newsvendor: dropping series %s -- predictive law violates E[pmf] == point forecast",
                names[int(i)] if int(i) < len(names) else i,
            )
        else:
            balanced.append(int(i))

    kept = np.asarray(balanced, dtype=int)
    if kept.size == 0:
        raise ValueError("no series survived the balanced-panel rule; nothing to evaluate")

    # Which series' forecast each series is scored against. Identity, unless the
    # permutation control is on.
    source_of = {int(i): int(i) for i in kept}
    if permute_forecasts_seed is not None:
        rng = np.random.default_rng(permute_forecasts_seed)
        shuffled = kept.copy()
        rng.shuffle(shuffled)
        source_of = {int(i): int(j) for i, j in zip(kept, shuffled, strict=True)}

    # Pass 2: pay the costs.
    per_series_cost: Dict[str, List[float]] = {p: [] for p in policy_names}
    per_series_method_cost: Dict[str, List[float]] = {m: [] for m in method_names}
    per_series_mase: Dict[str, List[float]] = {m: [] for m in method_names}
    per_series_order: Dict[str, List[float]] = {p: [] for p in policy_names}

    for i in kept:
        series = mat[i]
        src = source_of[int(i)]
        policy_costs: Dict[str, List[float]] = {p: [] for p in policy_names}
        policy_orders: Dict[str, List[float]] = {p: [] for p in policy_names}
        method_costs: Dict[str, List[float]] = {m: [] for m in method_names}
        method_mase: Dict[str, List[float]] = {m: [] for m in method_names}

        for cut in cuts:
            train = series[:cut]
            test = series[cut:cut + horizon]
            blocks = [float(np.sum(test[b * block:(b + 1) * block])) for b in range(n_blocks)]

            for name in method_names:
                raw = pmfs[(src, cut, name)]
                pmf = aggregate_pmf(raw, block) if block > 1 else _normalised(raw)
                mean, sd = pmf_moments(pmf)
                ctx = PolicyInput(
                    train=train, pmf=pmf, tau=tau, mean=mean, sd=sd, underage_usd=cu, overage_usd=co
                )
                # Every method gets the newsvendor rule, for the decision-cost leaderboard.
                q_nv = _policy_newsvendor(ctx)
                method_costs[name].append(
                    float(np.mean([realized_cost(q_nv, y, cu, co) for y in blocks]))
                )
                # MASE at the MONTHLY level, from the method's OWN point forecaster and
                # the repo's own `mase()` -- not a re-implementation, so this column is
                # directly comparable with the published leaderboard (and a test asserts
                # it reproduces it).
                point = points[(src, cut, name)]
                method_mase[name].append(mase(train, test, [point] * horizon, seasonality=seasonality))

                if name == forecast_method:
                    for pname in policy_names:
                        q = POLICIES[pname][0](ctx)
                        policy_orders[pname].append(q)
                        policy_costs[pname].append(
                            float(np.mean([realized_cost(q, y, cu, co) for y in blocks]))
                        )

        for p in policy_names:
            per_series_cost[p].append(float(np.mean(policy_costs[p])))
            per_series_order[p].append(float(np.mean(policy_orders[p])))
        for m in method_names:
            per_series_method_cost[m].append(float(np.mean(method_costs[m])))
            per_series_mase[m].append(float(np.mean(method_mase[m])))

    cost_arr = {p: np.asarray(v, dtype=float) for p, v in per_series_cost.items()}
    nv = cost_arr[NEWSVENDOR_POLICY]

    policies_out: Dict[str, Any] = {}
    paired_out: Dict[str, Any] = {}
    for p in policy_names:
        policies_out[p] = {
            "description": POLICIES[p][1],
            "mean_cost_usd_per_sku_period": float(np.mean(cost_arr[p])),
            "median_cost_usd_per_sku_period": float(np.median(cost_arr[p])),
            "mean_order_quantity": float(np.mean(per_series_order[p])),
            "n_series": int(cost_arr[p].size),
        }
        if p == NEWSVENDOR_POLICY:
            continue
        boot = paired_bootstrap(cost_arr[p] - nv, n_boot=n_boot, seed=seed)
        base_mean = float(np.mean(cost_arr[p]))
        boot["baseline_mean_cost"] = base_mean
        boot["policy_mean_cost"] = float(np.mean(nv))
        boot["pct_cost_reduction"] = float(100.0 * (base_mean - float(np.mean(nv))) / base_mean) if base_mean > 0 else None
        paired_out[p] = boot

    beaten = {p: bool(np.mean(cost_arr[p]) > np.mean(nv)) for p in BASELINE_POLICIES}
    toughest = min(BASELINE_POLICIES, key=lambda p: float(np.mean(cost_arr[p])))

    method_cost_mean = {m: float(np.mean(per_series_method_cost[m])) for m in method_names}
    method_mase_mean = {m: float(np.nanmean(per_series_mase[m])) for m in method_names}
    order_by_cost = sorted(method_names, key=lambda m: method_cost_mean[m])
    order_by_mase = sorted(method_names, key=lambda m: method_mase_mean[m])

    result: Dict[str, Any] = {
        "costs": costs.as_dict(),
        "protocol": {
            "panel": "Monash car parts (monash_car_parts_with_missing_values), CC-BY 4.0",
            "split": "rolling origin via app.ml.backtest.rolling_origins -- the same "
                     "function the published demand leaderboard and the macro backtest use",
            "horizon_months": horizon,
            "n_origins": n_windows,
            "train_sizes": [int(c) for c in cuts],
            "review_period_months": block,
            "blocks_per_origin": n_blocks,
            "seasonality": seasonality,
            "replication_unit": "series",
            "balance_rule": "series kept only if the seasonal-naive MASE denominator is "
                            "finite at every origin -- the published leaderboard's rule, "
                            "adopted so decision cost and MASE rank one identical panel",
            "forecast_method": forecast_method,
            "distribution_source": DIST_BUILDERS[forecast_method][1],
            "permutation_control": permute_forecasts_seed is not None,
        },
        "panel": {
            "n_series_available": int(mat.shape[0]),
            "n_series_considered": int(rows.size),
            "n_series_scored": int(kept.size),
            "n_series_dropped_unbalanced": n_unbalanced,
            "n_series_dropped_pmf_invariant": n_invariant,
            "n_decisions": int(kept.size * len(cuts) * n_blocks),
        },
        "policies": policies_out,
        "paired_vs_newsvendor": paired_out,
        "baselines_beaten": beaten,
        "toughest_baseline": toughest,
        "paired_vs_toughest_baseline": paired_out.get(toughest, {}),
        "method_leaderboard": {
            "decision_cost_usd_per_sku_period": method_cost_mean,
            "mase_mean": method_mase_mean,
            "order_by_decision_cost": order_by_cost,
            "order_by_mase": order_by_mase,
            "winner_changed": order_by_cost[0] != order_by_mase[0],
            "note": "Every method is given the SAME newsvendor rule, so this ranks "
                    "forecasts by the decision they produce rather than by point error. "
                    "MASE here is recomputed on this run from each method's own point "
                    "forecast (the mean of its predictive law, which equals its point twin "
                    "by construction), so both columns come from one pass over one panel.",
        },
        "caveats": _evaluation_caveats(costs, forecast_method, block, permute_forecasts_seed),
    }
    result["ship_gate"] = evaluate_newsvendor_ship_gate(result)
    return result


def _evaluation_caveats(
    costs: NewsvendorCosts, forecast_method: str, block: int, permute_seed: Optional[int]
) -> List[str]:
    out = [
        "The demand is REAL and the parts are NOT ours. Monash car parts is genuine "
        "intermittent spare-parts demand used as a STAND-IN for electronic components, "
        "because no public per-SKU demand series exists for the components this app sells. "
        "The statistical object is the same -- long runs of zeros punctuated by small "
        "integer orders -- and it is labelled a stand-in everywhere it is used.",
        "The panel carries NO PRICES, so every dollar figure is per unit at a unit price "
        f"of ${costs.unit_price_usd:.2f} and scales linearly. The critical fractile does "
        "not depend on price at all, which is what makes this evaluation possible without "
        "inventing one.",
        "SINGLE-PERIOD, ZERO STARTING INVENTORY. Each decision is scored as if the shelf "
        "were empty at the start of the period. Real replenishment carries stock forward, "
        "which every policy here would benefit from; the comparison between policies is "
        "unaffected in direction but the absolute costs are upper bounds.",
        f"Cu = {costs.shortage_multiple} x unit price ({costs.shortage_mode}) EXCLUDES the "
        "fixed expedite consignment charge, which is per shipment and cannot be made "
        "per-unit without a further assumption. That understates Cu, understates tau and "
        "understates q*: the measured saving is a lower bound.",
        "One order quantity per origin. The predictive law is flat over the horizon, so "
        "there is no re-optimisation as the horizon unfolds and no demand signal is used "
        "after the origin -- which is the correct handling of held-out data and also less "
        "than a real planner would do.",
    ]
    if block > 1:
        out.append(
            f"review_period_months={block} aggregates demand by convolving the one-month "
            "predictive law with itself, which is exact only under the model's i.i.d. "
            "assumption across periods. Real intermittent demand plausibly clusters; if it "
            "does, this understates the variance of the period total and therefore q*."
        )
    if DIST_BUILDERS[forecast_method][1] == "degenerate":
        out.append(
            f"forecast_method={forecast_method!r} is a DEGENERATE distribution with zero "
            "spread, so the newsvendor rule collapses to its point forecast and the cost "
            "asymmetry has no effect. Any comparison run this way is uninformative."
        )
    if permute_seed is not None:
        out.insert(
            0,
            "PERMUTATION CONTROL RUN -- each series was scored against ANOTHER series' "
            "forecast. These numbers are a falsification check on the harness and must "
            "never be quoted as a result.",
        )
    return out


#: The bar the newsvendor policy has to clear to be published, mirroring the lead-time and
#: regime ship gates rather than inventing a third convention. Two conditions:
#:   1. beat EVERY baseline on mean per-series held-out cost;
#:   2. beat the TOUGHEST one by a margin whose paired bootstrap 95% CI excludes zero.
#: Condition 2 matters because `normal_safety_stock` is a genuinely strong baseline: it has
#: the same forecast and the same service level and only the wrong distributional shape.
#: Beating it on a point estimate while the across-series spread swamps the difference is
#: not evidence, and this project does not ship policies on point estimates alone.
NEWSVENDOR_SHIP_GATE_POLICY = "beats_all_baselines_significantly"


def evaluate_newsvendor_ship_gate(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Decide whether an evaluation result is fit to publish. FAILS CLOSED.

    Missing evidence is a failure, not a pass: a run that cannot show its baselines has
    not earned a headline. A permutation-control run can never pass, whatever it scores.
    """
    common: Dict[str, Any] = {"policy": NEWSVENDOR_SHIP_GATE_POLICY}
    if not result:
        return {**common, "passed": False, "reason": "no evaluation result to gate"}
    if result.get("protocol", {}).get("permutation_control"):
        return {**common, "passed": False, "reason": "permutation-control run; not a publishable result"}

    beaten = result.get("baselines_beaten") or {}
    toughest = result.get("toughest_baseline")
    paired = result.get("paired_vs_toughest_baseline") or {}
    common.update({"toughest_baseline": toughest, "baselines_beaten": dict(beaten), "paired": dict(paired)})

    if not beaten:
        return {**common, "passed": False, "reason": "no baseline comparison present"}
    lost_to = sorted(name for name, won in beaten.items() if not won)
    if lost_to:
        return {**common, "passed": False, "reason": f"did not beat every baseline; lost or tied to {lost_to}"}
    if not paired:
        return {**common, "passed": False, "reason": f"no paired bootstrap against the toughest baseline ({toughest})"}
    if not paired.get("significant"):
        return {
            **common,
            "passed": False,
            "reason": (
                f"margin over {toughest} is not significant: mean difference "
                f"{paired.get('mean_difference')} with 95% CI "
                f"[{paired.get('ci95_low')}, {paired.get('ci95_high')}]"
            ),
        }
    return {
        **common,
        "passed": True,
        "reason": (
            f"beats all {len(beaten)} baselines; margin over {toughest} is "
            f"{paired.get('mean_difference'):.6f} USD/SKU-period with 95% CI "
            f"[{paired.get('ci95_low'):.6f}, {paired.get('ci95_high'):.6f}], excluding zero"
        ),
    }
