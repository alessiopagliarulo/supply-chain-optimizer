"""`Component.risk_score` is a catalogue attribute, not a probability, and the
code must not claim otherwise.

Why this file exists
--------------------
`risk_score` was once rendered with a `%` sign on the catalogue pages. A `%` is
a unit claim, and this quantity cannot support it:

* Nothing in this repo computes it. `seeds/seed_db.py:248` copies a HuggingFace
  dataset column through verbatim (`row.get("risk_score") or 0.0`).
* Upstream it behaves as an additive hand-weighted flag sum,
  ``0.60*chinese_origin + 0.25*critical_category + 0.10*limited_suppliers``.
* Its ENTIRE support across the 791 seeded parts is six values -- 0.00, 0.10,
  0.20, 0.25, 0.60, 0.70.
* 387 of those parts (48.9%) sit at 0.20 with ``risk_factors = NULL``: a nonzero
  number with no flag behind it. The score is not even a function of the flags
  it claims to sum.

There is no base rate, no exposure window and no unit, so it is not a
probability. This is the repo's own Check-8 pathology, already fixed three times
(`graph/builder.py`, `graph/simulation.py`, `optimization/recommendations.py`).

The pages that published the score (/dashboard and /components) and the shared
banding module they imported (`frontend/src/lib/risk.ts`) were removed when the
web app shrank to Route Plan, Simulation and Benchmarks (issue #16), and the
tests on their source went with them. What stays is the backend half: the model's
provenance claim and the seeded support that every statement above rests on.

If this fails
-------------
Do not relax the assertion. Fix the claim, or the data it describes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
#: The exact support of `risk_score` in the seeded catalogue, and the flag set
#: that explains each value. `None` means `risk_factors` is NULL.
DOCUMENTED_SUPPORT = {
    0.00: None,
    0.10: ["limited_suppliers"],
    0.20: None,  # the placeholder cohort: a nonzero score with no flag
    0.25: ["critical_category"],
    0.60: ["chinese_origin"],
    0.70: ["chinese_origin", "limited_suppliers"],
}


# ── The provenance claim in the ORM model ────────────────────────────────


def test_the_component_model_does_not_claim_nexar_provenance_for_risk_score() -> None:
    """`models/component.py:17` said "0-1 from Nexar analysis". Wrong twice.

    It is not from Nexar, and the Nexar path hardcodes 0.0 because the API
    exposes no such field.
    """
    source = (BACKEND_ROOT / "app" / "models" / "component.py").read_text(encoding="utf-8")
    assert "0-1 from Nexar analysis" not in source, (
        "component.py still credits risk_score to Nexar analysis. It is a "
        "verbatim HuggingFace dataset column (seeds/seed_db.py:248)."
    )
    assert "HuggingFace" in source, (
        "component.py must name the real provenance of risk_score."
    )


def test_the_nexar_client_really_does_hardcode_a_zero_risk_score() -> None:
    """The code fact the corrected comment cites. If Nexar ever starts serving a
    real score, the comment above `risk_score` has to be revisited, not this
    assertion relaxed.
    """
    source = (
        BACKEND_ROOT / "app" / "core" / "clients" / "nexar_client.py"
    ).read_text(encoding="utf-8")
    assert '"risk_score": 0.0' in source


# ── The support the comments assert ──────────────────────────────────────


def test_the_seeded_catalogue_matches_the_documented_risk_score_support() -> None:
    """Pins the six values and the placeholder cohort that every comment cites.

    Skips on a checkout without the seeded database; the numbers quoted in
    `lib/risk.ts` and `models/component.py` are only meaningful against it.
    """
    db_path = BACKEND_ROOT / "supply_chain.db"
    if not db_path.exists():
        pytest.skip("supply_chain.db not seeded in this checkout")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT risk_score, risk_factors FROM components").fetchall()
    except sqlite3.OperationalError:  # pragma: no cover - unseeded schema
        pytest.skip("components table not present in supply_chain.db")
    finally:
        conn.close()

    if not rows:
        pytest.skip("components table is empty")

    observed = {round(float(score), 2) for score, _ in rows}
    assert observed == set(DOCUMENTED_SUPPORT), (
        f"risk_score support changed: {sorted(observed)} vs the documented "
        f"{sorted(DOCUMENTED_SUPPORT)}. Every comment and UI caption that "
        "quotes this support has to be re-derived, starting with lib/risk.ts."
    )

    for score, raw in rows:
        expected = DOCUMENTED_SUPPORT[round(float(score), 2)]
        actual = json.loads(raw) if raw else None
        assert actual == expected, (
            f"risk_score {score} carries flags {actual!r}, documented as {expected!r}."
        )

    placeholder = [s for s, raw in rows if not (json.loads(raw) if raw else None) and s > 0]
    assert placeholder, (
        "The placeholder cohort is gone. If every nonzero score now has a flag "
        "behind it, the UI captions saying otherwise are stale."
    )
    share = len(placeholder) / len(rows)
    assert share > 0.4, (
        f"Placeholder cohort is {share:.1%} of the catalogue; the comments and "
        "captions quote ~48.9% and need updating."
    )
