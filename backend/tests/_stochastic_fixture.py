"""
The three-distributor, two-line sourcing instance shared by the solve and frontier tests.

Three distributors, two BOM lines of 1,000 units each, all at the same distance from the
factory, each failing independently with probability 0.10 over the horizon.

    distributor   price (line 10 / line 20)   stock per line
    1  cheapest   $1.00 / $2.00               1,000  - can carry the whole BOM alone
    2             $1.02 / $2.04                 500
    3             $1.04 / $2.08                 500

Buying everything from distributor 1 is cheapest on average, but it fails in 10% of
outcomes, which covers the whole worst 5%. Then the survivors hold only 500 + 500 units
per line, so the gap is bought back at an expedite premium. Splitting across 2 and 3
costs more up front and loses at most half the BOM to any one failure.

The scenario set is the full 2**3 = 8-atom support, probabilities written out by hand:
none fail 0.9**3 = 0.729; one fails 0.1 * 0.9**2 = 0.081; two fail 0.1**2 * 0.9 = 0.009;
all three fail 0.1**3 = 0.001.
"""
from __future__ import annotations

from app.optimization.sourcing import BomLine, Offer
from app.optimization.stochastic import DisruptionScenario, ScenarioSet

CHEAPEST = 1
DEMAND = {10: 1_000, 20: 1_000}
STOCK = {1: 1_000, 2: 500, 3: 500}
_PRICE = {1: 1.00, 2: 1.02, 3: 1.04}


def instance() -> tuple[list[BomLine], list[Offer], ScenarioSet]:
    bom = [
        BomLine(component_id=cid, mpn=f"PART-{cid}", quantity=qty)
        for cid, qty in DEMAND.items()
    ]
    offers = [
        Offer(
            component_id=cid,
            distributor_id=did,
            distributor_name=f"Distributor {did}",
            price_usd=_PRICE[did] * (1 if cid == 10 else 2),
            stock=STOCK[did],
            moq=1,
            is_domestic=True,
            dist_km_from_depot=800.0,
        )
        for cid in DEMAND
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
