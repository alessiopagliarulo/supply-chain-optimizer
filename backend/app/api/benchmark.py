"""
Benchmark API endpoints (04-02).

Public graph-analytics endpoints:
  GET /benchmark/fiedler-curve         — Sequential-removal λ₂ curve from GraphState
  GET /benchmark/single-source-components — Real component MPN+manufacturer+sole-source distributor

The sourcing MILP benchmark endpoints (`/benchmark/summary`, `/benchmark/cascade-heatmap`,
`/benchmark/diversification-frontier`) were removed with the sourcing optimizer; that work
is archived at git tag `archive/sourcing-v1`.

All endpoints are unauthenticated — public aggregate analytics, no user data (T-04-02-03/04).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db

router = APIRouter(prefix="/benchmark", tags=["benchmark"])


class FiedlerPoint(BaseModel):
    step: int
    removed: Optional[int] = None
    removed_name: Optional[str] = None
    lambda2: float
    delta_pct: float
    # Reference BOMs with at least one line that has NO supplier left in the graph
    # once the distributors up to and including this step have been removed.
    # Cumulative, and computed on the same graph λ₂ is computed on. Empty means
    # "checked, none collapsed" — `boms_checked` on the response says how many
    # BOMs stood behind that check, so an empty list is never ambiguous.
    collapsed_boms: List[str] = []


class FiedlerCurveResponse(BaseModel):
    points: List[FiedlerPoint]
    baseline_lambda2: float
    # How many reference BOMs the collapse check covered, and where they came
    # from. `boms_checked == 0` means the check could not run (no benchmarked
    # BOMs in the database) — the UI must then say so rather than rendering
    # "all BOMs remain fulfillable".
    boms_checked: int = 0
    bom_source: str = ""


class SingleSourceComponent(BaseModel):
    component_id: int
    mpn: str
    manufacturer: str
    distributor_id: int
    distributor_name: str


class SingleSourceComponentsResponse(BaseModel):
    components: List[SingleSourceComponent]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_graph_state():
    from app.graph import get_graph_state
    from app.startup import wait_for_graph

    # The graph build moved off the lifespan onto a background thread (app/startup.py),
    # so a request can now arrive before it has finished. Wait for that ONE build
    # rather than starting another or answering from a half-built graph. Returns
    # immediately when no warm-up is running — which is what keeps the "no graph
    # state -> 503" tests meaningful.
    wait_for_graph()
    gs = get_graph_state()
    if gs is None:
        raise HTTPException(
            status_code=503,
            detail="Graph not loaded — server starting up or graph build failed",
        )
    return gs


@router.get("/fiedler-curve", response_model=FiedlerCurveResponse)
def get_fiedler_curve():
    """
    Return the sequential-removal Fiedler λ₂ curve from pre-computed GraphState.

    Step 0 is the baseline (no removal). Subsequent steps show λ₂ after removing
    the most-central distributor.

    `collapsed_boms` lists the reference BOMs that have at least one line with no
    remaining supplier once every distributor up to that step is gone. It is
    computed in `main.compute_fiedler_curve` against the SAME graph λ₂ is computed
    on (the 80% training partition of the offer table), so the two columns of the
    chart describe one network, not two. `boms_checked` and `bom_source` disclose
    how many BOMs the check covered and where they came from; when `boms_checked`
    is 0 the check did not run and an empty `collapsed_boms` means nothing.
    """
    gs = _require_graph_state()

    if not gs.fiedler_curve:
        raise HTTPException(
            status_code=503,
            detail="Fiedler curve not computed — check server startup logs",
        )

    points = []
    for entry in gs.fiedler_curve:
        points.append(FiedlerPoint(
            step=entry["step"],
            removed=entry.get("removed"),
            removed_name=entry.get("removed_name"),
            lambda2=entry["lambda2"],
            delta_pct=entry["delta_pct"],
            collapsed_boms=entry.get("collapsed_boms", []),
        ))

    head = gs.fiedler_curve[0]
    baseline_lambda2 = head["lambda2"]
    return FiedlerCurveResponse(
        points=points,
        baseline_lambda2=baseline_lambda2,
        boms_checked=int(head.get("boms_checked", 0) or 0),
        bom_source=str(head.get("bom_source", "") or ""),
    )


@router.get("/single-source-components", response_model=SingleSourceComponentsResponse)
def get_single_source_components(db: Session = Depends(get_db)):
    """
    Return real component MPN + manufacturer + sole-source distributor from ORM joins.

    Reads GraphState.single_source_component_ids (frozenset[int]) then joins to
    Component and DistributorOffer tables to return authoritative catalog data.

    CRITICAL: mpn and manufacturer come ONLY from the Component ORM row — no fabricated
    strings, no distributor fields used as manufacturer values (VIZ-02, D-05).
    """
    gs = _require_graph_state()

    component_ids = list(gs.single_source_component_ids)
    if not component_ids:
        return SingleSourceComponentsResponse(components=[])

    from app.models.component import Component, DistributorOffer
    from app.models.distributor import Distributor

    components = (
        db.query(Component)
        .filter(Component.id.in_(component_ids))
        .all()
    )

    results = []
    for comp in components:
        # Find stocked offers for this component
        offers = (
            db.query(DistributorOffer)
            .filter(
                DistributorOffer.component_id == comp.id,
                DistributorOffer.stock > 0,
            )
            .all()
        )
        if not offers:
            # Fallback: any offer
            offers = (
                db.query(DistributorOffer)
                .filter(DistributorOffer.component_id == comp.id)
                .limit(1)
                .all()
            )
        if not offers:
            continue

        offer = offers[0]
        dist = (
            db.query(Distributor)
            .filter(Distributor.id == offer.distributor_id)
            .first()
        )
        if not dist:
            continue

        results.append(SingleSourceComponent(
            component_id=comp.id,
            mpn=comp.mpn,                              # REAL catalog MPN
            manufacturer=comp.manufacturer or "Unknown",   # REAL manufacturer name
            distributor_id=dist.id,
            distributor_name=dist.name,
        ))

    return SingleSourceComponentsResponse(components=results)
