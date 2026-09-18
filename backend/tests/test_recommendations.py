"""
Pure-compute unit tests for the resilience recommendation engine
(app/optimization/recommendations.py).

Everything runs against a tiny hand-built in-memory SQLite session plus a
fully-controlled GraphState (explicit betweenness + single_source set), so the
structural math and the closed-form scoring can be asserted exactly.

Controlled fixture (5 components, 3 distributors):
  c1: d1 only (price 10, stocked)                → orphan of d1; supplier-development
  c2: d1 (10, stocked) + d2 (8, stock 0)         → single-source, cheaper alt → no-regret
  c3: d1 (10, stocked) + d3 (15, stock 0)        → single-source, pricier alt → hedge
  c4: d1 (5, stocked) + d2 (5, stocked)          → multi-source, NOT single-source/orphan
  c5: d1 only (price 20, stocked)                → orphan of d1; supplier-development
betweenness: d1=1.0, d2=0.5, d3=0.2
"""
import pytest
import networkx as nx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.graph import GraphState
from app.models.distributor import Distributor
from app.models.component import Component, DistributorOffer
from app.optimization.recommendations import (
    compute_criticality_sweep,
    compute_dual_sourcing_plan,
    compute_tornado,
)
from app.graph.disruption import (
    DEFAULT_BASE_ANNUAL_PROB,
    DEFAULT_CENTRALITY_SPREAD,
    DEFAULT_HORIZON_DAYS,
    MAX_FAILURE_PROB,
    annual_to_horizon_prob,
    build_failure_probabilities,
)

EMERGENCY = 0.15


@pytest.fixture()
def rec_env():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    dists = [
        Distributor(id=1, name="SoleCo", latitude=35.15, longitude=-90.05,
                    city="Memphis", state="TN", country="USA", is_domestic=True),
        Distributor(id=2, name="AltCheap", latitude=40.0, longitude=-75.0,
                    city="Philly", state="PA", country="USA", is_domestic=True),
        Distributor(id=3, name="AltPricey", latitude=51.5, longitude=0.0,
                    city="London", state="", country="UK", is_domestic=False),
    ]
    session.add_all(dists)
    for cid in range(1, 6):
        session.add(Component(id=cid, mpn=f"MPN-{cid}", manufacturer="M",
                              category="Test", risk_score=0.3))
    offers = [
        # component_id, distributor_id, price, stock
        (1, 1, 10.0, 100),
        (2, 1, 10.0, 100), (2, 2, 8.0, 0),
        (3, 1, 10.0, 100), (3, 3, 15.0, 0),
        (4, 1, 5.0, 100),  (4, 2, 5.0, 100),
        (5, 1, 20.0, 100),
    ]
    for oid, (cid, did, price, stock) in enumerate(offers, start=1):
        session.add(DistributorOffer(id=oid, component_id=cid, distributor_id=did,
                                     price=price, stock=stock, moq=1))
    session.commit()

    # Fully-controlled GraphState.
    g = nx.DiGraph()
    for did in (1, 2, 3):
        g.add_node(f"d_{did}", bipartite=0)
    for cid in range(1, 6):
        g.add_node(f"c_{cid}", bipartite=1)
    for cid, did, *_ in offers:
        g.add_edge(f"d_{did}", f"c_{cid}", weight=1.0)

    gs = GraphState(
        graph=g,
        dist_nodes=frozenset({"d_1", "d_2", "d_3"}),
        betweenness={1: 1.0, 2: 0.5, 3: 0.2},
        pagerank={},
        k_core={},
        single_source_component_ids=frozenset({1, 2, 3, 5}),
        hhi_by_category={},
        fiedler=0.0,
        n_distributors=3,
        n_components=5,
        n_edges=len(offers),
    )
    yield session, gs
    session.close()
    Base.metadata.drop_all(bind=engine)


# ── 1. Criticality sweep ────────────────────────────────────────────────────

def test_criticality_sweep_ranks_sole_supplier_top(rec_env):
    session, gs = rec_env
    entries = compute_criticality_sweep(session, gs)

    top = entries[0]
    assert top.distributor_id == 1
    assert top.name == "SoleCo"
    # d1 is the ONLY offer for c1 and c5 → exactly 2 orphaned components.
    assert top.orphan_component_count == 2
    assert set(top.orphan_component_ids) == {1, 5}
    # spend at risk = avg price of c1 (10) + c5 (20) = 30.
    assert top.spend_at_risk_usd == pytest.approx(30.0)
    # d1 is the most-exposed distributor → REI normalizes to 1.0.
    assert top.rei == pytest.approx(1.0)
    # d2/d3 orphan nothing here (their components all have d1 as an alternative).
    others = {e.distributor_id: e for e in entries if e.distributor_id != 1}
    assert all(e.orphan_component_count == 0 for e in others.values())
    assert all(e.rei == 0.0 for e in others.values())


def test_criticality_sweep_respects_bom_filter(rec_env):
    session, gs = rec_env
    # Restrict to c4 only (multi-source) → nobody orphans anything.
    entries = compute_criticality_sweep(session, gs, bom_component_ids=[4])
    assert all(e.orphan_component_count == 0 for e in entries)


# ── 2. Dual-sourcing plan ───────────────────────────────────────────────────

def test_dual_sourcing_tiers_and_ranking(rec_env):
    session, gs = rec_env
    entries = compute_dual_sourcing_plan(session, gs)
    by_cid = {e.component_id: e for e in entries}

    # c2: cheaper second source (8 < 10) → incremental cost 0 → no-regret.
    c2 = by_cid[2]
    assert c2.tier == "no-regret"
    assert c2.recommended_second_source == "AltCheap"
    assert c2.risk_reduction_per_dollar is None
    assert c2.incremental_unit_cost_usd == pytest.approx(0.0)

    # c3: pricier second source (15 > 10) → hedge, with closed-form ROI.
    c3 = by_cid[3]
    assert c3.tier == "hedge"
    assert c3.recommended_second_source == "AltPricey"
    # p_fail is the CALIBRATED horizon disruption probability, not centrality.
    probs = build_failure_probabilities(sorted(gs.betweenness), gs.betweenness)
    p1 = probs[1]
    p2 = probs[3]
    price1, price2 = 10.0, 15.0
    exp_risk_reduction = (p1 - p1 * p2) * EMERGENCY * price1
    exp_incremental = max(0.0, price2 - price1)
    exp_rrpd = exp_risk_reduction / exp_incremental
    # The dataclass rounds money to 4dp and ratios to 6dp, hence the abs tolerances.
    assert c3.risk_reduction_usd == pytest.approx(exp_risk_reduction, abs=1e-4)
    assert c3.incremental_unit_cost_usd == pytest.approx(exp_incremental)
    assert c3.risk_reduction_per_dollar == pytest.approx(exp_rrpd, abs=1e-6)
    assert c3.expected_disruption_cost_usd == pytest.approx(
        p1 * EMERGENCY * price1, abs=1e-4
    )

    # c1 & c5: no alternative distributor at all → supplier-development.
    for cid in (1, 5):
        e = by_cid[cid]
        assert e.tier == "supplier-development"
        assert e.recommended_second_source is None
        assert e.second_source_price_usd is None
        assert e.risk_reduction_per_dollar is None

    # Ranking: no-regret first, then hedge, then supplier-development.
    assert entries[0].component_id == 2
    assert entries[1].component_id == 3
    assert {entries[2].component_id, entries[3].component_id} == {1, 5}


def test_dual_sourcing_qualification_cost_flips_no_regret_to_hedge(rec_env):
    session, gs = rec_env
    # A qualification cost makes the "free" second source cost money → hedge.
    entries = compute_dual_sourcing_plan(session, gs, qualification_cost_usd=2.0)
    c2 = next(e for e in entries if e.component_id == 2)
    assert c2.tier == "hedge"
    assert c2.incremental_unit_cost_usd == pytest.approx(2.0)
    assert c2.risk_reduction_per_dollar is not None


# ── 3. Tornado / one-way sensitivity ────────────────────────────────────────

def test_tornado_bars_sorted_desc_by_spread(rec_env):
    session, gs = rec_env
    result = compute_tornado(session, gs, bom_component_ids=[1, 2, 3, 4, 5], metric="cost")

    assert result["metric"] == "cost"
    assert result["baseline_output"] >= 0.0
    bars = result["bars"]
    assert len(bars) >= 3
    spreads = [b.spread for b in bars]
    assert spreads == sorted(spreads, reverse=True)
    for b in bars:
        assert b.spread == pytest.approx(abs(b.high_output - b.low_output))


def test_tornado_cvar_metric(rec_env):
    session, gs = rec_env
    result = compute_tornado(session, gs, bom_component_ids=[1, 2, 3, 4, 5], metric="cvar")
    assert result["metric"] == "cvar"
    # CVaR is a cost multiplier >= 1.0.
    assert result["baseline_output"] >= 1.0


# ── 4. REGRESSION: p_fail is a calibrated probability, not raw centrality ────
#
# Guards the 2026-08 repair of `compute_dual_sourcing_plan`, which published
#     p_fail = min(betweenness * stress_factor, 1.0)
# as `p_fail_current` / `p_fail_second`. Betweenness is a graph centrality score: no
# base rate, no exposure window, no unit. Because the builder's min-max normalization
# attains exactly 1.0 at its maximum, the most central distributor was published as
# failing with probability 1.0 — the same pathology already repaired in
# graph/builder.py and graph/simulation.py.

def test_dual_sourcing_p_fail_is_not_raw_betweenness(rec_env):
    """The published p_fail must not be the centrality score it used to be."""
    session, gs = rec_env
    entries = compute_dual_sourcing_plan(session, gs)
    assert entries

    # d1 is the sole supplier on every single-source line and has betweenness 1.0.
    # The old code published p_fail_current == 1.0 for all of them.
    for e in entries:
        assert e.current_supplier == "SoleCo"
        assert e.p_fail_current != pytest.approx(gs.betweenness[1])
        assert e.p_fail_current < 1.0

    # d3 ("AltPricey") has betweenness 0.2 and is the recommended second source for c3.
    c3 = next(e for e in entries if e.component_id == 3)
    assert c3.p_fail_second is not None
    assert c3.p_fail_second != pytest.approx(gs.betweenness[3])


def test_dual_sourcing_p_fail_lies_in_the_calibrated_band(rec_env):
    """Every published p_fail must sit inside the cited calibration's own bounds."""
    session, gs = rec_env
    entries = compute_dual_sourcing_plan(session, gs)

    p_base = annual_to_horizon_prob(DEFAULT_BASE_ANNUAL_PROB, DEFAULT_HORIZON_DAYS)
    # ~4.36% over the 60-day PO window, from the McKinsey 3.7-year base rate.
    assert 0.04 < p_base < 0.05
    lo = p_base / DEFAULT_CENTRALITY_SPREAD   # least-central supplier
    hi = min(p_base * DEFAULT_CENTRALITY_SPREAD, MAX_FAILURE_PROB)  # most-central

    published = [e.p_fail_current for e in entries]
    published += [e.p_fail_second for e in entries if e.p_fail_second is not None]
    assert published
    # `published` values are rounded to 6dp by the dataclass, hence the tolerance.
    tol = 1e-6
    for p in published:
        assert lo - tol <= p <= hi + tol, f"{p} outside calibrated band [{lo}, {hi}]"
        assert p <= MAX_FAILURE_PROB + tol


def test_dual_sourcing_p_fail_matches_the_shared_calibration_exactly(rec_env):
    """p_fail must be the app's ONE calibration, not a second one invented here."""
    session, gs = rec_env
    expected = build_failure_probabilities(sorted(gs.betweenness), gs.betweenness)
    entries = compute_dual_sourcing_plan(session, gs)

    c3 = next(e for e in entries if e.component_id == 3)
    assert c3.p_fail_current == pytest.approx(round(expected[1], 6))
    assert c3.p_fail_second == pytest.approx(round(expected[3], 6))
    # ...and everything derived from it moves with it.
    assert c3.expected_disruption_cost_usd == pytest.approx(
        expected[1] * EMERGENCY * c3.current_price_usd, abs=1e-4
    )


def test_dual_sourcing_prefers_gs_p_disruption_when_present(rec_env):
    """A GraphState that already carries p_disruption is used verbatim."""
    import dataclasses

    session, gs = rec_env
    forced = {1: 0.30, 2: 0.20, 3: 0.10}
    gs2 = dataclasses.replace(gs, p_disruption=forced)
    c3 = next(
        e for e in compute_dual_sourcing_plan(session, gs2) if e.component_id == 3
    )
    assert c3.p_fail_current == pytest.approx(0.30)
    assert c3.p_fail_second == pytest.approx(0.10)


def test_dual_sourcing_stress_factor_scales_the_calibrated_probability(rec_env):
    """stress_factor survives the repair as a multiplier on a CALIBRATED number.

    Same semantics as graph/simulation.py::run_monte_carlo — min(p * stress, 1.0).
    """
    session, gs = rec_env
    base = next(e for e in compute_dual_sourcing_plan(session, gs) if e.component_id == 3)
    stressed = next(
        e for e in compute_dual_sourcing_plan(session, gs, stress_factor=2.0)
        if e.component_id == 3
    )
    assert stressed.p_fail_current == pytest.approx(min(base.p_fail_current * 2.0, 1.0), abs=1e-5)
    assert stressed.expected_disruption_cost_usd > base.expected_disruption_cost_usd
