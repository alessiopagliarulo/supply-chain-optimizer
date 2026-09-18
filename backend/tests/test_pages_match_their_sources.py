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
slips in. What remains to pin is the one thing the Route Plan page documents by hand: the
customer CSV format, which must be exactly the node fields the solver accepts.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.api.routing import NodeIn
from tests._jsx import text_nodes, to_rendered_text

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
PAGES_DIR = FRONTEND_SRC / "pages"
CUSTOMER_CSV = FRONTEND_SRC / "lib" / "customerCsv.ts"


def _source(path: Path) -> str:
    assert path.is_file(), f"{path} is not in this checkout; the pins below read it"
    return path.read_text(encoding="utf-8")


# ══ Route Plan — the customer CSV it documents ═══════════════════════════════


def _documented_csv_columns() -> list[str]:
    m = re.search(r"export const CSV_COLUMNS = \[([^\]]*)\] as const;", _source(CUSTOMER_CSV))
    assert m is not None, "lib/customerCsv.ts no longer declares CSV_COLUMNS; the pin below is dead"
    return re.findall(r"'([a-z_]+)'", m.group(1))


def test_the_csv_columns_the_route_plan_page_documents_are_the_fields_the_solver_takes() -> None:
    """The page lists CSV_COLUMNS as the upload format and posts each row as a node.

    If the API's node model gains or renames a field, a CSV written to the documented
    format would be rejected (or silently lose the field), so the two must stay equal.
    """
    assert _documented_csv_columns() == list(NodeIn.model_fields), (
        f"Route Plan documents CSV columns {_documented_csv_columns()}, but POST /routing/solve "
        f"takes node fields {list(NodeIn.model_fields)}."
    )


def test_the_route_plan_page_renders_the_documented_columns() -> None:
    """The column list on the page is CSV_COLUMNS itself, not a hand-typed copy."""
    page = _source(PAGES_DIR / "RoutePlanPage.tsx")
    assert "CSV_COLUMNS.map(" in page and "CSV_COLUMN_HELP[c]" in page


def test_the_pages_state_their_limits_from_the_api() -> None:
    """Caps are rendered from GET /routing/instances, so they cannot drift from routing.py."""
    route_plan = _source(PAGES_DIR / "RoutePlanPage.tsx")
    for field in ("max_customers", "max_exact_customers", "max_time_limit_seconds"):
        assert f"limits.{field}" in route_plan, f"RoutePlanPage.tsx no longer renders limits.{field}"
    simulation = _source(PAGES_DIR / "SimulationPage.tsx")
    for field in ("max_replications", "max_tuning_customers"):
        assert f"limits.{field}" in simulation, f"SimulationPage.tsx no longer renders limits.{field}"


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
