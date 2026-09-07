"""
Drift guard: published PANEL figures must agree with the CSV on disk, not with
each other.

Why this file exists (2026-09-01)
----------------------------------
A weekly bot cron (``collect-lead-time-panel``, see
``.github/workflows/collect-lead-times.yml``) commits new rows to
``backend/seeds/data/lead_time_panel/observed_lead_times.csv`` every Monday. On
2026-08-31 it did: the CSV grew to 2,664 rows across five snapshot dates. But
``README.md``, ``docs/PROJECT_OVERVIEW.md`` and ``docs/RESEARCH_TECHNIQUES.md``
all kept publishing the OLD figure — "1,922 observations across four snapshot
dates" — in the same commit. That false number was live on public GitHub.
Nothing gated it, so no test went red.

The same thing happened again on 2026-09-07 (panel 2,664 -> 3,406 rows, five
snapshots -> six) and this file is what caught it. The reason it was allowed to
reach main at all is separate and now fixed: the collector pushes with the
default ``GITHUB_TOKEN``, and GitHub does not raise ``push`` events for commits
made with that token, so ``ci.yml`` could never run on the very commit that
falsified the docs. ``ci.yml`` now also triggers on ``workflow_run`` from the
collector and on a weekly schedule.

This repo has already shipped three defects where a doc and an artifact were
stale *together* and a doc-vs-artifact test stayed green (see
``test_artifacts_pinned_to_code.py``). So this file does NOT compare doc to doc,
and does not compare doc to another doc's restated figure either. It reads the
CSV itself and checks every doc's claim about *the panel* against the actual
parsed rows.

The one thing this file must NOT do is force the "served model" figures (e.g.
"trained on 1,879 rows / 4 snapshots") to match the CSV. Those describe an
older, frozen training cut and are *supposed* to differ from the live panel
whenever a retrain is owed — see the ``training_data_staleness`` block at
``GET /api/v1/ml/model-info``. Conflating the two subjects was the original
defect (the panel grew past the artifact while docs still described the older
training cut as if it were the panel). So the regexes below
are deliberately narrow: they only match sentences that describe *the panel /
the CSV on disk* (" observations across N snapshot(s)", "rows across N
snapshot(s)", "rows / N snapshots on disk"), never sentences that describe what
the served artifact was *trained on*.
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

REPO_ROOT = BACKEND_ROOT.parent
DOCS = REPO_ROOT / "docs"

PANEL_CSV = (
    REPO_ROOT / "backend" / "seeds" / "data" / "lead_time_panel" / "observed_lead_times.csv"
)

# Every PUBLISHED doc that states a panel figure. A doc removed from the repo is
# removed from here too -- asserting against a file that is no longer committed
# would pass on the maintainer's disk (where the file still exists) and fail in a
# clean CI checkout, which is the exact inversion of what this file is for.
DOC_PATHS = {
    "README.md": REPO_ROOT / "README.md",
    "docs/PROJECT_OVERVIEW.md": DOCS / "PROJECT_OVERVIEW.md",
    "docs/RESEARCH_TECHNIQUES.md": DOCS / "RESEARCH_TECHNIQUES.md",
}

# A sentence describing the PANEL: "<count> [real] [DigiKey] observations|rows|
# observed lead times|lead-time observations across <N> snapshot(s) [dates]".
# Requires the literal word "across" directly between the count phrase and the
# snapshot-count phrase, which is how every current panel-total sentence in
# these docs is written, and how the "trained on / usable rows of" artifact
# sentences are NOT written (those use "/" or parenthetical asides instead).
PANEL_ACROSS_RE = re.compile(
    r"([\d][\d,]*)\s+"
    r"(?:real\s+)?(?:DigiKey\s+)?"
    r"(?:observed lead times|lead-time observations|observations|rows)\s+"
    r"across\s+(?:\d+|three|four|five|six|seven)\s+snapshots?(?:\s+dates)?",
    re.IGNORECASE,
)

# The one doc (RESEARCH_TECHNIQUES.md) that phrases the panel total as
# "<count> rows / <N> snapshots on disk" instead of using "across".
PANEL_ON_DISK_RE = re.compile(
    r"([\d][\d,]*)\s+rows\s*/\s*(?:\d+|three|four|five|six|seven)\s+snapshots\s+on\s+disk",
    re.IGNORECASE,
)

# Per-snapshot breakdown claims of the form "<count> on <YYYY-MM-DD>", e.g.
# "742 on 2026-08-24". This phrasing is unique to panel breakdowns in these
# docs -- the served-artifact sentences never attach a count to a specific
# calendar date this way.
PER_SNAPSHOT_RE = re.compile(r"(\d[\d,]*)\s+on\s+(2026-\d{2}-\d{2})")


def _read_panel_csv() -> tuple[int, Counter]:
    """Return (total_row_count, Counter of snapshot_date -> row_count)."""
    with PANEL_CSV.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert "snapshot_date" in (reader.fieldnames or []), (
        f"expected a 'snapshot_date' column in {PANEL_CSV}, got {reader.fieldnames!r}"
    )
    per_date = Counter(row["snapshot_date"] for row in rows)
    return len(rows), per_date


def _fmt(n: int) -> int:
    """Normalize a comma-formatted number string to an int for comparison."""
    return int(str(n).replace(",", ""))


def test_panel_csv_parses_to_a_plausible_row_count() -> None:
    """Anti-vacuity guard: fail loudly if the CSV path moves or stops parsing,
    rather than let every other test in this file pass by comparing against
    zero rows / zero dates."""
    assert PANEL_CSV.is_file(), f"panel CSV not found at {PANEL_CSV}"
    total, per_date = _read_panel_csv()
    assert total > 1000, f"expected well over 1000 panel rows, parsed {total}"
    assert len(per_date) >= 4, f"expected at least 4 snapshot dates, parsed {sorted(per_date)}"
    assert all(count > 0 for count in per_date.values())


def test_all_four_docs_are_readable() -> None:
    """Anti-vacuity guard: if a doc path is wrong, every regex below would
    (falsely) find zero matches and every 'at least one match' assertion would
    legitimately fail loudly -- but let's fail with a clear message instead."""
    for label, path in DOC_PATHS.items():
        assert path.is_file(), f"expected doc at {path} ({label})"
        assert path.read_text(encoding="utf-8").strip(), f"{label} is empty"


def test_every_doc_states_the_panel_total_and_it_matches_the_csv() -> None:
    """The headline "N [real] observations/rows across M snapshots" claim, in
    whatever phrasing each doc currently uses, must equal the CSV's actual row
    count -- not the figure that was true on some earlier snapshot."""
    actual_total, _ = _read_panel_csv()

    for label, path in DOC_PATHS.items():
        text = path.read_text(encoding="utf-8")
        matches = [_fmt(m) for m in PANEL_ACROSS_RE.findall(text)]
        matches += [_fmt(m) for m in PANEL_ON_DISK_RE.findall(text)]

        assert matches, (
            f"{label}: found no 'panel total across N snapshots' sentence to check. "
            "Either the doc no longer states the panel total (fine, but then this "
            "test needs a matching regex added), or the wording drifted far enough "
            "that PANEL_ACROSS_RE / PANEL_ON_DISK_RE no longer match it -- in which "
            "case treat this as a red flag, not a reason to loosen the regex blindly."
        )
        for found in matches:
            assert found == actual_total, (
                f"{label} claims the panel holds {found:,} observations, but "
                f"{PANEL_CSV.relative_to(REPO_ROOT)} actually parses to "
                f"{actual_total:,} rows. Re-run the panel-total prose through the "
                "real CSV before publishing. Nothing this repo publishes may be "
                "contradicted by the code or the artifacts."
            )


def test_per_snapshot_breakdowns_match_the_csv() -> None:
    """Wherever a doc gives the per-date breakdown ("75 on 2026-07-01, 742 on
    2026-08-15, ..."), every (count, date) pair must match the CSV's actual
    per-date row count. This is a stricter, date-addressed check on top of the
    total: it catches a doc whose total happens to match by coincidence (or
    after a hand-edit) while individual snapshot counts are wrong."""
    _, actual_per_date = _read_panel_csv()

    docs_with_breakdowns = 0
    for label, path in DOC_PATHS.items():
        text = path.read_text(encoding="utf-8")
        pairs = PER_SNAPSHOT_RE.findall(text)
        if not pairs:
            continue
        docs_with_breakdowns += 1
        for count_str, date_str in pairs:
            found = _fmt(count_str)
            actual = actual_per_date.get(date_str)
            assert actual is not None, (
                f"{label} claims {found} rows on snapshot date {date_str}, but the "
                f"CSV has no rows at all for that date. Current dates on disk: "
                f"{sorted(actual_per_date)}"
            )
            assert found == actual, (
                f"{label} claims {found} rows on snapshot date {date_str}, but "
                f"{PANEL_CSV.relative_to(REPO_ROOT)} actually has {actual} rows "
                f"for that date."
            )

    assert docs_with_breakdowns >= 2, (
        "expected at least README.md and docs/PROJECT_OVERVIEW.md to publish a "
        f"per-snapshot breakdown of the panel -- found {docs_with_breakdowns}. If "
        "the breakdown prose was removed entirely, delete this test's expectation "
        "deliberately; do not let it pass by silently matching nothing."
    )


def test_served_artifact_claims_are_not_forced_to_match_the_live_panel() -> None:
    """Guard against re-introducing the original conflation bug the other way:
    a future edit must not make this file assert that the served-model
    training count (2,615 rows / 5 snapshots, trained 2026-09-03) equals the
    live CSV's row count. They are allowed -- expected -- to differ, and they
    differ even immediately after a retrain, because ``build_training_design``
    drops rows with no label or a bad DigiKey match (2,664 in that cut of the
    CSV, 2,615 fitted). Between retrains the gap widens by a whole snapshot --
    it is a full snapshot wide right now, with 3,406 rows on disk against a
    2,664-row training cut. README.md / docs/PROJECT_OVERVIEW.md /
    docs/RESEARCH_TECHNIQUES.md all publish both subjects side by side.
    This test just documents that the panel-total regexes above do not match the
    served-artifact sentences, so nobody "fixes" a future failure by loosening
    PANEL_ACROSS_RE until it accidentally does.

    The sentences below are copied from those docs and are the 2026-09-03
    vintage; they are FIXTURES for the regexes, not assertions about the docs'
    current wording, so a doc rewrite does not break this test -- but keeping
    them current is what makes the fixture representative."""
    actual_total, _ = _read_panel_csv()

    served_model_sentences = [
        # README.md
        "was trained **2026-09-03** on the **2,615** usable rows of the then "
        "2,664-row, five-snapshot cut of the panel, with **324** features",
        "The served model is fitted on an earlier cut of this panel (2,615 "
        "usable rows of the then 2,664-row, five-snapshot cut, trained "
        "2026-09-03)",
        # docs/PROJECT_OVERVIEW.md
        "the served model is fitted on an earlier cut -- 2,615 usable rows of "
        "the then five-snapshot panel / 324 API-derived features, retrained "
        "2026-09-03",
        # docs/RESEARCH_TECHNIQUES.md
        "the **served model is fitted on an earlier cut of it** -- 2,615 usable "
        "rows of the then 2,664-row, five-snapshot panel (sha256 "
        "`c68e2891...`), retrained 2026-09-03",
    ]

    for sentence in served_model_sentences:
        panel_matches = PANEL_ACROSS_RE.findall(sentence) + PANEL_ON_DISK_RE.findall(sentence)
        assert not panel_matches, (
            "a served-artifact sentence matched the panel-total regex "
            f"({panel_matches!r}); PANEL_ACROSS_RE / PANEL_ON_DISK_RE must never "
            f"match artifact-vintage prose. Sentence: {sentence!r}"
        )

    # Sanity: the trained-row count must not equal the live total, or this guard
    # is not actually exercising the distinction it claims to. The two can never
    # coincide while build_training_design drops any row at all, but assert it
    # rather than assume it.
    trained_rows = 2615
    assert trained_rows != actual_total, (
        f"the served-artifact row count ({trained_rows}) now equals the live "
        f"panel total ({actual_total}) -- the distinction this test documents "
        "is no longer observable; re-check both figures are still current."
    )
