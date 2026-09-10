"""
Orchestrator — runs all 4 strategies end-to-end.

Pipeline per strategy:
  1. Outlier filter + Stage 1 CP-SAT sourcing — each strategy runs its own
     MILP solve with strategy-specific parameters (the authoritative values live
     in strategies.py; these are `transport_penalty_scale` and
     `consolidation_bonus_usd`):
       cheapest  → global   (us_only=False, penalty_scale=1.0, consolidation=$0.50)
       fastest   → domestic (us_only=True,  penalty_scale=1.0, consolidation=$150.00)
       greenest  → domestic (us_only=True,  penalty_scale=2.5, consolidation=$2.50)
       balanced  → domestic (us_only=True,  penalty_scale=1.5, consolidation=$2.00)
     `fastest` was recalibrated on 2026-08-16 from 0.0 / $3.00 to 1.0 / $150.00:
     Stage 1 minimizes landed cost only, so a time-preferring strategy steers via
     distance (penalty scale) and supplier count (consolidation bonus). See the
     measured rationale on the `fastest` entry in strategies.py.
     Strategies share a cached solve only when ALL of (us_only, penalty_scale,
     consolidation_bonus) are identical — in practice each strategy is distinct.
  2. Stage 2 pickup TSP over each strategy's selected distributors.
     International distributors are air-freight legs (not truck tour stops).
  3. Cross-dock evaluation per strategy (fastest penalizes hub dwell time,
     greenest rewards consolidation savings). International air freight is
     passed in as a PARALLEL stream so the hub plan and the direct plan are
     compared over the same shipments.
  4. Compose final RouteAlternative with strategy_math + cost_breakdown.
     Cost, CO2, distance and ETA always come from ONE plan: the displayed legs
     when cross-dock is not applied, the consolidated hub plan when it is.
     `transport_cost_basis` / `route_legs_note` say which, per alternative.
  5. Rank with standard competition ranking and publish `strategy_divergence`,
     so identical plans are reported as identical instead of being separated by
     list order.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.optimization import schemas
from app.optimization.constants import AIR_FREIGHT_BASE_USD, AIR_FREIGHT_RATE_USD_PER_KG
from app.optimization.costs import (
    AVG_COMPONENT_KG,
    air_transit_days,
    haversine_km,
    holding_cost_usd,
    ml_factory_lead_time_days,
)
from app.optimization.cross_dock import (
    CrossDockDecision,
    DistributorShipment,
    RouteMetrics,
    evaluate_cross_dock,
    evaluate_direct,
    pickup_tour_legs,
)
from app.optimization.freight_hubs import FREIGHT_HUBS
from app.optimization.routing import (
    GeoPoint, RoutingNode, TspSolution, solve_pickup_tsp_detailed,
)
from app.optimization.sourcing import (
    BomLine,
    Offer,
    SourcingResult,
    solve_sourcing,
)
from app.optimization.strategies import (
    STRATEGIES,
    StrategyWeights,
    normalize_objectives,
    weighted_objective,
)

logger = logging.getLogger(__name__)

# Air-freight carbon factor: ~0.5 kg CO2e per tonne-km for a dedicated freighter.
# Because this is a kg-of-CO2e per kg-of-payload per km ratio the metric tonne
# cancels — 0.5 kg/tonne-km == 0.0005 kg/kg-km with no unit conversion. Contrast
# the TRUCK factor (constants.CO2_G_PER_TON_MILE), which is per US SHORT ton-mile
# and DOES need one; see the note above `costs.KG_PER_SHORT_TON`.
#
# ATTRIBUTION (resolved 2026-09-03 — the value stands, the label was wrong).
# This repo used to attribute 0.0005 to "ICAO 2023", but ICAO publishes no static
# air-freight table: its Carbon Emissions Calculator computes per flight from
# aircraft type, distance and load factor. The value that 0.0005 actually matches
# is the GLEC Framework v3.2 long-haul dedicated-freighter tank-to-wheel default,
# 503 g CO2e/tonne-km, and that is now the published label everywhere.
# It is therefore a combustion-only, long-haul, full-freighter figure: GLEC's
# well-to-wheel equivalent is 608, DEFRA UK 2023 long-haul CO2-only is 643, and
# DEFRA's recommended with-radiative-forcing figure is 1,099 g/tonne-km. Belly-
# hold and short-haul run 2-3x higher. So this is the optimistic end of the
# published range, and it makes the air-vs-truck ratio (4.51x) a LOWER BOUND.
# The label is carried in three published places kept in sync with this comment:
# strategies.py (`greenest.basis`, served to the UI), the StrategyMath.citations
# list below (served to the UI), and docs/OPTIMIZATION_DESIGN.md.
#
# Defined once here because it was previously a local in `_evaluate_strategy`
# AND a bare 0.0005 literal in the route-stop builder, two copies free to drift.
CO2_AIR_KG_PER_KG_KM = 0.0005


# ── Input data containers ────────────────────────────────────────────────────

@dataclass
class DistributorMeta:
    id: int
    name: str
    lat: float
    lng: float
    city: Optional[str]
    state: Optional[str]
    country: Optional[str]
    is_domestic: bool
    tier: str  # 'major'|'mid'|'broker'


# ── Monte Carlo ETA (retained from old optimize.py) ──────────────────────────

MONTE_CARLO_SIMULATIONS = 1000
MONTE_CARLO_SAMPLE_POINTS = 200
# The sampler is seeded from the plan's own base ETA rather than drawing on the
# global RNG. Two alternatives with the same base ETA — which is exactly what
# happens when two strategies converge on the same sourcing plan — then get the
# same distribution, instead of eta_p50 differing by a sampling wobble of 0.1 d
# and the speed ranking treating one identical plan as faster than the other.
MONTE_CARLO_SEED = 20260815

# ── The ETA simulation's assumed parameters, stated rather than buried ───────
#
# NONE OF THESE ARE MEASURED. They were four magic literals inside the function
# body, surfaced to the UI as "Monte Carlo simulation (1,000 scenarios)" and read
# as an empirical fulfilment band. They are now named, published on every
# response (RouteAlternative.monte_carlo_assumptions) and labelled uncalibrated.
#
# CAN THEY BE GROUNDED FROM THIS REPO? No — checked, not assumed. Grounding the
# transit multiplier needs observed shipped-vs-promised dates, and grounding the
# disruption mixture needs observed disruption incidence per shipment. This repo
# has neither: seeds/data/lead_time_panel carries DigiKey FACTORY (replenishment)
# lead times per part, which is a different quantity from shipment schedule
# adherence, and no feed here records realised delivery dates. Fitting the
# multiplier to the factory panel would produce a number with a citation and the
# wrong meaning, which is worse than an assumption that says it is one.
MONTE_CARLO_TRANSIT_MULTIPLIER_MEAN = 1.0
MONTE_CARLO_TRANSIT_MULTIPLIER_SIGMA = 0.15
MONTE_CARLO_DISRUPTION_DELAY_DAYS = [0.0, 1.0, 3.0, 7.0]
MONTE_CARLO_DISRUPTION_WEIGHTS = [0.85, 0.08, 0.05, 0.02]
MONTE_CARLO_ASSUMPTION_CAVEAT = (
    "ASSUMED, NOT MEASURED. The transit-time multiplier is modelled as "
    "Normal(mean, sigma) about the deterministic route ETA, and a disruption "
    "delay is drawn from the listed day values with the listed weights. Neither "
    "the multiplier's spread nor the disruption mixture is fitted to observed "
    "shipment data — this repository contains DigiKey factory lead times, which "
    "measure replenishment, not schedule adherence, and no record of realised "
    "delivery dates. Treat the p10/p50/p90 band as a sensitivity range around "
    "the route ETA, not as an empirical service-level distribution. The run is "
    "seeded, so the band is reproducible; reproducible is not the same as "
    "calibrated."
)
MONTE_CARLO_SAMPLE_KIND = (
    f"evenly-spaced quantiles of {MONTE_CARLO_SIMULATIONS} simulations "
    f"({MONTE_CARLO_SAMPLE_POINTS} points, min and max included)"
)


def _monte_carlo_eta(
    base_days: float,
    n: int = MONTE_CARLO_SIMULATIONS,
    sample_points: int = MONTE_CARLO_SAMPLE_POINTS,
) -> Dict[str, object]:
    """Simulate ``n`` delivery outcomes and return percentiles + a summary sample.

    The returned ``samples`` list is a DOWN-SAMPLE of the full run, taken at
    evenly spaced quantiles of the sorted draws, so it spans the whole
    distribution: ``samples[0]`` is the minimum and ``samples[-1]`` the maximum,
    and p10/p50/p90 always fall inside its range.

    This used to be ``samples[:200]`` — the 200 SMALLEST of 1000 draws — while
    being published as "1000 Monte Carlo simulations". Anything binning that list
    plotted only the left tail, which is why the chart's own p50 and p90 markers
    landed outside its x-axis. The quantile down-sample is deterministic given
    the draws, so it does not add a second source of randomness.

    THE DISTRIBUTION ITSELF IS ASSUMED, NOT MEASURED — see
    MONTE_CARLO_ASSUMPTION_CAVEAT above for what that means and why it could not
    honestly be grounded from the data in this repository. The parameters are
    published on every response so a reader can see what was assumed instead of
    inferring an empirical result from the phrase "Monte Carlo".

    The draw uses an isolated ``random.Random`` (the ``stochastic.py`` house
    pattern) rather than the global module, so it touches no global RNG state and
    the percentiles reproduce run to run. The seed is derived from the plan's own
    base ETA as well as the module seed, so two strategies that converge on the
    same plan get the same band — otherwise sampling noise alone gave identical
    plans eta_p50 5.6 vs 5.7 and the speed ranking split them.
    """
    rng = random.Random(f"{MONTE_CARLO_SEED}:{n}:{round(base_days, 6)}")
    samples = []
    for _ in range(n):
        delay = rng.gauss(
            MONTE_CARLO_TRANSIT_MULTIPLIER_MEAN, MONTE_CARLO_TRANSIT_MULTIPLIER_SIGMA
        )
        disruption = rng.choices(
            MONTE_CARLO_DISRUPTION_DELAY_DAYS,
            weights=MONTE_CARLO_DISRUPTION_WEIGHTS,
        )[0]
        samples.append(max(1.0, base_days * delay + disruption))
    samples.sort()

    k = max(1, min(sample_points, n))
    if k == 1:
        summary = [samples[n // 2]]
    else:
        summary = [samples[round(i * (n - 1) / (k - 1))] for i in range(k)]

    return {
        "p10": round(samples[int(0.1 * n)], 1),
        "p50": round(samples[int(0.5 * n)], 1),
        "p90": round(samples[int(0.9 * n)], 1),
        "samples": summary,
        "n_simulations": n,
        "sample_kind": MONTE_CARLO_SAMPLE_KIND,
        "seed": MONTE_CARLO_SEED,
        "assumptions": schemas.MonteCarloAssumptions(
            calibrated=False,
            seed=MONTE_CARLO_SEED,
            transit_multiplier_mean=MONTE_CARLO_TRANSIT_MULTIPLIER_MEAN,
            transit_multiplier_sigma=MONTE_CARLO_TRANSIT_MULTIPLIER_SIGMA,
            disruption_delay_days=list(MONTE_CARLO_DISRUPTION_DELAY_DAYS),
            disruption_weights=list(MONTE_CARLO_DISRUPTION_WEIGHTS),
            caveat=MONTE_CARLO_ASSUMPTION_CAVEAT,
        ),
    }


# ── Main orchestrator ────────────────────────────────────────────────────────

def _assess_supply_risk(
    sourcing: SourcingResult,
    bom: List[BomLine],
    offers: List[Offer],
    route_eta_days: float,
) -> schemas.SupplyRiskInfo:
    """Score every assigned BOM line with the ML factory-lead-time model.

    Unlike the gate this replaced, there is no threshold to clear: the model is
    consulted for EVERY line whose category is inside its trained vocabulary,
    on every strategy, on every run. A line is "declined" — not silently
    zero-filled — when its category was never observed in the panel.

    ``risk_adjusted_eta_days`` adds the longest factory lead time among
    ZERO-BUFFER lines (lines where the plan takes 100% of the distributor's
    reported shelf, so a stale inventory snapshot puts the balance on factory
    lead time) to the route ETA. When every line ships with buffer to spare it
    equals the route ETA, which is the correct answer, not a suppressed one.
    """
    line_by_cid = {b.component_id: b for b in bom}
    offer_by_key = {(o.component_id, o.distributor_id): o for o in offers}

    scored = 0
    declined = 0
    declined_reason: Optional[str] = None
    model_name: Optional[str] = None
    model_source_name: Optional[str] = None
    unavailable_reason: Optional[str] = None
    max_days: Optional[float] = None
    driver_mpn: Optional[str] = None
    zero_buffer_lines = 0
    zero_buffer_max_days = 0.0

    for a in sourcing.assignments:
        offer = offer_by_key.get((a.component_id, a.distributor_id))
        line = line_by_cid.get(a.component_id)
        if offer is None or line is None:
            continue
        if not (line.dk_category or line.category):
            declined += 1
            declined_reason = declined_reason or "BOM line carries no component category"
            continue

        # Pass everything we have; the resolved feature schema decides what it
        # consumes, so this call site does not change as the feature set grows.
        result = ml_factory_lead_time_days({
            "dk_category": line.dk_category,
            "dk_subcategory": line.dk_subcategory,
            "category": line.category,
            "manufacturer": line.manufacturer,
            "lifecycle_status": line.lifecycle_status,
            "is_normally_stocked": line.is_normally_stocked,
            "parameter_count": line.parameter_count,
            "package_case": line.package_case,
            "htsus_code": line.htsus_code,
            "rohs_status": line.rohs_status,
            "max_break_qty": line.max_break_qty,
            "price_break_count": line.price_break_count,
            # DigiKey's own price for the part — the SAME column the model trained
            # on. Falls back to this offer's price only when DigiKey never quoted
            # one, and the schema declines the line rather than guessing if both
            # are absent.
            "unit_price": (
                line.digikey_unit_price
                if line.digikey_unit_price is not None else float(offer.price_usd)
            ),
            "moq": offer.moq,
            "packaging": offer.packaging,
            "standard_pack": offer.standard_pack,
        })
        if not result.available or result.days is None:
            declined += 1
            declined_reason = declined_reason or result.reason
            unavailable_reason = unavailable_reason or result.reason
            continue

        scored += 1
        model_name = model_name or result.model_name
        model_source_name = model_source_name or result.model_source
        if max_days is None or result.days > max_days:
            max_days = result.days
            driver_mpn = a.mpn

        # Zero buffer: the plan takes the distributor's entire reported shelf for
        # this line (the MILP fills an offer to the brim when it cannot cover the
        # whole line). Nothing is left over if the weekly snapshot was stale.
        if offer.stock > 0 and a.quantity >= offer.stock:
            zero_buffer_lines += 1
            zero_buffer_max_days = max(zero_buffer_max_days, result.days)

    risk_adjusted = route_eta_days + zero_buffer_max_days

    if scored == 0:
        rationale = (
            "No BOM line could be scored by the lead-time model "
            f"({declined_reason or unavailable_reason or 'no model loaded'}). "
            "Delivery ETA is route-derived (distributor handling + ground transit); "
            "no factory-lead-time risk is claimed."
        )
    elif zero_buffer_lines:
        rationale = (
            f"Longest factory lead time in this plan is {max_days:.0f} d ({driver_mpn}). "
            f"{zero_buffer_lines} line(s) take 100% of a distributor's reported shelf, so a "
            f"stale inventory snapshot would push the balance onto factory lead time: "
            f"risk-adjusted ETA {risk_adjusted:.1f} d vs {route_eta_days:.1f} d shipping from stock."
        )
    else:
        rationale = (
            f"Longest factory lead time among the {scored} scored line(s) is {max_days:.0f} d "
            f"({driver_mpn}) — that is the replenishment exposure, not this shipment's ETA. "
            f"Every line ships from stock with buffer remaining, so the ETA stays at "
            f"{route_eta_days:.1f} d."
        )
    if declined:
        rationale += (
            f" {declined} line(s) were declined by the model rather than guessed "
            f"({declined_reason})."
        )

    return schemas.SupplyRiskInfo(
        model_available=scored > 0,
        model_name=model_name,
        model_source=model_source_name,
        lines_scored=scored,
        lines_declined=declined,
        declined_reason=declined_reason,
        max_factory_lead_time_days=round(max_days, 1) if max_days is not None else None,
        driver_mpn=driver_mpn,
        zero_buffer_lines=zero_buffer_lines,
        route_eta_days=round(route_eta_days, 2),
        risk_adjusted_eta_days=round(risk_adjusted, 2),
        rationale=rationale,
    )


def _build_route_data(
    sourcing: SourcingResult,
    distributors: Dict[int, DistributorMeta],
    depot: GeoPoint,
) -> tuple:
    """Build weight/cost maps, TSP tour (domestic only), and direct metrics.

    Domestic distributors: included in the TSP truck tour.
    International distributors: modelled as direct air freight shipments
      (flat IATA base + per-kg rate) that arrive in parallel with the truck tour.
      A PCB manufacturer orders internationally by air — the truck never goes to
      China. This avoids applying LTL trucking rates to transatlantic distances.
    """
    weight_by_did: Dict[int, float] = {}
    cost_by_did: Dict[int, float] = {}
    components_by_did: Dict[int, List[str]] = {}
    for a in sourcing.assignments:
        weight_by_did[a.distributor_id] = (
            weight_by_did.get(a.distributor_id, 0.0) + a.quantity * AVG_COMPONENT_KG
        )
        cost_by_did[a.distributor_id] = (
            cost_by_did.get(a.distributor_id, 0.0) + a.line_total
        )
        components_by_did.setdefault(a.distributor_id, []).append(
            f"{a.mpn} × {a.quantity}"
        )

    domestic_dids = [did for did in sourcing.selected_distributor_ids if distributors[did].is_domestic]
    intl_dids = [did for did in sourcing.selected_distributor_ids if not distributors[did].is_domestic]

    # TSP tour over domestic distributors only
    nodes: List[RoutingNode] = []
    for did in domestic_dids:
        d = distributors[did]
        nodes.append(RoutingNode(id=did, lat=d.lat, lng=d.lng, name=d.name))
    # Small tours (<= routing.EXACT_MAX_STOPS) are enumerated exhaustively and come
    # back proven optimal; larger ones fall through to GUIDED_LOCAL_SEARCH and are
    # explicitly NOT certified. `tsp` carries which of the two ran, and that
    # reaches the response as `RouteAlternative.routing_solver`.
    tsp = solve_pickup_tsp_detailed(depot, nodes)
    ordered_nodes = [next(n for n in nodes if n.id == did) for did in tsp.order]

    # Shipments for cross-dock evaluation — domestic only (makes sense to consolidate)
    shipments_by_did: Dict[int, DistributorShipment] = {}
    for did in domestic_dids:
        d = distributors[did]
        shipments_by_did[did] = DistributorShipment(
            distributor_id=did, distributor_name=d.name,
            lat=d.lat, lng=d.lng,
            weight_kg=max(weight_by_did[did], 0.1),
            distributor_tier=d.tier,
        )
    shipments_list = list(shipments_by_did.values())
    domestic_metrics = evaluate_direct(depot, ordered_nodes, shipments_by_did)

    # Air freight cost, distance, carbon and lead time for international
    # distributors. These consignments run in PARALLEL with the domestic truck
    # tour, so cost/carbon/distance add and lead time takes the max.
    #
    # Lead time is `air_transit_days(great-circle km)` (see costs.py), not the
    # flat 4 days this used to apply to every international origin on earth.
    # Two further bugs lived in these five lines: `intl_time = ...` ASSIGNED
    # inside the loop (so the last distributor won, rather than the slowest),
    # and the haversine distance was computed for CO2 and then thrown away, which
    # is how an alternative could report 3,665 kg CO2e over 0.0 km.
    #
    # Carbon uses the module-level CO2_AIR_KG_PER_KG_KM (GLEC v3.2) — the same
    # constant the per-leg route-stop builder below uses.
    intl_cost = 0.0
    intl_co2 = 0.0
    intl_time = 0.0
    intl_distance = 0.0
    for did in intl_dids:
        d = distributors[did]
        w = max(weight_by_did.get(did, 0.0), 0.1)
        dist_km = haversine_km(depot.lat, depot.lng, d.lat, d.lng)
        intl_cost += AIR_FREIGHT_BASE_USD + w * AIR_FREIGHT_RATE_USD_PER_KG
        intl_co2 += w * dist_km * CO2_AIR_KG_PER_KG_KM
        intl_distance += dist_km
        intl_time = max(intl_time, air_transit_days(dist_km))

    intl_metrics = RouteMetrics(
        cost_usd=intl_cost, lead_time_days=intl_time,
        co2_kg=intl_co2, distance_km=intl_distance,
    )
    direct_metrics = domestic_metrics.plus_parallel(intl_metrics)

    # For the ordered_nodes list used to build route stops, include intl distributors
    # as virtual nodes (they don't affect the truck tour but appear in the UI).
    intl_nodes = [RoutingNode(id=did, lat=distributors[did].lat, lng=distributors[did].lng,
                               name=distributors[did].name) for did in intl_dids]

    return (weight_by_did, cost_by_did, components_by_did,
            ordered_nodes, shipments_by_did, shipments_list, direct_metrics,
            intl_nodes, intl_cost, intl_metrics, tsp)


def optimize_bom(
    bom: List[BomLine],
    offers: List[Offer],
    distributors: Dict[int, DistributorMeta],
    depot: GeoPoint,
    us_only: bool = False,
    graph_aware: bool = False,
    require_dual_source: bool = False,
) -> schemas.MultiRouteResponse:
    """Run all 4 strategies and return a MultiRouteResponse.

    require_dual_source: pass-through to the Stage 1 sourcing MILP; when True
    each strategy's plan is forced to spread the BOM across ≥2 distributors
    (hard diversification) instead of consolidating onto one cheapest hub.
    """
    if not bom:
        raise ValueError("BOM is empty")

    # ── Stage 1: per-strategy sourcing solve, cached by us_only flag.
    # Strategies with different us_only_sourcing get different supplier pools,
    # which is the primary driver of divergence (cheapest picks global, fastest
    # picks domestic for lower handling times).
    sourcing_cache: Dict[bool, SourcingResult] = {}
    all_outlier_drops = []

    def _get_sourcing(strat) -> SourcingResult:
        # Cache key: all MILP-influencing parameters must be included.
        # transport_penalty_scale and consolidation_bonus_usd both affect the
        # MILP objective; strategies with any differing value run separate solves.
        cache_key = (
            strat.us_only_sourcing or us_only,
            getattr(strat, "transport_penalty_scale", 1.0),
            getattr(strat, "consolidation_bonus_usd", 1.0),
        )
        if cache_key not in sourcing_cache:
            result = solve_sourcing(
                bom, offers, strat, us_only=cache_key[0],
                graph_aware=graph_aware, require_dual_source=require_dual_source,
            )
            sourcing_cache[cache_key] = result
            all_outlier_drops.extend(result.outlier_drops)
        return sourcing_cache[cache_key]

    # Pre-solve all unique strategy variants upfront
    for strat in STRATEGIES:
        _get_sourcing(strat)

    # ── Run each strategy: build route data + cross-dock decision
    strategy_raw: List[Dict] = []
    for strat in STRATEGIES:
        sourcing = _get_sourcing(strat)
        (weight_by_did, cost_by_did, components_by_did,
         ordered_nodes, shipments_by_did, shipments_list,
         direct_metrics, intl_nodes, intl_transport_cost,
         intl_metrics, tsp) = _build_route_data(sourcing, distributors, depot)

        # `parallel=intl_metrics` keeps both sides of the comparison on the same
        # scope: the hub can only consolidate the DOMESTIC shipments, but the
        # international air freight is paid either way, so it must appear in the
        # consolidated plan too. Omitting it (the previous behaviour) compared a
        # domestic-only hub plan against a direct plan carrying transpacific air
        # freight and called the gap a consolidation saving.
        decision = evaluate_cross_dock(
            direct_metrics, shipments_list, depot, strat, hubs=FREIGHT_HUBS,
            parallel=intl_metrics,
        )
        if decision.enabled and decision.consolidated_metrics:
            m = decision.consolidated_metrics
        else:
            m = direct_metrics
        strategy_raw.append({
            "strategy": strat,
            "sourcing": sourcing,
            "cost": m.cost_usd,
            "time": m.lead_time_days,
            "carbon": m.co2_kg,
            "metrics": m,
            "decision": decision,
            "weight_by_did": weight_by_did,
            "cost_by_did": cost_by_did,
            "components_by_did": components_by_did,
            "ordered_nodes": ordered_nodes,
            "shipments_by_did": shipments_by_did,
            "intl_nodes": intl_nodes,
            "intl_transport_cost": intl_transport_cost,
            "tsp": tsp,
        })

    # Normalize across strategies
    normed = normalize_objectives([
        {"cost": r["cost"], "time": r["time"], "carbon": r["carbon"]}
        for r in strategy_raw
    ])

    # ── Assemble RouteAlternative list
    alternatives: List[schemas.RouteAlternative] = []
    for i, r in enumerate(strategy_raw):
        strat: StrategyWeights = r["strategy"]
        m: RouteMetrics = r["metrics"]
        decision: CrossDockDecision = r["decision"]
        norm = normed[i]
        sourcing: SourcingResult = r["sourcing"]
        weight_by_did = r["weight_by_did"]
        cost_by_did = r["cost_by_did"]
        components_by_did = r["components_by_did"]
        ordered_nodes = r["ordered_nodes"]

        # Build route stops.
        # Domestic distributors: truck tour with LTL per-leg cost.
        # International distributors: air freight (fixed cost, no truck leg distance).
        intl_nodes_r: List[RoutingNode] = r["intl_nodes"]
        intl_transport_cost_r: float = r["intl_transport_cost"]
        stops: List[schemas.RouteStop] = []
        intl_count = len(intl_nodes_r)
        seq = 0

        # The load profile comes from `cross_dock.pickup_tour_legs` — the SAME
        # function `evaluate_direct` scores the plan with. It used to be
        # re-derived here as a flat `domestic_weight` (the whole order, charged
        # to every leg including the outbound one on which the trailer is still
        # empty), so the legs drawn on the map and listed at checkout overstated
        # cost and CO2e against the very figures that ranked the four plans. The
        # display and the decision now read from one model, not two.
        shipments_by_did_r: Dict[int, DistributorShipment] = r["shipments_by_did"]
        tour_legs = pickup_tour_legs(
            depot, ordered_nodes,
            {did: sh.weight_kg for did, sh in shipments_by_did_r.items()},
        )
        for leg in tour_legs:
            seq += 1
            if leg.node is not None:
                d = distributors[leg.node.id]
                stops.append(schemas.RouteStop(
                    order=seq,
                    distributor_id=leg.node.id,
                    distributor_name=d.name,
                    city=d.city, state=d.state, country=d.country,
                    lat=d.lat, lng=d.lng,
                    components=components_by_did.get(leg.node.id, []),
                    distance_km=round(leg.distance_km, 1),
                    leg_cost_usd=round(leg.cost_usd, 2),
                    leg_co2e_kg=round(leg.co2_kg, 3),
                    leg_carried_kg=round(leg.carried_kg, 3),
                ))
            else:
                # Return-to-depot leg — the only fully laden leg of a pickup tour.
                stops.append(schemas.RouteStop(
                    order=seq,
                    distributor_id=0,
                    distributor_name="Factory (Depot)",
                    city=None, state=None, country="USA",
                    lat=depot.lat, lng=depot.lng,
                    components=[],
                    distance_km=round(leg.distance_km, 1),
                    leg_cost_usd=round(leg.cost_usd, 2),
                    leg_co2e_kg=round(leg.co2_kg, 3),
                    leg_carried_kg=round(leg.carried_kg, 3),
                ))

        # Air freight stops for international distributors (shown as separate legs)
        air_per_intl = intl_transport_cost_r / max(len(intl_nodes_r), 1)
        for node in intl_nodes_r:
            d = distributors[node.id]
            w = max(weight_by_did.get(node.id, 0.0), 0.1)
            af_dist_km = haversine_km(depot.lat, depot.lng, d.lat, d.lng)
            af_cost = AIR_FREIGHT_BASE_USD + w * AIR_FREIGHT_RATE_USD_PER_KG
            af_co2 = w * af_dist_km * CO2_AIR_KG_PER_KG_KM
            seq += 1
            stops.append(schemas.RouteStop(
                order=seq,
                distributor_id=node.id,
                distributor_name=d.name,
                city=d.city, state=d.state, country=d.country,
                lat=d.lat, lng=d.lng,
                components=components_by_did.get(node.id, []),
                # The great-circle distance actually flown — the SAME number the
                # leg's CO2 is derived from. It was hard-coded to 0.0 here, which
                # is why a plan could report thousands of kg of CO2e over a
                # reported total distance of 0.0 km.
                distance_km=round(af_dist_km, 1),
                leg_cost_usd=round(af_cost, 2),
                leg_co2e_kg=round(af_co2, 3),
                # An air consignment flies alone: it carries its own weight for
                # the whole flight, so there is no accrual to model here.
                leg_carried_kg=round(w, 3),
            ))

        # ── Totals ───────────────────────────────────────────────────────────
        # Two coherent cases, never a mix of the two:
        #
        #  * cross-dock NOT applied → every headline number is the sum of the
        #    displayed legs, so sum(leg_cost_usd) == total_transport_cost_usd.
        #  * cross-dock APPLIED → the plan really is the hub-routed one, so cost,
        #    carbon, distance AND eta all come from `m` (the consolidated
        #    metrics, which include the international air freight via
        #    `parallel`). `route` still lists the pre-consolidation pickup legs
        #    because those are what a map can draw; `route_legs_note` and the
        #    route_leg_* totals say so explicitly rather than leaving a silent
        #    discrepancy. Previously the headline cost was ALWAYS the direct-tour
        #    figure while the ETA and CO2 came from `m` — so a 65% saving was
        #    advertised in cross_dock and never charged.
        component_cost = sum(cost_by_did.values())
        leg_cost_total = round(sum(s.leg_cost_usd for s in stops), 2)
        leg_co2_total = round(sum(s.leg_co2e_kg for s in stops), 3)
        leg_distance_total = round(sum(s.distance_km for s in stops), 1)

        cross_dock_applied = bool(decision.enabled and decision.consolidated_metrics)
        if cross_dock_applied:
            transport_cost = round(m.cost_usd, 2)
            total_co2 = round(m.co2_kg, 3)
            total_distance = round(m.distance_km, 1)
            transport_cost_basis = "cross_dock_consolidated"
            hub_label = decision.hub.city if decision.hub else "the selected hub"
            route_legs_note = (
                f"Charged transport cost, CO2e, distance and ETA describe the "
                f"cross-dock plan consolidating through {hub_label}. The legs "
                f"listed in `route` are the pre-consolidation pickup legs "
                f"(${leg_cost_total:,.2f} / {leg_co2_total:,.1f} kg CO2e / "
                f"{leg_distance_total:,.0f} km) and are shown for map display "
                f"only — they are not what this plan is charged."
            )
        else:
            transport_cost = leg_cost_total
            total_co2 = leg_co2_total
            total_distance = leg_distance_total
            transport_cost_basis = "direct_pickup_tour"
            route_legs_note = (
                "Direct pickup tour: the headline transport cost, CO2e and "
                "distance are exactly the sum of the legs listed in `route`."
            )

        holding = holding_cost_usd(component_cost, m.lead_time_days)
        total_cost = component_cost + transport_cost + holding

        if ordered_nodes:
            rep_node = ordered_nodes[len(ordered_nodes) // 2]

        # ── ML factory lead time → supply-risk read-out (NOT the delivery ETA) ──
        #
        # This used to read: predict one ML lead time for a made-up
        # ("Microcontrollers", median distance, risk 0.5, coverage 10.0) row and
        # SWAP it in for the route ETA if
        #     |ml - route| / route > 0.10  and  ml < route * 2
        # Because the served model was a constant 62.1 d (see lead_time_model.py)
        # and `route_eta` never exceeded 16.4 d across all 234 recorded runs, the
        # second clause required route_eta > 31.05 d and the branch fired 0/234
        # times. Fixing the threshold would not have made it right: the model
        # predicts FACTORY lead time (time to replenish a part) while route_eta
        # is handling + transit for a unit shipping off the shelf. The sourcing
        # MILP hard-constrains ordered_qty <= offer.stock, so the delivery ETA of
        # the plan it produces genuinely IS route-derived.
        #
        # So the model now answers the question it was trained to answer, per BOM
        # line, using that line's real category / stock / price — and it is
        # reported as its own quantity instead of overwriting the ETA.
        route_eta = m.lead_time_days
        supply_risk = _assess_supply_risk(sourcing, bom, offers, route_eta)
        effective_eta = route_eta

        # ── Port congestion delay from live feeds (per D-02) ──────────────────
        try:
            from app.optimization.costs import _port_delay_days
            from app.feeds import get_live_data_cache as _get_ldc
            _feed_cache = _get_ldc()
            if _feed_cache is not None and ordered_nodes:
                rep_dist_obj = distributors[rep_node.id]
                port_delay = _port_delay_days(
                    rep_dist_obj.lat, rep_dist_obj.lng, _feed_cache
                )
                effective_eta += port_delay
        except Exception:
            pass  # graceful degradation — no port delay on error

        mc = _monte_carlo_eta(max(effective_eta, 1.0))

        cost_breakdown = schemas.CostBreakdown(
            component_cost=round(component_cost, 2),
            transport_cost=round(transport_cost, 2),
            holding_cost=round(holding, 2),
            total=round(total_cost, 2),
        )

        strategy_math = schemas.StrategyMath(
            weights={"cost": strat.w_cost, "time": strat.w_time, "carbon": strat.w_carbon},
            raw_objective_values={
                "cost": round(m.cost_usd, 2),
                "time": round(m.lead_time_days, 2),
                "carbon": round(m.co2_kg, 3),
            },
            normalized_objective_values={
                "cost": round(norm["cost_n"], 4),
                "time": round(norm["time_n"], 4),
                "carbon": round(norm["carbon_n"], 4),
            },
            weighted_total=round(weighted_objective(norm, strat), 4),
            citations=[
                "ATRI 2023 — Operational Costs of Trucking",
                # 161.8 g CO2e per US SHORT ton-mile. Sourced from the 2013
                # SmartWay technical documentation via EDF's 2014 Green Freight
                # Handbook p.11 — it is in no edition of EPA's GHG Emission
                # Factors Hub, so the previous "EPA SmartWay 2023" label named
                # the wrong vintage.
                "EPA SmartWay Technical Documentation 2013 (via EDF Green Freight "
                "Handbook 2014) — Heavy-Duty Truck, 161.8 g CO2e/short ton-mile",
                # The truck factor alone was cited while the air-freight factor
                # was used in the same CO2e figure, so the sources list on screen
                # was only half the provenance. ICAO publishes no static
                # air-freight table (its calculator is per-flight); 0.0005 is the
                # GLEC long-haul freighter default, the optimistic end of the
                # published range.
                "GLEC Framework v3.2 — Air Freight, long-haul dedicated freighter "
                "tank-to-wheel (503 g CO2e/tonne-km; optimistic end of range)",
                "Gartner 2022 — IT Supply Chain Benchmarks",
                "BTS CFS 2022 — Commodity Flow Survey",
                "Ghodsypour & O'Brien 1998 — Int'l J. Production Economics",
            ],
        )

        cd_info: Optional[schemas.CrossDockInfo]
        cm = decision.consolidated_metrics
        if decision.hub is not None:
            cd_info = schemas.CrossDockInfo(
                enabled=decision.enabled,
                applied=cross_dock_applied,
                hub_id=decision.hub.id,
                hub_name=decision.hub.name,
                hub_city=decision.hub.city,
                hub_state=decision.hub.state,
                hub_lat=decision.hub.latitude,
                hub_lng=decision.hub.longitude,
                savings_vs_direct_pct=decision.savings_vs_direct_pct,
                candidate_cost_savings_pct=decision.candidate_cost_savings_pct,
                objective_savings_pct=decision.objective_savings_pct,
                direct_cost_usd=round(decision.direct_metrics.cost_usd, 2),
                consolidated_cost_usd=round(cm.cost_usd if cm else 0.0, 2),
                consolidated_co2e_kg=round(cm.co2_kg if cm else 0.0, 3),
                consolidated_eta_days=round(cm.lead_time_days if cm else 0.0, 2),
                consolidated_distance_km=round(cm.distance_km if cm else 0.0, 1),
                rationale=decision.rationale,
            )
        else:
            cd_info = schemas.CrossDockInfo(
                enabled=False,
                applied=False,
                direct_cost_usd=round(decision.direct_metrics.cost_usd, 2),
                rationale=decision.rationale,
            )

        tsp_result: TspSolution = r["tsp"]
        routing_solver = schemas.RoutingSolverInfo(
            method=tsp_result.method,
            proven_optimal=tsp_result.proven_optimal,
            stop_count=tsp_result.stop_count,
            tours_enumerated=tsp_result.tours_enumerated,
            time_limit_seconds=tsp_result.time_limit_seconds,
            note=tsp_result.note,
        )

        sourcing_out = [
            schemas.SourcingAssignment(
                component_id=a.component_id, mpn=a.mpn,
                distributor_id=a.distributor_id,
                distributor_name=a.distributor_name,
                quantity=a.quantity,
                unit_price_usd=a.unit_price_usd,
                line_total_usd=round(a.line_total, 2),
            )
            for a in sourcing.assignments
        ]

        alternatives.append(schemas.RouteAlternative(
            id=strat.id,
            label=strat.label,
            description=strat.description,
            route=stops,
            sourcing=sourcing_out,
            total_cost_usd=round(total_cost, 2),
            total_transport_cost_usd=round(transport_cost, 2),
            total_component_cost_usd=round(component_cost, 2),
            total_co2e_kg=total_co2,
            total_distance_km=total_distance,
            base_eta_days=round(m.lead_time_days, 1),
            eta_p10=mc["p10"], eta_p50=mc["p50"], eta_p90=mc["p90"],
            monte_carlo_samples=mc["samples"],
            monte_carlo_n_simulations=mc["n_simulations"],
            monte_carlo_sample_kind=mc["sample_kind"],
            monte_carlo_seed=mc["seed"],
            monte_carlo_assumptions=mc["assumptions"],
            transport_cost_basis=transport_cost_basis,
            route_legs_note=route_legs_note,
            route_leg_cost_usd=leg_cost_total,
            route_leg_co2e_kg=leg_co2_total,
            route_leg_distance_km=leg_distance_total,
            stop_count=len(stops),
            international_stops=intl_count,
            cost_breakdown=cost_breakdown,
            strategy_math=strategy_math,
            cross_dock=cd_info,
            supply_risk=supply_risk,
            routing_solver=routing_solver,
        ))

    # ── Ranks: STANDARD COMPETITION RANKING (1, 2, 2, 4) ─────────────────────
    # Equal values get equal rank. The previous implementation sorted and then
    # numbered 1..4 by position, so two alternatives with byte-identical cost and
    # carbon were presented as "3rd cheapest" and "4th cheapest" purely because of
    # their order in STRATEGIES. On small carts all four plans are identical and
    # the UI showed a full 1-2-3-4 ladder over one plan — fabricated signal.
    def _rank(key_fn, ndigits: int = 6):
        vals = [(i, round(float(key_fn(a)), ndigits)) for i, a in enumerate(alternatives)]
        order = sorted(vals, key=lambda t: t[1])
        ranks = [0] * len(alternatives)
        prev_val: Optional[float] = None
        prev_rank = 0
        for position, (i, v) in enumerate(order, start=1):
            if prev_val is not None and v == prev_val:
                ranks[i] = prev_rank
            else:
                ranks[i] = position
                prev_rank = position
                prev_val = v
        return ranks

    cost_ranks = _rank(lambda a: a.total_cost_usd)
    speed_ranks = _rank(lambda a: a.eta_p50)
    carbon_ranks = _rank(lambda a: a.total_co2e_kg)
    dist_ranks = _rank(lambda a: a.total_distance_km)

    for i, a in enumerate(alternatives):
        a.cost_rank = cost_ranks[i]
        a.speed_rank = speed_ranks[i]
        a.carbon_rank = carbon_ranks[i]
        a.distance_rank = dist_ranks[i]

    # Deduplicate outlier drops across sourcing runs (same drop may appear in
    # both the global and domestic solve)
    seen_drops = set()
    outlier_drops_out = []
    for d in all_outlier_drops:
        key = (d.component_id, d.dropped_distributor_id)
        if key not in seen_drops:
            seen_drops.add(key)
            outlier_drops_out.append(schemas.OutlierDropLog(
                component_id=d.component_id, mpn=d.mpn,
                dropped_distributor_id=d.dropped_distributor_id,
                dropped_price_usd=d.dropped_price_usd,
                median_price_usd=d.median_price_usd,
                reason=d.reason,
            ))

    return schemas.MultiRouteResponse(
        alternatives=alternatives,
        recommended_id="balanced",
        outlier_drops=outlier_drops_out,
        strategy_divergence=_strategy_divergence(alternatives),
    )


def _strategy_divergence(
    alternatives: List[schemas.RouteAlternative],
) -> schemas.StrategyDivergence:
    """Report how many genuinely distinct plans the strategies produced.

    Grouping is on the SOURCING ASSIGNMENT SET — the (component_id,
    distributor_id, quantity) triples — because that is what makes two plans the
    same plan. Grouping on cost would merge plans that merely happen to price
    alike and would split identical plans whose costs differ by a rounding step.

    This exists so the UI can say the degenerate case out loud instead of the
    backend inventing rank differences to imply choice where there is none.
    """
    groups: Dict[tuple, List[str]] = {}
    for a in alternatives:
        key = tuple(sorted(
            (s.component_id, s.distributor_id, s.quantity) for s in a.sourcing
        ))
        groups.setdefault(key, []).append(a.id)

    identical = [ids for ids in groups.values() if len(ids) > 1]
    distinct = len(groups)
    total = len(alternatives)
    all_identical = distinct == 1 and total > 1

    if all_identical:
        note = (
            f"All {total} strategies converge on the same sourcing plan for this "
            f"cart. The fixed per-supplier freight charge dominates at this size, "
            f"so there is nothing left for the cost/time/carbon weightings to "
            f"trade off — the alternatives are one plan shown four times, not "
            f"four options."
        )
    elif identical:
        merged = "; ".join("/".join(ids) for ids in identical)
        note = (
            f"{distinct} distinct sourcing plans across {total} strategies. "
            f"These strategies buy exactly the same parts from the same "
            f"suppliers: {merged}. Where their headline numbers are also equal "
            f"they share ranks; any remaining difference between them comes from "
            f"Stage 3, where each strategy's own weighting can accept or reject "
            f"cross-dock consolidation differently on the same sourcing plan."
        )
    else:
        note = (
            f"All {total} strategies produced distinct sourcing plans — the "
            f"weightings genuinely trade off at this cart size."
        )

    return schemas.StrategyDivergence(
        total_strategies=total,
        distinct_plans=distinct,
        identical_groups=identical,
        all_identical=all_identical,
        note=note,
    )
