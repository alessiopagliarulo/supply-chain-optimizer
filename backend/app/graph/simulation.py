"""
Monte Carlo supply-disruption simulation.

Runs N=1,000 single-round percolation scenarios over the bipartite supply graph.
Each scenario independently fails distributors at their CALIBRATED disruption
probability (`GraphState.p_disruption` -- a cited annual base rate converted to the
sourcing exposure window and rank-shaped by centrality; see
`app/graph/builder.py::_build_disruption_probabilities`), then checks which BOM
components become unfulfillable. This is one-shot percolation, NOT an SIR/cascade
model -- there is no time dimension, no infection propagation between nodes, and no
recovery. Fixed seed=42 ensures reproducible output. N is a module constant -- never
controlled by user input.

HISTORY (2026-08 functionality audit). This module used to read `gs.betweenness`
directly as p_fail. Betweenness was min-max normalized in the builder, so the single
most central distributor had p_fail = exactly 1.0 -- it was modelled as down in 100%
of BASELINE scenarios. Forcing it to fail in a what-if therefore changed nothing, and
`/resilience/distributor-failure` returned zero impact for 91 of 92 distributors. Both
halves of that (the normalization and the missing base rate) are fixed.

SATURATION (2026-08-28). `cvar_95` is bounded above by `1 + EMERGENCY_COST_PREMIUM`
and pins there whenever the worst-5% tail is entirely total shortfalls. Under
`stress_factor=3.0` most benchmark plans sit on that ceiling, so CVaR-95 ties
between two plans that are in fact very differently exposed. `p_shortfall`,
`p_total_shortfall` and the existing `mean_cost_inflation` are means over ALL
scenarios rather than the tail and keep resolving past the ceiling; `cvar_95_saturated`
says when the ceiling has been hit. See `run_monte_carlo`'s SATURATION section.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Set

if TYPE_CHECKING:
    from app.graph import GraphState

# Fixed -- not user-configurable per T-02-03 (DoS mitigation)
N_SCENARIOS: int = 1000

# Reproducibility: all runs on the same DB produce identical numbers
DEFAULT_SEED: int = 42

# Multiplier applied to every calibrated disruption probability. 1.0 = the cited
# base-rate calibration as-is; >1.0 models a macro/geopolitical stress spike.
STRESS_FACTOR: float = 1.0

# 15% cost inflation per unfulfillable component
EMERGENCY_COST_PREMIUM: float = 0.15


@dataclass
class SimulationResult:
    """Output of a Monte Carlo cascade failure simulation run."""
    p10: float                  # 10th percentile fulfillment rate (worst scenarios)
    p50: float                  # Median fulfillment rate
    p90: float                  # 90th percentile fulfillment rate (best scenarios)
    cvar_95: float              # CVaR/Expected Shortfall: mean cost inflation of worst-5% scenarios
    #   BOUNDED: cvar_95 lies in [1.0, 1.0 + emergency_premium] and SATURATES at the
    #   upper end. See `cvar_95_ceiling` / `cvar_95_saturated` below and the
    #   "Saturation" section of run_monte_carlo's docstring before reporting it.
    n_scenarios: int            # Number of scenarios run (always N_SCENARIOS)
    seed: int                   # RNG seed used (always DEFAULT_SEED via API)
    mean_fulfillment: float = 1.0     # Expected fulfillment rate across all scenarios
    mean_cost_inflation: float = 1.0  # Expected emergency-procurement cost multiplier (>= 1.0)
    # ── Evidence that the simulation actually consumed this BOM ────────────────
    # A well-hedged BOM legitimately produces p10 = p50 = p90 = 1.0. Without the
    # fields below that is indistinguishable from a stub returning constants, which
    # is exactly how /graph/simulate came to look input-insensitive. These make the
    # structure the percolation ran over visible in the payload.
    n_bom_lines: int = 0              # BOM lines actually simulated
    n_unsupplied_lines: int = 0       # lines with NO supplier in the (restricted) pool
    n_single_source_lines: int = 0    # lines with exactly one supplier -> single point of failure
    n_suppliers_in_scope: int = 0     # distinct distributors that could fail for this BOM
    n_scenarios_with_shortfall: int = 0   # scenarios where >= 1 line was unfulfillable
    n_scenarios_total_shortfall: int = 0  # scenarios where EVERY line was unfulfillable
    worst_fulfillment: float = 1.0    # minimum fulfillment rate over all scenarios
    p_fail_min: float = 0.0           # smallest per-distributor failure probability used
    p_fail_median: float = 0.0        # median per-distributor failure probability used
    p_fail_max: float = 0.0           # largest per-distributor failure probability used
    n_forced_failures: int = 0        # distributors pinned to p = 1.0 by the caller
    # ── Measures that still move when cvar_95 is pinned at its ceiling ────────
    # cvar_95 is a mean over the worst-5% tail of a quantity that is itself bounded,
    # so on an n-line BOM it lives on the lattice {1 + (k/n)*premium : k = 0..n} and
    # tops out at 1 + premium. Once every scenario in the tail is a TOTAL shortfall
    # the statistic is pinned and two plans with very different exposure report the
    # identical number. The three fields below are computed from the same scenarios
    # and are NOT tail-truncated, so they keep resolving past that point. Publish at
    # least one of them wherever cvar_95 is published.
    p_shortfall: float = 0.0          # P(>= 1 line unfulfillable) = n_scenarios_with_shortfall / n
    p_total_shortfall: float = 0.0    # P(EVERY line unfulfillable) -- the event the cvar_95 tail is made of
    cvar_95_ceiling: float = 1.0      # structural max of cvar_95 for this run = 1.0 + emergency_premium
    cvar_95_saturated: bool = False   # cvar_95 is AT that ceiling: a tie here is NOT evidence of equal exposure


def _get_comp_to_dists(
    gs: "GraphState",
    bom_component_ids: List[int],
    allowed_distributor_ids: Optional[Set[int]] = None,
) -> Dict[int, Set[int]]:
    """
    For each BOM component cid, find all distributor IDs that have an edge to c_{cid}
    in the DiGraph (predecessors of the component node).

    allowed_distributor_ids: if provided, restrict each component's supplying set to
        this pool (intersection). Used to simulate a SELECTED sourcing plan, where a
        component sourced from only one chosen distributor becomes a genuine single
        point of failure even if other distributors exist in the full graph.

    Returns Dict[int, Set[int]]: component_id -> set of distributor_ids supplying it.
    """
    comp_to_dists: Dict[int, Set[int]] = {}
    graph = gs.graph
    for cid in bom_component_ids:
        node_name = f"c_{cid}"
        dist_ids: Set[int] = set()
        if graph.has_node(node_name):
            for pred in graph.predecessors(node_name):
                # Predecessor nodes are distributor nodes named "d_{did}"
                if pred.startswith("d_"):
                    try:
                        dist_ids.add(int(pred[2:]))
                    except ValueError:
                        pass
        if allowed_distributor_ids is not None:
            dist_ids &= allowed_distributor_ids
        comp_to_dists[cid] = dist_ids
    return comp_to_dists


def run_monte_carlo(
    gs: "GraphState",
    bom_component_ids: List[int],
    n_scenarios: int = N_SCENARIOS,
    seed: int = DEFAULT_SEED,
    stress_factor: float = STRESS_FACTOR,
    forced_failures: Optional[Set[int]] = None,
    allowed_distributor_ids: Optional[Set[int]] = None,
    emergency_premium: float = EMERGENCY_COST_PREMIUM,
) -> SimulationResult:
    """
    Run N=1,000 single-round percolation failure scenarios over the bipartite supply graph.

    Algorithm (per scenario):
      1. Sample distributor failures: each distributor fails with probability =
         min(gs.p_disruption[d] * stress_factor, 1.0), where p_disruption is the
         calibrated horizon disruption probability (NOT a centrality score)
      2. A component is unfulfillable if ALL its supplying distributors failed,
         or it has no suppliers in the graph.
      3. fulfillment_rate = n_fulfillable / n_bom
      4. cost_inflation = 1.0 + (n_unfulfillable / n_bom) * emergency_premium

    Output aggregation:
      - P10 = 10th percentile of fulfillment_rates (worst outcomes)
      - P50 = median fulfillment rate
      - P90 = 90th percentile (best outcomes)
      - CVaR_95 = mean cost_inflation of the worst-5% scenarios by fulfillment rate
        (this is Conditional VaR / Expected Shortfall, NOT Entropic VaR)
      - p_shortfall = fraction of ALL scenarios with >= 1 unfulfillable line
      - p_total_shortfall = fraction of ALL scenarios with EVERY line unfulfillable

    SATURATION -- read before publishing cvar_95 or comparing two plans on it.
      cost_inflation is a bounded, quantised quantity: on an n-line BOM it can only
      take the n+1 values {1 + (k/n)*emergency_premium : k = 0..n}. For the 4-line
      BOMs in the benchmark that is the 5-point lattice
      {1.0, 1.0375, 1.075, 1.1125, 1.15} at the default 15% premium. CVaR_95 averages
      the worst 5% of those, so it is bounded above by

          cvar_95_ceiling = 1.0 + emergency_premium

      and reaches that ceiling EXACTLY when every scenario in the 5% tail is a total
      shortfall -- equivalently, whenever p_total_shortfall >= 0.05. Past that point
      cvar_95 is pinned: two plans with very different exposure (P(total collapse)
      of 0.12 vs 1.00, say) report the identical number. A tie at the ceiling is a
      CEILING, not a measurement of equal risk, and must never be reported as one.
      This is the same defect class as the retired `cascade_risk_score`.

      `cvar_95_saturated` flags exactly that condition. When it is True, report
      `p_shortfall` / `p_total_shortfall` (or `mean_cost_inflation`, which is a mean
      over ALL scenarios rather than the tail) alongside it -- those are not
      tail-truncated and keep resolving where cvar_95 stops.

      Note what does NOT fix this: recomputing CVaR on the unfulfilled-line share
      instead of the cost multiplier. inflation = 1 + share*premium is an exact
      affine map of share, so CVaR(share) = (cvar_95 - 1) / premium carries
      identical information and merely moves the ceiling from 1+premium to 1.0. The
      saturation comes from truncating to the tail, not from the units.

    The n_scenarios parameter is not exposed to API callers (T-02-03 threat mitigation).
    API endpoint always passes N_SCENARIOS directly and ignores any user-supplied n value.

    Scenario controls (used by the resilience "what-if" endpoints):
      - stress_factor: scales every distributor's failure probability. >1.0 models a
        geopolitical/macro stress spike, 1.0 is the baseline.
      - forced_failures: set of distributor_ids that fail with probability 1.0 every
        scenario (e.g. simulating a named distributor outage).
      - allowed_distributor_ids: if provided, restricts the supplying pool for every
        component to this set of distributor_ids before failure sampling. Used to
        simulate resilience of a SELECTED sourcing plan rather than the full graph,
        so a component single-sourced from one chosen distributor is correctly
        treated as a single point of failure. Backward compatible: None (default)
        preserves prior full-graph behavior.
      - emergency_premium: cost-inflation multiplier applied per unfulfillable
        component share (replaces the module constant EMERGENCY_COST_PREMIUM as a
        tunable lever, e.g. for sensitivity/tornado analysis). Defaults to
        EMERGENCY_COST_PREMIUM, preserving prior behavior.
    """
    forced = forced_failures or set()

    # An empty BOM is not a simulable question -- there is nothing to fulfil, so every
    # percentile is vacuously 1.0. Returning that as if it were a result is precisely
    # what made `POST /graph/simulate []` indistinguishable from a real 5-part BOM.
    # Callers must reject an empty BOM before reaching here; if one slips through, say
    # so rather than manufacturing a confident-looking answer.
    if not bom_component_ids:
        raise ValueError(
            "bom_component_ids is empty: a fulfillment simulation over zero lines has "
            "no meaning. Supply at least one component id."
        )

    n_bom = len(bom_component_ids)

    # Build component -> supplying distributors mapping
    comp_to_dists = _get_comp_to_dists(gs, bom_component_ids, allowed_distributor_ids)

    # Build failure probability dict for each distributor that appears in the graph.
    #
    # gs.p_disruption is the calibrated per-distributor disruption probability over the
    # sourcing horizon (cited base rate -> exposure window -> bounded centrality rank
    # transform); see app/graph/builder.py::_build_disruption_probabilities and
    # app/graph/disruption.py. It replaces the min-max normalized betweenness that
    # used to be read directly as p_fail here -- which pinned the most central
    # distributor at p = 1.0 and made forcing its failure a no-op.
    #
    # Fallback: a GraphState built before this field existed (or a hand-built test
    # fixture) has an empty p_disruption. Rather than silently reverting to the broken
    # betweenness-as-probability behaviour, derive the probabilities on the spot from
    # the same calibration function.
    base_probs: Dict[int, float] = gs.p_disruption
    if not base_probs and gs.betweenness:
        from app.graph.disruption import build_failure_probabilities
        base_probs = build_failure_probabilities(sorted(gs.betweenness), gs.betweenness)

    all_dist_ids: Set[int] = set()
    for dist_ids in comp_to_dists.values():
        all_dist_ids.update(dist_ids)

    failure_probs: Dict[int, float] = {
        did: (
            1.0 if did in forced
            else min(base_probs.get(did, 0.0) * stress_factor, 1.0)
        )
        for did in all_dist_ids
    }

    # Isolated RNG -- does not affect any other module's random state
    rng = random.Random(seed)

    fulfillment_rates: List[float] = []
    cost_inflations: List[float] = []
    n_with_shortfall = 0
    n_total_shortfall = 0

    for _ in range(n_scenarios):
        # Step 1: determine which distributors fail this scenario
        failed_dists: Set[int] = {
            did
            for did, prob in failure_probs.items()
            if rng.random() < prob
        }

        # Step 2: count unfulfillable components
        n_unfulfillable = 0
        for cid in bom_component_ids:
            supplying_dists = comp_to_dists[cid]
            # Unfulfillable if no suppliers OR all suppliers failed
            if not supplying_dists or supplying_dists.issubset(failed_dists):
                n_unfulfillable += 1

        if n_unfulfillable:
            n_with_shortfall += 1
            # The event that fills the CVaR-95 tail and pins it at its ceiling.
            # Tracked separately because it keeps discriminating after cvar_95 has
            # saturated: cvar_95 == ceiling merely says this is >= 0.05.
            if n_unfulfillable == n_bom:
                n_total_shortfall += 1

        # Step 3: fulfillment rate
        n_fulfillable = n_bom - n_unfulfillable
        fulfillment_rate = n_fulfillable / n_bom

        # Step 4: cost inflation
        inflation = 1.0 + (n_unfulfillable / n_bom) * emergency_premium

        fulfillment_rates.append(fulfillment_rate)
        cost_inflations.append(inflation)

    # Sorted copy for percentiles. Do NOT sort fulfillment_rates in place --
    # it stays index-aligned with cost_inflations for the tail pairing below.
    sorted_rates = sorted(fulfillment_rates)

    # Percentile indices (clamped to valid range)
    def _percentile_idx(p: float) -> int:
        idx = int(p * n_scenarios)
        return max(0, min(idx, n_scenarios - 1))

    p10 = sorted_rates[_percentile_idx(0.10)]
    p50 = sorted_rates[_percentile_idx(0.50)]
    p90 = sorted_rates[_percentile_idx(0.90)]

    # CVaR (Expected Shortfall): pair each scenario's rate with ITS OWN inflation
    # (both lists still in scenario order), sort by rate, take worst 5%.
    paired = sorted(zip(fulfillment_rates, cost_inflations), key=lambda x: x[0])
    n_tail = max(1, int(0.05 * n_scenarios))
    worst_inflations = [inf for _, inf in paired[:n_tail]]
    cvar_95 = sum(worst_inflations) / len(worst_inflations)

    mean_fulfillment = sum(fulfillment_rates) / n_scenarios
    mean_cost_inflation = sum(cost_inflations) / n_scenarios

    prob_values = sorted(failure_probs.values())

    # cvar_95 is a mean over the worst-5% tail of a quantity bounded by
    # 1 + emergency_premium, so this is its structural maximum. Flag when the
    # statistic is sitting on it -- at that point it has stopped measuring and a
    # tie between two plans carries no information about their relative exposure.
    ceiling = 1.0 + emergency_premium
    saturated = cvar_95 >= ceiling - 1e-9

    return SimulationResult(
        p10=p10,
        p50=p50,
        p90=p90,
        cvar_95=cvar_95,
        n_scenarios=n_scenarios,
        seed=seed,
        mean_fulfillment=mean_fulfillment,
        mean_cost_inflation=mean_cost_inflation,
        n_bom_lines=n_bom,
        n_unsupplied_lines=sum(1 for d in comp_to_dists.values() if not d),
        n_single_source_lines=sum(1 for d in comp_to_dists.values() if len(d) == 1),
        n_suppliers_in_scope=len(all_dist_ids),
        n_scenarios_with_shortfall=n_with_shortfall,
        n_scenarios_total_shortfall=n_total_shortfall,
        p_shortfall=n_with_shortfall / n_scenarios,
        p_total_shortfall=n_total_shortfall / n_scenarios,
        cvar_95_ceiling=ceiling,
        cvar_95_saturated=saturated,
        worst_fulfillment=sorted_rates[0],
        p_fail_min=prob_values[0] if prob_values else 0.0,
        p_fail_median=prob_values[len(prob_values) // 2] if prob_values else 0.0,
        p_fail_max=prob_values[-1] if prob_values else 0.0,
        n_forced_failures=len(forced & all_dist_ids),
    )
