"""
Two-stage stochastic sourcing program with a CVaR objective (SAA + Rockafellar-Uryasev).

WHAT THIS REPLACES
------------------
`sourcing.solve_sourcing` prices supply risk as a *deterministic surcharge*: a 15%
"stockout risk premium" (`_stockout_risk_premium_obj_units`) plus a betweenness-weighted
"expected recourse loss" (`_graph_surcharge_obj_units`). Both are closed-form guesses at
what a disruption would cost. Neither has a second-stage decision: nothing in the model
ever re-optimizes after a supplier goes dark.

This module writes that down properly instead.

    FIRST STAGE  (here-and-now, before uncertainty resolves)
        y[d]   in {0,1}   qualify/open distributor d
        x[c,d] in {0,1}   award BOM line c to distributor d
        q[c,d] in Z+      units of c committed to d

    UNCERTAINTY
        A scenario s is a set F_s of distributors that cannot deliver over the
        sourcing horizon. Drawn by independent Bernoulli trials, exactly the
        percolation structure `graph.simulation.run_monte_carlo` already uses --
        but with CALIBRATED probabilities (see `build_failure_probabilities`).

    SECOND STAGE (recourse, after F_s is observed)
        r[c,d,s] in Z+    emergency units of c re-procured from surviving d
        u[c,s]   in Z+    units that cannot be re-procured at all (unmet demand)
        e[d,s]   in {0,1} an expedited consignment is raised on d in scenario s

    OBJECTIVE
        min  (1 - lambda) * E[cost]  +  lambda * CVaR_alpha[cost]

    CVaR is linearized with Rockafellar & Uryasev (2000):
        CVaR_alpha(Z) = min over eta of  eta + 1/(1-alpha) * E[(Z - eta)+]
    which introduces one free scalar `eta` and one non-negative `z_s` per scenario
    with z_s >= C_s - eta. Everything stays linear, so CP-SAT solves it exactly --
    no piecewise approximation, no quadratic term, no separate risk solver.

WHY CVaR AND NOT VARIANCE
-------------------------
Variance penalises upside as well as downside and is quadratic (CP-SAT cannot take
it). CVaR_95 is the mean cost of the worst 5% of scenarios: LP-representable, and it is
the number a procurement director actually asks for ("what does a bad quarter cost
me?"). It is also *coherent* -- monotone, subadditive, positively homogeneous and
translation invariant -- which VaR is not: Artzner, Delbaen, Eber & Heath (1999),
"Coherent Measures of Risk", Mathematical Finance 9(3):203-228. Subadditivity is the
practical one here: it means the model can never be gamed into looking safer by
splitting one BOM into two.

CVaR IS ALREADY A DISTRIBUTIONALLY ROBUST OBJECTIVE
---------------------------------------------------
Worth stating plainly, because it changes what this model claims. CVaR is not merely
one risk measure among many; it has an exact dual representation as a worst-case
expectation over an ambiguity set of measures:

    CVaR_alpha(Z) = sup { E_Q[Z] : Q << P,  dQ/dP <= 1/(1-alpha) }

i.e. the highest expected cost achievable by any probability measure Q that keeps the
same support as the assumed P but is allowed to re-weight it by up to a factor
1/(1-alpha). Rockafellar & Uryasev (2002), "Conditional value-at-risk for general loss
distributions", Journal of Banking & Finance 26(7):1443-1471.

So minimizing CVaR_95 is solving a distributionally robust optimization problem whose
ambiguity set is every scenario re-weighting bounded by a likelihood ratio of 20. That
matters *specifically because the disruption probabilities here are assumed rather than
measured*: the lambda > 0 end of the frontier is already hedged against getting those
probabilities wrong by up to 20x on any scenario. It does not excuse the assumption --
the sensitivity sweep still has to be run -- but it does mean the risk-averse end of
the frontier degrades gracefully under probability misspecification in a way the
risk-neutral end does not.

HONESTY: WHERE THE PROBABILITIES COME FROM
------------------------------------------
See `build_failure_probabilities`. The short version: the repo's existing simulator
uses min-max normalized betweenness centrality *directly* as a failure probability,
so the single most central distributor fails in 100% of scenarios and CVaR saturates
at a constant 1.15. A CVaR objective built on that would be meaningless. This module
does NOT reuse those probabilities. It anchors a base rate and uses centrality only
to rank-order relative risk inside a bounded, explicitly-swept spread.

WHAT IS AND IS NOT MODELLED (read before quoting any number)
------------------------------------------------------------
Modelled:  full-outage supplier disruption; emergency re-procurement from surviving
           suppliers at an expedite premium; expedited air consignment cost (fixed +
           per unit); residual-stock limits on emergency buys; unmet demand penalty;
           avoided cost of goods that were never delivered.
NOT modelled: partial capacity loss (outages are binary); correlated/common-cause
           failures (draws are independent across suppliers); disruption duration;
           the time/qualification cost of onboarding a NEW supplier mid-emergency;
           MOQ on emergency buys; price movement under stress; multi-period recovery.
Each omission is stated with its direction of bias in docs/CVAR_EFFICIENT_FRONTIER.md.

SAMPLING IS THE FALLBACK, NOT THE METHOD
----------------------------------------
"SAA" in the title is now only half the story, and the smaller half. Disruption here is
|D| independent Bernoulli variables, so the cost distribution has AT MOST 2**|D| atoms.
When |D| is small enough that the whole support fits the solver budget, this module
ENUMERATES it and both optimizes and scores on the true measure -- CP-SAT's integer
objective weights come from `round(p_s * W)` rather than from Monte Carlo draw counts
(see `quantize_probabilities`). There is then no sampling error in the answer and no
SAA optimality gap to bound.

Sampling remains for the pools too wide to enumerate -- and there it is genuinely SAA,
with `saa_optimality_gap` bounding what the sample size costs. The distinction matters
because it used to be invisible: results were SCORED on the enumerated support while
being CHOSEN on 200 draws of it, and published under a label that described only the
scoring.
"""
from __future__ import annotations

import itertools
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from app.optimization.constants import (
    AIR_FREIGHT_RATE_USD_PER_KG,
    AIR_FREIGHT_BASE_USD,
)
from app.optimization.sourcing import (
    AVG_KG_PER_UNIT,
    EMERGENCY_REPROCURE_PREMIUM,
    OBJ_SCALE,
    STOCKOUT_PENALTY_MULTIPLE,
    BomLine,
    Offer,
    SourcingAssignment,
    _freight_model_by_did,
    filter_price_outliers,
)
from app.optimization.strategies import StrategyWeights

logger = logging.getLogger(__name__)


# ── Solver outcomes, told apart ──────────────────────────────────────────────
#
# CP-SAT reports four terminal statuses and they mean genuinely different things.
# Collapsing them into one "infeasible" RuntimeError -- which this module used to do --
# blames the CALLER'S BOM for what is usually OUR solver budget running out:
#
#   OPTIMAL / FEASIBLE  a plan exists and we have it.
#   INFEASIBLE          CP-SAT PROVED no plan satisfies the constraints. That is a real
#                       statement about the BOM (demand exceeds total available stock,
#                       an MOQ exceeds a line's quantity, us_only emptied a line's pool).
#                       The caller can act on it, so it is a 4xx.
#   UNKNOWN             the time limit expired before ANY feasible solution was found.
#                       This says nothing whatsoever about feasibility -- a plan may
#                       well exist and usually does. It is a statement about OUR budget
#                       at this scenario count and lambda, so it is a 5xx (or a
#                       partial frontier), never a claim about the caller's input.
#   MODEL_INVALID       we built a malformed model. Our bug, never the caller's.
#
# All three subclass RuntimeError so existing `except RuntimeError` call sites keep
# working; call sites that care catch the specific type.

class StochasticSolveError(RuntimeError):
    """Base for every terminal CP-SAT outcome that is not a usable solution."""

    def __init__(
        self,
        message: str,
        *,
        status: str,
        lam: float,
        n_scenarios: int,
        n_draws: int,
        time_limit_s: float,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.lam = lam
        self.n_scenarios = n_scenarios
        self.n_draws = n_draws
        self.time_limit_s = time_limit_s


class ModelInfeasibleError(StochasticSolveError):
    """CP-SAT PROVED no feasible sourcing plan exists. A real statement about the BOM."""


class SolverBudgetExceededError(StochasticSolveError):
    """
    The time limit expired before any feasible solution was found (CP-SAT UNKNOWN).

    Explicitly NOT a feasibility claim. Reporting this as "no feasible sourcing plan
    exists for this BOM" is a false diagnosis of the caller's input.
    """


class ModelInvalidError(StochasticSolveError):
    """CP-SAT rejected the model as malformed. Always a bug on this side."""


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
# why `base_annual_prob` is a first-class parameter that the frontier script sweeps
# across 5%-40% instead of publishing one number as if it were measured.
MCKINSEY_2020_YEARS_BETWEEN_DISRUPTIONS = 3.7
DEFAULT_BASE_ANNUAL_PROB = 1.0 - math.exp(-1.0 / MCKINSEY_2020_YEARS_BETWEEN_DISRUPTIONS)

# Sourcing horizon: the exposure window for one purchase order. 60 days is the
# order-to-dock window this catalogue's lead times imply (observed DigiKey lead times
# in seeds/data/lead_time_panel median 12 weeks for constrained parts, but the
# BOM-level plan here is a stocked-part buy). Swept in the frontier script.
DEFAULT_HORIZON_DAYS = 60

# Centrality only RANK-ORDERS relative risk; it never sets the level. The most
# central supplier gets `spread` x the base rate, the least central gets 1/spread x,
# the median supplier gets exactly the base rate. spread = 1.0 disables centrality
# entirely (homogeneous base rate) and is one of the sensitivity arms.
DEFAULT_CENTRALITY_SPREAD = 3.0

# Hard ceiling: no supplier is modelled as failing more than half the time over a
# 60-day window. Guards against a badly-chosen base rate x spread combination
# reproducing the p=1.0 pathology this module exists to fix.
MAX_FAILURE_PROB = 0.5

# ── SAA / CVaR settings ──────────────────────────────────────────────────────
DEFAULT_ALPHA = 0.95          # CVaR tail level (worst 5%)

# CVaR is also reported at these levels. A single alpha is not enough to read a tail:
# when one scenario atom carries more than (1 - alpha) of the probability mass, the
# whole alpha-tail sits inside that one outcome and CVaR_alpha degenerates to VaR_alpha.
# Publishing 0.80/0.90/0.95/0.98 together makes that visible instead of hiding it.
REPORTED_ALPHAS = (0.80, 0.90, 0.95, 0.98)
DEFAULT_N_DRAWS = 200         # Monte Carlo draws for the SAA scenario set
DEFAULT_SEED = 42             # matches graph.simulation.DEFAULT_SEED

# lambda is discretized to 1/LAMBDA_DEN so every objective coefficient stays integer.
LAMBDA_DEN = 100

# ── Integer weights for an EXACTLY ENUMERATED scenario set ───────────────────
#
# CP-SAT needs integer objective coefficients. For a SAMPLED set the draw counts supply
# them for free, and for a long time that was taken to mean the solver could only ever
# optimize on a sample -- which quietly meant the model was CHOSEN on 10 observed
# failure sets while being SCORED on all 64, so 54 atoms of the support carried weight
# zero in the decision and the alpha = 0.95 tail the optimizer actually saw was four
# atoms wide.
#
# Integer weights do not require sampling. `round(p_s * W)` for a common denominator W
# is an exact integer representation of the measure to a resolution of 1/W, and W is
# bounded only by the int64 objective ceiling (`MAX_OBJ_COEFF`), not by anything
# statistical. That is a QUANTIZATION with a stated, measured residual -- it is not
# sampling error, it does not have a confidence interval, and it does not shrink by
# drawing more.
#
# W is chosen per solve as the largest value the objective magnitude can carry (see
# `_affordable_weight_total`), capped here. Atoms whose probability rounds to zero
# carry no objective weight and are left out of the model entirely; the mass they
# represent is summed and reported as `solve_residual_mass` rather than assumed away.
EXACT_WEIGHT_TOTAL_CAP = 10 ** 9

# Below this resolution the quantized measure stops being a faithful description of the
# support and the honest move is to refuse rather than to publish a "exact support"
# claim the weights do not support.
MIN_EXACT_WEIGHT_TOTAL = 10 ** 3

# ── Recourse cost model ──────────────────────────────────────────────────────
# Emergency units are bought at a premium and flown, not trucked.
# EMERGENCY_REPROCURE_PREMIUM (0.15) is reused verbatim from sourcing.py so the
# stochastic model and the heuristic surcharge it replaces price expediting the same
# way -- the comparison between them is then about STRUCTURE, not about constants.
EXPEDITE_PER_UNIT_USD = AVG_KG_PER_UNIT * AIR_FREIGHT_RATE_USD_PER_KG
EXPEDITE_FIXED_USD = AIR_FREIGHT_BASE_USD

# Fraction of committed goods cost NOT paid when a supplier fails to deliver.
# 1.0 = you do not pay for goods you never received (the economically correct
# default for a purchase order). Lower it to model deposits//cancellation fees.
DEFAULT_RECOVERY_RATE = 1.0

# CP-SAT time limit for the stochastic model. The deterministic model uses 5s; this
# one is 50-200x larger, so it gets more. Every solve reports its status and MIP gap
# rather than silently returning a truncated answer.
DEFAULT_TIME_LIMIT_S = 60.0

# OPTIONAL DETERMINISTIC BUDGET. `None` by default, everywhere -- every served endpoint
# and every existing caller keeps exactly the wall-clock behaviour above.
#
# WHY IT EXISTS. `max_time_in_seconds` is a WALL-CLOCK budget, so what CP-SAT achieves
# inside it is a property of the machine and its CPU load, not of the model. That is
# measured, not theoretical: two regenerations of docs/cvar_frontier.json from the same
# commit and the same hashed inputs disagreed on the converged count, on the MIP-gap
# percentiles and on which BOMs appeared in the published table, while not one cost,
# plan or CVaR value moved (see OUTSTANDING_WORK items 45 and 52).
#
# `max_deterministic_time` is a WORK budget: CP-SAT accumulates its own deterministic
# measure of the search performed and stops at the same point however fast the machine
# got there. Combined with `num_search_workers = 1` -- already required below -- the
# whole solve becomes reproducible: status, bound, gap, objective and plan.
#
# IT DOES NOT MAKE HARD INSTANCES CONVERGE. A solve that stops truncated still stops
# truncated. It stops in the SAME PLACE every time, which is the property a published
# artifact needs and a wall clock cannot give it.
#
# `max_time_in_seconds` stays applied alongside it as a runaway guard. Whichever binds
# first stops the solve, so a caller that needs the determinism guarantee must check
# that the clock did NOT bind -- compare the returned `wall_seconds` against
# `time_limit_s`. If the clock bound, this guarantee did not hold for that solve.
DEFAULT_DETERMINISTIC_LIMIT: Optional[float] = None

# Stop branch-and-bound only when the bound is CLOSED, not when it is merely close.
#
# WHY 0.0 AND NOT 0.1%. A relative gap limit is a licence to return an incumbent whose
# objective may be that much worse than the unknown optimum -- and CP-SAT still labels
# that return OPTIMAL. On the published frontier 0.1% of the objective is $111-183 per
# point, while ADJACENT frontier points are only $177-264 apart: the solver tolerance
# was the same order as the resolution of the curve it was drawing.
#
# The observable damage was non-reproducibility. Three runs of identical code on
# identical data returned three different frontiers -- lambda = 0.5 landing on 4
# suppliers in one run and 5 in another, every point reporting OPTIMAL -- and the
# headline "CVaR removed per dollar spent beyond the knee" read 0.409 in one place and
# 0.342 in another. A frontier that does not reproduce is not a frontier.
#
# There is no compute argument for the tolerance: these solves finish in 0.01-0.03 s.
# `time_limit_s` remains the only thing that can truncate a solve, and a truncated
# solve returns FEASIBLE with its achieved gap rather than OPTIMAL, so "OPTIMAL" now
# means proved.
DEFAULT_RELATIVE_GAP = 0.0

# Objective-coefficient safety ceiling. Beyond this, int64 overflow inside CP-SAT
# stops being theoretical. Checked, not assumed.
MAX_OBJ_COEFF = 4 * 10**17


def _usd_to_units(usd: float) -> int:
    """USD -> the MILLI-CENT integer objective units sourcing.py's MILP uses."""
    return int(round(usd * OBJ_SCALE))


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
    ignored entirely, every supplier on the flat base rate -- is a supported setting
    and is run as a sensitivity arm in every published frontier.

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


# ── Scenario sampling ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DisruptionScenario:
    """
    One realized state of the world: which distributors cannot deliver.

    `count` is the number of raw Monte Carlo draws that produced this exact set; it is
    zero for an enumerated atom, which was never drawn. `probability` is the mass this
    atom actually carries: count/n_draws for a sampled set, and the EXACT product of
    Bernoulli terms for an enumerated one.

    `probability` is the field everything downstream uses. Reporting weights by it, and
    so does the CP-SAT objective -- an enumerated set's exact probabilities are turned
    into integer objective weights by `quantize_probabilities`, so the optimizer no
    longer needs a sample to obtain integer coefficients. See `EXACT_WEIGHT_TOTAL_CAP`.
    """
    failed: FrozenSet[int]
    count: int = 0
    probability: float = 0.0


@dataclass
class ScenarioSet:
    """
    A sample-average-approximation scenario set.

    `scenarios` is DEDUPLICATED: identical failure sets are collapsed and carry an
    integer `count`. With a realistic base rate most draws are the no-disruption set,
    so deduplication typically cuts the model size 2-4x with ZERO change to the
    empirical distribution -- the weights are exactly the draw counts. `n_draws` is
    the honest sample size; `len(scenarios)` is the model size. Both are reported.
    """
    scenarios: List[DisruptionScenario]
    n_draws: int
    seed: int
    failure_probs: Dict[int, float] = field(default_factory=dict)
    kind: str = "saa"          # "saa" (sampled) or "exact" (fully enumerated)
    residual_mass: float = 0.0  # probability NOT represented (exact sets only)

    @property
    def n_distinct(self) -> int:
        return len(self.scenarios)

    @property
    def probabilities(self) -> List[float]:
        return [s.probability for s in self.scenarios]

    @property
    def p_no_disruption(self) -> float:
        for s in self.scenarios:
            if not s.failed:
                return s.probability
        return 0.0

    @property
    def mean_failures_per_scenario(self) -> float:
        return sum(len(s.failed) * s.probability for s in self.scenarios)

    @property
    def max_atom_probability(self) -> float:
        """
        Mass of the single largest atom. Decisive for reading a CVaR: when one atom
        carries more than (1 - alpha), the entire alpha-tail sits inside that one
        outcome, CVaR_alpha collapses onto VaR_alpha, and the number stops telling you
        anything about the shape of the tail beyond it.
        """
        return max((s.probability for s in self.scenarios), default=0.0)

    def support_size(self) -> int:
        """Number of atoms the underlying distribution can have at all: 2**|D|."""
        return 2 ** len(self.failure_probs)

    def __post_init__(self) -> None:
        """
        Derive `probability` from draw counts when a caller supplied only counts.

        Hand-built scenario sets (tests, fixtures, the "nothing ever fails" baseline)
        naturally specify counts and leave probability at its default. Silently treating
        those as zero-probability atoms would divide by zero -- or worse, quietly weight
        every scenario at nothing. Normalizing here keeps a count-only set valid.
        """
        if not self.scenarios:
            return
        if any(sc.probability > 0.0 for sc in self.scenarios):
            return
        total = sum(sc.count for sc in self.scenarios)
        if total <= 0:
            raise ValueError(
                "ScenarioSet has neither probabilities nor positive draw counts"
            )
        self.scenarios = [
            DisruptionScenario(failed=sc.failed, count=sc.count,
                               probability=sc.count / total)
            for sc in self.scenarios
        ]


def sample_scenarios(
    failure_probs: Dict[int, float],
    n_draws: int = DEFAULT_N_DRAWS,
    seed: int = DEFAULT_SEED,
) -> ScenarioSet:
    """
    Draw `n_draws` independent Bernoulli disruption scenarios and deduplicate them.

    Same percolation structure as `graph.simulation.run_monte_carlo` (independent
    per-distributor Bernoulli trials, isolated `random.Random(seed)` so no global RNG
    state is touched), but the scenarios are RETAINED rather than immediately reduced
    to two aggregate floats -- a two-stage program needs the realizations themselves.

    Independence across suppliers is an assumption and a known understatement of tail
    risk: real disruptions are correlated (one typhoon takes out several Shenzhen
    warehouses). Stated, not hidden.
    """
    rng = random.Random(seed)
    dids = sorted(failure_probs)
    counts: Dict[FrozenSet[int], int] = {}
    for _ in range(n_draws):
        failed = frozenset(d for d in dids if rng.random() < failure_probs[d])
        counts[failed] = counts.get(failed, 0) + 1

    scenarios = [
        DisruptionScenario(failed=f, count=c, probability=c / n_draws)
        for f, c in sorted(counts.items(), key=lambda kv: (len(kv[0]), sorted(kv[0])))
    ]
    return ScenarioSet(
        scenarios=scenarios,
        n_draws=n_draws,
        seed=seed,
        failure_probs=dict(failure_probs),
        kind="saa",
    )


# Enumerating 2**|D| atoms is only sane for a small supplier pool. 2**18 = 262,144 is
# already more atoms than any solve here needs; beyond that, sampling is the only
# option and the SAA optimality gap (see `saa_optimality_gap`) is how its error is
# bounded instead.
MAX_ENUMERABLE_DISTRIBUTORS = 18


def enumerate_scenarios(failure_probs: Dict[int, float]) -> ScenarioSet:
    """
    Enumerate the FULL support exactly: all 2**|D| failure sets with exact probabilities.

    WHY THIS MATTERS MORE THAN A BIGGER SAMPLE
    ------------------------------------------
    Disruption here is |D| independent Bernoulli variables, so the cost distribution has
    at most 2**|D| atoms -- full stop. For this catalogue's smaller BOMs that is a tiny
    number: `pcb_power_supply` is supplied by six distributors, so its entire support is
    64 atoms, of which 29 carry probability >= 1e-4 and the top 20 carry 99.7% of the
    mass. Drawing 200 Monte Carlo samples from that recovers only ~10 atoms and adds no
    information the enumeration does not already have exactly.

    A reviewer looking at "n_draws = 200, n_distinct = 10, alpha = 0.95" is right to
    object that 0.05 x 10 = 0.5 atoms lands in the tail, so the tail estimate is one
    scenario wide. The answer is not more sampling -- it is to stop sampling. With the
    support enumerated, E and CVaR are computed on the true measure and carry NO
    sampling error at all.

    What that does NOT fix, and what is reported alongside: when a single atom carries
    more than (1 - alpha) of the mass, CVaR_alpha is determined by that one outcome. See
    `ScenarioSet.max_atom_probability`.

    Raises ValueError above `MAX_ENUMERABLE_DISTRIBUTORS`; use `sample_scenarios` plus
    `saa_optimality_gap` there.
    """
    dids = sorted(failure_probs)
    if len(dids) > MAX_ENUMERABLE_DISTRIBUTORS:
        raise ValueError(
            f"exact enumeration needs 2**{len(dids)} = {2 ** len(dids):,} atoms, above "
            f"the {2 ** MAX_ENUMERABLE_DISTRIBUTORS:,} ceiling. Use sample_scenarios() "
            "and bound the sampling error with saa_optimality_gap()."
        )

    scenarios: List[DisruptionScenario] = []
    for r in range(len(dids) + 1):
        for combo in itertools.combinations(dids, r):
            failed = frozenset(combo)
            prob = 1.0
            for did in dids:
                prob *= failure_probs[did] if did in failed else (1.0 - failure_probs[did])
            scenarios.append(DisruptionScenario(failed=failed, count=0, probability=prob))

    total = sum(sc.probability for sc in scenarios)
    return ScenarioSet(
        scenarios=scenarios,
        n_draws=0,
        seed=-1,
        failure_probs=dict(failure_probs),
        kind="exact",
        residual_mass=abs(1.0 - total),
    )


def quantize_probabilities(
    probabilities: Sequence[float], weight_total: int,
) -> Tuple[List[int], float]:
    """
    Turn an exact probability measure into integer CP-SAT objective weights.

    Returns `(weights, residual_mass)` where `weights[i] = round(p_i * weight_total)`
    and `residual_mass` is the total probability of the atoms that rounded to zero.

    This is the whole reason an exactly enumerated support can be OPTIMIZED on and not
    merely scored on. Rounding is deliberate and unbiased: the alternative of flooring
    every atom at weight 1 keeps the atom count intact but OVERWEIGHTS the extreme
    failure sets by an order of magnitude (on the published 6-supplier instance,
    2.8e-4 of spurious mass against a true 2.9e-5), and those are exactly the atoms
    the CVaR tail is made of. Dropping them instead understates the tail by the
    reported `residual_mass`, which is a number you can read.
    """
    weights = [int(round(p * weight_total)) for p in probabilities]
    residual = sum(p for p, w in zip(probabilities, weights, strict=True) if w <= 0)
    return weights, residual


def _affordable_weight_total(
    w_first: int,
    w_mean: int,
    w_cvar: int,
    f_ub_units: int,
    r_span_units: int,
    z_ub_units: int,
    tail_k: int,
) -> int:
    """
    Largest total scenario weight whose worst-case objective still clears MAX_OBJ_COEFF.

    Inverts the same magnitude guard `solve_stochastic_sourcing` applies afterwards, so
    the weight resolution is chosen TO satisfy the int64 ceiling rather than the ceiling
    being discovered to be violated. The guard still runs on the weights actually used;
    this only picks the largest resolution that will pass it.
    """
    per_unit_weight = (
        w_first * f_ub_units
        + w_mean * r_span_units
        + w_cvar * (r_span_units + tail_k * z_ub_units)
    )
    if per_unit_weight <= 0:
        return EXACT_WEIGHT_TOTAL_CAP
    return int(MAX_OBJ_COEFF // per_unit_weight)


# ── Sizing the SOLVE scenario set to the solver budget ───────────────────────
#
# THE PROBLEM THIS SOLVES, measured rather than assumed.
#
# The second stage builds, per scenario, one `u` per affected line, one `r` per
# surviving offer on an affected line, and one `e` per surviving distributor. So the
# model grows LINEARLY in the number of DISTINCT scenarios and in the size of the
# supplier pool -- and the number of distinct scenarios itself grows with the pool,
# because with |D| suppliers at ~5% failure each, nearly every draw is a different
# failure set once |D| is large.
#
# On a real 3-line BOM (component ids 37/137/30) the pool is 55 distributors, 200
# draws deduplicate to 183 distinct scenarios, and the model reaches ~29,000
# variables. Measured on that instance at lambda = 0.5, one worker:
#
#   n_draws  distinct  variables   5s limit           15s limit
#      20       19        3,096    OPTIMAL   1.3s     OPTIMAL   1.3s
#      30       29        4,715    OPTIMAL   0.7s     OPTIMAL   0.7s
#      60       59        9,424    OPTIMAL   2.6s     OPTIMAL   2.6s
#     100       96       15,227    FEASIBLE  gap 57%  FEASIBLE  gap 36%
#     200      183       28,937    UNKNOWN (no solution at all)  FEASIBLE gap 21%
#
# The scenario count is the dominant lever by a wide margin -- tripling the time limit
# does not rescue 200 draws, while cutting draws to 60 solves the same instance to
# proven optimality in 2.6 s and lands on the SAME plan (1 supplier, E = $2,065.95).
#
# THIS LADDER IS THE FALLBACK PATH. When the supplier pool is small enough to enumerate,
# `fit_scenario_set` hands the solver the COMPLETE support instead and none of what
# follows applies -- there is no sample to thin and no SAA error to trade. Everything
# below is about the wide-pool instances where enumeration is not an option.
#
# WHY THINNING IS NOT A LOSS OF RIGOUR. The plan is CHOSEN on the solve set but SCORED
# on `evaluation_set` (see `solve_stochastic_sourcing`). Thinning the solve set trades
# SAA choice error -- which `saa_optimality_gap` is built to bound -- for the ability
# to return an answer at all. It does not touch the statistical quality of the
# published E and CVaR, which still come from the full set (or the exact enumerated
# support when the pool is small enough).
#
# And because `sample_scenarios` seeds an isolated `random.Random(seed)`, drawing n < N
# times yields exactly the first n draws of the N-draw sequence. The thinned set is a
# genuine sub-sample of the full one, not a differently-seeded second experiment.

# Second-stage variable budget for one solve. Set from the table above: 9,424
# variables solve to proven optimality inside 3s on one worker; 15,227 do not.
DEFAULT_MAX_RECOURSE_VARS = 9_000

# Draw counts tried, in descending order. Every entry is a prefix of the next one up.
SCENARIO_DRAW_LADDER: Tuple[int, ...] = (200, 150, 100, 75, 60, 50, 40, 30, 25)


def count_recourse_variables(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    scenario_set: ScenarioSet,
    us_only: bool = False,
    expedite_fixed_usd: float = EXPEDITE_FIXED_USD,
) -> int:
    """
    How many second-stage variables `solve_stochastic_sourcing` would create.

    Mirrors the model-construction loop exactly (same `affected` / `survivors` /
    `cap == 0` filters) so the number is the real one, not a proxy. Cheap: no model is
    built and nothing is solved.
    """
    prep = _prepare(bom, offers, weights, us_only)
    return _count_recourse_variables(prep, scenario_set, expedite_fixed_usd)


def _count_recourse_variables(
    prep: "_Prepared", scenario_set: ScenarioSet, expedite_fixed_usd: float,
) -> int:
    total = 0
    for scen in scenario_set.scenarios:
        failed = scen.failed
        affected = [
            b for b in prep.bom
            if any(o.distributor_id in failed
                   for o in prep.offers_by_component[b.component_id])
        ]
        if not affected:
            continue
        if expedite_fixed_usd > 0.0:
            total += len({
                o.distributor_id
                for b in affected
                for o in prep.offers_by_component[b.component_id]
                if o.distributor_id not in failed
            })
        for b in affected:
            total += 1  # u[c,s]
            total += sum(
                1 for o in prep.offers_by_component[b.component_id]
                if o.distributor_id not in failed
                and max(min(o.stock, b.quantity), 0) > 0
            )  # r[c,d,s]
    return total


@dataclass
class ScenarioBudgetFit:
    """The solve scenario set actually chosen, and an honest account of why."""
    scenario_set: ScenarioSet
    n_draws_requested: int
    n_draws_used: int
    n_distinct: int
    recourse_variables: int
    max_recourse_variables: int
    thinned: bool
    at_floor: bool  # still over budget at the smallest ladder rung
    exact: bool = False               # the solve set is the full enumerated support
    exact_rejected_reason: Optional[str] = None  # why it was not, when one was offered

    @property
    def kind(self) -> str:
        return self.scenario_set.kind

    @property
    def note(self) -> str:
        if self.exact:
            return (
                f"The plan is CHOSEN on the complete {self.n_distinct}-atom support "
                f"with exact probability weights, not on a sample of it "
                f"({self.recourse_variables:,} second-stage variables, inside the "
                f"{self.max_recourse_variables:,}-variable solve budget). Choice and "
                f"score are read from the same measure, so there is no sampling error "
                f"anywhere in this result and no SAA optimality gap to bound."
            )
        if not self.thinned:
            base = (
                f"{self.n_draws_used} draws deduplicated to {self.n_distinct} distinct "
                f"scenarios ({self.recourse_variables:,} second-stage variables), inside "
                f"the {self.max_recourse_variables:,}-variable solve budget."
            )
            if self.exact_rejected_reason:
                base += f" {self.exact_rejected_reason}"
            return base
        base = (
            f"Solve scenario set thinned from {self.n_draws_requested} to "
            f"{self.n_draws_used} draws ({self.n_distinct} distinct, "
            f"{self.recourse_variables:,} second-stage variables) to stay inside the "
            f"{self.max_recourse_variables:,}-variable solve budget. The PLAN is chosen "
            f"on this sub-sample; expected cost and CVaR are still scored on the full "
            f"evaluation set, so the published risk numbers keep their statistical "
            f"quality and only the SAA choice error grows."
        )
        if self.at_floor:
            base += (
                f" NOTE: even at the {self.n_draws_used}-draw floor this instance is "
                f"over budget, so the solve may still time out."
            )
        if self.exact_rejected_reason:
            base += f" {self.exact_rejected_reason}"
        return base


def fit_scenario_set(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    failure_probs: Dict[int, float],
    n_draws: int = DEFAULT_N_DRAWS,
    seed: int = DEFAULT_SEED,
    us_only: bool = False,
    max_recourse_vars: int = DEFAULT_MAX_RECOURSE_VARS,
    expedite_fixed_usd: float = EXPEDITE_FIXED_USD,
    exact_set: Optional[ScenarioSet] = None,
) -> ScenarioBudgetFit:
    """
    Pick the SOLVE scenario set: the exact support if it fits, else the largest draw
    count whose second-stage model fits the solve budget.

    EXACT FIRST, AND NOT AS AN OPTIMIZATION. When `exact_set` is supplied and its
    second stage fits the variable budget, it is the right solve set for a reason that
    has nothing to do with speed: it is the measure the answer is going to be SCORED
    on. Solving on a sample of a support you then score exactly means the optimizer
    optimizes one distribution and the page publishes another. On the published
    6-supplier instance that gap was measurable in the exact quantity the risk-neutral
    objective minimizes -- E[recourse] of $398.78 on 200 draws against $119.61 exact,
    a 3.3x error -- and the sampled solve resolved 10 of 64 atoms, leaving the
    alpha = 0.95 tail four atoms wide at every lambda against an exact 49-54.

    Falls back to the draw ladder when no exact set is offered or it does not fit. The
    ladder itself is unchanged: it returns the full `n_draws` set untouched whenever it
    already fits, and only engages on the large-pool instances that previously returned
    CP-SAT UNKNOWN and were reported to the user as "no feasible sourcing plan exists".
    """
    prep = _prepare(bom, offers, weights, us_only)

    exact_rejected: Optional[str] = None
    if exact_set is not None:
        if exact_set.kind != "exact":
            raise ValueError(
                f"exact_set must be an enumerated support, got kind={exact_set.kind!r}"
            )
        exact_vars = _count_recourse_variables(prep, exact_set, expedite_fixed_usd)
        if exact_vars <= max_recourse_vars:
            return ScenarioBudgetFit(
                scenario_set=exact_set,
                n_draws_requested=n_draws,
                n_draws_used=0,
                n_distinct=exact_set.n_distinct,
                recourse_variables=exact_vars,
                max_recourse_variables=max_recourse_vars,
                thinned=False,
                at_floor=False,
                exact=True,
            )
        exact_rejected = (
            f"The {exact_set.n_distinct}-atom exact support would need "
            f"{exact_vars:,} second-stage variables, over the "
            f"{max_recourse_vars:,}-variable solve budget, so the plan is chosen on a "
            f"sample of it and scored on all of it; the residual choice error is what "
            f"saa_optimality_gap bounds."
        )

    ladder = [n for n in SCENARIO_DRAW_LADDER if n <= n_draws] or [n_draws]
    if ladder[0] != n_draws:
        ladder = [n_draws, *ladder]

    chosen: Optional[ScenarioSet] = None
    chosen_vars = 0
    for candidate in ladder:
        scen = sample_scenarios(failure_probs, n_draws=candidate, seed=seed)
        n_vars = _count_recourse_variables(prep, scen, expedite_fixed_usd)
        chosen, chosen_vars = scen, n_vars
        if n_vars <= max_recourse_vars:
            break

    assert chosen is not None  # ladder is never empty
    return ScenarioBudgetFit(
        scenario_set=chosen,
        n_draws_requested=n_draws,
        n_draws_used=chosen.n_draws,
        n_distinct=chosen.n_distinct,
        recourse_variables=chosen_vars,
        max_recourse_variables=max_recourse_vars,
        thinned=chosen.n_draws < n_draws,
        at_floor=chosen_vars > max_recourse_vars,
        exact=False,
        exact_rejected_reason=exact_rejected,
    )


# ── Risk statistics on a weighted empirical distribution ─────────────────────

def weighted_var_cvar(
    values: Sequence[float],
    weights: Sequence[float],
    alpha: float = DEFAULT_ALPHA,
) -> Tuple[float, float]:
    """
    Exact VaR_alpha and CVaR_alpha of a weighted discrete distribution.

    Weights may be raw Monte Carlo counts or exact probabilities; only their ratios
    matter, since the mass is normalized by their sum.

    CVaR_alpha = E[Z | Z >= VaR_alpha], computed by accumulating exactly (1-alpha) of
    the total probability mass from the worst end and splitting the boundary atom
    fractionally. That fractional split is what makes this the true CVaR of the
    discrete measure rather than "mean of the worst ceil(k) samples" -- which is what
    `graph/simulation.py:210-213` does, and which is biased whenever the tail cut
    does not land on a sample boundary.
    """
    if len(values) != len(weights):
        raise ValueError("values and weights must be the same length")
    if not values:
        raise ValueError("cannot compute CVaR of an empty sample")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")

    total = float(sum(weights))
    tail_mass = (1.0 - alpha) * total
    pairs = sorted(zip(values, weights, strict=True), key=lambda t: -t[0])

    acc = 0.0
    accumulated = 0.0
    var = pairs[0][0]
    for value, weight in pairs:
        take = min(float(weight), tail_mass - acc)
        if take <= 0.0:
            break
        accumulated += value * take
        acc += take
        var = value
        if acc >= tail_mass - 1e-12:
            break
    return var, accumulated / tail_mass


@dataclass
class TailComposition:
    """
    How many distinct outcomes actually sit inside the alpha-tail.

    This is the diagnostic that decides whether a CVaR number means anything. CVaR_alpha
    averages the worst (1 - alpha) of the probability mass; if that mass is covered by a
    single atom, CVaR_alpha equals VaR_alpha and reports one scenario, however many
    Monte Carlo draws were taken. Resampling cannot fix that -- only a distribution with
    more distinct outcomes in the tail can, or a less extreme alpha.
    """
    alpha: float
    n_atoms_in_tail: int
    largest_tail_atom_share: float   # fraction of the tail mass in its biggest atom
    tail_mass: float
    degenerate: bool                 # one atom covers the whole tail


def tail_composition(
    values: Sequence[float],
    weights: Sequence[float],
    alpha: float = DEFAULT_ALPHA,
) -> TailComposition:
    """Count the distinct outcomes the alpha-tail is actually averaging over."""
    total = float(sum(weights))
    tail_mass = (1.0 - alpha) * total
    pairs = sorted(zip(values, weights, strict=True), key=lambda t: -t[0])

    acc = 0.0
    taken: List[float] = []
    for _value, weight in pairs:
        take = min(float(weight), tail_mass - acc)
        if take <= 0.0:
            break
        taken.append(take)
        acc += take
        if acc >= tail_mass - 1e-12:
            break

    largest = max(taken) / tail_mass if taken and tail_mass > 0 else 1.0
    return TailComposition(
        alpha=alpha,
        n_atoms_in_tail=len(taken),
        largest_tail_atom_share=largest,
        tail_mass=tail_mass / total if total else 0.0,
        degenerate=len(taken) <= 1,
    )


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    total = float(sum(weights))
    return sum(v * w for v, w in zip(values, weights, strict=True)) / total


# ── Result containers ────────────────────────────────────────────────────────

@dataclass
class ScenarioOutcome:
    """Realized cost of one scenario under a fixed first-stage plan."""
    failed: FrozenSet[int]
    count: int
    probability: float
    total_cost_usd: float
    recourse_cost_usd: float
    emergency_units: int
    unmet_units: int


@dataclass
class StochasticSourcingResult:
    """
    Output of one point on the efficient frontier.

    `expected_cost_usd` and `cvar_usd` are always recomputed POST HOC from the
    realized per-scenario costs, never read off the solver's `eta`/`z` variables.
    At lambda = 0 the CVaR block carries zero objective weight, so `eta` is free and
    reading it would produce a meaningless number.
    """
    lam: float
    alpha: float
    assignments: List[SourcingAssignment]
    selected_distributor_ids: List[int]
    first_stage_cost_usd: float
    expected_cost_usd: float
    var_usd: float
    cvar_usd: float
    expected_recourse_usd: float
    outcomes: List[ScenarioOutcome]
    cvar_by_alpha: Dict[float, float]
    max_atom_probability: float
    tail: TailComposition
    evaluation_kind: str
    status: str
    objective_units: float
    best_bound_units: float
    gap_pct: float
    wall_seconds: float
    evaluate_seconds: float
    n_variables: int
    n_scenarios_distinct: int
    n_draws: int
    # ── How the plan was CHOSEN, as distinct from how it was scored ──────────
    # `evaluation_kind` above says what the published E and CVaR were measured on.
    # These three say what the OPTIMIZER saw, which is a different question and was
    # for a long time answered wrongly by implication: a page reading "scenario
    # support: exact, 64 atoms" described the scoring while the solve ran on 200
    # draws that resolved 10 of those atoms.
    solve_kind: str = "saa"          # "saa" (draw counts) or "exact" (quantized mass)
    solve_weight_total: int = 0      # denominator of the integer objective weights
    solve_residual_mass: float = 0.0  # probability of atoms below the weight resolution
    n_scenarios_weighted: int = 0    # atoms that actually carried objective weight
    # ── Which budget truncated the solve, and how much work it actually did ──
    # `deterministic_time_limit` is None when the solve ran on the wall clock alone,
    # which is the shipped default. `deterministic_seconds` is always populated.
    deterministic_time_limit: Optional[float] = None
    deterministic_seconds: float = 0.0

    @property
    def n_suppliers(self) -> int:
        return len(self.selected_distributor_ids)

    @property
    def risk_premium_usd(self) -> float:
        """CVaR minus expected cost: the tail exposure this plan carries."""
        return self.cvar_usd - self.expected_cost_usd


# ── Shared model preparation ─────────────────────────────────────────────────

@dataclass
class _Prepared:
    bom: List[BomLine]
    offers_by_component: Dict[int, List[Offer]]
    offer_by_key: Dict[Tuple[int, int], Offer]
    all_distributors: List[int]
    fixed_by_did: Dict[int, float]
    per_unit_by_did: Dict[int, float]
    consolidation_usd: float
    unmet_unit_usd: Dict[int, float]


def _prepare(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    us_only: bool,
) -> _Prepared:
    """
    Apply exactly the pre-filters `solve_sourcing` applies (outlier filter, us_only,
    duplicate-(component, distributor) collapse to the cheapest tier) and build the
    shared freight model. Reusing `filter_price_outliers` and `_freight_model_by_did`
    rather than reimplementing them is what keeps the stochastic program's costs
    comparable, line for line, with the deterministic MILP's.
    """
    if not bom:
        raise ValueError("BOM is empty -- cannot solve sourcing with zero components")

    kept, _drops = filter_price_outliers(offers, bom)
    if us_only:
        kept = [o for o in kept if o.is_domestic]

    deduped: Dict[Tuple[int, int], Offer] = {}
    for o in kept:
        key = (o.component_id, o.distributor_id)
        best = deduped.get(key)
        if best is None or (o.price_usd, -o.stock) < (best.price_usd, -best.stock):
            deduped[key] = o

    offers_by_component: Dict[int, List[Offer]] = {}
    for o in deduped.values():
        offers_by_component.setdefault(o.component_id, []).append(o)
    for group in offers_by_component.values():
        group.sort(key=lambda o: o.distributor_id)

    missing = [b.mpn for b in bom if not offers_by_component.get(b.component_id)]
    if missing:
        raise ValueError(f"No valid offers for components after filtering: {missing}")

    penalty_scale = getattr(weights, "transport_penalty_scale", 1.0)
    freight = _freight_model_by_did(list(deduped.values()), penalty_scale)

    # Unmet-demand penalty per unit. Snyder & Daskin (2005) price a disrupted,
    # uncoverable assignment at a large-but-finite lost-sales cost; sourcing.py already
    # encodes that as STOCKOUT_PENALTY_MULTIPLE (3.0) x unit price, and the same
    # multiple is reused here so the stochastic program and the heuristic it replaces
    # share their constants.
    #
    # The base it multiplies is the DEAREST EMERGENCY route for the line, not the
    # dearest catalogue price. Anchoring on catalogue price alone made unmet demand
    # cheaper than recourse on small residual quantities -- 30 units at 3 x $2.50 =
    # $225 beat a $150 expedited consignment plus $94 of parts -- so the model would
    # leave a line unfilled while stock sat on a surviving shelf. Pricing "we could not
    # get it at all" at 3x "the most expensive way we could have got it" restores the
    # intended ordering: unmet demand is a genuine last resort, chosen only when no
    # survivor holds the stock or the shortfall is too small to justify flying in.
    unmet_unit_usd = {
        b.component_id: STOCKOUT_PENALTY_MULTIPLE
        * max(
            max(o.price_usd for o in offers_by_component[b.component_id]),
            max(
                o.price_usd * (1.0 + EMERGENCY_REPROCURE_PREMIUM) + EXPEDITE_PER_UNIT_USD
                for o in offers_by_component[b.component_id]
            ),
        )
        for b in bom
    }

    return _Prepared(
        bom=bom,
        offers_by_component=offers_by_component,
        offer_by_key=dict(deduped),
        all_distributors=sorted({o.distributor_id for o in deduped.values()}),
        fixed_by_did=freight.fixed_by_did,
        per_unit_by_did=freight.per_unit_by_did,
        consolidation_usd=getattr(weights, "consolidation_bonus_usd", 1.0),
        unmet_unit_usd=unmet_unit_usd,
    )


def _emergency_unit_usd(prep: _Prepared, cid: int, did: int) -> float:
    """Landed cost of ONE emergency unit of `cid` bought from surviving `did`."""
    offer = prep.offer_by_key[(cid, did)]
    return offer.price_usd * (1.0 + EMERGENCY_REPROCURE_PREMIUM) + EXPEDITE_PER_UNIT_USD


def _avoided_unit_usd(prep: _Prepared, cid: int, did: int, recovery_rate: float) -> float:
    """
    Cost NOT incurred, per committed unit, when `did` fails to deliver.

    Goods are recovered at `recovery_rate` (1.0 = you do not pay for undelivered
    goods, the correct default for a purchase order) and the per-unit freight is
    recovered in full (nothing shipped, nothing to bill). The per-VISIT fixed fee is
    NOT recovered: qualification and the consignment minimum are sunk the moment you
    open the supplier.
    """
    offer = prep.offer_by_key[(cid, did)]
    return recovery_rate * offer.price_usd + prep.per_unit_by_did.get(did, 0.0)


# ── The two-stage stochastic program ─────────────────────────────────────────

def solve_stochastic_sourcing(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    scenario_set: ScenarioSet,
    lam: float = 0.0,
    alpha: float = DEFAULT_ALPHA,
    us_only: bool = False,
    recovery_rate: float = DEFAULT_RECOVERY_RATE,
    expedite_fixed_usd: float = EXPEDITE_FIXED_USD,
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
    relative_gap_limit: float = DEFAULT_RELATIVE_GAP,
    warm_start: Optional[List[SourcingAssignment]] = None,
    evaluation_set: Optional[ScenarioSet] = None,
    deterministic_time_limit: Optional[float] = DEFAULT_DETERMINISTIC_LIMIT,
) -> StochasticSourcingResult:
    """
    Solve  min (1-lam) E[cost] + lam CVaR_alpha[cost]  over the SAA scenario set.

    FORMULATION (all variables integer, all constraints linear -> exact in CP-SAT)

      First stage
        sum_d q[c,d] = demand_c                         for every BOM line c
        q[c,d] <= stock[c,d] * x[c,d]
        q[c,d] >= moq[c,d] * x[c,d]
        y[d]   >= x[c,d]

      First-stage cost
        F = sum_{c,d} price[c,d] q[c,d]
          + sum_d fixed[d] y[d]
          + sum_d per_unit[d] sum_c q[c,d]
          + sum_d consolidation y[d]

      Second stage, scenario s with failed set F_s
        sum_{d not in F_s} r[c,d,s] + u[c,s] = sum_{d in F_s} q[c,d]   (cover the gap)
        r[c,d,s] + q[c,d] <= stock[c,d]                  (emergency draws on RESIDUAL
                                                          stock -- you cannot buy the
                                                          same units twice)
        sum_c r[c,d,s] <= cap_d * e[d,s]                 (an expedited consignment is
                                                          raised on d, or it is not)

      Scenario cost
        C_s = F
            + sum_{c, d not in F_s} (price[c,d](1+premium) + air_per_unit) r[c,d,s]
            + sum_{d not in F_s} expedite_fixed * e[d,s]
            + sum_c unmet_unit[c] * u[c,s]
            - sum_{c, d in F_s} (recovery * price[c,d] + per_unit[d]) q[c,d]

      Rockafellar-Uryasev, applied to the RECOURSE cost R_s = C_s - F
        z_s >= R_s - eta,  z_s >= 0,  eta free

      Objective (multiplied by LAMBDA_DEN * W to keep every coefficient integer)
        LAMBDA_DEN * W * F
      + (LAMBDA_DEN - lam_i) * sum_s w_s R_s
      + lam_i * ( W * eta + ceil(1/(1-alpha)) * sum_s w_s z_s )

    WHERE THE INTEGER WEIGHTS w_s COME FROM. For a SAMPLED scenario set they are the
    draw counts and W = n_draws, unchanged. For an EXACTLY ENUMERATED set they are the
    true probabilities quantized to a common denominator, w_s = round(p_s * W), with W
    chosen as large as the int64 objective ceiling permits (`quantize_probabilities`,
    `_affordable_weight_total`). Both are exact integer weightings of a discrete
    measure; only the sampled one carries sampling error.

    This is the point of the module. Scoring on the enumerated support while solving on
    a sample means the plan is CHOSEN against a measure that resolves 10 of 64 atoms
    and a 95% tail four atoms wide, then REPORTED against all 64 -- and a page saying
    "scenario support: exact, 64 atoms" describes only the second half of that. Pass
    the enumerated set as `scenario_set` and the choice is made on the same complete
    support the scores are read from, with no sampling gap left to bound.

    WHY R_s AND NOT C_s. Both E[.] and CVaR_alpha[.] are translation invariant:
    E[F + R] = F + E[R] and CVaR(F + R) = F + CVaR(R) for the deterministic first-
    stage cost F. So the objective above is *identical* to the one written on C_s,
    but the model never materializes 150+ copies of the ~80-term first-stage
    expression inside 150+ equality constraints. On a 157-scenario instance that
    change took the lambda=0 solve from a 60s timeout at a 1.7% gap to sub-second
    OPTIMAL. E[cost] and CVaR[cost] are still reported on the FULL cost C_s -- the
    reformulation is inside the solver, not in the published numbers.

    lam = 0 is the risk-neutral recourse problem (min expected cost). lam = 1 is pure
    min-CVaR. Sweeping lam in between traces the efficient frontier.

    NOTE ON THE WEIGHTED-SUM SWEEP. Scalarizing with lambda can only recover Pareto
    points on the convex hull of the (E, CVaR) image. Integer programs routinely have
    "unsupported" efficient points that no lambda exposes, so a lambda-sweep frontier
    is a subset of the true efficient set, never a superset. Stated in the published
    doc rather than implied away.
    """
    # Deferred import: CP-SAT (and the pandas/pyarrow it pulls in transitively)
    # costs ~830 ms to import and is needed only when a solve actually runs, not
    # at boot. Keeping it here takes OR-Tools off the `import app.main` path.
    from ortools.sat.python import cp_model

    if not 0.0 <= lam <= 1.0:
        raise ValueError(f"lam must be in [0, 1], got {lam}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if not scenario_set.scenarios:
        raise ValueError("scenario_set is empty")
    if scenario_set.kind not in ("saa", "exact"):
        raise ValueError(
            f"unknown scenario set kind {scenario_set.kind!r}; expected 'saa' or 'exact'"
        )

    tail_scale = 1.0 / (1.0 - alpha)
    if abs(tail_scale - round(tail_scale)) > 1e-9:
        raise ValueError(
            f"alpha={alpha} gives 1/(1-alpha)={tail_scale}, which is not an integer. "
            "Use alpha in {0.9, 0.95, 0.98, 0.99} so the CVaR objective stays exactly "
            "integer-coefficient (CP-SAT requirement)."
        )
    tail_k = int(round(tail_scale))

    prep = _prepare(bom, offers, weights, us_only)
    lam_i = int(round(lam * LAMBDA_DEN))
    n_draws = scenario_set.n_draws

    model = cp_model.CpModel()

    # ── First stage ──────────────────────────────────────────────────────────
    y: Dict[int, cp_model.IntVar] = {
        d: model.new_bool_var(f"y_{d}") for d in prep.all_distributors
    }
    x: Dict[Tuple[int, int], cp_model.IntVar] = {}
    q: Dict[Tuple[int, int], cp_model.IntVar] = {}
    for b in prep.bom:
        for o in prep.offers_by_component[b.component_id]:
            key = (b.component_id, o.distributor_id)
            x[key] = model.new_bool_var(f"x_c{key[0]}_d{key[1]}")
            q[key] = model.new_int_var(0, max(min(o.stock, b.quantity), 0), f"q_c{key[0]}_d{key[1]}")

    for b in prep.bom:
        group = prep.offers_by_component[b.component_id]
        model.add(sum(q[(b.component_id, o.distributor_id)] for o in group) == b.quantity)
        for o in group:
            key = (b.component_id, o.distributor_id)
            model.add(q[key] <= o.stock * x[key])
            model.add(q[key] >= max(o.moq, 1) * x[key])
            model.add(y[o.distributor_id] >= x[key])

    first_stage_terms = []
    for b in prep.bom:
        for o in prep.offers_by_component[b.component_id]:
            key = (b.component_id, o.distributor_id)
            first_stage_terms.append(_usd_to_units(o.price_usd) * q[key])
            per_unit = prep.per_unit_by_did.get(o.distributor_id, 0.0)
            if _usd_to_units(per_unit):
                first_stage_terms.append(_usd_to_units(per_unit) * q[key])
    for d in prep.all_distributors:
        opening = prep.fixed_by_did.get(d, 0.0) + prep.consolidation_usd
        if _usd_to_units(opening):
            first_stage_terms.append(_usd_to_units(opening) * y[d])
    first_stage_expr = sum(first_stage_terms)

    # ── Variable domains ─────────────────────────────────────────────────────
    # Bounded on the RECOURSE cost specifically, not on total cost. Loose bounds here
    # are not merely untidy: they set the domains of eta and every z_s, and the product
    # (objective coefficient x domain) is what has to stay inside int64.
    #
    # Most positive recourse can be: every committed unit is lost and covered the
    # dearest way available for its line (emergency buy or, dearer still, the unmet
    # penalty), plus one expedited consignment per supplier in the pool.
    worst_cover_usd = sum(
        b.quantity * max(
            max(_emergency_unit_usd(prep, b.component_id, o.distributor_id)
                for o in prep.offers_by_component[b.component_id]),
            prep.unmet_unit_usd[b.component_id],
        )
        for b in prep.bom
    )
    r_ub_usd = worst_cover_usd + expedite_fixed_usd * len(prep.all_distributors)

    # Most negative recourse can be: the whole BOM is refunded at its dearest price and
    # its freight avoided, with nothing re-bought.
    worst_refund_usd = sum(
        b.quantity * (
            max(o.price_usd for o in prep.offers_by_component[b.component_id])
            + max(prep.per_unit_by_did.get(o.distributor_id, 0.0)
                  for o in prep.offers_by_component[b.component_id])
        )
        for b in prep.bom
    )

    # First-stage cost, for the objective-magnitude guard only.
    f_ub_usd = sum(
        b.quantity * max(o.price_usd + prep.per_unit_by_did.get(o.distributor_id, 0.0)
                         for o in prep.offers_by_component[b.component_id])
        for b in prep.bom
    ) + sum(
        prep.fixed_by_did.get(d, 0.0) + prep.consolidation_usd
        for d in prep.all_distributors
    )

    r_ub_units = _usd_to_units(r_ub_usd) + 1
    r_lb_units = -_usd_to_units(worst_refund_usd) - 1
    # z_s >= R_s - eta with eta as low as r_lb, so z_s can reach the full span.
    z_ub_units = r_ub_units - r_lb_units
    f_ub_units = _usd_to_units(f_ub_usd) + 1
    r_span_units = max(r_ub_units, -r_lb_units)

    # ── Objective scaling and the int64 guard ────────────────────────────────
    # The three outer multipliers frequently share a factor (lam = 0.5 gives
    # 100/50/50, lam = 0.05 gives 100/95/5). Dividing them by their GCD is an exact
    # transformation -- it rescales the whole objective by a constant, leaving the
    # argmin untouched -- and it buys back up to two orders of magnitude of headroom
    # for large scenario sets.
    w_first, w_mean, w_cvar = LAMBDA_DEN, LAMBDA_DEN - lam_i, lam_i
    g = math.gcd(math.gcd(w_first, w_mean), w_cvar)
    if g > 1:
        w_first, w_mean, w_cvar = w_first // g, w_mean // g, w_cvar // g

    # ── Scenario weights ─────────────────────────────────────────────────────
    # A SAMPLED set weights by draw count, exactly as it always has: the empirical
    # measure IS the counts, and nothing about a sampled solve changes here.
    #
    # An EXACT set weights by its true probabilities, quantized to a common integer
    # denominator chosen as large as the int64 objective ceiling allows. That is the
    # fix for the defect this module used to document and then not act on: the plan is
    # now CHOSEN on the same complete support it is SCORED on, so there is no sampling
    # error left in the choice either -- and therefore no SAA optimality gap to bound.
    solve_residual_mass = 0.0
    weight_total_target = 0
    if scenario_set.kind == "exact":
        # Rounding each atom independently can push the realized total above the target
        # by at most half an atom each, so the target leaves that much room and the
        # magnitude guard below is still checked on the weights actually used.
        affordable = _affordable_weight_total(
            w_first, w_mean, w_cvar,
            f_ub_units, r_span_units, z_ub_units, tail_k,
        ) - len(scenario_set.scenarios)
        weight_total_target = min(EXACT_WEIGHT_TOTAL_CAP, max(affordable, 0))
        if weight_total_target < MIN_EXACT_WEIGHT_TOTAL:
            raise ValueError(
                f"this instance can only carry a scenario-weight denominator of "
                f"{weight_total_target:,} before the objective passes the int64 safety "
                f"ceiling {MAX_OBJ_COEFF:.3e}, below the {MIN_EXACT_WEIGHT_TOTAL:,} "
                "needed to represent the exact measure faithfully. Reduce the BOM "
                "volume, or pass a sampled scenario set and bound the sampling error "
                "with saa_optimality_gap()."
            )
        scenario_weights, solve_residual_mass = quantize_probabilities(
            scenario_set.probabilities, weight_total_target,
        )
    else:
        scenario_weights = [s.count for s in scenario_set.scenarios]

    weight_total = sum(scenario_weights)
    if weight_total <= 0:
        raise ValueError(
            "every scenario carries zero objective weight; the scenario set is empty "
            "of usable mass"
        )

    obj_bound = (
        w_first * weight_total * f_ub_units
        + w_mean * weight_total * r_span_units
        + w_cvar * weight_total * (r_span_units + tail_k * z_ub_units)
    )
    if obj_bound > MAX_OBJ_COEFF:
        raise ValueError(
            f"the objective could reach {obj_bound:.3e}, above the int64 safety ceiling "
            f"{MAX_OBJ_COEFF:.3e}. Reduce the scenario weight total (currently "
            f"{weight_total:,}) or the BOM volume "
            f"({sum(b.quantity for b in prep.bom)} units)."
        )

    # ── Second stage ─────────────────────────────────────────────────────────
    n_recourse_vars = 0
    scenario_recourse_vars: List[Optional[cp_model.IntVar]] = []
    r_by_scenario: List[Dict[Tuple[int, int], cp_model.IntVar]] = []
    u_by_scenario: List[Dict[int, cp_model.IntVar]] = []

    for s_idx, scen in enumerate(scenario_set.scenarios):
        if scenario_weights[s_idx] <= 0:
            # Below the weight resolution: it carries no objective weight, so building
            # its second stage would add variables and constraints that cannot change
            # the answer. Its probability is summed into `solve_residual_mass` and
            # published, never silently discarded.
            scenario_recourse_vars.append(None)
            r_by_scenario.append({})
            u_by_scenario.append({})
            continue
        failed = scen.failed
        recourse_terms = []
        r_vars: Dict[Tuple[int, int], cp_model.IntVar] = {}
        u_vars: Dict[int, cp_model.IntVar] = {}

        # Only BOM lines with an offer at a failed distributor can have a shortfall.
        affected = [
            b for b in prep.bom
            if any(o.distributor_id in failed for o in prep.offers_by_component[b.component_id])
        ]
        survivor_pool = sorted({
            o.distributor_id
            for b in affected
            for o in prep.offers_by_component[b.component_id]
            if o.distributor_id not in failed
        })
        e_vars: Dict[int, cp_model.IntVar] = {}
        if expedite_fixed_usd > 0.0:
            for d in survivor_pool:
                e_vars[d] = model.new_bool_var(f"e_d{d}_s{s_idx}")
                n_recourse_vars += 1

        r_cap: Dict[Tuple[int, int], int] = {}
        for b in affected:
            cid = b.component_id
            group = prep.offers_by_component[cid]
            survivors = [o for o in group if o.distributor_id not in failed]

            u_vars[cid] = model.new_int_var(0, b.quantity, f"u_c{cid}_s{s_idx}")
            n_recourse_vars += 1
            shortfall = sum(
                q[(cid, o.distributor_id)] for o in group if o.distributor_id in failed
            )

            cover_terms = []
            for o in survivors:
                key = (cid, o.distributor_id)
                cap = max(min(o.stock, b.quantity), 0)
                if cap == 0:
                    continue
                rv = model.new_int_var(0, cap, f"r_c{cid}_d{o.distributor_id}_s{s_idx}")
                n_recourse_vars += 1
                r_vars[key] = rv
                r_cap[key] = cap
                cover_terms.append(rv)
                # Emergency buys draw on RESIDUAL stock, not the full shelf.
                model.add(rv + q[key] <= o.stock)
                recourse_terms.append(
                    _usd_to_units(_emergency_unit_usd(prep, cid, o.distributor_id)) * rv
                )

            model.add(sum(cover_terms) + u_vars[cid] == shortfall)
            recourse_terms.append(_usd_to_units(prep.unmet_unit_usd[cid]) * u_vars[cid])

            for o in group:
                if o.distributor_id in failed:
                    avoided = _avoided_unit_usd(prep, cid, o.distributor_id, recovery_rate)
                    recourse_terms.append(-_usd_to_units(avoided) * q[(cid, o.distributor_id)])

        # An expedited consignment costs its air-freight minimum, once per supplier
        # actually used for emergency cover in this scenario.
        for d, ev in e_vars.items():
            units_from_d = [rv for (_c, did), rv in r_vars.items() if did == d]
            if not units_from_d:
                model.add(ev == 0)
                continue
            cap_d = sum(cap for (_c, did), cap in r_cap.items() if did == d)
            model.add(sum(units_from_d) <= cap_d * ev)
            recourse_terms.append(_usd_to_units(expedite_fixed_usd) * ev)

        # Scenarios with no failure among this BOM's suppliers have IDENTICALLY zero
        # recourse. With a realistic base rate that is ~25% of all draws; giving them
        # a variable and a constraint would be pure overhead.
        if not recourse_terms:
            scenario_recourse_vars.append(None)
        else:
            rec_var = model.new_int_var(r_lb_units, r_ub_units, f"R_s{s_idx}")
            model.add(rec_var == sum(recourse_terms))
            scenario_recourse_vars.append(rec_var)
        r_by_scenario.append(r_vars)
        u_by_scenario.append(u_vars)

    # ── Rockafellar-Uryasev CVaR block ───────────────────────────────────────
    # Applied to RECOURSE cost, not total cost -- see the docstring. E and CVaR are
    # both translation invariant, so adding the deterministic first-stage cost F back
    # afterwards is exact, and the model avoids replicating F's ~80 terms inside one
    # equality constraint per scenario.
    #
    # The block is built ONLY when lambda > 0. At lambda = 0 it carries zero objective
    # weight, so `eta` and every `z_s` become free variables in a large integer domain
    # that CP-SAT must still search: on a 157-scenario instance that turned a
    # sub-second solve into a 60s timeout returning a 1.7%-gap answer. Omitting it is
    # not an approximation -- at lambda = 0 the problem IS min E[cost], and CVaR is
    # reported either way, computed post hoc from the realized scenario costs.
    weighted = [i for i, w in enumerate(scenario_weights) if w > 0]
    expected_terms = [
        scenario_weights[i] * rec
        for i in weighted
        if (rec := scenario_recourse_vars[i]) is not None
    ]
    expected_block = sum(expected_terms)
    z: List[cp_model.IntVar] = []
    if lam_i > 0:
        eta = model.new_int_var(r_lb_units, r_ub_units, "eta")
        z = [model.new_int_var(0, z_ub_units, f"z_s{i}") for i in weighted]
        for i, zv in zip(weighted, z, strict=True):
            maybe_rec = scenario_recourse_vars[i]
            model.add(zv >= (maybe_rec if maybe_rec is not None else 0) - eta)
        cvar_block = weight_total * eta + tail_k * sum(
            scenario_weights[i] * zv for i, zv in zip(weighted, z, strict=True)
        )
        model.minimize(
            w_first * weight_total * first_stage_expr
            + w_mean * expected_block
            + w_cvar * cvar_block
        )
    else:
        model.minimize(weight_total * first_stage_expr + expected_block)

    # ── Warm start ───────────────────────────────────────────────────────────
    # Frontier continuation: hint the first stage with the neighbouring lambda's plan.
    # Adjacent points on the frontier usually share most of their sourcing decisions,
    # so the hint is a genuinely good incumbent and CP-SAT spends its time proving
    # optimality instead of finding one. Only FIRST-STAGE variables are hinted;
    # recourse is left to the solver.
    if warm_start:
        hint_q: Dict[Tuple[int, int], int] = {}
        for a in warm_start:
            key = (a.component_id, a.distributor_id)
            if key in q:
                hint_q[key] = hint_q.get(key, 0) + a.quantity
        hinted_dids = {did for (_c, did), n in hint_q.items() if n > 0}
        for key, var in q.items():
            model.add_hint(var, hint_q.get(key, 0))
        for key, var in x.items():
            model.add_hint(var, 1 if hint_q.get(key, 0) > 0 else 0)
        for did, var in y.items():
            model.add_hint(var, 1 if did in hinted_dids else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.relative_gap_limit = relative_gap_limit
    # REQUIRED: OR-Tools CP-SAT hangs at 0% CPU under bare-python invocation on macOS
    # with multiple workers. Also keeps the seed=42 reproducibility story true.
    solver.parameters.num_search_workers = 1
    if deterministic_time_limit is not None:
        # Work budget, not clock budget -- see DEFAULT_DETERMINISTIC_LIMIT. The wall
        # clock set above stays on as a runaway guard.
        solver.parameters.max_deterministic_time = deterministic_time_limit
    t0 = time.perf_counter()
    status = solver.Solve(model)
    wall = time.perf_counter() - t0
    # CP-SAT's own measure of the work it did. Reproducible where `wall` is not, and
    # the number to quote when reporting how much search a solve was given.
    deterministic_seconds = float(solver.response_proto.deterministic_time)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Tell the three failure statuses apart. See the exception classes at the top of
        # this module: only INFEASIBLE is a statement about the caller's BOM.
        status_name = solver.StatusName(status)
        common = {
            "status": status_name,
            "lam": lam,
            "n_scenarios": len(scenario_set.scenarios),
            "n_draws": n_draws,
            "time_limit_s": time_limit_s,
        }
        if status == cp_model.INFEASIBLE:
            raise ModelInfeasibleError(
                "CP-SAT proved no sourcing plan satisfies this BOM's constraints "
                f"(lam={lam:g}). Usual causes: total demand for a line exceeds the stock "
                "on offer across every distributor, an MOQ exceeds the line quantity, or "
                "a filter (us_only) emptied a line's supplier pool.",
                **common,
            )
        if status == cp_model.MODEL_INVALID:
            raise ModelInvalidError(
                f"CP-SAT rejected the model as invalid (lam={lam:g}). This is a defect "
                "in the model construction, not in the request.",
                **common,
            )
        raise SolverBudgetExceededError(
            f"solver budget exhausted at {len(scenario_set.scenarios)} scenarios and "
            f"lambda={lam:g}: CP-SAT hit its {time_limit_s:g}s limit before finding any "
            "feasible plan. This is a limit on OUR search budget, not a finding that the "
            "BOM has no solution -- a plan may well exist. Retry with fewer scenarios, a "
            "longer limit, or read the points that did solve.",
            **common,
        )

    assignments: List[SourcingAssignment] = []
    for b in prep.bom:
        for o in prep.offers_by_component[b.component_id]:
            qty = solver.Value(q[(b.component_id, o.distributor_id)])
            if qty > 0:
                assignments.append(SourcingAssignment(
                    component_id=b.component_id,
                    mpn=b.mpn,
                    distributor_id=o.distributor_id,
                    distributor_name=o.distributor_name,
                    quantity=qty,
                    unit_price_usd=o.price_usd,
                ))

    obj = solver.ObjectiveValue()
    bound = solver.BestObjectiveBound()

    # ── Report the plan's TRUE risk profile, not the solver's internal values ──
    # The joint model may return a first-stage plan whose per-scenario RECOURSE
    # variables are themselves only near-optimal (that is precisely what a non-zero
    # MIP gap means here: 157 loosely-coupled fixed-charge subproblems are hard to
    # prove optimal). Reading E and CVaR off those variables would publish a number
    # that is worse than the plan actually is. `evaluate_plan` re-solves each
    # scenario's second stage exactly and independently, so the published statistics
    # describe the recommended plan itself. The solver's status and MIP gap are
    # reported alongside and describe the quality of the FIRST-STAGE choice.
    #
    # SEPARATELY: the plan is CHOSEN on `scenario_set` and SCORED on `evaluation_set`.
    # Pass the SAME enumerated support as both and neither the choice nor the score
    # carries any sampling error, and there is no SAA gap to bound at all. Pass a
    # sample as the solve set and the exact support as the evaluation set and only the
    # CHOICE is subject to SAA error -- that is the case `saa_optimality_gap` exists
    # for. Defaults to the solve set, which reproduces in-sample scoring.
    scoring_set = evaluation_set if evaluation_set is not None else scenario_set
    profile = evaluate_plan(
        assignments, bom, offers, weights, scoring_set,
        alpha=alpha, us_only=us_only, recovery_rate=recovery_rate,
        expedite_fixed_usd=expedite_fixed_usd,
    )

    return StochasticSourcingResult(
        lam=lam,
        alpha=alpha,
        assignments=assignments,
        selected_distributor_ids=profile.selected_distributor_ids,
        first_stage_cost_usd=profile.first_stage_cost_usd,
        expected_cost_usd=profile.expected_cost_usd,
        var_usd=profile.var_usd,
        cvar_usd=profile.cvar_usd,
        cvar_by_alpha=profile.cvar_by_alpha,
        max_atom_probability=profile.max_atom_probability,
        tail=profile.tail,
        evaluation_kind=profile.evaluation_kind,
        expected_recourse_usd=profile.expected_recourse_usd,
        outcomes=profile.outcomes,
        status=solver.StatusName(status),
        objective_units=obj,
        best_bound_units=bound,
        gap_pct=(abs(obj - bound) / abs(obj) * 100.0) if obj else 0.0,
        wall_seconds=wall,
        evaluate_seconds=profile.wall_seconds,
        # `eta` exists only when the CVaR block is built, i.e. lambda > 0. Counting it
        # unconditionally overstated the lambda = 0 model by exactly one variable.
        n_variables=(
            len(x) + len(q) + len(y) + n_recourse_vars + len(z)
            + (1 if lam_i > 0 else 0)
        ),
        n_scenarios_distinct=len(scenario_set.scenarios),
        n_draws=n_draws,
        solve_kind=scenario_set.kind,
        solve_weight_total=weight_total,
        solve_residual_mass=solve_residual_mass,
        n_scenarios_weighted=len(weighted),
        deterministic_time_limit=deterministic_time_limit,
        deterministic_seconds=deterministic_seconds,
    )


# ── Evaluating an arbitrary plan under the same scenarios ────────────────────

def evaluate_plan(
    assignments: List[SourcingAssignment],
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    scenario_set: ScenarioSet,
    alpha: float = DEFAULT_ALPHA,
    us_only: bool = False,
    recovery_rate: float = DEFAULT_RECOVERY_RATE,
    expedite_fixed_usd: float = EXPEDITE_FIXED_USD,
) -> StochasticSourcingResult:
    """
    Score a FIXED first-stage plan (from any solver) under a scenario set, solving the
    second stage exactly in each scenario.

    This is what makes the comparison honest in three directions:
      * out-of-sample validation -- score a plan chosen on scenario set A against an
        independently drawn set B, which is the standard SAA sanity check and the only
        way to know the frontier is not fitted to its own noise;
      * the value of the stochastic solution (VSS) -- score the DETERMINISTIC MILP's
        plan under the same scenarios and compare with the lam=0 stochastic optimum;
      * comparing against the heuristic surcharge plan this module replaces.

    The recourse problem is solved to optimality per scenario as its own small
    fixed-charge model, so the scores are exact, not a greedy approximation.
    """
    prep = _prepare(bom, offers, weights, us_only)
    committed: Dict[Tuple[int, int], int] = {}
    for a in assignments:
        key = (a.component_id, a.distributor_id)
        committed[key] = committed.get(key, 0) + a.quantity

    unknown = [k for k in committed if k not in prep.offer_by_key]
    if unknown:
        raise ValueError(f"plan references offers not in the filtered pool: {unknown}")

    used = sorted({did for (_cid, did), qty in committed.items() if qty > 0})
    first_stage = (
        sum(prep.offer_by_key[k].price_usd * n for k, n in committed.items())
        + sum(prep.fixed_by_did.get(d, 0.0) + prep.consolidation_usd for d in used)
        + sum(prep.per_unit_by_did.get(k[1], 0.0) * n for k, n in committed.items())
    )

    demand = {b.component_id: b.quantity for b in bom}
    outcomes: List[ScenarioOutcome] = []
    total_wall = 0.0
    for scen in scenario_set.scenarios:
        t0 = time.perf_counter()
        recourse, emergency_units, unmet_units = _solve_recourse(
            prep, committed, demand, scen.failed, recovery_rate, expedite_fixed_usd,
        )
        total_wall += time.perf_counter() - t0
        outcomes.append(ScenarioOutcome(
            failed=scen.failed,
            count=scen.count,
            probability=scen.probability,
            total_cost_usd=first_stage + recourse,
            recourse_cost_usd=recourse,
            emergency_units=emergency_units,
            unmet_units=unmet_units,
        ))

    # Weighted by PROBABILITY, not by draw count. For a sampled set the two are
    # proportional and nothing changes; for an enumerated set this is what makes the
    # reported statistics exact rather than an estimate.
    probs = [o.probability for o in outcomes]
    costs = [o.total_cost_usd for o in outcomes]
    var_usd, cvar_usd = weighted_var_cvar(costs, probs, alpha)
    cvar_by_alpha = {
        a: weighted_var_cvar(costs, probs, a)[1] for a in REPORTED_ALPHAS
    }
    tail = tail_composition(costs, probs, alpha)

    return StochasticSourcingResult(
        lam=float("nan"),
        alpha=alpha,
        assignments=list(assignments),
        selected_distributor_ids=used,
        first_stage_cost_usd=first_stage,
        expected_cost_usd=weighted_mean(costs, probs),
        var_usd=var_usd,
        cvar_usd=cvar_usd,
        cvar_by_alpha=cvar_by_alpha,
        max_atom_probability=scenario_set.max_atom_probability,
        tail=tail,
        evaluation_kind=scenario_set.kind,
        expected_recourse_usd=weighted_mean([o.recourse_cost_usd for o in outcomes], probs),
        outcomes=outcomes,
        status="EVALUATED",
        objective_units=float("nan"),
        best_bound_units=float("nan"),
        gap_pct=0.0,
        wall_seconds=total_wall,
        evaluate_seconds=total_wall,
        n_variables=0,
        n_scenarios_distinct=len(scenario_set.scenarios),
        n_draws=scenario_set.n_draws,
        solve_kind="none",  # nothing was optimized here; a fixed plan was scored
        solve_weight_total=0,
        solve_residual_mass=0.0,
        n_scenarios_weighted=0,
    )


def _solve_recourse(
    prep: _Prepared,
    committed: Dict[Tuple[int, int], int],
    demand: Dict[int, int],
    failed: FrozenSet[int],
    recovery_rate: float,
    expedite_fixed_usd: float,
) -> Tuple[float, int, int]:
    """
    Exact second stage for ONE scenario with the first stage already fixed.

    Returns (recourse_cost_usd, emergency_units, unmet_units). Recourse cost is NET:
    emergency purchase + expedite freight + unmet penalty, minus the cost of goods and
    freight avoided because the failed supplier never shipped. It can therefore be
    negative in the (rare, and documented) case where an expensive supplier fails and
    a cheap survivor covers the gap for less than the refund.
    """
    shortfall: Dict[int, int] = {}
    avoided = 0.0
    for (cid, did), qty in committed.items():
        if did in failed and qty > 0:
            shortfall[cid] = shortfall.get(cid, 0) + qty
            avoided += _avoided_unit_usd(prep, cid, did, recovery_rate) * qty

    if not shortfall:
        return 0.0, 0, 0

    # Deferred import — see solve_stochastic_sourcing. Placed after the
    # no-shortfall early return so scenarios that need no recourse never pay it.
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    r_vars: Dict[Tuple[int, int], cp_model.IntVar] = {}
    u_vars: Dict[int, cp_model.IntVar] = {}
    terms = []

    survivor_pool: Set[int] = set()
    r_cap: Dict[Tuple[int, int], int] = {}
    for cid, gap in shortfall.items():
        survivors = [o for o in prep.offers_by_component[cid] if o.distributor_id not in failed]
        cover = []
        for o in survivors:
            residual = o.stock - committed.get((cid, o.distributor_id), 0)
            cap = max(min(residual, demand[cid]), 0)
            if cap == 0:
                continue
            rv = model.new_int_var(0, cap, f"r_{cid}_{o.distributor_id}")
            r_vars[(cid, o.distributor_id)] = rv
            r_cap[(cid, o.distributor_id)] = cap
            survivor_pool.add(o.distributor_id)
            cover.append(rv)
            terms.append(_usd_to_units(_emergency_unit_usd(prep, cid, o.distributor_id)) * rv)
        uv = model.new_int_var(0, gap, f"u_{cid}")
        u_vars[cid] = uv
        model.add(sum(cover) + uv == gap)
        terms.append(_usd_to_units(prep.unmet_unit_usd[cid]) * uv)

    if expedite_fixed_usd > 0.0:
        for d in sorted(survivor_pool):
            from_d = [rv for (_c, did), rv in r_vars.items() if did == d]
            if not from_d:
                continue
            ev = model.new_bool_var(f"e_{d}")
            cap_d = sum(cap for (_c, did), cap in r_cap.items() if did == d)
            model.add(sum(from_d) <= cap_d * ev)
            terms.append(_usd_to_units(expedite_fixed_usd) * ev)

    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"recourse subproblem infeasible for failed={sorted(failed)}")

    gross = solver.ObjectiveValue() / OBJ_SCALE
    return (
        gross - avoided,
        sum(solver.Value(v) for v in r_vars.values()),
        sum(solver.Value(v) for v in u_vars.values()),
    )


# ── Frontier ─────────────────────────────────────────────────────────────────

@dataclass
class FrontierPoint:
    lam: float
    expected_cost_usd: float
    cvar_usd: float
    var_usd: float
    first_stage_cost_usd: float
    expected_recourse_usd: float
    n_suppliers: int
    supplier_ids: List[int]
    status: str
    gap_pct: float
    wall_seconds: float
    evaluate_seconds: float
    n_variables: int
    cvar_by_alpha: Dict[float, float] = field(default_factory=dict)
    n_atoms_in_tail: int = 0
    largest_tail_atom_share: float = 0.0
    evaluation_kind: str = "saa"
    solve_kind: str = "saa"
    solve_weight_total: int = 0
    solve_residual_mass: float = 0.0
    n_scenarios_weighted: int = 0
    dominated: bool = False
    deterministic_time_limit: Optional[float] = None
    deterministic_seconds: float = 0.0


@dataclass
class UnsolvedLambda:
    """One frontier point that could not be produced, and why -- kept, not hidden."""
    lam: float
    reason: str          # "solver_budget_exhausted" | "sweep_budget_exhausted"
    solver_status: str   # CP-SAT status name, or "NOT_ATTEMPTED"
    detail: str
    time_limit_s: float
    n_scenarios: int


@dataclass
class FrontierSweep:
    """
    The result of a lambda sweep, INCLUDING the points that did not solve.

    A frontier with 4 of 6 lambda points solved and the other two labelled is strictly
    more useful than an error that claims the BOM has no solution. `complete` says
    which case this is; `unsolved` says exactly what was lost.
    """
    points: List[FrontierPoint]
    results: List[StochasticSourcingResult]
    unsolved: List[UnsolvedLambda] = field(default_factory=list)
    sweep_seconds: float = 0.0

    @property
    def complete(self) -> bool:
        return not self.unsolved

    @property
    def n_requested(self) -> int:
        return len(self.points) + len(self.unsolved)


def compute_frontier_sweep(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    scenario_set: ScenarioSet,
    lambdas: Sequence[float],
    alpha: float = DEFAULT_ALPHA,
    us_only: bool = False,
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
    evaluation_set: Optional[ScenarioSet] = None,
    allow_partial: bool = False,
    sweep_time_budget_s: Optional[float] = None,
    deterministic_time_limit: Optional[float] = DEFAULT_DETERMINISTIC_LIMIT,
) -> FrontierSweep:
    """
    Sweep lambda and return the cost-vs-CVaR points, the full results, AND the points
    that failed to solve.

    With `allow_partial=False` (the default, and the historical behaviour) any solver
    failure propagates. With `allow_partial=True` a point that exhausts the solver
    budget is recorded in `unsolved` and the sweep continues -- a partial frontier is a
    usable answer, an error that misattributes our time limit to the caller's BOM is
    not. A PROVEN infeasibility (`ModelInfeasibleError`) always propagates regardless:
    it is a real statement about the BOM and every other lambda will hit it too.

    `sweep_time_budget_s` caps the WHOLE sweep. Remaining lambdas are recorded as
    "sweep_budget_exhausted" / NOT_ATTEMPTED rather than silently dropped.
    """
    # Solved in DESCENDING lambda order. The pure-CVaR end is by far the easiest for
    # CP-SAT (the tail multiplier 1/(1-alpha) sharpens the objective and prunes hard),
    # while lambda = 0 is the slowest, so descending order means every hard point
    # inherits a warm start from an already-proved neighbour. Points are re-sorted
    # ascending before they are returned, so callers see the natural ordering.
    results: List[StochasticSourcingResult] = []
    unsolved: List[UnsolvedLambda] = []
    warm: Optional[List[SourcingAssignment]] = None
    sweep_t0 = time.perf_counter()
    ordered = sorted(lambdas, reverse=True)

    for i, lam in enumerate(ordered):
        elapsed = time.perf_counter() - sweep_t0
        if sweep_time_budget_s is not None and elapsed >= sweep_time_budget_s:
            for remaining in ordered[i:]:
                unsolved.append(UnsolvedLambda(
                    lam=remaining,
                    reason="sweep_budget_exhausted",
                    solver_status="NOT_ATTEMPTED",
                    detail=(
                        f"the {sweep_time_budget_s:g}s budget for the whole lambda sweep "
                        f"was spent after {len(results)} of {len(ordered)} points; this "
                        f"point was not attempted."
                    ),
                    time_limit_s=time_limit_s,
                    n_scenarios=scenario_set.n_distinct,
                ))
            break

        # The last point gets whatever budget is left rather than the full per-point
        # limit, so the sweep total stays near its cap. The 1.0s floor is deliberate: a
        # sub-second CP-SAT limit is not a smaller search, it is a guaranteed UNKNOWN,
        # so the sweep may overrun its budget by up to a second on its final point.
        point_limit = time_limit_s
        if sweep_time_budget_s is not None:
            point_limit = max(1.0, min(time_limit_s, sweep_time_budget_s - elapsed))
        try:
            res = solve_stochastic_sourcing(
                bom, offers, weights, scenario_set,
                lam=lam, alpha=alpha, us_only=us_only, time_limit_s=point_limit,
                warm_start=warm, evaluation_set=evaluation_set,
                deterministic_time_limit=deterministic_time_limit,
            )
        except SolverBudgetExceededError as exc:
            if not allow_partial:
                raise
            logger.warning("lam=%.2f unsolved: %s", lam, exc)
            unsolved.append(UnsolvedLambda(
                lam=lam,
                reason="solver_budget_exhausted",
                solver_status=exc.status,
                detail=str(exc),
                time_limit_s=point_limit,
                n_scenarios=exc.n_scenarios,
            ))
            continue  # keep the previous warm start; the next lambda may still solve
        warm = res.assignments
        results.append(res)
        logger.info(
            "lam=%.2f  E=$%.2f  CVaR=$%.2f  suppliers=%d  %s  gap=%.3f%%  %.2fs",
            lam, res.expected_cost_usd, res.cvar_usd, res.n_suppliers,
            res.status, res.gap_pct, res.wall_seconds,
        )

    results.sort(key=lambda r: r.lam)
    unsolved.sort(key=lambda u: u.lam)
    points = [
        FrontierPoint(
            lam=r.lam,
            expected_cost_usd=r.expected_cost_usd,
            cvar_usd=r.cvar_usd,
            var_usd=r.var_usd,
            first_stage_cost_usd=r.first_stage_cost_usd,
            expected_recourse_usd=r.expected_recourse_usd,
            n_suppliers=r.n_suppliers,
            supplier_ids=r.selected_distributor_ids,
            status=r.status,
            gap_pct=r.gap_pct,
            wall_seconds=r.wall_seconds,
            evaluate_seconds=r.evaluate_seconds,
            n_variables=r.n_variables,
            cvar_by_alpha=dict(r.cvar_by_alpha),
            n_atoms_in_tail=r.tail.n_atoms_in_tail,
            largest_tail_atom_share=r.tail.largest_tail_atom_share,
            evaluation_kind=r.evaluation_kind,
            solve_kind=r.solve_kind,
            solve_weight_total=r.solve_weight_total,
            solve_residual_mass=r.solve_residual_mass,
            n_scenarios_weighted=r.n_scenarios_weighted,
            deterministic_time_limit=r.deterministic_time_limit,
            deterministic_seconds=r.deterministic_seconds,
        )
        for r in results
    ]
    tol = 1e-6
    for p in points:
        p.dominated = any(
            other.expected_cost_usd < p.expected_cost_usd - tol
            and other.cvar_usd < p.cvar_usd - tol
            for other in points
        )
    return FrontierSweep(
        points=points,
        results=results,
        unsolved=unsolved,
        sweep_seconds=time.perf_counter() - sweep_t0,
    )


def compute_frontier(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    scenario_set: ScenarioSet,
    lambdas: Sequence[float],
    alpha: float = DEFAULT_ALPHA,
    us_only: bool = False,
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
    evaluation_set: Optional[ScenarioSet] = None,
    deterministic_time_limit: Optional[float] = DEFAULT_DETERMINISTIC_LIMIT,
) -> Tuple[List[FrontierPoint], List[StochasticSourcingResult]]:
    """
    Sweep lambda and return the cost-vs-CVaR points plus the full results.

    Strict form of `compute_frontier_sweep`: every lambda must solve or the failure
    propagates. Points are marked `dominated` when another point achieves both a lower
    expected cost AND a lower CVaR. On a correctly solved sweep there should be none;
    any that appear are a solver-truncation artefact and are reported rather than
    deleted.
    """
    sweep = compute_frontier_sweep(
        bom, offers, weights, scenario_set, lambdas,
        alpha=alpha, us_only=us_only, time_limit_s=time_limit_s,
        deterministic_time_limit=deterministic_time_limit,
        evaluation_set=evaluation_set, allow_partial=False,
    )
    return sweep.points, sweep.results


# ── Reading the SHAPE of a frontier, including when there is no trade-off ─────

def frontier_shape(points: Sequence[FrontierPoint]) -> Dict[str, Any]:
    """
    Describe what the frontier actually looks like -- especially when it is FLAT.

    A flat frontier is a legitimate and common finding, not a failure. On a single-line
    BOM where one supplier dominates at every risk appetite, or on any BOM below
    production volume where the fixed per-supplier charge swamps everything else, every
    lambda returns the same plan and there is genuinely no cost-vs-tail trade-off to
    make. `docs/BENCHMARK_VOLUME_CURVE.md` and section 4 of
    `docs/CVAR_EFFICIENT_FRONTIER.md` both document this: at 600 and 6,000 units the
    headline BOM's frontier collapses to a single point, and only at 60,000 does a knee
    appear.

    But "no knee" arriving as a null recommendation beside six identical rows LOOKS
    broken even when it is right. So the flatness is stated, with its cause, instead of
    being left for the reader to infer from a null.
    """
    if not points:
        return {
            "kind": "empty",
            "distinct_plans": 0,
            "has_tradeoff": False,
            "statement": "No frontier points were produced.",
        }

    plans = {tuple(sorted(p.supplier_ids)) for p in points}
    e_lo = min(p.expected_cost_usd for p in points)
    e_hi = max(p.expected_cost_usd for p in points)
    c_lo = min(p.cvar_usd for p in points)
    c_hi = max(p.cvar_usd for p in points)
    e_span = e_hi - e_lo
    c_span = c_hi - c_lo
    # Relative, so the verdict does not depend on the BOM's absolute spend.
    e_span_pct = 100.0 * e_span / e_lo if e_lo else 0.0
    c_span_pct = 100.0 * c_span / c_lo if c_lo else 0.0
    flat = len(plans) == 1 or (e_span_pct < 1e-6 and c_span_pct < 1e-6)

    if flat:
        only = sorted(next(iter(plans)))
        if len(only) == 1:
            cause = (
                f"a single supplier (distributor {only[0]}) is optimal at every risk "
                f"appetite, so there is no second source to shift volume to and nothing "
                f"to trade off."
            )
        else:
            cause = (
                f"the same {len(only)} suppliers are optimal at every risk appetite. "
                f"Below production volume the fixed per-supplier charge dominates the "
                f"objective, so the sourcing decision is settled by fee arithmetic and "
                f"risk cannot move it (see docs/BENCHMARK_VOLUME_CURVE.md)."
            )
        return {
            "kind": "flat",
            "distinct_plans": 1,
            "has_tradeoff": False,
            "expected_cost_span_usd": round(e_span, 2),
            "cvar_span_usd": round(c_span, 2),
            "supplier_ids": only,
            "statement": (
                f"No cost-vs-CVaR trade-off is available on this BOM: every lambda from "
                f"{min(p.lam for p in points):g} to {max(p.lam for p in points):g} "
                f"returns the identical plan, because {cause} A flat frontier is a "
                f"finding, not a failure -- there is simply no resilience to buy here."
            ),
        }

    return {
        "kind": "traded",
        "distinct_plans": len(plans),
        "has_tradeoff": True,
        "expected_cost_span_usd": round(e_span, 2),
        "expected_cost_span_pct": round(e_span_pct, 4),
        "cvar_span_usd": round(c_span, 2),
        "cvar_span_pct": round(c_span_pct, 4),
        "statement": (
            f"{len(plans)} distinct sourcing plans across the lambda sweep: expected "
            f"cost moves ${e_span:,.0f} ({e_span_pct:.2f}%) and CVaR-95 moves "
            f"${c_span:,.0f} ({c_span_pct:.2f}%) between the risk-neutral and "
            f"risk-averse ends."
        ),
    }


def find_knee(points: Sequence[FrontierPoint]) -> Optional[FrontierPoint]:
    """
    Knee of the cost-vs-CVaR frontier by maximum perpendicular distance to the chord
    joining the two extreme non-dominated points (the "Kneedle"/L-method criterion,
    Satopaa et al. 2011).

    Operationally: the point beyond which each additional dollar of expected cost buys
    the least additional CVaR reduction. Both axes are min-max normalized first, so
    the answer does not depend on whether costs are quoted in dollars or cents.

    Returns None when fewer than three distinct non-dominated points exist -- there is
    no knee on a two-point frontier and inventing one would be dishonest.
    """
    usable = [p for p in points if not p.dominated]
    uniq: List[FrontierPoint] = []
    for p in sorted(usable, key=lambda p: (p.expected_cost_usd, -p.cvar_usd)):
        if not uniq or abs(p.expected_cost_usd - uniq[-1].expected_cost_usd) > 1e-9 \
                or abs(p.cvar_usd - uniq[-1].cvar_usd) > 1e-9:
            uniq.append(p)
    if len(uniq) < 3:
        return None

    xs = [p.expected_cost_usd for p in uniq]
    ys = [p.cvar_usd for p in uniq]
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = min(ys), max(ys)
    if x_hi - x_lo < 1e-12 or y_hi - y_lo < 1e-12:
        return None

    nx = [(v - x_lo) / (x_hi - x_lo) for v in xs]
    ny = [(v - y_lo) / (y_hi - y_lo) for v in ys]
    x0, y0, x1, y1 = nx[0], ny[0], nx[-1], ny[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = math.hypot(dx, dy)
    if norm < 1e-12:
        return None

    best_idx, best_dist = 0, -1.0
    for i in range(len(uniq)):
        dist = abs(dy * (nx[i] - x0) - dx * (ny[i] - y0)) / norm
        if dist > best_dist:
            best_idx, best_dist = i, dist
    return uniq[best_idx]


# ── SAA solution quality (Mak, Morton & Wood 1999) ───────────────────────────

@dataclass
class SaaGapEstimate:
    """
    A statistical bound on how much the SAA sample size is costing us.

    Fields follow the standard construction:

      `lower_bound_mean`   Mean of M independent SAA optimal values at sample size N.
                           For a minimization problem E[v_N] <= v*, so this is a
                           statistically BIASED-LOW estimate of the true optimum, i.e.
                           an estimated lower bound. Mak, Morton & Wood (1999).
      `lower_bound_ci_*`   One-sided (1-delta) confidence limits from the M replicates
                           using the Student-t quantile at M-1 degrees of freedom.
      `upper_bound`        The true objective of ONE candidate first-stage plan,
                           evaluated on the reference distribution. Any feasible plan
                           gives a valid upper bound on the optimum, so this one is not
                           an estimate at all when the reference set is the exact
                           support.
      `gap_estimate`       upper_bound - lower_bound_mean, and its CI. This is the
                           quantity to report: it says "the sample size I used costs me
                           at most about this much".

    A SMALL NEGATIVE `gap_estimate` IS NORMAL AND IS NOT A BROKEN BOUND. The bias
    result is E[v_N] <= v*, an expectation; with a finite number of replications the
    sample MEAN can land slightly above the candidate plan's true value. When that
    happens the point estimate goes mildly negative, which is the signal "the remaining
    gap is smaller than the Monte Carlo noise in my estimate of it". The interval
    statement is the one that must hold: `upper_bound >= lower_bound_ci_low`.

    Kleywegt, Shapiro & Homem-de-Mello (2002) is the convergence result behind the
    procedure for discrete first-stage decisions, which is exactly this model.
    """
    n_scenarios: int
    n_replications: int
    lam: float
    alpha: float
    replicate_values: List[float]
    lower_bound_mean: float
    lower_bound_stderr: float
    lower_bound_ci_low: float
    lower_bound_ci_high: float
    upper_bound: float
    upper_bound_kind: str
    gap_estimate: float
    gap_ci_high: float
    gap_pct_of_upper: float
    wall_seconds: float


# One-sided Student-t quantiles at 95% for small replicate counts, so the confidence
# interval does not silently pretend the M replicates are normal with known variance.
# Index = degrees of freedom (M - 1). scipy is not a runtime dependency of this module.
_T_95_ONE_SIDED = {
    1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895,
    8: 1.860, 9: 1.833, 10: 1.812, 11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761,
    15: 1.753, 16: 1.746, 17: 1.740, 18: 1.734, 19: 1.729, 20: 1.725,
    24: 1.711, 29: 1.699, 39: 1.685, 59: 1.671,
}


def _t_quantile_95(dof: int) -> float:
    if dof <= 0:
        return float("inf")
    if dof in _T_95_ONE_SIDED:
        return _T_95_ONE_SIDED[dof]
    candidates = [d for d in _T_95_ONE_SIDED if d <= dof]
    return _T_95_ONE_SIDED[max(candidates)] if candidates else 1.645


def _objective_value(res: StochasticSourcingResult, lam: float, alpha: float) -> float:
    """(1-lam)*E[cost] + lam*CVaR_alpha[cost] in USD, from an evaluated result."""
    cvar = res.cvar_by_alpha.get(alpha, res.cvar_usd)
    return (1.0 - lam) * res.expected_cost_usd + lam * cvar


def saa_optimality_gap(
    bom: List[BomLine],
    offers: List[Offer],
    weights: StrategyWeights,
    failure_probs: Dict[int, float],
    reference_set: ScenarioSet,
    n_scenarios: int,
    n_replications: int = 10,
    lam: float = 0.5,
    alpha: float = DEFAULT_ALPHA,
    base_seed: int = 9000,
    us_only: bool = False,
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
    deterministic_time_limit: Optional[float] = DEFAULT_DETERMINISTIC_LIMIT,
) -> SaaGapEstimate:
    """
    Estimate how much the SAA sample size is costing, with a confidence interval.

    Procedure (Mak, Morton & Wood 1999; Kleywegt, Shapiro & Homem-de-Mello 2002):

      1. Solve the problem M times on M INDEPENDENT samples of size N. Because each
         solve optimizes against its own sample, its optimal value is optimistically
         biased: E[v_N] <= v*. The mean of the M values therefore estimates a LOWER
         bound on the true optimum, and the M replicates give it a standard error.
      2. Take one candidate first-stage plan -- here the best of the M by reference
         objective -- and evaluate it on the REFERENCE distribution. A feasible plan's
         true objective is an UPPER bound on the optimum by definition. When the
         reference set is the exact support (`kind == "exact"`), that upper bound is
         not an estimate; it is the true value of that plan.
      3. The gap between them bounds what a bigger sample could still buy.

    Sweeping N and watching the gap flatten is the honest way to justify a sample size,
    and it is the thing missing from "we used 200 draws because that seemed like a lot".

    WHEN THIS FUNCTION APPLIES AT ALL. It bounds the cost of SAMPLING, so it is only
    meaningful on instances that are actually sampled -- pools too wide to enumerate.
    When the solve set is the exact support (`fit_scenario_set` with an `exact_set` that
    fits the budget) there is no sample and no gap to bound: the answer is the optimum
    of the true measure, up to the integer weight quantization, which is reported as
    `solve_residual_mass` and is not a statistical quantity.
    """
    if n_replications < 2:
        raise ValueError("need at least 2 replications to form a confidence interval")

    t0 = time.perf_counter()
    replicate_values: List[float] = []
    candidates: List[List[SourcingAssignment]] = []

    for m in range(n_replications):
        sample = sample_scenarios(failure_probs, n_scenarios, base_seed + m)
        res = solve_stochastic_sourcing(
            bom, offers, weights, sample, lam=lam, alpha=alpha, us_only=us_only,
            time_limit_s=time_limit_s,
            deterministic_time_limit=deterministic_time_limit,
        )
        # In-sample objective of the in-sample optimum: the optimistically biased draw.
        replicate_values.append(_objective_value(res, lam, alpha))
        candidates.append(res.assignments)

    mean = sum(replicate_values) / len(replicate_values)
    if len(replicate_values) > 1:
        var = sum((v - mean) ** 2 for v in replicate_values) / (len(replicate_values) - 1)
        stderr = math.sqrt(var / len(replicate_values))
    else:
        stderr = 0.0
    t_crit = _t_quantile_95(len(replicate_values) - 1)

    # Upper bound: score every candidate on the reference distribution, keep the best.
    best_value = float("inf")
    for plan in candidates:
        prof = evaluate_plan(plan, bom, offers, weights, reference_set, alpha=alpha,
                             us_only=us_only)
        value = _objective_value(prof, lam, alpha)
        best_value = min(best_value, value)

    gap = best_value - mean
    gap_ci_high = best_value - (mean - t_crit * stderr)

    return SaaGapEstimate(
        n_scenarios=n_scenarios,
        n_replications=n_replications,
        lam=lam,
        alpha=alpha,
        replicate_values=[round(v, 4) for v in replicate_values],
        lower_bound_mean=mean,
        lower_bound_stderr=stderr,
        lower_bound_ci_low=mean - t_crit * stderr,
        lower_bound_ci_high=mean + t_crit * stderr,
        upper_bound=best_value,
        upper_bound_kind=(
            "exact (reference set is the full enumerated support)"
            if reference_set.kind == "exact"
            else f"sampled reference set, {reference_set.n_draws} draws"
        ),
        gap_estimate=gap,
        gap_ci_high=gap_ci_high,
        gap_pct_of_upper=(100.0 * gap / best_value) if best_value else 0.0,
        wall_seconds=time.perf_counter() - t0,
    )
