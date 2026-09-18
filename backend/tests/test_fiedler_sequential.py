"""
Regression tests for the Fiedler sequential-removal curve.

Guards Research Pitfall #1: `nx.algebraic_connectivity(..., method="tracemin_pcg")`
on stock-weighted bipartite graphs silently returned λ₂ = 0.0 in 146s during
Phase 2 testing on the 847-node LCC. Phase 4 uses `method="lanczos"` on an
UNWEIGHTED laplacian for the sequential-removal curve — this test suite is
the canary that catches regressions to that behavior.

See .planning/phases/04-benchmark-dashboard/04-RESEARCH.md §Pitfall 1 and
§Pattern 3.
"""
from __future__ import annotations

import time


from app.graph import get_graph_state, set_graph_state
from app.graph.builder import build_graph_state
from app.main import compute_fiedler_curve
from app.models.optimization_run import OptimizationRun


_EXPECTED_KEYS = {
    "step", "removed", "removed_name", "lambda2", "delta_pct", "collapsed_boms",
}
# Step 0 additionally carries the collapse check's provenance, which
# /benchmark/fiedler-curve promotes to the response so an empty collapsed_boms
# list can be told apart from a check that never ran.
_EXPECTED_KEYS_STEP_0 = _EXPECTED_KEYS | {"boms_checked", "bom_source"}


def test_curve_structure(graph_db_session):
    """Curve has exactly 6 entries with the expected schema; baseline has None for removal."""
    gs = build_graph_state(graph_db_session)
    curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)

    assert len(curve) == 6
    for i, entry in enumerate(curve):
        expected = _EXPECTED_KEYS_STEP_0 if i == 0 else _EXPECTED_KEYS
        assert set(entry.keys()) == expected, f"step {i} keys mismatch"
        assert entry["step"] == i

    # Baseline entry
    assert curve[0]["removed"] is None
    assert curve[0]["removed_name"] is None
    assert curve[0]["delta_pct"] == 0.0


def test_nonzero_on_connected(graph_db_session):
    """
    On the healthy 3-distributor connected test graph, baseline λ₂ must be > 0.

    This is the direct Pitfall #1 guard — the tracemin_pcg regression bug
    manifests as λ₂ = 0 on graphs that are definitely connected.
    """
    gs = build_graph_state(graph_db_session)
    curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)
    assert curve[0]["lambda2"] > 1e-6, (
        f"Pitfall #1 regression — baseline λ₂ should be > 0 on healthy graph, "
        f"got {curve[0]['lambda2']}"
    )


def test_wall_clock_bound(graph_db_session):
    """Full 6-step curve must complete in < 10 seconds on the small test graph."""
    gs = build_graph_state(graph_db_session)
    t0 = time.time()
    compute_fiedler_curve(gs, graph_db_session, top_k=5)
    elapsed = time.time() - t0
    assert elapsed < 10.0, f"Fiedler curve took {elapsed:.1f}s (expected < 10s)"


def test_lambda2_bounds_and_eventual_collapse(graph_db_session):
    """
    λ₂ values must be finite and non-negative; once distributors are exhausted,
    trailing steps must collapse to 0.0.

    NOTE: strict step-over-step monotonicity of λ₂ does NOT hold when each step
    measures λ₂ of the LARGEST connected component rather than the whole graph
    (per Pattern 3). Removing a bridge node can fragment a sparse graph into
    smaller, relatively tighter components whose λ₂ briefly rises. The robust
    signal is delta_pct measured against the fixed baseline (tested elsewhere).
    """
    gs = build_graph_state(graph_db_session)
    curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)

    for entry in curve:
        assert entry["lambda2"] >= 0.0, (
            f"λ₂ must be non-negative, got {entry['lambda2']} at step {entry['step']}"
        )
        # Algebraic connectivity of a simple graph on n nodes is bounded by n.
        # Our test graph has 13 nodes total; 2.0 is a generous upper bound on any
        # subgraph λ₂ observed under unweighted laplacian.
        assert entry["lambda2"] < 10.0, (
            f"λ₂ implausibly large ({entry['lambda2']}) at step {entry['step']}"
        )

    # Trailing entries (after the 3 distributors are exhausted) must be 0.0.
    assert curve[-1]["lambda2"] == 0.0, (
        f"Expected last step λ₂ = 0.0 after exhausting distributors, got {curve[-1]['lambda2']}"
    )


def test_fiedler_curve_on_graphstate(graph_db_session):
    """Assigning the curve onto GraphState + set_graph_state round-trips via get_graph_state."""
    gs = build_graph_state(graph_db_session)
    gs.fiedler_curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)
    set_graph_state(gs)
    try:
        gs_out = get_graph_state()
        assert gs_out is not None
        assert gs_out.fiedler_curve == gs.fiedler_curve
        assert len(gs_out.fiedler_curve) == 6
    finally:
        set_graph_state(None)


def test_graceful_fallback_on_disconnect(graph_db_session):
    """
    Requesting more removals than we have distributors must not raise.

    The test graph has 3 distributors; top_k=5 requires the last 2 removal steps
    to be padded. At least one trailing entry must have λ₂ = 0 (graph trivial).
    """
    gs = build_graph_state(graph_db_session)
    curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)
    assert len(curve) == 6
    assert any(entry["lambda2"] == 0.0 for entry in curve[1:]), (
        "Expected at least one trailing step with λ₂ = 0.0 after exhausting distributors"
    )


# ── collapsed_boms: documented since Phase 4, never written until 2026-08 ─────
#
# `collapsed_boms` was declared on the API schema and read by the endpoint, but no
# writer ever put the key in the curve. Every served point returned [] while the
# Benchmark page invited "Click a point to see which BOMs collapse". These tests
# are the guard: the key must be present, must be derived from real BOMs, and an
# empty list must be distinguishable from a check that did not run.

def _seed_benchmark_boms(session):
    """Two reference BOMs: one dual-sourced line, one sole-sourced on DigiKey.

    Mirrors the shape `seeds/run_benchmark.py` writes — the endpoint reads BOMs out
    of `optimization_runs`, not out of the seed module, so the app layer stays
    independent of scripts that are not on the path in production.
    """
    for name, mpn in (("dual_source_bom", "TEST-001"), ("sole_source_bom", "TEST-006")):
        session.add(OptimizationRun(
            run_id=7, run_tag="benchmark", bom_name=name,
            bom_items_json=[{"mpn": mpn, "quantity": 1}],
            strategy="balanced", graph_aware=False, scenario="nominal", arm="milp",
            total_cost_usd=10.0, eta_p50_days=5.0, co2_kg=1.0, cascade_risk_score=0.0,
        ))
    session.commit()


def test_collapsed_boms_key_is_always_written(graph_db_session):
    """Every step carries the key. It used to be absent from every entry."""
    gs = build_graph_state(graph_db_session)
    curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)
    for entry in curve:
        assert "collapsed_boms" in entry, f"step {entry['step']} has no collapsed_boms"
        assert isinstance(entry["collapsed_boms"], list)


def test_collapse_check_reports_when_it_did_not_run(graph_db_session):
    """No optimization_runs rows → boms_checked == 0 and a reason, not a silent [].

    An empty collapsed_boms list with boms_checked == 0 means "not computed"; the
    UI must not render it as "all BOMs remain fulfillable".
    """
    gs = build_graph_state(graph_db_session)
    curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)
    assert curve[0]["boms_checked"] == 0
    assert "did not run" in curve[0]["bom_source"]
    assert all(e["collapsed_boms"] == [] for e in curve)


def test_collapsed_boms_computed_from_real_boms(graph_db_session):
    """With BOMs seeded, the affordance actually fires and is cumulative."""
    _seed_benchmark_boms(graph_db_session)
    gs = build_graph_state(graph_db_session)
    curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)

    assert curve[0]["boms_checked"] == 2
    assert "run_id=7" in curve[0]["bom_source"]

    # Cumulative: a BOM that has collapsed never un-collapses at a later step.
    seen = set()
    for entry in curve:
        current = set(entry["collapsed_boms"])
        assert seen <= current, (
            f"step {entry['step']} dropped {sorted(seen - current)} from the collapse "
            f"set — collapsed_boms must be cumulative"
        )
        seen = current

    # The fixture graph has 3 distributors; by the last step every one of them has
    # been removed, so no line can have a supplier left.
    assert set(curve[-1]["collapsed_boms"]) == {"dual_source_bom", "sole_source_bom"}, (
        curve[-1]["collapsed_boms"]
    )

    # And the sole-sourced BOM must die no later than the dual-sourced one.
    def _first_step(name):
        return next(e["step"] for e in curve if name in e["collapsed_boms"])

    assert _first_step("sole_source_bom") <= _first_step("dual_source_bom")


def test_unresolvable_mpn_is_dropped_not_scored(graph_db_session):
    """A BOM line whose MPN is not in the catalogue is a data gap, not a collapse.

    Scoring it would blame the distributor removal for a missing component row.
    """
    graph_db_session.add(OptimizationRun(
        run_id=9, run_tag="benchmark", bom_name="ghost_bom",
        bom_items_json=[{"mpn": "NOT-A-REAL-MPN", "quantity": 1}],
        strategy="balanced", graph_aware=False, scenario="nominal", arm="milp",
        total_cost_usd=10.0, eta_p50_days=5.0, co2_kg=1.0, cascade_risk_score=0.0,
    ))
    graph_db_session.commit()

    gs = build_graph_state(graph_db_session)
    curve = compute_fiedler_curve(gs, graph_db_session, top_k=5)

    assert curve[0]["boms_checked"] == 0
    assert "unresolved MPNs: ghost_bom" in curve[0]["bom_source"]
    assert all("ghost_bom" not in e["collapsed_boms"] for e in curve)
