"""
One solve of the two-stage stochastic sourcing model
(`app.optimization.stochastic.solve_stochastic_sourcing`).

The point of the model is a behavioural contrast, so that is what is tested: on the same
instance, lambda = 0 (minimise expected cost) and lambda = 1 (minimise CVaR_95) must pick
DIFFERENT plans, and the risk-averse one must not put every line on the single cheapest
distributor.

THE FIXTURE, AND WHY IT HAS A TRADE-OFF
---------------------------------------
Three distributors, two BOM lines of 1,000 units each, all at the same distance from the
factory, each failing independently with probability 0.10 over the horizon.

    distributor   price (line 10 / line 20)   stock per line
    1  cheapest   $1.00 / $2.00               1,000  - can carry the whole BOM alone
    2             $1.02 / $2.04                 500
    3             $1.04 / $2.08                 500

Buying everything from distributor 1 is cheapest on average, but it fails in 10% of
outcomes, which covers the whole worst 5%. Then the survivors hold only 500 + 500 units per line, so
the gap is bought back at an expedite premium. Splitting across 2 and 3 costs more up
front and loses at most half the BOM to any one failure.

The scenario set is the full 2**3 = 8-atom support, probabilities written out by hand:
none fail 0.9**3 = 0.729; one fails 0.1 * 0.9**2 = 0.081; two fail 0.1**2 * 0.9 = 0.009;
all three fail 0.1**3 = 0.001.
"""
from __future__ import annotations

import pytest

from app.optimization.sourcing import BomLine, Offer
from app.optimization.stochastic import (
    DisruptionScenario,
    ScenarioSet,
    solve_stochastic_sourcing,
)
from app.optimization.strategies import get_strategy

CHEAPEST = 1

_PRICE = {1: 1.00, 2: 1.02, 3: 1.04}
_STOCK = {1: 1_000, 2: 500, 3: 500}


def _instance():
    bom = [
        BomLine(component_id=10, mpn="PART-10", quantity=1_000),
        BomLine(component_id=20, mpn="PART-20", quantity=1_000),
    ]
    offers = [
        Offer(
            component_id=cid,
            distributor_id=did,
            distributor_name=f"Distributor {did}",
            price_usd=_PRICE[did] * (1 if cid == 10 else 2),
            stock=_STOCK[did],
            moq=1,
            is_domestic=True,
            dist_km_from_depot=800.0,
        )
        for cid in (10, 20)
        for did in (1, 2, 3)
    ]
    atoms = [
        (frozenset(), 0.729),
        (frozenset({1}), 0.081), (frozenset({2}), 0.081), (frozenset({3}), 0.081),
        (frozenset({1, 2}), 0.009), (frozenset({1, 3}), 0.009), (frozenset({2, 3}), 0.009),
        (frozenset({1, 2, 3}), 0.001),
    ]
    scenarios = ScenarioSet(
        scenarios=[DisruptionScenario(failed=f, probability=p) for f, p in atoms],
        n_draws=0,
        seed=-1,
        failure_probs={1: 0.1, 2: 0.1, 3: 0.1},
        kind="exact",
    )
    return bom, offers, scenarios


def _solve(lam: float):
    bom, offers, scenarios = _instance()
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
        assert per_line == {10: 1_000, 20: 1_000}
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
