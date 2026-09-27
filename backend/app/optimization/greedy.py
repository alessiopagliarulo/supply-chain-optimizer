"""
Naive greedy and ADD-heuristic sourcing baselines.

These exist to honestly benchmark `solve_sourcing`'s CP-SAT MILP
(app.optimization.sourcing): the same pre-filters (outlier filter, us_only
domestic filter) and the exact same cost model (`_freight_model_by_did`,
`StrategyWeights.consolidation_bonus_usd`) are reused unchanged, so a
MILP-vs-greedy cost comparison is not rigged by scoring the two solvers with
different yardsticks.

PRICE RESOLUTION. These baselines pick and score on full floats. The MILP has
to hand CP-SAT integers, so it prices on the grid defined by
`sourcing.to_obj_units` (milli-cents, `OBJ_SCALE = 100_000`) — re-exported
below so this module names the resolution the MILP actually uses. A milli-cent
grid puts at most $5e-6/unit between the two arms, which is why
`landed_cost_breakdown` can reproduce `SourcingResult.objective_usd` to within
integer rounding (`test_greedy.py::test_milp_objective_equals_landed_cost_breakdown`).
Until 2026-08-28 the MILP priced on WHOLE CENTS instead — a ~6% error on the 15
components with sub-$0.10 offers and an exact zero on the $0.0031 one — so the
two arms were genuinely optimising different objectives. Do NOT quantise the
baselines to match: floats are the more accurate side, and the invariant that
matters is agreement to within the MILP's own rounding, not bug-compatibility.

`solve_sourcing_greedy` — myopic per-line cheapest-feasible-offer picker.
No fixed-charge/consolidation awareness at all: each BOM line is sourced
independently, which is exactly the naive baseline the MILP is meant to beat.

`solve_sourcing_greedy_add` — Kuehn & Hamburger (1963) style ADD / local
search heuristic. Starts from the naive greedy solution above, then
repeatedly looks for the single best BOM-line reassignment onto an
already-opened distributor that strictly reduces total landed cost, applying
one such improving move per pass until none remain (or an iteration cap is
hit). This captures some of the consolidation benefit the MILP gets "for
free" from being a single joint optimization.
"""
from __future__ import annotations

from typing import Dict, List

from app.optimization.sourcing import (
    BomLine,
    Offer,
    SourcingAssignment,
    SourcingResult,
    filter_price_outliers,
    _freight_model_by_did,
    OBJ_SCALE,  # noqa: F401 -- re-exported so callers share the exact scale MILP uses
    to_obj_units,  # noqa: F401 -- ditto: the ONE USD -> objective-unit conversion
)
from app.optimization.strategies import StrategyWeights


# ── Shared pre-filtering (mirrors solve_sourcing exactly) ────────────────────

def _prefilter(
    bom: List[BomLine],
    offers: List[Offer],
    us_only: bool,
):
    """
    Apply the identical pre-filters solve_sourcing applies before building
    its MILP: outlier filter, then (optionally) domestic-only filter. Raises
    the same ValueError types/messages solve_sourcing raises for the same
    infeasibility conditions, so callers can treat either solver identically.
    """
    if not bom:
        raise ValueError("BOM is empty — cannot solve sourcing with zero components")

    offers, drops = filter_price_outliers(offers, bom)

    if us_only:
        offers = [o for o in offers if o.is_domestic]

    offers_by_component: Dict[int, List[Offer]] = {}
    for o in offers:
        offers_by_component.setdefault(o.component_id, []).append(o)

    missing = [b.mpn for b in bom if not offers_by_component.get(b.component_id)]
    if missing:
        raise ValueError(
            f"No valid offers for components after filtering: {missing}"
        )

    return offers, drops, offers_by_component


# ── Per-line feasibility (single offer must cover the WHOLE line) ───────────

def _feasible_offers_for_line(b: BomLine, candidates: List[Offer]) -> List[Offer]:
    """
    Offers that can single-handedly cover a BOM line's full demand.

    A single-source assignment needs stock covering the greater of MOQ and
    demand (you can't place an order under MOQ, and stock must clear the
    quantity actually needed). If nothing clears that bar, fall back to
    plain stock >= demand (covers MOQ==1 offers, or lines where demand
    already exceeds MOQ).
    """
    strict = [o for o in candidates if o.stock >= max(o.moq, b.quantity)]
    if strict:
        return strict
    return [o for o in candidates if o.stock >= b.quantity]


def _cheapest_feasible_offer(b: BomLine, candidates: List[Offer]) -> Offer:
    feasible = _feasible_offers_for_line(b, candidates)
    if not feasible:
        # No single offer can fully cover this line's demand — fall back to
        # the cheapest offer with any stock at all rather than raising, so
        # the naive baseline always produces a complete assignment set.
        feasible = [o for o in candidates if o.stock > 0] or candidates
    return min(feasible, key=lambda o: o.price_usd)


def _build_cheapest_per_line(
    bom: List[BomLine],
    offers_by_component: Dict[int, List[Offer]],
) -> List[SourcingAssignment]:
    assignments: List[SourcingAssignment] = []
    for b in bom:
        candidates = offers_by_component[b.component_id]
        chosen = _cheapest_feasible_offer(b, candidates)
        assignments.append(SourcingAssignment(
            component_id=b.component_id,
            mpn=b.mpn,
            distributor_id=chosen.distributor_id,
            distributor_name=chosen.distributor_name,
            quantity=b.quantity,
            unit_price_usd=chosen.price_usd,
        ))
    return assignments


# ── Shared landed-cost scoring (anti-rigging: calls the real MILP helper) ──

def landed_cost_breakdown(
    assignments: List[SourcingAssignment],
    offers: List[Offer],
    bom: List[BomLine],
    weights: StrategyWeights,
) -> dict:
    """
    Score an assignment set with the exact same cost model solve_sourcing's
    objective minimizes — the shared `_freight_model_by_did` helper, not a
    reimplementation:

        component_cost
      + transport_fixed     = sum over OPENED distributors d of fixed[d]
      + transport_variable  = sum over d of per_unit[d] x units actually shipped from d
      + consolidation_charge

    The split matters. `transport_fixed` is a per-visit charge, so it grows with
    the number of suppliers opened (that is the fixed-charge economics the MILP
    exists to optimize). `transport_variable` is charged on real shipped
    quantity, so splitting a BOM across N suppliers ALLOCATES one BOM's variable
    freight among them — it does not multiply it by N. An earlier version charged
    every opened distributor a full representative-BOM shipment weight, which
    over-penalised splitting and inflated the MILP's apparent advantage.

    Sign convention (verified against solve_sourcing's model.Minimize call):
    transport and consolidation terms are ALL added as positive costs —
    consolidation_bonus_usd is not a subtracted discount, it's a flat
    per-distributor charge just like the fixed freight fee, so minimizing the sum
    naturally rewards using fewer distributors.

    `bom` is accepted for signature symmetry with the solvers (and validated
    callers); the freight model no longer needs it, because freight now depends
    on the assignment's real quantities rather than a BOM-wide average.
    """
    component_cost = sum(a.quantity * a.unit_price_usd for a in assignments)

    used_dids = sorted({a.distributor_id for a in assignments if a.quantity > 0})

    units_by_did: Dict[int, int] = {}
    for a in assignments:
        if a.quantity > 0:
            units_by_did[a.distributor_id] = (
                units_by_did.get(a.distributor_id, 0) + a.quantity
            )

    penalty_scale = getattr(weights, "transport_penalty_scale", 1.0)
    freight = _freight_model_by_did(offers, penalty_scale)

    transport_fixed = sum(freight.fixed_by_did.get(did, 0.0) for did in used_dids)
    transport_variable = sum(
        freight.per_unit_by_did.get(did, 0.0) * units
        for did, units in units_by_did.items()
    )

    consolidation_bonus = getattr(weights, "consolidation_bonus_usd", 1.0)
    consolidation_charge = consolidation_bonus * len(used_dids)

    total_cost = (
        component_cost + transport_fixed + transport_variable + consolidation_charge
    )

    return {
        "component_cost": component_cost,
        "transport_fixed": transport_fixed,
        "transport_variable": transport_variable,
        "transport_total": transport_fixed + transport_variable,
        "consolidation_charge": consolidation_charge,
        "total_cost": total_cost,
        "n_distinct_suppliers": len(used_dids),
    }


# ── Public solvers ───────────────────────────────────────────────────────────

def solve_sourcing_greedy(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    us_only: bool = True,
) -> SourcingResult:
    """
    Naive baseline: for each BOM line independently, pick the cheapest offer
    whose stock covers demand (MOQ-aware). Deliberately myopic — no
    fixed-charge/consolidation awareness — so it can serve as a fair "no
    optimization" comparison point for the MILP.
    """
    offers, drops, offers_by_component = _prefilter(bom, offers, us_only)
    assignments = _build_cheapest_per_line(bom, offers_by_component)
    breakdown = landed_cost_breakdown(assignments, offers, bom, weights)

    return SourcingResult(
        assignments=assignments,
        total_component_cost=breakdown["component_cost"],
        selected_distributor_ids=sorted({a.distributor_id for a in assignments}),
        outlier_drops=drops,
        status="GREEDY",
    )


def solve_sourcing_greedy_add(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    us_only: bool = True,
) -> SourcingResult:
    """
    Kuehn & Hamburger (1963) style ADD/local-search heuristic.

    Starts from the naive per-line-cheapest solution, then repeatedly finds
    the single best reassignment of one BOM line onto a distributor already
    opened by another line (feasible for that line's stock/MOQ) that
    strictly reduces total landed cost, and applies it. Repeats until no
    single-line reassignment improves total cost, capped at len(bom) passes
    to guarantee termination.
    """
    offers, drops, offers_by_component = _prefilter(bom, offers, us_only)
    assignments = _build_cheapest_per_line(bom, offers_by_component)
    bom_by_cid = {b.component_id: b for b in bom}

    max_passes = max(len(bom), 1)
    for _ in range(max_passes):
        current_cost = landed_cost_breakdown(assignments, offers, bom, weights)["total_cost"]
        best_move = None  # (index, new_assignment, new_total_cost)

        for i, a in enumerate(assignments):
            b = bom_by_cid[a.component_id]
            opened_dids = {
                other.distributor_id for j, other in enumerate(assignments) if j != i
            }
            if not opened_dids:
                continue

            candidates = [
                o for o in offers_by_component[b.component_id]
                if o.distributor_id in opened_dids and o.distributor_id != a.distributor_id
            ]
            candidates = _feasible_offers_for_line(b, candidates)

            for o in candidates:
                trial = list(assignments)
                trial[i] = SourcingAssignment(
                    component_id=a.component_id,
                    mpn=a.mpn,
                    distributor_id=o.distributor_id,
                    distributor_name=o.distributor_name,
                    quantity=b.quantity,
                    unit_price_usd=o.price_usd,
                )
                trial_cost = landed_cost_breakdown(trial, offers, bom, weights)["total_cost"]
                if trial_cost < current_cost - 1e-9 and (
                    best_move is None or trial_cost < best_move[2]
                ):
                    best_move = (i, trial[i], trial_cost)

        if best_move is None:
            break
        idx, new_assignment, _new_cost = best_move
        assignments[idx] = new_assignment

    breakdown = landed_cost_breakdown(assignments, offers, bom, weights)

    return SourcingResult(
        assignments=assignments,
        total_component_cost=breakdown["component_cost"],
        selected_distributor_ids=sorted({a.distributor_id for a in assignments}),
        outlier_drops=drops,
        status="GREEDY_ADD",
    )
