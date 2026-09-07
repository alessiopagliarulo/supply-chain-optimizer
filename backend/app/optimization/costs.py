"""
Freight cost + carbon + holding cost model.

All constants are cited from published industry sources. See
docs/OPTIMIZATION_DESIGN.md §5.1 for full
references.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional

from app.optimization.constants import (
    KM_PER_MILE, LBS_PER_KG, CWT_PER_LB,
    TL_RATE_USD_PER_MILE, LTL_BASE_FEE_USD, LTL_RATE_USD_PER_CWT_MILE,
    GROUND_KM_PER_DAY, CO2_G_PER_TON_MILE,
)

# Gartner IT Supply Chain Benchmarks 2022 — electronics annual holding cost
ANNUAL_HOLDING_RATE = 0.25

# ATA Cross-Docking Best Practices 2019 — midpoint of $30-$80 range
HUB_HANDLING_FEE_USD = 50.0

# BTS Intermodal Freight Transportation Model
HUB_DWELL_DAYS = 0.5

# FTL threshold: 10,000 lbs industry convention
TL_THRESHOLD_KG = 4536.0  # 10,000 lbs

# Distributor tier → handling days (proxy; data lacks SLAs)
HANDLING_DAYS_BY_TIER = {"major": 1, "mid": 2, "broker": 3}

# Average weight per electronic component unit (rough; used for BOM totals)
AVG_COMPONENT_KG = 0.05

# ── Freight CO2 denominators — READ THIS BEFORE TOUCHING ``co2_kg`` ──────────
#
# US and international freight factors are denominated in DIFFERENT tons, and
# the word "ton" alone does not say which:
#
#   TRUCK (EPA)  ``constants.CO2_G_PER_TON_MILE = 161.8`` is per US SHORT
#                ton-mile (2,000 lb = 907.18474 kg). EPA's own GHG Emission
#                Factors Hub prints the units for this factor family verbatim as
#                "short ton-mile" (2025 Hub, Table 8 "Scope 3 Category 4:
#                Upstream Transportation and Distribution": Medium- and
#                Heavy-Duty Truck 0.186, Rail 0.021, Waterborne 0.077, Aircraft
#                1.086 kg CO2 per short ton-mile). The ton-mile denominator is
#                BTS National Transportation Statistics Table 1-50, which is a
#                US short-ton series. The 2023 Hub printed the same column as a
#                bare "ton-mile", which is exactly how this ambiguity got in.
#                https://www.epa.gov/climateleadership/ghg-emission-factors-hub
#                Independent check: BTS states "1.459972 tonne-kilometers = 1 ton
#                mile", which only balances if a ton is 907.185 kg
#                (0.907185 x 1.60934 = 1.459972).
#
#                VINTAGE CAVEAT (unresolved, do not silently "correct" the
#                value): 161.8 is NOT in any edition of the Hub — Table 8 shows
#                170 (2023), 168 (2024), 186 (2025) g CO2/short ton-mile. 161.8
#                traces to EDF's 2014 Green Freight Handbook p.11, citing "EPA
#                SmartWay Shipper Partner Tool: Technical Documentation, 2013",
#                where the units are printed as "grams per short ton-mile" —
#                so the short-ton basis is confirmed for THIS figure directly,
#                not merely by US convention. The label "EPA SmartWay 2023"
#                used across this repo was the wrong vintage; the number is a
#                2013 SmartWay figure. RESOLVED 2026-09-03: the value is kept
#                and every published label now names the 2013 SmartWay technical
#                documentation and the EDF handbook as the route by which it is
#                cited (constants.py, solve.py citations, OPTIMIZATION_DESIGN.md).
#
#   AIR (GLEC)   ``solve.CO2_AIR_KG_PER_KG_KM = 0.0005`` is 0.5 kg CO2e per
#                METRIC tonne-km. IATA/GLEC air factors are metric, so that one
#                needs no conversion — kg/kg-km is dimensionless in mass.
#                Relabelled 2026-09-03 from "ICAO 2023" to GLEC Framework v3.2
#                (long-haul dedicated-freighter tank-to-wheel, 503 g CO2e/
#                tonne-km); ICAO publishes no static air-freight table. See the
#                attribution note above ``solve.CO2_AIR_KG_PER_KG_KM``.
#
# Dividing weight_kg by 1000 here (i.e. treating the EPA factor as per metric
# tonne) under-charges every truck leg by 9.28% of the correct value.
KG_PER_SHORT_TON = 907.18474


# ── Core functions ───────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two (lat, lon) points in kilometers."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def km_to_miles(km: float) -> float:
    return km / KM_PER_MILE


def transport_cost_usd(distance_km: float, weight_kg: float) -> float:
    """
    Returns USD cost for a single leg.

    Uses TL rate (ATRI 2023) when weight ≥ 10,000 lbs, otherwise LTL
    (FreightWaves SONAR / Old Dominion tariff). See spec §5.1.
    """
    miles = km_to_miles(distance_km)
    if weight_kg >= TL_THRESHOLD_KG:
        return miles * TL_RATE_USD_PER_MILE
    weight_lbs = weight_kg * LBS_PER_KG
    weight_cwt = weight_lbs * CWT_PER_LB
    return LTL_BASE_FEE_USD + weight_cwt * miles * LTL_RATE_USD_PER_CWT_MILE


def transit_days(distance_km: float) -> int:
    """Ground freight transit time (BTS CFS 2022: 800 km/day effective)."""
    return math.ceil(distance_km / GROUND_KM_PER_DAY)


def leg_lead_time_days(distance_km: float, distributor_tier: str) -> float:
    """Total lead time = distributor handling + ground transit."""
    handling = HANDLING_DAYS_BY_TIER.get(distributor_tier, 2)
    return handling + transit_days(distance_km)


# ── International air freight transit model ──────────────────────────────────
#
# Replaces a flat ``AIR_TRANSIT_DAYS = 4`` that was applied to EVERY international
# shipment regardless of origin, while domestic trucking accrued real per-km
# transit time. A Singapore supplier and a Toronto supplier were given the same
# transit time, which is how "Fastest Delivery" ended up slower than a plan that
# air-freighted from Shenzhen.
#
# Door-to-door air freight time = fixed origin handling + uplift wait + flight
# time over the actual great-circle distance + destination clearance.
#
#   AIR_EXPORT_HANDLING_DAYS   forwarder booking, pickup at the distributor,
#                              export declaration and airline acceptance cut-off.
#                              Forwarder published schedules (DHL Global
#                              Forwarding / Flexport air service descriptions)
#                              put origin handling for consolidated general cargo
#                              at 1-2 days; 2.0 is the mid/conservative point.
#   AIR_UPLIFT_WAIT_DAYS       wait for the next scheduled freighter rotation
#                              with available capacity. IATA's 2023 cargo market
#                              reports load factors around 45-55%, so space is
#                              normally available on the next rotation rather
#                              than the same day.
#   AIR_IMPORT_CLEARANCE_DAYS  US CBP entry filing, release, de-consolidation and
#                              hand-off to final mile — 1-3 days in CBP's own
#                              published entry-summary timelines; 2.0 is mid.
#   AIR_BLOCK_SPEED_KMH        Boeing 777F / 747-8F cruise is ~890-910 km/h;
#                              block speed (gate to gate, including taxi, climb
#                              and descent) is conventionally ~10% below cruise.
#   AIR_ROUTE_CIRCUITY         flown track vs great-circle distance. ICAO's
#                              Global Air Navigation Plan reports 8-12%
#                              horizontal flight inefficiency on long-haul
#                              routings.
#
# The constant part sums to 6.0 days; a 11,000 km Shenzhen → US West Coast lane
# lands at ~6.6 days door-to-door, inside the 5-8 day band published for standard
# (non-express) international air freight.
AIR_EXPORT_HANDLING_DAYS = 2.0
AIR_UPLIFT_WAIT_DAYS = 2.0
AIR_IMPORT_CLEARANCE_DAYS = 2.0
AIR_BLOCK_SPEED_KMH = 800.0
AIR_ROUTE_CIRCUITY = 1.10

AIR_FIXED_HANDLING_DAYS = (
    AIR_EXPORT_HANDLING_DAYS + AIR_UPLIFT_WAIT_DAYS + AIR_IMPORT_CLEARANCE_DAYS
)


def air_flight_days(distance_km: float) -> float:
    """Pure flight time for ``distance_km`` of great-circle separation, in days."""
    if distance_km <= 0.0:
        return 0.0
    return (distance_km * AIR_ROUTE_CIRCUITY) / (AIR_BLOCK_SPEED_KMH * 24.0)


def air_transit_days(distance_km: float) -> float:
    """Door-to-door international air freight lead time in days.

    Monotonically increasing in origin→destination distance, so two international
    suppliers on different continents no longer receive an identical ETA.
    """
    return AIR_FIXED_HANDLING_DAYS + air_flight_days(distance_km)


def co2_kg(distance_km: float, weight_kg: float) -> float:
    """Truck-freight carbon emissions in kg CO2e, from EPA's 161.8 g/ton-mile.

    The factor is per US SHORT ton-mile, so the payload is converted with
    ``KG_PER_SHORT_TON`` (907.18474), NOT with a metric 1000. See the unit note
    above that constant for the EPA source and why this matters.
    """
    miles = km_to_miles(distance_km)
    short_tons = weight_kg / KG_PER_SHORT_TON
    return short_tons * miles * (CO2_G_PER_TON_MILE / 1000.0)


def holding_cost_usd(inventory_value_usd: float, lead_time_days: float) -> float:
    """Gartner 2022: electronics annual holding rate 25%."""
    return inventory_value_usd * ANNUAL_HOLDING_RATE * (lead_time_days / 365.0)


@dataclass(frozen=True)
class FactoryLeadTime:
    """Result of an ML factory-lead-time query — including an honest refusal."""
    days: Optional[float]
    available: bool
    reason: Optional[str] = None      # why it is unavailable, when it is
    model_name: Optional[str] = None
    model_source: Optional[str] = None


def ml_factory_lead_time_days(record: "Mapping[str, object]") -> FactoryLeadTime:
    """ML prediction of the FACTORY (replenishment) lead time for a part, in days.

    WHAT THIS IS NOT
    ----------------
    This is *not* a delivery ETA. The model is trained on the factory lead time
    DigiKey publishes for a part — how long it takes to *make and restock* one —
    which is a supply-risk signal that is published whether or not the part is on
    the shelf. ``leg_lead_time_days`` (handling + ground transit) is the delivery
    ETA for a unit that ships from stock. Substituting one for the other, as
    ``solve.py`` used to, compares two different quantities.

    ``record`` is a plain mapping keyed by the ``record_key``s declared in
    ``app/ml/lead_time_model``: ``category``, ``manufacturer``,
    ``lifecycle_status``, ``stock``, ``unit_price``, ``moq``, ``standard_pack``,
    ``packaging``, ``is_normally_stocked``. Pass everything available — the
    resolved schema decides which keys it consumes, so this call site does not
    change when the feature set grows. Distance, tier, domesticity, risk score
    and Chinese-origin used to be passed here and were silently ignored (the
    training panel contains none of them), so they are gone.

    Returns a :class:`FactoryLeadTime`. ``available=False`` (with a reason) when:
      * no ML artifacts are loaded;
      * the persisted feature schema is not the one this code builds;
      * the record lacks a value the schema requires;
      * the category is outside the trained vocabulary.
    Callers must handle the refusal rather than receive a made-up number.
    """
    try:
        from app.ml import get_ml_state
        from app.ml.lead_time_model import predict_lead_time
        from app.ml.serving import get_serving_model, model_source
        from app.startup import wait_for_ml

        # The ML artifact load moved off the ASGI lifespan onto a background thread
        # (app/startup.py) to get the cold start down. Wait for it before reading the
        # global: without this, a request landing mid-warm-up would fall into the
        # "no lead-time model loaded" refusal below and the optimiser would price a
        # BOM with no factory lead time — a different published number, not just a
        # slower one. No-op when no warm-up is running.
        wait_for_ml()
        state = get_ml_state()
        # get_serving_model returns the MLflow champion when a registry was reachable
        # at startup, else the best model from the committed joblib (app/ml/serving.py).
        model = get_serving_model(state)
        if state is None or model is None or not state.feature_columns:
            return FactoryLeadTime(None, False, "no lead-time model loaded")
        days = predict_lead_time(model, dict(record), state.feature_columns)
        return FactoryLeadTime(
            days=days,
            available=True,
            model_name=state.best_lead_time_model,
            model_source=model_source(state),
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes an honest refusal
        return FactoryLeadTime(None, False, f"{type(exc).__name__}: {exc}")


# Port coordinates for haversine matching
_PORT_COORDS = {
    "LA_LB": (33.74, -118.27),     # Port of Los Angeles / Long Beach
    "NY_NJ": (40.67, -74.04),      # Port of New York / New Jersey
    "SAVANNAH": (32.08, -81.09),   # Port of Savannah
}
# Max delay days per port (per D-02 CONTEXT.md)
_PORT_MAX_DELAY = {
    "LA_LB": 3.0,
    "NY_NJ": 2.0,
    "SAVANNAH": 1.5,
}


def _port_delay_days(
    distributor_lat: float,
    distributor_lon: float,
    cache: "object | None",
) -> float:
    """
    Additive lead time delay from port congestion. Per D-02 from CONTEXT.md.

    Maps distributor to nearest monitored US port by haversine distance.
    Congestion ratio > 1.0 means fewer port calls than baseline (ships waiting).
    Delay = (congestion_ratio - 1.0) * port_max_delay, clamped to [0, max].

    Returns 0.0 when cache is None or portwatch data unavailable.
    """
    if cache is None:
        return 0.0
    if getattr(cache, 'portwatch', None) is None or cache.portwatch.data is None:
        return 0.0

    congestion_data = cache.portwatch.data  # {port_code: congestion_ratio}

    # Find nearest port
    nearest_port = None
    nearest_dist = float('inf')
    for port_code, (plat, plon) in _PORT_COORDS.items():
        d = haversine_km(distributor_lat, distributor_lon, plat, plon)
        if d < nearest_dist:
            nearest_dist = d
            nearest_port = port_code

    if nearest_port is None or nearest_port not in congestion_data:
        return 0.0

    congestion_ratio = congestion_data[nearest_port]
    max_delay = _PORT_MAX_DELAY.get(nearest_port, 1.5)
    # Ratio > 1.0 = congested; ratio <= 1.0 = normal
    delay = max(0.0, (congestion_ratio - 1.0) * max_delay)
    return min(delay, max_delay)


@dataclass(frozen=True)
class CostBreakdown:
    """Structured cost breakdown for a single strategy on a route."""
    component_cost: float
    transport_cost: float
    holding_cost: float

    @property
    def total(self) -> float:
        return self.component_cost + self.transport_cost + self.holding_cost
