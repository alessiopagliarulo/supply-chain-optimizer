"""Render the cost-vs-CVaR frontier chart from the committed artifact.

    cd backend && python -m seeds.render_cvar_frontier_chart

Writes ``docs/cvar_frontier.png``, the image embedded in ``README.md`` and in
``docs/CVAR_EFFICIENT_FRONTIER.md``.

WHY THIS SCRIPT EXISTS, RATHER THAN A SCREENSHOT
------------------------------------------------
The only picture of this result in the repository used to be a screenshot of the
live Frontier page. That page re-solves on demand and, on a free-tier backend,
often serves a PARTIAL sweep — the tiles in that screenshot read $2.88 per $1,
because five of seven lambdas had come back. The published figure is $4.27, off
the complete offline run. Both are honest; putting them next to each other in a
README is not, because the reader has no way to know they describe different
runs.

Drawing the chart from ``docs/cvar_frontier.json`` — the same artifact the prose
is generated from and that ``tests/test_docs_match_artifacts.py`` diffs the prose
against — means the picture and the sentence beside it cannot disagree. Every
number stamped on the image is read out of the artifact here; none is typed.

matplotlib is imported lazily and is deliberately NOT in ``requirements.txt``:
this is a one-off documentation generator, not a runtime dependency of the API.
Install it only when regenerating the chart (``pip install matplotlib``).
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = REPO_ROOT / "docs" / "cvar_frontier.json"
OUTPUT = REPO_ROOT / "docs" / "cvar_frontier.png"

# Validated with the data-viz palette validator (light surface #fcfcfb,
# categorical): lightness band, chroma floor, CVD separation (worst adjacent
# pair delta-E 24.7 protan / 32.7 tritan), normal-vision floor (33.6) and
# contrast vs. surface all PASS. Blue carries the single data series; orange is
# the accent on the recommended point, and it is never the only thing marking it
# — the knee also gets a larger marker and a direct text label, so identity is
# not colour-alone.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#84837c"
SERIES = "#2a78d6"
ACCENT = "#eb6834"
GRID = "#e3e2de"


def main() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    # matplotlib reads a $...$ PAIR as mathtext, so a label holding two literal
    # dollar signs ("$4.27 ... per $1") renders the span between them as italic
    # maths. Escaping every literal dollar is the fix; this was caught by looking
    # at the rendered PNG, which no amount of reading the code would have found.
    def usd(amount: float) -> str:
        return f"\\${amount:,.2f}"

    art = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    primary = art["primary"]["x10000"]
    frontier = primary["frontier"]
    knee = primary["knee"]
    units = primary["total_units"]
    commit = art["provenance"]["git"]["commit_short"]
    generated = art["meta"]["generated_utc"][:10]

    xs = [p["expected_cost_usd"] for p in frontier]
    ys = [p["cvar_95_usd"] for p in frontier]
    lams = [p["lambda"] for p in frontier]

    knee_x, knee_y = knee["expected_cost_usd"], knee["cvar_95_usd"]
    before = knee["vs_risk_neutral"]
    beyond = knee["beyond_the_knee"]

    fig, ax = plt.subplots(figsize=(10.0, 5.9), dpi=170)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    # Recessive grid and axes; the data is the only assertive thing on the page.
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
        ax.spines[side].set_linewidth(1.0)
    ax.tick_params(colors=INK_SECONDARY, labelsize=9, length=0)

    # The chord whose average slope IS the published $4.27 — drawn so the
    # "average over a stretch, not a rate at a point" claim is visible rather
    # than only asserted in prose.
    ax.plot(
        [xs[0], knee_x], [ys[0], knee_y],
        color=INK_MUTED, linewidth=1.4, linestyle=(0, (5, 4)), zorder=2,
    )

    ax.plot(xs, ys, color=SERIES, linewidth=2.0, zorder=3, solid_capstyle="round")
    # 2px surface ring on every marker so overlapping points stay countable —
    # lambda 0/0.05/0.1 land on the same plan, and so do 0.3/0.5 and 0.85/1.0.
    ax.scatter(
        xs, ys, s=90, color=SERIES, zorder=4,
        edgecolors=SURFACE, linewidths=2.0,
    )
    ax.scatter(
        [knee_x], [knee_y], s=210, color=ACCENT, zorder=5,
        edgecolors=SURFACE, linewidths=2.5, marker="D",
    )

    # Selective direct labels — the three points a reader acts on, not all nine.
    ax.annotate(
        f"λ = {lams[0]:g}  risk-neutral\n(the plan a cost-only optimiser picks)",
        (xs[0], ys[0]), textcoords="offset points", xytext=(14, 6),
        fontsize=9, color=INK_SECONDARY, ha="left", va="bottom",
    )
    ax.annotate(
        f"λ = {knee['lambda']:g}  the knee — recommended\n"
        f"{knee['n_suppliers']} suppliers instead of {frontier[0]['n_suppliers']}",
        (knee_x, knee_y), textcoords="offset points", xytext=(12, -34),
        fontsize=9.5, color=ACCENT, fontweight="semibold", ha="left", va="top",
    )
    ax.annotate(
        f"λ = {lams[-1]:g}  fully risk-averse",
        (xs[-1], ys[-1]), textcoords="offset points", xytext=(-10, 14),
        fontsize=9, color=INK_SECONDARY, ha="right", va="bottom",
    )

    ax.annotate(
        f"dashed chord = the published headline:\n"
        f"{usd(before['usd_of_cvar_removed_per_usd_of_expected_cost'])} of tail risk removed\n"
        f"per \\$1 of expected cost, averaged\n"
        f"over λ 0 → {knee['lambda']:g}",
        xy=((xs[0] + knee_x) / 2, (ys[0] + knee_y) / 2),
        textcoords="offset points", xytext=(26, 16),
        fontsize=9.5, color=INK, ha="left", va="bottom", linespacing=1.5,
    )
    ax.annotate(
        f"past the knee the same trade\n"
        f"returns {usd(beyond['usd_of_cvar_removed_per_usd_of_expected_cost'])}",
        xy=(knee_x, knee_y), textcoords="offset points", xytext=(80, 26),
        fontsize=9, color=INK_MUTED, ha="left", va="bottom", linespacing=1.5,
    )

    money = FuncFormatter(lambda v, _: f"\\${v/1000:,.0f}k")
    ax.xaxis.set_major_formatter(money)
    ax.yaxis.set_major_formatter(money)

    ax.set_xlabel("Expected cost  →  more expensive", fontsize=10, color=INK_SECONDARY, labelpad=9)
    ax.set_ylabel("CVaR-95  →  more tail risk", fontsize=10, color=INK_SECONDARY, labelpad=9)

    n_plans = len({(round(x, 2), round(y, 2)) for x, y in zip(xs, ys, strict=True)})
    ax.set_title(
        "Buying down tail risk costs less at first, then stops being worth it",
        fontsize=14.5, color=INK, fontweight="bold", loc="left", pad=40,
    )
    ax.text(
        0.0, 1.085,
        f"Cost vs. CVaR-95 efficient frontier · {len(frontier)} λ-solves on one "
        f"{len(primary['lines'])}-line BOM at {units:,} units",
        transform=ax.transAxes, fontsize=10, color=INK_SECONDARY, va="bottom",
    )
    ax.text(
        0.0, 1.028,
        f"CVaR-95 = mean cost of the worst 5% of scenarios · the {len(frontier)} solves land on "
        f"{n_plans} distinct plans, so markers overlap",
        transform=ax.transAxes, fontsize=9, color=INK_MUTED, va="bottom",
    )
    fig.text(
        0.008, 0.012,
        f"Generated from docs/cvar_frontier.json ({generated}, commit {commit}) by "
        f"seeds/render_cvar_frontier_chart.py — every figure read from the artifact, none typed.",
        fontsize=7.8, color=INK_MUTED,
    )

    ax.margins(x=0.20, y=0.24)
    fig.tight_layout(rect=(0, 0.030, 1, 1))
    fig.savefig(OUTPUT, facecolor=SURFACE)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
