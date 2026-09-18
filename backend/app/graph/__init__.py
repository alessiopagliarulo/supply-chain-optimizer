"""
Graph ML Network Risk Engine.

Builds a bipartite NetworkX DiGraph from the live SQLite database (Distributor <-> Component
nodes, offer edges weighted by inverse stock). Computes centrality metrics, k-core
decomposition, HHI per category, Fiedler algebraic connectivity, and Monte Carlo
cascade simulation.

Call get_graph_state() to get the loaded GraphState, or None if the graph has not been
built yet. The build is kicked off by the lifespan but runs on a background thread
(app/startup.py), so `None` is now reachable for the first few seconds of a process.
Anything on a request path must therefore either wait for that one build
(`app.startup.wait_for_graph`) or go through `ensure_graph_state`, which is
single-flight and caches — never call `build_graph_state` directly per request.
"""
from __future__ import annotations
import threading
from typing import TYPE_CHECKING, Optional, Dict, FrozenSet, List
from dataclasses import dataclass, field

import networkx as nx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass
class GraphState:
    graph: nx.DiGraph                          # bipartite DiGraph: dist->comp, weight=inv_stock
    dist_nodes: FrozenSet[str]                 # frozenset of 'd_{did}' node names
    betweenness: Dict[int, float]              # distributor_id -> normalized [0,1] betweenness
    # RAW PageRank score on the UNDIRECTED bipartite projection -- NOT min-max
    # normalized. Scores sum to 1.0 across ALL nodes (distributors + components),
    # so the distributor sub-total is < 1.0. Two things were wrong before
    # (2026-08 functionality audit):
    #   1. it was computed on the DiGraph, whose edges all run distributor->component.
    #      Every distributor therefore had in-degree 0 and received only teleport mass,
    #      making all 92 scores identical (0.00105149 each) -- a constant, not a metric.
    #   2. the identical scores were then min-max normalized, and a min-max over a
    #      zero-range vector maps EVERY entry to exactly 0.0. That is how the endpoint
    #      came to publish sum = 0.0 and max = 0.0 for 92 distributors.
    # Min-max normalization is not applied to ANY centrality any more: it is what
    # manufactured the p_fail = 1.0 pathology in the simulator as well.
    pagerank: Dict[int, float]                 # distributor_id -> raw PageRank score
    k_core: Dict[str, int]                     # node_name -> core number
    single_source_component_ids: FrozenSet[int]  # component_ids with only 1 stocked distributor
    hhi_by_category: Dict[str, float]          # category -> HHI (0-10000 scale)
    fiedler: float                             # WHOLE-GRAPH algebraic connectivity (λ₂).
                                                # Mathematically exact 0.0 whenever the graph
                                                # has >1 connected component -- this is NOT a
                                                # computation failure, it's the correct answer
                                                # to "is the whole graph connected?" (no).
    # Phase 4 (BENCH-05): sequential-removal λ₂ curve for top-k distributors.
    # Entries: [{"step": int, "removed": int|None, "removed_name": str|None,
    #            "lambda2": float, "delta_pct": float}, ...]
    fiedler_curve: List[dict] = field(default_factory=list)
    # Per-distributor probability of a material disruption over the sourcing horizon,
    # from app.graph.disruption.build_failure_probabilities (cited McKinsey
    # base rate -> exposure window -> bounded centrality RANK transform). This is the
    # ONE probability model the whole app uses. It replaces the previous practice of
    # feeding min-max normalized betweenness straight into a Bernoulli draw, which had
    # no base rate, no exposure window and no unit, and which by construction failed
    # the single most central distributor in 100% of scenarios.
    p_disruption: Dict[int, float] = field(default_factory=dict)
    # Provenance for p_disruption, published by /graph/metrics so the assumption is
    # inspectable rather than asserted. Keys: base_annual_prob, horizon_days,
    # centrality_spread, base_horizon_prob, max_failure_prob, source.
    p_disruption_calibration: Dict[str, object] = field(default_factory=dict)
    n_distributors: int = 0
    n_components: int = 0
    # TRUE number of edges in `graph`: deduplicated distributor->component pairs built
    # from EVERY offer row. Previously this held len(offer_rows) -- the raw offer-table
    # row count, 8,176 -- and was published as the edge count of a smaller graph.
    # The offer-row count is still available as n_offer_rows, and the only difference
    # between the two is n_duplicate_offer_rows (price-break tiers on the same pair).
    # A 20% "holdout" carve used to shrink this further; nothing ever read it, so it
    # was removed 2026-09-03 along with the n_holdout_offer_rows field.
    n_edges: int = 0
    n_offer_rows: int = 0                      # raw DistributorOffer rows read from the DB
    n_duplicate_offer_rows: int = 0            # offer rows collapsed onto an existing edge
    # Gap-audit fix (2026-07-01, "43 components / λ₂=0.0 is analytically useless"):
    # the whole-graph λ₂ above is always 0.0 for this supplier graph because it is
    # genuinely disconnected (many components carried by exactly one distributor,
    # isolated dist/comp nodes with no offers). These fields report the SAME metric
    # computed on the giant (largest) connected component instead, which is what
    # actually says something about how tightly the *main* supplier network is knit.
    # NOTE (2026-09-03): "43 components" above is correct AS HISTORY for the graph as
    # it existed on 2026-07-01. It is superseded -- 34 components as of 2026-09-03,
    # after the dead 20% holdout carve was removed from `graph/builder.py` (see
    # n_edges comment above). Do not read "43" as the current figure.
    n_connected_components: int = 1            # count of connected components in the full graph
    giant_component_size: int = 0              # node count (dist + comp) in the largest component
    giant_component_fraction: float = 0.0      # giant_component_size / total graph nodes
    fiedler_giant_component: float = 0.0       # λ₂ of the largest connected component only


_graph_state: Optional[GraphState] = None

# Bumped by EVERY set_graph_state. `ensure_graph_state` records it before a build and
# refuses to publish if it moved, so a slow background build can never stomp a state
# that someone installed deliberately while it was running (which is exactly what a
# test fixture does when it clears the global to force a build from ITS session).
_state_epoch: int = 0
_state_lock = threading.Lock()

# Single-flight guard. Held for the whole duration of one build so that concurrent
# callers WAIT on that build rather than each starting their own. See the docstring
# of `ensure_graph_state` for why this is not optional.
_build_lock = threading.Lock()


def set_graph_state(state: Optional[GraphState]) -> None:
    global _graph_state, _state_epoch
    with _state_lock:
        _graph_state = state
        _state_epoch += 1


def get_graph_state() -> Optional[GraphState]:
    return _graph_state


def graph_state_epoch() -> int:
    """How many times the process GraphState has been assigned. Diagnostics/tests."""
    return _state_epoch


def ensure_graph_state(db: "Session", only_if_epoch: Optional[int] = None) -> GraphState:
    """
    Return the process GraphState, building it EXACTLY ONCE if it is not loaded.

    WHY THIS EXISTS
    ---------------
    `app/api/resilience.py` and `app/api/stochastic.py` both used to do:

        gs = get_graph_state()
        if gs is None:
            gs = build_graph_state(db)      # <- and never stored it

    which is harmless only while the lifespan guarantees the global is populated
    before the first request. The moment the graph build is deferred off the lifespan
    that becomes a loaded gun: every request arriving during warm-up would rebuild the
    whole graph from scratch — Brandes betweenness over 883 nodes, ~1 s on a laptop and
    ~9 s on the deployed 0.5-CPU worker — concurrently, forever, because none of them
    ever wrote the result back. A handful of parallel requests is then a self-inflicted
    denial of service.

    So: build once, store it, and make concurrent callers block on that single build
    instead of starting their own.

    The returned state is always the one this caller's `db` describes: if another
    thread installed a different state mid-build, that one wins the global and this
    caller still gets the graph it asked for, rather than a silent substitution.

    ``only_if_epoch`` lets the background warm-up hand in the epoch it saw when it
    STARTED, rather than the one visible when the build finally acquires the lock.
    The difference matters: a test installs its own GraphState milliseconds after
    entering the lifespan, long before the warm-up thread gets that far, and without
    the earlier epoch the warm-up would see an unchanged counter and publish the real
    production graph on top of the test's fixture.
    """
    global _graph_state, _state_epoch

    state = _graph_state
    if state is not None:
        return state

    with _build_lock:
        # Double-checked: another thread may have finished a build while we queued.
        state = _graph_state
        if state is not None:
            return state

        from app.graph.builder import build_graph_state

        epoch_before = _state_epoch if only_if_epoch is None else only_if_epoch
        built = build_graph_state(db)
        with _state_lock:
            if _state_epoch == epoch_before:
                _graph_state = built
                _state_epoch += 1
        return built
