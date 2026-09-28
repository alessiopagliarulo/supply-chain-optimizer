#!/usr/bin/env python3
"""Rewrite the lead-time PANEL figures in the published docs from the CSV on disk.

The weekly collector (.github/workflows/collect-lead-times.yml) appends a
snapshot to ``backend/seeds/data/lead_time_panel/observed_lead_times.csv`` and
pushes it straight to main. README.md, docs/PROJECT_OVERVIEW.md and
docs/RESEARCH_TECHNIQUES.md state the panel's size in prose, and
tests/test_lead_time_panel_docs_pinned_to_csv.py checks that prose against the
CSV. Before this script existed every collector run turned that guard red,
because the rows moved and the prose did not. The collector now runs this
script after collecting and before committing, so the new rows and the prose
that describes them land in the same commit.

    cd backend
    python scripts/sync_panel_docs.py            # rewrite the docs in place
    python scripts/sync_panel_docs.py --check    # exit 1 if any doc is out of date

WHAT IT REWRITES. Only figures that are a pure function of the panel CSV: the
row total, the snapshot count, the per-snapshot breakdown, the date span, the
panel sha256 prefix, the STM32F103 / ATMEGA328 family row counts, and how far
the panel has moved past the served model (snapshots behind, rows and dates not
in the training cut). The training cut itself (row count, snapshot count, sha,
date, R²) is read from ``data/ml_models/metrics.joblib`` and never written: it
describes the served artifact and changes only when someone retrains.

HOW. Each figure lives in a sentence matched by one narrow rule below, and each
rule must match an exact number of times in each doc. Only the named groups in a
match are replaced; the words around them are left alone. If a sentence has been
reworded so a rule no longer matches (or matches more often), the script refuses
to write anything and says which rule and doc. tests/test_sync_panel_docs.py
runs this against the committed docs, so a rewording fails CI on its own pull
request instead of failing the collector on a Monday.

WHAT IT DOES NOT DO. It does not replace the guard. The guard still reads the
prose and the CSV independently, so a hand edit to a wrong number, or a
collector run that skipped this step, still turns it red.

After a retrain the "N snapshots behind" and "rows not in it" sentences stop
being true in their current form; the script refuses to render a gap of zero,
and the retrain's own pull request rewrites those sentences and the rules here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_CSV = Path("backend/seeds/data/lead_time_panel/observed_lead_times.csv")
METRICS = Path("backend/data/ml_models/metrics.joblib")
DOCS = ("README.md", "docs/PROJECT_OVERVIEW.md", "docs/RESEARCH_TECHNIQUES.md")

_DATE = r"\d{4}-\d{2}-\d{2}"
_NUM = r"\d[\d,]*"
_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


@dataclass(frozen=True)
class PanelFacts:
    total: int
    per_date: Dict[str, int]
    sha256: str
    stm32f103_rows: int
    atmega328_rows: int
    cut_rows: int
    cut_dates: Tuple[str, ...]

    @property
    def dates(self) -> List[str]:
        return sorted(self.per_date)

    @property
    def dates_since_cut(self) -> List[str]:
        return [d for d in self.dates if d not in self.cut_dates]


def _count(n: int) -> str:
    return f"{n:,}"


def _word(n: int) -> str:
    return _WORDS[n] if n < len(_WORDS) else str(n)


def _date_list(dates: List[str]) -> str:
    return dates[0] if len(dates) == 1 else ", ".join(dates[:-1]) + " and " + dates[-1]


def _gap(facts: PanelFacts) -> int:
    gap = len(facts.dates_since_cut)
    if gap == 0:
        raise SystemExit(
            "the served model's training cut covers every snapshot on disk, so the "
            "'snapshots behind' / 'rows not in it' sentences are no longer true. "
            "Rewrite them (and their rules in this script) as part of the retrain."
        )
    return gap


#: Fact name -> how to render it. A rule's named groups must all be keys here.
RENDER: Dict[str, Callable[[PanelFacts], str]] = {
    "total": lambda f: _count(f.total),
    "n_snapshots": lambda f: str(len(f.per_date)),
    "breakdown": lambda f: ", ".join(f"{_count(f.per_date[d])} on {d}" for d in f.dates),
    "first_date": lambda f: f.dates[0],
    "last_date": lambda f: f.dates[-1],
    "sha8": lambda f: f.sha256[:8],
    "gap_phrase": lambda f: f"{_word(_gap(f))} snapshot{'s' if _gap(f) != 1 else ''}",
    "rows_since_cut": lambda f: _count(f.total - f.cut_rows),
    "dates_since_cut": lambda f: _date_list(f.dates_since_cut),
    "since_cut_noun": lambda f: "snapshots" if _gap(f) != 1 else "snapshot",
    "stm32f103_rows": lambda f: _count(f.stm32f103_rows),
    "atmega328_rows": lambda f: _count(f.atmega328_rows),
}


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    expect: Dict[str, int]  # doc -> exact match count; absent means 0


def _rule(name: str, pattern: str, **expect: int) -> Rule:
    docs = {"readme": "README.md", "overview": "docs/PROJECT_OVERVIEW.md",
            "research": "docs/RESEARCH_TECHNIQUES.md"}
    return Rule(name, re.compile(pattern), {docs[k]: v for k, v in expect.items()})


RULES = [
    # "5,632 real observations across 9 snapshot dates", the guard's PANEL_ACROSS_RE subject.
    _rule("panel_across",
          rf"(?P<total>{_NUM}) real (?:DigiKey )?(?:observations|observed lead times) "
          rf"across (?P<n_snapshots>\d+) snapshot",
          readme=2, overview=1),
    # "5,632 rows / 9 snapshots on disk", the guard's PANEL_ON_DISK_RE subject.
    _rule("rows_on_disk", rf"(?P<total>{_NUM}) rows / (?P<n_snapshots>\d+) snapshots on disk",
          overview=1, research=1),
    # "(75 on 2026-07-01, 742 on 2026-08-15, ...)", the guard's PER_SNAPSHOT_RE subject.
    _rule("breakdown",
          rf"\((?P<breakdown>{_NUM} on {_DATE}(?:,\s+{_NUM} on {_DATE})*)\)",
          readme=2, overview=1),
    _rule("date_span",
          rf"holds (?P<n_snapshots>\d+) snapshot dates\s+spanning (?P<first_date>{_DATE}) "
          rf"to (?P<last_date>{_DATE})",
          overview=1),
    _rule("now_holds", rf"a panel that now holds (?P<total>{_NUM}) rows", research=1),
    _rule("observed_plus_live",
          rf"\((?P<total>{_NUM}) real observed lead times \+ live pricing\)", readme=1),
    _rule("family_rows",
          rf"(?P<stm32f103_rows>{_NUM}) STM32F103 rows and (?P<atmega328_rows>{_NUM}) "
          rf"ATMEGA328 rows\s+of the (?P<total>{_NUM}) on disk",
          readme=1),
    _rule("snapshots_behind", r"(?P<gap_phrase>\w+ snapshots?) behind the panel",
          readme=1, overview=1),
    _rule("rows_not_in_cut",
          rf"The (?P<rows_since_cut>{_NUM}) rows from the "
          rf"(?P<dates_since_cut>{_DATE}(?:(?:,\s+|\s+and\s+){_DATE})*)\s+"
          rf"(?P<since_cut_noun>snapshots?) are not\s+in it",
          readme=1),
    _rule("sha_since_run",
          rf"`(?P<sha8>[0-9a-f]{{8}})…` since the (?P<last_date>{_DATE})\s+collector run",
          readme=2),
    _rule("sha_on_disk",
          r"snapshots on disk\*\* \(sha256 `(?P<sha8>[0-9a-f]{8})…`\)", research=1),
]


def read_facts(root: Path) -> PanelFacts:
    import joblib

    raw = (root / PANEL_CSV).read_bytes()
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")))
    per_date = Counter(r["snapshot_date"] for r in rows)

    # The training cut is the head of the append-only panel: the first
    # n_panel_rows rows, spanning n_snapshot_dates dates. tests/
    # test_artifacts_pinned_to_code.py already fails if the head is rewritten.
    prov = joblib.load(root / METRICS)["provenance"]
    cut_rows = int(prov["n_panel_rows"])
    cut_dates = tuple(sorted({r["snapshot_date"] for r in rows[:cut_rows]}))
    later = {r["snapshot_date"] for r in rows[cut_rows:]}
    if len(rows) < cut_rows or len(cut_dates) != int(prov["n_snapshot_dates"]) \
            or later & set(cut_dates):
        raise SystemExit(
            f"the first {cut_rows} panel rows are not the {prov['n_snapshot_dates']}-snapshot "
            f"training cut metrics.joblib describes (cut dates {cut_dates}, later dates "
            f"{sorted(later)}); refusing to describe the gap"
        )

    def family(prefix: str) -> int:
        return sum(r["mpn"].upper().startswith(prefix) for r in rows)

    return PanelFacts(
        total=len(rows),
        per_date=dict(per_date),
        sha256=hashlib.sha256(raw).hexdigest(),
        stm32f103_rows=family("STM32F103"),
        atmega328_rows=family("ATMEGA328"),
        cut_rows=cut_rows,
        cut_dates=cut_dates,
    )


def render_doc(doc: str, text: str, facts: PanelFacts) -> str:
    """Return ``text`` with every rule's figures re-rendered from ``facts``."""
    for rule in RULES:
        matches = list(rule.pattern.finditer(text))
        want = rule.expect.get(doc, 0)
        if len(matches) != want:
            raise SystemExit(
                f"{doc}: rule {rule.name!r} matched {len(matches)} time(s), expected {want}. "
                "The sentence was reworded or duplicated; update RULES in "
                "backend/scripts/sync_panel_docs.py in the same change."
            )
        out, pos = [], 0
        for m in matches:
            for key, span in sorted(
                ((k, m.span(k)) for k in m.groupdict()), key=lambda kv: kv[1][0]
            ):
                out.append(text[pos:span[0]])
                out.append(RENDER[key](facts))
                pos = span[1]
        out.append(text[pos:])
        text = "".join(out)
    return text


def sync(root: Path, check: bool = False) -> List[str]:
    """Rewrite (or with ``check``, only compare) every doc; return those that differ."""
    facts = read_facts(root)
    rendered = {}
    for doc in DOCS:
        path = root / doc
        old = path.read_text(encoding="utf-8")
        new = render_doc(doc, old, facts)
        if new != old:
            rendered[path] = new
    if not check:
        for path, new in rendered.items():
            path.write_text(new, encoding="utf-8")
    return [str(p.relative_to(root)) for p in rendered]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report out-of-date docs and exit 1 instead of rewriting them")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    changed = sync(args.root, check=args.check)
    verb = "out of date" if args.check else "updated"
    for doc in changed:
        print(f"{verb}: {doc}")
    if not changed:
        print("panel figures already match the CSV")
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
