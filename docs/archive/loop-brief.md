# Product brief for the loop

**Read this first.** Every agent in this repo's improvement loop — Scout, Builder,
Auditor, Retro, Redraft — reads this file before it does anything. It is the only place
that says what this product *is*, what the owner is currently trying to achieve, and what
must be left alone. Without it, agents fall back to generic engineering hygiene.

> **This file starts as a template and is worthless until it is filled in.**
> If you are an agent and you find the placeholder text below still in place, say so in
> your output (and, if you have write access, open a proposal to fill it in) rather than
> guessing.

## Keeping this current — instructions for agents

- **Read before you propose.** Ideas that contradict "Off-limits areas" or ignore
  "Current goals" should not be filed.
- **Keep it true.** If you learn something here is stale or wrong — a goal that has clearly
  been met, an "off-limits" area the owner has since asked you to change, a description
  that no longer matches the code — propose an update to this file in the same pull request
  as the work that revealed it. Say plainly what changed and why.
- **Keep it short.** Aim for under 100 lines. It is loaded into every agent's context on
  every run; length here is paid for on every single run.
- **Do not turn it into a changelog.** Mistakes and corrections go in the local-only lessons log;
  metrics go in `metrics/loop-metrics.json`. This file describes the present, not the history.
- **Never delete a section.** If a section does not apply yet, write "Not decided yet"
  under it so the gap is visible instead of silent.

### This file vs the `scout` block in `.github/loop-config.json`

Both hold the owner's intent, and they are not rivals. This brief is the **long-form
context** every agent reads here in the repo. The `scout` block (`productSummary`,
`currentGoals`, `offLimits`, `lenses`, `maxPerRun`) is the **structured knob set** the
Scout's gate step injects straight into its prompt, edited from the dashboard.

**If the two conflict, the `scout` block wins for the Scout's behavior** — it is what the
owner most recently typed, and the Scout is told it is him speaking directly. Every other
agent only ever sees this file, so this file governs for them. A conflict is a bug, not a
setting: when you spot one, propose the fix to this file in your next PR. Full detail in
`docs/archive/DASHBOARD-CONTRACT.md` § 6.

---

## What this product is

An electronics supply-chain optimizer for component procurement, built on real market
data: 791 components, 92 distributors and 8,176 price offers — a frozen 2024 snapshot
originally collected via the Nexar API and redistributed on HuggingFace under CC-BY-4.0.
It is real data, but a snapshot, not a live feed (`docs/DATA_PROVENANCE.md`). The point of
the project is that every headline number is produced by a command in the repo and written
to a committed JSON artifact anyone can open.

- **Optimisation core** — an OR-Tools CP-SAT sourcing MILP, plus a two-stage stochastic
  program that produces a cost-vs-tail-risk (CVaR-95) efficient frontier.
- **Forecasting benchmarks** and a 50-gate model CI suite that gates the claims.
- **Figure integrity** — `tests/test_docs_match_artifacts.py` regenerates each document's
  `<!-- GENERATED: -->` regions from its artifact and fails on any difference, so published
  figures cannot silently diverge from the runs that produced them.
- **Stack**: Python backend (`backend/`), a frontend (`frontend/`), analysis and helper
  scripts (`scripts/`), metrics artifacts (`metrics/`), docs (`docs/`). Deployed via
  Render (`render.yaml`), with `docker-compose.yml` for local work.

## Current goals

1. **Every published number matches its committed artifact.** Wrong today: README leakage R² vs
   `docs/leakage_progression.json`, `INTERMITTENT_DEMAND.md` §9 ("not built"; it is), the landing hero
   crediting Prophet for Chronos's result.
2. **Fix anything broken**, live-demo path first (the printed demo login 403s on the cart), then the
   rest (e.g. arbitrary price tier at `cart.py:148`; fractional quantities dropped at `optimize.py:68`).
3. **Guards on published numbers must be able to fail, and must run:** CI deselects `slow` pin tests
   (`ci.yml:163`); deploy ships the branch tip, not the gated SHA; one page test ends in `or True`.
4. **Publish the newsvendor result** (`docs/newsvendor.json` has no doc), always with its baseline:
   36.26% cheaper vs the all-zero forecast MASE ranks first, 4.01% vs `scarf_minmax`.
5. **New ideas** that let an OR, forecasting or supply-chain recruiter try the strongest true results.
6. **Resume figures** (387 solves, $4.27 per $1, Brier 0.393, rank 1.66): a proposal that moves one is
   allowed, but its title starts with `[resume-figure]` and its body has a before/after table.

## Off-limits areas

- **Hand-edited numbers:** `GENERATED` regions, `docs/*.json`, `metrics.joblib`, provenance stamps.
  Tests diff them against their generators, so fix the generator and rerun it from a clean tree.
- **Synthetic, fabricated or live data** replacing the frozen 2024 snapshot (`DATA_PROVENANCE.md`),
  reseeding, or drift in `backend/seeds/data/`. If data is missing, say so.
- **Loosening a gate to go green** (skip, `slow` mark, wider hatch). Fix the cause; stricter is fine.
- **Do-not-claim lists** (`PROJECT_OVERVIEW.md`, `CVAR_EFFICIENT_FRONTIER.md` §10); no bare percentages.
- **Loop machinery:** `claude-*.yml`, `loop-metrics.*`, `loop-config.json`, this file's path (the
  Scout and Builder gates read it). Managed from the Loop Dashboard template.
- **Hosting, money, secrets:** Render free tier stays; no AWS move, key rotation, force-push, or secrets
  in issues (public repo). Leave `CLAUDE.md`, `LEARNINGS.md`, `.claude/`, `LICENSE`, CC-BY credit alone.

## How the owner works

- A student directing (not coding) a portfolio piece aimed at OR, forecasting and supply-chain roles.
- **Proposals:** one outcome each, plain English, a title that states the consequence, judgeable in
  one read (no approve/decline history yet).
- **Evidence:** `path:line`, and re-derive each number from its source (artifact, live endpoint, SQL),
  never another doc. Show a check going red; green ticks alone have misled before.
- **Owner's call:** money, credentials, live optimizer output, resume figures. Give 2–3 options, a
  pick, and what the owner must do (ideally nothing). Done means live (`/version` matches HEAD).
