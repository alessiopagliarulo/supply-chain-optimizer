"""No page may render a number that nothing verifies.

WHY THIS FILE EXISTS (2026-09-05)
---------------------------------
`test_frontier_page_matches_cvar_artifact.py` exists because `FrontierPage.tsx` shipped
``31 / 36`` and ``11 of 12`` for five days while `docs/cvar_frontier.json` said ``35 of 36``
and ``12 of 12``, and the attribution named the wrong experimental arm. The page had never
matched ANY version of the artifact, and a doc-vs-artifact test structurally could not see
it, because the page is neither.

That test fixed ONE page. It cannot fix the class, because it only knows the seven
`data-testid` anchors someone remembered to add. The generalisation is the inverse
question: instead of "does this pinned number match its artifact?", ask

    **does EVERY number this page renders have anything at all standing behind it?**

This file asks that of all thirteen pages. A rendered number passes only if it is

  (a) inside an element carrying a ``data-testid`` that a pin test actually asserts
      against an artifact (see ``PINNED``), or
  (b) named in ``GLOBAL_ALLOW`` / ``PAGE_ALLOW`` below, each entry carrying the reason it
      cannot drift — a unit, a definition, a citation, a past-tense date, or a figure
      pinned by name in ``test_pages_match_their_sources.py``.

Everything else fails, naming the file, the line, the residual number and the text around
it. The fix for a failure is to bind the number to an artifact/API field or to add an
allowlist entry with a real justification — never to widen a pattern until it goes quiet.

WHAT COUNTS AS "RENDERED"
-------------------------
`_jsx.text_nodes` (see that module for the parser and the two traps it handles) returns
only JSX **text nodes**. Class names, `data-*`, `aria-*`, `key=`, SVG geometry, import
paths, chart config, comments and `{...}` expressions are all excluded: a number that
reaches the screen through an expression came from props, state or the API and cannot
drift away from the backend the way a typed literal can.

To that this file adds the handful of **static string props that a browser shows to a
sighted reader** — `title` (the native tooltip), `hint`, `subtitle`, `caption`, `label`,
`placeholder`. `NewsvendorPage.tsx` publishes "51 monthly observations" through `hint=`
and `CheckoutPage.tsx` publishes a 25%/yr holding rate through `title=`; both are claims,
and both would be invisible to a text-node-only scan.

THREE GAPS THIS GUARD DOES NOT CLOSE — stated so nobody mistakes green for total
--------------------------------------------------------------------------------
1. **Literals that reach the screen through a JS string constant.** `ResiliencePage.tsx`
   renders "1,000 Monte Carlo scenarios" from ``const MC_SCENARIOS = 1000``, and
   `MapPage.tsx` renders a ``?? 92`` fallback distributor count inside a template literal.
   Both are typed literals that can go stale; neither is a JSX text node. Scanning every
   string in a `.tsx` file would drown the guard in false positives, so those two are
   pinned by name in `test_pages_match_their_sources.py` instead.
2. **`aria-label`.** Screen-reader text is published text, and BenchmarkPage's aria-labels
   do carry figures ("roughly 47 percent on toy orders"). They are excluded here only
   because the scope of this guard is what a sighted reader sees; that is a deliberate,
   known hole, not an oversight.
3. **The testid exemption is per-ELEMENT, not per-number.** `frontier-solve-quality-caveat`
   is pinned, so every number inside it is exempt — including "load averages 2.5, 43.5 and
   2.6", which the pin test does not assert. Criterion (a) says "an anchor a pin test
   reads", and it cannot know which digits inside that anchor the assertions actually
   cover.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._jsx import JsxText, decode_entities, text_nodes
from tests.test_frontier_page_matches_cvar_artifact import PINNED_TESTIDS

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = BACKEND_ROOT.parent / "frontend" / "src" / "pages"

#: Every page under `frontend/src/pages`. Discovered, never listed: a new page must be
#: triaged, not silently exempt because someone forgot to add it here.
PAGES = sorted(PAGES_DIR.glob("*.tsx"))

#: `data-testid` anchors a pin test reads against an artifact. Imported, not retyped, so
#: dropping a pin cannot quietly widen this guard's exemptions.
PINNED: dict[str, frozenset[str]] = {
    "FrontierPage.tsx": frozenset(PINNED_TESTIDS),
}

#: Static string props a browser shows to a sighted reader. `aria-*` and `alt` are
#: deliberately absent — see gap 2 in the module docstring.
VISIBLE_PROPS = ("title", "hint", "subtitle", "caption", "label", "placeholder")
#: A number as a reader would see it, for the failure message.
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

_PROP_RE = re.compile(
    rf'(?<![\w-])({"|".join(VISIBLE_PROPS)})\s*=\s*"([^"]*)"', re.IGNORECASE
)


# ── The allowlist ────────────────────────────────────────────────────────────
#
# Each entry is (phrase, why it cannot drift). The phrase is deleted from the rendered
# text before the digit check, so it must be specific enough to name ONE claim: an entry
# of "5" would silently license every 5 on the page.
#
# A phrase earns a place here only if it is a unit, a definition, a fixed convention, a
# citation, a past-tense date, or a figure pinned by name in another test. "It looked
# fine" is not a justification.

GLOBAL_ALLOW: tuple[tuple[str, str], ...] = (
    ("CVaR-95", "the NAME of the risk measure. 95 is the tail level in its definition."),
    ("CVaR₉₅", "the same measure, written with a subscript."),
    ("VaR-95", "the NAME of the companion quantile measure."),
    ("CVaR₉₅[cost]", "the objective term of the model, written as maths."),
    ("(1−λ)·E[cost] + λ·", "the objective FORMULA. 1 is the coefficient, not a result."),
    ("95% CI", "the confidence level the intervals are built at, fixed by protocol."),
    ("P10", "a percentile label: the 10th. A name, not a value."),
    ("P50", "a percentile label: the median. A name, not a value."),
    ("P90", "a percentile label: the 90th. A name, not a value."),
    ("worst-5%", "the CVaR-95 tail, i.e. the definition of the measure above."),
    ("worst 5%", "the same tail, spelled with a space."),
    ("5% tail", "the same tail, named as a noun."),
    ("0–1", "the range of a share. A scale, not a measurement."),
    ("0-1", "the same range, written with a hyphen."),
    ("CO2", "a chemical formula. The 2 is a subscript, not a quantity."),
    ("CO₂", "the same formula with a real subscript character."),
    ("1×", "the benchmark's nominal order size — the unit the sweep multiplies."),
    ("λ = 1", "the endpoint of the λ grid by definition: pure CVaR."),
    ("k = 1", "the single-supplier end of the diversification sweep, by definition."),
    ("k=1", "the same reference point, written without spaces."),
    ("k−1", "the previous step of the k sweep. An index, not a measurement."),
)

PAGE_ALLOW: dict[str, tuple[tuple[str, str], ...]] = {
    "BenchmarkPage.tsx": (
        (
            "0.0000× delta",
            "the value a saturated CVaR ratio takes by arithmetic, printed at the "
            "page's own 4-dp precision. Not a measurement: the sentence exists to say "
            "the metric cannot resolve anything here.",
        ),
        (
            "an exact 0 to the delta",
            "arithmetic. Two arms at the same ceiling differ by zero by construction.",
        ),
        (
            "an exact 0.0 to every delta",
            "same claim, one decimal place.",
        ),
        (
            "seed=42",
            "the RNG seed of the benchmark run — an input, not a result. Pinned to "
            "`meta.seed` where the artifact carries one; see the run_id caveat below.",
        ),
        (
            "Seed 42",
            "same seed, prose spelling.",
        ),
        (
            "until 2026-09-03",
            "a past-tense changelog date. What this page served before that date can "
            "never change.",
        ),
        (
            "on 2026-09-03",
            "same: the date the pool-matched arms entered the pipeline.",
        ),
        (
            "10-BOM cohort",
            "pinned to len(docs/volume_sweep.json['boms']) by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "9-BOM cohort",
            "pinned to docs/benchmark_results.json headline.n_boms_in_tables by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "on nine BOMs",
            "pinned to the same field. NOTE: the very next sentence renders the live "
            "{summary.n_boms}; this word is the hardcoded twin of it and is only "
            "correct while the cohort stays at nine.",
        ),
        (
            "stress_factor=3",
            "pinned to docs/benchmark_results.json meta.stress_factor by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "over 100%",
            "arithmetic: a share of a total whose other terms are net losses must "
            "exceed 100%. The share itself is rendered from the API beside it.",
        ),
        (
            "0, .25, .5, .75, 1",
            "the complete set of values a 4-line BOM's unfulfilled-line share can take. "
            "Enumeration of a definition, not a measurement.",
        ),
        (
            "an unqualified −8 pp",
            "the rounded form of the API-rendered delta immediately before it, quoted to "
            "say why it must NOT be claimed. Editorial, and the guard cannot bind it to "
            "the live value it paraphrases.",
        ),
        (
            "carries a 95%",
            "the confidence level of the bootstrap intervals, fixed by protocol. The "
            "noun it qualifies continues in the next element, so 'CI' is not adjacent.",
        ),
        (
            "~0 finding",
            "a direction, not a figure: the sentence's whole point is that the measured "
            "effect is indistinguishable from zero.",
        ),
        (
            "Top-5 highest-betweenness",
            "the removal budget of the sequential-attack sweep, fixed by the endpoint.",
        ),
        (
            "2 of the",
            "the count of tied BOMs, followed immediately by the live {summary.n_boms}. "
            "Hardcoded numerator over a served denominator; correct today, and listed "
            "here rather than pinned because the artifact publishes no tied-BOM count.",
        ),
        ("Δ cost vs", "an axis label; the 'cost' carries no figure."),
        (
            "mc_cvar_95 / baseline_cvar_95",
            "API field NAMES quoted in a tooltip, not values.",
        ),
        (
            "plan_cascade_risk = 1 −",
            "the definition of the field, written as maths.",
        ),
        (
            "4-line BOMs",
            "the arity of the reference BOMs this enumeration applies to; the "
            "enumeration above is only meaningful with it.",
        ),
        (
            "roughly 47 percent on toy orders",
            "the retracted headline, quoted inside an aria-label so a screen-reader user "
            "gets the same retraction a sighted one does. 47.25% is the artifact's "
            "total_save_pct_vs_greedy.",
        ),
        (
            "0 to 1",
            "the range of the right axis, spelled out for a screen reader.",
        ),
        (
            "6 steps from 0 to 5 distributors removed",
            "the shape of the sequential-removal sweep, spelled out for a screen reader.",
        ),
        (
            "Expect ~0",
            "the null hypothesis this tile tests, not a reading.",
        ),
    ),
    "CheckoutPage.tsx": (
        (
            "4 route strategies",
            "pinned to len(app.optimization.strategies.STRATEGIES) by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "within $1, 0.05 days, 0.05 kg and 0.5 km",
            "the TIE_FLOOR constants declared at the top of this same file — the page "
            "explaining its own tie rule. Pinned to them by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "at a 25%/yr electronics holding rate (Gartner IT Supply Chain Benchmarks "
            "2022). Holding $ = component value × 25% × (lead-time days ÷ 365)",
            "a cited cost ASSUMPTION and its formula: the rate is an input the backend "
            "uses, 2022 is the citation year, 365 is days in a year.",
        ),
    ),
    "Dashboard.tsx": (
        (
            "Top 5 by catalogue risk index",
            "the length of the list under it — `.slice(0, 5)` in this same file. Pinned "
            "by test_pages_match_their_sources.py.",
        ),
        (
            "every 15 min",
            "the external-feed refresh cadence, a configured interval rather than a "
            "measurement.",
        ),
    ),
    "FrontierPage.tsx": (
        (
            "the flat 15% risk surcharge this replaced",
            "pinned to app.optimization.sourcing.RISK_PREMIUM_RATE by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "The 15% surcharge it replaced",
            "same constant, tile heading.",
        ),
        (
            "7 λ points",
            "pinned to len(app.api.stochastic.LAMBDA_GRID) by "
            "test_pages_match_their_sources.py — the grid the live endpoint solves.",
        ),
        (
            "between spread 1.0 and 3.0",
            "pinned to the two lowest centrality_spread arms of the artifact's "
            "sensitivity grid by test_pages_match_their_sources.py.",
        ),
        (
            "how much worse the bad 5% is than the average case",
            "the CVaR-95 tail restated in words for a tooltip — the definition of the "
            "measure, not a reading of it.",
        ),
        (
            "Artzner et al. 1999",
            "an academic citation year.",
        ),
        (
            "Satopää et al. 2011",
            "an academic citation year.",
        ),
        (
            "a probability of 1.0",
            "the ceiling of a probability. The sentence says the fix cannot reach it.",
        ),
        (
            "nowhere near 1.0",
            "same ceiling, describing the calibrated model's headroom below it.",
        ),
        (
            "made the single most central distributor fail in 100% of scenarios, because "
            "a min-max rescale always attains 1.0 at its maximum",
            "the defect being described: a min-max rescale attains 1.0 at its maximum by "
            "construction. Arithmetic about a RETIRED model.",
        ),
        (
            "1/spread ×",
            "the lower bound of the bounded rank transform, written as maths.",
        ),
        (
            "Tail removed per $1 spent",
            "the unit of the column: dollars of tail per dollar spent.",
        ),
    ),
    "LandingPage.tsx": (
        (
            "static 2024",
            "the year the underlying Nexar/Octopart dataset was COLLECTED, recorded at "
            "backend/seeds/seed_db.py `DATASET_COLLECTED` (\"2024 (per dataset card: 404 "
            "general components + 387 telecom components)\"). A past-tense collection "
            "date: the snapshot was gathered in 2024 and always will have been, so it "
            "cannot drift. It is on the page precisely BECAUSE `distributor_offers` has "
            "no date column of any kind -- freshness is unfalsifiable from the data, so "
            "the page states the vintage instead of implying the offers are live. "
            "`test_landing_data_contract.py::test_the_offers_are_never_described_as_live` "
            "fails if that wording regresses, and also fails if a date column ever "
            "appears (which would mean this justification needs revisiting).",
        ),
    ),
    "MapPage.tsx": (
        (
            "Top 10% (decile) by betweenness",
            "the top band of this page's own percentile split; pinned to the p90 "
            "threshold in this file by test_pages_match_their_sources.py.",
        ),
        (
            "Next 30% by betweenness",
            "the middle band, p60–p90. Same pin.",
        ),
        (
            "Bottom 60% by betweenness",
            "the bottom band, below p60. Same pin.",
        ),
        (
            "top 10% / next 30% / bottom 60%",
            "the same three bands restated in prose. Same pin.",
        ),
        (
            "absolute 0–100% BOM-collapse percentage",
            "the range of a percentage — the sentence says this layer is NOT one.",
        ),
        (
            "a fixed threshold like 0.4 could never be reached",
            "an illustrative threshold, chosen to be above the observed maximum rendered "
            "immediately before it. Not a claim about the data.",
        ),
    ),
    "ModelCardPage.tsx": (
        (
            "can only ever emit 0 or 1",
            "the output set of a persistence classifier, by definition.",
        ),
        (
            "1.0 = perfectly calibrated",
            "the definition of a calibration slope, not a measured slope.",
        ),
    ),
    "NewsvendorPage.tsx": (
        (
            "takes over four minutes to precompute",
            "Bound to docs/newsvendor.json meta.wall_seconds > 240 by "
            "test_pages_match_their_sources.py::"
            "test_the_newsvendor_precompute_cost_is_the_sweep_the_artifact_timed. "
            "Deliberately a threshold, not a digit: wall_seconds is machine speed "
            "(255.2 -> 268.7 on a regeneration) and a hand-synced number is how the "
            "fabricated '108 s per setting' got here in the first place.",
        ),

        (
            "Snyder & Daskin (2005)",
            "an academic citation year.",
        ),
        (
            "Scarf (1958)",
            "an academic citation year.",
        ),
        (
            "a number above 0 and at most 1,000,000",
            "the input validation bounds enforced in this same file.",
        ),
        (
            "a number between 0 and 10,000",
            "the input validation bounds enforced in this same file.",
        ),
        (
            "12 to 600 non-negative counts",
            "the pasted-history bounds enforced in this same file.",
        ),
        (
            "0, 0, 2, 0, 1, 0, 0, 0, 3, 0, 0, 1, 0, 4, 0, 0, 1, 0",
            "a placeholder showing the INPUT FORMAT. Example data, labelled as such by "
            "being a placeholder.",
        ),
        (
            "a 5,000-replication paired bootstrap",
            "pinned to docs/newsvendor.json meta.n_boot by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "all 72 settings",
            "pinned to meta.evaluation_grid.n_configurations by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "the fractile is τ = 0.9931. The longest training window in this panel is 45 "
            "monthly observations, so the finest quantile the data can resolve is 1/45 = "
            "0.022",
            "every figure here is pinned to docs/newsvendor.json "
            "(sensitivity_line_down.costs.critical_ratio and protocol.train_sizes) by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "a 99.3rd percentile",
            "the same critical fractile as a percentile. Pinned with it.",
        ),
        (
            "T1 – T2674, 51 monthly observations each",
            "pinned to panel.n_series_available and to train_sizes + horizon_months by "
            "test_pages_match_their_sources.py.",
        ),
        (
            "Capped at the 6-month held-out horizon",
            "pinned to protocol.horizon_months by test_pages_match_their_sources.py.",
        ),
        (
            "The fixed $150 consignment charge",
            "a cost ASSUMPTION of the shortage model, stated so the reader knows it is "
            "excluded from the per-unit figures.",
        ),
        (
            "per $1.00 of unit price",
            "the panel carries no prices, so every dollar figure is per unit at $1.00. "
            "A normalisation, stated in the artifact's own caveats.",
        ),
        (
            "CI excludes 0",
            "the decision rule: an interval that does not cover zero. A definition.",
        ),
        (
            "against a shared zero line",
            "the reference line every interval is drawn against; zero is the null, not "
            "a measured value.",
        ),
        (
            "At a 3-month review period",
            "one cell of the review-period sweep, named. Pinned to the artifact's "
            "sensitivity_review_period_3 block by test_pages_match_their_sources.py.",
        ),
        (
            "At 6 months it wins again",
            "the sensitivity_review_period_6 block. Same pin.",
        ),
        (
            "quoting the 1-month number",
            "the primary block's review period. Same pin.",
        ),
        (
            "annual holding rate ×",
            "the formula label for the served rate beside it.",
        ),
        (
            "/12. Gartner 2022 electronics",
            "months in a year, and the citation year of the holding-rate source.",
        ),
        (
            "three rolling origins under six forecast methods",
            "pinned to protocol.n_origins and the evaluation grid's forecast_methods by "
            "test_pages_match_their_sources.py.",
        ),
    ),
    "NotFoundPage.tsx": (
        (
            "404",
            "the HTTP status this route represents.",
        ),
    ),
    "Register.tsx": (
        (
            "e.g. 40.7128",
            "a placeholder latitude showing the expected format.",
        ),
        (
            "e.g. -74.0060",
            "a placeholder longitude showing the expected format.",
        ),
    ),
    "ResiliencePage.tsx": (
        (
            "− 1)",
            "the tail of the formula `baseline BOM spend × (CVaR-95 − 1)`, split across "
            "elements by the <strong> around the served multiplier. 1 is the no-loss "
            "value the multiplier is measured from, not a figure.",
        ),
        (
            "0% where the supplier already meets the window",
            "arithmetic: no expedite is required when the lead time already fits, so the "
            "premium is zero by construction.",
        ),
    ),
    "SchedulerPage.tsx": (
        (
            "all 4 metrics",
            "the four scored metrics of the demand leaderboard, fixed by the endpoint's "
            "response shape.",
        ),
        (
            "0 · out of stock",
            "rendered only in the `offer.stock === 0` branch, so the literal restates the "
            "served value it is guarded by.",
        ),
        (
            "a frozen 2024 snapshot (CC-BY-4.0)",
            "the vintage and licence of the committed offer snapshot.",
        ),
    ),
}

#: Published numbers that NOTHING in this repo can verify. They are not allowlist
#: entries in good standing — they are a standing debt, listed so that (a) the suite
#: stays green on a known set and (b) no NEW unverifiable number can be added without
#: this list changing, which `test_the_unverifiable_debt_has_not_grown` refuses to let
#: happen quietly.
KNOWN_UNVERIFIED: dict[str, tuple[tuple[str, str], ...]] = {
    # NewsvendorPage's "108 s per setting on the deployed instance" was PAID OFF on
    # 2026-09-05, which is why this page no longer appears here. 108 traced to no
    # artifact, no document and no code path — an unrecorded stopwatch reading on a
    # Render container, published as fact. The page now cites the sweep's recorded
    # `meta.wall_seconds` instead, and that figure is pinned in
    # `test_pages_match_their_sources.py` so it moves when the artifact moves.
    "BenchmarkPage.tsx": (
        (
            "exist only from run 8 onward",
            "LIVE DEBT. No table in backend/supply_chain.db and no field in "
            "docs/benchmark_results.json records which run first carried pool-matched "
            "baseline arms; the artifact only knows it is run 9. The claim is probably "
            "true and is certainly unfalsifiable from this repo.",
        ),
    ),
}


# ── Machinery ────────────────────────────────────────────────────────────────


def _allowed_phrases(page: str) -> tuple[str, ...]:
    entries = (
        GLOBAL_ALLOW
        + PAGE_ALLOW.get(page, ())
        + KNOWN_UNVERIFIED.get(page, ())
    )
    #: Longest first: "CVaR-95" must not eat the "95" out of a longer allowed phrase
    #: before that phrase gets its chance to match.
    return tuple(sorted((p for p, _ in entries), key=len, reverse=True))


def _residual(text: str, phrases: tuple[str, ...]) -> str:
    """`text` with every allowed phrase removed, whitespace-normalised."""
    for phrase in phrases:
        text = text.replace(phrase, " ")
    return re.sub(r"\s+", " ", text).strip()


def _visible_prop_nodes(source: str) -> list[JsxText]:
    """Static `title=`/`hint=`/... string props — tooltip text a browser shows."""
    out: list[JsxText] = []
    for m in _PROP_RE.finditer(source):
        value = decode_entities(m.group(2))
        if re.search(r"\d", value):
            out.append(
                JsxText(text=f"{m.group(1)}=«{value}»", line=source.count("\n", 0, m.start()) + 1, testids=())
            )
    return out


def _claims(path: Path) -> list[tuple[JsxText, str]]:
    """Every rendered fragment of `path` that still shows a digit after the allowlist."""
    source = path.read_text(encoding="utf-8")
    pinned = PINNED.get(path.name, frozenset())
    phrases = _allowed_phrases(path.name)
    found: list[tuple[JsxText, str]] = []
    for node in text_nodes(source) + _visible_prop_nodes(source):
        if pinned.intersection(node.testids):
            continue  # criterion (a): a pin test reads this element against an artifact
        rendered = node.rendered if node.testids or "«" not in node.text else node.text
        if not re.search(r"\d", rendered):
            continue
        residual = _residual(rendered, phrases)
        if re.search(r"\d", residual):
            found.append((node, residual))
    return found


# ── The guard ────────────────────────────────────────────────────────────────


def test_the_pages_directory_was_actually_found() -> None:
    """A guard that scans zero files is a check that cannot fail."""
    assert len(PAGES) >= 13, (
        f"expected the 13+ pages of {PAGES_DIR}; found {[p.name for p in PAGES]}. "
        "If the frontend moved, re-point PAGES_DIR — do not let this scan nothing."
    )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_the_page_publishes_no_unverified_number(page: Path) -> None:
    claims = _claims(page)
    if not claims:
        return
    def _one(node: JsxText, residual: str) -> str:
        numbers = ", ".join(_NUMBER_RE.findall(residual)) or "?"
        return (
            f"  {page.name}:{node.line}\n"
            f"      number(s): {numbers}\n"
            f"      rendered : {node.rendered[:300]}"
        )

    lines = "\n".join(_one(node, residual) for node, residual in claims)
    pytest.fail(
        f"{len(claims)} rendered number(s) in {page.name} are backed by nothing:\n\n"
        f"{lines}\n\n"
        "Every number a page prints must trace to an artifact field, an API field or a\n"
        "code constant. Do ONE of:\n"
        "  1. render it from the response instead of typing it, or\n"
        "  2. pin it in backend/tests/test_pages_match_their_sources.py (phrase-anchored,\n"
        "     no data-testid required) and add the phrase to PAGE_ALLOW here saying so, or\n"
        "  3. add it to PAGE_ALLOW with a real justification — a unit, a definition, a\n"
        "     citation or a past-tense date.\n"
        "Widening a phrase until this goes quiet is not option 4.",
        pytrace=False,
    )


# ── The allowlist must not rot ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "page,phrase",
    [
        (page, phrase)
        for page, entries in list(PAGE_ALLOW.items()) + list(KNOWN_UNVERIFIED.items())
        for phrase, _ in entries
    ],
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_every_allowlisted_phrase_is_still_on_its_page(page: str, phrase: str) -> None:
    """A phrase nobody renders any more is dead weight that hides the next real hit.

    Without this, an allowlist only ever grows: someone deletes the sentence, the entry
    survives, and its justification silently starts licensing a different number.
    """
    source = (PAGES_DIR / page).read_text(encoding="utf-8")
    haystack = re.sub(r"\s+", " ", decode_entities(source))
    assert re.sub(r"\s+", " ", phrase) in haystack, (
        f"{page} no longer renders {phrase!r}, but the allowlist still exempts it. "
        "Delete the entry."
    )


def test_every_justification_says_something() -> None:
    """An entry whose reason is blank or a shrug is not an entry."""
    for page, entries in list(PAGE_ALLOW.items()) + list(KNOWN_UNVERIFIED.items()):
        for phrase, why in entries:
            assert len(why.strip()) >= 25, f"{page}: {phrase!r} has no real justification"
    for phrase, why in GLOBAL_ALLOW:
        assert len(why.strip()) >= 15, f"GLOBAL_ALLOW: {phrase!r} has no real justification"


def test_every_global_allowlist_phrase_is_still_rendered_somewhere() -> None:
    """A global exemption nobody uses is a licence sitting around waiting to be misused."""
    haystacks = {
        p.name: re.sub(r"\s+", " ", decode_entities(p.read_text(encoding="utf-8")))
        for p in PAGES
    }
    for phrase, _ in GLOBAL_ALLOW:
        needle = re.sub(r"\s+", " ", phrase)
        assert any(needle in h for h in haystacks.values()), (
            f"no page renders {phrase!r} any more, but GLOBAL_ALLOW still exempts it "
            "everywhere. Delete the entry."
        )


#: Published numbers this repo cannot verify. Started at 2 on 2026-09-05; the
#: NewsvendorPage "108 s" entry was paid off the same day. This number may go DOWN
#: freely — it must never go up without someone deciding to let it.
MAX_UNVERIFIED_DEBT = 1


def test_the_unverifiable_debt_has_not_grown() -> None:
    """One published number this repo cannot verify. One, and no more.

    ``KNOWN_UNVERIFIED`` buys a green suite in exchange for naming the debt exactly.
    The moment another entry is needed, this fails and forces the decision to be taken
    deliberately rather than by adding a line to a list nobody reads.

    The assertion is deliberately two-sided. If the count DROPS, this also fails —
    telling you to lower ``MAX_UNVERIFIED_DEBT`` — because a ratchet that only ever
    catches growth lets a paid-off debt leave slack behind for the next number to
    occupy silently. That is the same "label outlives the code" failure this whole
    file exists to prevent, and it would be embarrassing to reintroduce it here.
    """
    total = sum(len(v) for v in KNOWN_UNVERIFIED.values())
    assert total <= MAX_UNVERIFIED_DEBT, (
        f"{total} unverifiable published numbers are now tolerated (limit "
        f"{MAX_UNVERIFIED_DEBT}). Every entry in KNOWN_UNVERIFIED is a figure on the "
        "live site that no artifact, document or code path can confirm. Fix one "
        "before adding another."
    )
    assert total == MAX_UNVERIFIED_DEBT, (
        f"Only {total} unverifiable number(s) remain but MAX_UNVERIFIED_DEBT is still "
        f"{MAX_UNVERIFIED_DEBT}. Lower it to {total} so the ratchet keeps its grip — "
        "leftover slack is how the next unverified figure slips in unnoticed."
    )


def test_the_pinned_anchors_this_guard_trusts_still_exist() -> None:
    """Criterion (a) must never exempt an element that no longer exists."""
    for page, testids in PINNED.items():
        source = (PAGES_DIR / page).read_text(encoding="utf-8")
        for testid in testids:
            assert f'data-testid="{testid}"' in source, (
                f"{page} no longer carries data-testid={testid!r}, yet this guard still "
                f"treats it as a verified anchor."
            )
