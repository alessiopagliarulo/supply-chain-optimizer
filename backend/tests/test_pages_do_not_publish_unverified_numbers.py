"""No page may render a number that nothing verifies.

WHY THIS FILE EXISTS (2026-09-05)
---------------------------------
A page (the since-removed FrontierPage) once shipped figures for five days that had never
matched ANY version of the artifact they described, and a doc-vs-artifact test
structurally could not see it, because the page is neither.

A per-page pin test fixes ONE page. It cannot fix the class, because it only knows the
`data-testid` anchors someone remembered to add. The generalisation is the inverse
question: instead of "does this pinned number match its artifact?", ask

    **does EVERY number this page renders have anything at all standing behind it?**

This file asks that of every page. A rendered number passes only if it is

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
`placeholder`. The since-removed NewsvendorPage published "51 monthly observations"
through `hint=`; that is a claim, and it would be invisible to a text-node-only scan.

THREE GAPS THIS GUARD DOES NOT CLOSE — stated so nobody mistakes green for total
--------------------------------------------------------------------------------
1. **Literals that reach the screen through a JS string constant.** The since-removed
   ResiliencePage rendered "1,000 Monte Carlo scenarios" from ``const MC_SCENARIOS = 1000``.
   That is a typed literal that can go stale, but not a JSX text node. Scanning every string
   in a `.tsx` file would drown the guard in false positives, so such a figure is pinned by
   name in `test_pages_match_their_sources.py` instead.
2. **`aria-label`.** Screen-reader text is published text and can carry figures. It is
   excluded here only because the scope of this guard is what a sighted reader sees;
   that is a deliberate, known hole, not an oversight.
3. **The testid exemption is per-ELEMENT, not per-number.** Every number inside a pinned
   anchor is exempt, including digits the pin test does not assert. Criterion (a) says
   "an anchor a pin test reads", and it cannot know which digits inside that anchor the
   assertions actually cover.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._jsx import JsxText, decode_entities, text_nodes

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = BACKEND_ROOT.parent / "frontend" / "src" / "pages"

#: Every page under `frontend/src/pages`. Discovered, never listed: a new page must be
#: triaged, not silently exempt because someone forgot to add it here.
PAGES = sorted(PAGES_DIR.glob("*.tsx"))

#: `data-testid` anchors a pin test reads against an artifact. Import them from the pin
#: test rather than retyping them, so dropping a pin cannot quietly widen this guard's
#: exemptions. Empty since FrontierPage (the only pinned page) was removed with the
#: sourcing optimizer.
PINNED: dict[str, frozenset[str]] = {}

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

#: Empty since the sourcing-era pages were removed (issue #16): none of the risk-measure
#: and percentile names it used to exempt (CVaR-95, P10, 95% CI, ...) is rendered any more,
#: and `test_every_global_allowlist_phrase_is_still_rendered_somewhere` requires dropping
#: an exemption nobody uses.
GLOBAL_ALLOW: tuple[tuple[str, str], ...] = ()

PAGE_ALLOW: dict[str, tuple[tuple[str, str], ...]] = {
    "NotFoundPage.tsx": (
        (
            "404",
            "the HTTP status this route represents.",
        ),
    ),
    "LandingPage.tsx": (
        (
            "frozen 2024 snapshot",
            "the catalogue's snapshot year. The landing page makes no network request, so it "
            "cannot read it from GET /catalogue/provenance like the other pages do; it is "
            "pinned by name to catalogue_provenance.SNAPSHOT_YEAR in "
            "test_pages_match_their_sources.py.",
        ),
    ),
    "DigitalTwinPage.tsx": (
        (
            "Lateness p95",
            "the NAME of a statistic: the 95th percentile of per-stop lateness, which the "
            "page renders from the API's `p95_lateness` field. A label, not a value.",
        ),
    ),
}

#: Published numbers that NOTHING in this repo can verify. They are not allowlist
#: entries in good standing — they are a standing debt, listed so that (a) the suite
#: stays green on a known set and (b) no NEW unverifiable number can be added without
#: this list changing, which `test_the_unverifiable_debt_has_not_grown` refuses to let
#: happen quietly.
#:
#: EMPTY. The last entry (BenchmarkPage's "exist only from run 8 onward") left with
#: that page when the sourcing optimizer was removed.
KNOWN_UNVERIFIED: dict[str, tuple[tuple[str, str], ...]] = {}


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
    assert len(PAGES) >= 5, (
        f"expected the landing, Map, Route Plan, Digital Twin, Benchmarks and 404 pages of "
        f"{PAGES_DIR}; found {[p.name for p in PAGES]}. "
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
#: NewsvendorPage "108 s" entry was paid off the same day and the BenchmarkPage entry
#: left with that page. This number may go DOWN freely — it must never go up without
#: someone deciding to let it.
MAX_UNVERIFIED_DEBT = 0


def test_the_unverifiable_debt_has_not_grown() -> None:
    """No published number this repo cannot verify, and it stays that way.

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
