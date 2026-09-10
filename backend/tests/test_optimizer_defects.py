"""Regression tests for four optimizer defects fixed on 2026-08-16.

Each of these silently returned wrong-but-plausible numbers rather than
crashing, which is exactly the kind that comes back. One test per invariant.

  D1  Monte Carlo: the published sample was the 200 SMALLEST of 1000 draws
      while being labelled "1000 simulations", so the chart's own p50/p90
      markers fell outside the range of the points plotted.
  D2  International air freight was a flat 4 days from every origin on earth,
      and its legs reported distance_km = 0.0 next to non-zero CO2 derived from
      the distance that had just been thrown away. `intl_time` was also assigned
      rather than max-ed inside the loop.
  D3  Ranks were assigned by sort position, so two byte-identical plans were
      presented as "3rd" and "4th".
  D4  A cross-dock saving was advertised in `cross_dock` and never applied to
      the headline transport cost.
"""
from __future__ import annotations

import pytest

from app.optimization.costs import (
    AIR_FIXED_HANDLING_DAYS, air_transit_days, co2_kg, haversine_km,
)
from app.optimization.cross_dock import (
    DistributorShipment, RouteMetrics, evaluate_cross_dock, evaluate_hub,
)
from app.optimization.routing import GeoPoint
from app.optimization.solve import (
    MONTE_CARLO_SAMPLE_POINTS, MONTE_CARLO_SEED, MONTE_CARLO_SIMULATIONS,
    DistributorMeta, _monte_carlo_eta, optimize_bom,
)
from app.optimization.sourcing import BomLine, Offer


# ── Fixtures ─────────────────────────────────────────────────────────────────

DEPOT = GeoPoint(lat=34.8526, lng=-82.3940)  # Greenville SC


@pytest.fixture
def mixed_bom():
    """A BOM whose cheapest offers are international, so the air-freight branch
    of `_build_route_data` is actually exercised — the pre-existing suite has no
    fixture with a non-domestic distributor at all."""
    bom = [
        BomLine(component_id=1, mpn="PART-A", quantity=100),
        BomLine(component_id=2, mpn="PART-B", quantity=50),
        BomLine(component_id=3, mpn="PART-C", quantity=30),
    ]
    offers = [
        Offer(1, 10, "EastCoastPrime", price_usd=4.20, stock=500, moq=1, is_domestic=True),
        Offer(1, 30, "MidwestMajor", price_usd=4.60, stock=500, moq=1, is_domestic=True),
        Offer(1, 70, "ShenzhenSupply", price_usd=1.10, stock=500, moq=1, is_domestic=False),
        Offer(2, 10, "EastCoastPrime", price_usd=5.00, stock=500, moq=1, is_domestic=True),
        Offer(2, 20, "SoutheastMid", price_usd=4.50, stock=500, moq=1, is_domestic=True),
        Offer(2, 80, "SingaporeParts", price_usd=1.30, stock=500, moq=1, is_domestic=False),
        Offer(3, 20, "SoutheastMid", price_usd=8.00, stock=500, moq=1, is_domestic=True),
        Offer(3, 40, "DiscountBrokerEast", price_usd=6.50, stock=500, moq=1, is_domestic=True),
        Offer(3, 70, "ShenzhenSupply", price_usd=2.10, stock=500, moq=1, is_domestic=False),
    ]
    distributors = {
        10: DistributorMeta(10, "EastCoastPrime", 35.7796, -78.6382, "Raleigh", "NC", "USA", True, "major"),
        20: DistributorMeta(20, "SoutheastMid", 33.7490, -84.3880, "Atlanta", "GA", "USA", True, "mid"),
        30: DistributorMeta(30, "MidwestMajor", 41.8781, -87.6298, "Chicago", "IL", "USA", True, "major"),
        40: DistributorMeta(40, "DiscountBrokerEast", 40.7128, -74.0060, "New York", "NY", "USA", True, "broker"),
        70: DistributorMeta(70, "ShenzhenSupply", 22.5431, 114.0579, "Shenzhen", None, "CHN", False, "major"),
        80: DistributorMeta(80, "SingaporeParts", 1.3521, 103.8198, "Singapore", None, "SGP", False, "mid"),
    }
    return bom, offers, distributors, DEPOT


@pytest.fixture
def domestic_bom():
    """All-domestic BOM — exercises the direct-tour / cross-dock path."""
    bom = [
        BomLine(component_id=1, mpn="PART-A", quantity=100),
        BomLine(component_id=2, mpn="PART-B", quantity=50),
        BomLine(component_id=3, mpn="PART-C", quantity=30),
    ]
    offers = [
        Offer(1, 10, "EastCoastPrime", price_usd=1.20, stock=500, moq=1, is_domestic=True),
        Offer(1, 30, "MidwestMajor", price_usd=2.60, stock=500, moq=1, is_domestic=True),
        Offer(1, 50, "DiscountBrokerWest", price_usd=2.20, stock=500, moq=1, is_domestic=True),
        Offer(2, 10, "EastCoastPrime", price_usd=5.00, stock=500, moq=1, is_domestic=True),
        Offer(2, 20, "SoutheastMid", price_usd=2.50, stock=500, moq=1, is_domestic=True),
        Offer(3, 20, "SoutheastMid", price_usd=8.00, stock=500, moq=1, is_domestic=True),
        Offer(3, 40, "DiscountBrokerEast", price_usd=4.50, stock=500, moq=1, is_domestic=True),
        Offer(3, 50, "DiscountBrokerWest", price_usd=6.00, stock=500, moq=1, is_domestic=True),
    ]
    distributors = {
        10: DistributorMeta(10, "EastCoastPrime", 35.7796, -78.6382, "Raleigh", "NC", "USA", True, "major"),
        20: DistributorMeta(20, "SoutheastMid", 33.7490, -84.3880, "Atlanta", "GA", "USA", True, "mid"),
        30: DistributorMeta(30, "MidwestMajor", 41.8781, -87.6298, "Chicago", "IL", "USA", True, "major"),
        40: DistributorMeta(40, "DiscountBrokerEast", 40.7128, -74.0060, "New York", "NY", "USA", True, "broker"),
        50: DistributorMeta(50, "DiscountBrokerWest", 34.0522, -118.2437, "Los Angeles", "CA", "USA", True, "broker"),
    }
    return bom, offers, distributors, DEPOT


# ── D1: Monte Carlo sample truncation ────────────────────────────────────────

def test_monte_carlo_sample_spans_the_whole_distribution():
    """The published sample must cover the distribution its own percentiles
    describe. `samples[:200]` (the 200 smallest of 1000) did not: every point was
    below p10, so a histogram of it could not contain the p50/p90 markers."""
    mc = _monte_carlo_eta(10.0)
    samples = mc["samples"]

    assert len(samples) == MONTE_CARLO_SAMPLE_POINTS == 200
    assert mc["n_simulations"] == MONTE_CARLO_SIMULATIONS == 1000
    assert "quantile" in mc["sample_kind"].lower()

    # The exact regression: the sample must reach past p90 and down past p10.
    assert max(samples) >= mc["p90"], (
        f"max sample {max(samples)} < p90 {mc['p90']} — the published sample "
        f"does not contain the distribution's own upper marker"
    )
    assert min(samples) <= mc["p10"]
    assert min(samples) <= mc["p50"] <= max(samples)
    # Evenly-spaced quantiles of a sorted array come back sorted.
    assert samples == sorted(samples)
    # And roughly 80% of the points must sit inside p10..p90, as they would in
    # any faithful down-sample. The old slice put 100% of them below p10.
    inside = sum(1 for s in samples if mc["p10"] <= s <= mc["p90"])
    assert 0.70 <= inside / len(samples) <= 0.90


def test_monte_carlo_is_deterministic_for_the_same_eta():
    """Two strategies that converge on the same plan have the same base ETA and
    must get the same ETA distribution. When this drew on the global RNG, the
    sampling wobble alone gave identical plans eta_p50 5.6 vs 5.7 and the speed
    ranking then called one of them faster than the other."""
    a = _monte_carlo_eta(7.25)
    b = _monte_carlo_eta(7.25)
    assert (a["p10"], a["p50"], a["p90"]) == (b["p10"], b["p50"], b["p90"])
    assert a["samples"] == b["samples"]


def test_monte_carlo_samples_are_published_on_the_response(domestic_bom):
    bom, offers, distributors, depot = domestic_bom
    resp = optimize_bom(bom, offers, distributors, depot)
    for alt in resp.alternatives:
        assert len(alt.monte_carlo_samples) == 200
        assert alt.monte_carlo_n_simulations == 1000
        assert alt.monte_carlo_sample_kind
        assert max(alt.monte_carlo_samples) >= alt.eta_p90
        assert min(alt.monte_carlo_samples) <= alt.eta_p10


def test_monte_carlo_does_not_touch_the_global_rng():
    """It must draw from an isolated Random (the stochastic.py house pattern), so
    a route optimisation cannot perturb any other seeded computation."""
    import random as _random
    _random.seed(1234)
    before = _random.random()
    _random.seed(1234)
    _monte_carlo_eta(9.0)
    after = _random.random()
    assert before == after


def test_monte_carlo_publishes_its_seed_and_labels_its_assumptions(domestic_bom):
    """The band is seeded and reproducible, and the parameters behind it are
    ASSUMED. Both facts have to be in the response — "Monte Carlo simulation
    (1,000 scenarios)" otherwise reads as a measured service-level distribution
    when nothing in this repo calibrates it."""
    bom, offers, distributors, depot = domestic_bom
    resp = optimize_bom(bom, offers, distributors, depot)
    for alt in resp.alternatives:
        assert alt.monte_carlo_seed == MONTE_CARLO_SEED
        a = alt.monte_carlo_assumptions
        assert a is not None
        assert a.calibrated is False, (
            "flip this only when the parameters are fitted to observed shipment "
            "data — and cite it in the caveat when you do"
        )
        assert a.seed == MONTE_CARLO_SEED
        assert a.transit_multiplier_sigma > 0
        assert len(a.disruption_delay_days) == len(a.disruption_weights)
        assert sum(a.disruption_weights) == pytest.approx(1.0)
        assert "not measured" in a.caveat.lower()


def test_optimize_bom_percentiles_are_reproducible(domestic_bom):
    bom, offers, distributors, depot = domestic_bom
    first = optimize_bom(bom, offers, distributors, depot)
    second = optimize_bom(bom, offers, distributors, depot)
    for a, b in zip(first.alternatives, second.alternatives):
        assert (a.eta_p10, a.eta_p50, a.eta_p90) == (b.eta_p10, b.eta_p50, b.eta_p90)
        assert a.monte_carlo_samples == b.monte_carlo_samples


# ── D2: international transit time and distance ──────────────────────────────

def test_air_transit_days_depends_on_distance():
    """A flat 4 days for every international origin is what let a Singapore +
    Shenzhen plan out-run five US distributors."""
    near = air_transit_days(1_000.0)      # e.g. Toronto
    far = air_transit_days(15_000.0)      # e.g. Singapore
    assert far > near, "air transit must grow with origin distance"
    assert air_transit_days(0.0) == pytest.approx(AIR_FIXED_HANDLING_DAYS)
    # Sanity band: published door-to-door standard air freight is ~5-8 days.
    assert 5.0 <= air_transit_days(11_000.0) <= 8.0


def test_air_legs_report_the_distance_their_carbon_came_from(mixed_bom):
    """`distance_km=0.0` was hard-coded on air legs while leg_co2e_kg was derived
    from the haversine distance computed two lines earlier. A plan therefore
    reported thousands of kg of CO2e over a total distance of 0.0 km."""
    bom, offers, distributors, depot = mixed_bom
    resp = optimize_bom(bom, offers, distributors, depot)

    cheapest = next(a for a in resp.alternatives if a.id == "cheapest")
    assert cheapest.international_stops > 0, "fixture must exercise the air branch"

    intl_ids = {did for did, d in distributors.items() if not d.is_domestic}
    air_legs = [s for s in cheapest.route if s.distributor_id in intl_ids]
    assert air_legs

    for leg in air_legs:
        d = distributors[leg.distributor_id]
        expected_km = haversine_km(depot.lat, depot.lng, d.lat, d.lng)
        assert leg.distance_km == pytest.approx(expected_km, abs=0.15)
        # Non-zero carbon requires non-zero distance, and vice versa.
        assert (leg.leg_co2e_kg > 0) == (leg.distance_km > 0)

    assert cheapest.total_distance_km > 0.0
    assert cheapest.total_co2e_kg > 0.0


def test_international_lead_time_takes_the_slowest_origin(mixed_bom):
    """`intl_time = AIR_TRANSIT_DAYS` ASSIGNED inside the loop, so with the flat
    constant removed the last distributor iterated would have won instead of the
    slowest. The plan cannot arrive before its slowest consignment."""
    bom, offers, distributors, depot = mixed_bom
    resp = optimize_bom(bom, offers, distributors, depot)

    cheapest = next(a for a in resp.alternatives if a.id == "cheapest")
    intl_ids = {
        s.distributor_id for s in cheapest.route
        if s.distributor_id in distributors and not distributors[s.distributor_id].is_domestic
    }
    assert len(intl_ids) >= 2, "fixture must select at least two origins"

    slowest = max(
        air_transit_days(haversine_km(
            depot.lat, depot.lng,
            distributors[did].lat, distributors[did].lng,
        ))
        for did in intl_ids
    )
    assert cheapest.base_eta_days >= round(slowest, 1) - 0.05


def test_fastest_delivery_is_not_slower_than_lowest_cost(mixed_bom, domestic_bom):
    """The headline symptom: "Fastest Delivery" was measurably slower than
    "Lowest Cost", and speed_rank agreed with it."""
    for fixture in (mixed_bom, domestic_bom):
        bom, offers, distributors, depot = fixture
        resp = optimize_bom(bom, offers, distributors, depot)
        by_id = {a.id: a for a in resp.alternatives}
        best_eta = min(a.eta_p50 for a in resp.alternatives)
        assert by_id["fastest"].eta_p50 == pytest.approx(best_eta), (
            "the strategy labelled 'Fastest Delivery' must have the lowest "
            f"eta_p50: got {by_id['fastest'].eta_p50} vs best {best_eta}"
        )
        assert by_id["fastest"].speed_rank == 1


def test_speed_rank_agrees_with_eta_p50(mixed_bom):
    bom, offers, distributors, depot = mixed_bom
    resp = optimize_bom(bom, offers, distributors, depot)
    for a in resp.alternatives:
        for b in resp.alternatives:
            if a.eta_p50 < b.eta_p50:
                assert a.speed_rank < b.speed_rank
            if a.eta_p50 == b.eta_p50:
                assert a.speed_rank == b.speed_rank


# ── D3: fabricated rank differences between identical plans ──────────────────

def _plan_key(alt):
    return tuple(sorted(
        (s.component_id, s.distributor_id, s.quantity) for s in alt.sourcing
    ))


def test_equal_values_get_equal_ranks_and_unequal_values_do_not(
    domestic_bom, mixed_bom,
):
    """Standard competition ranking (1, 2, 2, 4): rank equality must be exactly
    value equality. The old helper numbered by sort position, so two identical
    plans came back as "3rd cheapest" and "4th cheapest".

    Note this is asserted on VALUES, not on sourcing plans: two strategies can
    share a sourcing plan and still differ downstream, because each applies its
    own weighting to the cross-dock accept/reject decision. That is real
    divergence, not invented — see test_strategy_divergence_* for how it is
    reported.
    """
    fields = [
        ("cost_rank", lambda a: a.total_cost_usd),
        ("speed_rank", lambda a: a.eta_p50),
        ("carbon_rank", lambda a: a.total_co2e_kg),
        ("distance_rank", lambda a: a.total_distance_km),
    ]
    for fixture in (domestic_bom, mixed_bom):
        bom, offers, distributors, depot = fixture
        resp = optimize_bom(bom, offers, distributors, depot)
        for field, value in fields:
            for a in resp.alternatives:
                for b in resp.alternatives:
                    va, vb = round(value(a), 6), round(value(b), 6)
                    ra, rb = getattr(a, field), getattr(b, field)
                    if va == vb:
                        assert ra == rb, (
                            f"{field}: {a.id} and {b.id} both have {va} but were "
                            f"ranked {ra} and {rb}"
                        )
                    elif va < vb:
                        assert ra < rb


def test_strategies_sharing_a_sourcing_plan_share_ranks_when_values_match(
    domestic_bom,
):
    """The concrete reported symptom: greenest and balanced produced the same
    cost, the same CO2, the same 7 sourcing lines and the same route — and were
    labelled cost_rank 3 vs 4 and carbon_rank 2 vs 3."""
    bom, offers, distributors, depot = domestic_bom
    resp = optimize_bom(bom, offers, distributors, depot)
    for a in resp.alternatives:
        for b in resp.alternatives:
            if _plan_key(a) != _plan_key(b):
                continue
            if a.total_cost_usd == pytest.approx(b.total_cost_usd, abs=0.005):
                assert a.cost_rank == b.cost_rank
            if a.total_co2e_kg == pytest.approx(b.total_co2e_kg, abs=0.005):
                assert a.carbon_rank == b.carbon_rank


def test_ranks_use_competition_ranking_not_sort_position(domestic_bom):
    """Equal values share a rank and the next distinct value skips — so the set
    of ranks is never a bare 1,2,3,4 ladder when values tie."""
    bom, offers, distributors, depot = domestic_bom
    resp = optimize_bom(bom, offers, distributors, depot)
    for field, value in [
        ("cost_rank", lambda a: a.total_cost_usd),
        ("carbon_rank", lambda a: a.total_co2e_kg),
        ("distance_rank", lambda a: a.total_distance_km),
    ]:
        pairs = sorted(
            ((value(a), getattr(a, field)) for a in resp.alternatives),
            key=lambda t: t[0],
        )
        for i, (v, r) in enumerate(pairs, start=1):
            n_strictly_better = sum(1 for v2, _ in pairs if v2 < v)
            assert r == n_strictly_better + 1, (
                f"{field}: value {v} ranked {r}, competition rank is "
                f"{n_strictly_better + 1}"
            )


def test_strategy_divergence_reports_the_degenerate_case(domestic_bom):
    """The response must SAY when strategies converged, rather than leaving the
    UI to infer difference from ranks that no longer differ."""
    bom, offers, distributors, depot = domestic_bom
    resp = optimize_bom(bom, offers, distributors, depot)
    dv = resp.strategy_divergence
    assert dv is not None
    assert dv.total_strategies == 4
    assert 1 <= dv.distinct_plans <= 4
    assert dv.note

    observed = {}
    for a in resp.alternatives:
        observed.setdefault(_plan_key(a), []).append(a.id)
    assert dv.distinct_plans == len(observed)
    assert dv.all_identical == (len(observed) == 1)
    expected_groups = sorted(
        sorted(ids) for ids in observed.values() if len(ids) > 1
    )
    assert sorted(sorted(g) for g in dv.identical_groups) == expected_groups


def test_single_line_cart_is_reported_as_degenerate_not_as_four_options():
    """On a one-line cart every strategy buys the same part from the same
    supplier. That must be stated, not dressed up as a 1-2-3-4 ranking."""
    bom = [BomLine(component_id=1, mpn="ONLY-PART", quantity=10)]
    offers = [
        Offer(1, 10, "EastCoastPrime", price_usd=1.20, stock=500, moq=1, is_domestic=True),
        Offer(1, 20, "SoutheastMid", price_usd=3.40, stock=500, moq=1, is_domestic=True),
    ]
    distributors = {
        10: DistributorMeta(10, "EastCoastPrime", 35.7796, -78.6382, "Raleigh", "NC", "USA", True, "major"),
        20: DistributorMeta(20, "SoutheastMid", 33.7490, -84.3880, "Atlanta", "GA", "USA", True, "mid"),
    }
    resp = optimize_bom(bom, offers, distributors, DEPOT)

    assert resp.strategy_divergence is not None
    assert resp.strategy_divergence.all_identical is True
    assert resp.strategy_divergence.distinct_plans == 1
    assert {a.cost_rank for a in resp.alternatives} == {1}
    assert {a.speed_rank for a in resp.alternatives} == {1}
    assert {a.carbon_rank for a in resp.alternatives} == {1}


# ── D4: cross-dock savings advertised but never applied ──────────────────────

def test_cross_dock_saving_is_either_charged_or_not_called_a_saving(
    domestic_bom, mixed_bom,
):
    """THE invariant. `fastest` used to report savings_vs_direct_pct 65.38 with
    direct 30,895.42 / consolidated 8,869.71 while total_transport_cost_usd was
    30,895.41 — the UN-consolidated figure."""
    for fixture in (domestic_bom, mixed_bom):
        bom, offers, distributors, depot = fixture
        resp = optimize_bom(bom, offers, distributors, depot)
        for alt in resp.alternatives:
            cd = alt.cross_dock
            assert cd is not None

            if cd.applied:
                assert alt.transport_cost_basis == "cross_dock_consolidated"
                assert alt.total_transport_cost_usd == pytest.approx(
                    cd.consolidated_cost_usd, abs=0.02
                ), (
                    f"{alt.id}: advertised a {cd.savings_vs_direct_pct}% saving "
                    f"but charged {alt.total_transport_cost_usd} instead of the "
                    f"consolidated {cd.consolidated_cost_usd}"
                )
                # The percentage must be the one the two cost figures imply.
                implied = round(
                    100.0 * (1.0 - cd.consolidated_cost_usd / cd.direct_cost_usd), 2
                )
                assert cd.savings_vs_direct_pct == pytest.approx(implied, abs=0.05)
                # A hub picked on time/carbon may cost MORE. That is allowed —
                # the headline cost moves with it — but it must not be worded as
                # a saving.
                if cd.savings_vs_direct_pct <= 0:
                    assert "not save money" in cd.rationale.lower() or \
                           "more" in cd.rationale.lower()
                    assert cd.consolidated_cost_usd >= cd.direct_cost_usd - 0.02
                # Carbon, distance and ETA describe the SAME plan as the cost.
                assert alt.total_co2e_kg == pytest.approx(cd.consolidated_co2e_kg, abs=0.01)
                assert alt.total_distance_km == pytest.approx(
                    cd.consolidated_distance_km, abs=0.15
                )
                assert alt.base_eta_days == pytest.approx(
                    cd.consolidated_eta_days, abs=0.05
                )
                assert alt.route_legs_note
            else:
                # Not applied ⇒ nothing may be reported as a realized saving.
                assert cd.savings_vs_direct_pct == 0.0
                assert alt.transport_cost_basis == "direct_pickup_tour"
                # …and the headline totals are exactly the displayed legs.
                assert alt.total_transport_cost_usd == pytest.approx(
                    sum(s.leg_cost_usd for s in alt.route), abs=0.02
                )
                assert alt.total_co2e_kg == pytest.approx(
                    sum(s.leg_co2e_kg for s in alt.route), abs=0.01
                )
                assert alt.total_distance_km == pytest.approx(
                    sum(s.distance_km for s in alt.route), abs=0.15
                )


def test_cost_breakdown_uses_the_charged_transport_cost(domestic_bom, mixed_bom):
    """Whatever transport cost the plan is charged must be the one that rolls up
    into total_cost_usd — no second, quieter number."""
    for fixture in (domestic_bom, mixed_bom):
        bom, offers, distributors, depot = fixture
        resp = optimize_bom(bom, offers, distributors, depot)
        for alt in resp.alternatives:
            assert alt.cost_breakdown is not None
            assert alt.cost_breakdown.transport_cost == pytest.approx(
                alt.total_transport_cost_usd, abs=0.02
            )
            assert alt.cost_breakdown.total == pytest.approx(
                alt.cost_breakdown.component_cost
                + alt.cost_breakdown.transport_cost
                + alt.cost_breakdown.holding_cost,
                abs=0.02,
            )
            assert alt.total_cost_usd == pytest.approx(alt.cost_breakdown.total, abs=0.02)


def test_cross_dock_compares_like_with_like_on_international_freight():
    """The hub can only consolidate domestic pickups, but the international air
    consignment is paid either way. Leaving it out of the hub plan while the
    direct plan carried it made the gap look like a consolidation saving."""
    depot = GeoPoint(lat=34.8526, lng=-82.3940)
    shipments = [
        DistributorShipment(10, "Raleigh", 35.7796, -78.6382, 5.0, "major"),
        DistributorShipment(20, "Atlanta", 33.7490, -84.3880, 5.0, "mid"),
    ]
    air = RouteMetrics(cost_usd=900.0, lead_time_days=6.5, co2_kg=40.0, distance_km=12_000.0)

    without_air = evaluate_hub(_ANY_HUB, depot, shipments)
    with_air = evaluate_hub(_ANY_HUB, depot, shipments, parallel=air)

    assert with_air.cost_usd == pytest.approx(without_air.cost_usd + 900.0)
    assert with_air.co2_kg == pytest.approx(without_air.co2_kg + 40.0)
    assert with_air.distance_km == pytest.approx(without_air.distance_km + 12_000.0)
    # Air freight moves in PARALLEL with the truck legs — time is a max, not a sum.
    assert with_air.lead_time_days == pytest.approx(
        max(without_air.lead_time_days, 6.5)
    )


def test_sub_threshold_hub_reports_a_candidate_not_a_saving():
    """A hub that fails the 5% test must report 0.0 realized saving while still
    disclosing what it would have been."""
    depot = GeoPoint(lat=34.8526, lng=-82.3940)
    shipments = [
        DistributorShipment(10, "Raleigh", 35.7796, -78.6382, 5.0, "major"),
        DistributorShipment(20, "Atlanta", 33.7490, -84.3880, 5.0, "mid"),
    ]
    # A direct baseline so cheap that no hub can beat it by 5%.
    direct = RouteMetrics(cost_usd=1.0, lead_time_days=0.1, co2_kg=0.01, distance_km=1.0)
    from app.optimization.strategies import get_strategy
    decision = evaluate_cross_dock(direct, shipments, depot, get_strategy("balanced"))

    assert decision.enabled is False
    assert decision.savings_vs_direct_pct == 0.0
    assert "threshold" in decision.rationale.lower()


def test_hub_metrics_report_the_distance_behind_their_carbon():
    depot = GeoPoint(lat=34.8526, lng=-82.3940)
    shipments = [
        DistributorShipment(10, "Raleigh", 35.7796, -78.6382, 5.0, "major"),
        DistributorShipment(20, "Atlanta", 33.7490, -84.3880, 5.0, "mid"),
    ]
    m = evaluate_hub(_ANY_HUB, depot, shipments)
    assert m.distance_km > 0.0
    assert m.co2_kg > 0.0
    # CO2 is derived from exactly those kilometres at the same EPA factor.
    expected = sum(
        co2_kg(haversine_km(s.lat, s.lng, _ANY_HUB.latitude, _ANY_HUB.longitude), s.weight_kg)
        for s in shipments
    ) + co2_kg(
        haversine_km(_ANY_HUB.latitude, _ANY_HUB.longitude, depot.lat, depot.lng),
        sum(s.weight_kg for s in shipments),
    )
    assert m.co2_kg == pytest.approx(expected)


from app.optimization.freight_hubs import get_hub as _get_hub  # noqa: E402

_ANY_HUB = _get_hub(5)  # Hartsfield-Jackson Cargo, Atlanta GA


# ── D5: the displayed legs described a heavier truck than the scored plan ────
#
# `solve.optimize_bom` built its route stops from a single `domestic_weight` —
# the WHOLE order, charged to every leg, including the outbound one on which the
# trailer is still empty. The optimizer's own scoring (`cross_dock.evaluate_direct`)
# had always accrued weight per stop. So the legs rendered on the map and in the
# checkout panel overstated the tour that ranked the four plans: measured on the
# deployed demo cart, +19.3% on cost ($405.77 vs $339.99) and +57.2% on CO2e
# (3.401 kg vs 2.163 kg). The display and the decision disagreed.
#
# Both now read from `cross_dock.pickup_tour_legs`, the one load model.

def _truck_legs(alt):
    """The truck-tour legs of an alternative, in order, return-to-depot last.

    Air-freight consignments are appended after the tour and fly alone, so they
    are not part of the accrual.
    """
    legs = []
    for stop in alt.route:
        legs.append(stop)
        if stop.distributor_id == 0:  # "Factory (Depot)" closes the tour
            break
    return legs


def test_the_outbound_leg_of_a_pickup_tour_is_driven_empty(domestic_bom):
    """A pickup tour is not a delivery tour run backwards.

    The truck leaves the depot with nothing aboard, so the FIRST leg carries
    0 kg and — because EPA ton-mile factors attribute emissions to freight
    carried — emits nothing. It still costs money: `transport_cost_usd` charges
    the LTL base fee regardless of weight, which is correct, moving a truck has
    a fixed cost. Before the fix this leg was charged the entire order.
    """
    from app.optimization.constants import LTL_BASE_FEE_USD

    bom, offers, distributors, depot = domestic_bom
    resp = optimize_bom(bom, offers, distributors, depot)

    for alt in resp.alternatives:
        legs = _truck_legs(alt)
        assert len(legs) >= 2, f"{alt.id}: expected a domestic pickup tour"
        outbound = legs[0]
        # The published cost/carbon come first: they are what the site shows, and
        # they are wrong on their own terms if the truck is billed for freight it
        # has not collected yet.
        assert outbound.leg_co2e_kg == 0.0, (
            f"{alt.id}: the empty outbound leg to {outbound.distributor_name} is "
            f"billed {outbound.leg_co2e_kg} kg CO2e of freight it is not carrying"
        )
        assert outbound.leg_cost_usd == pytest.approx(LTL_BASE_FEE_USD, abs=0.01), (
            f"{alt.id}: the empty outbound leg costs ${outbound.leg_cost_usd:,.2f}, "
            f"not the bare LTL base fee of ${LTL_BASE_FEE_USD:,.2f}"
        )
        assert outbound.leg_carried_kg == 0.0, (
            f"{alt.id}: the outbound leg to {outbound.distributor_name} reports "
            f"{outbound.leg_carried_kg} kg aboard before a single pickup"
        )


def test_displayed_leg_weights_accrue_stop_by_stop_up_to_the_full_order(domestic_bom):
    """Weight only ever goes up along the tour, and the return leg carries it all.

    This is the invariant the display path did not have: it applied one flat
    weight to every leg, so the profile was constant rather than accruing.
    """
    from app.optimization.costs import AVG_COMPONENT_KG

    bom, offers, distributors, depot = domestic_bom
    resp = optimize_bom(bom, offers, distributors, depot)
    order_kg = sum(line.quantity for line in bom) * AVG_COMPONENT_KG

    # Weight per unit distance implied by the PUBLISHED carbon of each leg. This
    # is derived from `leg_co2e_kg` and `distance_km` alone — fields that
    # predate the fix — so the accrual is checked against what the site actually
    # renders, not against a field the fix introduced.
    unit = co2_kg(1.0, 1.0)

    for alt in resp.alternatives:
        legs = _truck_legs(alt)
        implied = [round(leg.leg_co2e_kg / (unit * leg.distance_km), 3)
                   for leg in legs]

        assert implied[0] == 0.0, (
            f"{alt.id}: the published carbon implies {implied[0]} kg aboard on "
            f"the outbound leg"
        )
        assert implied != [implied[0]] * len(implied), (
            f"{alt.id}: every leg's carbon implies the same load — the profile "
            f"is flat, not accruing"
        )
        assert implied == sorted(implied), (
            f"{alt.id}: implied load profile {implied} decreases along the tour"
        )
        assert implied[-1] == pytest.approx(order_kg, abs=0.01), (
            f"{alt.id}: the return leg's carbon implies {implied[-1]} kg, not "
            f"the whole {order_kg} kg order"
        )

        # ...and the weight the response REPORTS agrees with the weight its own
        # carbon implies, so the two can never drift apart unnoticed again.
        assert legs[-1].distributor_id == 0, f"{alt.id}: tour does not end at the depot"
        carried = [leg.leg_carried_kg for leg in legs]
        assert carried == pytest.approx(implied, abs=0.01)
        assert legs[-1].leg_carried_kg == pytest.approx(order_kg, abs=1e-6)


@pytest.mark.parametrize("fixture_name", ["domestic_bom", "mixed_bom"])
def test_the_displayed_legs_sum_to_the_direct_tour_the_optimizer_scored(
    fixture_name, request
):
    """The legs on screen must BE the direct pickup tour that was scored.

    `cross_dock.direct_cost_usd` is the cost of the direct plan the hub decision
    was measured against — the same tour `route` draws. When the two disagree,
    the site is publishing one truck and deciding with another. They disagreed
    by up to 19% before the load model was shared.
    """
    bom, offers, distributors, depot = request.getfixturevalue(fixture_name)
    resp = optimize_bom(bom, offers, distributors, depot)

    for alt in resp.alternatives:
        assert alt.cross_dock is not None
        assert alt.route_leg_cost_usd == pytest.approx(
            alt.cross_dock.direct_cost_usd, abs=0.05
        ), (
            f"{alt.id}: legs sum to ${alt.route_leg_cost_usd:,.2f} but the direct "
            f"tour that was scored cost ${alt.cross_dock.direct_cost_usd:,.2f}"
        )


@pytest.mark.parametrize("fixture_name", ["domestic_bom", "mixed_bom"])
def test_a_direct_pickup_plan_publishes_the_numbers_it_was_ranked_on(
    fixture_name, request
):
    """When cross-dock is NOT applied, the headline IS the tour, exactly.

    `strategy_math.raw_objective_values` are the cost/carbon that went into the
    weighted objective and the ranking. For a `direct_pickup_tour` alternative
    the headline totals and the leg totals must both equal them — three views of
    one plan. Pre-fix the leg totals were a fourth, heavier plan that nothing
    had scored.
    """
    bom, offers, distributors, depot = request.getfixturevalue(fixture_name)
    resp = optimize_bom(bom, offers, distributors, depot)

    checked = 0
    for alt in resp.alternatives:
        if alt.transport_cost_basis != "direct_pickup_tour":
            continue
        checked += 1
        raw = alt.strategy_math.raw_objective_values
        assert alt.route_leg_cost_usd == pytest.approx(raw["cost"], abs=0.05)
        assert alt.route_leg_co2e_kg == pytest.approx(raw["carbon"], abs=0.005)
        assert alt.total_transport_cost_usd == pytest.approx(raw["cost"], abs=0.05)
        assert alt.total_co2e_kg == pytest.approx(raw["carbon"], abs=0.005)
    assert checked, "no direct_pickup_tour alternative in this fixture to check"
