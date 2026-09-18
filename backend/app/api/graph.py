"""
Graph ML API endpoints.

GET  /graph/metrics   — Returns supply graph topology metrics (no auth required).
POST /graph/simulate  — Runs Monte Carlo cascade simulation (no auth required).

Both endpoints are public — they expose only aggregate analytics with no prices,
user data, or sensitive offer details (T-02-04 mitigation).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/graph", tags=["graph"])


# ── Request / Response schemas ────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    # min_length=1: an empty BOM used to return p10=p50=p90=cvar_95=1.0 -- byte
    # identical to the answer for a real 5-part BOM -- which is what proved the
    # endpoint was ignoring its input. There is no fulfillment distribution over zero
    # lines, so an empty list is a client error, not a result.
    bom_component_ids: List[int] = Field(..., min_length=1, max_length=200)

    @field_validator("bom_component_ids")
    @classmethod
    def _positive_ids(cls, v: List[int]) -> List[int]:
        if any(cid <= 0 for cid in v):
            raise ValueError("bom_component_ids must all be positive integers")
        return v


class SimulateScope(BaseModel):
    """
    The structure the percolation actually ran over.

    A genuinely well-hedged BOM produces p10 = p50 = p90 = 1.0, which is a real
    result -- but it is indistinguishable from a stub returning constants unless the
    inputs are published alongside. These fields are that evidence.
    """
    n_bom_lines: int
    n_suppliers_in_scope: int
    n_single_source_lines: int
    n_unsupplied_lines: int
    n_scenarios_with_shortfall: int
    worst_fulfillment: float
    mean_fulfillment: float
    mean_cost_inflation: float
    p_fail_min: float
    p_fail_median: float
    p_fail_max: float


class SimulateResponse(BaseModel):
    p10: float
    p50: float
    p90: float
    cvar_95: float
    n_scenarios: int
    seed: int
    scope: SimulateScope
    interpretation: str
    caveats: List[str]


class GraphMetricsResponse(BaseModel):
    n_distributors: int
    n_components: int
    # TRUE edge count of the graph these metrics describe. This used to be the raw
    # DistributorOffer row count (8,176) reported as the edge count of a smaller
    # graph. n_offer_rows and n_duplicate_offer_rows account for the whole
    # difference: n_edges + n_duplicate_offer_rows == n_offer_rows. (A third field,
    # n_holdout_offer_rows, was removed 2026-09-03 with the dead 20% holdout carve
    # that produced it.)
    n_edges: int
    n_offer_rows: int
    n_duplicate_offer_rows: int
    # Algebraic connectivity (Fiedler value / λ₂), reported honestly as TWO numbers —
    # do not conflate them:
    fiedler_whole_graph: float       # λ₂ of the ENTIRE graph. Exactly 0.0 whenever the
                                      # graph is disconnected (n_connected_components > 1),
                                      # which it is here — this is the mathematically
                                      # correct answer, not a computation failure.
    fiedler_giant_component: float   # λ₂ of the LARGEST connected component only. This is
                                      # the informative number — how tightly-knit the main
                                      # supplier network is, ignoring isolated/orphan nodes.
    n_connected_components: int      # total connected components in the whole graph
    giant_component_size: int        # node count (distributors + components) in the giant component
    giant_component_fraction: float  # giant_component_size / (n_distributors + n_components)
    single_source_count: int
    betweenness: Dict[str, float]   # str keys for JSON serialization (distributor_id)
    pagerank: Dict[str, float]
    # What the numbers above mean, so neither is read as something it is not. Both
    # are RAW scores now: the min-max rescale that used to be applied has been removed
    # (it forced max -> 1.0 / min -> 0.0 regardless of spread, and mapped PageRank's
    # zero-range vector to all-zeros).
    centrality_notes: Dict[str, str]
    # Per-distributor probability of a material disruption over the sourcing horizon,
    # and the calibration behind it. This is what the Monte Carlo actually samples --
    # published so it is never confused with the centrality scores above, which is the
    # confusion that produced "p_fail = betweenness".
    p_disruption: Dict[str, float]
    p_disruption_calibration: Dict[str, Any]
    k_core_summary: Dict[int, int]  # {core_number: node_count}
    hhi_by_category: Dict[str, float]


# ── Endpoints ─────────────────────────────────────────────────────────────────

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


@router.get("/metrics", response_model=GraphMetricsResponse)
def get_graph_metrics():
    """
    Return supply graph topology metrics.

    All metrics are computed from real offer data in the live database.
    Response contains only aggregate analytics — no prices, no user data (T-02-04).
    """
    gs = _require_graph_state()

    # k_core_summary: count of nodes at each core level
    k_core_summary = dict(Counter(gs.k_core.values()))

    return GraphMetricsResponse(
        n_distributors=gs.n_distributors,
        n_components=gs.n_components,
        n_edges=gs.n_edges,
        n_offer_rows=gs.n_offer_rows,
        n_duplicate_offer_rows=gs.n_duplicate_offer_rows,
        fiedler_whole_graph=gs.fiedler,
        fiedler_giant_component=gs.fiedler_giant_component,
        n_connected_components=gs.n_connected_components,
        giant_component_size=gs.giant_component_size,
        giant_component_fraction=round(gs.giant_component_fraction, 4),
        single_source_count=len(gs.single_source_component_ids),
        betweenness={str(k): round(v, 6) for k, v in gs.betweenness.items()},
        pagerank={str(k): round(v, 6) for k, v in gs.pagerank.items()},
        centrality_notes={
            "betweenness": (
                "networkx bipartite betweenness centrality on the UNDIRECTED "
                "projection, raw (the algorithm's own normalization only). It is a "
                "structural centrality score, NOT a failure probability — see "
                "p_disruption for that."
            ),
            "pagerank": (
                "PageRank on the UNDIRECTED projection, raw. Scores sum to 1.0 across "
                "ALL nodes (distributors AND components), so the distributor sub-total "
                "is well under 1.0. Edge weight is 1/max(stock,1), so a heavier edge is "
                "a SCARCER supply link: read this as concentration of thin supply, not "
                "as market share."
            ),
            "why_raw": (
                "Both were previously min-max rescaled to [0,1]. That forces the "
                "maximum to exactly 1.0 whatever the real spread — which is how "
                "betweenness became a p_fail of 1.0 for the top distributor — and maps "
                "an all-identical vector to all-zeros, which is how PageRank published "
                "sum=0.0 for 92 distributors."
            ),
        },
        p_disruption={str(k): round(v, 6) for k, v in gs.p_disruption.items()},
        p_disruption_calibration=dict(gs.p_disruption_calibration),
        k_core_summary=k_core_summary,
        hhi_by_category={k: round(v, 2) for k, v in gs.hhi_by_category.items()},
    )


@router.post("/simulate", response_model=SimulateResponse)
def post_graph_simulate(body: SimulateRequest):
    """
    Run Monte Carlo cascade failure simulation.

    N=1,000 scenarios with fixed seed=42 — reproducible output.
    N is not user-configurable (T-02-03 mitigation).

    Input handling (2026-08 audit fixes):
      - an EMPTY BOM is a 422, not a vacuous 1.0/1.0/1.0/1.0 answer;
      - component ids that do not exist in the graph are a 404 naming them, rather
        than a 200 in which every missing line silently counts as unfulfillable and
        drives `cvar_95` above what the fulfillment percentiles imply.
    """
    gs = _require_graph_state()

    # Unknown ids used to be accepted and treated as "has no suppliers", i.e. always
    # unfulfillable — a 200 describing a BOM the caller never asked about.
    unknown = sorted(
        {cid for cid in body.bom_component_ids if not gs.graph.has_node(f"c_{cid}")}
    )
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown component_id(s): {unknown[:20]}"
                f"{' …' if len(unknown) > 20 else ''}. "
                f"{len(unknown)} of {len(set(body.bom_component_ids))} distinct ids in "
                "this BOM are not present in the supply graph."
            ),
        )

    from app.graph.simulation import run_monte_carlo, N_SCENARIOS
    result = run_monte_carlo(
        gs,
        bom_component_ids=body.bom_component_ids,
        n_scenarios=N_SCENARIOS,  # always constant, never from request
        seed=42,
    )

    if result.n_unsupplied_lines:
        interpretation = (
            f"{result.n_unsupplied_lines} of {result.n_bom_lines} lines have no "
            "supplier at all in the graph — those lines are unfulfillable in every "
            "scenario, so the percentiles below are a floor, not a risk estimate."
        )
    elif result.n_scenarios_with_shortfall == 0:
        interpretation = (
            f"Fully hedged: across {result.n_scenarios:,} scenarios not one left any "
            f"of the {result.n_bom_lines} lines unfulfillable. Every line is carried by "
            f"at least 2 of the {result.n_suppliers_in_scope} distributors in scope "
            f"(single-source lines: {result.n_single_source_lines}), and the largest "
            f"per-distributor disruption probability in play is "
            f"{result.p_fail_max:.1%}. p10 = p50 = p90 = 1.0 is the correct answer "
            "here, not a stalled simulation."
        )
    else:
        interpretation = (
            f"{result.n_scenarios_with_shortfall:,} of {result.n_scenarios:,} scenarios "
            f"({result.n_scenarios_with_shortfall / result.n_scenarios:.1%}) left at "
            f"least one of the {result.n_bom_lines} lines unfulfillable; the worst "
            f"scenario delivered {result.worst_fulfillment:.1%} of the BOM. "
            f"{result.n_single_source_lines} line(s) are single-sourced."
        )

    return SimulateResponse(
        p10=result.p10,
        p50=result.p50,
        p90=result.p90,
        cvar_95=result.cvar_95,
        n_scenarios=result.n_scenarios,
        seed=result.seed,
        scope=SimulateScope(
            n_bom_lines=result.n_bom_lines,
            n_suppliers_in_scope=result.n_suppliers_in_scope,
            n_single_source_lines=result.n_single_source_lines,
            n_unsupplied_lines=result.n_unsupplied_lines,
            n_scenarios_with_shortfall=result.n_scenarios_with_shortfall,
            worst_fulfillment=round(result.worst_fulfillment, 4),
            mean_fulfillment=round(result.mean_fulfillment, 4),
            mean_cost_inflation=round(result.mean_cost_inflation, 4),
            p_fail_min=round(result.p_fail_min, 6),
            p_fail_median=round(result.p_fail_median, 6),
            p_fail_max=round(result.p_fail_max, 6),
        ),
        interpretation=interpretation,
        caveats=[
            "Distributor failures are drawn INDEPENDENTLY. Real disruptions are "
            "correlated (one typhoon takes out several suppliers at once), so this "
            "tail is if anything optimistic.",
            "cvar_95 is a COST MULTIPLIER (>= 1.0), not a rate — it is the mean "
            "emergency-procurement inflation over the worst 5% of scenarios. It is on "
            "a different scale from p10/p50/p90, which are fulfillment fractions in "
            "[0, 1]; a cvar_95 of 1.15 is not '115% fulfillment'.",
            "Disruption probabilities are an assumption from a cited FIRM-level base "
            "rate applied per supplier; see p_disruption_calibration on /graph/metrics.",
            "Single-round percolation: no time dimension, no propagation between "
            "nodes, no recovery.",
        ],
    )
