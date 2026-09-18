"""Regression tests for freight and cross-dock defects fixed on 2026-08-16.

Each of these silently returned wrong-but-plausible numbers rather than
crashing, which is exactly the kind that comes back. One test per invariant.

  D2  International air freight was a flat 4 days from every origin on earth.
  D4  A cross-dock saving was advertised in `cross_dock` and never applied to
      the headline transport cost.

The orchestrator-level regressions (D1 Monte Carlo sampling, D3 ranking, D5
displayed legs) were removed with the sourcing optimizer; see git tag
`archive/sourcing-v1`.
"""
from __future__ import annotations

import pytest

from app.optimization.costs import (
    AIR_FIXED_HANDLING_DAYS, air_transit_days, co2_kg, haversine_km,
)
from app.optimization.cross_dock import (
    DistributorShipment, ObjectiveWeights, RouteMetrics, evaluate_cross_dock,
    evaluate_hub,
)
from app.optimization.freight_hubs import get_hub as _get_hub
from app.optimization.routing import GeoPoint

_ANY_HUB = _get_hub(5)  # Hartsfield-Jackson Cargo, Atlanta GA


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


# ── D4: cross-dock savings advertised but never applied ──────────────────────

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
    weights = ObjectiveWeights(w_cost=0.40, w_time=0.35, w_carbon=0.25)
    decision = evaluate_cross_dock(direct, shipments, depot, weights)

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
