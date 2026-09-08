"""The `/frontier` PAGE must agree with `docs/cvar_frontier.json`.

WHY THIS FILE EXISTS (2026-09-01)
---------------------------------
`FrontierPage.tsx` publishes a block of numbers that describe the OFFLINE study, not the
live API response: the sensitivity-grid tile, the "offline study behind this page"
paragraph, the solver-budget caveat, the VSS tile and the "surcharge it replaced" tile.
Every one of them is a literal typed into JSX. Nothing regenerated them, and nothing
compared them to anything.

The result shipped to production and stayed there for five days: the tile said

    31 / 36   ... and 11 of 12 in the arm that removes centrality from the model entirely

while the committed artifact said **35 of 36** cells have a knee, **12 of 12** in the
centrality-ignored arm (`centrality_spread = 1.0`), and the single knee-less cell is in
the `centrality_spread = 3.0` arm — so even the attribution named the wrong arm. The
page had never matched ANY version of the artifact.

`test_cvar_doc_matches_artifact.py` did not catch it and structurally could not: it is a
DOC-vs-ARTIFACT check, and the page is neither. This repo has been bitten three times by
exactly that blind spot — two documents agreeing with each other while both disagree with
the thing that produced them. So this file deliberately compares **what the page renders**
against **the artifact**, with no document in between.

HOW IT WORKS
------------
Each pinned claim lives inside an element carrying a stable ``data-testid``. ``tests/_jsx``
pulls that element out of the `.tsx` source, strips the JSX tags and `{' '}` spacers, and
collapses the result to the plain text a browser would show. Every number is then regexed
out of that text and compared to a field of `docs/cvar_frontier.json`.

Those helpers moved out of this file on 2026-09-05 (behaviour unchanged) so the
class-level guard, ``test_pages_do_not_publish_unverified_numbers.py``, could reuse them.
That guard asks the inverse question of every page: not "does this pinned number match its
artifact?" but "does every number this page prints have anything standing behind it at
all?" — and it treats the anchors listed in ``PINNED_TESTIDS`` below as verified precisely
because this file reads them.

Consequences worth knowing before you "fix" a failure here:

* Regenerating the artifact WILL turn these red. That is the point — the solver telemetry
  (387 / 347 / 40, the 15-unit / 80-unit deterministic budgets, the 95% worst gap) moves
  when the budget or the model moves. Red here means the page has to be re-typed to the
  new artifact, not that the test is wrong.
* Deleting a ``data-testid`` turns these red too, loudly, rather than silently skipping.

THE SOLVER-BUDGET PIN MOVED FIELDS ON 2026-09-01
------------------------------------------------
``test_the_solver_budget_caveat_matches_the_solver_block`` used to read the page's
"15 s / 60 s" against ``meta.solver.max_time_in_seconds_*``. Those fields still exist,
but the sweep now runs on ``max_deterministic_time`` — a WORK budget — and the wall-clock
fields became a **runaway guard** sitting twenty times clear of it (300 s / 1,600 s). An
assertion phrased purely in seconds would therefore have forced the page to publish
"300 s per solve", which is true of no solve in the artifact. The pin was re-pointed at
the field that actually binds, and gained the two checks that make the page's new
REPRODUCIBILITY claim falsifiable: ``deterministic_budget_in_force`` must be true, and
``n_wall_clock_bound`` must be 0.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests._jsx import rendered as _rendered
from tests._jsx import strip_comments

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent

ARTIFACT = REPO_ROOT / "docs" / "cvar_frontier.json"
PAGE = REPO_ROOT / "frontend" / "src" / "pages" / "FrontierPage.tsx"

#: Number words the page spells out instead of digitising.
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def artifact() -> dict:
    if not ARTIFACT.is_file():
        pytest.skip(f"{ARTIFACT} not generated; run `python -m seeds.run_cvar_frontier`")
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def page_source() -> str:
    if not PAGE.is_file():  # pragma: no cover - guards a moved file, not a branch
        pytest.skip(f"{PAGE} is not present in this checkout")
    return PAGE.read_text(encoding="utf-8")


# ── Extracting the rendered text of one element out of the JSX ───────────────
#
# The extraction itself now lives in `tests/_jsx.py`, because
# `test_pages_do_not_publish_unverified_numbers.py` needs the same JSX-to-text step to
# walk EVERY page. Nothing about the behaviour here changed: `rendered()` still returns
# the plain text a browser would show for the element carrying `testid`, and a missing
# anchor still raises rather than skipping.


def rendered(source: str, testid: str) -> str:
    return _rendered(source, testid, where=PAGE.name)


def _one(pattern: str, text: str, testid: str) -> re.Match[str]:
    m = re.search(pattern, text)
    assert m is not None, (
        f"the text rendered by data-testid={testid!r} no longer matches {pattern!r}.\n"
        f"rendered text was:\n  {text}"
    )
    return m


def _fmt_lambda(value: float) -> str:
    """0.2 -> '0.2', 0.25 -> '0.25', 1.0 -> '1'. Matches how the page writes λ."""
    return f"{value:g}"


# ── Derived truth, computed from the artifact only ───────────────────────────


@pytest.fixture(scope="module")
def sensitivity(artifact: dict) -> dict:
    rows = artifact["sensitivity"]["rows"]
    grid = artifact["sensitivity"]["grid"]
    #: "removes centrality from the model entirely" == the flat arm, the smallest
    #: spread on the grid, which by construction is 1.0 (every supplier on the same
    #: cited base rate).
    flat_spread = min(grid["centrality_spread"])
    flat_rows = [r for r in rows if r["centrality_spread"] == flat_spread]
    missing = [r for r in rows if r.get("knee_lambda") is None]
    return {
        "n_cells": len(rows),
        "n_with_knee": sum(1 for r in rows if r.get("knee_lambda") is not None),
        "flat_spread": flat_spread,
        "n_flat_cells": len(flat_rows),
        "n_flat_with_knee": sum(1 for r in flat_rows if r.get("knee_lambda") is not None),
        "n_missing": len(missing),
        "missing_spreads": sorted({r["centrality_spread"] for r in missing}),
    }


# ── 1. The sensitivity-grid tile — the claim that actually shipped wrong ─────


def test_the_sensitivity_tile_headline_counts_the_cells_with_a_knee(
    page_source: str, sensitivity: dict
) -> None:
    """`35 / 36` must be `knee_lambda is not None` counted over `sensitivity.rows`.

    Shipped as `31 / 36`. 31 is not the count of anything in the artifact.
    """
    text = rendered(page_source, "frontier-sensitivity-knee-cells")
    m = _one(r"^(\d+)\s*/\s*(\d+)$", text, "frontier-sensitivity-knee-cells")
    assert (int(m.group(1)), int(m.group(2))) == (
        sensitivity["n_with_knee"],
        sensitivity["n_cells"],
    ), (
        f"the /frontier sensitivity tile publishes {text!r}, but "
        f"docs/cvar_frontier.json has a knee in {sensitivity['n_with_knee']} of "
        f"{sensitivity['n_cells']} sensitivity rows."
    )


def test_the_sensitivity_tile_counts_the_centrality_ignored_arm(
    page_source: str, sensitivity: dict
) -> None:
    """`12 of 12 in the arm that removes centrality` == the `centrality_spread = 1.0` arm.

    Shipped as `11 of 12`. The centrality-ignored arm is fully intact in the artifact;
    the one knee-less cell is in a different arm entirely (see the next test).
    """
    testid = "frontier-sensitivity-knee-caption"
    text = rendered(page_source, testid)
    m = _one(
        r"(\d+) of (\d+) in the arm that removes centrality from the model entirely",
        text,
        testid,
    )
    assert (int(m.group(1)), int(m.group(2))) == (
        sensitivity["n_flat_with_knee"],
        sensitivity["n_flat_cells"],
    ), (
        f"the /frontier sensitivity tile says {m.group(0)!r}, but the "
        f"centrality_spread = {sensitivity['flat_spread']} arm of "
        f"docs/cvar_frontier.json has a knee in {sensitivity['n_flat_with_knee']} of "
        f"{sensitivity['n_flat_cells']} cells."
    )


def test_the_sensitivity_tile_names_the_centrality_ignored_arm_correctly(
    page_source: str, sensitivity: dict
) -> None:
    """The arm the page calls "removes centrality" must be the flat one on the grid."""
    testid = "frontier-sensitivity-knee-caption"
    text = rendered(page_source, testid)
    m = _one(
        r"removes centrality from the model entirely \( centrality_spread = ([\d.]+) \)",
        text,
        testid,
    )
    assert float(m.group(1)) == sensitivity["flat_spread"], (
        f"the page calls centrality_spread = {m.group(1)} the centrality-ignored arm, "
        f"but the flat arm on the artifact's grid is {sensitivity['flat_spread']}."
    )


def test_the_sensitivity_tile_attributes_the_missing_knee_to_the_right_arm(
    page_source: str, sensitivity: dict
) -> None:
    """The knee-less cell's arm must be the one the artifact actually puts it in.

    The page used to imply it sat in the centrality-ignored arm ("11 of 12 in the arm
    that removes centrality"). It does not: it is `base_annual_prob = 0.05,
    centrality_spread = 3.0, horizon_days = 30`.
    """
    testid = "frontier-sensitivity-knee-caption"
    text = rendered(page_source, testid)
    m = _one(
        r"The (\w+) cells? with no knee (?:is|are) in the centrality_spread = ([\d.]+) arm",
        text,
        testid,
    )
    count_word = m.group(1).lower()
    claimed = NUMBER_WORDS.get(count_word, None)
    if claimed is None and count_word.isdigit():
        claimed = int(count_word)
    assert claimed == sensitivity["n_missing"], (
        f"the page says {count_word!r} cell(s) have no knee; the artifact has "
        f"{sensitivity['n_missing']}."
    )
    assert [float(m.group(2))] == sensitivity["missing_spreads"], (
        f"the page attributes the knee-less cell(s) to centrality_spread = "
        f"{m.group(2)}, but the artifact puts them in "
        f"{sensitivity['missing_spreads']}."
    )


# ── 2. "The offline study behind this page" paragraph ────────────────────────


def test_the_offline_study_paragraph_matches_the_solve_quality_block(
    page_source: str, artifact: dict
) -> None:
    """387 / 347 / 40, the grid size, the BOM count and the 95% worst gap.

    These are solver telemetry. Since 2026-09-01 they reproduce across runs (the sweep
    is on a deterministic work budget), but they still move when the budget, the model
    or the data moves. When they do, this test goes red and the page must be re-typed to
    the new artifact.
    """
    testid = "frontier-offline-study-summary"
    text = rendered(page_source, testid)
    sq = artifact["solve_quality"]

    m = _one(r"of (\d+) λ-solves across (\w+) reference BOMs", text, testid)
    assert int(m.group(1)) == sq["n_solves"], (
        f"page: {m.group(1)} λ-solves; artifact solve_quality.n_solves = {sq['n_solves']}"
    )
    assert NUMBER_WORDS[m.group(2).lower()] == len(artifact["breadth"]), (
        f"page: {m.group(2)} reference BOMs; artifact breadth has "
        f"{len(artifact['breadth'])}"
    )

    m = _one(r"a (\d+)-cell sensitivity grid", text, testid)
    assert int(m.group(1)) == len(artifact["sensitivity"]["rows"])

    m = _one(r"Of those, (\d+) converged and (\d+) did not", text, testid)
    assert (int(m.group(1)), int(m.group(2))) == (
        sq["n_converged"],
        sq["n_not_converged"],
    ), (
        f"page: {m.group(1)} converged / {m.group(2)} did not; artifact: "
        f"{sq['n_converged']} / {sq['n_not_converged']}"
    )

    m = _one(r"could be (\d+)% away from the unknown optimum", text, testid)
    worst = max(arm["worst_gap_pct"] for arm in sq["by_arm"].values())
    assert int(m.group(1)) == round(worst), (
        f"page: {m.group(1)}% worst gap; artifact worst arm gap = {worst}%"
    )


def test_the_page_reproduces_the_primary_arm_and_says_so_accurately(
    page_source: str, artifact: dict
) -> None:
    """`27 of 27 ... at a worst MIP gap of 0.000%` is the PRIMARY arm of `by_arm`."""
    testid = "frontier-offline-study-summary"
    text = rendered(page_source, testid)
    primary = artifact["solve_quality"]["by_arm"]["primary"]

    m = _one(r"(\d+) of (\d+) λ-solves returned OPTIMAL", text, testid)
    assert (int(m.group(1)), int(m.group(2))) == (
        primary["n_converged"],
        primary["n_solves"],
    )

    m = _one(r"worst MIP gap of ([\d.]+)%", text, testid)
    assert float(m.group(1)) == pytest.approx(primary["worst_gap_pct"], abs=5e-4), (
        f"page: worst MIP gap {m.group(1)}%; artifact by_arm.primary.worst_gap_pct = "
        f"{primary['worst_gap_pct']}"
    )


# ── 3. The solver-budget caveat ──────────────────────────────────────────────


def test_the_solver_budget_caveat_matches_the_solver_block(
    page_source: str, artifact: dict
) -> None:
    """`15 units per solve in the breadth arm, 80 in the primary arm` == `meta.solver`.

    Pinned to ``max_deterministic_time_*`` — the budget that decides where a solve
    stops — and NOT to ``max_time_in_seconds_*``, which is the wall-clock runaway guard
    behind it. See the note in this module's docstring for why the field moved.
    """
    testid = "frontier-solve-quality-caveat"
    text = rendered(page_source, testid)
    solver = artifact["meta"]["solver"]

    m = _one(
        r"([\d.]+) units per solve in the breadth arm, ([\d.]+) in the primary arm",
        text,
        testid,
    )
    assert float(m.group(1)) == solver["max_deterministic_time_breadth"], (
        f"page: {m.group(1)} units breadth budget; artifact "
        f"meta.solver.max_deterministic_time_breadth = "
        f"{solver['max_deterministic_time_breadth']}"
    )
    assert float(m.group(2)) == solver["max_deterministic_time_primary"], (
        f"page: {m.group(2)} units primary budget; artifact "
        f"meta.solver.max_deterministic_time_primary = "
        f"{solver['max_deterministic_time_primary']}"
    )


def test_the_pages_reproducibility_claim_is_true_of_the_artifact(
    page_source: str, artifact: dict
) -> None:
    """The page now tells the reader the counters reproduce. That must be EARNED.

    Two artifact fields carry the whole claim, and if either flips the sentence on
    screen becomes false:

    * ``deterministic_budget_in_force`` — the sweep ran on a WORK budget at all;
    * ``n_wall_clock_bound`` — how many solves the wall-clock runaway guard stopped
      anyway. Any nonzero value means those solves stopped wherever a stopwatch
      happened to land, so their counters are load-dependent and the page is lying.

    This is the falsifiable half of the caveat. Without it the page could keep
    claiming reproducibility across a regeneration that silently lost it.
    """
    sq = artifact["solve_quality"]
    assert sq["deterministic_budget_in_force"] is True, (
        "the /frontier page tells the reader the solve-quality counters reproduce, but "
        "docs/cvar_frontier.json says solve_quality.deterministic_budget_in_force is "
        f"{sq['deterministic_budget_in_force']!r} — the sweep ran on the wall clock and "
        "the counters are a run log of one machine. Re-word the page or re-run the "
        "sweep with --breadth-det-limit / --primary-det-limit."
    )
    assert sq["n_wall_clock_bound"] == 0, (
        f"solve_quality.n_wall_clock_bound = {sq['n_wall_clock_bound']}: the wall-clock "
        "runaway guard, not the deterministic work budget, decided where that many "
        "solves stopped. Their counters are load-dependent, so the /frontier page's "
        "\"these counts reproduce\" claim is false for this artifact."
    )

    text = rendered(page_source, "frontier-solve-quality-caveat")
    assert re.search(r"deterministic", text, re.I), (
        "the caveat no longer tells the reader the budget is deterministic WORK rather "
        "than elapsed time, which is the only reason the counters reproduce."
    )
    assert re.search(r"elapsed time", text, re.I), (
        "the caveat must keep saying that elapsed time does NOT reproduce. A "
        "deterministic budget fixes where the search stops, never how long the machine "
        "took to get there, and every solve_seconds / sweep_wall_seconds in the "
        "artifact is still a run log of one machine."
    )


def test_the_page_never_quotes_the_runaway_guard_as_the_per_solve_budget(
    page_source: str, artifact: dict
) -> None:
    """`max_time_in_seconds_*` is a backstop, not a budget, and must not be published.

    The trap this guards: the budget pin above used to read those fields, so the
    obvious way to make it green after the regeneration was to re-type the page to
    "300 s / 1600 s". No solve in the artifact runs anywhere near either number — the
    worst one in the whole run used 8.6 s of wall clock against the 300 s guard — so
    publishing them would replace a stale figure with a fabricated one.
    """
    solver = artifact["meta"]["solver"]
    text = rendered(page_source, "frontier-solve-quality-caveat")
    for field in ("max_time_in_seconds_breadth", "max_time_in_seconds_primary"):
        guard = solver[field]
        assert not re.search(rf"\b{guard:g}\s*s\b", text), (
            f"the /frontier solver caveat publishes {guard:g} s, which is "
            f"meta.solver.{field} — the WALL-CLOCK RUNAWAY GUARD, not the per-solve "
            "budget. The budget that binds is meta.solver.max_deterministic_time_*."
        )


# ── 4. The value-of-the-stochastic-solution tile ─────────────────────────────


def test_the_vss_tile_matches_the_artifact(page_source: str, artifact: dict) -> None:
    vss = artifact["primary"]["x10000"]["value_of_the_stochastic_solution"]

    headline = rendered(page_source, "frontier-vss-usd")
    m = _one(r"^\$([\d,]+)$", headline, "frontier-vss-usd")
    assert int(m.group(1).replace(",", "")) == round(vss["VSS_usd"]), (
        f"page: {headline}; artifact VSS_usd = {vss['VSS_usd']}"
    )

    caption = rendered(page_source, "frontier-vss-caption")
    m = _one(r"^([\d.]+)% of spend", caption, "frontier-vss-caption")
    assert float(m.group(1)) == pytest.approx(round(vss["VSS_pct_of_RP"], 2), abs=5e-3), (
        f"page: {m.group(1)}% of spend; artifact VSS_pct_of_RP = {vss['VSS_pct_of_RP']}"
    )


# ── 5. Where the shipped 15% heuristic lands on the curve ────────────────────


def test_the_shipped_plan_lands_where_the_artifact_says_it_lands(
    page_source: str, artifact: dict
) -> None:
    """`λ ≈ 0.2` is `baselines[shipped_milp_graph_aware=False].nearest_lambda`.

    Shipped as `λ ≈ 0.10`, copied from hand-written prose in
    `docs/CVAR_EFFICIENT_FRONTIER.md` that the document's own GENERATED baselines
    table ("Sits at λ ≈ **0.2**") already contradicted. 0.10 is not the value of any
    field in the artifact.
    """
    baselines = artifact["primary"]["x10000"]["baselines"]
    shipped = next(
        b for b in baselines if b["plan"] == "shipped_milp_graph_aware=False"
    )
    testid = "frontier-shipped-plan-nearest-lambda"
    text = rendered(page_source, testid)
    m = _one(r"λ ≈ ([\d.]+)", text, testid)
    assert m.group(1) == _fmt_lambda(shipped["nearest_lambda"]), (
        f"page: λ ≈ {m.group(1)}; artifact nearest_lambda for the shipped MILP plan = "
        f"{shipped['nearest_lambda']}"
    )


# ── 6. The anchors this file depends on must all still exist ─────────────────


PINNED_TESTIDS = (
    "frontier-sensitivity-knee-cells",
    "frontier-sensitivity-knee-caption",
    "frontier-offline-study-summary",
    "frontier-solve-quality-caveat",
    "frontier-vss-usd",
    "frontier-vss-caption",
    "frontier-shipped-plan-nearest-lambda",
)


@pytest.mark.parametrize("testid", PINNED_TESTIDS)
def test_every_pinned_anchor_is_still_in_the_page(page_source: str, testid: str) -> None:
    """A pin that silently stops finding its element is a check that cannot fail."""
    assert page_source.count(f'data-testid="{testid}"') == 1, (
        f"{PAGE.name} must contain exactly one data-testid={testid!r}; the artifact "
        f"pins in this file read the published number through it."
    )


def test_the_retired_wrong_literals_are_gone(page_source: str) -> None:
    """The exact strings that were live and wrong, or live and superseded.

    ``31 / 36``, ``11 of 12`` and ``λ ≈ 0.10`` were live and WRONG from 2026-08-27 to
    2026-09-01. ``349 converged`` / ``38 did not`` and the ``15 s`` / ``60 s`` budget
    copy were correct for the wall-clock vintage and were superseded by the
    deterministic-budget regeneration on 2026-09-01; they are listed here so a revert
    or a copy-paste cannot quietly reinstate them.

    Comments are stripped first: the file's own header names these strings when it
    explains why they were retired, and that prose renders nothing.
    """
    code = strip_comments(page_source)
    for retired in (
        "31 / 36", "11 of 12", "λ ≈ 0.10",
        "349 converged", "38 did not",
        "15 s per solve", "60 s in the primary arm",
    ):
        assert retired not in code, (
            f"{PAGE.name} still contains {retired!r}, which contradicts "
            f"docs/cvar_frontier.json."
        )
