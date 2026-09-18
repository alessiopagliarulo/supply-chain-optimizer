"""
Resilience recommendation engine (pure compute).

Three structural analyses that turn the supply graph + real DB fields into
actionable procurement recommendations. Every figure is derived from real
database columns (offer prices, distributor geography, betweenness centrality);
nothing is fabricated.

  1. compute_criticality_sweep    — rank distributors by the single-source
     exposure they create (orphaned components + spend at risk), no Monte Carlo.
  2. compute_dual_sourcing_plan   — rank single-source components by the payoff
     of qualifying a second source (no-regret / hedge / supplier-development).
     Failure probabilities come from the app's ONE calibrated disruption model
     (`graph.disruption.build_failure_probabilities`), never from raw centrality.
  3. compute_tornado              — one-way sensitivity of a BOM's landed cost
     (or tail-risk CVaR) to the real model levers.

These functions are deliberately free of FastAPI/Pydantic so they can be unit
tested against a tiny hand-built session + GraphState fixture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.models.component import Component, DistributorOffer
from app.models.distributor import Distributor
from app.graph.simulation import run_monte_carlo, EMERGENCY_COST_PREMIUM
from app.graph.disruption import build_failure_probabilities

# Cap on how many orphaned component ids we echo back per distributor (payload hygiene).
_ORPHAN_ID_CAP = 25


def _horizon_failure_probabilities(gs) -> Dict[int, float]:
    """The ONE calibrated per-distributor disruption probability, for this module.

    `gs.p_disruption` is built once per graph by
    `app/graph/builder.py::_build_disruption_probabilities`, which delegates to
    `app.graph.disruption.build_failure_probabilities`: a cited annual base
    rate (McKinsey Global Institute 2020 — a disruption lasting a month or longer
    every 3.7 years, i.e. 1 - exp(-1/3.7) = 0.2368/yr) converted to the 60-day PO
    exposure window (1 - (1-p)**(60/365) = 0.0436), then rank-shaped by betweenness
    inside a bounded band (spread**(2u-1), u = tie-aware percentile rank) and capped
    at 0.5.

    Fallback mirrors `graph/simulation.py`: a GraphState built before that field
    existed — or a hand-built test fixture — has an empty `p_disruption`. Rather than
    silently reverting to betweenness-as-probability, derive the SAME calibration on
    the spot from `gs.betweenness`. No second calibration is invented here.
    """
    probs: Dict[int, float] = getattr(gs, "p_disruption", None) or {}
    if probs:
        return probs
    betweenness = getattr(gs, "betweenness", None) or {}
    if not betweenness:
        return {}
    return build_failure_probabilities(sorted(betweenness), betweenness)


# ────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class CriticalityEntry:
    distributor_id: int
    name: str
    country: Optional[str]
    is_domestic: bool
    orphan_component_count: int          # components for which this distributor is the ONLY offer
    orphan_component_ids: List[int]      # capped at _ORPHAN_ID_CAP
    components_supplied: int             # distinct components this distributor offers
    spend_at_risk_usd: float             # sum of avg offer price over its orphaned components
    betweenness: float                   # normalized [0,1] graph betweenness
    rei: float                           # Relative Exposure Index = spend_at_risk / max(spend_at_risk)


@dataclass
class DualSourceEntry:
    component_id: int
    mpn: str
    category: str
    current_supplier: str
    current_price_usd: float
    recommended_second_source: Optional[str]
    second_source_price_usd: Optional[float]
    incremental_unit_cost_usd: float
    p_fail_current: float
    p_fail_second: Optional[float]
    expected_disruption_cost_usd: float
    risk_reduction_usd: float
    risk_reduction_per_dollar: Optional[float]
    tier: str                            # 'no-regret' | 'hedge' | 'supplier-development'


@dataclass
class TornadoBar:
    lever: str
    low_label: str
    high_label: str
    low_output: float
    high_output: float
    spread: float


# ────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ────────────────────────────────────────────────────────────────────────────

def _load_offers(db: Session, bom_component_ids: Optional[List[int]]) -> List[DistributorOffer]:
    q = db.query(DistributorOffer)
    if bom_component_ids is not None:
        q = q.filter(DistributorOffer.component_id.in_(bom_component_ids))
    return q.all()


def _avg_price_by_component(offers: List[DistributorOffer]) -> Dict[int, float]:
    """Average real offer price per component across the supplied offer set."""
    sums: Dict[int, float] = {}
    counts: Dict[int, int] = {}
    for o in offers:
        sums[o.component_id] = sums.get(o.component_id, 0.0) + (o.price or 0.0)
        counts[o.component_id] = counts.get(o.component_id, 0) + 1
    return {cid: sums[cid] / counts[cid] for cid in sums if counts[cid]}


def _component_cost(offers: List[DistributorOffer]) -> float:
    """Raw BOM component cost = sum of each line's average real offer price."""
    return sum(_avg_price_by_component(offers).values())


# ────────────────────────────────────────────────────────────────────────────
# 1. Distributor-criticality sweep (exact structural compute, NO Monte Carlo)
# ────────────────────────────────────────────────────────────────────────────

def compute_criticality_sweep(
    db: Session,
    gs,
    bom_component_ids: Optional[List[int]] = None,
    top_n: Optional[int] = 20,
) -> List[CriticalityEntry]:
    """Rank distributors by the single-source exposure they create.

    A component is *orphaned* by distributor d iff d is its only offer
    (dists_by_component[cid] == {d}) — the same "no alternative" definition used
    by the distributor-failure endpoint. Spend-at-risk is the summed average real
    offer price of a distributor's orphaned components. The Relative Exposure
    Index (rei) normalizes that against the most-exposed distributor.

    top_n=None returns every distributor (used by the endpoint to derive the
    network-wide max before slicing); otherwise the sorted list is sliced.
    """
    offers = _load_offers(db, bom_component_ids)

    dists_by_component: Dict[int, Set[int]] = {}
    components_by_dist: Dict[int, Set[int]] = {}
    for o in offers:
        dists_by_component.setdefault(o.component_id, set()).add(o.distributor_id)
        components_by_dist.setdefault(o.distributor_id, set()).add(o.component_id)

    avg_price = _avg_price_by_component(offers)

    # distributor metadata
    dist_rows = {d.id: d for d in db.query(Distributor).all()}

    entries: List[CriticalityEntry] = []
    for did, comp_set in components_by_dist.items():
        orphan_ids = sorted(
            cid for cid in comp_set if dists_by_component.get(cid) == {did}
        )
        spend_at_risk = sum(avg_price.get(cid, 0.0) for cid in orphan_ids)
        d = dist_rows.get(did)
        entries.append(
            CriticalityEntry(
                distributor_id=did,
                name=d.name if d else f"dist_{did}",
                country=d.country if d else None,
                is_domestic=bool(d.is_domestic) if d else False,
                orphan_component_count=len(orphan_ids),
                orphan_component_ids=orphan_ids[:_ORPHAN_ID_CAP],
                components_supplied=len(comp_set),
                spend_at_risk_usd=round(spend_at_risk, 2),
                betweenness=round(gs.betweenness.get(did, 0.0), 6),
                rei=0.0,  # filled in below once the max is known
            )
        )

    # Relative Exposure Index against the most-exposed distributor.
    max_spend = max((e.spend_at_risk_usd for e in entries), default=0.0)
    for e in entries:
        e.rei = round(e.spend_at_risk_usd / max_spend, 6) if max_spend > 0 else 0.0

    entries.sort(
        key=lambda e: (-e.orphan_component_count, -e.spend_at_risk_usd, -e.betweenness)
    )
    if top_n is not None:
        entries = entries[:top_n]
    return entries


# ────────────────────────────────────────────────────────────────────────────
# 2. Ranked dual-sourcing plan
# ────────────────────────────────────────────────────────────────────────────

def compute_dual_sourcing_plan(
    db: Session,
    gs,
    bom_component_ids: Optional[List[int]] = None,
    qualification_cost_usd: float = 0.0,
    stress_factor: float = 1.0,
    top_n: Optional[int] = 20,
) -> List[DualSourceEntry]:
    """Rank single-source components by the payoff of qualifying a second source.

    For each single-source component (optionally ∩ the BOM):
      - the sole distributor supplies it at real price1; its failure probability is
        the CALIBRATED horizon disruption probability
        `min(gs.p_disruption[d] * stress_factor, 1.0)` — see
        `_horizon_failure_probabilities`.
      - candidates = any other distributor offering the component. None ⇒
        'supplier-development' tier (must build a new supplier, no ROI yet).
      - otherwise pick the cheapest candidate (tie-break: lowest betweenness) and
        score the hedge with the emergency-premium disruption model.

    Tiers:
      - 'no-regret'            : second source costs nothing extra (incremental == 0)
      - 'hedge'               : second source costs more, ranked by risk_reduction_per_dollar
      - 'supplier-development' : no alternative exists in the data

    ON p_fail — WHAT THIS USED TO DO AND WHY IT WAS WRONG
    ----------------------------------------------------
    Until the 2026-08 repair this function computed

        p_fail = min(betweenness.get(did, 0.0) * stress_factor, 1.0)

    where `betweenness` is a graph centrality score. That expression has no base rate,
    no exposure window and no unit: a centrality score was published to the UI as
    `p_fail_current` / `p_fail_second`, i.e. as a probability. It is the same defect
    already repaired in `graph/builder.py` and `graph/simulation.py`, where the extra
    min-max normalization on betweenness (since removed) forced the maximum to exactly
    1.0, made the most central distributor fail in 100% of scenarios, and left 91 of
    92 distributors with zero measurable impact on `/resilience/distributor-failure`.

    Removing that normalization did not repair THIS call site, it only changed which
    way the numbers were wrong. On the live catalogue raw betweenness runs from 0.0 to
    0.2914851 with a median of 0.000683 across 92 distributors (re-measured 2026-09-03
    after the dead 20% holdout carve was removed from `graph/builder.py`, which grew
    the graph from 5,789 to 7,363 edges), so the endpoint published a
    typical sole supplier as carrying a ~0.07% chance of disruption over a purchase
    order (the calibrated figure is 4.4%) — and the 16 distributors whose betweenness
    is exactly 0.0 as being incapable of failing at all, which zeroed
    their `expected_disruption_cost_usd` and `risk_reduction_usd` and sank every part
    they single-source to the bottom of the recommendation ranking. The expression also
    asserts, silently, that the LARGEST distributors are the most likely to fail.

    `p_fail` now comes from the single calibrated model the whole app shares
    (`app.graph.disruption.build_failure_probabilities`), so the level is a cited base rate over a stated
    exposure window and centrality only rank-orders relative risk inside a bounded
    band. `expected_disruption_cost_usd`, `risk_reduction_usd`,
    `risk_reduction_per_dollar` and the ranking all move with it. Tiers do not: they
    are decided by incremental unit cost, which no probability enters.

    ON stress_factor — KEPT, AND WHY
    --------------------------------
    It survives the change because its meaning survives it. It is no longer a
    magnitude being read off a centrality score; it is a scenario multiplier applied
    to an already-calibrated probability, exactly as `graph/simulation.py::
    run_monte_carlo` applies it (`min(p_disruption[d] * stress, 1.0)`), and exactly
    what the tornado's `geopolitical_stress` lever means. 1.0 (the only value any
    caller currently passes) is the baseline no-op; >1.0 models a macro stress spike.
    Dropping it would leave this module unable to answer the stressed what-if that its
    sibling simulator answers.
    """
    ss_ids: Set[int] = set(gs.single_source_component_ids)
    if bom_component_ids is not None:
        ss_ids &= set(bom_component_ids)
    if not ss_ids:
        return []

    offers = db.query(DistributorOffer).filter(
        DistributorOffer.component_id.in_(ss_ids)
    ).all()
    comps = {c.id: c for c in db.query(Component).filter(Component.id.in_(ss_ids)).all()}
    dist_rows = {d.id: d for d in db.query(Distributor).all()}
    betweenness = gs.betweenness
    # Calibrated horizon disruption probability -- NOT centrality. See the docstring.
    p_disruption = _horizon_failure_probabilities(gs)

    # component_id -> {distributor_id -> best (min) offer price}, plus stocked flag
    price_by_dist: Dict[int, Dict[int, float]] = {}
    stocked_by_dist: Dict[int, Set[int]] = {}
    for o in offers:
        pd = price_by_dist.setdefault(o.component_id, {})
        price = o.price if o.price is not None else 0.0
        if o.distributor_id not in pd or price < pd[o.distributor_id]:
            pd[o.distributor_id] = price
        if (o.stock or 0) > 0:
            stocked_by_dist.setdefault(o.component_id, set()).add(o.distributor_id)

    entries: List[DualSourceEntry] = []
    for cid in ss_ids:
        pd = price_by_dist.get(cid)
        if not pd:
            continue  # no offers at all — nothing to recommend

        # Sole supplier = the single stocked distributor; fall back to cheapest offer.
        stocked = stocked_by_dist.get(cid, set())
        if len(stocked) == 1:
            sole_did = next(iter(stocked))
        else:
            sole_did = min(pd, key=lambda d: pd[d])
        price1 = pd[sole_did]
        p_fail_current = min(p_disruption.get(sole_did, 0.0) * stress_factor, 1.0)
        expected_disruption = p_fail_current * EMERGENCY_COST_PREMIUM * price1

        comp = comps.get(cid)
        mpn = comp.mpn if comp else f"c_{cid}"
        category = (comp.category if comp else None) or "Unknown"
        current_supplier = dist_rows[sole_did].name if sole_did in dist_rows else f"dist_{sole_did}"

        candidates = [d for d in pd if d != sole_did]
        if not candidates:
            entries.append(DualSourceEntry(
                component_id=cid, mpn=mpn, category=category,
                current_supplier=current_supplier, current_price_usd=round(price1, 4),
                recommended_second_source=None, second_source_price_usd=None,
                incremental_unit_cost_usd=round(qualification_cost_usd, 4),
                p_fail_current=round(p_fail_current, 6), p_fail_second=None,
                expected_disruption_cost_usd=round(expected_disruption, 4),
                risk_reduction_usd=0.0, risk_reduction_per_dollar=None,
                tier="supplier-development",
            ))
            continue

        # Cheapest candidate, tie-break lowest betweenness.
        cand = min(candidates, key=lambda d: (pd[d], betweenness.get(d, 0.0)))
        price2 = pd[cand]
        p_fail_second = min(p_disruption.get(cand, 0.0) * stress_factor, 1.0)
        # Residual joint-failure model: both sources must fail to disrupt the line.
        risk_reduction = (p_fail_current - p_fail_current * p_fail_second) * EMERGENCY_COST_PREMIUM * price1
        incremental = max(0.0, price2 - price1) + qualification_cost_usd

        if incremental > 0:
            rr_per_dollar = risk_reduction / incremental
            tier = "hedge"
        else:
            rr_per_dollar = None
            tier = "no-regret"

        entries.append(DualSourceEntry(
            component_id=cid, mpn=mpn, category=category,
            current_supplier=current_supplier, current_price_usd=round(price1, 4),
            recommended_second_source=(dist_rows[cand].name if cand in dist_rows else f"dist_{cand}"),
            second_source_price_usd=round(price2, 4),
            incremental_unit_cost_usd=round(incremental, 4),
            p_fail_current=round(p_fail_current, 6),
            p_fail_second=round(p_fail_second, 6),
            expected_disruption_cost_usd=round(expected_disruption, 4),
            risk_reduction_usd=round(risk_reduction, 4),
            risk_reduction_per_dollar=(round(rr_per_dollar, 6) if rr_per_dollar is not None else None),
            tier=tier,
        ))

    # Sort: no-regret first, then risk_reduction_per_dollar desc, then exposure desc.
    def _key(e: DualSourceEntry):
        tier_rank = 0 if e.tier == "no-regret" else 1
        neg_rrpd = -e.risk_reduction_per_dollar if e.risk_reduction_per_dollar is not None else float("inf")
        return (tier_rank, neg_rrpd, -e.expected_disruption_cost_usd)

    entries.sort(key=_key)
    if top_n is not None:
        entries = entries[:top_n]
    return entries


# ────────────────────────────────────────────────────────────────────────────
# 3. One-way sensitivity / tornado
# ────────────────────────────────────────────────────────────────────────────

def compute_tornado(
    db: Session,
    gs,
    bom_component_ids: List[int],
    metric: str = "cost",
) -> dict:
    """One-way sensitivity of a BOM's landed cost (or tail-risk CVaR) to the real
    model levers, holding all other levers at baseline.

    Levers (ranges = the live UI slider bounds):
      - geopolitical stress            : stress_factor 1.0 → 2.0
      - delivery target days           : 30 → 7 (force-fail distributors slower than target)
      - most-critical distributor      : available → forced outage of the #1 criticality entry
      - emergency premium              : 0.10 → 0.20

    metric='cost' returns component landed cost (component_cost * mean_cost_inflation);
    metric='cvar' returns the CVaR-95 tail multiplier.
    """
    # Lazy import avoids a circular dependency (resilience imports this module).
    from app.api.resilience import _distributor_lead_days

    offers = _load_offers(db, bom_component_ids)
    component_cost = _component_cost(offers)

    def _output(sim) -> float:
        if metric == "cvar":
            return sim.cvar_95
        return component_cost * sim.mean_cost_inflation

    def _mc(**kwargs):
        return run_monte_carlo(gs, bom_component_ids=bom_component_ids, **kwargs)

    baseline_output = _output(_mc())

    # Distributors supplying this BOM (only these matter to the simulation).
    bom_dist_ids = {o.distributor_id for o in offers}
    dist_rows = {
        d.id: d
        for d in db.query(Distributor).filter(Distributor.id.in_(bom_dist_ids)).all()
    }

    bars: List[TornadoBar] = []

    # Lever 1: geopolitical stress 1.0 → 2.0
    bars.append(TornadoBar(
        lever="geopolitical_stress",
        low_label="stress 1.0x", high_label="stress 2.0x",
        low_output=round(_output(_mc(stress_factor=1.0)), 2),
        high_output=round(_output(_mc(stress_factor=2.0)), 2),
        spread=0.0,
    ))

    # Lever 2: delivery target days 30 → 7 (tighter window fails slower distributors).
    def _fail_slower_than(target_days: float) -> Set[int]:
        return {
            did for did, d in dist_rows.items()
            if _distributor_lead_days(d) > target_days
        }
    bars.append(TornadoBar(
        lever="delivery_target_days",
        low_label="30 days", high_label="7 days",
        low_output=round(_output(_mc(forced_failures=_fail_slower_than(30))), 2),
        high_output=round(_output(_mc(forced_failures=_fail_slower_than(7))), 2),
        spread=0.0,
    ))

    # Lever 3: most-critical distributor available → forced outage.
    top_crit = compute_criticality_sweep(db, gs, bom_component_ids, top_n=1)
    if top_crit:
        crit = top_crit[0]
        bars.append(TornadoBar(
            lever="critical_distributor_availability",
            low_label="available",
            high_label=f"{crit.name} down",
            low_output=round(baseline_output, 2),
            high_output=round(_output(_mc(forced_failures={crit.distributor_id})), 2),
            spread=0.0,
        ))

    # Lever 4: emergency premium 0.10 → 0.20.
    bars.append(TornadoBar(
        lever="emergency_premium",
        low_label="premium 0.10", high_label="premium 0.20",
        low_output=round(_output(_mc(emergency_premium=0.10)), 2),
        high_output=round(_output(_mc(emergency_premium=0.20)), 2),
        spread=0.0,
    ))

    for b in bars:
        b.spread = round(abs(b.high_output - b.low_output), 2)
    bars.sort(key=lambda b: -b.spread)

    return {
        "baseline_output": round(baseline_output, 2),
        "metric": metric,
        "bars": bars,
    }
