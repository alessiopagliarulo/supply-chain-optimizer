"""
Per-distributor disruption-probability calibration.

The single probability model the resilience/graph code uses. Relocated from the
archived two-stage stochastic sourcing program (tag `archive/sourcing-v1`, formerly
`app/optimization/stochastic.py`) when the sourcing optimizer was removed; the
calibration itself is unchanged.
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence


# ── Disruption-probability calibration ───────────────────────────────────────
#
# CITED BASE RATE.
# McKinsey Global Institute, "Risk, resilience, and rebalancing in global value
# chains" (August 2020), verified 2026-08-15 at
# https://www.mckinsey.com/capabilities/operations/our-insights/risk-resilience-and-rebalancing-in-global-value-chains
# Verbatim: "companies can now expect supply chain disruptions lasting a month or
# longer to occur every 3.7 years". Treated as a Poisson rate, giving the ANNUAL
# probability of at least one material, month-plus outage:
#     lambda = 1 / 3.7 per year  ->  P(>=1 event in a year) = 1 - exp(-1/3.7) = 0.2368
#
# That figure is firm-level ("a company"), not per-supplier, and this module says so
# rather than silently reinterpreting it. It is used here as the annual probability
# that a GIVEN distributor in this network suffers a material outage. That is an
# assumption, it is almost certainly too high for a single supplier, and it is exactly
# why `base_annual_prob` is a first-class parameter rather than a constant presented
# as if it were measured.
MCKINSEY_2020_YEARS_BETWEEN_DISRUPTIONS = 3.7
DEFAULT_BASE_ANNUAL_PROB = 1.0 - math.exp(-1.0 / MCKINSEY_2020_YEARS_BETWEEN_DISRUPTIONS)

# Sourcing horizon: the exposure window for one purchase order. 60 days is the
# order-to-dock window this catalogue's lead times imply (observed DigiKey lead times
# in seeds/data/lead_time_panel median 12 weeks for constrained parts, but the
# BOM-level plan here is a stocked-part buy).
DEFAULT_HORIZON_DAYS = 60

# Centrality only RANK-ORDERS relative risk; it never sets the level. The most
# central supplier gets `spread` x the base rate, the least central gets 1/spread x,
# the median supplier gets exactly the base rate. spread = 1.0 disables centrality
# entirely (homogeneous base rate).
DEFAULT_CENTRALITY_SPREAD = 3.0

# Hard ceiling: no supplier is modelled as failing more than half the time over a
# 60-day window. Guards against a badly-chosen base rate x spread combination
# reproducing the p=1.0 pathology this module exists to fix.
MAX_FAILURE_PROB = 0.5


# ── Probability calibration ──────────────────────────────────────────────────

def annual_to_horizon_prob(p_annual: float, horizon_days: int) -> float:
    """
    Convert an annual disruption probability to the probability of at least one
    disruption inside a `horizon_days` exposure window, assuming a constant hazard:

        p_h = 1 - (1 - p_annual) ** (horizon_days / 365)

    This step is not cosmetic. A 23.7%/year rate is 4.4% over a 60-day PO window;
    applying the annual number directly to a single order would overstate tail risk
    by ~5x. The existing simulator has no horizon concept at all.
    """
    if not 0.0 <= p_annual < 1.0:
        raise ValueError(f"p_annual must be in [0, 1), got {p_annual}")
    if horizon_days <= 0:
        raise ValueError(f"horizon_days must be positive, got {horizon_days}")
    return 1.0 - (1.0 - p_annual) ** (horizon_days / 365.0)


def build_failure_probabilities(
    distributor_ids: Sequence[int],
    betweenness: Optional[Dict[int, float]] = None,
    base_annual_prob: float = DEFAULT_BASE_ANNUAL_PROB,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    centrality_spread: float = DEFAULT_CENTRALITY_SPREAD,
    max_prob: float = MAX_FAILURE_PROB,
) -> Dict[int, float]:
    """
    Per-distributor disruption probability over the sourcing horizon.

    THE PROBLEM THIS FIXES
    ----------------------
    `graph/simulation.py:155-161` does:

        failure_probs = {did: min(betweenness.get(did, 0.0) * stress_factor, 1.0) ...}

    where `betweenness` is MIN-MAX NORMALIZED to [0,1] in `graph/builder.py:126-132`.
    A min-max normalization always attains 1.0 at its maximum, so *by construction*
    the single most central distributor in the network fails in every scenario and
    the least central one never fails. In this database that is literally true:
    max(betweenness) = 1.0, median = 0.0053. There is no base rate, no time horizon,
    and no unit anywhere in that expression -- a centrality rank is being read as a
    probability. Downstream, `cvar_95` therefore pins at 1.0 + EMERGENCY_COST_PREMIUM
    = 1.15 in nearly every benchmark row: a constant wearing a Monte Carlo costume.

    THE FIX
    -------
        1. LEVEL comes from a cited base rate, converted to the exposure window:
               p_base = annual_to_horizon_prob(base_annual_prob, horizon_days)
        2. SHAPE comes from centrality, but only as a bounded RANK transform:
               m_d    = spread ** (2 * u_d - 1),   u_d = percentile rank of
                                                   betweenness_d in [0, 1]
               p_d    = min(p_base * m_d, max_prob)
           The most central supplier is `spread` times the base rate, the least
           central `1/spread` times, the median supplier exactly the base rate. The
           multiplier's geometric mean is 1, so the cohort's typical rate stays at
           the cited figure.

    WHY A RANK TRANSFORM. Raw betweenness in this network is pathologically skewed
    (max 1.0, mean 0.050, median 0.0053 across 92 distributors, 18 of them exactly
    0). Multiplying a base rate by that raw score would hand the hub a 20x multiplier
    and reintroduce the same failure. A rank transform keeps the ORDERING that the
    graph analysis genuinely earns while refusing to read a magnitude off it that the
    data does not support.

    WHAT THIS STILL ASSUMES, EXPLICITLY. That more central suppliers are more likely
    to be disrupted at all. No source in this repo establishes that, and the opposite
    is arguable (large hub distributors are typically better capitalised and more
    redundant than small ones). That is why `centrality_spread=1.0` -- centrality
    ignored entirely, every supplier on the flat base rate -- is a supported setting.

    Passing `betweenness=None` (or an all-equal map) yields the flat base rate for
    every distributor.
    """
    if centrality_spread < 1.0:
        raise ValueError(f"centrality_spread must be >= 1.0, got {centrality_spread}")

    p_base = annual_to_horizon_prob(base_annual_prob, horizon_days)
    dids = sorted(set(distributor_ids))
    if not dids:
        return {}

    if betweenness is None or centrality_spread == 1.0:
        return {did: min(p_base, max_prob) for did in dids}

    scores = [(betweenness.get(did, 0.0), did) for did in dids]
    scores.sort()
    n = len(scores)

    # Percentile rank in [0, 1] with ties sharing the mean rank of their run, so a
    # block of equal-betweenness suppliers (very common here -- 18 sit at exactly 0)
    # all receive the identical multiplier instead of an arbitrary ordering artefact.
    rank_of: Dict[int, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[j + 1][0] == scores[i][0]:
            j += 1
        mean_rank = (i + j) / 2.0
        u = mean_rank / (n - 1) if n > 1 else 0.5
        for k in range(i, j + 1):
            rank_of[scores[k][1]] = u
        i = j + 1

    probs: Dict[int, float] = {}
    for did in dids:
        multiplier = centrality_spread ** (2.0 * rank_of[did] - 1.0)
        probs[did] = min(p_base * multiplier, max_prob)
    return probs
