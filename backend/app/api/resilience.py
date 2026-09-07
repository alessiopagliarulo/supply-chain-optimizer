"""
Resilience Scenario API endpoints (Phase 6).

Three POST endpoints for interactive "what if" scenario exploration:
  1. POST /resilience/distributor-failure — Simulate distributor outage via graph cascade
  2. POST /resilience/geopolitical-risk — Scale stored component risk scores by a multiplier
  3. POST /resilience/delivery-target — Simulate tight delivery constraint by re-pricing

All endpoints cache results (1h TTL) with deterministic SHA256 cache keys to meet <2s response time.
OpenTelemetry tracing logs slow spans (>500ms) for performance diagnostics.
No auth required; public API (aggregate metrics only, no prices/user data).
"""
import logging
from enum import StrEnum
from typing import List, Optional, Dict

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.distributor import Distributor
from app.models.component import Component, DistributorOffer
from app.cache import CacheManager
from app.graph import ensure_graph_state, get_graph_state
from app.graph.simulation import run_monte_carlo
from app.startup import wait_for_graph
from app.optimization.costs import haversine_km
from app.optimization.constants import GROUND_KM_PER_DAY
from app.optimization.recommendations import (
    compute_criticality_sweep,
    compute_dual_sourcing_plan,
    compute_tornado,
)
from dataclasses import asdict

# OpenTelemetry tracer setup (optional — no-op if not installed)
try:
    from opentelemetry import trace
    tracer = trace.get_tracer(__name__)
except ImportError:
    class _NoOpSpan:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def set_attribute(self, *_): pass
    class _NoOpTracer:
        def start_as_current_span(self, *_, **__): return _NoOpSpan()
    tracer = _NoOpTracer()
logger = logging.getLogger(__name__)
SLOW_PATH_THRESHOLD_MS = 500

router = APIRouter(prefix="/resilience", tags=["resilience"])


# ────────────────────────────────────────────────────────────────────────────
# Request / Response Models
# ────────────────────────────────────────────────────────────────────────────

MAX_BOM_LINES = 200
MAX_LINE_QUANTITY = 1_000_000


class BomLineIn(BaseModel):
    """One BOM line WITH its build quantity."""
    component_id: int = Field(..., ge=1)
    quantity: int = Field(..., ge=1, le=MAX_LINE_QUANTITY)


class _BomRequest(BaseModel):
    """
    Base for every scenario request. Carries the BOM in one of two forms.

    `items` is the real one: component id AND quantity per line. `bom_component_ids`
    is the legacy quantity-free form, kept so existing callers keep working — but a
    BOM with no quantities can only be priced at ONE UNIT PER LINE, which is why
    `/resilience/*` used to report a $177 baseline for a BOM that `/optimize/vrp`
    prices at $12k–$69k. When the legacy form is used the response says so explicitly
    (`quantity_source = "assumed_one_unit_per_line"`) instead of presenting a
    prototype-quantity figure as a procurement number.
    """
    bom_component_ids: List[int] = Field(
        default_factory=list,
        description=f"Component IDs in BOM (max {MAX_BOM_LINES}). Quantity is assumed "
                    "to be 1 per line — prefer `items` to state real quantities.",
    )
    items: Optional[List[BomLineIn]] = Field(
        None,
        description="BOM lines with explicit quantities. Takes precedence over "
                    "bom_component_ids when both are supplied.",
    )

    def resolved_lines(self) -> "Dict[int, int]":
        """component_id -> quantity, deduplicated (repeated ids sum)."""
        lines: Dict[int, int] = {}
        if self.items:
            for line in self.items:
                lines[line.component_id] = lines.get(line.component_id, 0) + line.quantity
        else:
            for cid in self.bom_component_ids:
                lines[cid] = lines.get(cid, 0) + 1
        return lines

    def quantity_source(self) -> str:
        return "explicit" if self.items else "assumed_one_unit_per_line"

    @model_validator(mode="after")
    def _check_bom(self):
        if not self.items and not self.bom_component_ids:
            raise ValueError("supply either `items` or a non-empty `bom_component_ids`")
        n_lines = len(self.items) if self.items else len(self.bom_component_ids)
        if n_lines > MAX_BOM_LINES:
            raise ValueError(f"BOM must not exceed {MAX_BOM_LINES} lines")
        return self


class DistributorFailureRequest(_BomRequest):
    distributor_id: int = Field(..., description="ID of distributor to simulate failure")


class GeopoliticalRiskRequest(_BomRequest):
    risk_multiplier: float = Field(
        ...,
        ge=0.5,
        le=5.0,
        description=(
            "Stress dial applied to each component's STORED risk_score column, and "
            "passed into the Monte Carlo as a scenario stress factor. It does NOT "
            "read or override any live feed — this endpoint imports no feed cache. "
            "The live GPR signal reaches the optimizer elsewhere, on /optimize/*."
        ),
    )


class DeliveryTargetRequest(_BomRequest):
    target_delivery_days: int = Field(..., ge=1, le=90, description="Target delivery timeframe")


class HedgingSummary(BaseModel):
    """
    Whether this BOM is structurally exposed to the scenario at all.

    A diversified BOM legitimately shows zero fulfillment impact when one distributor
    goes down — every line simply has an alternate. That is a genuine finding, but
    returning it as bare zeros with an empty `affected_bom_ids` is indistinguishable
    from a broken endpoint. This block says which it is.
    """
    n_bom_lines: int
    n_lines_with_alternate: int
    n_lines_orphaned: int
    orphaned_component_ids: List[int] = Field(default_factory=list)
    n_single_source_lines: int
    fully_hedged: bool
    # The fulfilment pair the statement is COMPOSED from, echoed so the claim and
    # the numbers that justify it travel together and can be checked against each
    # other without a second request. None only when the caller did not measure them.
    baseline_fulfillment_p50: Optional[float] = None
    scenario_fulfillment_p50: Optional[float] = None
    fulfillment_p50_delta_pts: Optional[float] = None
    statement: str


class CostSubstitution(BaseModel):
    """
    Where the cost delta actually comes from.

    The Monte Carlo can only price a line that becomes COMPLETELY unavailable (a flat
    emergency premium). It cannot express the ordinary consequence of losing a
    supplier: paying the next-cheapest offer instead. This block is that repricing —
    every line is re-costed against the offers that survive the scenario.
    """
    baseline_component_cost_usd: float
    scenario_component_cost_usd: float
    substitution_delta_usd: float
    n_lines_repriced: int
    n_lines_unpriceable: int
    largest_line_increase_usd: float
    largest_line_component_id: Optional[int] = None
    basis: str


class ScenarioResponse(BaseModel):
    """Common response shape for all three scenario endpoints."""
    baseline_cost_usd: float
    scenario_cost_usd: float
    cost_delta_pct: float
    baseline_eta_days: float
    scenario_eta_days: float
    eta_delta_days: float
    baseline_risk_score: float
    scenario_risk_score: float
    risk_delta: float
    # Dollar-denominated tail-risk framing (P3). CVaR-95 is the mean emergency-
    # procurement cost multiplier over the worst-5% Monte Carlo scenarios; the
    # spend-at-risk translates it into the extra USD a tail disruption would add
    # to this BOM's procurement bill = component_cost * (CVaR-95 - 1).
    baseline_cvar_95: float = 1.0
    procurement_spend_at_risk_usd: float = 0.0
    # `procurement_spend_at_risk_usd` and `affected_bom_ids` measure DIFFERENT things
    # and are routinely non-zero and empty respectively. This states which is which,
    # so the pair never reads as "spend at risk with nothing at risk".
    spend_at_risk_basis: str = ""
    # What the two ETA figures MEAN. They are the slowest line of the plan whose
    # cost is printed beside them — not the fastest supplier in the catalogue.
    eta_basis: str = ""
    baseline_fulfillment_p10: float
    baseline_fulfillment_p50: float
    baseline_fulfillment_p90: float
    scenario_fulfillment_p10: float
    scenario_fulfillment_p50: float
    scenario_fulfillment_p90: float
    affected_bom_ids: List[int]
    affected_suppliers: List[str]
    # Real per-alternative-supplier detail for the BOM impact table: each entry is
    # {"name": str, "lead_time_days": float}, lead time derived from real distributor
    # geography (no hardcoded per-supplier constants).
    alternative_suppliers: List[Dict] = Field(default_factory=list)
    # PER-COMPONENT detail for the BOM impact table, one entry per id in
    # `affected_bom_ids` and in the same order. Each is
    # {"component_id", "mpn", "current_supplier", "alternative_suppliers": [...]}.
    #
    # This exists because the table used to be built client-side from
    # `affected_bom_ids` alone: it printed the literal string "Primary" as every
    # line's supplier, and attached the BOM-WIDE `alternative_suppliers` list to
    # every row — so a line orphaned by the outage (no surviving supplier at all,
    # by the definition that put it in `affected_bom_ids`) was shown offering ten
    # "rerouting options" from distributors that never carried the part.
    # `alternative_suppliers` here is scoped to the component AND the scenario.
    affected_components: List[Dict] = Field(default_factory=list)
    # ── Quantities and structural context (2026-08 audit) ─────────────────────
    bom_quantities: Dict[str, int] = Field(default_factory=dict)
    quantity_source: str = "assumed_one_unit_per_line"
    total_units: int = 0
    cost_basis: str = ""
    hedging: Optional[HedgingSummary] = None
    cost_substitution: Optional[CostSubstitution] = None


class DeliveryTargetResponse(ScenarioResponse):
    """Extends common response with supplier capability lists."""
    suppliers_capable: List[Dict] = Field(default_factory=list)
    suppliers_cannot_meet: List[Dict] = Field(default_factory=list)
    # ── Item 7: the target must never be echoed back as the achieved ETA ──────
    target_delivery_days: int = 0
    target_met: bool = False
    target_is_binding: bool = False
    unmet_component_ids: List[int] = Field(default_factory=list)
    eta_note: str = ""


# ────────────────────────────────────────────────────────────────────────────
# Cache Utility Functions (refactored to use CacheManager)
# ────────────────────────────────────────────────────────────────────────────

def _compute_cache_key(scenario_type: str, **params) -> str:
    """Generate deterministic SHA256 cache key from scenario params."""
    # Use CacheManager's key generation for consistency
    param_dict = {k: v for k, v in sorted(params.items())}
    return CacheManager.generate_key(scenario_type, param_dict)


def _get_cached_result(db: Session, cache_key: str) -> Optional[dict]:
    """Retrieve cached result if it exists and has not expired."""
    try:
        return CacheManager.get(db, cache_key)
    except Exception as e:
        logger.warning(f"Cache get failed: {e}")
        return None


def _cache_result(
    db: Session,
    scenario_type: str,
    cache_key: str,
    result: dict,
    ttl_hours: int = 1
) -> None:
    """Store result in cache with TTL."""
    try:
        CacheManager.set(db, cache_key, scenario_type, result)
    except Exception as e:
        logger.warning(f"Cache set failed: {e}")


# ────────────────────────────────────────────────────────────────────────────
# Scenario Computation Helpers
# ────────────────────────────────────────────────────────────────────────────

# Continental shipping reference point: FedEx Memphis "WorldHub" (a real logistics
# super-hub). Distributor ETA is derived from real haversine distance to this hub at
# the published BTS ground freight rate (GROUND_KM_PER_DAY), plus order processing and
# (for non-domestic suppliers) customs handling. No hardcoded per-supplier lead times.
_REF_HUB_LAT, _REF_HUB_LNG = 35.1495, -90.0490
_ORDER_PROCESSING_DAYS = 2.0   # order handling before dispatch
_CUSTOMS_DAYS = 5.0            # international customs + handling
_DEFAULT_ETA_DAYS = 21.0       # fallback only when a BOM has no resolvable supplier geo


def _graph(db: Session):
    """
    Return the live GraphState, building it on demand if not yet loaded (e.g. tests).

    Two things this must get right now that the startup build is deferred onto a
    background thread (app/startup.py):

      1. WAIT for that one build instead of racing it. Otherwise a request arriving
         during warm-up would build a second graph in parallel with the first.
      2. STORE what it builds. This function used to call `build_graph_state(db)` and
         throw the result away, which was harmless only while the lifespan guaranteed
         a populated global before the first request. Un-cached it is a denial of
         service: Brandes betweenness over 883 nodes, per request, forever, on a
         0.5-CPU worker. `ensure_graph_state` is single-flight and caches.
    """
    wait_for_graph()
    gs = get_graph_state()
    if gs is None:
        gs = ensure_graph_state(db)
    return gs


def _distributor_lead_days(dist: Distributor) -> float:
    """Geography-derived lead-time ESTIMATE for one distributor (days).

    NOT an observed lead time. It is a deterministic proxy: great-circle distance
    from the reference hub, converted at a fixed ground speed, plus a fixed order
    processing allowance and a customs allowance for non-domestic suppliers. The
    distributor coordinates are real; the elapsed time derived from them is a
    model, and it is never compared against a measured delivery.

    This path deliberately does not use the observed DigiKey lead-time panel or
    the trained lead-time model. Those measure FACTORY replenishment lead time
    per part; this needs a per-distributor shipping estimate for a hypothetical
    plan, which the panel does not observe.
    """
    dist_km = haversine_km(dist.latitude, dist.longitude, _REF_HUB_LAT, _REF_HUB_LNG)
    days = _ORDER_PROCESSING_DAYS + dist_km / GROUND_KM_PER_DAY
    if not dist.is_domestic:
        days += _CUSTOMS_DAYS
    return days


def _real_alt_suppliers(db: Session, supplier_names: List[str]) -> List[Dict]:
    """Build per-alternative-supplier detail for the BOM impact table.

    Given the affected/alternative distributor names, return a list of
    {"name", "lead_time_days"} with the lead time ESTIMATED from each
    distributor's real geography via `_distributor_lead_days` — never a hardcoded
    constant, and never an observed delivery time either. Suppliers are sorted
    fastest-first so the most useful reroute options surface at the top.
    """
    if not supplier_names:
        return []
    dists = (
        db.query(Distributor)
        .filter(Distributor.name.in_(supplier_names))
        .all()
    )
    alts = [
        {"name": d.name, "lead_time_days": round(_distributor_lead_days(d), 1)}
        for d in dists
    ]
    alts.sort(key=lambda a: a["lead_time_days"])
    return alts


def _affected_component_details(
    db: Session,
    component_ids: List[int],
    excluded_distributor_id: Optional[int] = None,
    allowed_distributor_ids: Optional[set] = None,
) -> List[Dict]:
    """Real, per-component rows for the BOM impact table.

    For each affected component: its real MPN, the distributor it is sourced from
    today (the cheapest priced offer in the unconstrained catalogue), and the
    suppliers that can still serve THAT component under the scenario.

    The last part is the fix. The BOM-wide `alternative_suppliers` list is the set
    of distributors still serving ANY line of the BOM, and the UI was attaching it
    to every affected row. For a distributor failure "affected" means orphaned —
    no surviving supplier for that line — so the table claimed ten reroute options
    for lines that have exactly zero. Scoping the query to the component and the
    scenario makes the number the table prints true: it is 0 for an orphaned line,
    and for a geopolitical migration it is the real other distributors that carry
    the part, with lead times from real distributor geography.
    """
    if not component_ids:
        return []

    comps = {
        c.id: c for c in
        db.query(Component).filter(Component.id.in_(component_ids)).all()
    }
    offers = db.query(DistributorOffer).filter(
        DistributorOffer.component_id.in_(component_ids)
    ).all()
    dist_ids = {o.distributor_id for o in offers}
    dists = {
        d.id: d for d in
        db.query(Distributor).filter(Distributor.id.in_(dist_ids)).all()
    } if dist_ids else {}

    by_component: Dict[int, List[DistributorOffer]] = {}
    for o in offers:
        by_component.setdefault(o.component_id, []).append(o)

    rows: List[Dict] = []
    for cid in component_ids:
        comp_offers = by_component.get(cid, [])
        priced = [
            o for o in comp_offers
            if o.price is not None and float(o.price) > 0 and o.distributor_id in dists
        ]
        # Today's source = the cheapest offer that exists for this line, before the
        # scenario removes anything. Never a placeholder string.
        current = min(priced, key=lambda o: float(o.price)) if priced else None
        current_name = dists[current.distributor_id].name if current else None

        surviving = {
            o.distributor_id for o in comp_offers
            if o.distributor_id in dists
            and o.distributor_id != excluded_distributor_id
            and (allowed_distributor_ids is None or o.distributor_id in allowed_distributor_ids)
            and (current is None or o.distributor_id != current.distributor_id)
        }
        alts = [
            {
                "name": dists[did].name,
                "lead_time_days": round(_distributor_lead_days(dists[did]), 1),
            }
            for did in surviving
        ]
        alts.sort(key=lambda a: a["lead_time_days"])

        comp = comps.get(cid)
        rows.append({
            "component_id": cid,
            "mpn": comp.mpn if comp is not None else str(cid),
            "current_supplier": current_name,
            "alternative_suppliers": alts,
        })
    return rows


def _plan_eta_days(
    db: Session,
    chosen: Dict[int, int],
) -> Optional[float]:
    """ETA of an ACTUAL purchase plan: the slowest line in the plan.

    `chosen` is the `component_id -> distributor_id` argmin map that `_price_bom`
    returns — the suppliers this plan really buys from. A BOM is complete only once
    every line has landed, so the ETA is the max of those suppliers'
    geography-derived lead-time estimates. Returns None when the plan buys nothing.

    This replaces a max-over-lines of MIN-over-suppliers, which answered a question
    nobody asked: "if every line came from the fastest distributor in the whole
    catalogue, when would the BOM land". That number described a DIFFERENT PLAN from
    the cost printed beside it. On the demo cart the cheapest-offer rule sends 4 of 5
    lines to a Singapore distributor at 26.6 days, so the $166.94 plan's estimated ETA is
    26.6 days — the page published 2.8, a 9.4x understatement, and its own
    line-by-line table named the 26.6-day supplier on those four rows.

    Every ETA on these endpoints now comes from this function, so cost and ETA always
    describe the same set of suppliers.
    """
    if not chosen:
        return None
    dists = {
        d.id: d
        for d in db.query(Distributor)
        .filter(Distributor.id.in_(sorted(set(chosen.values()))))
        .all()
    }
    leads = [_distributor_lead_days(dists[did]) for did in chosen.values() if did in dists]
    return max(leads) if leads else None


def _effective_plan(
    scenario_chosen: Dict[int, int],
    baseline_chosen: Dict[int, int],
    unpriceable: List[int],
) -> Dict[int, int]:
    """The plan a scenario actually leaves you with, orphaned lines included.

    `_carry_orphaned_lines` already keeps an unbuyable line's BASELINE cost in the
    scenario bill — losing your only supplier is not a saving. The ETA has to make
    the same assumption or the two figures drift apart into different plans again,
    which is the exact defect this module was carrying. So an orphaned line here
    contributes its baseline supplier's lead time: the emergency buy that replaces it
    cannot plausibly land sooner than the supplier it replaces.
    """
    plan = dict(scenario_chosen)
    for cid in unpriceable:
        if cid in baseline_chosen:
            plan[cid] = baseline_chosen[cid]
    return plan


_SPEND_AT_RISK_BASIS = (
    "procurement_spend_at_risk_usd = goods cost x (CVaR-95 - 1): the extra dollars a "
    "worst-5% disruption would add, computed on the BASELINE simulation across every "
    "distributor's own disruption probability. affected_bom_ids is a different, "
    "deterministic question — which lines this ONE distributor's outage leaves with no "
    "supplier at all. A non-zero spend-at-risk beside an empty affected_bom_ids is "
    "therefore consistent: nothing is orphaned by this outage, but the BOM still "
    "carries ordinary tail exposure to the rest of the network."
)

_COST_BASIS = (
    "Goods cost only: for each line, quantity x the cheapest available offer price. "
    "Freight, per-supplier fees, MOQ rounding and duty are NOT included — those live "
    "in /optimize/* , which is why its totals are larger. Scenario cost re-prices "
    "every line against the offers that survive the scenario, then applies the Monte "
    "Carlo's expected emergency-procurement multiplier."
)


_ETA_BASIS = (
    "baseline_eta_days / scenario_eta_days = the SLOWEST line of the plan whose cost "
    "is reported beside it: max over lines of a GEOGRAPHY-DERIVED lead-time ESTIMATE "
    "for the distributor that line is actually bought from (cheapest available offer). "
    "That estimate is great-circle distance at a fixed ground speed plus fixed order-"
    "processing and customs allowances -- the coordinates are real, the elapsed time is "
    "modelled, and it is NOT an observed lead time and not a prediction from the trained "
    "lead-time model (that model predicts factory replenishment weeks per part, a "
    "different quantity). It is also NOT the fastest supplier in the catalogue -- that "
    "figure described a plan nobody buys and understated the demo cart by 9.4x. A cheap "
    "distant supplier is therefore visibly also a slow one, and dropping it can IMPROVE "
    "the ETA while raising cost."
)


def _require_known_components(db: Session, quantities: Dict[int, int]) -> None:
    """404 on component ids that are not in the catalogue.

    Without this an unknown id is silently treated as a line with no supplier: it
    shows up as "orphaned" in the hedging block and drags the fulfillment percentiles
    down, so the caller gets a confident 200 describing a BOM it never asked about.
    `/graph/simulate` and `/stochastic/frontier` both already reject unknown ids;
    these endpoints now agree with them.
    """
    known = {
        cid for (cid,) in db.query(Component.id).filter(
            Component.id.in_(list(quantities))
        ).all()
    }
    unknown = sorted(set(quantities) - known)
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown component_id(s): {unknown[:20]}"
                f"{' …' if len(unknown) > 20 else ''} "
                f"({len(unknown)} of {len(quantities)} BOM lines)."
            ),
        )


def _price_bom(
    db: Session,
    quantities: Dict[int, int],
    excluded_distributor_id: Optional[int] = None,
    allowed_distributor_ids: Optional[set] = None,
) -> tuple[float, Dict[int, float], List[int], Dict[int, int]]:
    """Quantity-weighted goods cost of a BOM at the CHEAPEST AVAILABLE offer per line.

    Returns (total_cost, per_line_cost, unpriceable_component_ids, chosen_distributor_by_line).

    The fourth element is the argmin distributor per line — the suppliers this plan
    actually buys from. It used to be discarded, which is why the Monte Carlo was
    simulating the whole catalogue instead of the plan: with 11-37 suppliers per
    line, P(all fail) is ~1e-13, so CVaR-95 pinned to exactly 1.0, every fulfilment
    percentile to 100%, and spend-at-risk to $0.00 — and the page then asserted that
    zero was a meaningful result for a hedged BOM. run_benchmark.py always passed the
    selected set; this endpoint never did.

    Two audit fixes live here:
      * quantity is honoured. The old helper summed one unit of each line's AVERAGE
        offer price, so a 5,000-unit build and a 1-unit prototype priced identically.
      * removing a distributor actually RE-PRICES the BOM. The old code computed the
        component cost once and never recomputed it, so losing the cheapest supplier
        on four of five lines produced a cost delta of exactly 0.0 — the model could
        only see total stockouts, never substitution to the next-cheapest offer.
    """
    if not quantities:
        return 0.0, {}, [], {}
    offers = db.query(DistributorOffer).filter(
        DistributorOffer.component_id.in_(list(quantities))
    ).all()

    per_line: Dict[int, float] = {}
    unpriceable: List[int] = []
    chosen: Dict[int, int] = {}
    total = 0.0
    for cid, qty in quantities.items():
        candidates = [
            o
            for o in offers
            if o.component_id == cid
            and o.price is not None
            and float(o.price) > 0
            and o.distributor_id != excluded_distributor_id
            and (allowed_distributor_ids is None or o.distributor_id in allowed_distributor_ids)
        ]
        if not candidates:
            unpriceable.append(cid)
            continue
        best = min(candidates, key=lambda o: float(o.price))
        line_cost = float(best.price) * qty
        per_line[cid] = line_cost
        chosen[cid] = int(best.distributor_id)
        total += line_cost
    return total, per_line, sorted(unpriceable), chosen


# Served fulfilment percentiles are rounded to 3 dp, so 0.0005 is half of the
# smallest representable difference: below it the two fields are identical as
# published and no drop may be claimed.
_FULFILMENT_EPS = 5e-4


def _pct_str(fraction: float) -> str:
    """0.8 -> '80%', 0.855 -> '85.5%'. Never invents precision the field lacks."""
    s = f"{fraction * 100:.1f}"
    return (s[:-2] if s.endswith(".0") else s) + "%"


def _pts_str(delta_fraction: float) -> str:
    """-0.2 -> '-20 pts'. Signed: this is a delta, not a magnitude."""
    s = f"{delta_fraction * 100:+.1f}"
    return (s[:-2] if s.endswith(".0") else s) + " pts"


def _fulfilment_clause(
    baseline_p50: Optional[float], scenario_p50: Optional[float]
) -> str:
    """One sentence about MODELLED fulfilment, composed from the two served fields.

    Structural hedging (every line still has a supplier) and modelled fulfilment
    (what the Monte Carlo delivers over the plan that SURVIVES the scenario) are
    different questions with different answers: `_price_bom` re-selects suppliers
    under the scenario and the simulation is restricted to that new, often more
    failure-prone, set. The hedging statement used to answer the first question and
    then assert an answer to the second -- "Zero fulfillment impact is the correct
    answer" -- without ever reading `scenario_fulfillment_p50`. On the live API at
    646bb66 that sentence shipped beside baseline 1.0 / scenario 0.8, a 20-point
    drop the same response had already priced into `risk_delta = 0.2`.

    This is backlog item 23's pattern applied here: COMPOSE the interpretation from
    the served fields instead of branch-selecting a fixed string that cannot see them.
    """
    if baseline_p50 is None or scenario_p50 is None:
        return (
            " Modelled fulfilment was not measured for this call, so this block makes "
            "no claim about it."
        )
    delta = scenario_p50 - baseline_p50
    if delta < -_FULFILMENT_EPS:
        return (
            f" That is NOT zero fulfillment impact: median modelled fulfilment still "
            f"falls {_pct_str(baseline_p50)} to {_pct_str(scenario_p50)} "
            f"({_pts_str(delta)}), because the suppliers that survive are not the ones "
            f"the baseline plan was buying from. Structural hedging and modelled "
            f"fulfilment are different questions, and this response answers them "
            f"differently -- see baseline_fulfillment_p50 / scenario_fulfillment_p50."
        )
    if delta > _FULFILMENT_EPS:
        return (
            f" Median modelled fulfilment does not fall -- it RISES "
            f"{_pct_str(baseline_p50)} to {_pct_str(scenario_p50)} ({_pts_str(delta)}), "
            f"because the surviving plan is less exposed than the baseline plan it "
            f"replaces."
        )
    return (
        f" Median modelled fulfilment is unchanged at {_pct_str(baseline_p50)} "
        f"(scenario_fulfillment_p50 == baseline_fulfillment_p50), so zero fulfillment "
        f"impact is the correct answer here, not a missing computation."
    )


def _hedging_summary(
    db: Session,
    quantities: Dict[int, int],
    n_single_source_lines: int,
    scenario_label: str,
    excluded_distributor_id: Optional[int] = None,
    allowed_distributor_ids: Optional[set] = None,
    baseline_fulfillment_p50: Optional[float] = None,
    scenario_fulfillment_p50: Optional[float] = None,
) -> HedgingSummary:
    """How much of this BOM the scenario can actually reach.

    Exactly one of `excluded_distributor_id` (an outage) / `allowed_distributor_ids`
    (a constraint that shrinks the pool) should be given. With neither, the scenario
    does not remove any supplier and the block reports the BOM's STRUCTURAL
    redundancy instead — which is what a risk-index spike actually exposes.
    """
    cids = list(quantities)
    offers = db.query(DistributorOffer).filter(
        DistributorOffer.component_id.in_(cids)
    ).all()
    removes_suppliers = (
        excluded_distributor_id is not None or allowed_distributor_ids is not None
    )
    orphaned: List[int] = []
    with_alt = 0
    for cid in cids:
        remaining = {
            o.distributor_id for o in offers
            if o.component_id == cid
            and o.distributor_id != excluded_distributor_id
            and (allowed_distributor_ids is None or o.distributor_id in allowed_distributor_ids)
        }
        if remaining:
            with_alt += 1
        else:
            orphaned.append(cid)

    n = len(cids)
    fulfilment = _fulfilment_clause(baseline_fulfillment_p50, scenario_fulfillment_p50)
    if not n:
        statement = "Empty BOM."
    elif orphaned:
        statement = (
            f"{len(orphaned)} of {n} lines lose every supplier under {scenario_label} "
            f"(component ids {orphaned[:10]}). Those lines are unfulfillable."
        ) + fulfilment
    elif removes_suppliers:
        statement = (
            f"This BOM is fully hedged against {scenario_label} STRUCTURALLY: all {n} "
            f"of {n} lines still have at least one supplier, so no line is orphaned."
            + fulfilment
            + " The cost impact is the substitution to the next-cheapest surviving "
            "offer, reported in cost_substitution."
        )
    else:
        # No supplier is removed by this scenario, so describe the redundancy the BOM
        # would rely on if one were. Claiming it is "hedged against" a risk-index
        # spike would be a tautology.
        statement = (
            f"{scenario_label} does not remove any supplier from the catalogue, so no "
            f"line is orphaned by it. Structurally, {n_single_source_lines} of {n} "
            "lines are single-sourced and would be exposed to an actual outage; the "
            "spike's effect flows through the emergency-procurement multiplier "
            "instead."
        ) + fulfilment

    return HedgingSummary(
        n_bom_lines=n,
        n_lines_with_alternate=with_alt,
        n_lines_orphaned=len(orphaned),
        orphaned_component_ids=orphaned[:50],
        n_single_source_lines=n_single_source_lines,
        fully_hedged=bool(n) and not orphaned,
        baseline_fulfillment_p50=baseline_fulfillment_p50,
        scenario_fulfillment_p50=scenario_fulfillment_p50,
        fulfillment_p50_delta_pts=(
            None
            if baseline_fulfillment_p50 is None or scenario_fulfillment_p50 is None
            else round((scenario_fulfillment_p50 - baseline_fulfillment_p50) * 100, 1)
        ),
        statement=statement,
    )


def _compute_baseline_metrics(db: Session, quantities: Dict[int, int]) -> dict:
    """Compute baseline cost, ETA, risk, and fulfillment distribution for a BOM.

    Fulfillment percentiles and the expected emergency-procurement premium come from
    the real Monte Carlo cascade simulation; ETA from real distributor geography;
    goods cost from the cheapest available offer per line x that line's QUANTITY.
    """
    bom_component_ids = list(quantities)
    components = db.query(Component).filter(Component.id.in_(bom_component_ids)).all()

    component_cost, per_line, unpriceable, chosen = _price_bom(db, quantities)
    # The suppliers this plan actually buys from. Restricting the simulation to them
    # is what run_benchmark.py has always done; without it the cascade model answers
    # "could anyone, anywhere sell this part" rather than "is this plan exposed".
    plan_distributor_ids = set(chosen.values()) or None

    risk_sum = sum(comp.risk_score or 0.0 for comp in components)
    avg_risk = risk_sum / len(components) if components else 0.0

    # Real Monte Carlo cascade simulation (N=1,000, seed=42 → deterministic).
    sim = run_monte_carlo(
        _graph(db), bom_component_ids, allowed_distributor_ids=plan_distributor_ids
    )
    # ETA of the plan we just priced — max lead over the suppliers `chosen`
    # actually buys from. NOT the catalogue-wide fastest supplier per line, which
    # is a plan nobody is buying and was understating this BOM by 9.4x.
    baseline_eta = _plan_eta_days(db, chosen)

    # Tail-risk dollars: CVaR-95 (mean cost multiplier of the worst-5% scenarios)
    # applied to raw component spend gives the emergency-procurement premium a
    # tail disruption would add. (cvar_95 - 1) strips the baseline so the figure
    # is the *extra* dollars exposed, not the total bill.
    spend_at_risk = component_cost * max(0.0, sim.cvar_95 - 1.0)

    return {
        "_component_cost": round(component_cost, 2),
        "_per_line_cost": per_line,
        "_unpriceable": unpriceable,
        "_plan_distributor_ids": plan_distributor_ids,
        "_chosen": chosen,
        "_sim": sim,
        "_mean_cost_inflation": sim.mean_cost_inflation,
        "baseline_cost_usd": round(component_cost * sim.mean_cost_inflation, 2),
        "baseline_cvar_95": round(sim.cvar_95, 4),
        "procurement_spend_at_risk_usd": round(spend_at_risk, 2),
        "spend_at_risk_basis": _SPEND_AT_RISK_BASIS,
        "baseline_eta_days": round(baseline_eta if baseline_eta is not None else _DEFAULT_ETA_DAYS, 1),
        "eta_basis": _ETA_BASIS,
        "baseline_risk_score": round(avg_risk, 3),
        "baseline_fulfillment_p10": round(sim.p10, 3),
        "baseline_fulfillment_p50": round(sim.p50, 3),
        "baseline_fulfillment_p90": round(sim.p90, 3),
    }


def _carry_orphaned_lines(
    scenario_cost: float,
    baseline_per_line: Dict[int, float],
    unpriceable: List[int],
) -> float:
    """Keep an unbuyable line in the scenario's bill instead of deleting it.

    `_price_bom` skips a line that has no surviving offer, so a scenario that ORPHANS
    a line would otherwise come out CHEAPER than the baseline — losing your only
    supplier for a part would be reported as a saving. (Observed: removing
    "Component Stockers USA" from a 60-line BOM scored -32.8%.) The line's cost does
    not go away when the supplier does; it becomes an emergency buy, priced by the
    Monte Carlo's `mean_cost_inflation` on top of the figure carried here.
    """
    if not unpriceable:
        return scenario_cost
    return scenario_cost + sum(baseline_per_line.get(cid, 0.0) for cid in unpriceable)


def _substitution_block(
    baseline_cost: float,
    baseline_per_line: Dict[int, float],
    scenario_cost: float,
    scenario_per_line: Dict[int, float],
    scenario_unpriceable: List[int],
) -> CostSubstitution:
    """Line-by-line diff of the baseline vs scenario goods cost."""
    repriced = 0
    worst_delta = 0.0
    worst_cid: Optional[int] = None
    for cid, base_line in baseline_per_line.items():
        scen_line = scenario_per_line.get(cid)
        if scen_line is None:
            continue
        delta = scen_line - base_line
        if abs(delta) > 1e-9:
            repriced += 1
        if delta > worst_delta:
            worst_delta = delta
            worst_cid = cid
    return CostSubstitution(
        baseline_component_cost_usd=round(baseline_cost, 2),
        scenario_component_cost_usd=round(scenario_cost, 2),
        substitution_delta_usd=round(scenario_cost - baseline_cost, 2),
        n_lines_repriced=repriced,
        n_lines_unpriceable=len(scenario_unpriceable),
        largest_line_increase_usd=round(worst_delta, 2),
        largest_line_component_id=worst_cid,
        basis=_COST_BASIS,
    )


def _identify_affected_boms(
    db: Session,
    bom_component_ids: List[int],
    excluded_distributor_id: Optional[int] = None
) -> tuple[List[int], List[str]]:
    """Identify components and suppliers affected by distributor removal."""
    affected_bom_ids = []
    affected_suppliers = []

    for comp_id in bom_component_ids:
        offers = db.query(DistributorOffer).filter(
            DistributorOffer.component_id == comp_id
        ).all()

        remaining_offers = [o for o in offers if o.distributor_id != excluded_distributor_id]

        # Component is affected if removing the distributor leaves no alternatives
        if not remaining_offers:
            affected_bom_ids.append(comp_id)

        # Find alternative suppliers
        alt_suppliers = set()
        for offer in remaining_offers:
            dist = db.query(Distributor).filter(Distributor.id == offer.distributor_id).first()
            if dist:
                alt_suppliers.add(dist.name)
        affected_suppliers.extend(list(alt_suppliers))

    return affected_bom_ids, list(set(affected_suppliers))


def _risk_tier(score: float) -> int:
    """Map a risk score to a tier index. Matches frontend lib/risk.ts thresholds.

    0 = low (<0.4), 1 = medium (0.4–0.7), 2 = high (>=0.7).
    """
    if score < 0.4:
        return 0
    if score < 0.7:
        return 1
    return 2


def _identify_geo_affected(
    db: Session,
    bom_component_ids: List[int],
    risk_multiplier: float,
) -> tuple[List[int], List[str], float]:
    """Identify components whose risk tier migrates upward under a GPR spike.

    Applies the multiplier to each component's individual risk_score (capped at
    1.0) and flags components that cross into a higher tier. Affected suppliers
    are the distributors that source those migrating components. Also returns the
    BOM-wide scenario risk score as the mean of the per-component scenario risks,
    keeping the aggregate consistent with the migrations shown to the user.
    """
    components = db.query(Component).filter(Component.id.in_(bom_component_ids)).all()

    affected_bom_ids: List[int] = []
    scenario_risks: List[float] = []
    for comp in components:
        baseline_risk = comp.risk_score or 0.0
        scenario_risk = min(baseline_risk * risk_multiplier, 1.0)
        scenario_risks.append(scenario_risk)
        if _risk_tier(scenario_risk) > _risk_tier(baseline_risk):
            affected_bom_ids.append(comp.id)

    # Suppliers exposed to the spike = distributors sourcing the migrating components.
    affected_suppliers: List[str] = []
    if affected_bom_ids:
        offers = db.query(DistributorOffer).filter(
            DistributorOffer.component_id.in_(affected_bom_ids)
        ).all()
        dist_ids = {o.distributor_id for o in offers}
        if dist_ids:
            dists = db.query(Distributor).filter(Distributor.id.in_(dist_ids)).all()
            affected_suppliers = sorted({d.name for d in dists})

    scenario_risk_score = (
        round(sum(scenario_risks) / len(scenario_risks), 3) if scenario_risks else 0.0
    )
    return affected_bom_ids, affected_suppliers, scenario_risk_score


# ────────────────────────────────────────────────────────────────────────────
# POST /resilience/distributor-failure
# ────────────────────────────────────────────────────────────────────────────

@router.post("/distributor-failure", response_model=ScenarioResponse)
def post_distributor_failure(
    body: DistributorFailureRequest,
    db: Session = Depends(get_db),
):
    """
    Simulate supply chain impact of a distributor failure.

    Uses graph cascade simulation to determine which BOMs break,
    alternative suppliers, and cost/ETA/risk deltas.
    Results cached (1h TTL) with deterministic SHA256 key.
    OpenTelemetry spans track cache hits/misses and slow computation paths.
    """
    with tracer.start_as_current_span("distributor_failure_scenario") as span:
        quantities = body.resolved_lines()
        bom_component_ids = list(quantities)
        # Set span attributes
        span.set_attribute("distributor_id", body.distributor_id)
        span.set_attribute("bom_size", len(bom_component_ids))

        # Check distributor exists
        dist = db.query(Distributor).filter(Distributor.id == body.distributor_id).first()
        if not dist:
            span.set_attribute("error", "distributor_not_found")
            raise HTTPException(status_code=400, detail=f"Distributor {body.distributor_id} not found")

        # Compute cache key
        cache_key = _compute_cache_key(
            "distributor-failure",
            distributor_id=body.distributor_id,
            bom=sorted(quantities.items()),
            # The resolved quantities can be identical whether they were stated or
            # assumed, but the response REPORTS which — so it belongs in the key.
            quantity_source=body.quantity_source(),
        )
        span.set_attribute("cache_key", cache_key)

        # Check cache
        cached = _get_cached_result(db, cache_key)
        if cached:
            span.set_attribute("cache_hit", True)
            span.set_attribute("result_source", "cache")
            logger.debug("Cache hit for distributor_failure:%s", body.distributor_id)
            return ScenarioResponse(**cached)

        span.set_attribute("cache_hit", False)

        # Compute baseline
        _require_known_components(db, quantities)
        baseline = _compute_baseline_metrics(db, quantities)

        # Simulate scenario: force this distributor to fail in every Monte Carlo
        # scenario, then recompute fulfillment, cost, ETA and risk from the result.
        with tracer.start_as_current_span("simulate_distributor_removal"):
            # Re-price FIRST: the surviving plan defines which suppliers the Monte
            # Carlo should expose. Simulating before re-pricing meant simulating the
            # whole catalogue, which is why every fulfilment percentile came back 100%.
            scenario_component_cost, scenario_per_line, scenario_unpriceable, scenario_chosen = _price_bom(
                db, quantities, excluded_distributor_id=body.distributor_id
            )
            scenario_sim = run_monte_carlo(
                _graph(db),
                bom_component_ids=bom_component_ids,
                forced_failures={body.distributor_id},
                allowed_distributor_ids=set(scenario_chosen.values()) or None,
            )
            # COST — two independent effects, both real:
            #   1. substitution: every line re-priced against the offers that survive
            #      the outage (this is what used to be missing entirely, and it is why
            #      losing a BOM's cheapest supplier reported a 0.0% cost delta);
            #   2. the Monte Carlo's expected emergency-procurement multiplier, which
            #      only moves when a line has NO surviving supplier.
            scenario_component_cost = _carry_orphaned_lines(
                scenario_component_cost, baseline["_per_line_cost"], scenario_unpriceable
            )
            scenario_cost = scenario_component_cost * scenario_sim.mean_cost_inflation
            # ETA of the SURVIVING PLAN: the max lead over the suppliers the
            # re-priced BOM now buys from, with an orphaned line carrying its
            # baseline supplier's lead exactly as it carries its baseline cost.
            # This is what makes the delta meaningful — both sides are plans.
            scenario_eta_raw = _plan_eta_days(
                db,
                _effective_plan(
                    scenario_chosen, baseline["_chosen"], scenario_unpriceable
                ),
            )
            scenario_eta = round(
                scenario_eta_raw if scenario_eta_raw is not None else baseline["baseline_eta_days"], 1
            )
            # Risk rises in proportion to the median fulfillment lost to the outage.
            fulfillment_drop = max(0.0, baseline["baseline_fulfillment_p50"] - scenario_sim.p50)
            scenario_risk = min(baseline["baseline_risk_score"] + fulfillment_drop, 1.0)

            affected_bom_ids, affected_suppliers = _identify_affected_boms(
                db, bom_component_ids, body.distributor_id
            )

        # Build response
        result = {
            "baseline_cost_usd": baseline["baseline_cost_usd"],
            "scenario_cost_usd": round(scenario_cost, 2),
            "cost_delta_pct": round((scenario_cost - baseline["baseline_cost_usd"]) / baseline["baseline_cost_usd"] * 100, 1) if baseline["baseline_cost_usd"] else 0.0,
            "baseline_eta_days": baseline["baseline_eta_days"],
            "scenario_eta_days": scenario_eta,
            "eta_delta_days": round(scenario_eta - baseline["baseline_eta_days"], 1),
            "baseline_risk_score": baseline["baseline_risk_score"],
            "baseline_cvar_95": baseline["baseline_cvar_95"],
            "procurement_spend_at_risk_usd": baseline["procurement_spend_at_risk_usd"],
            "spend_at_risk_basis": baseline["spend_at_risk_basis"],
            "eta_basis": baseline["eta_basis"],
            "scenario_risk_score": round(scenario_risk, 3),
            "risk_delta": round(scenario_risk - baseline["baseline_risk_score"], 3),
            "baseline_fulfillment_p10": baseline["baseline_fulfillment_p10"],
            "baseline_fulfillment_p50": baseline["baseline_fulfillment_p50"],
            "baseline_fulfillment_p90": baseline["baseline_fulfillment_p90"],
            "scenario_fulfillment_p10": round(scenario_sim.p10, 3),
            "scenario_fulfillment_p50": round(scenario_sim.p50, 3),
            "scenario_fulfillment_p90": round(scenario_sim.p90, 3),
            "affected_bom_ids": affected_bom_ids,
            "affected_suppliers": affected_suppliers,
            "alternative_suppliers": _real_alt_suppliers(db, affected_suppliers),
            "affected_components": _affected_component_details(
                db, affected_bom_ids, excluded_distributor_id=body.distributor_id
            ),
            "bom_quantities": {str(k): v for k, v in quantities.items()},
            "quantity_source": body.quantity_source(),
            "total_units": sum(quantities.values()),
            "cost_basis": _COST_BASIS,
            "hedging": _hedging_summary(
                db, quantities,
                n_single_source_lines=baseline["_sim"].n_single_source_lines,
                scenario_label=f"{dist.name} going dark",
                excluded_distributor_id=body.distributor_id,
                baseline_fulfillment_p50=baseline["baseline_fulfillment_p50"],
                scenario_fulfillment_p50=round(scenario_sim.p50, 3),
            ).model_dump(),
            "cost_substitution": _substitution_block(
                baseline["_component_cost"], baseline["_per_line_cost"],
                scenario_component_cost, scenario_per_line, scenario_unpriceable,
            ).model_dump(),
        }

        # Cache result
        _cache_result(db, "distributor-failure", cache_key, result)
        span.set_attribute("result_source", "computed")
        logger.debug(f"Computed and cached distributor_failure scenario for distributor {body.distributor_id}")

        return ScenarioResponse(**result)


# ────────────────────────────────────────────────────────────────────────────
# POST /resilience/geopolitical-risk
# ────────────────────────────────────────────────────────────────────────────

@router.post("/geopolitical-risk", response_model=ScenarioResponse)
def post_geopolitical_risk(
    body: GeopoliticalRiskRequest,
    db: Session = Depends(get_db),
):
    """
    Simulate impact of a geopolitical risk spike on the supply chain.

    THIS ENDPOINT READS NO LIVE FEED. It is a stress dial, not a feed override.
    It multiplies each component's STORED `risk_score` column by `risk_multiplier`
    (capped at 1.0), reports which components cross a risk tier, and passes the
    multiplier into the Monte Carlo as a `stress_factor` scaling every distributor's
    failure probability — which is what produces the cost delta.

    This docstring previously claimed it "overrides live feed indices (GPR_INDEX,
    ACLED_CONFLICT_COUNT)". That was FALSE and had been false for as long as it was
    written: this module imports no feed cache and contains no reference to one.
    The GPR signal is real and it does reach the CP-SAT objective as an origin
    surcharge — but it does so in `app/optimization/sourcing.py`, on the
    `/api/v1/optimize/*` endpoints, not here. The two are not wired together.

    Results cached (1h TTL). OpenTelemetry spans track performance.
    """
    with tracer.start_as_current_span("geopolitical_risk_scenario") as span:
        quantities = body.resolved_lines()
        bom_component_ids = list(quantities)
        # Set span attributes
        span.set_attribute("risk_multiplier", body.risk_multiplier)
        span.set_attribute("bom_size", len(bom_component_ids))

        # Compute cache key
        cache_key = _compute_cache_key(
            "geopolitical-risk",
            risk_multiplier=body.risk_multiplier,
            bom=sorted(quantities.items()),
            quantity_source=body.quantity_source(),
        )
        span.set_attribute("cache_key", cache_key)

        # Check cache
        cached = _get_cached_result(db, cache_key)
        if cached:
            span.set_attribute("cache_hit", True)
            span.set_attribute("result_source", "cache")
            logger.debug(f"Cache hit for geopolitical_risk:{body.risk_multiplier}")
            return ScenarioResponse(**cached)

        span.set_attribute("cache_hit", False)

        # Compute baseline
        _require_known_components(db, quantities)
        baseline = _compute_baseline_metrics(db, quantities)

        # Simulate scenario: apply risk multiplier per-component so individual
        # tier migrations (low→medium→high) are surfaced, not just a BOM-wide scalar.
        with tracer.start_as_current_span("apply_geopolitical_multiplier"):
            affected_bom_ids, affected_suppliers, scenario_risk = _identify_geo_affected(
                db, bom_component_ids, body.risk_multiplier
            )
            # Elevated stress scales every distributor's failure probability in the
            # Monte Carlo, so cost and fulfillment fall out of the real cascade model.
            scenario_sim = run_monte_carlo(
                _graph(db),
                bom_component_ids=bom_component_ids,
                stress_factor=body.risk_multiplier,
                # No supplier leaves the catalogue here, so the plan is unchanged —
                # but it is still the PLAN that is exposed, not the whole catalogue.
                allowed_distributor_ids=baseline.get("_plan_distributor_ids"),
            )
            # A risk-index spike does not delete any supplier from the catalogue, so
            # there is nothing to substitute to: the goods cost is unchanged and the
            # whole effect flows through the emergency-procurement multiplier.
            scenario_cost = baseline["_component_cost"] * scenario_sim.mean_cost_inflation
            # A risk-index spike removes no supplier, so the plan is the baseline
            # plan and its ETA is the baseline plan's ETA. Recomputed from the same
            # `_plan_eta_days` as every other branch rather than aliased, so a future
            # change to the plan here cannot silently keep publishing the old ETA.
            geo_eta = _plan_eta_days(db, baseline["_chosen"])
            scenario_eta = round(
                geo_eta if geo_eta is not None else baseline["baseline_eta_days"], 1
            )
            span.set_attribute("affected_count", len(affected_bom_ids))

        # Build response
        result = {
            "baseline_cost_usd": baseline["baseline_cost_usd"],
            "scenario_cost_usd": round(scenario_cost, 2),
            "cost_delta_pct": round((scenario_cost - baseline["baseline_cost_usd"]) / baseline["baseline_cost_usd"] * 100, 1) if baseline["baseline_cost_usd"] else 0.0,
            "baseline_eta_days": baseline["baseline_eta_days"],
            "scenario_eta_days": scenario_eta,
            "eta_delta_days": round(scenario_eta - baseline["baseline_eta_days"], 1),
            "baseline_risk_score": baseline["baseline_risk_score"],
            "baseline_cvar_95": baseline["baseline_cvar_95"],
            "procurement_spend_at_risk_usd": baseline["procurement_spend_at_risk_usd"],
            "spend_at_risk_basis": baseline["spend_at_risk_basis"],
            "eta_basis": baseline["eta_basis"],
            "scenario_risk_score": round(scenario_risk, 3),
            "risk_delta": round(scenario_risk - baseline["baseline_risk_score"], 3),
            "baseline_fulfillment_p10": baseline["baseline_fulfillment_p10"],
            "baseline_fulfillment_p50": baseline["baseline_fulfillment_p50"],
            "baseline_fulfillment_p90": baseline["baseline_fulfillment_p90"],
            "scenario_fulfillment_p10": round(scenario_sim.p10, 3),
            "scenario_fulfillment_p50": round(scenario_sim.p50, 3),
            "scenario_fulfillment_p90": round(scenario_sim.p90, 3),
            "affected_bom_ids": affected_bom_ids,
            "affected_suppliers": affected_suppliers,
            "alternative_suppliers": _real_alt_suppliers(db, affected_suppliers),
            "affected_components": _affected_component_details(db, affected_bom_ids),
            "bom_quantities": {str(k): v for k, v in quantities.items()},
            "quantity_source": body.quantity_source(),
            "total_units": sum(quantities.values()),
            "cost_basis": _COST_BASIS,
            "hedging": _hedging_summary(
                db, quantities,
                n_single_source_lines=baseline["_sim"].n_single_source_lines,
                scenario_label=f"A {body.risk_multiplier}x geopolitical risk spike",
                baseline_fulfillment_p50=baseline["baseline_fulfillment_p50"],
                scenario_fulfillment_p50=round(scenario_sim.p50, 3),
            ).model_dump(),
        }

        # Cache result
        _cache_result(db, "geopolitical-risk", cache_key, result)
        span.set_attribute("result_source", "computed")
        logger.debug(f"Computed and cached geopolitical_risk scenario with multiplier {body.risk_multiplier}")

        return ScenarioResponse(**result)


# ────────────────────────────────────────────────────────────────────────────
# POST /resilience/delivery-target
# ────────────────────────────────────────────────────────────────────────────

@router.post("/delivery-target", response_model=DeliveryTargetResponse)
def post_delivery_target(
    body: DeliveryTargetRequest,
    db: Session = Depends(get_db),
):
    """
    Simulate impact of tight delivery constraint on supply chain.

    Identifies suppliers capable of meeting target_delivery_days, RE-PRICES the BOM
    against the surviving offers, and shows the cost/risk impact.

    "Re-prices", not "re-optimizes": no scenario in this module invokes CP-SAT. Each
    one picks the cheapest surviving offer per line greedily and re-runs the Monte
    Carlo. The MILP lives in `app/optimization/sourcing.py` and is reached only via
    `/api/v1/optimize/*`.
    Results cached (1h TTL). OpenTelemetry spans track performance.
    """
    with tracer.start_as_current_span("delivery_target_scenario") as span:
        quantities = body.resolved_lines()
        bom_component_ids = list(quantities)
        # Set span attributes
        span.set_attribute("target_delivery_days", body.target_delivery_days)
        span.set_attribute("bom_size", len(bom_component_ids))

        # Compute cache key
        cache_key = _compute_cache_key(
            "delivery-target",
            target_delivery_days=body.target_delivery_days,
            bom=sorted(quantities.items()),
            quantity_source=body.quantity_source(),
        )
        span.set_attribute("cache_key", cache_key)

        # Check cache
        cached = _get_cached_result(db, cache_key)
        if cached:
            span.set_attribute("cache_hit", True)
            span.set_attribute("result_source", "cache")
            logger.debug(f"Cache hit for delivery_target:{body.target_delivery_days}")
            return DeliveryTargetResponse(**cached)

        span.set_attribute("cache_hit", False)

        # Compute baseline
        _require_known_components(db, quantities)
        baseline = _compute_baseline_metrics(db, quantities)

        # Identify suppliers capable of meeting target, by REAL geography-derived
        # lead time (no hardcoded per-supplier days).
        with tracer.start_as_current_span("identify_capable_suppliers"):
            distributors = db.query(Distributor).all()
            suppliers_capable = []
            suppliers_cannot_meet = []
            incapable_ids: set = set()
            capable_ids: set = set()

            for dist in distributors:
                lead = _distributor_lead_days(dist)
                if lead <= body.target_delivery_days:
                    capable_ids.add(dist.id)
                    suppliers_capable.append({
                        "name": dist.name,
                        "lead_time_days": round(lead, 1),
                        "cost_adjustment_pct": 0.0,  # meets the window natively, no expedite premium
                    })
                else:
                    incapable_ids.add(dist.id)
                    suppliers_cannot_meet.append({
                        "name": dist.name,
                        "min_lead_time_days": round(lead, 1),
                        "reason": "lead_time_too_long",
                    })

        # Simulate scenario: suppliers that cannot meet the window are unavailable, so
        # force them to fail in the Monte Carlo. Tightening the window removes suppliers,
        # which the cascade model translates into higher cost and lower fulfillment.
        # Re-price against the suppliers that can actually hit the window. A tighter
        # window is a smaller offer pool, so the cheapest surviving offer is >= the
        # unconstrained cheapest — the constraint has a real, visible price. This runs
        # BEFORE the simulation so the cascade model sees the constrained plan.
        scenario_component_cost, scenario_per_line, scenario_unpriceable, scenario_chosen = _price_bom(
            db, quantities, allowed_distributor_ids=capable_ids
        )
        scenario_sim = run_monte_carlo(
            _graph(db),
            bom_component_ids=bom_component_ids,
            forced_failures=incapable_ids,
            allowed_distributor_ids=set(scenario_chosen.values()) or None,
        )
        # Lines with no capable supplier cannot be bought inside the window at any
        # price. Keep their baseline cost in the total rather than silently DROPPING
        # them, which would make an infeasible target look cheaper.
        scenario_component_cost = _carry_orphaned_lines(
            scenario_component_cost, baseline["_per_line_cost"], scenario_unpriceable
        )
        scenario_cost = scenario_component_cost * scenario_sim.mean_cost_inflation

        # ── ETA: the ACHIEVED date of the CONSTRAINED PLAN ──────────────────────
        # Two defects were fixed here, in order.
        #   1. `scenario_eta = float(body.target_delivery_days)` — the endpoint simply
        #      asserted the target as if it had been met.
        #   2. Its replacement took the fastest supplier per line inside the window,
        #      which is the same class of error the baseline carried: it described a
        #      plan the reported cost was not paying for.
        # Now both sides are plans. `scenario_chosen` is the cheapest offer per line
        # restricted to in-window suppliers, so the ETA below is that plan's slowest
        # line — and it is <= the target by construction whenever every line is
        # buyable, because no supplier in the pool is slower than the target.
        unmet = list(scenario_unpriceable)
        constrained_plan = _effective_plan(
            scenario_chosen, baseline["_chosen"], scenario_unpriceable
        )
        plan_eta = _plan_eta_days(db, constrained_plan)
        scenario_eta = round(
            plan_eta if plan_eta is not None else baseline["baseline_eta_days"], 1
        )

        if unmet:
            # Some line has no in-window supplier at all. `_carry_orphaned_lines` keeps
            # its baseline cost in the bill, so the ETA above keeps its baseline
            # supplier's lead time: one plan, one cost, one date.
            target_met = False
            eta_note = (
                f"Target of {body.target_delivery_days} day(s) is INFEASIBLE: "
                f"{len(unmet)} of {len(quantities)} lines have no supplier that can "
                f"deliver inside the window (component ids {unmet[:10]}). "
                f"scenario_eta_days is the ETA of the plan you are actually left with "
                f"({scenario_eta} days) — in-window suppliers where they exist, the "
                f"baseline supplier where they do not — not the requested target."
            )
        else:
            target_met = scenario_eta <= body.target_delivery_days + 1e-9
            if not incapable_ids:
                eta_note = (
                    f"Every distributor already delivers inside "
                    f"{body.target_delivery_days} day(s), so the window removes no "
                    f"supplier: the constrained plan IS the baseline plan, at "
                    f"{scenario_eta} days and the same cost."
                )
            elif scenario_eta < baseline["baseline_eta_days"] - 1e-9:
                eta_note = (
                    f"Restricting to suppliers inside a {body.target_delivery_days}-day "
                    f"window moves the plan's ETA from {baseline['baseline_eta_days']} "
                    f"to {scenario_eta} days. The window is binding: it forces lines off "
                    f"their cheapest supplier, and cost_delta_pct is what that speed "
                    f"costs."
                )
            else:
                eta_note = (
                    f"The {body.target_delivery_days}-day window drops "
                    f"{len(incapable_ids)} distributor(s) but none of them were in the "
                    f"baseline plan, so the plan's ETA is unchanged at {scenario_eta} "
                    f"days — relaxing a satisfied constraint is not a degradation."
                )

        target_is_binding = bool(incapable_ids) and (
            bool(unmet)
            or abs(scenario_component_cost - baseline["_component_cost"]) > 1e-9
            or abs(scenario_eta - baseline["baseline_eta_days"]) > 1e-9
            or scenario_sim.p50 < baseline["baseline_fulfillment_p50"]
        )

        fulfillment_drop = max(0.0, baseline["baseline_fulfillment_p50"] - scenario_sim.p50)
        scenario_risk = min(baseline["baseline_risk_score"] + fulfillment_drop, 1.0)

        # Build response
        result = {
            "baseline_cost_usd": baseline["baseline_cost_usd"],
            "scenario_cost_usd": round(scenario_cost, 2),
            "cost_delta_pct": round((scenario_cost - baseline["baseline_cost_usd"]) / baseline["baseline_cost_usd"] * 100, 1) if baseline["baseline_cost_usd"] else 0.0,
            "baseline_eta_days": baseline["baseline_eta_days"],
            "scenario_eta_days": scenario_eta,
            "eta_delta_days": round(scenario_eta - baseline["baseline_eta_days"], 1),
            "baseline_risk_score": baseline["baseline_risk_score"],
            "baseline_cvar_95": baseline["baseline_cvar_95"],
            "procurement_spend_at_risk_usd": baseline["procurement_spend_at_risk_usd"],
            "spend_at_risk_basis": baseline["spend_at_risk_basis"],
            "eta_basis": baseline["eta_basis"],
            "scenario_risk_score": round(scenario_risk, 3),
            "risk_delta": round(scenario_risk - baseline["baseline_risk_score"], 3),
            "baseline_fulfillment_p10": baseline["baseline_fulfillment_p10"],
            "baseline_fulfillment_p50": baseline["baseline_fulfillment_p50"],
            "baseline_fulfillment_p90": baseline["baseline_fulfillment_p90"],
            "scenario_fulfillment_p10": round(scenario_sim.p10, 3),
            "scenario_fulfillment_p50": round(scenario_sim.p50, 3),
            "scenario_fulfillment_p90": round(scenario_sim.p90, 3),
            "affected_bom_ids": unmet,
            "affected_suppliers": [s["name"] for s in suppliers_capable],
            "alternative_suppliers": [
                {"name": s["name"], "lead_time_days": s["lead_time_days"]}
                for s in suppliers_capable
            ],
            "affected_components": _affected_component_details(
                db, unmet, allowed_distributor_ids=capable_ids
            ),
            "suppliers_capable": suppliers_capable,
            "suppliers_cannot_meet": suppliers_cannot_meet,
            "target_delivery_days": body.target_delivery_days,
            "target_met": target_met,
            "target_is_binding": target_is_binding,
            "unmet_component_ids": unmet,
            "eta_note": eta_note,
            "bom_quantities": {str(k): v for k, v in quantities.items()},
            "quantity_source": body.quantity_source(),
            "total_units": sum(quantities.values()),
            "cost_basis": _COST_BASIS,
            "hedging": _hedging_summary(
                db, quantities,
                n_single_source_lines=baseline["_sim"].n_single_source_lines,
                scenario_label=f"a {body.target_delivery_days}-day delivery window",
                allowed_distributor_ids=capable_ids,
                baseline_fulfillment_p50=baseline["baseline_fulfillment_p50"],
                scenario_fulfillment_p50=round(scenario_sim.p50, 3),
            ).model_dump(),
            "cost_substitution": _substitution_block(
                baseline["_component_cost"], baseline["_per_line_cost"],
                scenario_component_cost, scenario_per_line, scenario_unpriceable,
            ).model_dump(),
        }

        # Cache result
        _cache_result(db, "delivery-target", cache_key, result)
        span.set_attribute("result_source", "computed")
        logger.debug(f"Computed and cached delivery_target scenario with target {body.target_delivery_days} days")

        return DeliveryTargetResponse(**result)


# ════════════════════════════════════════════════════════════════════════════
# Recommendation engine endpoints (criticality sweep, dual-sourcing, sensitivity)
#
# These append to the "what-if" endpoints above. They share the same live
# GraphState (`_graph`) and the SHA256 cache helpers, and every figure they
# return is derived from real DB fields (offer prices, distributor geography,
# graph betweenness) — see app.optimization.recommendations for the compute.
# ════════════════════════════════════════════════════════════════════════════

# ── Request / Response schemas ──────────────────────────────────────────────

class CriticalitySweepRequest(BaseModel):
    bom_component_ids: Optional[List[int]] = Field(
        None, description="Restrict the sweep to these components; omit for the whole network"
    )
    top_n: int = Field(20, ge=1, le=200, description="Number of top distributors to return")


class CriticalityEntryModel(BaseModel):
    distributor_id: int
    name: str
    country: Optional[str] = None
    is_domestic: bool
    orphan_component_count: int
    orphan_component_ids: List[int]
    components_supplied: int
    spend_at_risk_usd: float
    betweenness: float
    rei: float


class CriticalitySweepResponse(BaseModel):
    entries: List[CriticalityEntryModel]
    max_spend_at_risk_usd: float
    network_wide: bool
    # ── Why so many entries are zero (2026-08 audit item 6) ───────────────────
    # `spend_at_risk_usd` / `orphan_component_count` count ONLY components for which a
    # distributor is the sole offer. In this catalogue 97.7% of components have two or
    # more distributors, so 91 of 92 distributors genuinely orphan nothing. That is a
    # real finding about a well-diversified catalogue — but returned as bare zeros it
    # is indistinguishable from a broken query, so the scope is stated here.
    n_distributors_scored: int = 0
    n_distributors_with_exposure: int = 0
    n_components_in_scope: int = 0
    n_single_source_components: int = 0
    single_source_share_pct: float = 0.0
    exposure_definition: str = ""
    interpretation: str = ""


class DualSourcingRequest(BaseModel):
    bom_component_ids: Optional[List[int]] = Field(
        None, description="Restrict to these components; omit for all single-source components"
    )
    qualification_cost_usd: float = Field(
        0.0, ge=0.0, description="One-off cost to qualify a second source, added to incremental unit cost"
    )
    top_n: int = Field(20, ge=1, le=200, description="Number of top recommendations to return")


class DualSourceEntryModel(BaseModel):
    component_id: int
    mpn: str
    category: str
    current_supplier: str
    current_price_usd: float
    recommended_second_source: Optional[str] = None
    second_source_price_usd: Optional[float] = None
    incremental_unit_cost_usd: float
    p_fail_current: float
    p_fail_second: Optional[float] = None
    expected_disruption_cost_usd: float
    risk_reduction_usd: float
    risk_reduction_per_dollar: Optional[float] = None
    tier: str


class DualSourcingResponse(BaseModel):
    entries: List[DualSourceEntryModel]
    no_regret_count: int
    hedge_count: int
    supplier_development_count: int
    # ── Why `entries` is often empty for a BOM (2026-08 audit item 6) ─────────
    # The plan only has something to recommend for SINGLE-SOURCED lines. A BOM whose
    # every line already has two or more distributors has nothing to dual-source — the
    # empty list is the answer, not a missing computation.
    n_bom_lines: Optional[int] = None
    n_single_source_lines: int = 0
    fully_hedged: bool = False
    p_fail_basis: str = ""
    interpretation: str = ""


class SensitivityMetric(StrEnum):
    """The only two metrics the tornado supports.

    Declared as an enum so OpenAPI advertises the constraint. It used to be an
    unconstrained `str` in the schema while the handler 400'd on anything but these
    two — a generated client had no way to know.
    """
    cost = "cost"
    cvar = "cvar"


class SensitivityRequest(BaseModel):
    bom_component_ids: List[int] = Field(
        ..., min_length=1, max_length=MAX_BOM_LINES,
        description="Component IDs in BOM",
    )
    metric: SensitivityMetric = Field(
        SensitivityMetric.cost,
        description="'cost' for landed cost, 'cvar' for tail-risk CVaR-95",
    )


class TornadoBarModel(BaseModel):
    lever: str
    low_label: str
    high_label: str
    low_output: float
    high_output: float
    spread: float


class SensitivityResponse(BaseModel):
    baseline_output: float
    metric: str
    bars: List[TornadoBarModel]
    # A lever with spread 0.0 is a real result — the BOM's outcome does not move when
    # that lever is swung across its whole range. Returning four such bars with no
    # explanation is what made this endpoint look broken, so the zero levers are named
    # and the structural reason is given.
    zero_spread_levers: List[str] = Field(default_factory=list)
    n_bom_lines: int = 0
    n_single_source_lines: int = 0
    interpretation: str = ""


_P_FAIL_BASIS = (
    "p_fail_current / p_fail_second are the CALIBRATED probability that the supplier "
    "suffers a material disruption inside the sourcing horizon: a cited annual base "
    "rate (McKinsey Global Institute 2020 — a disruption lasting a month or longer "
    "every 3.7 years, i.e. 1 - exp(-1/3.7) = 0.2368/yr) converted to the exposure "
    "window (1 - (1-p)**(days/365)) and then rank-shaped by betweenness inside a "
    "bounded band, capped at 0.5. They are NOT centrality scores. Until the 2026-08 "
    "audit this field held min-max normalized betweenness read directly as a "
    "probability, which had no base rate, no exposure window and no unit, implied the "
    "largest distributors fail most often, and gave the single most central "
    "distributor a p_fail of exactly 1.0. GET /stochastic/calibration publishes the "
    "same model per distributor and lets you vary its three parameters."
)


def _recalibrate_dual_sourcing(db: Session, entries: list, gs) -> list:
    """Replace centrality-as-probability with the calibrated disruption probability.

    `app.optimization.recommendations.compute_dual_sourcing_plan` scores p_fail as
    `min(betweenness * stress, 1.0)`. That is the exact defect `/stochastic/calibration`
    documents: a min-max normalized centrality read as a probability, with no base
    rate, no exposure window and no unit — and it implied the largest distributors are
    the most likely to fail. The probabilities, and everything downstream of them
    (`expected_disruption_cost_usd`, `risk_reduction_usd`, `risk_reduction_per_dollar`
    and therefore the ranking), are recomputed here against `gs.p_disruption`, the one
    calibrated model the rest of the app now uses.

    Tiers are untouched: they are decided by incremental unit cost, which no
    probability enters.
    """
    probs: Dict[int, float] = gs.p_disruption or {}
    if not probs or not entries:
        return entries

    from app.graph.simulation import EMERGENCY_COST_PREMIUM

    # The dataclass carries supplier NAMES, not ids, so resolve names -> ids once.
    prob_by_name: Dict[str, float] = {}
    for d in db.query(Distributor).all():
        if d.id in probs:
            prob_by_name[str(d.name)] = probs[d.id]

    for e in entries:
        p_cur = prob_by_name.get(e.current_supplier)
        if p_cur is None:
            # Unresolvable supplier: leave the entry untouched rather than assert a
            # number we cannot justify.
            continue
        e.p_fail_current = round(p_cur, 6)
        e.expected_disruption_cost_usd = round(
            p_cur * EMERGENCY_COST_PREMIUM * e.current_price_usd, 4
        )
        if e.recommended_second_source is None:
            e.p_fail_second = None
            e.risk_reduction_usd = 0.0
            e.risk_reduction_per_dollar = None
            continue
        p_sec = prob_by_name.get(e.recommended_second_source)
        if p_sec is None:
            continue
        e.p_fail_second = round(p_sec, 6)
        # Residual joint-failure model: both sources must fail to disrupt the line.
        rr = (p_cur - p_cur * p_sec) * EMERGENCY_COST_PREMIUM * e.current_price_usd
        e.risk_reduction_usd = round(rr, 4)
        e.risk_reduction_per_dollar = (
            round(rr / e.incremental_unit_cost_usd, 6)
            if e.incremental_unit_cost_usd > 0 else None
        )

    def _key(e):
        tier_rank = 0 if e.tier == "no-regret" else 1
        neg = -e.risk_reduction_per_dollar if e.risk_reduction_per_dollar is not None else float("inf")
        return (tier_rank, neg, -e.expected_disruption_cost_usd)

    entries.sort(key=_key)
    return entries


# ── POST /resilience/criticality-sweep ──────────────────────────────────────

@router.post("/criticality-sweep", response_model=CriticalitySweepResponse)
def post_criticality_sweep(
    body: CriticalitySweepRequest,
    db: Session = Depends(get_db),
):
    """Rank distributors by the single-source exposure they create (orphaned
    components + spend at risk). Pure structural compute, no Monte Carlo."""
    with tracer.start_as_current_span("criticality_sweep") as span:
        span.set_attribute("top_n", body.top_n)
        span.set_attribute("network_wide", body.bom_component_ids is None)
        if body.bom_component_ids is not None and len(body.bom_component_ids) > 200:
            raise HTTPException(status_code=400, detail="bom_component_ids must not exceed 200 items")

        cache_key = _compute_cache_key(
            "criticality-sweep",
            bom_component_ids=sorted(body.bom_component_ids) if body.bom_component_ids else None,
            top_n=body.top_n,
        )
        span.set_attribute("cache_key", cache_key)
        cached = _get_cached_result(db, cache_key)
        if cached:
            span.set_attribute("cache_hit", True)
            return CriticalitySweepResponse(**cached)
        span.set_attribute("cache_hit", False)

        # Full list first so max_spend / rei reflect ALL distributors, then slice.
        gs = _graph(db)
        full = compute_criticality_sweep(db, gs, body.bom_component_ids, top_n=None)
        max_spend = max((e.spend_at_risk_usd for e in full), default=0.0)

        # Scope statistics: how much of the catalogue is single-sourced at all. Without
        # these a page of zeros is unreadable.
        scope_q = db.query(DistributorOffer.component_id, DistributorOffer.distributor_id)
        if body.bom_component_ids is not None:
            scope_q = scope_q.filter(
                DistributorOffer.component_id.in_(body.bom_component_ids)
            )
        dists_by_comp: Dict[int, set] = {}
        for cid, did in scope_q.all():
            dists_by_comp.setdefault(cid, set()).add(did)
        n_components = len(dists_by_comp)
        n_single = sum(1 for s in dists_by_comp.values() if len(s) == 1)
        n_with_exposure = sum(1 for e in full if e.orphan_component_count > 0)
        share = round(100.0 * n_single / n_components, 2) if n_components else 0.0

        if n_with_exposure == 0:
            interpretation = (
                f"No distributor in this scope is the sole source of anything: all "
                f"{n_components} components in scope are carried by two or more of the "
                f"{len(full)} distributors scored. Every spend_at_risk_usd and rei is "
                "therefore legitimately 0.0 — the catalogue is diversified, not the "
                "query broken. Widen the scope (omit bom_component_ids) to see the "
                "network-wide single-source parts."
            )
        else:
            interpretation = (
                f"{n_with_exposure} of {len(full)} distributors are the sole source of "
                f"at least one component. {n_single} of {n_components} components in "
                f"scope ({share}%) are single-sourced; the remaining distributors score "
                "0.0 because they orphan nothing, which is the correct answer for them."
            )

        result = {
            "entries": [asdict(e) for e in full[: body.top_n]],
            "max_spend_at_risk_usd": round(max_spend, 2),
            "network_wide": body.bom_component_ids is None,
            "n_distributors_scored": len(full),
            "n_distributors_with_exposure": n_with_exposure,
            "n_components_in_scope": n_components,
            "n_single_source_components": n_single,
            "single_source_share_pct": share,
            "exposure_definition": (
                "A component is ORPHANED by distributor d iff d is its only offer in "
                "the catalogue. spend_at_risk_usd is the summed average offer price of "
                "d's orphaned components (goods only, one unit per line); rei "
                "normalizes that against the most-exposed distributor. A distributor "
                "that is merely the CHEAPEST source of many parts scores 0.0 here — "
                "that is price exposure, not availability exposure, and it shows up in "
                "/resilience/distributor-failure's cost_substitution instead."
            ),
            "interpretation": interpretation,
        }
        _cache_result(db, "criticality-sweep", cache_key, result)
        return CriticalitySweepResponse(**result)


# ── POST /resilience/dual-sourcing-plan ─────────────────────────────────────

@router.post("/dual-sourcing-plan", response_model=DualSourcingResponse)
def post_dual_sourcing_plan(
    body: DualSourcingRequest,
    db: Session = Depends(get_db),
):
    """Rank single-source components by the payoff of qualifying a second source,
    bucketed into no-regret / hedge / supplier-development tiers."""
    with tracer.start_as_current_span("dual_sourcing_plan") as span:
        span.set_attribute("top_n", body.top_n)
        if body.bom_component_ids is not None and len(body.bom_component_ids) > 200:
            raise HTTPException(status_code=400, detail="bom_component_ids must not exceed 200 items")

        cache_key = _compute_cache_key(
            "dual-sourcing-plan",
            bom_component_ids=sorted(body.bom_component_ids) if body.bom_component_ids else None,
            qualification_cost_usd=body.qualification_cost_usd,
            top_n=body.top_n,
        )
        span.set_attribute("cache_key", cache_key)
        cached = _get_cached_result(db, cache_key)
        if cached:
            span.set_attribute("cache_hit", True)
            return DualSourcingResponse(**cached)
        span.set_attribute("cache_hit", False)

        # Full list first so tier counts are honest across ALL single-source parts.
        gs = _graph(db)
        full = compute_dual_sourcing_plan(
            db, gs, body.bom_component_ids,
            qualification_cost_usd=body.qualification_cost_usd, top_n=None,
        )
        full = _recalibrate_dual_sourcing(db, full, gs)

        n_lines = len(set(body.bom_component_ids)) if body.bom_component_ids else None
        n_ss = len(
            set(gs.single_source_component_ids) & set(body.bom_component_ids)
            if body.bom_component_ids is not None
            else set(gs.single_source_component_ids)
        )
        if not full and n_lines is not None:
            interpretation = (
                f"This BOM is fully hedged: none of its {n_lines} lines is "
                "single-sourced, so there is nothing to dual-source. The empty list is "
                "the finding. Omit bom_component_ids to see the "
                f"{len(gs.single_source_component_ids)} single-source components in the "
                "wider catalogue."
            )
        elif not full:
            interpretation = "No single-source components found in the catalogue."
        else:
            interpretation = (
                f"{len(full)} single-source line(s) in scope"
                + (f" out of {n_lines} BOM lines" if n_lines is not None else "")
                + ". Entries are ranked no-regret first, then by risk reduction per "
                "dollar of incremental unit cost."
            )

        result = {
            "entries": [asdict(e) for e in full[: body.top_n]],
            "no_regret_count": sum(1 for e in full if e.tier == "no-regret"),
            "hedge_count": sum(1 for e in full if e.tier == "hedge"),
            "supplier_development_count": sum(1 for e in full if e.tier == "supplier-development"),
            "n_bom_lines": n_lines,
            "n_single_source_lines": n_ss,
            "fully_hedged": n_lines is not None and n_ss == 0,
            "p_fail_basis": _P_FAIL_BASIS,
            "interpretation": interpretation,
        }
        _cache_result(db, "dual-sourcing-plan", cache_key, result)
        return DualSourcingResponse(**result)


# ── POST /resilience/sensitivity ────────────────────────────────────────────

@router.post("/sensitivity", response_model=SensitivityResponse)
def post_sensitivity(
    body: SensitivityRequest,
    db: Session = Depends(get_db),
):
    """One-way sensitivity (tornado) of a BOM's landed cost or tail-risk CVaR to
    the real model levers, holding all other levers at baseline."""
    with tracer.start_as_current_span("sensitivity_tornado") as span:
        metric = body.metric.value
        span.set_attribute("metric", metric)
        span.set_attribute("bom_size", len(body.bom_component_ids))

        cache_key = _compute_cache_key(
            "sensitivity",
            bom_component_ids=sorted(body.bom_component_ids),
            metric=metric,
        )
        span.set_attribute("cache_key", cache_key)
        cached = _get_cached_result(db, cache_key)
        if cached:
            span.set_attribute("cache_hit", True)
            return SensitivityResponse(**cached)
        span.set_attribute("cache_hit", False)

        gs = _graph(db)
        tornado = compute_tornado(db, gs, body.bom_component_ids, metric=metric)
        bars = [asdict(b) for b in tornado["bars"]]
        zero_levers = [b["lever"] for b in bars if abs(b["spread"]) < 1e-9]

        cids = sorted(set(body.bom_component_ids))
        n_ss = len(set(gs.single_source_component_ids) & set(cids))

        if len(zero_levers) == len(bars) and bars:
            interpretation = (
                f"Every lever has zero spread on this BOM. That is a structural result, "
                f"not a stalled computation: none of its {len(cids)} lines is "
                f"single-sourced ({n_ss} single-source lines), so no distributor outage, "
                "stress multiplier or delivery window in the swept ranges leaves any "
                "line without a supplier — and this tornado's outputs only move when a "
                "line becomes completely unavailable. The cost of losing a supplier you "
                "CAN substitute is priced by /resilience/distributor-failure's "
                "cost_substitution block, not here."
            )
        elif zero_levers:
            interpretation = (
                f"{len(zero_levers)} of {len(bars)} levers do not move this BOM's "
                f"{metric}: {', '.join(zero_levers)}. The BOM is insensitive to them "
                f"over the swept ranges ({n_ss} of {len(cids)} lines are single-sourced)."
            )
        else:
            interpretation = (
                f"All {len(bars)} levers move this BOM's {metric}; bars are ordered by "
                "spread, widest first."
            )

        result = {
            "baseline_output": tornado["baseline_output"],
            "metric": tornado["metric"],
            "bars": bars,
            "zero_spread_levers": zero_levers,
            "n_bom_lines": len(cids),
            "n_single_source_lines": n_ss,
            "interpretation": interpretation,
        }
        _cache_result(db, "sensitivity", cache_key, result)
        return SensitivityResponse(**result)
