"""
One solve of the two-stage stochastic sourcing model
(`app.optimization.stochastic.solve_stochastic_sourcing`).

The point of the model is a behavioural contrast, so that is what is tested: on the same
instance, lambda = 0 (minimise expected cost) and lambda = 1 (minimise CVaR_95) must pick
DIFFERENT plans, and the risk-averse one must not put every line on the single cheapest
distributor.

The fixture is described in `tests/_stochastic_fixture.py`: three equally risky
distributors, the cheapest able to carry the whole BOM alone, the other two half each.
"""
from __future__ import annotations

import pytest

from app.optimization.stochastic import solve_stochastic_sourcing
from app.optimization.strategies import get_strategy
from tests._stochastic_fixture import CHEAPEST, DEMAND, instance


def _solve(lam: float):
    bom, offers, scenarios = instance()
    return solve_stochastic_sourcing(
        bom, offers, get_strategy("balanced"), scenarios, lam=lam, time_limit_s=20.0,
    )


@pytest.fixture(scope="module")
def neutral():
    return _solve(0.0)


@pytest.fixture(scope="module")
def averse():
    return _solve(1.0)


def _lines_by_distributor(result):
    out: dict[int, set[int]] = {}
    for a in result.assignments:
        if a.quantity > 0:
            out.setdefault(a.distributor_id, set()).add(a.component_id)
    return out


def test_each_solve_returns_a_plan_that_covers_the_bom_and_its_cost_distribution(
    neutral, averse,
):
    for result in (neutral, averse):
        assert result.status == "OPTIMAL"
        per_line: dict[int, int] = {}
        for a in result.assignments:
            per_line[a.component_id] = per_line.get(a.component_id, 0) + a.quantity
        assert per_line == DEMAND
        # One realized cost per scenario atom, and the atoms carry the whole measure.
        assert len(result.outcomes) == 8
        assert sum(o.probability for o in result.outcomes) == pytest.approx(1.0)
        assert result.cvar_usd >= result.var_usd >= min(o.total_cost_usd for o in result.outcomes)
        assert result.cvar_usd >= result.expected_cost_usd


def test_risk_neutral_plan_puts_everything_on_the_cheapest_distributor(neutral):
    assert _lines_by_distributor(neutral) == {CHEAPEST: {10, 20}}


def test_risk_neutral_plan_has_the_lowest_expected_cost(neutral, averse):
    assert neutral.expected_cost_usd < averse.expected_cost_usd


def test_risk_averse_plan_does_not_concentrate_on_the_cheapest_distributor(averse):
    by_dist = _lines_by_distributor(averse)
    assert by_dist != {CHEAPEST: {10, 20}}
    assert len(by_dist) >= 2, f"lambda = 1 still single-sources: {by_dist}"


def test_risk_aversion_buys_a_lower_tail(neutral, averse):
    assert averse.cvar_usd < neutral.cvar_usd
