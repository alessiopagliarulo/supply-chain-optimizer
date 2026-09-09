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

<!-- What the owner is trying to achieve over the next few weeks, most important first.
     Be concrete enough that an agent can tell whether an idea serves a goal or not.
     Delete goals when they are met — a stale goal steers the loop wrong for weeks. -->

_Not filled in yet._

## Off-limits areas

<!-- Where agents must not propose or make changes, and why. Typical entries: payment or
     billing code, auth, anything touching production data or credentials, a subsystem
     mid-rewrite, a vendor integration under contract, design/branding decisions.
     "Why" matters — an agent that understands the reason can spot the edge cases. -->

_Not filled in yet._

## How the owner works

<!-- How to pitch to this person. For example: how technical they are; how much detail
     they want in a proposal; what evidence convinces them (file:line? a screenshot? a
     number?); what they have repeatedly said no to; how quickly they triage; whether
     they prefer several small changes or one big one. -->

_Not filled in yet._
