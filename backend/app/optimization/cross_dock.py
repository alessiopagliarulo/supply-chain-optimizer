"""
Cross-dock consolidation analysis.

For each candidate hub, compute:
  - N LTL legs (distributor → hub)
  - 1 consolidated leg (hub → depot, TL if ≥10,000 lbs)
  - Hub handling fee + dwell time
Pick the hub that minimizes the weighted objective — but only if it
beats direct pickup by ≥5% (the improvement threshold avoids pointless
hub trips when gains are marginal).

METHOD: exhaustive enumeration over a FIXED set of 10 candidate hubs
(``freight_hubs.FREIGHT_HUBS``). Every hub is scored, the argmin of the
strategy's weighted objective wins, and it is accepted only if it clears
the 5% threshold above. Because the candidate set is fixed and tiny, that
enumeration is EXACT — it is the global optimum over the modelled hub
set, found without any heuristic, in microseconds.

What it is NOT: there is no Lagrangian multiplier, no relaxation of any
constraint, and no capacity constraint anywhere in this module. Hubs are
modelled as uncapacitated and always available, so there is nothing here
to relax. An earlier version of this docstring claimed "Lagrangian
relaxation of the Capacitated Facility Location Problem (Daskin 2013,
Ch. 4)"; that was never true of this code.

NOT BUILT: if the hub set ever grew past what enumeration can chew
(hundreds of candidates), or if hubs gained per-hub throughput capacity,
this WOULD become a Capacitated Facility Location Problem and would want
the Lagrangian-relaxation / branch-and-bound treatment in Daskin,
*Network and Discrete Location* (2013), Ch. 4. That is a future direction,
not a description of this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional

from app.optimization.costs import (
    HANDLING_DAYS_BY_TIER, HUB_DWELL_DAYS, HUB_HANDLING_FEE_USD,
    co2_kg, haversine_km, km_to_miles, leg_lead_time_days, transit_days,
    transport_cost_usd, TL_RATE_USD_PER_MILE, TL_THRESHOLD_KG,
)
from app.optimization.freight_hubs import FREIGHT_HUBS, FreightHub
from app.optimization.routing import GeoPoint, RoutingNode
from app.optimization.strategies import StrategyWeights


CROSS_DOCK_IMPROVEMENT_THRESHOLD = 0.95  # hub must beat direct by ≥ 5%


@dataclass(frozen=True)
class DistributorShipment:
    distributor_id: int
    distributor_name: str
    lat: float
    lng: float
    weight_kg: float
    distributor_tier: str  # 'major'|'mid'|'broker'


@dataclass(frozen=True)
class RouteMetrics:
    cost_usd: float
    lead_time_days: float
    co2_kg: float
    # Total transported distance behind cost_usd/co2_kg. Defaulted so every
    # existing positional construction keeps working; it exists so a caller can
    # report a distance that is consistent with the carbon derived from it
    # instead of reporting 0.0 next to a non-zero CO2 figure.
    distance_km: float = 0.0

    def plus_parallel(self, other: "Optional[RouteMetrics]") -> "RouteMetrics":
        """Fold in a shipment stream that runs in PARALLEL with this one.

        Costs, carbon and distance add; lead time is the max, because the two
        streams move at the same time (a domestic truck tour and an
        international air consignment do not queue behind each other).
        """
        if other is None:
            return self
        return RouteMetrics(
            cost_usd=self.cost_usd + other.cost_usd,
            lead_time_days=max(self.lead_time_days, other.lead_time_days),
            co2_kg=self.co2_kg + other.co2_kg,
            distance_km=self.distance_km + other.distance_km,
        )


@dataclass(frozen=True)
class CrossDockDecision:
    enabled: bool
    hub: Optional[FreightHub]
    direct_metrics: RouteMetrics
    consolidated_metrics: Optional[RouteMetrics]
    # SIGNED transport-cost delta that is ACTUALLY TAKEN by this decision, in
    # percent of ``direct_metrics.cost_usd``. It is 0.0 whenever ``enabled`` is
    # False — a saving nobody banks is not a saving. When ``enabled`` is True the
    # caller must charge ``consolidated_metrics.cost_usd``, and this number is
    # exactly 100 * (1 - consolidated/direct).
    #
    # It can be NEGATIVE: hubs are chosen on the strategy's weighted objective, so
    # a time- or carbon-weighted strategy may buy speed or tonne-miles at a higher
    # transport cost. The number stays honest because the headline cost moves with
    # it in both directions; ``rationale`` states which case applies.
    savings_vs_direct_pct: float
    rationale: str
    # The cost saving the best hub WOULD deliver, reported whether or not the
    # decision took it (so a sub-threshold near-miss is still visible).
    candidate_cost_savings_pct: float = 0.0
    # The improvement on the strategy's weighted objective — this, not the cost
    # saving, is the criterion the 5% accept/reject threshold is applied to.
    # Reported separately so the two are never confused for one another again.
    objective_savings_pct: float = 0.0


def _weighted_objective(metrics: RouteMetrics, weights: StrategyWeights) -> float:
    """
    Single-alternative weighted objective (no normalization — used only for
    direct-vs-consolidated comparison within one strategy).

    The 100.0 and 10.0 factors are ad-hoc unit bridges that put days and kg
    of CO2 on a roughly dollar-like scale so the weights are not swamped by
    raw magnitude. They are NOT derived from anything and are not the
    min-max normalization used to rank the four finished alternatives (see
    ``strategies.normalize_objectives``). They are legitimate only because
    both sides of the direct-vs-hub comparison pass through the same
    transform, which leaves the argmin within one strategy unaffected.
    """
    return (
        weights.w_cost * metrics.cost_usd
        + weights.w_time * metrics.lead_time_days * 100.0  # hours-worth scale
        + weights.w_carbon * metrics.co2_kg * 10.0
    )


def evaluate_hub(
    hub: FreightHub,
    depot: GeoPoint,
    shipments: List[DistributorShipment],
    parallel: Optional[RouteMetrics] = None,
) -> RouteMetrics:
    """
    Compute cost/time/CO2 for consolidating all shipments at this hub.

    N LTL legs distributor → hub, then 1 consolidated leg hub → depot.

    ``parallel`` is a shipment stream this hub cannot consolidate and that the
    direct baseline also carries — in practice the international air-freight
    consignments. It is folded into the result so the hub plan and the direct
    plan are compared on the SAME scope. Leaving it out (what this module did
    before) compared a domestic-only hub plan against a direct plan that also
    paid for transpacific air freight, and reported the difference as a
    consolidation saving.
    """
    total_cost = HUB_HANDLING_FEE_USD
    total_co2 = 0.0
    total_distance = 0.0
    max_leg_time = 0.0
    total_weight = 0.0

    for s in shipments:
        d_km = haversine_km(s.lat, s.lng, hub.latitude, hub.longitude)
        total_cost += transport_cost_usd(d_km, s.weight_kg)
        total_co2 += co2_kg(d_km, s.weight_kg)
        total_distance += d_km
        leg_time = leg_lead_time_days(d_km, s.distributor_tier)
        if leg_time > max_leg_time:
            max_leg_time = leg_time
        total_weight += s.weight_kg

    # Consolidated hub → depot leg
    d_hub_depot_km = haversine_km(hub.latitude, hub.longitude, depot.lat, depot.lng)
    total_cost += transport_cost_usd(d_hub_depot_km, total_weight)
    total_co2 += co2_kg(d_hub_depot_km, total_weight)
    total_distance += d_hub_depot_km
    consolidated_leg_time = transit_days(d_hub_depot_km)

    total_time = max_leg_time + HUB_DWELL_DAYS + consolidated_leg_time

    return RouteMetrics(
        cost_usd=total_cost, lead_time_days=total_time,
        co2_kg=total_co2, distance_km=total_distance,
    ).plus_parallel(parallel)


@dataclass(frozen=True)
class PickupLeg:
    """One leg of a depot -> d1 -> ... -> dn -> depot pickup tour.

    ``carried_kg`` is what is ACTUALLY ABOARD while this leg is driven, which is
    the whole point of this type existing: the truck leaves the depot empty and
    only the return leg is fully laden.
    """
    # Destination of the leg. ``None`` marks the final return-to-depot leg.
    node: Optional[RoutingNode]
    distance_km: float
    carried_kg: float
    cost_usd: float
    co2_kg: float
    transit_days: int


def pickup_tour_legs(
    depot: GeoPoint,
    ordered_nodes: List[RoutingNode],
    weight_by_node_id: Mapping[int, float],
) -> List[PickupLeg]:
    """THE single source of truth for a pickup tour's load profile.

    The truck leaves the depot EMPTY and accrues weight at each stop, so the leg
    INTO stop *i* carries only what was collected at stops 1..i-1, and the return
    leg carries the whole order.

    Both consumers must go through here:
      * ``evaluate_direct`` below, which SCORES the direct plan (and therefore
        decides whether cross-dock consolidation is worth taking);
      * ``solve.optimize_bom``, which RENDERS the legs the map and the checkout
        panel display.

    They used to model the load independently, and the renderer charged the FULL
    order weight to every leg including the empty outbound one — so the legs on
    screen overstated cost and CO2e against the very numbers that ranked the
    plans. Extracting the accrual here means the displayed legs cannot again
    describe a different vehicle from the one that was scored.

    Rate class is a property of the TOUR, not of the leg. One truck is dispatched
    for the whole milk-run, so if the full order warrants a truckload rate you pay
    that rate for every leg, including the ones where the trailer is still filling
    up. Deciding TL-vs-LTL per leg from the carried weight instead re-prices the
    early legs of a full tour as LTL and — measured on a real 3-stop domestic run
    with 11,000 kg — inflates it by 775%. Emissions still scale with what is
    actually aboard, because ton-mile factors are per unit of freight moved.

    Two honest consequences of the cited cost models, worth stating rather than
    papering over:
      * An empty leg still costs money — ``transport_cost_usd`` charges
        ``LTL_BASE_FEE_USD`` regardless of weight, which is correct: moving a
        truck has a fixed cost.
      * An empty leg emits nothing under ``co2_kg``, because EPA SmartWay
        factors are per TON-MILE and attribute emissions to freight carried.
        That is the convention, not an oversight; empty-running emissions are
        outside the accounting boundary this project cites.
    """
    if not ordered_nodes:
        return []

    # One dispatch decision for the whole tour (see docstring).
    tour_weight_kg = sum(weight_by_node_id.get(n.id, 0.0) for n in ordered_nodes)
    tl_tour = tour_weight_kg >= TL_THRESHOLD_KG

    def leg_cost(d_km: float, carried: float) -> float:
        if tl_tour:
            return km_to_miles(d_km) * TL_RATE_USD_PER_MILE
        return transport_cost_usd(d_km, carried)

    legs: List[PickupLeg] = []
    prev_lat, prev_lng = depot.lat, depot.lng
    carried_kg = 0.0  # the truck leaves the depot empty
    for node in ordered_nodes:
        d_km = haversine_km(prev_lat, prev_lng, node.lat, node.lng)
        # Charged on what is ALREADY aboard for this leg — the pickup at `node`
        # happens on arrival and is carried onward, not backwards.
        legs.append(PickupLeg(
            node=node,
            distance_km=d_km,
            carried_kg=carried_kg,
            cost_usd=leg_cost(d_km, carried_kg),
            co2_kg=co2_kg(d_km, carried_kg),
            transit_days=transit_days(d_km),
        ))
        # RoutingNode.id IS the distributor_id (-1 for the depot).
        carried_kg += weight_by_node_id.get(node.id, 0.0)
        prev_lat, prev_lng = node.lat, node.lng

    # Return leg to the depot — the only fully laden leg of a pickup tour.
    d_km = haversine_km(prev_lat, prev_lng, depot.lat, depot.lng)
    legs.append(PickupLeg(
        node=None,
        distance_km=d_km,
        carried_kg=carried_kg,
        cost_usd=leg_cost(d_km, carried_kg),
        co2_kg=co2_kg(d_km, carried_kg),
        transit_days=transit_days(d_km),
    ))
    return legs


def evaluate_direct(
    depot: GeoPoint,
    ordered_nodes: List[RoutingNode],
    shipments_by_did: dict,
) -> RouteMetrics:
    """
    Compute cost/time/CO2 for the direct pickup tour.

    A single truck drives depot → d1 → d2 → ... → depot, PICKING UP as it goes.
    We model this as a sequence of LTL-or-TL legs.

    The load profile and the rate class both live in ``pickup_tour_legs`` above —
    read its docstring for the modelling and the citations. This function only
    folds those legs into plan-level metrics, so the plan that gets SCORED here
    and the legs that get DISPLAYED by ``solve.optimize_bom`` are by construction
    the same vehicle carrying the same freight.
    """
    if not ordered_nodes:
        return RouteMetrics(0.0, 0.0, 0.0)

    # Handling happens in parallel before the truck arrives — use the slowest
    # distributor tier across the pickup set (max, not sum).
    max_handling = max(
        HANDLING_DAYS_BY_TIER.get(s.distributor_tier, 2)
        for s in shipments_by_did.values()
    )

    legs = pickup_tour_legs(
        depot, ordered_nodes,
        {did: s.weight_kg for did, s in shipments_by_did.items()},
    )

    total_cost = 0.0
    total_co2 = 0.0
    total_distance = 0.0
    total_transit_days = 0.0
    for leg in legs:
        total_cost += leg.cost_usd
        total_co2 += leg.co2_kg
        total_distance += leg.distance_km
        total_transit_days += leg.transit_days

    total_time = max_handling + total_transit_days
    return RouteMetrics(
        cost_usd=total_cost, lead_time_days=total_time,
        co2_kg=total_co2, distance_km=total_distance,
    )


def _pct_saving(direct_value: float, candidate_value: float) -> float:
    """Percent reduction of ``candidate_value`` vs ``direct_value`` (0.0 if N/A)."""
    if direct_value <= 0.0:
        return 0.0
    return round(100.0 * (1.0 - candidate_value / direct_value), 2)


def evaluate_cross_dock(
    direct: RouteMetrics,
    shipments: List[DistributorShipment],
    depot: GeoPoint,
    weights: StrategyWeights,
    hubs: List[FreightHub] = None,
    parallel: Optional[RouteMetrics] = None,
) -> CrossDockDecision:
    """Enumerate hubs, pick the best — or reject if it doesn't clear the threshold.

    ``parallel`` is the non-consolidatable stream (international air freight) that
    ``direct`` already includes; it is added to every hub plan so both sides of
    the comparison cover the same shipments. Without it the "saving" partly
    consisted of air freight that the hub plan simply forgot to pay for.

    Two distinct percentages come back:
      * ``objective_savings_pct`` — improvement on the strategy's WEIGHTED
        objective. This is what the 5% accept/reject threshold tests.
      * ``savings_vs_direct_pct`` — the transport-COST reduction that is actually
        taken, i.e. 0.0 unless ``enabled``. Callers must charge
        ``consolidated_metrics.cost_usd`` whenever it is non-zero.
    """
    if hubs is None:
        hubs = FREIGHT_HUBS

    # Cross-dock requires at least 2 distributors to make sense
    if len(shipments) < 2:
        return CrossDockDecision(
            enabled=False, hub=None, direct_metrics=direct,
            consolidated_metrics=None, savings_vs_direct_pct=0.0,
            rationale="single-distributor route — no consolidation benefit",
        )

    direct_obj = _weighted_objective(direct, weights)
    best_hub: Optional[FreightHub] = None
    best_metrics: Optional[RouteMetrics] = None
    best_obj = float("inf")

    for hub in hubs:
        m = evaluate_hub(hub, depot, shipments, parallel=parallel)
        obj = _weighted_objective(m, weights)
        if obj < best_obj:
            best_obj = obj
            best_metrics = m
            best_hub = hub

    if best_hub is None or best_metrics is None:
        return CrossDockDecision(
            enabled=False, hub=None, direct_metrics=direct,
            consolidated_metrics=None, savings_vs_direct_pct=0.0,
            rationale="no hubs provided",
        )

    obj_savings = _pct_saving(direct_obj, best_obj)
    cost_savings = _pct_saving(direct.cost_usd, best_metrics.cost_usd)

    # 5% improvement threshold, applied to the weighted objective
    if best_obj >= CROSS_DOCK_IMPROVEMENT_THRESHOLD * direct_obj:
        return CrossDockDecision(
            enabled=False, hub=best_hub, direct_metrics=direct,
            consolidated_metrics=best_metrics,
            savings_vs_direct_pct=0.0,
            candidate_cost_savings_pct=cost_savings,
            objective_savings_pct=obj_savings,
            rationale=f"hub {best_hub.city} beat direct by {obj_savings:.1f}% on the "
                      f"weighted objective — below the 5% threshold, so the direct "
                      f"pickup tour is kept and no saving is claimed",
        )

    # The hub is selected on the WEIGHTED objective, so a time- or carbon-weighted
    # strategy can rationally pick a hub that costs MORE than direct pickup and buys
    # speed or tonne-miles with the difference. Say that out loud instead of
    # printing a negative "saving".
    if cost_savings > 0:
        rationale = (
            f"consolidating via {best_hub.city} improves the weighted objective by "
            f"{obj_savings:.1f}% and is applied: transport cost charged is "
            f"${best_metrics.cost_usd:,.2f} vs ${direct.cost_usd:,.2f} direct — a "
            f"{cost_savings:.1f}% cost saving"
        )
    else:
        rationale = (
            f"consolidating via {best_hub.city} improves the weighted objective by "
            f"{obj_savings:.1f}% on time/carbon and is applied, but it does NOT save "
            f"money: transport cost charged is ${best_metrics.cost_usd:,.2f} vs "
            f"${direct.cost_usd:,.2f} direct, i.e. {-cost_savings:.1f}% MORE. The "
            f"charged figure is the consolidated one"
        )

    return CrossDockDecision(
        enabled=True, hub=best_hub, direct_metrics=direct,
        consolidated_metrics=best_metrics,
        savings_vs_direct_pct=cost_savings,
        candidate_cost_savings_pct=cost_savings,
        objective_savings_pct=obj_savings,
        rationale=rationale,
    )
