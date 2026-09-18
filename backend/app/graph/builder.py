"""
Build GraphState from SQLite at startup.

Reads all Distributor rows and DistributorOffer rows (joined with Component for category).
Constructs a bipartite nx.DiGraph: distributor->component edges weighted by 1/max(stock,1).
Computes all metrics and returns a fully-populated GraphState.

Called once from main.py lifespan — never per-request.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Dict, FrozenSet, Set, Tuple

import networkx as nx
from networkx.algorithms import bipartite
from sqlalchemy.orm import Session

from app.graph import GraphState

logger = logging.getLogger(__name__)


def build_graph_state(db: Session) -> GraphState:
    """
    Build the full GraphState from the live SQLite database.

    Steps:
      1. Load distributors and offers (with component category)
      2. Build nx.DiGraph with dist->comp edges weighted by inv_stock, from EVERY
         offer row (see the note below about the 20% holdout that used to be carved
         out here and silently shrank every published topology figure)
      3. Compute betweenness centrality (via bipartite projection, undirected), RAW
      4. Compute PageRank on the UNDIRECTED projection, RAW
         (both were previously min-max rescaled; see the notes at each step for why
         that had to go — it is what published PageRank 0.0 for all 92 distributors
         and what handed the top distributor a failure probability of exactly 1.0)
      5. Compute k-core decomposition
      6. Identify single-source components
      7. Compute HHI per component category
      8. Compute Fiedler value: whole-graph λ₂ (0.0 if disconnected, which it is) AND
         λ₂ of the largest connected component (the informative number)
      9. Calibrate per-distributor disruption probabilities (the ONE probability model
         the app uses — see _build_disruption_probabilities)
     10. Log timing and counts

    NOTE (2026-09-03): steps 1-2 used to be separated by a "holdout partition" that
    randomly removed 20% of the (component, distributor) offer pairs — 1,574 of 8,176
    rows — before the graph was constructed. Nothing in the repository ever read the
    resulting `holdout_offer_pairs` set: there was no link-prediction model, no
    evaluation, no consumer of any kind. Its only effect was that every published
    network and resilience figure described a graph missing a fifth of the real supply
    links, which OVERSTATED fragility. Measured against the served DB, removing it
    moves the graph from 5,789 to 7,363 edges, 43 to 34 connected components, and
    giant-component λ₂ from 0.2377 to 0.2788. The carve and the `n_holdout_offer_rows`
    field it fed are gone; the graph is built from all offer rows, so
    n_edges + n_duplicate_offer_rows == n_offer_rows exactly.
    """
    t0 = time.time()

    from app.models.distributor import Distributor
    from app.models.component import Component, DistributorOffer

    # -- 1. Load data ----------------------------------------------------------
    distributors = db.query(Distributor).all()
    # Join offers with component category in Python to avoid SQLAlchemy complexity
    offers_raw = (
        db.query(
            DistributorOffer.component_id,
            DistributorOffer.distributor_id,
            DistributorOffer.stock,
            Component.category,
        )
        .join(Component, Component.id == DistributorOffer.component_id)
        .all()
    )
    components = db.query(Component.id).all()

    dist_ids: Set[int] = {d.id for d in distributors}
    comp_ids: Set[int] = {c.id for c in components}

    # -- 2. Build DiGraph (from ALL offer rows) --------------------------------
    G = nx.DiGraph()

    for did in dist_ids:
        G.add_node(f"d_{did}", bipartite=0)
    for cid in comp_ids:
        G.add_node(f"c_{cid}", bipartite=1)

    # Category lookup: component_id -> category
    cat_by_comp: Dict[int, str] = {}
    for row in offers_raw:
        cat_by_comp[row.component_id] = row.category or "Unknown"

    n_duplicate_offer_rows = 0
    for row in offers_raw:
        inv_stock = 1.0 / max(row.stock, 1)
        u, v = f"d_{row.distributor_id}", f"c_{row.component_id}"
        # If edge already exists (duplicate offer rows), take minimum inv_stock (highest stock)
        if G.has_edge(u, v):
            n_duplicate_offer_rows += 1
            if inv_stock < G[u][v]["weight"]:
                G[u][v]["weight"] = inv_stock
        else:
            G.add_edge(u, v, weight=inv_stock)

    dist_nodes: FrozenSet[str] = frozenset(f"d_{did}" for did in dist_ids if f"d_{did}" in G)
    n_dist = len(dist_ids)
    n_comp = len(comp_ids)
    # n_edges is the EDGE COUNT OF `G`. It used to be len(offers_raw) -- the raw
    # offer-table row count -- which meant /graph/metrics published 8,176 edges for a
    # graph holding fewer. Now that the holdout carve is gone the two differ for
    # exactly ONE reason, reported separately rather than conflated: duplicate
    # (component, distributor) offer rows (price-break tiers) collapse to one edge.
    # So n_edges + n_duplicate_offer_rows == n_offer_rows, exactly.
    n_edges = G.number_of_edges()
    n_offer_rows = len(offers_raw)

    # -- 3. Betweenness centrality (bipartite) ---------------------------------
    # bipartite.betweenness_centrality requires an undirected graph and the distributor
    # node set. NOTE: it does NOT read edge weights -- networkx's bipartite betweenness
    # is unweighted and takes no `weight` argument. The previous comment here claimed it
    # was "stock-weighted"; it never was. It IS normalized by the algorithm itself so
    # values already lie in [0, 1] with 1.0 the theoretical maximum for the partition.
    #
    # The extra MIN-MAX rescale that used to be applied on top has been REMOVED. It
    # forced max -> exactly 1.0 and min -> exactly 0.0 regardless of the underlying
    # spread, which is precisely how the most central distributor acquired a literal
    # failure probability of 1.0 downstream (see p_disruption below), and how a
    # zero-range vector (PageRank, step 5) collapsed to all-zeros.
    G_undirected = G.to_undirected()
    try:
        btwn_raw = bipartite.betweenness_centrality(G_undirected, dist_nodes)
        betweenness: Dict[int, float] = {
            did: float(btwn_raw.get(f"d_{did}", 0.0)) for did in dist_ids
        }
    except Exception as exc:
        logger.warning("Betweenness centrality failed: %s — using zeros", exc)
        betweenness = {did: 0.0 for did in dist_ids}

    # -- 4. PageRank ------------------------------------------------------------
    # Computed on the UNDIRECTED projection, and published RAW.
    #
    # On the DiGraph every edge runs distributor -> component, so no distributor has a
    # single in-edge: each of the 92 received only the uniform teleport share and all
    # 92 scores were bit-identical (0.00105149). Min-max normalizing an all-identical
    # vector then mapped every entry to exactly 0.0, which is what /graph/metrics has
    # been publishing (sum 0.0, max 0.0). Both the graph choice and the normalization
    # are fixed here.
    #
    # Edge weight is the graph's inverse-stock weight, kept for consistency with the
    # rest of the graph. Read it accordingly: a heavier edge is a SCARCER supply link,
    # so this PageRank ranks distributors by how much of the catalogue's *thin* supply
    # concentrates on them, not by raw volume. That is the fragility reading, and it is
    # the one the resilience surfaces want. `centrality_notes.pagerank` in the
    # /graph/metrics response states this so the number is not read as market share.
    try:
        pr_raw = nx.pagerank(G_undirected, weight="weight", max_iter=200)
        pagerank: Dict[int, float] = {
            did: float(pr_raw.get(f"d_{did}", 0.0)) for did in dist_ids
        }
    except Exception as exc:
        logger.warning("PageRank failed: %s — using zeros", exc)
        pagerank = {did: 0.0 for did in dist_ids}

    # Degeneracy guard: a centrality that resolves to ONE distinct value across every
    # distributor carries no information, and silently publishing it is the failure this
    # module just got audited for. Log loudly instead of shipping a constant as a metric.
    for _name, _vals in (("betweenness", betweenness), ("pagerank", pagerank)):
        if len(_vals) > 1 and len({round(v, 12) for v in _vals.values()}) == 1:
            logger.error(
                "DEGENERATE METRIC: %s resolved to the single value %.12g for all %d "
                "distributors — it carries no information and must not be interpreted.",
                _name, next(iter(_vals.values())), len(_vals),
            )

    # -- 5. k-core decomposition -----------------------------------------------
    try:
        k_core: Dict[str, int] = dict(nx.core_number(G_undirected))
    except Exception as exc:
        logger.warning("k-core failed: %s — using zeros", exc)
        k_core = {}

    # -- 6. Single-source components --------------------------------------------
    # A component is single-source if only 1 distributor carries it with stock > 0
    stocked_dists_by_comp: Dict[int, Set[int]] = defaultdict(set)
    for row in offers_raw:
        if row.stock > 0:
            stocked_dists_by_comp[row.component_id].add(row.distributor_id)
    single_source_ids: FrozenSet[int] = frozenset(
        cid for cid, dists in stocked_dists_by_comp.items() if len(dists) == 1
    )

    # -- 7. HHI per component category -----------------------------------------
    # HHI = sum of squared market shares per category
    # Market share = distributor's share of total stock in that category
    stock_by_cat_dist: Dict[str, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for row in offers_raw:
        cat = cat_by_comp.get(row.component_id, "Unknown")
        stock_by_cat_dist[cat][row.distributor_id] += max(row.stock, 0)

    hhi_by_category: Dict[str, float] = {}
    for cat, dist_stocks in stock_by_cat_dist.items():
        total = sum(dist_stocks.values())
        if total == 0:
            hhi_by_category[cat] = 0.0
        else:
            hhi_by_category[cat] = sum(
                (s / total * 100) ** 2 for s in dist_stocks.values()
            )

    # -- 8. Fiedler value (algebraic connectivity) -----------------------------
    # This supplier graph is genuinely disconnected (34 components: many parts
    # carried by exactly one distributor, isolated dist/comp nodes with no
    # offers) so the WHOLE-GRAPH λ₂ is mathematically 0.0 -- correct, but tells
    # a viewer nothing about how tightly the *main* network is knit. We report
    # both, clearly separated:
    #   fiedler                 -- whole-graph λ₂ (0.0 whenever n_cc > 1; exact, not a fallback)
    #   fiedler_giant_component -- λ₂ of the largest connected component only
    # The giant-component Laplacian is built UNWEIGHTED. The stock-weighted
    # (inv_stock) edges create an ill-conditioned weighted Laplacian that ARPACK
    # fails to converge on for this graph (confirmed empirically: "ARPACK error
    # -1: No convergence" on the giant component, 847 nodes as of 2026-09-03 after
    # the dead 20% holdout carve was removed from graph construction; was 839) --
    # the unweighted
    # Laplacian converges in <0.1s and is the same approach already used by the
    # sequential-removal curve in main.py:compute_fiedler_curve, so the two
    # numbers stay comparable.
    # tracemin_pcg also hangs on large stock-weighted bipartite graphs (Pitfall #1).
    # Run in a thread with a hard 8s timeout; fall back to 0.0 on timeout/error.
    import concurrent.futures as _cf

    def _compute_fiedler():
        _n_cc = nx.number_connected_components(G_undirected)
        _n_nodes = G_undirected.number_of_nodes()
        ccs = list(nx.connected_components(G_undirected))
        largest_cc = max(ccs, key=len) if ccs else set()
        _giant_size = len(largest_cc)

        # Whole-graph λ₂: exact 0.0 whenever disconnected -- no computation needed.
        if _n_cc <= 1 and _n_nodes > 1:
            try:
                _whole = nx.algebraic_connectivity(G_undirected, method="lanczos")
            except Exception as _exc:
                logger.warning("Whole-graph Fiedler failed: %s — using 0.0", _exc)
                _whole = 0.0
        else:
            _whole = 0.0

        # Giant-component λ₂ on the UNWEIGHTED subgraph (see comment above).
        _giant = 0.0
        if _giant_size > 2:
            G_lcc = G_undirected.subgraph(largest_cc).copy()
            for u, v in G_lcc.edges():
                G_lcc[u][v]["weight"] = 1.0
            try:
                _giant = nx.algebraic_connectivity(G_lcc, method="lanczos", normalized=False)
            except Exception as _exc:
                logger.warning("Giant-component Fiedler failed: %s — using 0.0", _exc)
                _giant = 0.0

        return _whole, _giant, _n_cc, _giant_size, _n_nodes

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _f = _pool.submit(_compute_fiedler)
            try:
                fiedler, fiedler_giant, n_cc, giant_size, n_nodes = _f.result(timeout=8)
            except _cf.TimeoutError:
                logger.warning("Fiedler computation timed out (>8s) — using 0.0")
                fiedler, fiedler_giant, n_cc, giant_size, n_nodes = 0.0, 0.0, 0, 0, 0
    except Exception as exc:
        logger.warning("Fiedler computation failed: %s — using 0.0", exc)
        fiedler, fiedler_giant, n_cc, giant_size, n_nodes = 0.0, 0.0, 0, 0, 0

    giant_fraction = (giant_size / n_nodes) if n_nodes > 0 else 0.0

    # -- 9. Disruption probabilities (ONE calibrated model for the whole app) --
    # Imported lazily: app.optimization.* pulls in the solver stack, and importing it
    # at module scope would make `app.graph` depend on it in both directions
    # (app.optimization.recommendations already imports app.graph.simulation).
    p_disruption, p_calibration = _build_disruption_probabilities(betweenness)

    elapsed = time.time() - t0
    logger.info(
        "Graph built: %d distributors, %d components, %d edges "
        "(%d offer rows, %d duplicate pairs collapsed), "
        "whole-graph lambda2=%.4f, giant-component lambda2=%.4f "
        "(%d connected components, giant=%d/%d nodes = %.1f%%, %.2fs)",
        n_dist, n_comp, n_edges, n_offer_rows, n_duplicate_offer_rows,
        fiedler, fiedler_giant,
        n_cc, giant_size, n_nodes, giant_fraction * 100, elapsed,
    )

    return GraphState(
        graph=G,
        dist_nodes=dist_nodes,
        betweenness=betweenness,
        pagerank=pagerank,
        k_core=k_core,
        single_source_component_ids=single_source_ids,
        hhi_by_category=hhi_by_category,
        fiedler=fiedler,
        p_disruption=p_disruption,
        p_disruption_calibration=p_calibration,
        n_distributors=n_dist,
        n_components=n_comp,
        n_edges=n_edges,
        n_offer_rows=n_offer_rows,
        n_duplicate_offer_rows=n_duplicate_offer_rows,
        n_connected_components=n_cc,
        giant_component_size=giant_size,
        giant_component_fraction=giant_fraction,
        fiedler_giant_component=fiedler_giant,
    )


def _build_disruption_probabilities(
    betweenness: Dict[int, float],
) -> Tuple[Dict[int, float], Dict[str, object]]:
    """
    Per-distributor probability of a material disruption over the sourcing horizon.

    This is the SINGLE probability model the application uses. It delegates to
    `app.graph.disruption.build_failure_probabilities`:

        level  <- a cited base rate, converted to an exposure window
                  (McKinsey Global Institute 2020: a disruption lasting a month or
                  longer every 3.7 years -> 1 - exp(-1/3.7) = 0.2368 per year,
                  -> 1 - (1-p)**(60/365) = 0.0436 over a 60-day PO window)
        shape  <- centrality, but only as a BOUNDED RANK transform: the most central
                  supplier gets `spread` x the base rate, the least central 1/spread x,
                  the median exactly the base rate; capped at MAX_FAILURE_PROB = 0.5.

    Before this, `graph/simulation.py` read min-max normalized betweenness straight
    into a Bernoulli draw. That expression had no base rate, no exposure window and no
    unit, and -- because a min-max normalization attains 1.0 at its maximum -- it
    failed the single most central distributor (DigiKey) in 100% of scenarios at
    BASELINE. Forcing DigiKey to fail in a what-if scenario was therefore a no-op:
    the model already had it permanently dark. That is what made 91 of 92 distributors
    produce literally zero impact on `/resilience/distributor-failure`.

    Nothing about the probabilities is duplicated here; only the call is.
    """
    from app.graph import disruption as stoch

    dist_ids = sorted(betweenness)
    probs = stoch.build_failure_probabilities(dist_ids, betweenness)
    calibration: Dict[str, object] = {
        "base_annual_prob": round(stoch.DEFAULT_BASE_ANNUAL_PROB, 6),
        "horizon_days": stoch.DEFAULT_HORIZON_DAYS,
        "centrality_spread": stoch.DEFAULT_CENTRALITY_SPREAD,
        "base_horizon_prob": round(
            stoch.annual_to_horizon_prob(
                stoch.DEFAULT_BASE_ANNUAL_PROB, stoch.DEFAULT_HORIZON_DAYS,
            ),
            6,
        ),
        "max_failure_prob": stoch.MAX_FAILURE_PROB,
        "source": (
            "McKinsey Global Institute, 'Risk, resilience, and rebalancing in global "
            "value chains', August 2020 — disruptions lasting a month or longer every "
            "3.7 years. FIRM-level frequency applied per supplier, which likely "
            "OVERSTATES individual supplier risk. Treat as an assumption; "
            "base_annual_prob, horizon_days and centrality_spread are parameters of "
            "app.graph.disruption.build_failure_probabilities."
        ),
        "method": (
            "p_d = min(base_horizon_prob * spread**(2*rank_d - 1), max_failure_prob), "
            "rank_d = percentile rank of distributor d's betweenness."
        ),
    }
    if probs:
        logger.info(
            "Disruption probabilities calibrated for %d distributors: "
            "min=%.5f median=%.5f max=%.5f (base horizon prob %.5f)",
            len(probs), min(probs.values()),
            sorted(probs.values())[len(probs) // 2], max(probs.values()),
            calibration["base_horizon_prob"],
        )
    return probs, calibration
