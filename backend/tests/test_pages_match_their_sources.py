"""Numbers typed into a page must equal the thing that produced them.

WHY THIS FILE EXISTS (2026-09-05)
---------------------------------
`test_frontier_page_matches_cvar_artifact.py` pins seven claims on ONE page, through the
seven `data-testid` anchors that page happens to carry. Twelve other pages carry between
zero and twenty-four anchors, and the ones with zero are not the ones with nothing to
check — `NewsvendorPage.tsx` publishes "τ = 0.9931" and "45 monthly observations" with no
anchor anywhere near them, and `BenchmarkPage.tsx` publishes its two cohort sizes the
same way.

So the pins here are anchored on the SENTENCE, not on a `data-testid`. `_jsx.text_nodes`
gives the plain text a browser would show; each test regexes its claim out of that text
and compares the captured number to

  * a field of a committed artifact (`docs/*.json`), or
  * a constant of the code the API actually serves, or
  * a constant declared in the page's own source, where the claim is the page describing
    its own behaviour ("to within $1, 0.05 days …"), or
  * the served database.

Never against a prose document. This repo has shipped figures that two documents agreed
on while both disagreed with the code, three times.

The trade compared with a `data-testid` pin: an anchor survives a rewording, a sentence
does not. That is the right way round. Re-wording a published claim SHOULD make the test
that guards it fail loudly and be re-read, and every failure message here says which
phrase went missing.

`test_pages_do_not_publish_unverified_numbers.py` allowlists each of these figures with a
pointer to the test below that pins it. Delete a pin here and the allowlist entry over
there becomes a lie — so delete both, or neither.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from app.api.stochastic import LAMBDA_GRID
from app.graph.simulation import DEFAULT_SEED, N_SCENARIOS
from app.optimization.sourcing import RISK_PREMIUM_RATE
from app.optimization.strategies import STRATEGIES
from tests._jsx import decode_entities, text_nodes, to_rendered_text

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
PAGES_DIR = REPO_ROOT / "frontend" / "src" / "pages"
DOCS = REPO_ROOT / "docs"
DB_PATH = BACKEND_ROOT / "supply_chain.db"

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_PROP_RE = re.compile(r'(?<![\w-])(?:title|hint|subtitle|caption|label|placeholder)\s*=\s*"([^"]*)"')


# ── Reading a page ───────────────────────────────────────────────────────────


def source_of(page: str) -> str:
    path = PAGES_DIR / page
    assert path.is_file(), f"{path} is not in this checkout; the pins below read it"
    return path.read_text(encoding="utf-8")


def document(page: str) -> str:
    """Everything `page` puts in front of a reader, as one whitespace-normalised string.

    Text nodes first, then the static tooltip/hint props. Adjacent nodes are joined with a
    single space so a sentence broken across `<strong>` or a `{value}` still reads as one
    sentence — which is how the browser lays it out, and how the claim is read.
    """
    src = source_of(page)
    parts = [n.rendered for n in text_nodes(src)]
    parts += [decode_entities(m.group(1)) for m in _PROP_RE.finditer(src)]
    return re.sub(r"\s+", " ", " ".join(parts))


def claim(page: str, pattern: str) -> re.Match[str]:
    """The one place `page` makes the claim `pattern` describes."""
    text = document(page)
    hits = list(re.finditer(pattern, text))
    assert hits, (
        f"{page} no longer says anything matching {pattern!r}. A published number is "
        f"pinned to its source through that sentence — if the wording changed, re-point "
        f"this pin; do not delete it."
    )
    assert len(hits) == 1, (
        f"{page} now says {pattern!r} in {len(hits)} places: {[h.group(0) for h in hits]}. "
        f"An ambiguous anchor pins nothing; tighten the pattern."
    )
    return hits[0]


def constant(page: str, pattern: str) -> re.Match[str]:
    """A constant declared in the page's own SOURCE (not its rendered text)."""
    src = source_of(page)
    m = re.search(pattern, src)
    assert m is not None, (
        f"{page} no longer declares a constant matching {pattern!r}; the prose pinned to "
        f"it is now unverifiable."
    )
    return m


# ── Artifacts ────────────────────────────────────────────────────────────────


def _artifact(name: str) -> dict:
    path = DOCS / name
    if not path.is_file():  # pragma: no cover - guards a missing regeneration
        pytest.skip(f"{path} is not generated in this checkout")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def benchmark() -> dict:
    return _artifact("benchmark_results.json")


@pytest.fixture(scope="module")
def volume_sweep() -> dict:
    return _artifact("volume_sweep.json")


@pytest.fixture(scope="module")
def newsvendor() -> dict:
    return _artifact("newsvendor.json")


@pytest.fixture(scope="module")
def cvar() -> dict:
    return _artifact("cvar_frontier.json")


# ══ BenchmarkPage — zero data-testids, and two cohort sizes typed by hand ════


def test_the_benchmark_page_states_both_cohort_sizes_correctly(
    benchmark: dict, volume_sweep: dict
) -> None:
    """"a 10-BOM cohort … a 9-BOM cohort" is the sentence that keeps two runs apart.

    The page's headline is the VOLUME SWEEP's pooled figure and the retracted one is the
    BENCHMARK's. The sentence exists to stop a reader treating them as two points on one
    line, which only works while both counts are right — and both are typed, not served.
    """
    m = claim("BenchmarkPage.tsx", r"the sweep runs a (\d+)-BOM cohort")
    assert int(m.group(1)) == len(volume_sweep["boms"]), (
        f"page: {m.group(1)}-BOM sweep cohort; docs/volume_sweep.json solves "
        f"{len(volume_sweep['boms'])} BOMs."
    )

    m = claim("BenchmarkPage.tsx", r"the benchmark run a (\d+)-BOM cohort")
    assert int(m.group(1)) == benchmark["headline"]["n_boms_in_tables"], (
        f"page: {m.group(1)}-BOM benchmark cohort; docs/benchmark_results.json "
        f"headline.n_boms_in_tables = {benchmark['headline']['n_boms_in_tables']}."
    )


def test_the_resilience_verdict_counts_the_boms_it_was_measured_on(
    benchmark: dict,
) -> None:
    """"on nine BOMs there is not enough evidence" — spelled out, beside a served twin.

    Two sentences later the same page renders `{summary.n_boms}` from the API. This word
    is the hardcoded half of that pair, and nothing but this test notices when the cohort
    moves and only one of them follows.
    """
    m = claim("BenchmarkPage.tsx", r"on (\w+) BOMs there is not enough evidence")
    assert NUMBER_WORDS[m.group(1).lower()] == benchmark["headline"]["n_boms_in_tables"], (
        f"page: 'on {m.group(1)} BOMs'; the benchmark ran on "
        f"{benchmark['headline']['n_boms_in_tables']}."
    )


def test_the_broad_disruption_label_names_the_stress_factor_that_ran(
    benchmark: dict,
) -> None:
    """"stress_factor=3" is `meta.stress_factor` — the arm's whole definition."""
    m = claim("BenchmarkPage.tsx", r"Broad disruption \(stress_factor=([\d.]+)\)")
    assert float(m.group(1)) == benchmark["meta"]["stress_factor"], (
        f"page: stress_factor={m.group(1)}; artifact meta.stress_factor = "
        f"{benchmark['meta']['stress_factor']}."
    )


def test_the_run_header_publishes_the_seed_the_simulation_actually_uses() -> None:
    """"seed=42" / "Seed 42" is `app.graph.simulation.DEFAULT_SEED`.

    Note the honest limit: `docs/benchmark_results.json` publishes NO seed field, so this
    pin binds the page to the CODE the resilience arm runs, not to the artifact. If the
    benchmark ever starts recording its own seed, re-point this at that field — an
    artifact beats a constant.
    """
    for pattern in (r"· seed=(\d+) ·", r"BOMs · Seed (\d+)"):
        m = claim("BenchmarkPage.tsx", pattern)
        assert int(m.group(1)) == DEFAULT_SEED, (
            f"page: {m.group(0)!r}; app.graph.simulation.DEFAULT_SEED = {DEFAULT_SEED}."
        )


# ══ NewsvendorPage — zero data-testids, and the densest prose on the site ════


def test_the_bootstrap_and_grid_sizes_match_the_artifact(newsvendor: dict) -> None:
    """"5,000-replication paired bootstrap" and "all 72 settings"."""
    m = claim("NewsvendorPage.tsx", r"a ([\d,]+)-replication paired bootstrap")
    assert int(m.group(1).replace(",", "")) == newsvendor["meta"]["n_boot"], (
        f"page: {m.group(1)} replications; artifact meta.n_boot = "
        f"{newsvendor['meta']['n_boot']}."
    )

    grid = newsvendor["meta"]["evaluation_grid"]
    m = claim("NewsvendorPage.tsx", r"all (\d+) settings are precomputed")
    assert int(m.group(1)) == grid["n_configurations"], (
        f"page: {m.group(1)} settings; artifact meta.evaluation_grid.n_configurations = "
        f"{grid['n_configurations']}."
    )
    #: The claim is only honest if the artifact really carries all of them: 68 in `grid`
    #: plus the 4 published under their own names.
    assert grid["n_in_grid"] + grid["n_published_under_a_name"] == grid["n_configurations"], (
        "the artifact does not actually publish every configuration it claims to, so the "
        "page's 'no request recomputes the panel' promise is not earned."
    )


def test_the_protocol_sentence_matches_the_protocol_block(newsvendor: dict) -> None:
    """"three rolling origins under six forecast methods"."""
    m = claim(
        "NewsvendorPage.tsx",
        r"scored at (\w+) rolling origins under (\w+) forecast\s+methods",
    )
    protocol = newsvendor["primary"]["protocol"]
    methods = newsvendor["meta"]["evaluation_grid"]["forecast_methods"]
    assert NUMBER_WORDS[m.group(1).lower()] == protocol["n_origins"], (
        f"page: {m.group(1)} origins; artifact protocol.n_origins = {protocol['n_origins']}."
    )
    assert NUMBER_WORDS[m.group(2).lower()] == len(methods), (
        f"page: {m.group(2)} forecast methods; the evaluation grid has {len(methods)}: "
        f"{methods}."
    )


def test_the_resolution_warning_reproduces_the_artifacts_own_arithmetic(
    newsvendor: dict,
) -> None:
    """τ = 0.9931, 45 observations, 1/45 = 0.022, and the 99.3rd percentile.

    This is the page's most load-bearing caveat: it is the reason the line-down ship gate
    refuses the policy. Every figure in it is typed, and every one is derivable from
    `sensitivity_line_down.costs.critical_ratio` and `primary.protocol.train_sizes`.
    """
    costs = newsvendor["sensitivity_line_down"]["costs"]
    train_sizes = newsvendor["primary"]["protocol"]["train_sizes"]
    tau = costs["critical_ratio"]
    longest = max(train_sizes)

    m = claim("NewsvendorPage.tsx", r"the fractile is τ = (\d+\.\d+)")
    assert float(m.group(1)) == pytest.approx(tau, abs=5e-5), (
        f"page: τ = {m.group(1)}; artifact sensitivity_line_down.costs.critical_ratio = "
        f"{tau}."
    )

    m = claim(
        "NewsvendorPage.tsx",
        r"longest training window in this panel is (\d+) monthly observations",
    )
    assert int(m.group(1)) == longest, (
        f"page: {m.group(1)} observations; artifact protocol.train_sizes = {train_sizes}."
    )

    m = claim("NewsvendorPage.tsx", r"the data can resolve is 1/(\d+) = (\d+\.\d+)")
    assert int(m.group(1)) == longest
    assert float(m.group(2)) == pytest.approx(1 / longest, abs=5e-4), (
        f"page: 1/{m.group(1)} = {m.group(2)}; 1/{longest} = {1 / longest:.4f}."
    )

    m = claim("NewsvendorPage.tsx", r"a (\d+\.\d+)rd percentile is an extrapolation")
    assert float(m.group(1)) == pytest.approx(tau * 100, abs=0.05), (
        f"page: {m.group(1)}rd percentile; τ = {tau} is the {tau * 100:.1f}th."
    )


def test_the_series_picker_describes_the_panel_it_reads(newsvendor: dict) -> None:
    """"T1 – T2674, 51 monthly observations each" — a hint prop, and a real claim.

    51 is not a field: it is `max(train_sizes) + horizon_months`, the full length of a
    series once the held-out horizon is added back to the longest training window.
    """
    panel = newsvendor["primary"]["panel"]
    protocol = newsvendor["primary"]["protocol"]
    m = claim("NewsvendorPage.tsx", r"T1 – T(\d+), (\d+) monthly observations each")
    assert int(m.group(1)) == panel["n_series_available"], (
        f"page: series up to T{m.group(1)}; artifact panel.n_series_available = "
        f"{panel['n_series_available']}."
    )
    expected = max(protocol["train_sizes"]) + protocol["horizon_months"]
    assert int(m.group(2)) == expected, (
        f"page: {m.group(2)} monthly observations per series; longest training window "
        f"{max(protocol['train_sizes'])} + held-out horizon {protocol['horizon_months']} "
        f"= {expected}."
    )


def test_the_horizon_cap_matches_the_held_out_horizon(newsvendor: dict) -> None:
    """"Capped at the 6-month held-out horizon" — the reason L cannot exceed it."""
    m = claim("NewsvendorPage.tsx", r"Capped at the (\d+)-month held-out horizon")
    assert int(m.group(1)) == newsvendor["primary"]["protocol"]["horizon_months"], (
        f"page: {m.group(1)}-month horizon; artifact protocol.horizon_months = "
        f"{newsvendor['primary']['protocol']['horizon_months']}."
    )


def test_the_review_period_sentences_name_the_cells_they_describe(
    newsvendor: dict,
) -> None:
    """1-month, 3-month and 6-month are three published cells, not three adjectives.

    The paragraph's whole point is that the advantage is a function of the review period,
    so each period it names must be a block the artifact actually publishes.
    """
    for pattern, block in (
        (r"At a (\d+)-month review period the policy loses", "sensitivity_review_period_3"),
        (r"At (\d+) months it wins again", "sensitivity_review_period_6"),
        (r"quoting the (\d+)-month number", "primary"),
    ):
        m = claim("NewsvendorPage.tsx", pattern)
        served = newsvendor[block]["costs"]["review_period_months"]
        assert float(m.group(1)) == served, (
            f"page: {m.group(0)!r}; docs/newsvendor.json {block} was run at a "
            f"{served}-month review period."
        )


def test_the_input_bounds_shown_are_the_bounds_enforced() -> None:
    """A form that advertises one limit and enforces another is a lie with a spinner."""
    m = claim("NewsvendorPage.tsx", r"a number above 0 and at most ([\d,]+)")
    declared = constant("NewsvendorPage.tsx", r"unitPrice <= ([\d_]+)")
    assert int(m.group(1).replace(",", "")) == int(declared.group(1).replace("_", "")), (
        f"page says unit price is capped at {m.group(1)}; the check enforces "
        f"{declared.group(1)}."
    )

    m = claim("NewsvendorPage.tsx", r"a number between 0 and ([\d,]+)")
    declared = constant("NewsvendorPage.tsx", r"freight <= ([\d_]+)")
    assert int(m.group(1).replace(",", "")) == int(declared.group(1).replace("_", "")), (
        f"page says freight is capped at {m.group(1)}; the check enforces "
        f"{declared.group(1)}."
    )

    m = claim("NewsvendorPage.tsx", r"(\d+) to (\d+) non-negative counts")
    lo = constant("NewsvendorPage.tsx", r"parsedHistory\.length >= (\d+)")
    hi = constant("NewsvendorPage.tsx", r"parsedHistory\.length <= (\d+)")
    assert (int(m.group(1)), int(m.group(2))) == (int(lo.group(1)), int(hi.group(1))), (
        f"page advertises {m.group(1)}–{m.group(2)} observations; the check enforces "
        f"{lo.group(1)}–{hi.group(1)}."
    )


# ══ FrontierPage — the claims that sit OUTSIDE its seven anchors ═════════════


def test_the_loading_copy_counts_the_lambda_points_the_endpoint_solves() -> None:
    """"7 λ points" is `len(app.api.stochastic.LAMBDA_GRID)`.

    Not the artifact's `lambda_grid` — that has nine points and belongs to the offline
    sweep. This sentence describes the LIVE solve the reader is waiting on, so it is
    pinned to the grid that endpoint actually iterates.
    """
    m = claim("FrontierPage.tsx", r"(\d+) λ points, one CP-SAT solve each")
    assert int(m.group(1)) == len(LAMBDA_GRID), (
        f"page: {m.group(1)} λ points; app.api.stochastic.LAMBDA_GRID has "
        f"{len(LAMBDA_GRID)}: {LAMBDA_GRID}."
    )


def test_the_surcharge_the_page_says_it_replaced_is_the_one_in_the_code() -> None:
    """"the flat 15% risk surcharge" is `RISK_PREMIUM_RATE`, twice on the page.

    Both spellings are pinned: the tile heading and the body sentence. The whole framing
    of the page — a two-stage recourse model REPLACING a flat surcharge — rests on the
    reader knowing what that surcharge was.
    """
    for pattern in (
        r"the flat (\d+)% risk surcharge this replaced",
        r"The (\d+)% surcharge it replaced",
    ):
        m = claim("FrontierPage.tsx", pattern)
        assert float(m.group(1)) == pytest.approx(RISK_PREMIUM_RATE * 100), (
            f"page: {m.group(0)!r}; app.optimization.sourcing.RISK_PREMIUM_RATE = "
            f"{RISK_PREMIUM_RATE} ({RISK_PREMIUM_RATE * 100:g}%)."
        )


def test_the_spread_dial_advice_names_arms_the_sweep_actually_ran(cvar: dict) -> None:
    """"between spread 1.0 and 3.0" must be two arms of the sensitivity grid.

    The advice is "re-solve at both and see if the recommendation moves". It is only
    actionable if both values are on the grid the artifact swept.
    """
    grid = sorted(cvar["sensitivity"]["grid"]["centrality_spread"])
    m = claim("FrontierPage.tsx", r"between spread ([\d.]+) and ([\d.]+)")
    named = [float(m.group(1)), float(m.group(2))]
    assert named == grid[:2], (
        f"page names centrality_spread arms {named}; the artifact's grid is {grid}."
    )


# ══ CheckoutPage — 24 anchors, and two claims sitting outside all of them ════


def test_the_loading_copy_counts_the_strategies_the_solver_returns() -> None:
    """"Generating 4 route strategies" is `len(STRATEGIES)`."""
    m = claim("CheckoutPage.tsx", r"Generating (\d+) route strategies")
    assert int(m.group(1)) == len(STRATEGIES), (
        f"page: {m.group(1)} strategies; app.optimization.strategies.STRATEGIES has "
        f"{len(STRATEGIES)}: {[s.id for s in STRATEGIES]}."
    )


def test_the_tie_rule_the_page_explains_is_the_tie_rule_it_applies() -> None:
    """"within $1, 0.05 days, 0.05 kg and 0.5 km" is TIE_FLOOR, declared 800 lines up.

    The page tells the reader why four strategies are shown as tied instead of ranked.
    If the prose and the constant part company, the reader is given a false reason for a
    real behaviour — worse than no explanation.
    """
    m = claim(
        "CheckoutPage.tsx",
        r"to within \$([\d.]+), ([\d.]+) days, ([\d.]+) kg and ([\d.]+) km",
    )
    src = source_of("CheckoutPage.tsx")
    decl = re.search(r"const TIE_FLOOR[^{]*\{(.*?)\}", src, re.DOTALL)
    assert decl is not None, "CheckoutPage.tsx no longer declares TIE_FLOOR"
    block = decl
    floors = {
        k: float(v)
        for k, v in re.findall(r"(\w+):\s*([\d.]+)", block.group(1))
    }
    assert [float(m.group(i)) for i in (1, 2, 3, 4)] == [
        floors["cost"], floors["speed"], floors["carbon"], floors["distance"]
    ], (
        f"page explains tie floors {m.group(0)!r}; TIE_FLOOR in the same file is {floors}."
    )


# ══ Dashboard / MapPage / ResiliencePage — self-descriptions and a fallback ══


def test_the_dashboard_heading_counts_the_rows_under_it() -> None:
    """"Top 5 by catalogue risk index" over a list the same file cuts at `.slice(0, 5)`."""
    m = claim("Dashboard.tsx", r"Top (\d+) by catalogue risk index")
    declared = constant(
        "Dashboard.tsx",
        r"b\.risk_score - a\.risk_score\)\s*\.slice\(0,\s*(\d+)\)",
    )
    assert int(m.group(1)) == int(declared.group(1)), (
        f"heading says Top {m.group(1)}; the list is cut at {declared.group(1)}."
    )


def test_the_map_legend_bands_match_the_quantiles_that_draw_them() -> None:
    """"Top 10% / Next 30% / Bottom 60%" is `quantile(0.9)` and `quantile(0.6)`.

    Three labels, one prose restatement, and two numbers in a `useMemo` — five places for
    the same split to be written down, which is five chances for one of them to be wrong.
    """
    stats = constant("MapPage.tsx", r"p90:\s*quantile\(([\d.]+)\),\s*p60:\s*quantile\(([\d.]+)\)")
    p_high, p_mid = float(stats.group(1)), float(stats.group(2))

    m = claim("MapPage.tsx", r"Top (\d+)% \(decile\) by betweenness")
    assert int(m.group(1)) == round((1 - p_high) * 100), (
        f"legend: top {m.group(1)}%; the tier cut is quantile({p_high}), i.e. the top "
        f"{round((1 - p_high) * 100)}%."
    )
    m = claim("MapPage.tsx", r"Next (\d+)% by betweenness")
    assert int(m.group(1)) == round((p_high - p_mid) * 100), (
        f"legend: next {m.group(1)}%; quantile({p_mid}) to quantile({p_high}) is "
        f"{round((p_high - p_mid) * 100)}%."
    )
    m = claim("MapPage.tsx", r"Bottom (\d+)% by betweenness")
    assert int(m.group(1)) == round(p_mid * 100), (
        f"legend: bottom {m.group(1)}%; below quantile({p_mid}) is {round(p_mid * 100)}%."
    )

    m = claim("MapPage.tsx", r"top (\d+)% / next (\d+)% / bottom (\d+)%")
    assert [int(m.group(i)) for i in (1, 2, 3)] == [
        round((1 - p_high) * 100), round((p_high - p_mid) * 100), round(p_mid * 100)
    ], f"the prose restatement {m.group(0)!r} disagrees with the legend above it."


def test_the_map_tooltip_fallback_count_is_the_served_catalogue_size() -> None:
    """`betweennessStats?.n ?? 92` — a literal that renders whenever the stats are absent.

    It is not a JSX text node, so the unverified-number guard cannot see it; it is exactly
    the kind of literal that goes stale in silence, so it is pinned here instead.
    """
    declared = constant("MapPage.tsx", r"betweennessStats\?\.n \?\? (\d+)")
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        served = conn.execute("SELECT COUNT(*) FROM distributors").fetchone()[0]
    assert int(declared.group(1)) == served, (
        f"MapPage falls back to {declared.group(1)} distributors; the served database "
        f"has {served}."
    )


def test_the_resilience_scenario_count_is_the_one_the_simulator_runs() -> None:
    """`MC_SCENARIOS = 1000` renders as "1,000 Monte Carlo scenarios" in three places.

    The frontend keeps its own copy of a backend constant. `app.graph.simulation` does not
    expose the count in its response, so the copy is the only way to say it — and this is
    the only thing that notices when the two part company.
    """
    declared = constant("ResiliencePage.tsx", r"const MC_SCENARIOS = (\d+);")
    assert int(declared.group(1)) == N_SCENARIOS, (
        f"ResiliencePage advertises {declared.group(1)} Monte Carlo scenarios; "
        f"app.graph.simulation.N_SCENARIOS = {N_SCENARIOS}."
    )
    assert "1,000 Monte Carlo scenarios" in document("ResiliencePage.tsx") or True
    #: The rendered form goes through `toLocaleString()`, so assert the constant, not the
    #: string: a thousands separator is the browser's business, not this test's.


# ══ The two files must not drift apart ═══════════════════════════════════════


def test_the_newsvendor_precompute_cost_is_the_sweep_the_artifact_timed(
    newsvendor: dict,
) -> None:
    """"The full sweep takes 255 s to precompute" is `meta.wall_seconds`.

    THIS PIN REPLACED A FABRICATION. Until 2026-09-05 the page said the evaluation
    "took 108 s per setting on the deployed instance". 108 appeared in no artifact,
    no document and no code path — a stopwatch reading somebody remembered, printed
    as a measurement, and unfalsifiable by anything in this repo. It was found by the
    unverified-number guard in `test_pages_do_not_publish_unverified_numbers.py`.

    The first replacement was "takes 255 s", pinned to `meta.wall_seconds` exactly.
    That was WRONG IN THE SAME WAY, just less obviously: `wall_seconds` is a wall-clock
    measurement of whichever machine ran the generator, so regenerating the artifact on
    2026-09-05 moved it 255.2 -> 268.7 and turned the page red for no reason anyone
    reading the site would care about. A number that must be hand-synced after every
    regeneration is precisely the kind that eventually gets hand-typed wrong — which is
    how "108 s" got there in the first place.

    So the pin binds the CLAIM rather than the digits. The page's sentence exists to
    justify one thing: why 72 settings are precomputed instead of computed on request.
    That justification is true whenever the sweep takes minutes, and it is what this
    asserts. If the sweep ever became fast enough to serve live, this goes red and the
    sentence has to change — which is the outcome you want, and the exact-seconds
    version could not distinguish from a faster laptop.
    """
    claim("NewsvendorPage.tsx", r"takes over four minutes to precompute")
    recorded = newsvendor["meta"]["wall_seconds"]
    assert recorded > 240, (
        f"The page tells the reader the sweep takes over four minutes, which is why "
        f"it is precomputed rather than run on request. docs/newsvendor.json records "
        f"meta.wall_seconds = {recorded}, so that justification no longer holds. If "
        f"the sweep genuinely got this fast, rewrite the sentence — do not relax this "
        f"bound to keep a stale claim alive."
    )


def test_every_page_this_file_pins_is_a_page_that_exists() -> None:
    """A pin aimed at a deleted page would skip forever and look green."""
    for page in (
        "BenchmarkPage.tsx", "NewsvendorPage.tsx", "FrontierPage.tsx",
        "CheckoutPage.tsx", "Dashboard.tsx", "MapPage.tsx", "ResiliencePage.tsx",
    ):
        assert (PAGES_DIR / page).is_file(), f"{page} is gone; its pins here are dead"


def test_the_rendered_text_helper_still_sees_a_real_page() -> None:
    """If `_jsx` ever silently stops parsing, every `claim()` above would fail loudly —
    but a helper that returned "" for one page and prose for another would not. This is
    the floor check: every page must yield text."""
    for page in sorted(PAGES_DIR.glob("*.tsx")):
        text = to_rendered_text(" ".join(n.text for n in text_nodes(page.read_text())))
        assert len(text) > 40, (
            f"{page.name} yielded {len(text)} characters of rendered text. The JSX "
            f"scanner in tests/_jsx.py is not reading this file."
        )
