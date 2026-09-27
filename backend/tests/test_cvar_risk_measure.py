"""
The risk measure the sourcing model optimizes: VaR and CVaR of a weighted discrete
cost distribution (`app.optimization.stochastic.weighted_var_cvar`).

Every expected number below was worked by hand from the definitions, never by running
the code's own accumulation:

    VaR_alpha(Z)  = the smallest z with P(Z <= z) >= alpha
    CVaR_alpha(Z) = the mean of the worst (1 - alpha) of the probability mass, splitting
                    an atom fractionally when the tail boundary falls inside it
                    (Rockafellar & Uryasev 2002).

Each case says how its numbers were derived, so a reader can re-derive them on paper.
"""
from __future__ import annotations

import pytest

from app.optimization.stochastic import weighted_var_cvar


def test_boundary_atom_is_split_fractionally():
    # Five outcomes, probabilities 0.50 / 0.30 / 0.16 / 0.02 / 0.02.
    # Cumulative P(Z <= z): 100 -> 0.50, 200 -> 0.80, 300 -> 0.96, so VaR_0.95 = 300.
    # The worst 5% of mass is 0.02 at 5000 + 0.02 at 1000 + 0.01 of the 300 atom:
    #   CVaR_0.95 = (0.02*5000 + 0.02*1000 + 0.01*300) / 0.05 = 123 / 0.05 = 2460.
    values = [100.0, 200.0, 300.0, 1000.0, 5000.0]
    probs = [0.50, 0.30, 0.16, 0.02, 0.02]

    var, cvar = weighted_var_cvar(values, probs, alpha=0.95)

    assert var == pytest.approx(300.0)
    assert cvar == pytest.approx(2460.0)


def test_raw_draw_counts_give_the_same_answer_as_probabilities():
    # The same distribution written as Monte Carlo counts out of 100 draws. Only the
    # ratios of the weights define the distribution, so VaR and CVaR are unchanged.
    values = [5000.0, 100.0, 1000.0, 300.0, 200.0]   # deliberately unsorted
    counts = [2, 50, 2, 16, 30]

    var, cvar = weighted_var_cvar(values, counts, alpha=0.95)

    assert var == pytest.approx(300.0)
    assert cvar == pytest.approx(2460.0)


def test_equal_weight_sample_is_not_the_mean_of_the_worst_whole_draws():
    # Thirty equally likely costs 10, 20, ..., 300. The tail holds 5% of 30 draws =
    # 1.5 draws: all of 300 and half of 290.
    #   P(Z <= 280) = 28/30 = 0.933 < 0.95 and P(Z <= 290) = 29/30 >= 0.95, so VaR = 290.
    #   CVaR_0.95 = (300*1 + 290*0.5) / 1.5 = 445 / 1.5 = 296.666...
    # "Mean of the worst ceil(1.5) = 2 draws" would give 295 - a biased estimate.
    values = [10.0 * k for k in range(1, 31)]
    weights = [1] * 30

    var, cvar = weighted_var_cvar(values, weights, alpha=0.95)

    assert var == pytest.approx(290.0)
    assert cvar == pytest.approx(296.6666666667)
    assert cvar != pytest.approx(295.0)


def test_a_single_atom_covering_the_whole_tail_makes_cvar_equal_var():
    # Costs 10 with probability 0.90 and 50 with probability 0.10. The worst 5% of mass
    # lies entirely inside the 50 atom, so VaR_0.95 = CVaR_0.95 = 50: the tail averages
    # one scenario however many draws produced it.
    var, cvar = weighted_var_cvar([10.0, 50.0], [0.90, 0.10], alpha=0.95)

    assert var == pytest.approx(50.0)
    assert cvar == pytest.approx(50.0)


def test_invalid_inputs_are_rejected_rather_than_scored():
    with pytest.raises(ValueError):
        weighted_var_cvar([1.0, 2.0], [1.0], alpha=0.95)
    with pytest.raises(ValueError):
        weighted_var_cvar([], [], alpha=0.95)
    with pytest.raises(ValueError):
        weighted_var_cvar([1.0], [1.0], alpha=1.0)
    with pytest.raises(ValueError):
        weighted_var_cvar([1.0], [1.0], alpha=0.0)
