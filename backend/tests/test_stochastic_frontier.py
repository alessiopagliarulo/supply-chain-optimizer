"""
A lambda sweep of the sourcing model (`app.optimization.stochastic.compute_frontier`) on
the three-distributor fixture in `tests/_stochastic_fixture.py`.

A frontier is only a frontier if moving the risk weight trades one axis for the other:
as lambda rises, expected cost may only go up and CVaR_95 may only come down. Every
point must also be a plan that can actually be placed - each BOM line fully covered from
stock that exists.
"""
from __future__ import annotations

import pytest

from app.optimization.stochastic import compute_frontier
from app.optimization.strategies import get_strategy
from tests._stochastic_fixture import DEMAND, STOCK, instance

LAMBDAS = [0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0]

# Cents. The plans are chosen on integer-quantized probability weights and scored on the
# exact probabilities, so two optimal neighbours may differ by rounding, never by more.
TOL_USD = 0.01


@pytest.fixture(scope="module")
def sweep():
    bom, offers, scenarios = instance()
    return compute_frontier(
        bom, offers, get_strategy("balanced"), scenarios, LAMBDAS, time_limit_s=20.0,
    )


def test_one_point_per_lambda_in_sweep_order(sweep):
    points, results = sweep
    assert sorted(p.lam for p in points) == LAMBDAS
    assert len(results) == len(LAMBDAS)


def test_expected_cost_never_falls_as_risk_aversion_rises(sweep):
    points, _ = sweep
    by_lam = sorted(points, key=lambda p: p.lam)
    for lo, hi in zip(by_lam, by_lam[1:]):
        assert hi.expected_cost_usd >= lo.expected_cost_usd - TOL_USD, (lo.lam, hi.lam)


def test_cvar_never_rises_as_risk_aversion_rises(sweep):
    points, _ = sweep
    by_lam = sorted(points, key=lambda p: p.lam)
    for lo, hi in zip(by_lam, by_lam[1:]):
        assert hi.cvar_usd <= lo.cvar_usd + TOL_USD, (lo.lam, hi.lam)


def test_the_sweep_actually_trades(sweep):
    points, _ = sweep
    by_lam = sorted(points, key=lambda p: p.lam)
    assert by_lam[-1].cvar_usd < by_lam[0].cvar_usd
    assert by_lam[-1].expected_cost_usd > by_lam[0].expected_cost_usd


def test_every_point_is_a_feasible_plan(sweep):
    points, results = sweep
    for point, result in zip(sorted(points, key=lambda p: p.lam),
                             sorted(results, key=lambda r: r.lam)):
        assert point.status in ("OPTIMAL", "FEASIBLE")
        assert not point.dominated
        covered: dict[int, int] = {}
        for a in result.assignments:
            assert 0 < a.quantity <= STOCK[a.distributor_id]
            covered[a.component_id] = covered.get(a.component_id, 0) + a.quantity
        assert covered == DEMAND
        assert sorted({a.distributor_id for a in result.assignments}) == point.supplier_ids
        assert point.cvar_usd >= point.var_usd
        assert point.cvar_usd >= point.expected_cost_usd
