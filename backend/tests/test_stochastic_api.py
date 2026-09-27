"""
Tests for the cost-vs-CVaR frontier API (app/api/stochastic.py).

The endpoint tests split three ways:

1. **Contract** -- the response carries the frontier, the knee-derived recommendation,
   the solver diagnostics, and the caveats. The caveats are asserted, not decorative:
   they are the disclosure that the disruption probabilities are an assumption, and a
   silent regression that dropped them would be exactly the kind of quiet
   overclaiming this subsystem was built to stop.

2. **Calibration transparency** -- `/stochastic/calibration` must publish the legacy
   simulator's probability next to the calibrated one, and the legacy column must show
   the p = 1.0 saturation while the calibrated column must not.

3. **DoS posture** -- the caller cannot enlarge the compute budget. Draw count and
   lambda grid are server-fixed; BOM size and quantity are capped by the schema.
"""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

import app.api.stochastic as stoch_api
import app.graph as graph_state_module
from app.core.database import Base, get_db
from app.optimization import stochastic as stoch
from app.main import app
from app.models.component import Component, DistributorOffer
from app.models.distributor import Distributor
from app.optimization.strategies import get_strategy

from tests.conftest import TestSession, test_engine

FRONTIER = "/api/v1/stochastic/frontier"
CALIBRATION = "/api/v1/stochastic/calibration"


# 6 distributors x 6 components, deliberately shaped to admit a GRADED cost-vs-risk
# tradeoff rather than a single all-or-nothing switch.
#
# HubCo (id 1) is the cheapest source for every line AND the only distributor touching
# all six components, so the bipartite betweenness projection ranks it top and the
# calibration hands it the highest disruption probability. The rest form a price ladder
# with decreasing centrality. Because each of the six lines can be moved off HubCo
# independently, the (E, CVaR) image has interior points and the lambda sweep produces
# an actual curve -- with only two or three lines it collapses to two vertices and
# there is no knee to find (the convex-hull limitation of a weighted-sum sweep).
_SUPPLIES = {1: [1, 2, 3, 4, 5, 6], 2: [1, 2, 3, 4], 3: [2, 3, 5, 6],
             4: [1, 4, 5, 6], 5: [3, 5], 6: [4, 6]}
_UNIT_BASE = {1: 1.00, 2: 1.20, 3: 1.45, 4: 1.75, 5: 2.10, 6: 2.55}
_STOCK = {1: 6000, 2: 2500, 3: 2000, 4: 2000, 5: 1500, 6: 1500}
_GEO = {
    1: ("HubCo", "Nashville", "TN", 36.16, -86.78),
    2: ("MidCo", "Chicago", "IL", 41.88, -87.63),
    3: ("PlainsCo", "Denver", "CO", 39.74, -104.99),
    4: ("DesertCo", "Phoenix", "AZ", 33.45, -112.07),
    5: ("EdgeCo", "Seattle", "WA", 47.61, -122.33),
    6: ("RimCo", "Portland", "OR", 45.52, -122.68),
}
N_COMPONENTS = 6


@pytest.fixture()
def frontier_db():
    """
    Seed the network above.

    Uses the shared file-backed test engine rather than an in-memory SQLite DB:
    TestClient dispatches requests on a worker thread, and SQLAlchemy's in-memory
    pooling hands a *fresh, empty* database to each new thread, which shows up as
    "no such table: components" from inside the endpoint.
    """
    Base.metadata.create_all(bind=test_engine)
    session = TestSession()

    for did, (name, city, state, lat, lng) in _GEO.items():
        session.add(Distributor(
            id=did, name=name, latitude=lat, longitude=lng, city=city, state=state,
            country="USA", is_domestic=True,
        ))
    for cid in range(1, N_COMPONENTS + 1):
        session.add(Component(
            id=cid, mpn=f"FRONTIER-{cid:03d}", manufacturer="TestCo",
            manufacturer_country="USA", category="Microcontrollers",
            description=f"Frontier test part {cid}", risk_score=0.3,
        ))

    offer_id = 1
    for did, comps in _SUPPLIES.items():
        for cid in comps:
            session.add(DistributorOffer(
                id=offer_id, component_id=cid, distributor_id=did,
                price=round(_UNIT_BASE[did] * (1 + 0.35 * cid), 2),
                stock=_STOCK[did], moq=1, sku=f"SKU-{cid}-{did}", currency="USD",
            ))
            offer_id += 1
    session.commit()

    yield session

    session.close()
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def graph_client(frontier_db):
    """
    TestClient bound to `frontier_db`, with the process-global GraphState cleared so
    the endpoint builds the graph from THIS session. Without that, the app lifespan
    leaves a GraphState built from the real database in place and `_graph()` would
    prefer it -- silently scoring the test's BOM against production betweenness.
    `conftest.restore_process_globals` puts the previous global back afterwards.
    """
    def _override():
        try:
            yield frontier_db
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        graph_state_module.set_graph_state(None)
        yield client
    app.dependency_overrides.clear()


# ── Contract ─────────────────────────────────────────────────────────────────

def test_frontier_returns_a_swept_frontier(graph_client):
    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 1, "quantity": 40},
                  {"component_id": 2, "quantity": 40}],
    })
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["frontier"]) >= 2
    lambdas = [p["lambda"] for p in body["frontier"]]
    assert lambdas == sorted(lambdas), "frontier must come back in ascending lambda"
    assert lambdas[0] == 0.0 and lambdas[-1] == 1.0

    for point in body["frontier"]:
        assert point["cvar_95_usd"] >= point["expected_cost_usd"] - 1e-6
        assert point["expected_cost_usd"] >= point["first_stage_cost_usd"] - 1e-6
        assert point["n_suppliers"] >= 1
        assert point["solver_status"] in {"OPTIMAL", "FEASIBLE"}


def test_frontier_reports_solver_diagnostics_and_scenario_size(graph_client):
    """Problem size and solve quality are published, not hidden."""
    r = graph_client.post(FRONTIER, json={"items": [{"component_id": 1, "quantity": 50}]})
    assert r.status_code == 200, r.text
    body = r.json()

    solver = body["solver"]
    assert solver["num_search_workers"] == 1
    assert solver["worst_mip_gap_pct"] >= 0.0
    assert isinstance(solver["any_point_hit_time_limit"], bool)

    scen = body["scenarios"]
    # The published statistics describe a MEASURE, and which measure it is has to be
    # part of the answer: an exactly enumerated support was never drawn, so quoting a
    # draw count next to it is a category error.
    assert scen["kind"] in ("exact", "saa")
    if scen["kind"] == "saa":
        assert scen["n_draws"] == 200
        assert 1 <= scen["n_distinct"] <= scen["n_draws"]
    else:
        assert scen["n_draws"] == 0
        assert scen["n_distinct"] == 2 ** scen["evaluation_set"]["support_size_log2"]
    assert 0.0 <= scen["p_no_disruption"] <= 1.0


def test_frontier_always_discloses_that_probabilities_are_assumed(graph_client):
    """
    The caveats are load-bearing. This endpoint's numbers rest on a firm-level base
    rate used per supplier; publishing a CVaR without saying so is the failure mode
    this whole module exists to correct.
    """
    r = graph_client.post(FRONTIER, json={"items": [{"component_id": 1, "quantity": 10}]})
    assert r.status_code == 200, r.text
    caveats = " ".join(r.json()["caveats"]).lower()
    assert "assumption" in caveats
    assert "mckinsey" in caveats
    assert "correlated" in caveats, "independence of failures must be disclosed"
    assert "convex hull" in caveats, "the weighted-sum limitation must be disclosed"


def test_lower_disruption_probability_gives_a_lower_tail(graph_client):
    """The base rate is a real dial, not decoration."""
    def cvar_at(base_prob: float) -> float:
        r = graph_client.post(FRONTIER, json={
            "items": [{"component_id": 1, "quantity": 50}],
            "base_annual_prob": base_prob,
        })
        assert r.status_code == 200, r.text
        return max(p["cvar_95_usd"] for p in r.json()["frontier"])

    assert cvar_at(0.02) <= cvar_at(0.40)


def test_shorter_horizon_gives_a_lower_tail(graph_client):
    """A 7-day exposure window cannot be riskier than a 365-day one."""
    def cvar_at(days: int) -> float:
        r = graph_client.post(FRONTIER, json={
            "items": [{"component_id": 1, "quantity": 50}],
            "horizon_days": days,
        })
        assert r.status_code == 200, r.text
        return max(p["cvar_95_usd"] for p in r.json()["frontier"])

    assert cvar_at(7) <= cvar_at(365)


def test_recommendation_is_expressed_as_a_decision(graph_client):
    """
    The deliverable is not the knee's coordinates, it is the trade the knee implies:
    what a dollar of extra expected cost buys in tail reduction before it, versus
    after. Both ratios must be present and must tell the story in the right direction.
    """
    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": c, "quantity": 600} for c in range(1, 7)],
    })
    assert r.status_code == 200, r.text
    rec = r.json()["recommendation"]
    assert rec is not None, (
        "this fixture is built to have a graded frontier; a None recommendation means "
        "the sweep collapsed to fewer than three distinct non-dominated points"
    )
    assert 0.0 <= rec["knee_lambda"] <= 1.0
    assert rec["cvar_reduction_usd"] > 0.0, "the knee must actually reduce the tail"
    assert rec["extra_expected_cost_usd"] > 0.0, "and it must cost something"
    assert rec["cvar_removed_per_dollar_spent"] > rec[
        "cvar_removed_per_dollar_spent_beyond_knee"
    ], "resilience must be a worse buy after the knee than before it -- that IS the knee"
    assert rec["statement"]


def test_flat_frontier_says_so_explicitly_instead_of_returning_a_null(graph_client):
    """
    A single-line BOM off one supplier has nothing to trade off. The endpoint must still
    not nominate an arbitrary point as "the recommendation" -- but a bare `null` beside
    six identical rows reads as a broken endpoint even when it is exactly right.

    So the absence of a knee is now STATED: `available: false`, a machine-readable
    reason, and a sentence naming the cause.
    """
    r = graph_client.post(FRONTIER, json={"items": [{"component_id": 5, "quantity": 5}]})
    assert r.status_code == 200, r.text
    body = r.json()

    rec = body["recommendation"]
    assert rec is not None, "an explanation must replace the old bare null"
    assert rec["available"] is False
    assert rec["knee_lambda"] is None, "no knee may be invented"
    assert rec["reason"] in {"no_tradeoff_available", "too_few_distinct_points"}
    assert rec["statement"], "the absence of a recommendation must be explained in words"

    shape = body["frontier_shape"]
    assert shape["kind"] in {"flat", "traded"}
    assert shape["statement"]
    if shape["kind"] == "flat":
        assert shape["has_tradeoff"] is False
        assert shape["distinct_plans"] == 1
        assert "no cost-vs-cvar trade-off" in shape["statement"].lower()


def test_second_identical_request_is_served_from_cache(graph_client):
    payload = {"items": [{"component_id": 1, "quantity": 25}]}
    first = graph_client.post(FRONTIER, json=payload)
    second = graph_client.post(FRONTIER, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert second.json()["frontier"] == first.json()["frontier"]


def test_unknown_component_is_a_404(graph_client):
    r = graph_client.post(FRONTIER, json={"items": [{"component_id": 999999, "quantity": 1}]})
    assert r.status_code == 404


def test_unknown_strategy_is_a_400(graph_client):
    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 1, "quantity": 1}], "strategy": "nonsense",
    })
    assert r.status_code == 400


def test_quantity_beyond_available_stock_is_a_422_not_a_500(graph_client):
    """An infeasible BOM is the caller's problem, and must be reported as one."""
    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 1, "quantity": 100000}],
    })
    assert r.status_code == 422


# ── REGRESSION: the status-mapping bug ───────────────────────────────────────
#
# The defect this section exists to stop recurring:
#
#   `solve_stochastic_sourcing` treated ANY status outside {OPTIMAL, FEASIBLE} as
#   infeasible, and the endpoint turned that into
#     422 "No feasible sourcing plan exists for this BOM: stochastic sourcing model
#          infeasible (status=UNKNOWN, lam=1.0, scenarios=183)"
#
#   CP-SAT's UNKNOWN means "the time limit expired before I found ANY solution". It is
#   a statement about OUR search budget and says nothing whatsoever about feasibility.
#   Six of seven realistic BOMs were told their input had no solution when in fact all
#   seven were solvable -- the endpoint just could not afford to solve them.
#
# The failure mode is silent (a plausible 4xx with a confident sentence), so it is
# pinned from both ends: at the model layer that UNKNOWN raises a budget error, and at
# the API layer that a budget error is never a 422 and never claims infeasibility.

def test_unknown_status_raises_a_budget_error_not_an_infeasibility_claim(graph_client):
    """
    Model layer. Force CP-SAT to time out with an absurdly small budget on a model big
    enough that it cannot finish, and assert the exception TYPE and WORDING.
    """
    bom, offers = stoch_api._load_bom_and_offers(
        _session_of(graph_client),
        [stoch_api.BomItem(component_id=c, quantity=300) for c in range(1, 7)],
    )
    probs = {did: 0.25 for did in sorted({o.distributor_id for o in offers})}
    scenarios = stoch.sample_scenarios(probs, n_draws=200, seed=7)

    with pytest.raises(stoch.SolverBudgetExceededError) as excinfo:
        stoch.solve_stochastic_sourcing(
            bom, offers, get_strategy("balanced"), scenarios,
            lam=0.5, time_limit_s=0.001,
        )

    exc = excinfo.value
    assert not isinstance(exc, stoch.ModelInfeasibleError), (
        "a time limit must never be reported as a proven infeasibility"
    )
    assert exc.status == "UNKNOWN"
    assert exc.n_scenarios == scenarios.n_distinct
    text = str(exc).lower()
    assert "budget" in text
    assert "infeasible" not in text, (
        "the message must not use the word 'infeasible' -- that is the false diagnosis"
    )
    assert "no feasible" not in text


def test_proven_infeasibility_is_a_different_exception_from_a_timeout(graph_client):
    """
    The other side of the same coin: a BOM that CP-SAT can PROVE has no solution must
    raise `ModelInfeasibleError`, so the endpoint can legitimately return a 422. If
    both cases collapsed back to one type the bug would be back.
    """
    session = _session_of(graph_client)
    # Demand far beyond the stock in existence on this line.
    bom, offers = stoch_api._load_bom_and_offers(
        session, [stoch_api.BomItem(component_id=6, quantity=90000)],
    )
    probs = {did: 0.05 for did in sorted({o.distributor_id for o in offers})}
    scenarios = stoch.sample_scenarios(probs, n_draws=20, seed=3)

    with pytest.raises(stoch.ModelInfeasibleError) as excinfo:
        stoch.solve_stochastic_sourcing(
            bom, offers, get_strategy("balanced"), scenarios,
            lam=0.0, time_limit_s=10.0,
        )
    assert excinfo.value.status == "INFEASIBLE"
    assert not isinstance(excinfo.value, stoch.SolverBudgetExceededError)


def _budget_error(lam: float) -> stoch.SolverBudgetExceededError:
    return stoch.SolverBudgetExceededError(
        "solver budget exhausted", status="UNKNOWN", lam=lam,
        n_scenarios=42, n_draws=200, time_limit_s=1.0,
    )


def test_solver_budget_exhaustion_is_a_503_never_a_422_about_the_bom(
    graph_client, monkeypatch,
):
    """
    API layer. When every lambda exhausts the budget the response must be a 503 that
    names OUR budget, and must not assert anything about the caller's BOM.

    The failure is injected at the solver rather than by shrinking the time limit: this
    fixture's network is small enough that CP-SAT's presolve finishes it before any
    plausible limit bites, so a timing-based test would not exercise the path at all.
    Everything above the injection point -- the sweep's partial handling and the
    endpoint's status mapping -- is the real code.
    """
    def _always_out_of_budget(*_args, lam=0.0, **_kwargs):
        raise _budget_error(lam)

    monkeypatch.setattr(stoch.__name__ + ".solve_stochastic_sourcing",
                        _always_out_of_budget, raising=True)

    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 1, "quantity": 40}],
    })

    assert r.status_code != 422, (
        "our search budget running out is not a finding about the caller's BOM"
    )
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "solver_budget_exhausted"
    message = detail["message"].lower()
    assert "search budget" in message, "the message must name OUR budget as the cause"
    assert "not a finding that the bom is infeasible" in message, (
        "and must explicitly disclaim the feasibility conclusion the old 422 asserted"
    )
    # "infeasible" may appear only inside that disclaimer, never as a claim.
    assert message.count("infeasible") == 1
    assert detail["n_scenarios"] > 0
    assert detail["solver_status"] == "UNKNOWN"


def test_a_partial_frontier_beats_an_error(graph_client, monkeypatch):
    """
    A frontier with some lambdas solved and the rest labelled is a usable answer. The
    unsolved points must be disclosed, not silently dropped, and `partial` must say so.
    """
    real = stoch.solve_stochastic_sourcing

    def _fail_the_risk_neutral_end(*args, lam=0.0, **kwargs):
        if lam < 0.5:
            raise _budget_error(lam)
        return real(*args, lam=lam, **kwargs)

    monkeypatch.setattr(stoch.__name__ + ".solve_stochastic_sourcing",
                        _fail_the_risk_neutral_end, raising=True)

    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 1, "quantity": 40},
                  {"component_id": 2, "quantity": 40}],
    })
    assert r.status_code == 200, (
        "some points solved, so the caller gets those rather than an error"
    )
    body = r.json()

    assert body["partial"] is True
    assert body["frontier"], "a partial frontier must still carry the points that solved"
    assert body["unsolved_points"], "and must disclose the ones that did not"
    assert (len(body["frontier"]) + len(body["unsolved_points"])
            == body["solver"]["points_requested"])
    assert body["solver"]["points_unsolved"] == len(body["unsolved_points"])
    for u in body["unsolved_points"]:
        assert u["lambda"] < 0.5
        assert u["reason"] in {"solver_budget_exhausted", "sweep_budget_exhausted"}
        assert u["solver_status"] in {"UNKNOWN", "NOT_ATTEMPTED"}
    assert any("PARTIAL FRONTIER" in c for c in body["caveats"])


# ── Reproducing the published artifact ───────────────────────────────────────

def test_the_depot_is_disclosed_because_it_changes_the_answer(graph_client):
    """
    `docs/cvar_frontier.json` was generated at the San Francisco depot while this
    endpoint defaults to the Memphis reference hub, and the depot sets every freight
    distance -- so the two answer different questions. On the published headline BOM
    that is $147,272 vs $182,256 of expected cost at lambda = 0.

    The endpoint must therefore (a) echo the depot it used, and (b) let a caller set it,
    or the published numbers are not reproducible from the live service at all.
    """
    payload = {"items": [{"component_id": 1, "quantity": 40},
                         {"component_id": 4, "quantity": 40}]}
    default = graph_client.post(FRONTIER, json=payload)
    assert default.status_code == 200, default.text
    inst = default.json()["instance"]
    assert inst["depot_lat"] == stoch_api.DEPOT_LAT
    assert inst["depot_lng"] == stoch_api.DEPOT_LNG
    assert "cvar_frontier.json" in inst["depot_note"]

    moved = graph_client.post(FRONTIER, json={
        **payload,
        "depot_lat": stoch_api.ARTIFACT_DEPOT_LAT,
        "depot_lng": stoch_api.ARTIFACT_DEPOT_LNG,
    })
    assert moved.status_code == 200, moved.text
    assert moved.json()["instance"]["depot_lat"] == stoch_api.ARTIFACT_DEPOT_LAT
    assert moved.json()["cached"] is False, (
        "the depot must be part of the cache key -- it changes the answer"
    )


def test_scoring_is_exact_when_the_support_is_small_enough_to_enumerate(graph_client):
    """
    The endpoint used to score every plan on a 200-draw sample even when the entire
    support was a handful of atoms, while the offline script enumerated it exactly.
    That is the divergence that made the endpoint's numbers non-comparable with the
    published ones. On a small pool the endpoint must now enumerate too.
    """
    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 6, "quantity": 40}],  # supplied by 3 distributors
    })
    assert r.status_code == 200, r.text
    ev = r.json()["scenarios"]["evaluation_set"]
    if ev["support_size_log2"] > 8:
        pytest.skip("pool too large to enumerate on this fixture")
    assert ev["kind"] == "exact"
    assert ev["n_atoms"] == 2 ** ev["support_size_log2"]
    assert "no sampling error" in ev["note"]


def test_the_plan_is_chosen_on_the_same_support_it_is_scored_on(graph_client):
    """
    THE BUG THIS PINS. The endpoint scored on the exact enumerated support while
    solving on 200 Monte Carlo draws of it, and published "scenario support: exact"
    over the pair. On the headline instance that left 54 of 64 atoms at objective
    weight zero in the DECISION and an alpha = 0.95 tail 4 atoms wide against an exact
    49-54, while the page described the scoring.
    """
    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 6, "quantity": 40}],  # supplied by 3 distributors
    })
    assert r.status_code == 200, r.text
    body = r.json()
    scen = body["scenarios"]
    if scen["evaluation_set"]["kind"] != "exact":
        pytest.skip("pool too large to enumerate on this fixture")

    solve = scen["solve_set"]
    assert solve["exact"] is True, "the solve set must be the exact support, not a sample"
    assert solve["kind"] == "exact"
    assert solve["n_distinct"] == scen["evaluation_set"]["n_atoms"]
    assert solve["n_draws"] == 0, "an enumerated support was never drawn"

    # The support is enumerated, but CP-SAT weights are integers, so the measure is
    # quantized. That residual is published per point rather than rounded away.
    assert 0.0 <= solve["residual_mass"] < 1e-3
    assert solve["weight_denominator"] >= 1_000
    for point in body["frontier"]:
        assert point["solve_kind"] == "exact"
        assert point["n_atoms_weighted_in_solve"] <= solve["n_distinct"]
        assert point["solve_residual_mass"] <= solve["residual_mass"] + 1e-12

    assert any("CHOSEN on the complete" in c for c in body["caveats"])


def test_the_published_scenario_statistics_come_from_the_exact_measure(graph_client):
    """
    `p_no_disruption` and `mean_failures_per_scenario` used to be the 200-draw SAMPLE
    frequencies, printed three rows under a label advertising exact enumeration
    (0.690 against an exact 0.70053; 0.335 against an exact 0.34036). They must be read
    off the measure the risk numbers are actually taken from.
    """
    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 6, "quantity": 40}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    scen = body["scenarios"]
    if scen["kind"] != "exact":
        pytest.skip("pool too large to enumerate on this fixture")

    # Independent recomputation. For |D| independent Bernoulli failures P(no
    # disruption) is the product of the survival probabilities and E[failures] is the
    # sum of them -- both exact, neither a sample frequency. The probabilities come
    # from the same calibration the endpoint used, rebuilt here from the BOM's own
    # supplier pool (centrality is ranked WITHIN that pool, so the whole-network
    # /calibration listing is a different measure and cannot be substituted).
    cal = body["calibration"]
    session = _session_of(graph_client)
    pool = sorted({
        o.distributor_id
        for o in session.query(DistributorOffer)
        .filter(DistributorOffer.component_id == 6).all()
    })
    assert len(pool) == cal["n_distributors_in_pool"]
    gs = stoch_api._graph(session)
    probs = stoch.build_failure_probabilities(
        pool, gs.betweenness,
        base_annual_prob=cal["base_annual_prob"],
        horizon_days=cal["horizon_days"],
        centrality_spread=cal["centrality_spread"],
    )
    expected_none = math.prod(1.0 - p for p in probs.values())
    expected_mean = sum(probs.values())

    assert scen["p_no_disruption"] == pytest.approx(expected_none, abs=1e-5)
    assert scen["mean_failures_per_scenario"] == pytest.approx(expected_mean, abs=1e-5)


def _session_of(client):
    """The session the TestClient's get_db override yields."""
    gen = app.dependency_overrides[get_db]()
    session = next(gen)
    return session


# ── Calibration transparency ─────────────────────────────────────────────────

def test_calibration_publishes_the_legacy_probability_beside_the_calibrated_one(graph_client):
    r = graph_client.get(CALIBRATION)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["distributors"], "expected at least one distributor"
    for row in body["distributors"]:
        assert row["legacy_simulator_p_fail"] == pytest.approx(
            row["betweenness_normalized"]
        ), "the legacy column must be the raw betweenness, verbatim"
        assert row["p_disruption_over_horizon"] < 1.0

    contrast = body["contrast_with_existing_simulator"]
    assert contrast["max_calibrated_p_fail"] < contrast["max_legacy_p_fail"] or \
        contrast["max_legacy_p_fail"] == 0.0
    assert body["base_rate_source"]["citation"].startswith("McKinsey")
    assert "known_weakness" in body["base_rate_source"]


def test_calibration_spread_of_one_flattens_every_probability(graph_client):
    r = graph_client.get(CALIBRATION, params={"centrality_spread": 1.0})
    assert r.status_code == 200, r.text
    probs = {row["p_disruption_over_horizon"] for row in r.json()["distributors"]}
    assert len(probs) == 1, (
        "spread=1.0 is the 'centrality tells us nothing' arm; every supplier must sit "
        "on the flat base rate"
    )


def test_calibration_rejects_out_of_range_parameters(graph_client):
    assert graph_client.get(CALIBRATION, params={"base_annual_prob": 1.5}).status_code == 400
    assert graph_client.get(CALIBRATION, params={"horizon_days": 0}).status_code == 400
    assert graph_client.get(CALIBRATION, params={"centrality_spread": 0.2}).status_code == 400


# ── DoS posture ──────────────────────────────────────────────────────────────

def test_caller_cannot_enlarge_the_compute_budget(graph_client):
    """
    Draw count and lambda grid are server-fixed (the T-02-03 mitigation pattern from
    graph/simulation.py). Supplying them must not change the work done.
    """
    r = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 1, "quantity": 20}],
        "n_draws": 100000,
        "lambda_grid": [0.0, 0.01, 0.02, 0.03],
        "time_limit_s": 600,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    scen = body["scenarios"]
    # The server picks the scenario set: either the exact support (never drawn) or its
    # own 200-draw sample. A caller-supplied 100,000 reaches neither.
    assert scen["n_draws"] in (0, stoch_api.N_DRAWS)
    assert scen["solve_set"]["n_draws"] in (0, stoch_api.N_DRAWS)
    assert scen["n_distinct"] <= max(stoch_api.N_DRAWS, stoch_api.MAX_EVALUATION_ATOMS)
    assert scen["solve_set"]["second_stage_variables"] <= (
        scen["solve_set"]["variable_budget"]
    )
    assert len(body["frontier"]) == len(stoch_api.LAMBDA_GRID)
    assert body["solver"]["max_time_in_seconds_per_point"] == stoch_api.SOLVE_TIME_LIMIT_S
    assert body["solver"]["sweep_time_budget_s"] == stoch_api.SWEEP_TIME_BUDGET_S


def test_bom_size_and_quantity_are_capped_by_the_schema(graph_client):
    too_many = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 1, "quantity": 1} for _ in range(26)],
    })
    assert too_many.status_code == 422

    too_big = graph_client.post(FRONTIER, json={
        "items": [{"component_id": 1, "quantity": 100001}],
    })
    assert too_big.status_code == 422
