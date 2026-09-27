"""
Invariants of the two-stage stochastic sourcing program (app/optimization/stochastic.py).

Three families of test here, in descending order of how much they matter:

1. **The probability calibration is not the old broken one.** The repo's existing
   simulator reads min-max normalized betweenness centrality directly as a failure
   probability, which forces the most central distributor to p = 1.0 in every
   scenario. These tests pin the properties that make the new calibration different:
   nothing saturates, the median supplier sits at the cited base rate, and centrality
   can be switched off entirely.

2. **The stochastic model degenerates correctly.** With no uncertainty it must
   reproduce the deterministic landed cost exactly -- scored through `greedy.
   landed_cost_breakdown`, the same anti-rigging helper `test_greedy.py` uses. If the
   stochastic program cannot reproduce the deterministic answer when there is nothing
   to be stochastic about, none of its other numbers mean anything.

3. **The frontier behaves like a frontier.** CVaR dominates both VaR and the mean,
   lambda = 0 attains the lowest expected cost in the sweep, lambda = 1 attains the
   lowest CVaR, risk aversion actually buys a lower tail and is not free, and the
   published statistics for a plan match an independent exact re-evaluation of that
   same plan.
"""
from __future__ import annotations

import math

import pytest

from app.optimization.greedy import landed_cost_breakdown
from app.optimization.sourcing import BomLine, Offer, SourcingAssignment
from app.optimization.stochastic import (
    DEFAULT_ALPHA,
    DEFAULT_BASE_ANNUAL_PROB,
    DEFAULT_RELATIVE_GAP,
    MAX_ENUMERABLE_DISTRIBUTORS,
    MAX_FAILURE_PROB,
    DisruptionScenario,
    ModelInfeasibleError,
    ModelInvalidError,
    ScenarioSet,
    SolverBudgetExceededError,
    StochasticSolveError,
    annual_to_horizon_prob,
    build_failure_probabilities,
    compute_frontier,
    compute_frontier_sweep,
    count_recourse_variables,
    enumerate_scenarios,
    evaluate_plan,
    find_knee,
    fit_scenario_set,
    frontier_shape,
    quantize_probabilities,
    saa_optimality_gap,
    sample_scenarios,
    solve_stochastic_sourcing,
    tail_composition,
    weighted_var_cvar,
)
from app.optimization.strategies import get_strategy

BALANCED = get_strategy("balanced")


def _offer(cid: int, did: int, price: float, stock: int = 10_000, moq: int = 1) -> Offer:
    return Offer(
        component_id=cid,
        distributor_id=did,
        distributor_name=f"dist-{did}",
        price_usd=price,
        stock=stock,
        moq=moq,
        is_domestic=True,
        dist_km_from_depot=100.0,
    )


def _two_line_bom(qty: int = 100):
    bom = [
        BomLine(component_id=1, mpn="PART-A", quantity=qty),
        BomLine(component_id=2, mpn="PART-B", quantity=qty),
    ]
    offers = [
        _offer(1, 10, 2.00), _offer(1, 11, 2.40), _offer(1, 12, 3.10),
        _offer(2, 10, 1.00), _offer(2, 11, 1.30), _offer(2, 12, 1.90),
    ]
    return bom, offers


def _wide_bom(qty: int = 200):
    """
    Two lines sourced from a SIX-distributor pool (ids 10..15) on a price ladder.

    Width matters for any test about tail composition: the cost distribution has at
    most 2**|D| atoms, so a three-supplier BOM can only ever produce eight, and its
    5% tail is one atom wide no matter how it is evaluated.
    """
    bom = [
        BomLine(component_id=1, mpn="PART-A", quantity=qty),
        BomLine(component_id=2, mpn="PART-B", quantity=qty),
    ]
    offers = []
    for i, did in enumerate(range(10, 16)):
        offers.append(_offer(1, did, 2.00 + 0.30 * i, stock=qty * 2))
        offers.append(_offer(2, did, 1.00 + 0.22 * i, stock=qty * 2))
    return bom, offers


def _certain_scenarios(n_draws: int = 50) -> ScenarioSet:
    """A scenario set in which nothing ever fails."""
    return ScenarioSet(
        scenarios=[DisruptionScenario(failed=frozenset(), count=n_draws)],
        n_draws=n_draws,
        seed=0,
        failure_probs={},
    )


# ── 1. Probability calibration ───────────────────────────────────────────────

def test_horizon_conversion_shrinks_an_annual_rate():
    """A 60-day exposure window must carry far less risk than a whole year."""
    p_year = annual_to_horizon_prob(0.2374, 365)
    p_60d = annual_to_horizon_prob(0.2374, 60)
    assert p_year == pytest.approx(0.2374, abs=1e-9)
    assert p_60d == pytest.approx(1 - 0.7626 ** (60 / 365), abs=1e-9)
    assert 0.03 < p_60d < 0.06, (
        f"a 23.7%/yr rate over 60 days should be ~4.4%, got {p_60d:.4f} -- the "
        "horizon conversion is the step that stops an ANNUAL rate being charged "
        "against a single purchase order"
    )


def test_horizon_conversion_rejects_nonsense_inputs():
    with pytest.raises(ValueError):
        annual_to_horizon_prob(1.0, 60)
    with pytest.raises(ValueError):
        annual_to_horizon_prob(0.2, 0)


def test_no_supplier_ever_saturates_at_probability_one():
    """
    REGRESSION GUARD for the defect this module exists to fix.

    graph/simulation.py:155-161 uses min-max normalized betweenness AS the failure
    probability. A min-max normalization always attains 1.0 at its maximum, so the
    most central distributor fails in 100% of scenarios. Here the most central
    supplier is handed betweenness exactly 1.0 -- the pathological input -- and must
    still come out with a sane probability.
    """
    betweenness = {1: 1.0, 2: 0.5, 3: 0.05, 4: 0.0, 5: 0.0}
    probs = build_failure_probabilities([1, 2, 3, 4, 5], betweenness)

    assert max(probs.values()) < MAX_FAILURE_PROB + 1e-12
    assert probs[1] < 0.5, (
        f"the most central distributor got p={probs[1]:.3f}; betweenness is being "
        "read as a probability again"
    )
    assert min(probs.values()) > 0.0, "every supplier carries some disruption risk"


def test_median_supplier_sits_on_the_cited_base_rate():
    """Centrality re-shapes risk around the base rate; it must not move the level."""
    betweenness = {i: float(i) for i in range(1, 8)}  # 7 suppliers, distinct ranks
    probs = build_failure_probabilities(
        list(betweenness), betweenness,
        base_annual_prob=DEFAULT_BASE_ANNUAL_PROB, horizon_days=60,
        centrality_spread=3.0,
    )
    p_base = annual_to_horizon_prob(DEFAULT_BASE_ANNUAL_PROB, 60)
    median_did = 4  # the middle of seven distinct ranks -> multiplier 3**0 == 1
    assert probs[median_did] == pytest.approx(p_base, rel=1e-9)


def test_centrality_spread_of_one_gives_a_flat_base_rate():
    """The 'centrality tells us nothing about failure' sensitivity arm."""
    betweenness = {1: 1.0, 2: 0.4, 3: 0.0}
    probs = build_failure_probabilities([1, 2, 3], betweenness, centrality_spread=1.0)
    assert len(set(probs.values())) == 1


def test_probability_is_monotone_in_centrality_and_bounded_by_the_spread():
    betweenness = {1: 0.0, 2: 0.2, 3: 0.9}
    probs = build_failure_probabilities([1, 2, 3], betweenness, centrality_spread=3.0)
    assert probs[1] < probs[2] < probs[3]
    assert probs[3] / probs[1] == pytest.approx(9.0, rel=1e-6), (
        "with spread=3 the extremes must be 3x above and 3x below the base rate, "
        "i.e. a 9x span -- and no wider"
    )


def test_ties_in_centrality_get_identical_probabilities():
    """18 of this network's 92 distributors sit at exactly betweenness 0."""
    betweenness = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.7}
    probs = build_failure_probabilities([1, 2, 3, 4], betweenness)
    assert probs[1] == probs[2] == probs[3]
    assert probs[4] > probs[1]


def test_centrality_spread_below_one_is_rejected():
    with pytest.raises(ValueError):
        build_failure_probabilities([1, 2], {1: 0.0, 2: 1.0}, centrality_spread=0.5)


def test_scenario_sampling_is_reproducible_and_deduplicated():
    probs = {d: 0.1 for d in range(1, 11)}
    a = sample_scenarios(probs, n_draws=300, seed=42)
    b = sample_scenarios(probs, n_draws=300, seed=42)
    assert [s.failed for s in a.scenarios] == [s.failed for s in b.scenarios]
    assert [s.count for s in a.scenarios] == [s.count for s in b.scenarios]

    assert sum(s.count for s in a.scenarios) == 300, "weights must be the draw counts"
    assert a.n_distinct < a.n_draws, "deduplication did nothing"
    # 10 suppliers at p=0.1 -> P(no failure) = 0.9^10 = 0.349
    assert a.p_no_disruption == pytest.approx(0.349, abs=0.06)
    assert a.mean_failures_per_scenario == pytest.approx(1.0, abs=0.25)


# ── 2. Risk statistics ───────────────────────────────────────────────────────

def test_weighted_cvar_matches_a_hand_computation():
    """
    Ten equally weighted outcomes 1..10, alpha = 0.9 -> the tail is exactly one
    sample, the worst one. CVaR = 10, VaR = 10.
    """
    values = list(range(1, 11))
    weights = [1] * 10
    var, cvar = weighted_var_cvar(values, weights, alpha=0.9)
    assert var == 10
    assert cvar == pytest.approx(10.0)


def test_weighted_cvar_splits_the_boundary_atom_fractionally():
    """
    Two outcomes, weights 1 and 99, alpha = 0.95 -> 5% of the mass is 5 units, of
    which 1 comes from the worst value (100) and 4 from the next (10).
    CVaR = (1*100 + 4*10) / 5 = 28.

    `graph/simulation.py` takes "mean of the worst ceil(5%) SAMPLES" instead, which
    on this input returns 100 -- 3.5x too high. Splitting the boundary atom is what
    makes this the true CVaR of the discrete measure.
    """
    var, cvar = weighted_var_cvar([100.0, 10.0], [1, 99], alpha=0.95)
    assert cvar == pytest.approx(28.0)
    assert var == pytest.approx(10.0)


def test_weighted_cvar_ordering_holds():
    values = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    weights = [1] * 8
    mean = sum(values) / len(values)
    var, cvar = weighted_var_cvar(values, weights, alpha=0.75)
    assert cvar >= var >= mean


def test_weighted_cvar_rejects_bad_input():
    with pytest.raises(ValueError):
        weighted_var_cvar([1.0], [1, 2], alpha=0.95)
    with pytest.raises(ValueError):
        weighted_var_cvar([], [], alpha=0.95)
    with pytest.raises(ValueError):
        weighted_var_cvar([1.0], [1], alpha=1.0)


# ── 3. The stochastic program itself ─────────────────────────────────────────

def test_with_no_uncertainty_it_reproduces_the_deterministic_landed_cost():
    """
    ANTI-RIGGING INVARIANT. Given a scenario set where nothing ever fails, every
    scenario cost must equal the plan's deterministic landed cost as scored by
    `greedy.landed_cost_breakdown` -- the shared helper the MILP benchmark uses.

    If this drifts, the stochastic program is optimizing a different cost model from
    the rest of the repo and no comparison against it is meaningful.
    """
    bom, offers = _two_line_bom(qty=100)
    res = solve_stochastic_sourcing(
        bom, offers, BALANCED, _certain_scenarios(), lam=0.5, us_only=False,
    )
    scored = landed_cost_breakdown(res.assignments, offers, bom, BALANCED)

    assert res.first_stage_cost_usd == pytest.approx(scored["total_cost"], abs=0.02)
    assert res.expected_cost_usd == pytest.approx(scored["total_cost"], abs=0.02)
    assert res.cvar_usd == pytest.approx(scored["total_cost"], abs=0.02)
    assert res.expected_recourse_usd == pytest.approx(0.0, abs=1e-9)


def test_with_no_uncertainty_it_consolidates_onto_the_cheapest_supplier():
    """Sanity: with no risk, fixed-charge economics should still dominate."""
    bom, offers = _two_line_bom(qty=100)
    res = solve_stochastic_sourcing(
        bom, offers, BALANCED, _certain_scenarios(), lam=0.0, us_only=False,
    )
    assert res.n_suppliers == 1
    assert res.selected_distributor_ids == [10]


def test_cvar_dominates_both_var_and_the_mean():
    """
    CVaR >= VaR and CVaR >= E always. VaR >= E does NOT hold in general and is
    deliberately not asserted: this cost distribution is a large point mass at the
    no-disruption cost with a thin, expensive tail, so the mean is pulled above the
    95th percentile. That asymmetry is exactly why CVaR is the right tail measure
    here and VaR is not.
    """
    bom, offers = _two_line_bom(qty=200)
    probs = build_failure_probabilities([10, 11, 12], {10: 1.0, 11: 0.3, 12: 0.0})
    scenarios = sample_scenarios(probs, n_draws=200, seed=7)
    for lam in (0.0, 0.5, 1.0):
        res = solve_stochastic_sourcing(
            bom, offers, BALANCED, scenarios, lam=lam, us_only=False, time_limit_s=30,
        )
        assert res.cvar_usd >= res.var_usd - 1e-6
        assert res.cvar_usd >= res.expected_cost_usd - 1e-6
        assert res.expected_cost_usd >= res.first_stage_cost_usd - 1e-6, (
            "expected cost must sit at or above the committed first-stage spend; a "
            "disruption cannot make the plan cheaper on average"
        )


def test_risk_aversion_moves_the_award_to_a_lower_probability_supplier():
    """
    The behavioural test that the whole module exists for.

    d10 is the cheapest supplier and also the most central, so the calibration gives
    it the highest disruption probability. A risk-neutral buyer takes the cheap,
    fragile supplier. A CVaR-averse buyer should pay up for the safer one -- and the
    difference must be visible in the tail, not just in the objective.
    """
    bom, offers = _two_line_bom(qty=200)
    probs = build_failure_probabilities([10, 11, 12], {10: 1.0, 11: 0.3, 12: 0.0})
    assert probs[10] > probs[11] > probs[12]
    scenarios = sample_scenarios(probs, n_draws=200, seed=7)

    risk_neutral = solve_stochastic_sourcing(
        bom, offers, BALANCED, scenarios, lam=0.0, us_only=False, time_limit_s=30,
    )
    risk_averse = solve_stochastic_sourcing(
        bom, offers, BALANCED, scenarios, lam=1.0, us_only=False, time_limit_s=30,
    )
    assert risk_averse.cvar_usd < risk_neutral.cvar_usd, (
        "buying risk aversion must actually buy a lower tail"
    )
    assert risk_averse.expected_cost_usd >= risk_neutral.expected_cost_usd - 1e-6, (
        "and it must not be free -- expected cost cannot improve as well"
    )


def test_disruption_forces_recourse_that_covers_the_whole_shortfall():
    """
    Single-source the BOM onto d10, then fail d10 in every scenario. Every committed
    unit must be re-procured or explicitly recorded as unmet -- the recourse balance
    constraint is what makes this a two-stage program rather than a cost surcharge.
    """
    bom, offers = _two_line_bom(qty=50)
    plan = [
        SourcingAssignment(1, "PART-A", 10, "dist-10", 50, 2.00),
        SourcingAssignment(2, "PART-B", 10, "dist-10", 50, 1.00),
    ]
    always_down = ScenarioSet(
        scenarios=[DisruptionScenario(failed=frozenset({10}), count=40)],
        n_draws=40, seed=0, failure_probs={10: 1.0},
    )
    profile = evaluate_plan(plan, bom, offers, BALANCED, always_down, us_only=False)

    outcome = profile.outcomes[0]
    assert outcome.emergency_units + outcome.unmet_units == 100
    assert outcome.unmet_units == 0, "d11 and d12 hold plenty of stock"
    assert outcome.recourse_cost_usd > 0.0, "recovering from a total outage is not free"
    assert profile.expected_cost_usd > profile.first_stage_cost_usd


def test_unmet_demand_is_recorded_when_survivors_cannot_cover():
    """No survivor has the stock, so the shortfall must surface as unmet units."""
    bom = [BomLine(component_id=1, mpn="PART-A", quantity=100)]
    offers = [_offer(1, 10, 2.00, stock=100), _offer(1, 11, 2.50, stock=30)]
    plan = [SourcingAssignment(1, "PART-A", 10, "dist-10", 100, 2.00)]
    always_down = ScenarioSet(
        scenarios=[DisruptionScenario(failed=frozenset({10}), count=10)],
        n_draws=10, seed=0, failure_probs={10: 1.0},
    )
    profile = evaluate_plan(plan, bom, offers, BALANCED, always_down, us_only=False)
    outcome = profile.outcomes[0]
    assert outcome.emergency_units == 30, (
        "the 30 units a survivor still holds must be bought, not written off -- if "
        "this is 0 the unmet-demand penalty has fallen below the cost of recourse"
    )
    assert outcome.unmet_units == 70


def test_published_statistics_match_an_independent_re_evaluation():
    """
    The solver's own recourse variables can be left near-optimal when the MIP gap does
    not close, so `solve_stochastic_sourcing` reports statistics from an independent
    exact re-evaluation. This pins that the two agree for the plan returned.
    """
    bom, offers = _two_line_bom(qty=150)
    probs = build_failure_probabilities([10, 11, 12], {10: 1.0, 11: 0.4, 12: 0.0})
    scenarios = sample_scenarios(probs, n_draws=150, seed=11)
    res = solve_stochastic_sourcing(
        bom, offers, BALANCED, scenarios, lam=0.5, us_only=False, time_limit_s=30,
    )
    again = evaluate_plan(res.assignments, bom, offers, BALANCED, scenarios)
    assert again.expected_cost_usd == pytest.approx(res.expected_cost_usd, abs=0.01)
    assert again.cvar_usd == pytest.approx(res.cvar_usd, abs=0.01)
    assert again.first_stage_cost_usd == pytest.approx(res.first_stage_cost_usd, abs=0.01)


def test_lambda_zero_attains_the_lowest_expected_cost_in_the_sweep():
    bom, offers = _two_line_bom(qty=200)
    probs = build_failure_probabilities([10, 11, 12], {10: 1.0, 11: 0.3, 12: 0.0})
    scenarios = sample_scenarios(probs, n_draws=200, seed=3)
    points, _ = compute_frontier(
        bom, offers, BALANCED, scenarios, [0.0, 0.5, 1.0], time_limit_s=30,
    )
    by_lam = {p.lam: p for p in points}
    best_e = min(p.expected_cost_usd for p in points)
    assert by_lam[0.0].expected_cost_usd == pytest.approx(best_e, abs=0.01)


def test_lambda_one_attains_the_lowest_cvar_in_the_sweep():
    bom, offers = _two_line_bom(qty=200)
    probs = build_failure_probabilities([10, 11, 12], {10: 1.0, 11: 0.3, 12: 0.0})
    scenarios = sample_scenarios(probs, n_draws=200, seed=3)
    points, _ = compute_frontier(
        bom, offers, BALANCED, scenarios, [0.0, 0.5, 1.0], time_limit_s=30,
    )
    by_lam = {p.lam: p for p in points}
    best_cvar = min(p.cvar_usd for p in points)
    assert by_lam[1.0].cvar_usd == pytest.approx(best_cvar, abs=0.01)


def test_frontier_is_returned_in_ascending_lambda_order():
    bom, offers = _two_line_bom(qty=100)
    points, _ = compute_frontier(
        bom, offers, BALANCED, _certain_scenarios(), [0.0, 0.25, 1.0], time_limit_s=20,
    )
    assert [p.lam for p in points] == [0.0, 0.25, 1.0]


def test_alpha_that_breaks_integer_coefficients_is_rejected():
    """
    CP-SAT needs integer objective coefficients, so 1/(1-alpha) must be an integer.
    Failing loudly beats silently rounding the tail level the caller asked for.
    """
    bom, offers = _two_line_bom(qty=10)
    with pytest.raises(ValueError, match="integer"):
        solve_stochastic_sourcing(
            bom, offers, BALANCED, _certain_scenarios(), lam=0.5, alpha=0.93,
        )


def test_lambda_outside_the_unit_interval_is_rejected():
    bom, offers = _two_line_bom(qty=10)
    with pytest.raises(ValueError):
        solve_stochastic_sourcing(bom, offers, BALANCED, _certain_scenarios(), lam=1.5)


def test_empty_bom_is_rejected_like_the_deterministic_solver():
    with pytest.raises(ValueError, match="BOM is empty"):
        solve_stochastic_sourcing([], [], BALANCED, _certain_scenarios(), lam=0.0)


def test_default_alpha_is_the_repo_wide_ninety_five():
    """The repo standardized on CVaR-95. Never 'EVaR'."""
    assert DEFAULT_ALPHA == 0.95


def test_default_base_rate_matches_the_cited_mckinsey_figure():
    """
    McKinsey Global Institute 2020: a month-plus disruption every 3.7 years.
    P(>=1 in a year) = 1 - exp(-1/3.7). Pinned so the "cited" number in the docs
    cannot drift away from the number the code uses.
    """
    assert DEFAULT_BASE_ANNUAL_PROB == pytest.approx(1 - math.exp(-1 / 3.7), rel=1e-12)
    assert DEFAULT_BASE_ANNUAL_PROB == pytest.approx(0.2368, abs=5e-4)


# ── Knee detection ───────────────────────────────────────────────────────────

def test_knee_is_none_when_the_frontier_is_too_short_to_have_one():
    points, _ = compute_frontier(
        bom=_two_line_bom(qty=50)[0], offers=_two_line_bom(qty=50)[1],
        weights=BALANCED, scenario_set=_certain_scenarios(), lambdas=[0.0, 1.0],
        time_limit_s=20,
    )
    assert find_knee(points) is None, (
        "a two-point frontier has no knee; inventing one would be dishonest"
    )


def test_knee_finds_the_elbow_of_a_synthetic_frontier():
    """
    A frontier that drops steeply then flattens: the knee is the last point of the
    steep segment. Built synthetically so the expected answer is unambiguous.
    """
    from app.optimization.stochastic import FrontierPoint

    xs_ys = [(100.0, 200.0), (101.0, 150.0), (103.0, 120.0),
             (110.0, 118.0), (130.0, 117.0), (170.0, 116.5)]
    points = [
        FrontierPoint(
            lam=i / 5, expected_cost_usd=x, cvar_usd=y, var_usd=y,
            first_stage_cost_usd=x, expected_recourse_usd=0.0, n_suppliers=1,
            supplier_ids=[1], status="OPTIMAL", gap_pct=0.0, wall_seconds=0.0,
            evaluate_seconds=0.0, n_variables=0,
        )
        for i, (x, y) in enumerate(xs_ys)
    ]
    knee = find_knee(points)
    assert knee is not None
    assert knee.expected_cost_usd == pytest.approx(103.0), (
        f"expected the elbow at E=103, got E={knee.expected_cost_usd}"
    )


# ── 4. Scenario support: enumeration vs sampling ─────────────────────────────

def test_exact_enumeration_covers_the_whole_support():
    """
    Disruption is |D| independent Bernoulli variables, so the distribution has exactly
    2**|D| atoms and they must sum to probability 1.
    """
    probs = {1: 0.10, 2: 0.05, 3: 0.20}
    exact = enumerate_scenarios(probs)

    assert exact.kind == "exact"
    assert exact.n_distinct == 8 == exact.support_size()
    assert sum(s.probability for s in exact.scenarios) == pytest.approx(1.0, abs=1e-12)
    assert exact.residual_mass == pytest.approx(0.0, abs=1e-12)

    by_set = {s.failed: s.probability for s in exact.scenarios}
    assert by_set[frozenset()] == pytest.approx(0.90 * 0.95 * 0.80)
    assert by_set[frozenset({1, 3})] == pytest.approx(0.10 * 0.95 * 0.20)
    assert by_set[frozenset({1, 2, 3})] == pytest.approx(0.10 * 0.05 * 0.20)


def test_enumeration_refuses_a_pool_it_cannot_hold():
    probs = {d: 0.05 for d in range(MAX_ENUMERABLE_DISTRIBUTORS + 2)}
    with pytest.raises(ValueError, match="exact enumeration"):
        enumerate_scenarios(probs)


def test_sampling_converges_to_the_exact_measure():
    """SAA is consistent: more draws, closer to the enumerated truth."""
    probs = {1: 0.10, 2: 0.05, 3: 0.20}
    exact = enumerate_scenarios(probs)
    exact_p_none = exact.p_no_disruption

    err_small = abs(sample_scenarios(probs, 100, 1).p_no_disruption - exact_p_none)
    err_large = abs(sample_scenarios(probs, 20000, 1).p_no_disruption - exact_p_none)
    assert err_large < err_small
    assert err_large < 0.01


def test_exact_evaluation_puts_many_atoms_in_the_tail_where_sampling_puts_few():
    """
    THE REGRESSION GUARD FOR THE 'n_distinct = 10, alpha = 0.95' OBJECTION.

    With a six-supplier pool, 200 Monte Carlo draws recover ~10 of the 64 atoms and the
    5% tail is averaged over a handful of them. Enumerating the support puts an order of
    magnitude more distinct outcomes in the same tail -- and costs nothing, because the
    support was only ever 64 atoms wide.
    """
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    sampled = sample_scenarios(probs, 200, 42)
    exact = enumerate_scenarios(probs)

    bom, offers = _wide_bom(qty=200)
    plan = solve_stochastic_sourcing(
        bom, offers, BALANCED, sampled, lam=0.5, us_only=False, time_limit_s=30,
    ).assignments

    on_sample = evaluate_plan(plan, bom, offers, BALANCED, sampled)
    on_exact = evaluate_plan(plan, bom, offers, BALANCED, exact)

    assert on_exact.evaluation_kind == "exact"
    assert on_exact.tail.n_atoms_in_tail > on_sample.tail.n_atoms_in_tail
    assert not on_exact.tail.degenerate


def test_tail_composition_flags_a_degenerate_tail():
    """One atom holding the whole tail means CVaR has collapsed onto VaR."""
    values = [100.0, 10.0, 5.0]
    weights = [0.20, 0.50, 0.30]
    comp = tail_composition(values, weights, alpha=0.95)
    assert comp.n_atoms_in_tail == 1
    assert comp.degenerate is True
    assert comp.largest_tail_atom_share == pytest.approx(1.0)

    spread = tail_composition([9.0, 8.0, 7.0, 6.0], [0.25] * 4, alpha=0.5)
    assert spread.n_atoms_in_tail == 2
    assert spread.degenerate is False


def test_cvar_is_reported_across_several_alphas_and_is_monotone():
    """A more extreme tail level can never give a smaller CVaR."""
    bom, offers = _two_line_bom(qty=150)
    probs = build_failure_probabilities([10, 11, 12], {10: 1.0, 11: 0.3, 12: 0.0})
    exact = enumerate_scenarios(probs)
    res = evaluate_plan(
        solve_stochastic_sourcing(
            bom, offers, BALANCED, sample_scenarios(probs, 200, 5), lam=0.5,
            us_only=False, time_limit_s=30,
        ).assignments,
        bom, offers, BALANCED, exact,
    )
    alphas = sorted(res.cvar_by_alpha)
    values = [res.cvar_by_alpha[a] for a in alphas]
    assert values == sorted(values), f"CVaR must be non-decreasing in alpha: {res.cvar_by_alpha}"


# ── 4b. OPTIMIZING on the exact support, not merely scoring on it ────────────
#
# THE BUG THESE PIN. The solver used to REFUSE an enumerated scenario set outright:
#
#     if scenario_set.kind != "saa":
#         raise ValueError("solve_stochastic_sourcing needs a SAMPLED scenario set:
#                           CP-SAT requires integer objective coefficients, and only
#                           draw counts supply them.")
#
# The premise was false. `round(p_s * W)` supplies exact integer weights for any
# common denominator W, and W is bounded only by the int64 objective ceiling. The
# consequence of believing it was that every published frontier was SCORED on the
# complete 64-atom support and CHOSEN on 200 draws that resolved 10 of those atoms --
# so 54 atoms carried objective weight zero in the decision, the alpha = 0.95 tail the
# optimizer actually saw was 4 atoms wide against an exact 49-54, and the page's
# "scenario support: exact, 64 atoms" described only the half of the pipeline that was
# not making the decision.
#
# Measured on the published instance in the exact quantity the risk-neutral objective
# minimizes: E[recourse] = $398.78 on the sample against $119.61 exact, a 3.3x error.


def test_the_solver_optimises_on_an_enumerated_support_rather_than_refusing_it():
    """An exact set must SOLVE, and must report that it was solved exactly."""
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    exact = enumerate_scenarios(probs)

    res = solve_stochastic_sourcing(bom, offers, BALANCED, exact, lam=0.5)

    assert res.solve_kind == "exact", "the solve set must be reported, not inferred"
    assert res.evaluation_kind == "exact"
    assert res.solve_weight_total > 0
    assert res.assignments, "an exact solve must still return a plan"


def test_the_exact_solve_weights_the_support_the_sample_leaves_at_zero():
    """
    The defect in one assertion: a 200-draw solve gives objective weight to a handful
    of atoms, an exact solve gives it to (nearly) all of them, and the mass left below
    the integer weight resolution is reported rather than assumed away.
    """
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    exact = enumerate_scenarios(probs)
    sampled = sample_scenarios(probs, n_draws=200, seed=42)

    from_sample = solve_stochastic_sourcing(bom, offers, BALANCED, sampled, lam=0.5,
                                            evaluation_set=exact)
    from_exact = solve_stochastic_sourcing(bom, offers, BALANCED, exact, lam=0.5,
                                           evaluation_set=exact)

    assert from_exact.n_scenarios_weighted > 3 * from_sample.n_scenarios_weighted, (
        f"an exact solve weighted {from_exact.n_scenarios_weighted} atoms against the "
        f"sample's {from_sample.n_scenarios_weighted}; that gap IS the defect"
    )
    assert from_exact.n_scenarios_weighted >= 0.5 * exact.n_distinct
    # Whatever mass falls below the weight resolution is a published number, and it is
    # orders of magnitude smaller than the mass a 200-draw sample simply never sees.
    assert from_exact.solve_residual_mass < 1e-3
    assert from_sample.solve_residual_mass == 0.0  # draw counts have no residual


def test_choosing_on_the_exact_support_never_loses_to_choosing_on_a_sample():
    """
    The plan chosen on the exact measure must be at least as good ON THAT MEASURE as
    the plan chosen on a sample of it. This is the property that makes the fix worth
    making: not that the numbers move, but that they move the right way.
    """
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    exact = enumerate_scenarios(probs)
    sampled = sample_scenarios(probs, n_draws=200, seed=42)

    for lam in (0.0, 0.3, 0.7, 1.0):
        a = solve_stochastic_sourcing(bom, offers, BALANCED, sampled, lam=lam,
                                      evaluation_set=exact)
        b = solve_stochastic_sourcing(bom, offers, BALANCED, exact, lam=lam,
                                      evaluation_set=exact)

        def value(r, lam=lam):
            cvar = r.cvar_by_alpha.get(DEFAULT_ALPHA, r.cvar_usd)
            return (1.0 - lam) * r.expected_cost_usd + lam * cvar

        # A cent of slack for the integer weight quantization; the differences this
        # catches are dollars.
        assert value(b) <= value(a) + 0.01, (
            f"lam={lam}: choosing on the exact support gave {value(b):.2f}, worse than "
            f"choosing on a 200-draw sample at {value(a):.2f}"
        )


def test_integer_weights_reproduce_draw_counts_at_the_sample_denominator():
    """
    The generalization is a strict superset of the old behaviour, not a replacement
    for it: quantizing a SAMPLED set's probabilities at its own draw count returns the
    draw counts exactly, so nothing about a sampled solve changed.
    """
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    sampled = sample_scenarios(probs, n_draws=200, seed=7)

    weights, residual = quantize_probabilities(sampled.probabilities, sampled.n_draws)

    assert weights == [s.count for s in sampled.scenarios]
    assert residual == 0.0


def test_the_weight_denominator_never_breaches_the_int64_objective_ceiling():
    """
    The magnitude guard is not loosened to make room for the exact weights; the weights
    are chosen to fit under it. A BOM big enough to exhaust the resolution must RAISE
    rather than quietly publish a measure the weights cannot represent.
    """
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    exact = enumerate_scenarios(probs)

    res = solve_stochastic_sourcing(bom, offers, BALANCED, exact, lam=0.85)
    assert res.solve_weight_total >= 1_000, (
        "below this the quantized measure stops describing the support faithfully"
    )

    huge_bom = [BomLine(component_id=b.component_id, mpn=b.mpn, quantity=10 ** 9)
                for b in bom]
    huge_offers = [
        Offer(component_id=o.component_id, distributor_id=o.distributor_id,
              distributor_name=o.distributor_name, price_usd=o.price_usd,
              stock=2 * 10 ** 9, moq=o.moq, is_domestic=o.is_domestic,
              dist_km_from_depot=o.dist_km_from_depot)
        for o in offers
    ]
    with pytest.raises(ValueError, match="int64|ceiling"):
        solve_stochastic_sourcing(huge_bom, huge_offers, BALANCED, exact, lam=0.85)


# ── 4c. The solver proves optimality; it does not stop on a tolerance ────────
#
# THE BUG THIS PINS. `relative_gap_limit` was 0.001, which licences CP-SAT to return an
# incumbent up to 0.1% worse than the optimum and still label it OPTIMAL. On the
# published frontier 0.1% is $111-183 per point while ADJACENT points are $177-264
# apart, so the solver tolerance was the same order as the resolution of the curve it
# was drawing. Three runs of identical code on identical data returned three different
# frontiers, one of them containing a DOMINATED point at lambda = 1.0, and the headline
# "CVaR removed per dollar beyond the knee" read 0.409 in one place and 0.342 in
# another. These solves take hundredths of a second; there was never a budget argument
# for the tolerance.


def test_the_solver_is_required_to_prove_optimality_not_merely_approach_it():
    assert DEFAULT_RELATIVE_GAP == 0.0, (
        "a non-zero relative gap limit lets CP-SAT return a non-optimal plan labelled "
        "OPTIMAL, which is what made the published frontier irreproducible"
    )


def test_a_solve_that_proves_optimality_does_not_depend_on_its_time_budget():
    """
    With the tolerance at zero the only thing that can truncate a solve is the time
    limit, and these solves finish in hundredths of a second. So the SAME model under
    three different budgets must return the SAME plan -- which is what "reproducible
    frontier" means operationally.
    """
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    exact = enumerate_scenarios(probs)

    plans = []
    for time_limit_s in (5.0, 15.0, 60.0):
        res = solve_stochastic_sourcing(bom, offers, BALANCED, exact, lam=0.5,
                                        time_limit_s=time_limit_s)
        assert res.status == "OPTIMAL", (
            f"a {time_limit_s:g}s budget was not enough to PROVE optimality; the "
            "reproducibility argument rests on it being enough"
        )
        plans.append(sorted(
            (a.component_id, a.distributor_id, a.quantity) for a in res.assignments
        ))

    assert plans[0] == plans[1] == plans[2], (
        "the plan changed with the solver's time budget, so the frontier is an "
        "artefact of how long the solver happened to run"
    )


def test_lambda_zero_does_not_count_an_eta_variable_it_never_creates():
    """
    `eta` exists only when the CVaR block is built, i.e. lambda > 0. The published
    `n_variables` counted it unconditionally, so every risk-neutral point in
    docs/cvar_frontier.json overstates its model by exactly one variable.
    """
    bom, offers = _two_line_bom(qty=50)
    probs = {10: 0.1, 11: 0.05, 12: 0.02}
    sampled = sample_scenarios(probs, n_draws=60, seed=3)

    first_stage_vars = (
        2 * sum(len([o for o in offers if o.component_id == b.component_id]) for b in bom)
        + len({o.distributor_id for o in offers})
    )
    recourse_vars = count_recourse_variables(bom, offers, BALANCED, sampled)

    risk_neutral = solve_stochastic_sourcing(bom, offers, BALANCED, sampled, lam=0.0)
    assert risk_neutral.n_variables == first_stage_vars + recourse_vars, (
        "at lambda = 0 there is no eta and no z, so the count is first stage plus "
        "recourse and nothing else"
    )

    risk_averse = solve_stochastic_sourcing(bom, offers, BALANCED, sampled, lam=0.5)
    assert risk_averse.n_variables == (
        first_stage_vars + recourse_vars + sampled.n_distinct + 1
    ), "at lambda > 0 the count adds one z per weighted atom and exactly one eta"


def test_fit_prefers_the_exact_support_and_falls_back_when_it_does_not_fit():
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    exact = enumerate_scenarios(probs)

    fits = fit_scenario_set(bom, offers, BALANCED, probs, exact_set=exact)
    assert fits.exact is True
    assert fits.scenario_set is exact
    assert fits.kind == "exact"
    assert "complete" in fits.note and "sample" in fits.note

    # Squeeze the variable budget below what the exact support needs: the fit must fall
    # back to the draw ladder AND say why, never silently drop the exact set.
    exact_vars = count_recourse_variables(bom, offers, BALANCED, exact)
    tight = fit_scenario_set(
        bom, offers, BALANCED, probs, exact_set=exact,
        max_recourse_vars=exact_vars - 1,
    )
    assert tight.exact is False
    assert tight.scenario_set.kind == "saa"
    assert tight.exact_rejected_reason is not None
    assert str(exact.n_distinct) in tight.note


# ── 5. SAA solution quality (Mak, Morton & Wood 1999) ────────────────────────

def test_saa_optimality_gap_brackets_the_optimum_and_shrinks_with_sample_size():
    """
    The SAA lower bound is optimistically biased and the candidate plan's true value is
    a valid upper bound, so the gap must be non-negative -- and a larger sample must not
    make it worse.
    """
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.04 + 0.02 * (did - 9) for did in range(10, 16)}
    reference = enumerate_scenarios(probs)

    small = saa_optimality_gap(
        bom, offers, BALANCED, probs, reference, n_scenarios=25, n_replications=10,
        lam=0.5, time_limit_s=30,
    )
    large = saa_optimality_gap(
        bom, offers, BALANCED, probs, reference, n_scenarios=400, n_replications=10,
        lam=0.5, time_limit_s=30,
    )

    for est in (small, large):
        # E[v_N] <= v* holds IN EXPECTATION. With a finite number of replications the
        # sample mean can land slightly above the candidate plan's true value, which
        # makes the POINT estimate of the gap mildly negative -- that is the signal
        # "the gap is inside Monte Carlo noise", not a broken bound. The statement that
        # must hold is the interval one: the upper bound cannot sit below the lower
        # confidence limit.
        assert est.upper_bound >= est.lower_bound_ci_low
        assert est.gap_ci_high >= 0.0
        assert est.lower_bound_ci_low <= est.lower_bound_mean <= est.lower_bound_ci_high
        assert len(est.replicate_values) == est.n_replications
        assert est.upper_bound_kind.startswith("exact")
        assert abs(est.gap_pct_of_upper) < 25.0

    # More scenarios must not make the bracket worse by a material margin.
    assert large.gap_ci_high <= small.gap_ci_high * 1.5 + 1.0


def test_saa_optimality_gap_needs_enough_replications_for_a_ci():
    bom, offers = _two_line_bom(qty=50)
    probs = {10: 0.1, 11: 0.05, 12: 0.02}
    with pytest.raises(ValueError, match="replications"):
        saa_optimality_gap(
            bom, offers, BALANCED, probs, enumerate_scenarios(probs),
            n_scenarios=20, n_replications=1,
        )


# ── 6. REGRESSION: telling CP-SAT's failure statuses apart ───────────────────
#
# THE BUG THESE PIN. `solve_stochastic_sourcing` used to end with
#
#     if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
#         raise RuntimeError(f"stochastic sourcing model infeasible (status=...)")
#
# and the API turned every RuntimeError into
# `422 "No feasible sourcing plan exists for this BOM"`. CP-SAT reports UNKNOWN when
# the time limit expires before it finds ANY solution -- a statement about the search
# budget that carries no information about feasibility at all. So six of seven
# realistic BOMs were told their input had no solution when every one of them was in
# fact solvable; the service simply could not afford to solve them in 5 seconds.
#
# The regression is silent by nature: it produces a plausible 4xx with a confident
# sentence rather than a crash. Hence tests on the exception TYPE and on the WORDING.

def test_a_timeout_is_a_budget_error_and_never_claims_infeasibility():
    """UNKNOWN must raise SolverBudgetExceededError, and must not say 'infeasible'."""
    bom, offers = _wide_bom(qty=400)
    probs = {did: 0.30 for did in range(10, 16)}
    scenarios = sample_scenarios(probs, n_draws=400, seed=11)

    with pytest.raises(SolverBudgetExceededError) as excinfo:
        solve_stochastic_sourcing(
            bom, offers, BALANCED, scenarios, lam=0.5, time_limit_s=1e-4,
        )

    exc = excinfo.value
    assert exc.status == "UNKNOWN"
    assert not isinstance(exc, ModelInfeasibleError), (
        "a time limit must never be reported as a proven infeasibility"
    )
    assert exc.lam == 0.5
    assert exc.n_scenarios == scenarios.n_distinct
    assert exc.time_limit_s == 1e-4

    message = str(exc).lower()
    assert "budget" in message
    assert "infeasible" not in message, (
        "the exact word that produced the false diagnosis; it must not reappear"
    )
    assert "no feasible" not in message


def test_a_proven_infeasibility_is_a_distinct_error_type():
    """
    The other half of the contract. Demand beyond all available stock is genuinely
    unsatisfiable, CP-SAT proves it, and THAT is the case a 4xx may be raised on. If
    both cases collapsed back to one exception type the bug would be back.
    """
    bom = [BomLine(component_id=1, mpn="PART-A", quantity=5_000)]
    offers = [_offer(1, 10, 2.00, stock=100), _offer(1, 11, 2.40, stock=100)]
    scenarios = sample_scenarios({10: 0.05, 11: 0.05}, n_draws=20, seed=1)

    with pytest.raises(ModelInfeasibleError) as excinfo:
        solve_stochastic_sourcing(
            bom, offers, BALANCED, scenarios, lam=0.0, time_limit_s=10.0,
        )
    assert excinfo.value.status == "INFEASIBLE"
    assert not isinstance(excinfo.value, SolverBudgetExceededError)
    # Both are StochasticSolveError, so a caller that does not care can still catch one.
    assert isinstance(excinfo.value, StochasticSolveError)


def test_both_failure_types_remain_runtime_errors_for_existing_callers():
    """Subclassing RuntimeError is deliberate: old `except RuntimeError` sites keep working."""
    assert issubclass(ModelInfeasibleError, RuntimeError)
    assert issubclass(SolverBudgetExceededError, RuntimeError)
    assert issubclass(ModelInvalidError, RuntimeError)


# ── 7. REGRESSION: sizing the solve set so it CAN solve ──────────────────────

def test_a_scenario_set_that_fits_the_budget_is_left_alone():
    """The common case must not change: a small pool keeps all of its draws."""
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.08 for did in range(10, 16)}
    fit = fit_scenario_set(bom, offers, BALANCED, probs, n_draws=200, seed=42)

    assert fit.thinned is False
    assert fit.n_draws_used == 200
    assert fit.recourse_variables <= fit.max_recourse_variables
    assert fit.scenario_set.n_draws == 200


def test_an_oversized_scenario_set_is_thinned_to_a_genuine_sub_sample():
    """
    Over budget, the solve set shrinks -- and because `sample_scenarios` seeds an
    isolated RNG, the smaller set must be a PREFIX of the larger draw sequence rather
    than a differently-seeded second experiment. That is what makes the thinning a
    sub-sample instead of a new sample.
    """
    bom, offers = _wide_bom(qty=200)
    probs = {did: 0.30 for did in range(10, 16)}
    fit = fit_scenario_set(
        bom, offers, BALANCED, probs, n_draws=200, seed=42, max_recourse_vars=300,
    )

    assert fit.thinned is True
    assert fit.n_draws_used < 200
    assert fit.n_draws_requested == 200
    assert "thinned" in fit.note.lower()

    # The counter must agree with what the model would actually build.
    assert count_recourse_variables(
        bom, offers, BALANCED, fit.scenario_set,
    ) == fit.recourse_variables

    # Prefix property: every draw in the thinned set comes from the same sequence.
    full = sample_scenarios(probs, n_draws=200, seed=42)
    thinned_total = sum(s.count for s in fit.scenario_set.scenarios)
    assert thinned_total == fit.n_draws_used
    full_counts = {s.failed: s.count for s in full.scenarios}
    for s in fit.scenario_set.scenarios:
        assert s.failed in full_counts, (
            "a thinned draw that never appears in the full sequence means the sub-sample "
            "is not a prefix of it"
        )
        assert s.count <= full_counts[s.failed]


def test_thinning_the_solve_set_rescues_an_instance_that_otherwise_times_out():
    """
    The end-to-end point of the budget: an instance the full draw count cannot solve
    inside a realistic limit becomes solvable when the solve set is sized to fit.
    """
    bom, offers = _wide_bom(qty=400)
    probs = {did: 0.30 for did in range(10, 16)}

    fit = fit_scenario_set(
        bom, offers, BALANCED, probs, n_draws=400, seed=11, max_recourse_vars=600,
    )
    assert fit.thinned is True

    res = solve_stochastic_sourcing(
        bom, offers, BALANCED, fit.scenario_set, lam=0.5, time_limit_s=10.0,
        evaluation_set=sample_scenarios(probs, n_draws=400, seed=11),
    )
    assert res.status in {"OPTIMAL", "FEASIBLE"}
    assert res.expected_cost_usd > 0.0
    # Scored on the FULL set even though it was chosen on the thinned one -- thinning
    # costs SAA choice error, not the statistical quality of the published numbers.
    assert res.n_scenarios_distinct == fit.scenario_set.n_distinct


# ── 8. Partial frontiers, and saying so when the frontier is flat ────────────

def test_a_partial_sweep_keeps_the_points_that_solved(monkeypatch):
    """
    A frontier with some lambdas solved and the rest labelled beats an exception. The
    unsolved ones must be recorded with their status, not silently dropped.
    """
    bom, offers = _two_line_bom(qty=100)
    scenarios = sample_scenarios({10: 0.1, 11: 0.05, 12: 0.02}, n_draws=50, seed=5)

    import app.optimization.stochastic as st
    real = st.solve_stochastic_sourcing

    def _fail_below_half(*args, lam=0.0, **kwargs):
        if lam < 0.5:
            raise SolverBudgetExceededError(
                "solver budget exhausted", status="UNKNOWN", lam=lam,
                n_scenarios=scenarios.n_distinct, n_draws=50, time_limit_s=1.0,
            )
        return real(*args, lam=lam, **kwargs)

    monkeypatch.setattr(st, "solve_stochastic_sourcing", _fail_below_half)

    sweep = compute_frontier_sweep(
        bom, offers, BALANCED, scenarios, [0.0, 0.25, 0.5, 1.0],
        time_limit_s=10.0, allow_partial=True,
    )
    assert sweep.complete is False
    assert [p.lam for p in sweep.points] == [0.5, 1.0]
    assert [u.lam for u in sweep.unsolved] == [0.0, 0.25]
    assert all(u.solver_status == "UNKNOWN" for u in sweep.unsolved)
    assert all(u.reason == "solver_budget_exhausted" for u in sweep.unsolved)
    assert sweep.n_requested == 4


def test_a_proven_infeasibility_still_propagates_even_when_partial_is_allowed(monkeypatch):
    """
    `allow_partial` is about OUR budget. A BOM CP-SAT has proved unsatisfiable is a real
    finding and every other lambda will hit it too, so swallowing it would be wrong.
    """
    bom = [BomLine(component_id=1, mpn="PART-A", quantity=5_000)]
    offers = [_offer(1, 10, 2.00, stock=100)]
    scenarios = sample_scenarios({10: 0.05}, n_draws=20, seed=1)

    with pytest.raises(ModelInfeasibleError):
        compute_frontier_sweep(
            bom, offers, BALANCED, scenarios, [0.0, 0.5, 1.0],
            time_limit_s=10.0, allow_partial=True,
        )


def test_a_flat_frontier_is_described_rather_than_left_for_the_reader_to_infer():
    """
    Six identical points with a null recommendation looks broken even when it is right.
    `frontier_shape` must name the flatness and its cause.
    """
    bom, offers = _two_line_bom(qty=100)
    points, _ = compute_frontier(
        bom, offers, BALANCED, _certain_scenarios(), [0.0, 0.25, 0.5, 0.75, 1.0],
        time_limit_s=10.0,
    )
    shape = frontier_shape(points)

    assert shape["kind"] == "flat"
    assert shape["has_tradeoff"] is False
    assert shape["distinct_plans"] == 1
    assert shape["supplier_ids"]
    assert "no cost-vs-cvar trade-off" in shape["statement"].lower()
    assert "finding, not a failure" in shape["statement"]
    # And the knee finder must still refuse to invent one.
    assert find_knee(points) is None


def test_a_graded_frontier_is_reported_as_traded_not_flat():
    """
    The other side of the flat case. Same fixture as
    `test_risk_aversion_moves_the_award_to_a_lower_probability_supplier`, which is
    built so the risk-averse end genuinely pays up for a safer supplier -- so the shape
    must come back `traded`, with more than one distinct plan and a real CVaR span. If
    a change ever made everything look flat, this catches it.
    """
    bom, offers = _two_line_bom(qty=200)
    probs = build_failure_probabilities([10, 11, 12], {10: 1.0, 11: 0.3, 12: 0.0})
    points, _ = compute_frontier(
        bom, offers, BALANCED, sample_scenarios(probs, n_draws=200, seed=7),
        [0.0, 0.25, 0.5, 0.75, 1.0], time_limit_s=30.0,
        evaluation_set=enumerate_scenarios(probs),
    )
    shape = frontier_shape(points)

    assert shape["kind"] == "traded"
    assert shape["has_tradeoff"] is True
    assert shape["distinct_plans"] >= 2
    assert shape["cvar_span_usd"] > 0.0
    assert "distinct sourcing plans" in shape["statement"]


def test_compute_frontier_still_raises_by_default(monkeypatch):
    """The strict wrapper keeps its old contract: no silent partial results."""
    bom, offers = _two_line_bom(qty=100)
    scenarios = sample_scenarios({10: 0.1, 11: 0.05}, n_draws=30, seed=2)

    import app.optimization.stochastic as st

    def _always_fail(*_args, lam=0.0, **_kwargs):
        raise SolverBudgetExceededError(
            "solver budget exhausted", status="UNKNOWN", lam=lam,
            n_scenarios=1, n_draws=30, time_limit_s=1.0,
        )

    monkeypatch.setattr(st, "solve_stochastic_sourcing", _always_fail)
    with pytest.raises(SolverBudgetExceededError):
        compute_frontier(bom, offers, BALANCED, scenarios, [0.0, 1.0], time_limit_s=5.0)
