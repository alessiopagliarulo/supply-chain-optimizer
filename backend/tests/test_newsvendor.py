"""Tests for the newsvendor decision layer.

Three jobs, in descending order of how much they are worth.

1. **The optimum is the optimum.** The critical fractile is a closed-form claim, and a
   closed-form claim can be checked against brute force rather than against itself. Uniform
   and normal demand have analytic optima; a discrete pmf has a finite support that can be
   enumerated. Every one of those is asserted here against an independent minimisation, so
   a sign error or an off-by-one in the inverse cdf cannot pass.

2. **The evaluation measures the forecast, not the cost function.** The permutation control
   is the load-bearing test in this file. A newsvendor harness will happily produce a
   confident-looking saving from a forecast with no information in it, because ordering
   *anything* sensible beats ordering nothing under an asymmetric cost. Scoring each series
   against ANOTHER series' predictive distribution has to destroy the advantage. If it does
   not, every number this module publishes is an artifact of the cost shape.

3. **The stated invariants hold on the real data.** The MASE column recomputed here has to
   reproduce the published leaderboard, and the guard against the upstream `_size_shape`
   defect has to actually fire on the series that triggers it.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.api import newsvendor as nv_api
from app.ml.proper_scoring import pinball_loss
from app.optimization import newsvendor as nv

ARTIFACT = Path(__file__).resolve().parents[2] / "docs" / "intermittent_demand.json"
PANEL = Path(__file__).resolve().parents[1] / "seeds" / "data" / "car_parts_monthly.npz"

needs_panel = pytest.mark.skipif(not PANEL.is_file(), reason="Monash car-parts panel absent")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _grid_cost(values: np.ndarray, probs: np.ndarray, q: float, cu: float, co: float) -> float:
    """Expected newsvendor cost of ordering q against a discretised demand law."""
    return float(np.sum(probs * (cu * np.maximum(values - q, 0.0) + co * np.maximum(q - values, 0.0))))


def _brute_force_argmin(values: np.ndarray, probs: np.ndarray, grid: np.ndarray, cu: float, co: float) -> float:
    costs = np.array([_grid_cost(values, probs, q, cu, co) for q in grid])
    return float(grid[int(np.argmin(costs))])


# ── 1. The analytic optimum on distributions with a closed form ──────────────

@pytest.mark.parametrize("cu,co", [(1.0, 1.0), (9.0, 1.0), (1.0, 9.0), (0.15, 0.0208333), (3.0, 0.0208333)])
def test_uniform_closed_form_is_the_true_minimiser(cu, co):
    """D ~ U[a, b] has q* = a + tau (b - a). Checked against brute force, not against itself."""
    a, b = 4.0, 20.0
    tau = nv.critical_ratio(cu, co)
    closed = nv.uniform_order_quantity(a, b, tau)
    assert closed == pytest.approx(a + tau * (b - a))

    grid = np.linspace(a - 2.0, b + 2.0, 4001)
    values = np.linspace(a, b, 20001)
    probs = np.full(values.size, 1.0 / values.size)
    assert _brute_force_argmin(values, probs, grid, cu, co) == pytest.approx(closed, abs=0.02)


@pytest.mark.parametrize("tau", [0.05, 0.25, 0.5, 0.75, 0.878, 0.95])
def test_normal_closed_form_is_the_true_minimiser(tau):
    """q* = mu + sigma * Phi^-1(tau), checked against a brute-force minimisation."""
    mu, sigma = 40.0, 9.0
    cu, co = tau, 1.0 - tau  # any pair with this ratio
    closed = nv.normal_order_quantity(mu, sigma, tau)

    values = np.linspace(mu - 8 * sigma, mu + 8 * sigma, 40001)
    dens = np.exp(-0.5 * ((values - mu) / sigma) ** 2)
    probs = dens / dens.sum()
    grid = np.linspace(mu - 4 * sigma, mu + 4 * sigma, 4001)
    assert _brute_force_argmin(values, probs, grid, cu, co) == pytest.approx(closed, abs=0.05)


def test_discrete_fractile_is_the_exact_argmin_over_the_integers():
    """On a count law the fractile is not an approximation of the optimum -- it IS the optimum.

    Randomised over 200 pmfs and cost pairs. This is the assertion that would catch an
    off-by-one in `searchsorted`, a `>` where `>=` belongs, or a tau/1-tau inversion, none
    of which a single hand-picked example reliably exposes.
    """
    rng = np.random.default_rng(11)
    for _ in range(200):
        k = int(rng.integers(2, 25))
        pmf = rng.random(k) ** 3
        pmf[0] += 3.0  # a big zero atom, like real intermittent demand
        pmf = pmf / pmf.sum()
        cu = float(rng.uniform(0.01, 5.0))
        co = float(rng.uniform(0.01, 5.0))
        q = nv.order_quantity_from_pmf(pmf, nv.critical_ratio(cu, co))
        support = np.arange(k, dtype=float)
        costs = np.array([_grid_cost(support, pmf, float(x), cu, co) for x in support])
        best = float(costs.min())
        assert _grid_cost(support, pmf, q, cu, co) == pytest.approx(best, abs=1e-12), (
            f"fractile q={q} is not the argmin for cu={cu}, co={co}"
        )


# ── 2. Monotonicity ──────────────────────────────────────────────────────────

def test_order_quantity_is_non_decreasing_in_the_critical_ratio():
    """More expensive shortages can never mean ordering less. The core comparative static."""
    rng = np.random.default_rng(3)
    pmf = rng.random(30) ** 2
    pmf[0] += 2.0
    pmf = pmf / pmf.sum()
    taus = np.linspace(0.01, 0.99, 99)
    qs = [nv.order_quantity_from_pmf(pmf, float(t)) for t in taus]
    assert all(b >= a for a, b in zip(qs, qs[1:])), qs


def test_critical_ratio_rises_with_underage_and_falls_with_overage():
    base = nv.critical_ratio(1.0, 1.0)
    assert nv.critical_ratio(2.0, 1.0) > base
    assert nv.critical_ratio(1.0, 2.0) < base
    assert base == pytest.approx(0.5)


def test_a_longer_review_period_lowers_the_fractile():
    """Holding accrues with time, expediting does not -- so the asymmetry narrows."""
    taus = [nv.newsvendor_costs(review_period_months=m).critical_ratio for m in (1, 3, 6, 12)]
    assert all(b < a for a, b in zip(taus, taus[1:])), taus


# ── 3. Boundaries ────────────────────────────────────────────────────────────

def test_degenerate_costs_are_rejected_rather_than_clipped():
    """Cu=0 and Co=0 are not newsvendor problems; they must not silently become policies."""
    with pytest.raises(ValueError):
        nv.critical_ratio(0.0, 1.0)
    with pytest.raises(ValueError):
        nv.critical_ratio(1.0, 0.0)
    with pytest.raises(ValueError):
        nv.critical_ratio(0.0, 0.0)
    with pytest.raises(ValueError):
        nv.critical_ratio(-1.0, 1.0)


def test_extreme_fractiles_land_on_the_ends_of_the_support():
    pmf = np.array([0.5, 0.2, 0.2, 0.1])
    assert nv.order_quantity_from_pmf(pmf, 1e-9) == 0.0
    assert nv.order_quantity_from_pmf(pmf, 1.0 - 1e-12) == 3.0
    with pytest.raises(ValueError):
        nv.order_quantity_from_pmf(pmf, 0.0)
    with pytest.raises(ValueError):
        nv.order_quantity_from_pmf(pmf, 1.0)


def test_a_degenerate_forecast_orders_the_same_thing_at_every_fractile():
    """`zero` and `naive_last` have no spread, so the cost asymmetry cannot reach them.

    This is why a point forecast cannot answer a stocking question, stated as an assertion.
    """
    pmf = np.zeros(10)
    pmf[4] = 1.0
    assert {nv.order_quantity_from_pmf(pmf, t) for t in (0.01, 0.5, 0.878, 0.99)} == {4.0}


def test_expected_cost_at_zero_order_is_the_full_shortage_bill():
    pmf = np.array([0.5, 0.3, 0.2])
    out = nv.expected_cost_from_pmf(pmf, 0.0, underage_usd=2.0, overage_usd=7.0)
    assert out["expected_units_short"] == pytest.approx(0.7)
    assert out["expected_units_held"] == pytest.approx(0.0)
    assert out["expected_total_usd"] == pytest.approx(1.4)
    assert out["cycle_service_level"] == pytest.approx(0.5)


# ── 4. The identity that makes the demand leaderboard a decision leaderboard ──

def test_realized_cost_is_the_pinball_loss_scaled_by_the_total_cost():
    """cost(q, y) == (Cu + Co) * pinball(q, y, tau), exactly, for every q and y.

    Quoted in `proper_scoring.py`'s own docstring as the reason step 1.4 could reuse the
    pinball loss as a decision cost. Asserted here so it stays true.
    """
    rng = np.random.default_rng(5)
    for _ in range(500):
        cu = float(rng.uniform(0.01, 9.0))
        co = float(rng.uniform(0.01, 9.0))
        tau = nv.critical_ratio(cu, co)
        q = float(rng.integers(0, 40))
        y = float(rng.integers(0, 40))
        assert nv.realized_cost(q, y, cu, co) == pytest.approx((cu + co) * pinball_loss(q, y, tau), rel=1e-12)


def test_expected_cost_matches_a_monte_carlo_of_realized_cost():
    """The exact expectation and a simulation of the same thing must agree."""
    rng = np.random.default_rng(9)
    pmf = np.array([0.6, 0.15, 0.1, 0.08, 0.07])
    cu, co = 0.15, 0.0208333
    q = 2.0
    draws = rng.choice(len(pmf), size=400_000, p=pmf)
    mc = float(np.mean([nv.realized_cost(q, float(y), cu, co) for y in draws[:40_000]]))
    exact = nv.expected_cost_from_pmf(pmf, q, cu, co)["expected_total_usd"]
    assert mc == pytest.approx(exact, rel=0.05)


def test_aggregate_pmf_matches_a_monte_carlo_of_the_period_sum():
    pmf = np.array([0.7, 0.2, 0.06, 0.04])
    conv = nv.aggregate_pmf(pmf, 3)
    assert conv.sum() == pytest.approx(1.0)
    rng = np.random.default_rng(21)
    draws = rng.choice(len(pmf), size=(200_000, 3), p=pmf).sum(axis=1)
    empirical = np.bincount(draws, minlength=conv.size)[: conv.size] / draws.size
    assert np.max(np.abs(empirical - conv)) < 0.005
    assert nv.pmf_moments(conv)[0] == pytest.approx(3 * nv.pmf_moments(pmf)[0])


# ── 5. Where every cost input comes from ─────────────────────────────────────

def test_the_critical_fractile_does_not_depend_on_the_unit_price():
    """The property the whole panel evaluation rests on: the panel carries no prices."""
    taus = {nv.newsvendor_costs(unit_price_usd=p).critical_ratio for p in (0.01, 1.0, 17.5, 1000.0)}
    assert len(taus) == 1
    assert taus.pop() == pytest.approx(0.15 / (0.15 + 0.25 / 12.0))


def test_dollar_costs_scale_linearly_in_the_unit_price():
    one = nv.newsvendor_costs(unit_price_usd=1.0)
    forty = nv.newsvendor_costs(unit_price_usd=40.0)
    assert forty.underage_usd == pytest.approx(40.0 * one.underage_usd)
    assert forty.overage_usd == pytest.approx(40.0 * one.overage_usd)


def test_overage_is_the_repos_own_holding_cost_function_not_a_reimplementation():
    from app.optimization.costs import ANNUAL_HOLDING_RATE, holding_cost_usd

    assert ANNUAL_HOLDING_RATE == 0.25  # Gartner IT Supply Chain Benchmarks 2022
    costs = nv.newsvendor_costs(unit_price_usd=12.0, review_period_months=2.0)
    assert costs.overage_usd == pytest.approx(holding_cost_usd(12.0, 2.0 * nv.DAYS_PER_MONTH))


def test_expedite_premium_matches_the_simulation_constant():
    """Duplicated for import hygiene, pinned by a test so the duplicate cannot drift."""
    from app.graph.simulation import EMERGENCY_COST_PREMIUM

    assert nv.EXPEDITE_PREMIUM == EMERGENCY_COST_PREMIUM


def test_line_down_mode_carries_its_resolution_warning():
    """tau = 0.993 is past what 45 monthly observations can resolve. Say so, every time."""
    expedite = nv.newsvendor_costs(shortage_mode="expedite")
    line_down = nv.newsvendor_costs(shortage_mode="line_down")
    assert expedite.resolution_warning is None
    assert line_down.critical_ratio > 0.99
    assert line_down.resolution_warning is not None
    assert "EXTRAPOLATION" in line_down.resolution_warning


def test_unknown_shortage_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown shortage_mode"):
        nv.newsvendor_costs(shortage_mode="vibes")


# ── 6. Scarf's distribution-free order ───────────────────────────────────────

def test_scarf_reduces_to_the_mean_under_symmetric_costs():
    assert nv.scarf_order_quantity(10.0, 4.0, 1.0, 1.0) == pytest.approx(10.0)


def test_scarf_moves_with_the_asymmetry_and_with_the_spread():
    assert nv.scarf_order_quantity(10.0, 4.0, 9.0, 1.0) > 10.0
    assert nv.scarf_order_quantity(10.0, 4.0, 1.0, 9.0) < 10.0
    assert nv.scarf_order_quantity(10.0, 8.0, 9.0, 1.0) > nv.scarf_order_quantity(10.0, 4.0, 9.0, 1.0)
    assert nv.scarf_order_quantity(10.0, 4.0, 1.0, 0.0001) >= 0.0


# ── 7. The guard on the upstream predictive-law defect ───────────────────────

@needs_panel
def test_the_series_that_motivated_the_guard_now_produces_a_sane_law():
    """Series T1857 at 39 months WAS a real failure of `intermittent._size_shape`.

    Its non-zero order sizes were overdispersed by a few parts in 1e16, so the
    method-of-moments shape r = m^2 / (v - m) evaluated to ~1e16 instead of collapsing
    to the Poisson limit, and the size law came back with a mean of 65 against a point
    forecast of 0.78. A scoring rule saw a slightly worse CRPS; a DECISION read the
    0.878 quantile and ordered 70 units where the right answer was 2.

    `_size_shape` now guards the Poisson limit numerically rather than exactly, so this
    window is no longer pathological. This test pins the FIX end-to-end on the real
    series; `test_the_invariant_guard_still_refuses_a_broken_law` below pins the guard
    that would catch any future regression.
    """
    names, mat = nv.load_panel()
    idx = {n: i for i, n in enumerate(names)}
    for origin in (39, 45):
        pmf, source = nv.predictive_distribution(mat[idx["T1857"]][:origin], method="tsb")
        assert source == "parametric"
        q = nv.order_quantity_from_pmf(pmf, 0.878)
        assert q < 10, f"origin {origin}: ordered {q} units on a series of small counts"


def test_the_invariant_guard_still_refuses_a_broken_law(monkeypatch):
    """Defence in depth: the fix removed the known trigger, not the guard.

    `E[pmf] == point forecast` is the property the demand leaderboard is built on. If a
    future change to the compound-Bernoulli lift breaks it again, a decision must refuse
    rather than quietly order off a tail nobody measured. Forcing a mismatch proves the
    guard is still wired, independent of whether any real series currently trips it.
    """
    names, mat = nv.load_panel()
    idx = {n: i for i, n in enumerate(names)}
    train = mat[idx["T1857"]][:39]

    real_builder = nv.POINT_BUILDERS["tsb"]

    def _inflated_point(series, horizon):
        # Same law, a point forecast 100x too large -> the two must disagree.
        return [float(real_builder(series, horizon)[0]) * 100.0 + 50.0]

    monkeypatch.setitem(nv.POINT_BUILDERS, "tsb", _inflated_point)
    with pytest.raises(nv.PredictiveLawError, match="does not match its own point forecast"):
        nv.predictive_distribution(train, method="tsb")


# ── 8. The panel evaluation: does the policy beat its baselines? ─────────────

@pytest.fixture(scope="module")
def evaluation():
    if not PANEL.is_file():
        pytest.skip("Monash car-parts panel absent")
    return nv.run_panel_evaluation(n_boot=1000, seed=0)


@needs_panel
def test_the_policy_beats_every_stated_baseline_with_a_ci_that_excludes_zero(evaluation):
    """The house rule: a policy ships only by beating a stated baseline, significantly."""
    assert evaluation["panel"]["n_series_scored"] > 2500
    assert set(evaluation["baselines_beaten"]) == set(nv.BASELINE_POLICIES)
    assert all(evaluation["baselines_beaten"].values()), evaluation["baselines_beaten"]
    for name, paired in evaluation["paired_vs_newsvendor"].items():
        assert paired["mean_difference"] > 0, name
        assert paired["ci95_low"] > 0, f"{name}: CI {paired['ci95_low']}..{paired['ci95_high']} includes zero"
    assert evaluation["ship_gate"]["passed"] is True


@needs_panel
def test_the_mase_winner_is_not_the_decision_cost_winner(evaluation):
    """The point of the whole exercise, measured rather than asserted.

    `zero` -- forecasting nothing, every period -- wins MASE on this panel. Given the same
    newsvendor rule it produces the WORST decision cost of the six methods. A leaderboard
    and a decision are answering different questions.
    """
    board = evaluation["method_leaderboard"]
    assert board["order_by_mase"][0] == "zero"
    assert board["order_by_decision_cost"][-1] == "zero"
    assert board["order_by_decision_cost"][0] == "tsb"
    assert board["winner_changed"] is True


@needs_panel
def test_the_recomputed_mase_reproduces_the_published_leaderboard(evaluation):
    """If this drifts, either the protocol moved or the recomputation is not the same thing."""
    if not ARTIFACT.is_file():
        pytest.skip("intermittent_demand.json absent")
    published = json.loads(ARTIFACT.read_text())["configs"]["primary"]["leaderboard"]
    for method, mine in evaluation["method_leaderboard"]["mase_mean"].items():
        theirs = published[method]["mase"]["mean"]
        assert mine == pytest.approx(theirs, rel=0.01), f"{method}: {mine} vs published {theirs}"


@needs_panel
def test_the_permutation_control_destroys_the_advantage():
    """THE NEGATIVE CONTROL, and the most important test in this file.

    Score every series against ANOTHER series' predictive distribution. The cost function
    is unchanged, the panel is unchanged, the policy is unchanged -- the only thing removed
    is the pairing between a series and a forecast of it. Cost must rise and the ship gate
    must fail. If it did not, the measured saving would be a property of the asymmetric
    cost shape rather than of any information in the forecast, and every number this module
    publishes would be worthless.
    """
    real = nv.run_panel_evaluation(n_boot=500, seed=0, max_series=800)
    fake = nv.run_panel_evaluation(n_boot=500, seed=0, max_series=800, permute_forecasts_seed=17)
    real_cost = real["policies"][nv.NEWSVENDOR_POLICY]["mean_cost_usd_per_sku_period"]
    fake_cost = fake["policies"][nv.NEWSVENDOR_POLICY]["mean_cost_usd_per_sku_period"]
    assert fake_cost > real_cost * 1.1, f"permuted {fake_cost} vs real {real_cost}"
    assert fake["ship_gate"]["passed"] is False
    assert "permutation" in fake["ship_gate"]["reason"]
    assert any("PERMUTATION CONTROL" in c for c in fake["caveats"])


@needs_panel
def test_the_line_down_sensitivity_does_not_pass_the_ship_gate():
    """An honest failure, kept as a test because it is the point.

    At shortage_mode='line_down' the fractile is 0.993, past what 45 monthly observations
    can resolve. The policy still looks better on a point estimate, and the paired bootstrap
    says the margin over the toughest baseline is not distinguishable from zero. The gate
    catches it. If a future change makes this pass, something has been quietly flattered.
    """
    result = nv.run_panel_evaluation(shortage_mode="line_down", n_boot=1000, seed=0)
    assert result["costs"]["resolution_warning"] is not None
    assert result["ship_gate"]["passed"] is False


def test_the_ship_gate_fails_closed_on_missing_evidence():
    assert nv.evaluate_newsvendor_ship_gate(None)["passed"] is False
    assert nv.evaluate_newsvendor_ship_gate({})["passed"] is False
    assert nv.evaluate_newsvendor_ship_gate({"baselines_beaten": {}})["passed"] is False
    lost = nv.evaluate_newsvendor_ship_gate(
        {"baselines_beaten": {"a": True, "b": False}, "toughest_baseline": "b", "paired_vs_toughest_baseline": {}}
    )
    assert lost["passed"] is False and "did not beat every baseline" in lost["reason"]
    insignificant = nv.evaluate_newsvendor_ship_gate(
        {
            "baselines_beaten": {"a": True},
            "toughest_baseline": "a",
            "paired_vs_toughest_baseline": {"significant": False, "mean_difference": 0.1, "ci95_low": -0.1, "ci95_high": 0.3},
        }
    )
    assert insignificant["passed"] is False and "not significant" in insignificant["reason"]


def test_paired_bootstrap_reports_ties_rather_than_hiding_them():
    """Half the panel ties on most comparisons. A win rate that ignored ties would lie."""
    diff = np.array([0.0] * 50 + [1.0] * 30 + [-1.0] * 20)
    out = nv.paired_bootstrap(diff, n_boot=500, seed=0)
    assert out["tie_rate"] == pytest.approx(0.5)
    assert out["win_rate"] == pytest.approx(0.3)
    assert out["loss_rate"] == pytest.approx(0.2)
    assert out["win_rate"] + out["tie_rate"] + out["loss_rate"] == pytest.approx(1.0)


# ── 9. API contract ──────────────────────────────────────────────────────────

ASSUMPTIONS = "/api/v1/newsvendor/assumptions"
DECISION = "/api/v1/newsvendor/decision"
EVALUATION = "/api/v1/newsvendor/evaluation"


def test_assumptions_endpoint_names_every_cost_source(client):
    body = client.get(ASSUMPTIONS).json()
    assert body["critical_fractile"]["critical_ratio"] == pytest.approx(0.878049, abs=1e-5)
    assert "Gartner" in body["inputs"]["holding_rate_annual"]["source"]
    assert "Snyder & Daskin" in body["inputs"]["stockout_escalation_multiple"]["source"]
    assert body["inputs"]["excluded_fixed_expedite_charge"]["value"] == 150.0
    assert any("CARRYING-CHARGE" in c for c in body["caveats"])


def test_assumptions_endpoint_rejects_an_unknown_shortage_mode(client):
    assert client.get(ASSUMPTIONS, params={"shortage_mode": "nope"}).status_code == 422


def test_decision_endpoint_returns_the_order_its_fractile_and_its_caveats(client):
    history = [0, 0, 1, 0, 2, 0, 0, 1, 3, 0, 0, 1, 0, 0, 2, 1, 0, 0, 1, 0, 0, 4, 0, 1]
    resp = client.post(DECISION, json={"demand_history": history, "unit_price_usd": 12.5})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order_quantity"] >= 1
    assert body["costs"]["critical_ratio"] == pytest.approx(0.878049, abs=1e-5)
    assert body["costs"]["underage_usd_per_unit"] == pytest.approx(12.5 * 0.15)
    assert body["demand_distribution"]["source"] == "parametric"
    assert "NOT the lead-time model" in body["demand_distribution"]["driving_model"]
    assert set(body["expected"]) >= {"expected_underage_usd", "expected_overage_usd", "expected_total_usd"}
    assert body["comparisons"]["order_point_forecast"] <= body["order_quantity"]
    # The caveats are asserted, not decorative: they are the disclosure that the panel is a
    # stand-in and that the costs are industry averages. A silent regression that dropped
    # them is exactly the quiet overclaiming this subsystem exists to prevent.
    assert any("STAND-IN" in c for c in body["caveats"])
    assert any("SINGLE PERIOD" in c for c in body["caveats"])


def test_decision_endpoint_order_rises_with_the_shortage_cost(client):
    history = [0, 0, 1, 0, 2, 0, 0, 1, 3, 0, 0, 1, 0, 0, 2, 1, 0, 0, 1, 0, 0, 4, 0, 1]
    cheap = client.post(DECISION, json={"demand_history": history, "shortage_mode": "expedite"}).json()
    dear = client.post(DECISION, json={"demand_history": history, "shortage_mode": "line_down"}).json()
    assert dear["order_quantity"] >= cheap["order_quantity"]
    assert any("EXTRAPOLATION" in c for c in dear["caveats"])


def test_decision_endpoint_rejects_bad_input(client):
    assert client.post(DECISION, json={}).status_code == 422
    assert client.post(DECISION, json={"demand_history": [1, 2, 3], "series": "T1"}).status_code == 422
    assert client.post(DECISION, json={"demand_history": [1, 2, 3]}).status_code == 422
    assert client.post(DECISION, json={"demand_history": [0] * 20 + [-1]}).status_code == 422
    assert client.post(DECISION, json={"demand_history": [0] * 24, "method": "prophet"}).status_code == 422


@needs_panel
def test_decision_endpoint_can_reproduce_a_published_rolling_origin(client):
    resp = client.post(DECISION, json={"series": "T2674", "train_periods": 45, "method": "tsb"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["input"]["series"] == "T2674"
    assert body["input"]["n_periods"] == 45
    assert body["demand_distribution"]["quantiles"]["q50"] <= body["order_quantity"]
    assert client.post(DECISION, json={"series": "NOPE"}).status_code == 404


@needs_panel
def test_evaluation_endpoint_serves_the_baseline_comparison_and_its_gate(client):
    nv_api._cached_evaluation.cache_clear()
    resp = client.get(EVALUATION)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ship_gate"]["passed"] is True
    assert set(body["paired_vs_newsvendor"]) == set(nv.BASELINE_POLICIES)
    assert body["units"]["cost"].startswith("USD per SKU")
    assert body["protocol"]["permutation_control"] is False
    assert any("STAND-IN" in c for c in body["caveats"])
    assert client.get(EVALUATION, params={"forecast_method": "arima"}).status_code == 422
