"""Published prose must agree with the artifact it claims to quote.

Every number in this repo's headline docs is supposed to come from a committed
JSON artifact or a code constant. In practice six of them did not, because they
were transcribed by hand once and then never re-checked:

  * "467 part families"        — 467 is the count of ``_group_key`` outputs. The
                                 count of ``base_product`` values is 360. The
                                 number was right; the *noun* was wrong.
  * "CVaR figure is 100% data-derived" — the spend side is; the probability side
                                 is betweenness centrality, which is not a
                                 calibrated likelihood.
  * "serving coverage 94.4%"   — matched no denominator in the repo. Measured
                                 today: 97.85% of offer×component pairs, 93.05%
                                 of components. Stale, pre-DB-rebuild.
  * "2,658 series"             — a count that exists nowhere. The panel is 2,674
                                 and 2,646 are scored.
  * "$4.27 per $1 of tail risk" — true only at 60,000 units. ``knee`` is ``null``
                                 at 100x and 1000x.

  * "R² +0.638 → +0.082 → −0.550" — and a second, different trio "+0.612 → +0.189
                                 → −0.476". Both described an 810-row,
                                 27-manufacturer, ``random_forest`` vintage that
                                 two retrains had already replaced. One was
                                 hardcoded into the API's own ``caveat`` string,
                                 next to a ``leakage_audit`` block reporting
                                 different numbers for a different model — a
                                 single JSON response contradicting itself.

This module is the regression guard for that class of drift: it re-derives each
figure from its source and fails when the doc and the source disagree. It is
deliberately fast — it reads JSON, text and one joblib, fits nothing, and touches
no DB.

**Where the honest source is the ARTIFACT, read the artifact.** ``docs/*.json``
is a *generated* file, so a doc that agrees with it proves only that both were
written on the same day. Anything describing the DEPLOYED model — the champion's
name, the row/family/manufacturer counts it was fitted on, its ship-gate margin,
its leakage audit — is checked against ``data/ml_models/metrics.joblib``, and the
generated JSON is cross-checked against that same artifact so the two can never
drift apart silently again. That cross-check is the gate that was missing: the
stale trio agreed with a stale ``leakage_progression.json`` perfectly well.

Two shapes of assertion are used, and the distinction matters:

  * **Generated regions** (``docs/INTERMITTENT_DEMAND.md``) are compared
    byte-for-byte against what the generator would write today. That is the only
    check strong enough to catch a hand-edit of a generated table.
  * **Curated prose** everywhere else is checked for the presence of the right
    number and the *absence* of the retracted one, because prose cannot be
    regenerated and a substring check is what is actually enforceable.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

REPO_ROOT = BACKEND_ROOT.parent
DOCS = REPO_ROOT / "docs"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _json(name: str) -> Dict[str, Any]:
    path = DOCS / name
    if not path.is_file():
        pytest.skip(f"{name} is not committed in this checkout")
    return json.loads(path.read_text())


def _doc(name: str) -> str:
    path = DOCS / name
    if not path.is_file():
        pytest.skip(f"{name} is not committed in this checkout")
    return path.read_text()


#: The SERVED artifact. Everything that describes the deployed model is checked
#: against this, not against a generated doc.
METRICS_PATH = BACKEND_ROOT / "data" / "ml_models" / "metrics.joblib"


@pytest.fixture(scope="module")
def leakage() -> Dict[str, Any]:
    return _json("leakage_progression.json")


@pytest.fixture(scope="module")
def metrics() -> Dict[str, Any]:
    """``metrics.joblib`` — the numbers the API actually publishes."""
    if not METRICS_PATH.is_file():
        pytest.skip("no committed lead-time artifact in this checkout")
    import joblib

    return dict(joblib.load(METRICS_PATH))


@pytest.fixture(scope="module")
def audit(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """``lead_time_leakage_audit`` — computed on every retrain, served on every call."""
    block = metrics.get("lead_time_leakage_audit")
    if not block:
        pytest.fail(
            "the committed artifact carries no lead_time_leakage_audit. That block is "
            "the repo's headline ML finding and is computed on every retrain — an "
            "artifact without it must not be published."
        )
    return dict(block)


@pytest.fixture(scope="module")
def intermittent() -> Dict[str, Any]:
    return _json("intermittent_demand.json")


@pytest.fixture(scope="module")
def cvar() -> Dict[str, Any]:
    return _json("cvar_frontier.json")


# ─────────────────────────────────────────────────────────────────────────────
# 1. 467 family GROUPING KEYS vs 360 BASE PRODUCTS
# ─────────────────────────────────────────────────────────────────────────────

def test_the_two_grouping_counts_are_what_the_artifact_says(leakage):
    """The grouping-key count and the part-family count are different quantities.

    These used to be asserted as the literals 467 and 360. A literal only catches
    an edit to the doc; it cannot catch the case that actually happened, where the
    doc and the JSON agreed with each other and both described a retired panel. So
    the literals are gone and the *relationships* are asserted instead — and
    ``test_the_leakage_artifact_describes_the_served_model_dataset`` below pins the
    absolute values against ``metrics.joblib``, which is the only thing that
    cannot be regenerated into agreement with a stale doc.
    """
    identity = leakage["identity_column_in_sample_r2"]
    keys = leakage["counts"]["n_family_group_keys"]
    base = identity["base_product"]["n_levels"]
    assert identity["family_group_key"]["n_levels"] == keys
    # A `_group_key` fallback can only ever SPLIT a base_product, never merge two.
    assert keys >= base
    # ...and on this panel some rows do fall back, so the two are genuinely
    # different numbers and must never be given the same noun.
    assert keys > base
    # Every identity column is measured on the same rows the progression used.
    for column, block in identity.items():
        assert block["n_rows"] == leakage["counts"]["n_rows"], column


def test_group_key_docstring_states_both_counts(leakage):
    """``lead_time_model._group_key`` is the code of record for these counts.

    Derived from the artifact rather than hardcoded: the docstring used to say
    "collapses 736 MPNs into 360 families", which stayed in the source across two
    panel growths because nothing compared it to anything.
    """
    source = (BACKEND_ROOT / "app" / "ml" / "lead_time_model.py").read_text()
    identity = leakage["identity_column_in_sample_r2"]
    mpns = identity["mpn"]["n_levels"]
    base = identity["base_product"]["n_levels"]
    keys = leakage["counts"]["n_family_group_keys"]
    assert f"collapses {mpns} MPNs into {base} base_product levels" in source, (
        "_group_key's docstring no longer states the MPN and base_product counts "
        "the artifact measured"
    )
    assert f"{keys} group" in source
    # The retired wording conflated the two counts under one noun.
    assert "MPNs into 360 families" not in source


def test_the_leakage_artifact_describes_the_served_model_dataset(leakage, metrics, audit):
    """THE missing gate. The JSON and the served artifact must describe one dataset.

    ``docs/leakage_progression.json`` is regenerated by a script; ``metrics.joblib``
    is written by a retrain. Nothing forced them to agree, so when the panel grew
    from 810 to 1,879 rows and the champion changed from ``random_forest`` to
    ``gradient_boosting``, the JSON simply went on describing the old run — and
    every doc that faithfully quoted it went stale with it. This test is the
    interlock: the progression must have been measured on the same panel bytes,
    the same rows, the same groups and the same champion the deployed model was
    fitted on, or it is not evidence about the deployed model at all.
    """
    prov = metrics["provenance"]
    assert leakage["champion_model"] == metrics["best_lead_time_model"] == audit["model"], (
        "the progression's headline estimator is not the served champion"
    )
    assert (
        leakage["provenance"]["inputs"]["lead_time_panel"]["sha256"]
        == prov["training_data_sha256"]
    ), (
        "the progression was measured on different panel BYTES than the model was "
        "trained on — regenerate it with `python -m seeds.run_leakage_progression`"
    )
    counts = leakage["counts"]
    assert counts["n_rows"] == audit["n_rows"] == prov["n_training_rows"]
    assert counts["n_manufacturers"] == audit["n_manufacturers"] == metrics[
        "lead_time_n_manufacturers"
    ]
    # `leakage_audit.n_families` counts _group_key outputs, like n_family_group_keys.
    assert counts["n_family_group_keys"] == audit["n_families"] == prov["n_distinct_families"]


def test_the_leakage_artifact_was_generated_from_a_clean_tree(leakage):
    """A progression measured from a dirty tree is not reproducible evidence."""
    git = leakage["provenance"]["git"]
    assert git["dirty"] is False, (
        "docs/leakage_progression.json was generated from a modified working tree "
        f"({git.get('dirty_file_count')} dirty paths) — regenerate it from a clean one"
    )


#: A retracted phrase is allowed to survive inside the paragraph that retracts it —
#: deleting the old wording entirely would erase the record of the correction. What is
#: forbidden is quoting it as a live claim.
_CORRECTION = re.compile(
    r"correct|retract|retired|stale|previously|used to be|no longer|superseded"
    r"|is not|earlier revision",
    re.I,
)


def _paragraphs(text: str):
    return re.split(r"\n\s*\n", text)


def _assert_only_in_a_correction(doc: str, text: str, phrase: str, why: str) -> None:
    for paragraph in _paragraphs(text):
        if phrase not in paragraph:
            continue
        assert _CORRECTION.search(paragraph), (
            f"{doc} states '{phrase}' as a live claim. {why}\n"
            f"offending paragraph:\n{paragraph.strip()[:400]}"
        )


#: The grouping-key count of the retired 810-row panel. Kept as a literal ON PURPOSE:
#: the wording it was attached to must never come back, whatever the count is today.
RETIRED_GROUP_KEY_COUNT = 467

#: RESEARCH_TECHNIQUES.md also says "467 families" but is owned by another workstream,
#: so it is named here rather than asserted on — add it when that file is quiet.
@pytest.mark.parametrize(
    "doc", ["LEAKAGE_PROGRESSION.md", "MODEL_CI.md", "PROJECT_OVERVIEW.md"]
)
def test_no_doc_calls_the_group_key_count_a_count_of_part_families(doc, leakage):
    """The retracted phrasing, in every casing it was written in.

    Checked for the CURRENT grouping-key count as well as the retired 467, because
    the error is the noun, not the number: whatever the key count becomes, calling
    it "part families" attaches it to the base_product count, which is smaller.
    """
    text = _doc(doc)
    identity = leakage["identity_column_in_sample_r2"]
    base = identity["base_product"]["n_levels"]
    for count in {leakage["counts"]["n_family_group_keys"], RETIRED_GROUP_KEY_COUNT}:
        for phrase in (
            f"{count} part families", f"{count} families", f"{count} part-families",
        ):
            _assert_only_in_a_correction(
                doc, text, phrase,
                f"{count} counts _group_key outputs; the count of part families / "
                f"base_product values is {base}.",
            )


def test_leakage_doc_quotes_both_counts_with_the_right_names(leakage):
    text = _doc("LEAKAGE_PROGRESSION.md")
    keys = leakage["counts"]["n_family_group_keys"]
    base = leakage["identity_column_in_sample_r2"]["base_product"]["n_levels"]
    assert f"{keys} family grouping keys" in text
    assert f"{base} distinct `base_product` values" in text
    assert f"{leakage['counts']['n_manufacturers']} manufacturers" in text
    assert f"{leakage['counts']['n_rows']} rows" in text


def test_model_ci_leakage_table_matches_the_artifact(leakage):
    """MODEL_CI.md restates the GroupKFold progression; it must restate the measured one."""
    text = _doc("MODEL_CI.md")
    prog = leakage["progression"]
    for value in (prog["random_mean"], prog["family_mean"], prog["manufacturer_mean"]):
        rendered = f"{abs(value):.3f}"
        assert rendered in text, f"MODEL_CI.md does not quote {value:+.3f}"
    # The counts beside that table describe the same run.
    counts = leakage["counts"]
    assert f"{counts['n_rows']:,} rows" in text
    assert f"{counts['n_manufacturers']} manufacturers" in text
    assert f"{counts['n_family_group_keys']} family grouping keys" in text


#: The two retired progressions, both of a `random_forest` champion on an 810-row,
#: 27-manufacturer panel. They are literals because a retracted number is retracted
#: forever — it may appear only inside the paragraph that says it was wrong.
RETIRED_PROGRESSIONS = ("+0.638", "+0.082", "−0.550", "+0.612", "+0.189", "−0.476")


def test_model_ci_quotes_the_served_artifacts_own_leakage_audit(audit):
    """The GroupShuffleSplit trio in MODEL_CI.md comes from metrics.joblib.

    This is the line that rotted: it quoted "+0.612 -> +0.189 -> -0.476" while the
    artifact beside it reported a different champion on a different panel. Read
    from the artifact, it cannot say that again without failing here.
    """
    text = _doc("MODEL_CI.md")
    assert f"`{audit['model']}`" in text, (
        "MODEL_CI.md does not name the champion the leakage audit was computed on"
    )
    for regime in ("random", "family", "manufacturer"):
        value = audit[regime]
        assert f"{abs(value):.4f}" in text, (
            f"MODEL_CI.md does not quote the artifact's {regime} R² ({value:+.4f})"
        )
    assert f"{audit['n_rows']:,} rows" in text


@pytest.mark.parametrize("doc", ["LEAKAGE_PROGRESSION.md", "MODEL_CI.md"])
def test_no_doc_states_a_retired_progression_as_a_live_claim(doc):
    for value in RETIRED_PROGRESSIONS:
        _assert_only_in_a_correction(
            doc, _doc(doc), value,
            "that figure belongs to the retired 810-row / 27-manufacturer / "
            "random_forest vintage. The live numbers come from "
            "docs/leakage_progression.json and metrics.joblib.",
        )


def test_model_ci_ship_gate_verdict_matches_the_artifact(metrics):
    """The "current verdict" table is the artifact's ship gate, not a memory of it."""
    text = _doc("MODEL_CI.md")
    gate = metrics["lead_time_ship_gate"]
    paired = gate["paired"]
    assert f"`{gate['best']}` beats all 4 baselines" in text, (
        f"MODEL_CI.md's verdict row does not name the served champion {gate['best']!r}"
    )
    assert f"`{gate['toughest_baseline']}`" in text
    assert f"**{paired['mean_rmse_reduction_days']} d**" in text
    assert f"**[{paired['ci95_low']}, {paired['ci95_high']}]**" in text
    assert f"**{paired['folds_model_won']}/{paired['n_folds']}**" in text


def test_model_ci_provenance_table_shows_this_artifacts_provenance(metrics):
    """The provenance table is the committed artifact's own, not an illustration."""
    text = _doc("MODEL_CI.md")
    prov = metrics["provenance"]
    for field in (
        "n_training_rows", "n_panel_rows", "n_distinct_families", "n_snapshot_dates",
    ):
        assert f"| `{field}` | `{prov[field]}` |" in text, (
            f"MODEL_CI.md's provenance table does not show {field}={prov[field]}"
        )
    assert f"`{prov['trained_at']}`" in text
    assert f"`{prov['git_sha'][:8]}" in text
    assert f"`{prov['training_data_sha256'][:8]}" in text


def test_the_api_caveat_interpolates_the_audit_instead_of_quoting_a_literal(audit):
    """`GET /ml/model-comparison`'s caveat must be derived from the same artifact.

    The endpoint published a hardcoded trio inside `caveat` while returning a
    different `leakage_audit` block in the same payload. Two checks, because
    either alone can be defeated: the sentence the code produces today must carry
    the artifact's numbers, AND no retired figure may survive as a live literal in
    the module.
    """
    from app.api.ml import _leakage_sentence

    sentence = _leakage_sentence(audit)
    assert audit["model"] in sentence
    for regime in ("random", "family", "manufacturer"):
        assert f"{abs(audit[regime]):.4f}" in sentence, (
            f"the served caveat does not carry the artifact's {regime} R²"
        )
    # An artifact with no audit must say so, never fall back to a remembered number.
    empty = _leakage_sentence(None)
    assert not any(ch.isdigit() for ch in empty), (
        "the no-audit caveat invents a number instead of declining to state one"
    )

    source = (BACKEND_ROOT / "app" / "api" / "ml.py").read_text()
    for value in RETIRED_PROGRESSIONS:
        _assert_only_in_a_correction(
            "app/api/ml.py", source, value.replace("−", "-"),
            "a retired progression is hardcoded in the serve layer again.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. The CVaR dollar figure is NOT "100% data-derived"
# ─────────────────────────────────────────────────────────────────────────────

def test_impact_framing_retracts_the_fully_data_derived_claim():
    text = _doc("IMPACT_FRAMING.md")
    _assert_only_in_a_correction(
        "IMPACT_FRAMING.md", text, "100% data-derived",
        "README.md retracted it: the spend side is real, the probability side is "
        "betweenness centrality and is not calibrated.",
    )
    assert "betweenness centrality" in text, (
        "the retraction must name what the probability side actually is"
    )
    # The probability side is calibrated now (stochastic.py anchors to a cited
    # base rate), so naming the calibration is required — and so is keeping the
    # residual assumption visible. Both, or this drifts back into an overclaim.
    assert "calibrated" in text, (
        "the doc must say the probability side is calibrated, not proxied"
    )
    assert "McKinsey" in text, (
        "the calibration must name its cited base rate, not just assert calibration"
    )
    assert "assumption, not a measurement" in text, (
        "naming the calibration without keeping what is still ASSUMED visible is "
        "how 'fully data-derived' crept in the first time"
    )


def test_the_readme_retraction_is_still_the_one_being_propagated():
    """If README's wording changes, this doc's copy of it must be revisited."""
    readme = (REPO_ROOT / "README.md").read_text()
    assert "betweenness centrality" in readme
    assert "calibrated" in readme
    assert "McKinsey" in readme, (
        "README must cite the base rate the probability side is anchored to"
    )
    assert "still assumed, not measured" in readme or "assumption, not a measurement" in readme, (
        "README must keep saying what is still assumed, not only what is calibrated"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Serving coverage — one denominator, stated
# ─────────────────────────────────────────────────────────────────────────────

#: Measured 2026-08-16 against the tracked supply_chain.db and the committed
#: lead-time artifacts. 8,000 of 8,176 offer x component pairs answer; 736 of 791
#: distinct components answer through the endpoint. The two differ only because 55
#: components have digikey_category IS NULL and those 55 own 176 offers.
PAIR_COVERAGE = "97.85%"
COMPONENT_COVERAGE = "93.05%"
RETRACTED_COVERAGE = "94.4%"


@pytest.mark.parametrize("doc", ["MODEL_CI.md", "archive/ML_API_PUSH_PLAN.md"])
def test_coverage_docs_quote_the_measured_rate_not_the_retracted_one(doc):
    text = _doc(doc)
    assert PAIR_COVERAGE in text, (
        f"{doc} must quote the measured {PAIR_COVERAGE} answer rate over "
        "(offer, component) pairs — the denominator tests/test_serve_coverage.py uses"
    )
    assert COMPONENT_COVERAGE in text, (
        f"{doc} must also give the component-level rate {COMPONENT_COVERAGE} "
        "(736 of 791), because the endpoint answers per component"
    )
    _assert_only_in_a_correction(
        doc, text, RETRACTED_COVERAGE,
        f"{RETRACTED_COVERAGE} matches neither denominator and predates the DB rebuild.",
    )


def test_the_two_coverage_denominators_are_arithmetically_consistent():
    """8000/8176 and 736/791 must actually round to the published percentages."""
    assert f"{8000 / 8176 * 100:.2f}%" == PAIR_COVERAGE
    assert f"{736 / 791 * 100:.2f}%" == COMPONENT_COVERAGE


# ─────────────────────────────────────────────────────────────────────────────
# 4. The series count in model_comparison.py
# ─────────────────────────────────────────────────────────────────────────────

def test_model_comparison_docstring_quotes_a_series_count_that_exists(intermittent):
    source = (BACKEND_ROOT / "app" / "ml" / "model_comparison.py").read_text()
    assert "2,658" not in source and "2658 series" not in source, (
        "model_comparison.py still cites 2,658 series, a number that appears in no "
        "artifact"
    )
    scored = intermittent["configs"]["primary"]["n_series_scored"]
    panel = intermittent["dataset"]["n_series"]
    assert f"{scored:,}" in source, f"expected the scored count {scored:,}"
    assert f"{panel:,}" in source, f"expected the panel size {panel:,}"


def test_the_scored_and_panel_counts_are_the_ones_the_docs_use(intermittent):
    assert intermittent["dataset"]["n_series"] == 2674
    assert intermittent["configs"]["primary"]["n_series_scored"] == 2646
    assert (
        intermittent["configs"]["primary"]["n_series_dropped_undefined"]
        == 2674 - 2646
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. "$4.27 per $1" is conditional on 60,000 units
# ─────────────────────────────────────────────────────────────────────────────

def test_the_knee_ratio_exists_only_at_the_top_volume(cvar):
    primary = cvar["primary"]
    assert primary["x100"]["knee"] is None
    assert primary["x1000"]["knee"] is None
    knee = primary["x10000"]["knee"]
    assert knee is not None
    ratio = knee["vs_risk_neutral"]["usd_of_cvar_removed_per_usd_of_expected_cost"]
    assert round(ratio, 2) == 4.27, f"the artifact's knee ratio is now {ratio}"
    assert primary["x10000"]["total_units"] == 60000


@pytest.mark.parametrize("doc", ["PROJECT_OVERVIEW.md", "archive/ML_API_PUSH_PLAN.md"])
def test_every_quote_of_the_knee_ratio_carries_its_volume_condition(doc):
    """A summary may quote $4.27 only in a sentence that also states the volume."""
    text = _doc(doc)
    for paragraph in _paragraphs(text):
        if "4.27" not in paragraph:
            continue
        assert "60,000" in paragraph or "60000" in paragraph, (
            f"{doc} quotes $4.27 without the 60,000-unit condition:\n{paragraph}"
        )
        assert re.search(r"knee`? is `?null|no trade-off|frontier is flat", paragraph), (
            f"{doc} quotes $4.27 without saying the knee vanishes at lower volume:"
            f"\n{paragraph}"
        )
        assert "CVAR_EFFICIENT_FRONTIER.md" in paragraph, (
            f"{doc} quotes $4.27 without pointing at the fuller disclosure:\n{paragraph}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. INTERMITTENT_DEMAND.md is generated, and still matches its generator
# ─────────────────────────────────────────────────────────────────────────────

def test_the_generated_regions_are_byte_identical_to_the_generator_output(intermittent):
    """The strong check: re-render every GENERATED block from the artifact.

    A hand-edit inside a marked region fails here, which is the whole point — the
    doc used to be hand-transcribed and that is how its numbers drifted.
    """
    from seeds import run_carparts_backtest as rcb

    doc_path = DOCS / "INTERMITTENT_DEMAND.md"
    if not doc_path.is_file():
        pytest.skip("INTERMITTENT_DEMAND.md is not committed in this checkout")
    current = doc_path.read_text()
    expected = rcb.splice_generated(current, rcb.render_blocks(intermittent))
    assert current == expected, (
        "docs/INTERMITTENT_DEMAND.md disagrees with docs/intermittent_demand.json. "
        "Re-run `cd backend && python -m seeds.run_carparts_backtest` rather than "
        "editing the generated regions by hand."
    )


def test_the_doc_declares_every_block_the_generator_renders(intermittent):
    """Marker set and block set must agree, in both directions."""
    from seeds import run_carparts_backtest as rcb

    text = _doc("INTERMITTENT_DEMAND.md")
    declared = set(re.findall(r"<!-- GENERATED:([a-z0-9_]+) BEGIN -->", text))
    closed = set(re.findall(r"<!-- GENERATED:([a-z0-9_]+) END -->", text))
    assert declared == closed, f"unbalanced markers: {declared ^ closed}"
    assert declared == set(rcb.render_blocks(intermittent))


def test_curated_prose_in_the_demand_doc_still_matches_the_artifact(intermittent):
    """Numbers that live in hand-written sentences, outside any marker."""
    text = _doc("INTERMITTENT_DEMAND.md")
    scored = intermittent["configs"]["primary"]["n_series_scored"]
    nonzero = intermittent["dataset"]["nonzero_fraction"]
    assert f"{scored:,}" in text
    assert f"{nonzero * 100:.1f}%" in text
    # §4's Poisson-limit justification is prose, and it quotes the artifact.
    justification = intermittent["size_distribution"]["empirical_justification"]
    for token in re.findall(r"\d+\.\d+|\d+\.\d%", justification):
        assert token in text, (
            f"the size-law justification quotes {token}, which the doc no longer does"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Provenance is stamped, and dirtiness is loud
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "artifact", ["leakage_progression.json", "intermittent_demand.json"]
)
def test_generated_artifacts_carry_a_provenance_block(artifact):
    from seeds.provenance import DIRTY_WARNING

    prov = _json(artifact).get("provenance")
    assert prov is not None, f"{artifact} has no provenance block"
    assert prov["generator"].startswith("seeds.run_")
    assert prov["generated_at_utc"].endswith("Z")
    git = prov["git"]
    assert isinstance(git["dirty"], bool), "dirtiness must be an explicit boolean"
    if git["dirty"]:
        # The whole point of the migration: never a silent `-dirty` suffix again.
        # The property asserted is that the flag carries a loud explanation, not that
        # it carries one exact sentence — the wording lives in seeds/provenance.py.
        assert git["warning"], "a dirty artifact must carry a warning string"
        assert "reproduc" in git["warning"].lower()
        assert "UNCOMMITTED" in git["warning"]
        assert DIRTY_WARNING
    assert prov["inputs"], f"{artifact} records no input hashes"
    for meta in prov["inputs"].values():
        assert meta["exists"], f"{artifact} hashes a missing input: {meta['path']}"
        assert len(meta["sha256"]) == 64


def test_the_demand_artifact_no_longer_hides_dirtiness_in_a_sha_suffix(intermittent):
    """The old failure mode: `meta.git_sha` = '<sha>-dirty', and nothing else."""
    assert "git_sha" not in intermittent["meta"], (
        "meta.git_sha is back. Git state belongs in the provenance block, where "
        "`dirty` is a boolean with a warning attached."
    )


#: Every generated doc paired with the artifact it renders. Verified 2026-09-05:
#: each of these renders its artifact's full commit SHA, and its dirty banner
#: agrees with its artifact's `dirty` flag in both directions.
#:
#: KNOWN GAP, not an oversight: `DIVERSIFICATION_FRONTIER.md` is deliberately
#: absent. Its artifact (`diversification_frontier.json`, dirty at 247cd343f1)
#: renders NEITHER its generating commit NOR the dirty banner, so adding it here
#: would make the suite red on arrival. Fixing that doc is a separate change;
#: this comment exists so the omission is recorded rather than silent.
_DOC_ARTIFACT_PAIRS = [
    ("LEAKAGE_PROGRESSION.md", "leakage_progression.json"),
    ("INTERMITTENT_DEMAND.md", "intermittent_demand.json"),
    ("BENCHMARK_RESULTS.md", "benchmark_results.json"),
    ("BENCHMARK_VOLUME_CURVE.md", "volume_sweep.json"),
    ("CHRONOS_BENCHMARK.md", "chronos_benchmark.json"),
    ("FORECAST_BACKTEST.md", "forecast_backtest.json"),
    ("CVAR_EFFICIENT_FRONTIER.md", "cvar_frontier.json"),
]


@pytest.mark.parametrize(("doc", "artifact"), _DOC_ARTIFACT_PAIRS)
def test_the_doc_renders_the_commit_its_artifact_recorded(doc, artifact):
    """The doc must disclose its artifact's git state — and stop disclosing it.

    The original form of this test asserted only one half: *if* the artifact is
    dirty, the doc must say "DIRTY WORKING TREE". That is a disclosure check, not
    a reproducibility gate — it is satisfied by a doc that loudly admits its
    numbers are unreproducible, and it says nothing at all about a clean one. The
    reproducibility gate is
    `test_every_generated_artifact_was_generated_from_a_clean_tree` above.

    The half that was missing is the one this repo actually keeps failing: a
    banner that OUTLIVES the condition it described. Regenerate an artifact from
    a clean tree, forget to re-render its doc, and the doc goes on warning
    readers that a now-reproducible result is not reproducible. So the `else`
    below is asserted too.
    """
    text = _doc(doc)
    prov = _provenance_block(_json(artifact))
    assert prov is not None, f"{artifact} carries no provenance block to render"
    git = prov["git"]
    assert git["commit"] in text, f"{doc} does not show its generating commit"
    if git["dirty"]:
        assert "DIRTY WORKING TREE" in text, (
            f"{doc} was generated from a dirty tree and does not say so"
        )
    else:
        assert "DIRTY WORKING TREE" not in text, (
            f"{artifact} is now clean, but {doc} still carries a DIRTY WORKING "
            "TREE banner. Re-render the doc — a warning that outlived its "
            "condition tells readers a reproducible result is not reproducible."
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. EVERY generated artifact must come from a clean tree — not just one
# ─────────────────────────────────────────────────────────────────────────────
#
# `test_the_leakage_artifact_was_generated_from_a_clean_tree` (above) states the
# right rule — "a progression measured from a dirty tree is not reproducible
# evidence" — and then applies it to exactly ONE of the nine artifacts under
# `docs/` that carry a `provenance.git` block. That is the repo's recurring
# failure mode in miniature: a good rule written once, bound to one filename, and
# never extended to its class. On 2026-09-05 seven of the nine were dirty and no
# check said so.
#
# So the sweep below DISCOVERS artifacts instead of listing them. A new
# `docs/*.json` that stamps provenance is covered the day it lands, with no edit
# here — which is the entire point of generalising.

#: The two nestings the generators actually emit. `seeds/provenance.py` returns
#: one block; `run_forecast_backtest` and `run_chronos_benchmark` file it under
#: `meta`, everything else at the top level.
_PROVENANCE_PATHS = (("provenance",), ("meta", "provenance"))


def _provenance_block(payload: Any) -> Dict[str, Any] | None:
    """The artifact's ``provenance`` mapping, whichever shape it uses.

    Returns ``None`` when the artifact carries no git-stamped provenance at all
    (``docs/backend_verification.json`` is a hand-run check log, not a generator
    output, and is correctly out of scope).
    """
    if not isinstance(payload, dict):
        return None
    for path in _PROVENANCE_PATHS:
        node: Any = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and isinstance(node.get("git"), dict):
            return node
    return None


def _artifacts_with_git_provenance() -> list[str]:
    """Every ``docs/*.json`` that stamps a git block, discovered at collection."""
    found = []
    for path in sorted(DOCS.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):  # pragma: no cover - unreadable file
            continue
        if _provenance_block(payload) is not None:
            found.append(path.name)
    return found


#: Artifacts that were generated from a dirty tree BEFORE this gate existed.
#:
#: Dated **2026-09-05**. This is a debt list, not a permission slip: every entry
#: is a published number whose recorded commit will NOT reproduce it. Each names
#: the artifact's current commit and dirty-file count so an exemption cannot
#: silently start covering a *different* dirty artifact, and the test below fails
#: the moment one of these turns clean — a stale exemption must be deleted, not
#: left to rot. That is the same "label outliving the code" failure this repo
#: keeps hitting.
#:
#: Regenerate from a clean tree, then remove the entry. Nothing here may be
#: cleared by editing a provenance stamp.
CLEAN_TREE_EXEMPTIONS: Dict[str, Dict[str, Any]] = {
    # EMPTY, AND THAT IS THE POINT. On 2026-09-05 this held seven entries —
    # benchmark_results, chronos_benchmark, diversification_frontier,
    # forecast_backtest, intermittent_demand, newsvendor and volume_sweep — every
    # artifact in docs/ except the two that had already been regenerated. All seven
    # were regenerated from a clean tree that same day and every entry was deleted.
    #
    # Not one substantive number moved across any of the seven. The only changes
    # were provenance blocks, timestamps and wall-clock latency. So the dirtiness
    # was never an accuracy problem; it was a REPRODUCIBILITY problem, and the
    # difference is worth keeping straight: a clean stamp says the tree was tidy
    # when the file was written, while a leaf-by-leaf diff against the previous
    # version says the code still produces the number. Regenerating bought both.
    #
    # Adding an entry here is legitimate — sometimes an artifact must ship before
    # its tree can be cleaned. But it is a DEBT, not a category. Write the commit
    # and the dirty count so the exemption cannot silently drift onto a different
    # artifact, and delete it the moment the artifact goes clean:
    # `test_no_clean_tree_exemption_names_a_clean_artifact` fails if you do not,
    # which is what stops this list becoming another label that outlives its
    # condition.
}


def test_the_clean_tree_sweep_actually_finds_artifacts():
    """A sweep that discovers nothing passes vacuously. Prove it discovers."""
    found = _artifacts_with_git_provenance()
    assert len(found) >= 5, (
        "the clean-tree sweep found only "
        f"{found} — docs/*.json should carry many git-stamped artifacts. A "
        "discovery-based gate that finds nothing is a green check that cannot "
        "go red."
    )
    assert "leakage_progression.json" in found, (
        "the sweep no longer reaches leakage_progression.json, the one artifact "
        "the original single-file rule covered. The generalisation must be a "
        "superset of what it replaced, never a replacement that drops it."
    )


@pytest.mark.parametrize("artifact", _artifacts_with_git_provenance())
def test_every_generated_artifact_was_generated_from_a_clean_tree(artifact):
    """The leakage rule, applied to its whole class.

    An artifact generated from a modified working tree is not reproducible
    evidence: checking out its recorded SHA will not reproduce its numbers, so
    every figure the site publishes from it is unfalsifiable.

    Two directions are asserted, and both can fail:

    1. An artifact NOT on ``CLEAN_TREE_EXEMPTIONS`` must be clean.
    2. An artifact ON the list must still be dirty, at the same commit and the
       same dirty-file count. Once it is regenerated the exemption is a lie
       about the repo and has to be deleted.
    """
    git = _provenance_block(_json(artifact))["git"]
    dirty = git["dirty"]
    exemption = CLEAN_TREE_EXEMPTIONS.get(artifact)

    if exemption is None:
        assert dirty is False, (
            f"docs/{artifact} was generated from a modified working tree "
            f"({git.get('dirty_file_count')} dirty paths at "
            f"{git.get('commit_short')}) and is not on the dated exemption list. "
            "Regenerate it from a clean tree — never by editing the provenance "
            "stamp, which would doctor the record. If it genuinely cannot be "
            "regenerated yet, add a dated entry to CLEAN_TREE_EXEMPTIONS in "
            f"{Path(__file__).name} saying why."
        )
        return

    assert dirty is True, (
        f"docs/{artifact} is now CLEAN, but CLEAN_TREE_EXEMPTIONS still excuses "
        f"it (recorded {exemption['commit_short']}, "
        f"{exemption['dirty_file_count']} dirty paths). Delete that entry — a "
        "stale exemption is a label that outlived the code, and the next reader "
        "will believe a reproducible artifact is not reproducible."
    )
    assert git.get("commit_short") == exemption["commit_short"], (
        f"docs/{artifact} now records commit {git.get('commit_short')}, not the "
        f"{exemption['commit_short']} its exemption describes. The exemption "
        "excuses one specific known-dirty artifact, not every future dirty "
        "regeneration of it — re-verify and update or delete the entry."
    )
    assert git.get("dirty_file_count") == exemption["dirty_file_count"], (
        f"docs/{artifact} now reports {git.get('dirty_file_count')} dirty paths, "
        f"not the {exemption['dirty_file_count']} its exemption describes — it "
        "was regenerated from a different dirty tree. Re-verify the entry."
    )


@pytest.mark.parametrize("artifact", sorted(CLEAN_TREE_EXEMPTIONS))
def test_no_clean_tree_exemption_names_an_artifact_that_is_gone(artifact):
    """An exemption for a deleted artifact is dead weight that hides the count."""
    assert artifact in _artifacts_with_git_provenance(), (
        f"CLEAN_TREE_EXEMPTIONS excuses docs/{artifact}, which no longer exists "
        "or no longer carries a provenance.git block. Delete the entry."
    )
    assert CLEAN_TREE_EXEMPTIONS[artifact]["reason"].strip(), (
        f"the exemption for docs/{artifact} carries no reason"
    )
