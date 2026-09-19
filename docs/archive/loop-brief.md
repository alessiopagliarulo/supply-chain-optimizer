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

A logistics engine for electronics components: real component and distributor data, real distributor
locations on a real map, forecasting that generates the orders being routed, a routing engine for the
capacitated VRP with time windows (CVRPTW), and a digital twin. It was stripped to routing-only, then the
owner changed direction: the sourcing-era pieces at tag `archive/sourcing-v1` are being brought back
and combined with the routing engine. Every headline number comes from a repo command and a committed artifact.

**Already exists:**
- **Routing engine** (`backend/app/vrp/`): one data model (`model.py`), three solver paths - CP-SAT
  exact (`cpsat.py`), Clarke-Wright savings (`savings.py`), OR-Tools routing (`ortools_routing.py`) -
  and one shared validator (`validate.py`) every solution passes through. Solomon instances and two
  25-customer samples ship in `backend/app/vrp/data/`.
- **Simulation** (`simulate.py`): SimPy discrete-event run of a route plan with random travel and
  service times; **buffer tuning** (`buffer_tuning.py`): grid search over schedule/capacity buffers.
- **Benchmark**: `backend/scripts/benchmark_solomon.py` writes `docs/benchmark_results.json` (168
  cases x 3 solvers, gaps vs best-known); `docs/BENCHMARKING.md` explains it and a test re-validates it.
- **Web app** (`frontend/`) today: Landing plus Route Plan, Simulation, Benchmarks, no login, backed by
  `backend/app/api/routing.py`. Sourcing-era routers are still mounted in `backend/app/api/__init__.py`.
- **Deploy**: Render (`render.yaml`, `deploy-render.yml` deploys the CI-gated SHA). No AWS config here.

**Target (combined product):** five pages, still no login - Landing, **Map** (real distributor
locations on OpenStreetMap tiles, no paid map provider), **Route Plan**, **Digital Twin** (a normal day
and a disrupted day side by side, built on the simulation and the archived resilience/disruption work)
and **Benchmarks**. Routing runs on straight-line (great-circle) distances times a documented road
factor. Forecasting (lead-time and demand) quietly generates the orders being routed, and shows one
forecast-vs-actual chart.

**In flight right now (firstmate's crew - do not propose these):** the Benchmarks page fix, and
restoring the component and distributor data from `archive/sourcing-v1`.

## Current goals

1. **Restore and combine, one piece at a time with tests:** component data, distributor locations
   with real coordinates, map components, resilience/disruption work, lead-time and demand
   forecasting - each from `archive/sourcing-v1`, each wired into the routing flow. Skip the pieces
   listed as in flight above.
2. **The front door describes the combined product**, with the routing benchmark among its headline
   results; do not describe pages that are not live yet as if they were.
3. **Publish the Solomon benchmark honestly**, always against its baseline (best-known solutions), from
   `docs/benchmark_results.json`; explain its weak spots (e.g. OR-Tools is feasible on only 49 of 56
   100-customer cases; CP-SAT's mean gap grows with size).
4. **Guards must run where they gate:** `ci.yml:163` still passes `-m "not slow"`, so the gate the
   deploy waits on never runs the `slow` pins; `repo-tests.yml` runs them (PR 20) but does not gate deploy.
   This, and any other `.github/workflows/` change, may be proposed but not built by the loop (GitHub
   refuses the App): the proposal says it needs a workflow change and is left for the owner's crew.
5. **Fix anything broken on the live flow** first; then **new ideas** that let an OR, logistics or
   supply-chain recruiter try the strongest true result on real data.
6. **Resume figures** still published (Brier 0.393, rank 1.66): a proposal that moves one is allowed,
   but its title starts with `[resume-figure]` and its body has a before/after table.

## Off-limits areas

- **Hand-edited numbers:** `GENERATED` regions, `docs/*.json` (including `benchmark_results.json`),
  `metrics.joblib`, provenance stamps. Fix the generator and rerun it from a clean tree.
- **Synthetic, fabricated or live data** replacing the frozen 2024 snapshot, drift in
  `backend/seeds/data/`, or benchmark results not written by the script. If data is missing, say so.
- **Loosening a gate to go green** (skip, deselect, xfail, `slow` mark, wider tolerance). Fix the cause.
- **Do-not-claim lists** (`PROJECT_OVERVIEW.md` "What NOT to claim"); no bare percentages.
- **A blanket revert of the strip-down.** Restore archived pieces one at a time, each with tests.
- **Loop machinery:** `claude-*.yml`, `loop-metrics.*`, `loop-config.json`, this file's path (the
  Scout and Builder gates read it). Managed from the Loop Dashboard template.
- **Hosting, money, secrets:** no hosting or plan change, AWS move, key rotation, force-push, or
  secrets in issues (public repo). Leave `CLAUDE.md`, `LEARNINGS.md`, `.claude/`, `LICENSE`, CC-BY credit alone.

## How the owner works

- A student directing (not coding) a portfolio piece aimed at OR, logistics and supply-chain roles.
- **Loop autonomy is off** (`autonomousBuildEnabled: false`): the loop drafts ideas as proposals only.
  The Builder builds only proposals the owner has labelled approved, and loop PRs are merged only by the owner.
- **Proposals:** one outcome each, plain English, a title that states the consequence, judgeable in
  one read (no approve/decline history yet).
- **Evidence:** `path:line`, and re-derive each number from its source (artifact, live endpoint, SQL),
  never another doc. Show a check going red; green ticks alone have misled before.
- **Owner's call:** money, credentials, live solver output, resume figures. Give 2–3 options, a
  pick, and what the owner must do (ideally nothing). Done means live (`/version` matches HEAD).
