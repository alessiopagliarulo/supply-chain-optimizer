"""
Two-stage stochastic sourcing program with a CVaR objective (SAA + Rockafellar-Uryasev).

Restored from git tag `archive/sourcing-v1` one slice at a time. This first slice is
the risk measure alone: VaR and CVaR of a weighted discrete cost distribution, and the
diagnostic that counts how many distinct outcomes the CVaR tail actually averages.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

# ── SAA / CVaR settings ──────────────────────────────────────────────────────
DEFAULT_ALPHA = 0.95          # CVaR tail level (worst 5%)


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
