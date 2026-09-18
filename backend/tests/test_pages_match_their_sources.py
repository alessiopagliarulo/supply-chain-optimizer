"""What a page tells the reader must equal the thing that produced it.

WHY THIS FILE EXISTS (2026-09-05)
---------------------------------
A `data-testid` pin only covers the anchors a page happens to carry, so the pins here are
anchored on what the page SAYS: `_jsx.text_nodes` gives the plain text a browser would
show, and each test compares a claim in it to a committed artifact, a constant of the code
the API serves, or the page's own source. Never to a prose document.

THE THREE PAGES (issue #16)
---------------------------
The sourcing-era pages this file used to pin (Newsvendor, Dashboard, Resilience) were
removed with them, and their pins went too: a pin on a page that no longer exists guards
nothing.

Route Plan, Simulation and Benchmarks type no figures of their own. Every limit they
state (customer caps, time limit, replications) is rendered from the `limits` block of
GET /routing/instances, and every result from the API response or the benchmark
artifact; `test_pages_do_not_publish_unverified_numbers.py` enforces that no typed digit
slips in. That the pages really render those limits, and that a customer CSV in the
documented format round-trips through the solver, is checked in a real browser by
`frontend/scripts/ui-gate.cjs`, not by reading page source here.
"""
from __future__ import annotations

from pathlib import Path

from tests._jsx import text_nodes, to_rendered_text

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
PAGES_DIR = REPO_ROOT / "frontend" / "src" / "pages"


# ══ The helper every page scan depends on ════════════════════════════════════


def test_the_rendered_text_helper_still_sees_a_real_page() -> None:
    """If `_jsx` ever silently stopped parsing, the page guards would scan nothing and pass.
    This is the floor check: every page must yield text."""
    for page in sorted(PAGES_DIR.glob("*.tsx")):
        text = to_rendered_text(" ".join(n.text for n in text_nodes(page.read_text())))
        assert len(text) > 40, (
            f"{page.name} yielded {len(text)} characters of rendered text. The JSX "
            f"scanner in tests/_jsx.py is not reading this file."
        )
