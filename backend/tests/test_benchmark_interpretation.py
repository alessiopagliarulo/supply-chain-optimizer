"""The served `resilience.interpretation` must agree with the numbers beside it.

Why this file exists
--------------------
Until 2026-08-28 the interpretation string was a hardcoded sentence chosen by a
branch that tested one thing only: whether any reduction was *exactly* 0.0.

    flat = [name for name, value in reductions.items() if abs(value) < 1e-9]
    if flat:  ...
    else:
        "All four reductions are non-zero: the graph-aware arm lowered both plan
         cascade risk and the CVaR-95 tail under stress and targeted disruption."

Sign was never consulted. A reduction is ``mean(blind - graph)``, so a NEGATIVE
value means the graph-aware arm scored **worse** — and a negative value is
non-zero, so it took the `else` branch and published "lowered".

That is not hypothetical. On run 6 the live endpoint returned, in one response:

    stress_cascade_risk_reduction                  = -0.0833      (the arm RAISED it)
    intervals.stress_cascade_risk_reduction.significant = False   (CI covers zero)
    interpretation = "...the graph-aware arm lowered both plan cascade risk..."

The response refuted its own prose, on the public API, in the same endpoint the
2026-08-28 honesty sweep had just rewritten.

What is pinned here
-------------------
The interpretation is now COMPOSED from `reductions` and `intervals`. These tests
seed panels where the answer is known by construction and assert the prose
matches: a metric that went the wrong way must be reported as having gone the
wrong way, and a metric whose interval covers zero must be marked unquotable.

If a test here fails
--------------------
Do not soften the assertion. The failure means the served sentence and the served
numbers disagree, which is the exact defect this file was written to stop.
"""
from __future__ import annotations

import pytest

from app.api.benchmark import get_benchmark_summary  # noqa: F401  (import guard)

from tests.test_benchmark_intervals import _client, _make_db, _row

#: The four resilience reductions the interpretation describes. `nominal_cost_premium_pct`
#: also carries an interval and appears in `significant_metrics`, but it is a cost premium
#: rather than a reduction and deliberately has no per-metric clause.
REDUCTIONS = (
    "stress_cascade_risk_reduction",
    "stress_cvar95_reduction",
    "targeted_cascade_risk_reduction",
    "targeted_cvar95_reduction",
)


def _panel(session, *, stress: str):
    """Nine BOMs with a real targeted effect and a controllable stress effect.

    `stress` selects what the stress panel proves:
      "worse"  — the graph-aware arm carries HIGHER risk on every differing BOM,
                 so the reduction is negative and its interval excludes zero. This
                 is the run-6 shape that used to publish "lowered".
      "better" — the sign flips; the negative control.
      "mixed"  — the differing BOMs disagree in sign, so the mean is non-zero but
                 SMALL and its interval covers zero. This is the only shape that
                 exercises the "not quotable" clause, and without it the
                 significance tests below assert nothing at all.
    """
    assert stress in {"worse", "better", "mixed"}
    for i in range(9):
        bom = f"bom_{i:02d}"
        identical = i < 2
        blind_ids, graph_ids = [1], ([1] if identical else [1, 2])
        graph_cost = 100.0 if identical else 130.0

        if identical:
            stress_b, stress_g = 0.5, 0.5
        elif stress == "worse":
            stress_b, stress_g = 0.5, 0.9  # blind - graph < 0 on every BOM
        elif stress == "better":
            stress_b, stress_g = 0.9, 0.5  # blind - graph > 0 on every BOM
        else:  # "mixed": 4 BOMs negative, 3 positive -> small mean, CI covers zero
            stress_b = 0.5
            stress_g = 0.9 if i % 2 == 0 else 0.1

        for scen, b_risk, g_risk in [
            ("nominal", 0.25, 0.25),
            ("stress", stress_b, stress_g),
            ("targeted", 1.0, 1.0 if identical else 0.5),
        ]:
            session.add(_row(bom, "milp", False, scen, 100.0, b_risk, 1.15, blind_ids))
            session.add(_row(bom, "milp", True, scen, graph_cost, g_risk, 1.15, graph_ids))
        session.add(_row(bom, "greedy", False, "nominal", 160.0, 0.3, 1.2, [1, 2, 3]))
        session.add(_row(bom, "greedy_add", False, "nominal", 140.0, 0.3, 1.2, [1, 2]))
    session.commit()


def _clause(text: str, metric: str) -> str:
    """The metric's own clause from the `Per metric:` section.

    Splitting the whole string on ";" is not enough: every metric name also appears
    in the header sentence ("...survive their interval (targeted_...)"), so a naive
    match returns the header and the assertion reads the wrong text.
    """
    assert "Per metric: " in text, f"interpretation has no per-metric section: {text!r}"
    per_metric = text.split("Per metric: ", 1)[1]
    for chunk in per_metric.split(";"):
        if chunk.strip().startswith(metric):
            return chunk.strip()
    return ""


def _resilience(stress: str):
    from app.main import app

    Session = _make_db()
    session = Session()
    _panel(session, stress=stress)
    client = _client(session)
    try:
        resp = client.get("/api/v1/benchmark/summary")
        assert resp.status_code == 200, resp.text
        return resp.json()["resilience"]
    finally:
        app.dependency_overrides.clear()
        session.close()


# ── The defect itself ─────────────────────────────────────────────────────────

def test_a_metric_that_went_the_wrong_way_is_never_described_as_lowered():
    """THE regression test. This panel is what run 6 looked like.

    The graph-aware arm carries HIGHER stress cascade risk, so the reduction is
    negative. The old hardcoded `else` branch published "the graph-aware arm
    lowered both plan cascade risk and the CVaR-95 tail" for exactly this input.
    """
    resil = _resilience("worse")
    reduction = resil["stress_cascade_risk_reduction"]
    text = resil["interpretation"]

    # Precondition: this panel really does produce the failing condition.
    assert reduction < 0, "panel did not reproduce a negative reduction"

    # The prose must name it as worse, and must not claim the opposite.
    assert "WRONG WAY" in text
    assert "stress_cascade_risk_reduction RAISED it" in text
    assert "lowered both plan cascade risk" not in text


def test_the_wrong_way_metric_is_named_not_just_counted():
    """A count without the name leaves the reader unable to act on it."""
    resil = _resilience("worse")
    text = resil["interpretation"]
    assert "stress_cascade_risk_reduction" in text.split("WRONG WAY")[1]


# ── The honest positive case still reads correctly ────────────────────────────

def test_a_genuine_reduction_is_reported_as_lowered():
    """The negative control: flip the sign and the prose must flip with it."""
    resil = _resilience("better")
    assert resil["stress_cascade_risk_reduction"] > 0
    text = resil["interpretation"]
    assert "WRONG WAY" not in text
    assert "stress_cascade_risk_reduction lowered it" in text


# ── Significance must qualify the sentence, not only the interval block ───────

def test_every_reduction_appears_in_the_prose_with_its_own_verdict():
    """No metric may be silently omitted from the summary sentence."""
    resil = _resilience("worse")
    text = resil["interpretation"]
    for metric in REDUCTIONS:
        assert metric in text, f"{metric} missing from interpretation"


def test_a_non_significant_delta_is_marked_unquotable_in_the_prose():
    """The interval block already says `significant: false`. So must the sentence.

    A reader who quotes the prose and never opens `intervals` is the reader this
    guards: the disclaimer has to travel with the claim.

    Uses the "mixed" panel deliberately. On the "worse" panel every non-zero delta
    is significant, so this loop would skip every metric and assert NOTHING while
    still reporting green — the failure mode the loop's learnings log records for 2026-08-28.
    The `checked` counter below makes that impossible to reintroduce silently.
    """
    resil = _resilience("mixed")
    text = resil["interpretation"]
    checked = 0
    for metric, ci in resil["intervals"].items():
        if metric not in REDUCTIONS or ci["significant"]:
            continue
        if abs(resil[metric]) < 1e-9:
            continue  # exact ties are described separately
        clause = _clause(text, metric)
        assert clause, f"{metric} has no clause in the interpretation"
        assert "covers zero" in clause, (
            f"{metric} is not significant but its clause does not say so: {clause!r}"
        )
        checked += 1
    assert checked, (
        "this test asserted nothing: the panel produced no non-significant, "
        "non-zero delta. Fix the panel, not this assertion."
    )


def test_prose_and_interval_block_agree_on_which_metrics_survived():
    """The two published summaries of the same fact must not disagree."""
    resil = _resilience("mixed")
    text = resil["interpretation"]
    checked = 0
    for metric in resil["significant_metrics"]:
        if metric not in REDUCTIONS or abs(resil.get(metric, 0.0)) < 1e-9:
            continue
        clause = _clause(text, metric)
        assert clause, f"{metric} is significant but has no clause in the prose"
        assert "covers zero" not in clause, (
            f"{metric} is significant but the prose disclaims it: {clause!r}"
        )
        checked += 1
    assert checked, "this test asserted nothing: no significant non-zero delta in the panel"


@pytest.mark.parametrize("stress", ["worse", "better", "mixed"])
def test_the_sentence_never_contradicts_the_sign_of_any_reduction(stress):
    """Property form: for every metric, the verb must match the sign."""
    resil = _resilience(stress)
    text = resil["interpretation"]
    for metric in REDUCTIONS:
        value = resil[metric]
        if abs(value) < 1e-9:
            continue
        expected = "lowered it" if value > 0 else "RAISED it"
        assert f"{metric} {expected}" in text, (
            f"{metric} = {value} but the prose does not say {expected!r}"
        )
