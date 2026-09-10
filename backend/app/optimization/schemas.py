"""
Pydantic response models for the optimization pipeline.

Additive to the existing RouteAlternative shape — the frontend only
reads new fields when present.
"""
from __future__ import annotations

from typing import Dict, List, Optional
from pydantic import BaseModel


class BomLine(BaseModel):
    component_id: int
    mpn: str
    quantity: int


class OfferRef(BaseModel):
    component_id: int
    distributor_id: int
    price_usd: float
    stock: int
    moq: int


class SourcingAssignment(BaseModel):
    component_id: int
    mpn: str
    distributor_id: int
    distributor_name: str
    quantity: int
    unit_price_usd: float
    line_total_usd: float


class RouteStop(BaseModel):
    order: int
    distributor_id: int
    distributor_name: str
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    lat: float
    lng: float
    components: List[str]
    distance_km: float
    leg_cost_usd: float
    leg_co2e_kg: float
    # Freight ACTUALLY ABOARD while this leg is driven, in kg. A pickup tour
    # leaves the depot empty and accrues weight at each stop, so the outbound leg
    # is 0.0 and only the return leg carries the whole order; `leg_cost_usd` and
    # `leg_co2e_kg` are derived from THIS number, not from the order total. It is
    # published so the accrual is checkable from the response instead of having
    # to be taken on trust — the two used to disagree silently.
    leg_carried_kg: float = 0.0


class CostBreakdown(BaseModel):
    component_cost: float
    transport_cost: float
    holding_cost: float
    total: float


class StrategyMath(BaseModel):
    weights: Dict[str, float]               # {cost, time, carbon}
    raw_objective_values: Dict[str, float]  # {cost, time, carbon}
    normalized_objective_values: Dict[str, float]
    weighted_total: float
    citations: List[str]


class CrossDockInfo(BaseModel):
    """Cross-dock consolidation read-out.

    INVARIANT (locked by tests/test_optimizer_defects.py): when ``applied`` is
    True the alternative's ``total_transport_cost_usd`` EQUALS
    ``consolidated_cost_usd`` and ``savings_vs_direct_pct`` is exactly
    100*(1 - consolidated/direct). When ``applied`` is False,
    ``savings_vs_direct_pct`` is 0.0 — a saving that is not banked is not
    reported as a saving. ``candidate_cost_savings_pct`` carries the
    "what it would have been" number in that case.

    ``savings_vs_direct_pct`` is SIGNED. Hubs are chosen on the strategy's
    weighted objective, so a time- or carbon-weighted strategy can pick a hub
    that costs more and buys speed or tonne-miles with the difference; the value
    is then negative and ``rationale`` says so. It stays honest either way
    because the headline transport cost moves with it.
    """
    enabled: bool
    applied: bool = False          # the consolidated cost is what the plan is charged
    hub_id: Optional[int] = None
    hub_name: Optional[str] = None
    hub_city: Optional[str] = None
    hub_state: Optional[str] = None
    hub_lat: Optional[float] = None
    hub_lng: Optional[float] = None
    savings_vs_direct_pct: float = 0.0        # realized transport-cost saving
    candidate_cost_savings_pct: float = 0.0   # what the best hub would have saved
    objective_savings_pct: float = 0.0        # improvement on the weighted objective
    direct_cost_usd: float = 0.0
    consolidated_cost_usd: float = 0.0
    consolidated_co2e_kg: float = 0.0
    consolidated_eta_days: float = 0.0
    consolidated_distance_km: float = 0.0
    rationale: str = ""


class SupplyRiskInfo(BaseModel):
    """ML factory-lead-time read-out for a sourcing plan.

    This is the *only* place the lead-time model touches the optimizer output,
    and it is deliberately separate from ``eta_p50``. The model predicts the
    FACTORY (replenishment) lead time a distributor publishes for a part; the
    route ETA is handling + ground transit for units that ship from stock. The
    sourcing MILP enforces ``ordered_qty <= offer.stock`` on every assignment, so
    the shipped plan really does ship from stock and its ETA really is
    route-derived. Overwriting the route ETA with the factory lead time — which
    is what this code did before 2026-08-15, behind a gate that could never fire
    — conflated two different quantities.

    ``risk_adjusted_eta_days`` is the conservative read: for lines where the plan
    takes 100% of a distributor's reported shelf (zero buffer), a stale inventory
    snapshot means the balance would come off the factory lead time instead.
    """
    model_available: bool
    model_name: Optional[str] = None
    model_source: Optional[str] = None        # mlflow_registry | local_joblib | none
    lines_scored: int = 0                     # BOM lines the model could price
    lines_declined: int = 0                   # lines outside the trained category vocabulary
    declined_reason: Optional[str] = None
    max_factory_lead_time_days: Optional[float] = None
    driver_mpn: Optional[str] = None          # the longest-lead part in the plan
    zero_buffer_lines: int = 0                # lines that consume a distributor's whole shelf
    route_eta_days: float = 0.0               # handling + transit, ships-from-stock
    risk_adjusted_eta_days: float = 0.0       # route_eta + factory LT on zero-buffer lines
    rationale: str = ""


class MonteCarloAssumptions(BaseModel):
    """The ETA simulation's parameters, published rather than buried.

    ``calibrated`` is False and stays False until someone fits these to observed
    shipment data. They were four literals inside a function body while the UI
    presented the resulting band as "Monte Carlo simulation (1,000 scenarios)",
    which reads as an empirical service-level distribution. It is not one — it is
    a seeded sensitivity range around the deterministic route ETA.
    """
    calibrated: bool = False
    seed: int = 0
    transit_multiplier_mean: float = 0.0
    transit_multiplier_sigma: float = 0.0
    disruption_delay_days: List[float] = []
    disruption_weights: List[float] = []
    caveat: str = ""


class OutlierDropLog(BaseModel):
    component_id: int
    mpn: str
    dropped_distributor_id: int
    dropped_price_usd: float
    median_price_usd: float
    reason: str


class RoutingSolverInfo(BaseModel):
    """How the Stage-2 pickup tour in `route` was actually solved.

    ``proven_optimal`` is True ONLY when ``method == "exact_enumeration"`` — i.e.
    every distinct tour was evaluated on the integer-metre haversine matrix and
    the cheapest returned. GUIDED_LOCAL_SEARCH is a metaheuristic: it returns a
    good local optimum and no certificate, so it reports False. Nothing on screen
    may call a tour optimal unless this field says so.

    ``tours_enumerated`` is the exhaustive path's own work counter (n!/2 after
    reversal symmetry) and is 0 on the metaheuristic path.
    ``time_limit_seconds`` is the metaheuristic's budget — which it always spends
    in full, because GUIDED_LOCAL_SEARCH has no convergence criterion — and is
    null on the exact path, which has no budget to spend.
    """
    method: str                                  # exact_enumeration | guided_local_search
    proven_optimal: bool                         #   | greedy_nearest_neighbour | no_stops
    stop_count: int                              # domestic truck-tour stops only
    tours_enumerated: int = 0
    time_limit_seconds: Optional[float] = None
    note: str = ""


class RouteAlternative(BaseModel):
    id: str
    label: str
    description: str
    route: List[RouteStop]
    sourcing: List[SourcingAssignment]
    total_cost_usd: float
    total_transport_cost_usd: float
    total_component_cost_usd: float
    total_co2e_kg: float
    total_distance_km: float
    base_eta_days: float
    eta_p10: float
    eta_p50: float
    eta_p90: float
    # A DOWN-SAMPLED view of the simulation, not the simulation itself. See
    # monte_carlo_n_simulations / monte_carlo_sample_kind below — these two make
    # the list self-describing so nothing downstream has to guess (it used to be
    # the 200 SMALLEST of 1000 draws while being labelled "1000 simulations",
    # which put p50 and p90 outside the range of the points plotted).
    monte_carlo_samples: List[float]
    monte_carlo_n_simulations: int = 0
    monte_carlo_sample_kind: str = ""
    monte_carlo_seed: int = 0
    monte_carlo_assumptions: Optional[MonteCarloAssumptions] = None
    stop_count: int
    international_stops: int
    cost_rank: int = 0
    speed_rank: int = 0
    carbon_rank: int = 0
    distance_rank: int = 0
    # New fields (optional — frontend reads if present)
    cost_breakdown: Optional[CostBreakdown] = None
    strategy_math: Optional[StrategyMath] = None
    cross_dock: Optional[CrossDockInfo] = None
    supply_risk: Optional[SupplyRiskInfo] = None
    routing_solver: Optional[RoutingSolverInfo] = None
    # ── Where the headline transport numbers come from ───────────────────────
    # "direct_pickup_tour"  → totals are the sum of the legs in `route`.
    # "cross_dock_consolidated" → cross-dock cleared its threshold and IS applied:
    #   total_transport_cost_usd / total_co2e_kg / total_distance_km / base_eta_days
    #   describe the hub-routed plan, while `route` still lists the
    #   pre-consolidation pickup legs (they are what a map can draw). The
    #   route_leg_* fields below carry the totals of those displayed legs so the
    #   difference is explicit instead of hidden.
    transport_cost_basis: str = "direct_pickup_tour"
    route_legs_note: str = ""
    route_leg_cost_usd: float = 0.0
    route_leg_co2e_kg: float = 0.0
    route_leg_distance_km: float = 0.0


class StrategyDivergence(BaseModel):
    """How many genuinely different plans the 4 strategies produced.

    Grouping is by the SOURCING ASSIGNMENT SET — the (component_id,
    distributor_id, quantity) triples — not by cost, which can collide by
    accident. When several strategies land on the same assignment set they are
    the same plan, and the ranks they are given reflect that (competition
    ranking: equal values share a rank) instead of being separated by list order.
    """
    total_strategies: int
    distinct_plans: int
    identical_groups: List[List[str]] = []   # groups of ≥2 strategy ids sharing a plan
    all_identical: bool = False
    note: str = ""


class MultiRouteResponse(BaseModel):
    alternatives: List[RouteAlternative]
    recommended_id: str
    outlier_drops: List[OutlierDropLog] = []
    strategy_divergence: Optional[StrategyDivergence] = None
