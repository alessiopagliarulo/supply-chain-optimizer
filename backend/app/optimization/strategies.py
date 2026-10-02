"""
Four multi-objective weight profiles.

Weighted sum scalarization over normalized cost/time/carbon objectives.
See spec §5.2 — Marler & Arora (2004), Ghodsypour & O'Brien (1998).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class StrategyWeights:
    id: str
    label: str
    description: str
    w_cost: float
    w_time: float
    w_carbon: float
    basis: str  # citation / industry rationale
    us_only_sourcing: bool = False  # if True, filter to domestic (US) distributors only
    # Scales transport-cost penalty in the sourcing MILP objective.
    # Higher values push the solver toward nearby distributors.
    # cheapest=1.0 (landed cost), fastest=1.0 (real transport cost; the time
    # lever is the consolidation bonus below), greenest=2.5 (tonne-mile
    # minimisation), balanced=1.5 (moderate).
    transport_penalty_scale: float = 1.0
    # USD bonus subtracted per distributor used — rewards consolidation.
    # Positive = fewer stops; set lower for strategies that accept more stops.
    consolidation_bonus_usd: float = 1.0

    @property
    def as_tuple(self) -> tuple:
        return (self.w_cost, self.w_time, self.w_carbon)


STRATEGIES: List[StrategyWeights] = [
    StrategyWeights(
        id="cheapest",
        label="Lowest Cost",
        description="Pure procurement optimization — minimize total landed cost",
        w_cost=1.00, w_time=0.00, w_carbon=0.00,
        basis="Weber (1991), Vendor selection criteria and methods",
        us_only_sourcing=False,
        transport_penalty_scale=1.0,   # full landed cost (components + freight)
        consolidation_bonus_usd=0.5,   # weak consolidation incentive — split is OK if cheaper
    ),
    StrategyWeights(
        id="fastest",
        label="Fastest Delivery",
        description="JIT/lean procurement — minimize lead time at reasonable cost",
        w_cost=0.15, w_time=0.80, w_carbon=0.05,
        basis="Toyota Production System literature; JIT practice",
        us_only_sourcing=True,
        # WHY THESE TWO NUMBERS (changed 2026-08-16 — measured, not guessed):
        # The Stage 1 MILP minimizes landed COST only; it has no lead-time term.
        # A time-preferring strategy therefore has exactly two levers over it,
        # and both are proxies:
        #   transport_penalty_scale → distance (transit days rise with km)
        #   consolidation_bonus_usd → supplier count (each extra pickup adds a
        #                             handling window AND at least one more
        #                             transit day, because leg transit is
        #                             ceil(km / 800) — a 50 km leg still costs a
        #                             full day).
        # The previous values were 0.0 / $3.00, i.e. essentially no awareness of
        # either. "Fastest Delivery" then just minimised component price among
        # domestic offers and routinely produced the LONGEST tour of the four
        # strategies — measured on real BOMs it came 4th of 4 on ETA at 12 and 40
        # lines (9.5 d vs 5.5 d for "greenest") with 13 pickup stops.
        # 1.0 charges real, unscaled transport cost (so the plan is not distance
        # blind) and $150 — 2x the $75 LTL base fee — prices the time cost of
        # opening one more supplier. Swept at 2/5/12/25/40 BOM lines built from
        # REAL catalogue components and their real offers, but assembled for the
        # sweep: BOM_CATALOG's ten named BOMs are all 4-line, so every size other
        # than that was composed for this test rather than drawn from the
        # catalogue. Result: fastest has the lowest ETA of all four strategies at
        # every size, while remaining a DISTINCT plan wherever any strategies
        # diverge at all.
        # Raising the distance penalty instead (>=1.5) also makes it fastest but
        # collapses it onto "greenest" — same lever, same answer.
        # The clean fix is a lead-time term in the Stage 1 objective
        # (app/optimization/sourcing.py); these are calibrated proxies until then.
        transport_penalty_scale=1.0,
        consolidation_bonus_usd=150.0,
    ),
    StrategyWeights(
        id="greenest",
        label="Lowest Carbon",
        description="ESG-compliant — eliminates international air freight (~4.5× the CO2e per kg-km of domestic truck)",
        w_cost=0.25, w_time=0.05, w_carbon=0.70,
        basis="CDP Supply Chain Disclosure framework; GLEC Framework v3.2 air-freight factor",
        # Air freight emits ~4.5x the CO2e per kg-km of domestic truck, NOT the
        # 30-40x this once claimed. Derived from the project's own two cited
        # factors, both put on a kg-CO2e per kg-of-freight per km basis:
        #   truck  161.8 g per US SHORT ton-mile (EPA SmartWay Technical
        #          Documentation 2013, via EDF Green Freight Handbook 2014 p.11)
        #          = 161.8 / (907.18474 x 1.60934) g = 0.000110824 kg/kg-km
        #   air    0.5 kg per METRIC tonne-km (GLEC Framework v3.2 long-haul
        #          dedicated-freighter tank-to-wheel default, 503 g CO2e/tonne-km)
        #          = 0.000500000 kg/kg-km
        #   ratio  0.0005 / 0.000110824 = 4.51x
        # The 30-40x figure is a real number from the literature, but it compares
        # air freight to OCEAN, not to road. Substituting one for the other
        # overstated the carbon case for US-only sourcing by six to eight times.
        # 4.51x is a LOWER BOUND: the air factor is the optimistic end of the
        # published range (GLEC well-to-wheel 608, DEFRA UK 2023 long-haul
        # CO2-only 643, DEFRA with radiative forcing 1,099 g/tonne-km; belly-hold
        # and short-haul run 2-3x higher). See solve.CO2_AIR_KG_PER_KG_KM.
        # (Until 2026-09-03 the ratio was published as 4.97x, computed while
        # costs.co2_kg divided weight by a metric 1000 instead of a short ton.)
        us_only_sourcing=True,
        transport_penalty_scale=2.5,   # prefer nearby domestic distributors to cut tonne-miles; unlike fastest which picks cheapest regardless of distance
        consolidation_bonus_usd=2.5,   # strong consolidation: fewer truck legs = lower CO2
    ),
    StrategyWeights(
        id="balanced",
        label="Balanced",
        description="Balanced weighting across cost/time/carbon — avoids international air freight CO2 penalty",
        w_cost=0.40, w_time=0.35, w_carbon=0.25,
        basis="Ghodsypour & O'Brien (1998), Int'l J. Production Economics 56-57",
        # Domestic-only: the air-freight CO2e penalty (~4.5x per kg-km, see above)
        # outweighs component price savings in the weighted objective.
        us_only_sourcing=True,
        transport_penalty_scale=1.5,   # moderate distance penalty: balance cost vs tonne-miles
        consolidation_bonus_usd=2.0,   # moderate consolidation incentive
    ),
]


def get_strategy(strategy_id: str) -> StrategyWeights:
    for s in STRATEGIES:
        if s.id == strategy_id:
            return s
    raise KeyError(f"Unknown strategy id: {strategy_id}")


def normalize_objectives(
    raw_values: List[dict],
) -> List[dict]:
    """
    Min-max normalize each objective across alternatives to [0, 1].

    Input: list of dicts with keys 'cost', 'time', 'carbon' (raw values).
    Output: same list but with 'cost_n', 'time_n', 'carbon_n' added in [0,1].
    If all values for an objective are equal, normalized value is 0.
    """
    def _minmax(key: str) -> tuple:
        vals = [v[key] for v in raw_values]
        return min(vals), max(vals)

    cmin, cmax = _minmax("cost")
    tmin, tmax = _minmax("time")
    kmin, kmax = _minmax("carbon")

    def _norm(v: float, lo: float, hi: float) -> float:
        if hi == lo:
            return 0.0
        return (v - lo) / (hi - lo)

    out = []
    for v in raw_values:
        out.append({
            **v,
            "cost_n": _norm(v["cost"], cmin, cmax),
            "time_n": _norm(v["time"], tmin, tmax),
            "carbon_n": _norm(v["carbon"], kmin, kmax),
        })
    return out


def weighted_objective(
    normalized: dict, weights: StrategyWeights,
) -> float:
    """Apply strategy weights to a normalized objective dict."""
    return (
        weights.w_cost * normalized["cost_n"]
        + weights.w_time * normalized["time_n"]
        + weights.w_carbon * normalized["carbon_n"]
    )
