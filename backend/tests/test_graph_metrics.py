"""Unit tests for app/graph/ metrics — Phase 02."""
import time


def test_graph_state_singleton():
    from app.graph import get_graph_state, set_graph_state, GraphState
    import networkx as nx
    # Reset state
    set_graph_state(None)
    assert get_graph_state() is None
    # Create minimal GraphState to verify set/get round-trip
    g = nx.DiGraph()
    state = GraphState(
        graph=g,
        dist_nodes=frozenset(),
        betweenness={},
        pagerank={},
        k_core={},
        single_source_component_ids=frozenset(),
        hhi_by_category={},
        fiedler=0.0,
        n_distributors=0,
        n_components=0,
        n_edges=0,
    )
    set_graph_state(state)
    assert get_graph_state() is state
    set_graph_state(None)


def test_graph_builds_from_db(graph_db_session):
    from app.graph.builder import build_graph_state
    gs = build_graph_state(graph_db_session)
    assert gs.n_distributors == 3
    assert gs.n_components == 10
    # n_edges is the TRUE edge count of the graph these metrics describe. It used to
    # hold len(offer_rows) instead, which is why /graph/metrics published 8,176 edges
    # for a graph holding fewer. The graph is built from EVERY offer row (the dead 20%
    # holdout carve was removed 2026-09-03), so the ONLY difference between the offer
    # row count and the edge count is duplicate (component, distributor) rows.
    assert gs.n_edges == gs.graph.number_of_edges()
    assert gs.n_offer_rows == 15
    assert gs.n_edges == gs.n_offer_rows - gs.n_duplicate_offer_rows
    assert gs.n_edges == 15, gs.n_edges


def test_graph_builds_under_2s(graph_db_session):
    from app.graph.builder import build_graph_state
    start = time.time()
    build_graph_state(graph_db_session)
    elapsed = time.time() - start
    assert elapsed < 2.0, f"Graph build took {elapsed:.2f}s, expected < 2s"


def test_betweenness_centrality(graph_db_session):
    from app.graph.builder import build_graph_state
    gs = build_graph_state(graph_db_session)
    btwn = gs.betweenness
    # All 3 distributor IDs present
    assert 1 in btwn and 2 in btwn and 3 in btwn, f"Missing dist IDs in betweenness: {list(btwn.keys())}"
    # All values in [0, 1]
    for did, val in btwn.items():
        assert 0.0 <= val <= 1.0, f"betweenness[{did}]={val} out of [0,1]"
    # dist 1 carries all 10 components (most edges) -> normalized to 1.0 (max)
    assert btwn[1] >= btwn[2], f"dist1 betweenness {btwn[1]} < dist2 {btwn[2]}"
    assert btwn[1] >= btwn[3], f"dist1 betweenness {btwn[1]} < dist3 {btwn[3]}"


def test_pagerank_centrality(graph_db_session):
    from app.graph.builder import build_graph_state
    gs = build_graph_state(graph_db_session)
    pr = gs.pagerank
    # All 3 distributor IDs present
    assert 1 in pr and 2 in pr and 3 in pr, f"Missing dist IDs in pagerank: {list(pr.keys())}"
    # All values in [0, 1]
    for did, val in pr.items():
        assert 0.0 <= val <= 1.0, f"pagerank[{did}]={val} out of [0,1]"
    # dist 1 carries all 10 components -> highest PageRank
    assert pr[1] >= pr[2], f"dist1 pagerank {pr[1]} < dist2 {pr[2]}"
    assert pr[1] >= pr[3], f"dist1 pagerank {pr[1]} < dist3 {pr[3]}"


def test_fiedler_value(graph_db_session):
    from app.graph.builder import build_graph_state
    gs = build_graph_state(graph_db_session)
    # Whole-graph fiedler must be a float >= 0.0 -- never raises, never negative
    assert isinstance(gs.fiedler, float), f"fiedler is {type(gs.fiedler)}, expected float"
    assert gs.fiedler >= 0.0, f"fiedler {gs.fiedler} is negative"
    # Log it so CI output shows the value (no specific value assertion -- depends on connectivity)
    print(f"\nFiedler value (test fixture): {gs.fiedler:.6f}")


def test_fiedler_giant_component_reported_honestly(graph_db_session):
    """Gap-audit fix: when the graph is disconnected, whole-graph λ₂ is exactly
    0.0 (mathematically correct) but the giant-component λ₂ + component metadata
    must ALSO be reported so the number carries information."""
    from app.graph.builder import build_graph_state
    gs = build_graph_state(graph_db_session)

    assert isinstance(gs.fiedler_giant_component, float)
    assert gs.fiedler_giant_component >= 0.0
    assert isinstance(gs.n_connected_components, int) and gs.n_connected_components >= 1
    assert isinstance(gs.giant_component_size, int) and gs.giant_component_size >= 0
    assert 0.0 <= gs.giant_component_fraction <= 1.0

    total_nodes = gs.n_distributors + gs.n_components
    if total_nodes > 0:
        assert gs.giant_component_size <= total_nodes

    # Whole-graph λ₂ is exactly 0.0 whenever there's more than one component --
    # this must never be silently swapped for the giant-component value.
    if gs.n_connected_components > 1:
        assert gs.fiedler == 0.0, (
            f"whole-graph fiedler should be exactly 0.0 when disconnected "
            f"({gs.n_connected_components} components), got {gs.fiedler}"
        )

    print(
        f"\nGiant component: {gs.giant_component_size} nodes "
        f"({gs.giant_component_fraction:.1%}), lambda2={gs.fiedler_giant_component:.4f}, "
        f"n_connected_components={gs.n_connected_components}"
    )


def test_kcore_decomposition(graph_db_session):
    from app.graph.builder import build_graph_state
    gs = build_graph_state(graph_db_session)
    k_core = gs.k_core
    # k_core should have entries for node names used in the graph
    assert len(k_core) > 0, "k_core dict is empty"
    # All values are non-negative integers
    for node, val in k_core.items():
        assert isinstance(val, int) and val >= 0, f"k_core[{node}]={val} invalid"
    # Dist 1 node should be present
    assert "d_1" in k_core, f"'d_1' not in k_core; keys: {list(k_core.keys())[:10]}"
    # Component nodes should be present
    assert "c_1" in k_core, "'c_1' not in k_core"


def test_hhi_per_category(graph_db_session):
    from app.graph.builder import build_graph_state
    gs = build_graph_state(graph_db_session)
    hhi = gs.hhi_by_category
    # Both categories present
    assert "Microcontrollers" in hhi, f"Microcontrollers not in HHI: {list(hhi.keys())}"
    assert "Op-Amps" in hhi, f"Op-Amps not in HHI: {list(hhi.keys())}"
    # All values in valid range
    for cat, val in hhi.items():
        assert 0.0 <= val <= 10000.0, f"hhi[{cat}]={val} out of [0, 10000]"
    # Op-Amps: only dist 1 has stock -> near-monopoly -> HHI close to 10000
    assert hhi["Op-Amps"] > 9000, (
        f"Op-Amps HHI={hhi['Op-Amps']:.1f}, expected > 9000 (monopoly)"
    )
    # Microcontrollers: dist 1 and dist 2 each carry all 5 comps with equal stock
    # -> each has 50% share -> HHI = 50^2 + 50^2 = 5000
    assert 4000 <= hhi["Microcontrollers"] <= 6000, (
        f"Microcontrollers HHI={hhi['Microcontrollers']:.1f}, expected ~5000 (duopoly)"
    )
    print(f"\nHHI by category: { {k: round(v, 1) for k, v in hhi.items()} }")


def test_single_source_flags(graph_db_session):
    from app.graph.builder import build_graph_state
    gs = build_graph_state(graph_db_session)
    ss = gs.single_source_component_ids
    # Components 6-10 have only dist 1 as a stocked distributor -> single source
    for cid in [6, 7, 8, 9, 10]:
        assert cid in ss, f"component {cid} should be single-source but is not in {ss}"
    # Components 1-5 have dist 1 AND dist 2 -> not single source
    for cid in [1, 2, 3, 4, 5]:
        assert cid not in ss, f"component {cid} should NOT be single-source but is in {ss}"
    print(f"\nSingle-source component count: {len(ss)}")


def test_monte_carlo_returns_percentiles(graph_db_session):
    from app.graph.builder import build_graph_state
    from app.graph.simulation import run_monte_carlo
    gs = build_graph_state(graph_db_session)
    bom_ids = list(range(1, 11))  # all 10 test components
    result = run_monte_carlo(gs, bom_ids)
    # Percentiles must be ordered
    assert result.p10 <= result.p50 <= result.p90, (
        f"Percentile ordering violated: p10={result.p10} p50={result.p50} p90={result.p90}"
    )
    # All rates in valid range
    for pct_name, val in [("p10", result.p10), ("p50", result.p50), ("p90", result.p90)]:
        assert 0.0 <= val <= 1.0, f"{pct_name}={val} out of [0, 1]"
    # CVaR must be >= 1.0 (no negative cost inflation)
    assert result.cvar_95 >= 1.0, f"cvar_95={result.cvar_95} < 1.0"
    # N scenarios preserved
    assert result.n_scenarios == 1000
    print(f"\nMonte Carlo: p10={result.p10:.3f} p50={result.p50:.3f} p90={result.p90:.3f} cvar={result.cvar_95:.4f}")


def test_monte_carlo_reproducible(graph_db_session):
    from app.graph.builder import build_graph_state
    from app.graph.simulation import run_monte_carlo
    gs = build_graph_state(graph_db_session)
    bom_ids = [1, 3, 6, 8, 10]
    result_a = run_monte_carlo(gs, bom_ids, seed=42)
    result_b = run_monte_carlo(gs, bom_ids, seed=42)
    assert result_a.p10 == result_b.p10, f"p10 not reproducible: {result_a.p10} != {result_b.p10}"
    assert result_a.p50 == result_b.p50, f"p50 not reproducible: {result_a.p50} != {result_b.p50}"
    assert result_a.p90 == result_b.p90, f"p90 not reproducible: {result_a.p90} != {result_b.p90}"
    assert result_a.cvar_95 == result_b.cvar_95, f"cvar_95 not reproducible: {result_a.cvar_95} != {result_b.cvar_95}"


def test_cvar_at_95th_percentile(graph_db_session):
    from app.graph.builder import build_graph_state
    from app.graph.simulation import run_monte_carlo, EMERGENCY_COST_PREMIUM
    gs = build_graph_state(graph_db_session)
    bom_ids = list(range(1, 11))
    result = run_monte_carlo(gs, bom_ids)
    # CVaR is bounded: minimum is 1.0 (no failures), maximum is 1 + EMERGENCY_COST_PREMIUM (all fail)
    assert 1.0 <= result.cvar_95 <= 1.0 + EMERGENCY_COST_PREMIUM + 0.001, (
        f"cvar_95={result.cvar_95} out of expected range [1.0, {1.0 + EMERGENCY_COST_PREMIUM:.3f}]"
    )


