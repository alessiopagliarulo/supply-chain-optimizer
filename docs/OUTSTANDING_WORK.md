# Outstanding work

Live backlog. Every item was found by a verification pass against the deployed
site and the artifacts, not from a wishlist. Ordered by **published-claim risk**:
a false published claim outranks a correctness bug, which outranks polish.

Status: `TODO` · `WIP` · `DONE` · `DEFERRED (owner)`

Live: 247cd34 · updated 2026-09-03 (verified 2026-09-02 by calling the deployed services;
`/optimize/vrp` measured at 0.59-0.67 s, down from ~10 s.)

## What is actually still open

**Items 1–40 below are all `DONE`.** Of the four findings from the 2026-08-30 ML pipeline
verification pass, **all four are now `DONE`** (2026-08-28/29), as is the lower-priority
`recommended_k` item. What follows is the record of each:

1. ~~`/ml/stress` publishes a probability computed from a **2026-07-01** frame with no as-of
   field~~ — **DONE** (2026-08-29). The response now carries `observation_date`,
   `observation_frequency`, `observation_age_days`, `observation_age_months`,
   `vintage_is_stale`, `max_observation_age_days` and a composed `vintage_label`, all derived
   from the same `tail(1)` row that is scored — never a fabricated date (the unavailable branch
   reports "no data vintage" rather than a guess). `/model-card` prints `Jul 2026` beside the
   82.8% at equal type size; `/optimize` tags the claim and renders the vintage and an amber
   note *larger* than the claim they qualify. 9 tests in `test_stress_vintage.py`, each proven
   red. **Optimizer behaviour is deliberately unchanged** — a stale frame still prices the full
   surcharge (owner decision, 2026-08-29); `sourcing.py:686-697` records where to gate it if
   that is ever revisited. `STRESS_FRAME_MAX_AGE_DAYS = 120` turns the suite red on
   **2026-10-29** if nobody retrains; the fix is to retrain, not to raise the constant.
2. ~~`p_shortfall` / `p_total_shortfall` / `cvar_95_saturated` are computed in
   `graph/simulation.py` and served nowhere~~ — **DONE** (2026-08-28). Migration `0009` adds
   `mc_p_shortfall`, `mc_p_total_shortfall`, `mc_cvar_95_ceiling` and `mc_cvar_95_saturated` to
   `optimization_runs`; `seeds/run_benchmark.py` fills them; `/benchmark/summary` serves
   `cvar95_ceiling`, `*_cvar95_saturated`, `*_cvar95_ceiling_tied_boms`,
   `*_p_total_shortfall_reduction` (with its own paired bootstrap interval) and a composed
   `saturation_note`; the `/benchmark` CVaR tiles print the flag, name the ceiling-tied BOMs and
   show the measure that still resolves. The real re-run (run 7) changed **0 of 72 cells** against
   run 6. On the published 18: **10 cells are ceiling-tied and `p_total_shortfall` breaks 8 of
   them** — and under broad stress it removes **0.197** (95% CI 0.118 to 0.259, significant) where
   both published stress deltas cover zero.
3. ~~`README.md:196-214` publishes the retired 810-row / 27-manufacturer / `random_forest`
   vintage~~ — **DONE**. The grouped-split study had already been re-run and committed
   (`docs/leakage_progression.json`, generated 2026-08-26) but the README table was never synced
   to it; README now quotes the current figures (1,879 rows / 472 family grouping keys / 28
   manufacturers,
   `+0.804 → +0.084 → −0.784`, plus the served `metrics.joblib` audit `+0.8084 → +0.1169 →
   −0.3895`) with the retired 810-row/27-manufacturer numbers labelled and dated as superseded.
   Same stale panel size (817 rows / 2 snapshots / 56 features) also found and corrected in
   `PROJECT_OVERVIEW.md` and `RESEARCH_TECHNIQUES.md`.
   `python_version` provenance stamping (3.13 local vs 3.11 CI) remains unresolved — see "Owner
   decisions" below.

   **Correction, 2026-08-30 — this entry's own claim was false until today.** It said README
   quoted "472 family keys", and the string `472` appeared **zero** times in `README.md`. Both
   numbers are real and they count different things: `docs/leakage_progression.json`
   (`counts.n_family_group_keys` = **472**, `identity_column_in_sample_r2.base_product.n_levels`
   = **361**) and the served `metrics.joblib['lead_time_leakage_audit']['n_families']` = **472**.
   361 is the raw `base_product` level count, used only for the in-sample R²=0.848 identity
   check; 472 is the `_group_key` fold-group count (`base_product`, falling back to MPN then
   row — `app/ml/lead_time_model.py:1644`) that the GroupKFold split and every published family
   R² are actually grouped on. README quoted 361 correctly for the identity check but labelled
   its GroupKFold row `(base_product)`, implying the split had 361 groups when it has 472.
   **Fixed in README, not in this backlog note:** the table row now reads
   `GroupKFold by part family (**472 family grouping keys**)` and the prose beneath names both
   numbers, what each counts, and the artifact field each comes from. The claim above is now
   true. (Also noticed, not fixed — not my file: `seeds/run_leakage_progression.py:54,612`
   carry stale `467` comments where the computed value is 472.)
4. ~~**UNVERIFIED** — FRED write-on-read in `fred_client.py:307,355`~~ — **VERIFIED LIVE, then
   FIXED** (2026-08-29). Both writes were unconditional and both endpoints are *keyless*
   (`fredgraph.csv` / NY Fed `.xlsx`), so no missing API key made it latent. Production serving
   never reaches the path (`serving.py::resolve_regime_signal` loads artifacts), but the two
   `@pytest.mark.integration` tests do — meaning **the documented `pytest tests/ -q` gate could
   rewrite `backend/seeds/data/`**. Commit `035ae78` (2026-08-16, a lead-time bug-fix) had
   already swept 685 lines of `gscpi_monthly.csv` along this way. Fix: `refresh_cache=False`
   default on `fetch_gscpi()` / `fetch_regime_feature_frame()`; only
   `seeds/train_ml_models.py:46` passes `True` — deliberately at the *call site*, because the
   integration test calls `retrain_regime_model()` directly and a flag inside the function body
   would have left the exposure intact. ALFRED `vintage_date` is now threaded through to all
   four `REGIME_FEATURE_SERIES` (default `None` = latest = unchanged). GSCPI gets no pin — it is
   a NY Fed proprietary single-`.xlsx`, revised in place, with no archival endpoint; documented
   at `fred_client.py:293-300`. 10 mutations, 10 reds. Proven: the two integration tests now run
   live against FRED and the seed CSVs stay byte-identical.

   The recommendation to *exclude* `integration` from CI was **rejected** — it removes coverage
   to solve a problem the guard already fixes. The marker is now registered in `pytest.ini`
   (filterable, no longer warning-noise) and still runs everywhere.

Plus, lower priority: ~~`/benchmark` anchors `k === 2` as a bare numeral~~ — **DONE**
(2026-08-28). The API serves `recommended_k` + `recommended_k_basis` from `_recommended_k()`,
the one place the rule lives, and `_frontier_finding()` composes its sentence from the same call;
the page consumes the field in all three places and labels the row "recommended" in words.
Published sentence and verdict are byte-identical. The rule is **the cheapest priced step**, not
"the largest significant k" — the latter returns k=3 on this frontier and would have flipped the
published verdict.


## Found 2026-08-29 — three gates that could not go red

None of these was on any backlog. Each is the same class of defect: a check that passed
because it was incapable of failing. The standing rule: *a check that cannot fail is worse than
no check.*

**41. The standing TypeScript gate typechecked nothing.** `DONE`. `frontend/tsconfig.json` is
a solution file (`"files": []` + `references`), so `npx tsc --noEmit` — the gate written in
the repo's standing bar — resolves no source files and exits 0 on any error. Proven by planting
`const x: number = "definitely not a number"` in `services/api.ts`: `--noEmit` passed,
`tsc -b --force` reported it. It had already waved through two real type errors in this
session's own work. Gate corrected to `npx tsc -b --force` (what `npm run build` already
runs, so CI was never affected) with a "Never do this" entry recording the evidence.

**42. `docs/volume_sweep.json` was generated by a pre-fix optimizer.** `DONE`. Commit
`6a33ad0` changed `sourcing.py` (milli-cent quantisation, risk surcharges, an
`is_chinese_origin` double-count) and hand-edited only the artifact's `meta` prose without
re-running the generator. `test_volume_curve_pooled_table_matches_sweep_json` compares the
**doc to the artifact** — both were stale together, so it stayed green while both disagreed
with the code. Verbatim the failure mode the repo's standing bar opens with. Regenerated via the real
`python -m seeds.run_volume_sweep` (1.0 s): the published **2.6-8.0%** range *reproduces*
(2.61%-7.97%), 12 of 13 rows bit-identical; one genuine cell moved
(`pcb_power_supply` @10,000x, $181,919.39 → $181,908.01, 5 → 6 suppliers, pooled 7.96% →
7.97%). **A scratch reimplementation had claimed the whole curve was near-zero/negative. It
was wrong.** The standing rule held: only the real generator settles a number.

**43. Only two artifact-vs-code pins existed in the whole suite.** `DONE`. Everything else
that looked like one was doc-to-artifact, artifact-to-artifact, schema, or API-to-artifact —
none of which goes red when *code* moves and the artifact is not regenerated.
`backend/tests/test_artifacts_pinned_to_code.py` adds 5 pins in 5.0 s, each re-solving through
the generator's own function (never a reimplementation): all 80 points of `volume_sweep.json`,
the full k-sweep of `diversification_frontier.json`, the `primary` block of `newsvendor.json`,
the frontend fallback table, and a shared constant. Anti-vacuity guards included: a DB
row-count assertion (791/92/8,176 — catches the CWD-relative-SQLite trap), a floor on points
actually re-solved, a wall-clock ceiling, and a distinct message when CP-SAT returns
`FEASIBLE` so "solver truncated" is never misread as "artifact stale". Proven red by moving
`LTL_BASE_FEE_USD` 75.0 → 76.0 (1,003 differing values) and `EXPEDITE_PREMIUM` 0.15 → 0.16
(104 differing values).

Related, fixed with 42: `frontend/src/lib/volumeDecayCurveData.ts` holds a hardcoded copy of
the volume curve, rendered on `/benchmark` whenever the API is unreachable, pinned by nothing.
It had drifted (10,000x row still 7.96), and a separate pre-existing drift in its
`greedy_fixed_share_of_cost_pct` column disagreed with the code's own arithmetic on 9 rows.
Both corrected and now pinned. It is the only such hardcoded table in `frontend/src/`.

~~**Left unpinned, deliberately, with sizing:** `cvar_frontier.json` (~1,316 s),
`leakage_progression.json` (~215 s nested CV), `chronos_benchmark.json` (torch + HF weights +
network), `forecast_backtest.json` (Prophet per rolling origin), `backend_verification.json`
(42 live HTTPS calls). **Open decision:** whether to add them.~~ — **RESOLVED 2026-08-30, see
item 44.** Four of the five are now pinned behind `-m slow`. **The sizing above was the cost of
REGENERATING each artifact, not of re-solving a canonical slice of it** — and that mistake is
why five artifacts went a day and a half unguarded. Measured truth: the whole `-m slow` block
costs **~50 s**.

**44. `docs/cvar_frontier.json` was stale, and four heavy artifacts are now pinned.** `DONE`
(2026-08-30). `backend/tests/test_artifacts_pinned_to_code.py` gains four `@pytest.mark.slow`
pins, each re-solving through the generator's own function:

| Artifact | What is re-solved | Cost | Proven red by |
|---|---|---|---|
| `cvar_frontier.json` | the whole PRIMARY arm via `_run_primary` — 3 volumes × 9 λ, both baselines, the VSS | ~45 s | `LTL_BASE_FEE_USD` 75.0 → 76.0 |
| `leakage_progression.json` | panel + row accounting + feature columns + the identity-R² table in full, then **fold 0 of each of the 3 regimes** via `score_regime` (24 fits, not 1,200) | ~5 s | `Ridge(alpha=1.0 → 1.1)` |
| `forecast_backtest.json` | all three arms, whole — **since 2026-08-30 split into a deterministic `seasonal_naive` pin (CI) and a `slow` Prophet pin, see item 54** | ~0.7 s | `SEASONAL_PERIOD` 12 → 11 |
| `chronos_benchmark.json` | classical arms unconditionally (**likewise split by determinism, item 54**); the zero-shot arm + cold-start table from the **locally cached** weights under `HF_HUB_OFFLINE` | ~4 s | `quantile_levels` 0.5 → 0.55 |

**46. The forecast pin is promoted out of `slow`, so CI gets a pin on a demand-series artifact.**
**DONE** (2026-08-30) — **but only the deterministic half survived CI; superseded in detail by
item 54, read that first.** CI's backend job runs `pytest tests/ -q --tb=short -m "not slow"`
(`.github/workflows/ci.yml:63`) and installs `requirements.txt` only (line 26) — so the five
`slow` pins above were invisible to CI.

**A CORRECTION TO THIS ENTRY'S ORIGINAL FRAMING (2026-08-30).** It said CI had "no
artifact-vs-code coverage at all". **That was wrong.** Sections 1-4 of
`test_artifacts_pinned_to_code.py` (volume sweep, the frontend fallback table, the production
floor, the diversification frontier, newsvendor) are *unmarked* and were already running on CI:
`backend/supply_chain.db` is committed — `.gitignore:40` un-ignores it explicitly — so the
optimizer pins do not skip there. CI run `33318131193` reported **1,114 passed and exactly ONE
skip** across the whole suite, which it could not have done had five pins been skipping. The
real gap was narrower and should have been stated that way: **neither DEMAND-SERIES artifact
(`forecast_backtest.json`, `chronos_benchmark.json`) had any CI pin**, both being `slow` in
full. That is what items 46 and 54 actually closed. The
`forecast_backtest.json` pin needs no database, no network (the series loads offline from the
committed ALFRED vintage pin) and no `requirements-ml.txt` dependency, so it was unmarked.

**What this entry got wrong:** it promoted ALL THREE arms, having reasoned only about *cost* and
*dependencies* and never about *reproducibility*. The Prophet arms are not bit-reproducible off
the machine that writes the artifact, and CI went red on them. Only the `seasonal_naive` arm is
in CI now; the Prophet arms are back behind `slow`. The right question for a pin is not "is it
cheap and dependency-free here?" but **"does it produce the same bytes on the machine that will
run it?"**

The red proof below still stands and is the one CI now relies on: **proven red**, per the
standing rule, by `SEASONAL_PERIOD` 12 → 11 in `seeds/run_forecast_backtest.py:77`, which fails
the deterministic pin with *"69 differing values"* across the `seasonal_naive.*` fields (and the
chronos deterministic pin with 61); restored and verified byte-identical
(`git status --porcelain` on that path empty).

The other four stay `slow`, and the reasons were re-checked rather than assumed:
`cvar_frontier.json` opens a seeded `SessionLocal` (`_assert_row_counts`, `build_graph_state`,
`_load_offers_for_bom`) and takes **42.7–43.4 s**; both `chronos_benchmark.json` pins need
`torch==2.12.1` / `chronos-forecasting==2.3.0` from `requirements-ml.txt`, which CI never
installs; `leakage_progression.json` takes 4.6–4.7 s (no DB — reads `PANEL_PATH` directly),
which is a judgement call, not a hard constraint.

**The chronos classical-arms promotion proposed here was taken, and was half wrong.** This entry
argued the pin needs no torch and no network and so was promotable on the same argument as the
forecast pin. True as far as it went — and still an incomplete argument, because dependencies are
not the only thing that has to hold on CI. Its Prophet half is not reproducible there and turned
CI red with 61 differing values. The arm is now split: `seasonal_naive` runs in CI, `prophet`
is back behind `slow`. See item 54.

**What the cvar pin caught on the day it was written.** `6a33ad0` changed `sourcing.py`;
`volume_sweep.json` was regenerated for it on 2026-08-29 and **`cvar_frontier.json` was not**,
though it carries the same deterministic MILP as a baseline. It published
`first_stage_cost_usd: 181919.39` / 5 suppliers where the optimizer returns `181908.01` / 6 —
*the identical cell that moved in the volume sweep* — and `CVAR_EFFICIENT_FRONTIER.md:534-535`
showed a reader the derived **$183,171 / $219,128 / 5**. `test_cvar_doc_matches_artifact.py` was
green throughout: it compares the doc to the artifact, and both were stale together. **Third
recurrence of the failure mode the repo's standing bar opens with.** It also invented a difference that does
not exist: the doc showed the shipped MILP at $183,171 / 5 suppliers *beside* the mean-value EEV
baseline at $182,932 / 6, as though the two were different plans. They are the same plan —
`solve_sourcing` with disruptions assumed away IS the mean-value solve — and after the fix the
two rows agree, which is the honest reading of a VSS of 0.37%.

Fixed by a real `python -m seeds.run_cvar_frontier` (1,331 s, no `--quick`): **10 fields moved,
all of them inside the two `shipped_milp_graph_aware=*` baselines at ×10,000** — 6 dollar
figures, 2 supplier counts, 2 supplier lists. No other published cost or CVaR figure in the
artifact changed. (The solve-quality churn that came with the re-run is item 45, not drift.)

**`backend_verification.json` is honestly UNPINNABLE, and the test file says so.** No generator
for it exists in this repo — it is a hand-run snapshot from the 2026-08-19 production repair —
so a pin would have to be the reimplementation the loop's learnings log forbids. Its content is 42 live
HTTPS responses with a per-check `seconds` field that cannot reproduce; a test re-issuing them
would go red on a Render cold start and green on a broken build.

**45. `cvar_frontier.json` cannot be regenerated reproducibly, and its doc does not say so.**
**DONE** (2026-08-30, relabelled) — **and SUPERSEDED 2026-09-01: it now CAN be regenerated
reproducibly.** See "What actually fixed it" at the end of this item. Everything from here to
that note describes the WALL-CLOCK vintage and is kept as the record of the defect; do not read
it as a description of the committed artifact.

As it stood on 2026-08-30: the breadth and sensitivity arms solved under a 15 s wall-clock
CP-SAT budget, and that vintage's `solve_quality.by_arm` recorded **46 of the breadth arm's 150
solves hitting that limit**, 38 of them left unconverged, worst gap **92.69%**. (`primary`, at a
60 s budget, was 27 solves / 27 converged / 0 hits / 0.00% worst gap — which is why it is the arm
that is pinned.) Two honest back-to-back runs of the same generator on the same machine therefore
disagreed. **Measured over two full 22-minute regenerations on 2026-08-30:**

* **Under CPU load**, the headline counters moved (349 → 347 converged, 48 → 52 non-`OPTIMAL`,
  MIP-gap p90 3.79% → 6.07%) and **`smart_meter` dropped out of the breadth table entirely** —
  a published row became `excluded / —` purely because one λ ran out of clock.
* **On a quiet machine**, the headline counters landed back on 349/38/48 exactly — but their
  *composition* did not. 16 `worst_mip_gap_pct` values moved (`audio_dsp_board ×1`
  84.69% → 88.82%, `industrial_motor_driver ×1` 9.28% → 10.06%), `automotive_ecu ×10` closed
  one λ MORE (3 → 4 converged) and `audio_dsp_board ×1` one FEWER (2 → 1), and the
  `not_converged` list reordered.

No breadth cost or CVaR figure moved in either run — the *plans* are stable. What is unstable is
every number the document publishes ABOUT the solve, and `CVAR_EFFICIENT_FRONTIER.md` presented
those as findings (its solve-time spotlight table reshuffles with them too).

**Owner decision (2026-08-30): relabel, do not regenerate.** Raising the time limit costs a
22-minute regeneration and would leave the figures machine-dependent *less often* rather than
reproducible; relabelling is truthful immediately and cannot rot. The artifact was **not**
regenerated for this item.

That decision was correct about *raising* the limit and wrong about the ceiling — the right move
was not a bigger clock but a different KIND of budget. See "What actually fixed it" below.

**Resolution — what was labelled, and what was deliberately left alone.**
`docs/CVAR_EFFICIENT_FRONTIER.md` now carries the machine-and-load-dependence notice in the
same `###`-heading weight as the claims it qualifies (never smaller — the mistake made once
already), in six places, all *outside* the `GENERATED:` markers so a re-render cannot wipe them:

* a document-header callout above §"What this replaces", stating the boundary in one line;
* §0, retitled *"Solve quality — a run log of one machine's time budget, not a property of the
  problem"*, carrying the full notice: the 15 s / 60 s budgets, `num_search_workers = 1`, the
  hardware from `Provenance`, and the two-regeneration evidence table (349 → 347, p90 3.787% →
  6.07%, `smart_meter` dropped; then 349/38/48 back but 16 gap values moved);
* a `###` run-log banner immediately above the generated `solve_quality` block;
* §4 (`Status` / `Gap` / `Solve` columns and the per-sweep solve-quality line), §6 (`all λ
  converged`, the "36 of 36" line), §7 (`Wall`, `all λ converged`), §8 (`Worst gap`, `all λ
  converged`, the `excluded` markings, **and which rows appear at all**), §9 (`λ-sweep wall
  time`, `Worst gap`, `λ not converged` — the whole table);
* the three §9 "measured" bullets, the 60-draw/200-draw sizing measurement, and the §1
  RU-reformulation timing, each marked as this machine's seconds with the *ordering* named as
  the part that transfers.

**Deliberately NOT caveated, because over-caveating real results is its own dishonesty:** every
cost, plan, supplier set, CVaR value, tail decomposition, knee, VSS, SAA bound and frontier
point. None of them moved in either regeneration, and each caveat above says so explicitly so a
reader cannot spread the doubt onto the economics.

**Also fixed while here — the same figures were stale on the live site.**
`frontend/src/pages/FrontierPage.tsx` hard-coded *"330 converged / 57 did not"* and a primary-arm
*"worst MIP gap 0.082%"* for an artifact that said **349 / 38** and **0.000%** — a superseded
vintage published as current. Corrected against `docs/cvar_frontier.json`, and the page now
carries the solve-quality caveat at the same type size as the counts, with the
"no cost, plan or CVaR value moved" boundary stated on screen
(`data-testid="frontier-solve-quality-caveat"`). `test_cvar_doc_matches_artifact.py` 25 passed.

*(Two corrections since. The counts are now **351 / 36** against the 2026-09-01 deterministic-budget
artifact, and the caveat says the counters REPRODUCE rather than that they do not. And
`npx tsc --noEmit` was never a TypeScript gate here — the root `tsconfig.json` is a solution file,
so it typechecks nothing and exits 0 on any error; the gate is `npx tsc -b --force`.)*

**Out of scope but found:** `docs/BENCHMARK_VOLUME_CURVE.md:385` publishes the same class of
figure for a different artifact — *"of 326 MILP solve attempts, 296 were feasible and all 296
returned `OPTIMAL` — none hit the 5s time limit"*. That is a 5-second-budget measurement on one
machine, presented as a property. Not touched here; it belongs to `volume_sweep.json` — and it is
now the LAST place in the repo where a solve-quality figure is genuinely load-dependent.

**What actually fixed it — 2026-09-01, `max_deterministic_time`.**
A wall clock fixes the search PATH at one worker but not where the search STOPS. CP-SAT's
`max_deterministic_time` is a WORK budget and does fix the stopping point. The sweep now runs at
**15 deterministic units per solve in `breadth`, 80 in `primary`**, `num_search_workers = 1`,
`relative_gap_limit = 0.0`, with the wall clock kept only as a **runaway guard** twenty times
clear of the budget (300 s / 1,600 s).

*Proven before the regeneration, not asserted after it.* A 15-solve verification sweep
(3 instances × 5 λ, run as full `compute_frontier` sweeps so the warm-start chain was exercised),
sha256 over every published field, five separate interpreter processes:

| Run | Budget | Load avg (1-min) | `OVERALL_SHA256` |
|---|---|---:|---|
| W1 | wall clock 15 s | 2.45 | `8f6eeab5f6e22684…` |
| W2 | wall clock 15 s | **35.45** | `421cd46a86848a6d…` — differs from W1 |
| D1 | deterministic 15 | 2.45 | `10d34ccfae6868c0…` |
| D2 | deterministic 15 | **43.47** | `10d34ccfae6868c0…` — identical |
| D3 | deterministic 15 | 2.64 | `10d34ccfae6868c0…` — identical |

Per-instance digests matched across D1/D2/D3 individually too: `8a0f8211…`, `28023494…`,
`74b538eb…`. **These are digests of that 15-solve sweep, NOT of the 387-solve
`cvar_frontier.json` — they cannot be recomputed from the committed artifact.** The wall-clock
control's damage under load: `smart_meter ×10` went from gaps `[5.156, 0.0, 8.692, 12.390,
18.511]` with 1 converged λ to `[5.561, 8.179, 13.389, 20.854, 19.000]` with **0** — losing its
row in the §8 table; `rf_transceiver_module ×1` worst gap 92.690 → 94.352; `pcb_power_supply ×100`
identical either way. Root cause, measured directly: at the same 15 s clock `smart_meter ×10`
received **6.7–13.5** units of work idle but only **1.8–4.7** saturated. Same budget, a third of
the search.

The artifact was then regenerated (1,600.1 s): **387 solves, 351 converged, 36 not**, worst gap
**94.955%**; breadth **150 / 114**, primary **27 / 27**, sensitivity **180 / 180**, saa **30 / 30**.
`solve_quality.deterministic_budget_in_force: true` and **`n_wall_clock_bound: 0`** — the
falsifiable check; a nonzero value would mean the guard, not the work budget, decided where those
solves stopped and the counters do not reproduce.

**What did NOT become reproducible, and is still labelled as a one-machine run log:** every
`solve_seconds`, every `sweep_wall_seconds` and `meta.wall_seconds`. A work budget fixes where the
search stops, never how long the hardware took to get there.

**A deterministic budget makes truncation reproducible, not absent.** `drone_flight_controller ×1`
(48.32–62.25% at all five λ) and `automotive_ecu ×1` (12.43–90.79%) still have no converged λ and
are still **excluded**; `rf_transceiver_module ×1` still carries the worst solve in the run
(94.955%) with only one of five λ proved. That exclusion is a statement about the compute budget,
not about those BOMs — the same `rf_transceiver_module` closes all five λ to 0.000% at ×100 and
×1,000, and a 20× budget sweep on the hardest instance bought three points of gap (92.69% → 89.12%
against the previous vintage).

**No plan, supplier set, cost or CVaR value moved.** The economics were always reproducible; what
changed is that the telemetry describing how hard they were to prove now is too.

**⚠️ RESIDUAL — two `excluded_reason` strings are WRONG in the currently-committed
`docs/cvar_frontier.json`, and only a regeneration clears them.**
The generator had two places that built the "why this instance was excluded" sentence. `_classify`
names the budget that actually bound; the second path, in `_run_breadth`, did not — it interpolated
`TIME_LIMIT_BREADTH_S`, the **wall-clock runaway guard**. So the committed artifact tells a reader
that `drone_flight_controller ×1` and `automotive_ecu ×1` were excluded because *"none of the 5
lambda points converged within the **300s** per-solve limit"*. **That is false as a description of
what stopped those solves.** They stopped at the 15-unit deterministic work budget — every one of
them reports `deterministic_seconds ≈ 15.0` against `deterministic_time_limit: 15.0`, and
`n_wall_clock_bound` is **0**, meaning the guard stopped nothing. (The longest single solve
recorded anywhere in the artifact took **70.9 s** of wall clock, well under the 300 s guard; the
worst-gap solve took 8.6 s.) A reader hitting the raw JSON would conclude those exclusions are
clock- and load-dependent when they are not.

*Fixed at source on 2026-09-01* — a single `_budget_prose()` helper both paths now use, and
`_render_breadth` no longer reads a module global at all (it was rendering "300s budget" into the
document from module state rather than from the artifact, which is what left
`test_cvar_doc_matches_artifact[breadth]` failing). **The artifact was deliberately NOT hand-edited**
— editing a generated file by hand is how this repo has previously ended up with two documents
agreeing while both disagreed with the code.

**Clears on the next regeneration**, which is already planned to reset `provenance.git.dirty` once
this work is committed (~27 min). **If that run slips, this residual stays live** — the doc and the
page are correct, but the JSON's two strings are not. Nothing else in the artifact is affected: the
strings are prose fields, no count, gap, cost, plan or CVaR value depends on them.


## Found 2026-08-30 — the live-site sweep

The owner asked for every remaining error. These were found by calling the deployed API and
driving the deployed UI, not by reading code. **Four were user-facing failures on the live site.**

**46. `POST /optimize/vrp` returned HTTP 500 on ordinary carts.** `DONE`. The pre-flight checked
only that an offer ROW existed, never that it carried stock, so a zero-stock row passed and then
pinned every `q` below a demand equality — CP-SAT returned INFEASIBLE and the API reported
`"Solver failed: Sourcing MILP infeasible"`, which `CheckoutPage.tsx` renders verbatim. The server
had not failed; the catalogue was out of stock. **Reproduced live on `5a97482`**: component 24
(100 units in China, 3 domestically) with quantity 4 → 500. Blast radius, counted in the served DB:
**18 components have zero stock across every offer; 31 have a priced domestic offer with zero
domestic stock** — and three of the four strategies hard-code `us_only_sourcing=True`, so it fired
even for requests sent with `us_only=false`. Now a `ValueError` → **400** naming the part, the
quantity needed and the stock available. 4 tests; 3 proven red with the guard disabled (the 4th is
the feasible-side boundary and correctly stays green).

**47. `GET /newsvendor/evaluation` took 259.9 s and starved the entire API.** `DONE`. Render runs
**one uvicorn worker on 0.5 CPU**, so one CPU-bound request blocks everything, and abandoning it
does not stop the computation. Steady-state recompute measured at ~107 s; a source comment claimed
"~4 s", which was a dev-machine number (profiled locally at 3.4 s — production was ~30×). This was
also the true cause of the UI gate's random aborts: each abandoned run left the server saturated
for the next. Now served from `docs/newsvendor.json` — **~30 ms**, and all **72** reachable
configurations precomputed (255 s of generator time, artifact 65 KB → 0.98 MB). Served latency
median **0.7 ms**, max 3.7 ms. Every named block proved bit-for-bit identical (0 differences); all
72 recomputed the slow way and compared leaf-by-leaf.

**48. `review_period_months` 7–12 returned HTTP 500, and the dropdown offered "12 months".**
`DONE`. `run_panel_evaluation` splits a 6-month held-out horizon into `floor(horizon/L)` blocks and
raises when that is zero, so the advertised range was one click from a traceback. These could not
be precomputed without fabricating, so the range is now bounded to the horizon (**422**, derived
from `PANEL_HORIZON` rather than restated) with a `ValueError → 422` guard behind it, and the
frontend offers only 1–6.

**49. `/market/*` published fabricated constants.** `DONE`. When the upstream was unavailable the
routes returned `risk_weight_multiplier: 1.0`, `tariff_multiplier: 1.0`, `alerts_count: 0` and
`critical_alerts: 0` as bare values beside seven nulls — a reader could not distinguish "we checked
and the world is calm" from "we never fetched anything". This project forbids synthetic data
absolutely. Those fields are now `Optional` and `None` on every unavailable path, each response
carries an `unavailable_reason`, and `MarketSummaryResponse` gained the `available` flag it never
had. 33 tests, 10 mutations each proven red — **plus four mirror tests pinning the AVAILABLE path**,
so "null everything" cannot pass either. Also established by probing the vendor: the client POSTs
to `https://supplymaven.com/api/v1/tools`, which **404s with or without a token**, so it has never
once succeeded and adding a key would change nothing. Repointing it at the documented MCP endpoint
is unverified work and was deliberately NOT attempted.

**Superseded 2026-09-01 by item 55: the six routes were removed entirely.** The honesty fix
above was correct and shipped; the owner then decided that an endpoint which answers honestly
and can never carry data does not belong on a public surface. Item 49 is left here as the record
of what the routes did and why — do not read it as a description of a live feature.

**50. `/frontier` rendered a retired vintage.** `DONE`. `FrontierPage.tsx` hard-coded
"330 converged / 57 did not" and a primary-arm worst gap of "0.082%"; the artifact of the day said
**349 / 38** and **0.000%**. Pinned by nothing. Corrected against `docs/cvar_frontier.json`, and
now pinned by `test_frontier_page_matches_cvar_artifact.py` — which is what turned red on the
2026-09-01 regeneration and drove the page to the current **351 / 36**.

**51. `docs/cvar_frontier.json` was stale — the third recurrence.** `DONE`. Commit `6a33ad0` moved
`sourcing.py`; `volume_sweep.json` was regenerated for it and this one was not. It published
`first_stage_cost_usd: 181919.39` / 5 suppliers where the optimizer returns `181908.01` / 6 — the
same cell that moved in the volume sweep — and the doc showed a reader **$183,171**. It also
presented the shipped MILP beside the mean-value EEV baseline as if they were different plans; they
are the same plan. `test_cvar_doc_matches_artifact.py` was green throughout, because it compares
the doc to the artifact and both were stale together. Fixed by a real 1,331 s regeneration; exactly
10 fields moved, all inside the ×10,000 baselines. **Caught by the new artifact-vs-code pin on the
day it was written.**

**52. Solve-quality figures were published as findings.** `DONE` — **and superseded 2026-09-01
by the deterministic-budget fix recorded at the end of item 45; the counters now reproduce, and
`CVAR_EFFICIENT_FRONTIER.md` §0 says so instead of the reverse.** As it stood: the CVaR breadth
arm ran CP-SAT on a 15 s wall-clock budget with 46/150 solves hitting it, and two full
regenerations disagreed: 349→347
converged, p90 gap 3.787%→6.07%, `smart_meter` dropped from the table entirely; on a quiet machine
the counters returned but 16 gap values moved and two BOMs swapped a converged λ. **No plan and no
cost moved** — only the numbers describing the solve. Owner chose relabelling over raising the time
limit (which costs a 22-minute regeneration and would still be machine-dependent). Every
solve-quality figure was labelled a one-machine run log, at heading weight and outside the
`GENERATED:` markers so `render_doc()` cannot wipe it. Costs, plans, supplier sets, CVaR values and
the frontier itself are deliberately **not** caveated — over-caveating real results is its own
dishonesty. The identical claim in `BENCHMARK_VOLUME_CURVE.md` ("all 296 returned OPTIMAL — none
hit the 5s limit") is caveated the same way, and that one is still true of a wall-clock budget.

**2026-09-01 — the same principle, applied in the other direction.** Once the deterministic budget
made the counters reproducible, the "run log of one machine" labels became *under*-claiming, which
is the same dishonesty pointing the other way. Every one of those six labels was rewritten to say
what reproduces (the counters), what does not (elapsed time, permanently), and what a deterministic
budget does not buy (convergence — only reproducible truncation). The wording sits at the same
`###` weight and outside the `GENERATED:` markers, as before.

**53. The UI gate could never finish.** `DONE`. `waitUntil:'networkidle'` was **unsatisfiable** on
two routes — `/frontier` holds `stochastic/frontier` open, `/newsvendor` fired the 260 s solver —
so the gate threw FATAL rather than FAIL, at a different route each run. Navigation and readiness
are now decoupled: `domcontentloaded` + `#root > *`, then a bounded, *reported* readiness wait
(no first-party request in flight, no `.animate-spin`, held 500 ms, per-route caps sized from
measurement). Bounded retry around **navigation only** — never around an assertion. Third-party
basemap tiles excluded and the ignored hosts printed. **Nothing was weakened and it is checkable:**
the diff removes 8 lines, all navigation plumbing; `git diff | grep -c "^-.*ok("` returns **0**; no
threshold and no route×viewport matrix entry changed; 41 assertions were ADDED. Result:
**239 passed, 0 failed**. Proven still red four ways — hung endpoint, nonexistent route, absent
element, unreachable BASE. The `/newsvendor` settle cap was 300 s for the old recompute; now 30 s,
so a regression back to recomputing fails instead of hiding inside a five-minute budget.

**54. The two demand-series artifacts now have CI pins — but only their DETERMINISTIC arms.** `DONE`.

The first attempt promoted the WHOLE forecast pin and the WHOLE chronos classical-arms pin into
CI's default suite. Both passed locally and **both went red on CI** (run `33318131193`, commit
`16d3714`): 135 differing values in `forecast_backtest.json`, 61 in `chronos_benchmark.json`. Every
one of the **196** was a `prophet.*` key and none was a `seasonal_naive.*` key — at ~0.2–0.3 %
(*arithmetic corrected 2026-09-02: this sentence and commit `1c00994`'s message both said "160",
which is neither 135 + 61 = 196 nor either count alone. The two per-artifact counts are the
measured ones and are what the test file records; the total was simply mis-added.*)
relative (`prophet.overall.wape` 0.0313 vs 0.0312, `prophet.overall.rmse` 1413.3469 vs 1410.4055).
**The artifacts were current; the pin's scope was wrong**, and its message ("The ARTIFACT is stale,
not this test") was flatly false — a check that misdiagnoses is its own defect.

The arms are now split by **determinism, not by cost**:

| Arm | Runs in CI (`-m "not slow"`) | Why |
|---|---|---|
| `seasonal_naive` (both artifacts) | **yes** — 0.01 s / <0.01 s | No arithmetic of its own: it copies observations out of a SHA-256-pinned series, and every metric is `round(x, 4)`-ed before it is written |
| `prophet`, `prophet_served_config` | no — `slow`, local-only, ~0.5 s | Prophet fits via Stan (L-BFGS): **not bit-reproducible** across platform / interpreter / BLAS |
| `chronos` zero-shot | no — `slow` | torch + chronos-forecasting live in `requirements-ml.txt`, which CI never installs, so this arm was never compared on a second platform at all. Its determinism is **unproven**, not disproven — and an arm whose determinism cannot be shown is left `slow`, the safe side |
| `cvar_frontier` | no — `slow` | Opens a seeded `SessionLocal` CI does not have; ~43 s |
| `leakage_progression` | no — `slow` | ~5 s, reads `PANEL_PATH` directly (no DB) — a judgement call, not a hard constraint |

**KNOWN CONSTRAINT — Prophet cannot be pinned on CI.** The committed artifacts are generated on one
machine (macOS/arm64, Python 3.13); CI is Linux/x86_64, Python 3.11. Stan's optimiser gives
platform-dependent results and no seed or flag changes that. The two honest options were (a) run
the Prophet pin only where the artifact is written, or (b) widen the tolerance until a
non-deterministic fit passes anywhere — and (b) is a check that cannot reliably fail, which
the loop's learnings log (2026-08-28) forbids. **(a) was taken: both halves keep the same strict
`STAT_ABS_TOL`/`STAT_REL_TOL` of 1e-9.** The local standing gate (`pytest tests/ -q`, no `-m`
filter) still runs the Prophet pins on every push, on the only machine where those artifacts can
actually go stale. `slow` in this block means LOCAL-ONLY, not expensive.

**Evidence the classification is measured, not assumed.** Both artifacts' arms were re-scored
inside a `linux/amd64` container on CI's exact stack (Python 3.11.16, numpy 2.4.4, pandas 2.3.3,
prophet 1.3.0) under literal `!=` equality: `seasonal_naive` **0** differing leaves in both;
`prophet` 61 + `prophet_served_config` 74 = **135**, and chronos `prophet` **61** — reproducing
CI's failure counts exactly.

**Proven red.** `SEASONAL_PERIOD` 12→11 in `seeds/run_forecast_backtest.py:77` (a constant the
chronos generator imports rather than restates, so it moves both) fails the forecast pin with 69
differing values and the chronos pin with 61, while both Prophet pins stay green. Generator then
restored and confirmed byte-identical by sha256. The Prophet pins were separately shown red on
genuine drift (`weekly_seasonality` True) with the new, non-misdiagnosing message.

**Failure messages now match what a mismatch can mean.** A deterministic-arm mismatch still says
the artifact is stale, and says why that inference is safe. A Prophet-arm mismatch says
`THIS IS NOT NECESSARILY A STALE ARTIFACT` and walks the reader through ruling out platform
non-reproducibility first — OS/arch, interpreter, BLAS build, prophet/cmdstanpy/numpy/pandas
versions — then points at the deterministic pin as the test that actually answers "is it stale",
and only then at regeneration.

**Not pinnable, honestly:** `docs/backend_verification.json` has no generator — it is a hand-run
snapshot from the 2026-08-19 repair, and its content is 42 live HTTPS responses with a per-check
`seconds` field, so a pin would go red on a Render cold start and green on a broken build. Recorded
in the test file's docstring rather than faked.

**55. The six `/market/*` routes were removed from the API surface.** `DONE` (2026-09-01, owner
decision). They were the last open item in "Owner decisions — not mine to take".

**Why, on evidence, not taste.** Three facts, each checked rather than assumed:

1. **No consumer.** A case-insensitive grep of `frontend/src` for
   `market|tariff|gdi|risk_weight|supplymaven|commodit|alerts_count|critical_alerts` returns
   exactly one hit — the string "Unauthorized / gray-market channel", a component-sourcing
   tooltip in `SchedulerPage.tsx` unrelated to this router. The panel that would have consumed
   these was deleted on 2026-08-23; `DigitalTwinPage.tsx`, the intended consumer of
   `/trade-policy`, no longer exists and `/digital-twin` redirects to `/resilience`.
2. **No data, ever.** `supplymaven_client.py` POSTed to `https://supplymaven.com/api/v1/tools`,
   which **404s with or without a bearer token** — re-probed against the vendor on 2026-08-30,
   both verbs. The 404 body is the vendor's Next.js not-found page, so the REST path does not
   exist. Five of the six routes therefore never once returned real data, and adding a key
   would have changed nothing.
3. **Item 49 had already taken the honest option and it was not enough.** Those routes were
   fixed on 2026-08-30 to answer `available: false` + `unavailable_reason` with every invented
   number set to `None`. That was correct, and it left six public endpoints whose only possible
   honest answer is "there is nothing here". On a portfolio piece a reader opening Swagger finds
   a documented feature that cannot work — the removal is the fix the honesty pass exposed.

**Removed.** `backend/app/api/market_intelligence.py` (the router, all six routes and its eight
response schemas), its registration in `backend/app/api/__init__.py`,
`backend/app/core/clients/supplymaven_client.py` (checked first: `market_intelligence.py` was its
only caller anywhere in the repo, so it was left with none), its export from
`app/core/clients/__init__.py`, the `SUPPLYMAVEN_API_KEY` setting from `app/core/config.py`,
`backend/.env.example` and `render.yaml`, `backend/tests/test_market_intelligence_unavailable.py`
(the 33 unavailable-path tests plus the four mirror tests — they pinned routes that no longer
exist), the six `/market/*` 401 guards in `backend/tests/test_auth_guards.py`, and the six live
probes in `scripts/verify_backend.py`.

**Measured, not asserted.** `app.openapi()["paths"]` went **51 → 45**; the removed set is exactly
`/api/v1/market/{summary,disruption-index,alerts,commodities,trade-policy,status}` and **no other
path changed**. Operations 53 → 47, component schemas 102 → 94 (the eight `/market/*` models), tags
15 → 14 (`market-intelligence` gone). The only remaining "market" string in the spec is the
unrelated `include_unauthorized` "Include gray market offers" parameter.

**Deliberately NOT touched.** `docs/backend_verification.json` still records those six as `200` —
it is a dated hand-run snapshot from the 2026-08-19 repair with no generator (see the "Not
pinnable, honestly" note above), it was true on that day, and hand-editing a generated artifact is
the exact failure this repo has shipped twice. A fresh run of `scripts/verify_backend.py` now
simply reports six fewer checks. Archived documents under `docs/archive/` are likewise left as
written; they are superseded records, not live descriptions.

**One capability genuinely goes away.** `/market/status` was the only endpoint reporting which
distributor API keys (`NEXAR`, `DIGIKEY`, `OEMSECRETS`, `TRUSTEDPARTS`, `EASYPOST`) were
configured. It had no caller either, and the comments in `config.py` and `easypost_client.py` that
pointed readers at it now say it is gone rather than pointing at a 404.

**Safe against a stale environment.** `Settings.Config.extra = "ignore"`, so a `SUPPLYMAVEN_API_KEY`
still set on the Render service after this ships is ignored, not a startup error.

**Gates.** `ruff check app` and `mypy app` clean (76 source files). Suite collection **1123 → 1084**,
a delta of exactly the 39 tests removed (33 in `test_market_intelligence_unavailable.py` — 8+8+1+5+5+1+1
parametrised plus the four mirror tests and the client no-alerts/no-answer test — and the 6 auth
guards); **nothing else left the collection**. Full run, twice: **1078 passed, 2 failed, 3 skipped,
1 xfailed** in ~12 min. Frontend untouched, so `tsc -b` / `npm run build` were not re-run;
`git status --porcelain backend/seeds/data/` empty.

**The second failure is NOT from this change** — it is HEAD moving to `44e718c`
("data(lead-times): weekly observed snapshot 2026-08-31"), which rewrote
`backend/seeds/data/lead_time_panel/observed_lead_times.csv` without regenerating what depends on it.
`test_leakage_progression_reproduces_from_the_live_lead_time_model` fails on its own input-sha guard
(`c68e289124ec` on disk vs `0884a9778fe8` in `docs/leakage_progression.json`) before it fits anything;
both files are byte-identical to HEAD and neither is in this diff. The same commit explains the other
two deltas: `test_committed_artifact_beats_a_naive_baseline_on_genuinely_held_out_data` **xfails** by
design (the panel moved past the artifact — 263 → 324 feature columns, so there is no honest holdout)
and `test_panel_structure.py` skips with "artifact predates the current panel". `test_the_served_
estimator_is_the_one_the_metrics_describe` is the documented permitted local-only failure. **The owed
work is a retrain** (`python -m seeds.train_ml_models`) plus a `run_leakage_progression` regeneration —
separate from this item and deliberately not attempted here.

> **That retrain LANDED 2026-09-03.** `seeds.train_ml_models` (~7 min) then
> `seeds.run_leakage_progression` (337 s). The served artifact moved from 1,879 rows / 4 snapshots /
> 263 features / `0884a977…` to **2,615 rows / 5 snapshots / 324 features / `c68e2891…`**, champion
> still `gradient_boosting`, schema still v3, ship gate still PASS (4.788 d mean RMSE reduction vs
> `manufacturer_mean`, 95% CI [2.511, 7.367], 17/20 folds). The 50-fold `GroupKFold` progression
> moved **+0.804 / +0.084 / −0.784 → +0.825 / +0.073 / −0.697** (medians +0.810 / +0.183 / −0.105 →
> +0.826 / +0.140 / −0.104). The **regime model is byte-identical** — 219 folds, Brier 0.3926 vs
> persistence 0.5388 and climatology 0.6725 — so nothing in that half of the model card moved.
> `test_leakage_progression_reproduces_from_the_live_lead_time_model` now passes, and
> `test_committed_artifact_beats_a_naive_baseline_on_genuinely_held_out_data` **re-armed itself**
> (no longer xfail) and was proven still able to fail: substituting a constant predictor for the
> champion turned it red at 9.1% held-out RMSE reduction against a 10% floor.

**56. Four documents stated the lead-time panel's RETIRED size as the current one — one of
them on public GitHub.** `DONE`
(2026-09-01), found by an `ml-pipeline-verifier` pass.

**The defect.** `44e718c` ("data(lead-times): weekly observed snapshot 2026-08-31") added 1,533
rows of real DigiKey observations. It changed no code and regenerated nothing that depends on the
CSV, so every document describing "the panel" kept quoting the pre-commit size. Anyone who opened
the committed CSV could falsify them in one command. The distinction that had collapsed:

| Subject of the sentence | Truth | Source it was checked against |
|---|---|---|
| **the panel / the CSV** | **2,664 rows, 5 snapshot dates** (75 on 2026-07-01, 742 on 2026-08-15, 363 on 2026-08-17, 742 on 2026-08-24, 742 on 2026-08-31), 29 manufacturers, 748 MPNs, **324** design columns | `wc -l` + a `groupby` on `seeds/data/lead_time_panel/observed_lead_times.csv`, sha256 `c68e2891…`; columns recomputed with `build_observed_matrix` |
| **the served artifact** | **1,879 training rows** of a then-**1,922**-row panel, **4** snapshots, **263** features, **472** family group keys, **28** manufacturers, trained **2026-08-24T14:11:49Z** at `cf00e433` | `metrics.joblib['provenance']`, `feature_cols.joblib` (`len` = 263), `docs/leakage_progression.json` `counts.*` |

A sentence whose subject is the artifact is **stale-but-true** and was dated and kept, not
rewritten. Only panel-subject sentences were corrected. **Every number written was traced to
`metrics.joblib`, `feature_cols.joblib`, the JSON artifact or the CSV — none was copied from
another document**, which is how items 3 and 41 got their contradictions.

**Fixed.**

- `README.md` — the headline bullet said "the lead-time panel is 1,922 real observations across
  four snapshot dates"; the data-sources table row and the `**Data:**` one-liner said the same.
  Now 2,664 / five, with the 2026-08-31 date added. A second bullet was added naming the served
  artifact's vintage explicitly, so the `1,879` / `472` / `28` / `263` figures in the leakage
  table below it can never again be read as panel figures; the leakage table itself is now
  labelled "properties of the 2026-08-24 artifact vintage", in the same style as the retired
  810-row vintage already disclosed there.
- A maintainer working note (unpublished) restated the same figure and was corrected the same
  way: a two-row artifact-vs-panel table that names the gap rather than hiding it. The "paired
  change between the two snapshots" finding now names which two (2026-07-01 and 2026-08-15) —
  it was ambiguous the moment a fifth snapshot existed.
- `docs/PROJECT_OVERVIEW.md` — the capability table, the achievement bullet and the ST-event
  paragraph. Line 180 claimed **"there are two snapshots"**, which matched neither the artifact
  (4) nor the disk (5) and **was already wrong before `44e718c`**; the correction records that.
- `docs/RESEARCH_TECHNIQUES.md` — the "verified against the artifacts on disk" table row said
  "1,922 rows … 4 snapshots". That table's whole claim is that it was checked against artifacts,
  so a stale row there is worse than elsewhere. It now carries both vintages and the retrain debt.
- `backend/tests/test_model_ci_gates.py` — the docstring of
  `_warn_and_xfail_if_the_panel_moved_past_the_artifact` described a *simulated* 2026-08-31 run
  reaching "2,664 rows / **352** columns". **352 was wrong and self-inconsistent**: the same
  sentence says 263 columns plus 61 new ones, which is 324. Recomputing the design matrix gives
  **324** (2,615 trainable rows), and `docs/archive/MAINTENANCE-AND-KNOWN-ISSUES.md` and the item
  55 write-up above both independently say 324. Corrected, and de-hypothesised — the run is real
  now. Its breakdown of the 61 new columns was also wrong (it attributed all 61 to
  `c=category=*`; measured: 37 `package_case`, 12 `category`, 6 `dk_subcategory`, 6 `htsus_code`).
  **This is a docstring, not an assertion** — no gate threshold was touched.

**Deliberately NOT changed.** The `1,879` / `1,922` / `472` / `28` / `263` figures in
`docs/MODEL_CI.md`, `docs/LEAKAGE_PROGRESSION.md`, `docs/leakage_progression.json`,
`backend/app/api/ml.py` and `backend/app/ml/lead_time_model.py` are correct descriptions of the
served artifact and of that JSON's own generation-time provenance — `MODEL_CI.md:195` is literally
a table of `metrics.joblib['provenance']` fields, where `n_panel_rows = 1922` is the right answer.
Editing them by hand would fabricate a retrain that has not happened. They move together, from
`train_ml_models` + `run_leakage_progression`, when the retrain lands. Everything under
`docs/archive/` was left as written, per this file's own convention.

**The gap is disclosed, not papered over.** `model_store.check_training_data_staleness` compares
the panel sha256 the artifact recorded at fit time against the file on disk and currently returns
`stale: true` with both hashes and the retrain command; `/api/v1/ml/model-info` serves it. It is a
warning and not a build failure by design, so a scheduled collector commit cannot turn CI red on
its own. That tripwire working is the reason this was catchable at all.

**Still owed, and not attempted here:** the retrain
(`cd backend && python -m seeds.train_ml_models`) plus a `run_leakage_progression` regeneration.
Until then `test_artifacts_pinned_to_code.py::test_leakage_progression_reproduces_from_the_live_lead_time_model`
stays red on its input-sha guard and
`test_committed_artifact_beats_a_naive_baseline_on_genuinely_held_out_data` stays `xfail` — both
for this exact data-vintage reason, both **correct to be red**. Neither was "fixed" by editing an
artifact.

**57. The scenario cache could not tell that the code had changed, so a deploy kept serving the
previous build's sentences.** `DONE` (2026-09-01), found by measurement, not by reading.

**The defect.** `CacheManager.generate_key` hashed `scenario_type` + the request params and
nothing else — no component identifying the code that produced the body. The cache table lives in
the **tracked** `backend/supply_chain.db` with a 1-hour TTL, so any entry written by build N was
readable, byte for byte, by build N+1.

**Hit for real, which is how it was found.** After `_hedging_summary` / `_fulfilment_clause` in
`app/api/resilience.py` stopped claiming *"Zero fulfillment impact"* when the endpoint's own
fulfilment fields disagreed, the first boot after the fix served **the retired sentence out of
cache**, with `hedging.baseline_fulfillment_p50 = null`. The frontend caught it; the API did not.
The site published a claim the code no longer makes — the thing the standing bar exists to forbid.

**Same shape as the 2026-08-29 alembic incident, and CI is blind to it for the same reason.** CI
builds a fresh database with an empty cache table, so the cross-build read cannot happen there.
Only a database carrying rows an *earlier* build wrote can produce it, and that is exactly what the
tracked artifact is. `git show HEAD:backend/supply_chain.db` carries **10 such rows** today
(`delivery-target` ×4, `distributor-failure` ×5 from 2026-08-16, `cvar-frontier` from 2026-08-27),
every one with a bare 64-char key. Stated precisely, because it matters: those ten are all **long
expired**, so they were never served — they are dead weight that rides into production on every
deploy. The rows that bite are the unexpired ones, written minutes before the code changed.

**Fixed — one version signal, not a second mechanism.** New `backend/app/core/version.py`:

- `build_commit()` — `RENDER_GIT_COMMIT`, else `git rev-parse HEAD`, else `"unknown"`. This is
  lifted verbatim out of `main.py`, and `/version` now **calls it** rather than keeping its own
  copy, so "which build is live" and "which build wrote this cached body" cannot become two
  different answers.
- `code_version()` — a 12-hex token, now the leading component of every cache key
  (`"<code_version>:<sha256>"`, 77 chars, inside `cache_key`'s `String(512)`: **no migration**).

**Measured, not assumed, on cost.** The fingerprint is `lru_cache`d per process: **2.6 ms once**
over the 77 files of `backend/app`, then **1.9 µs** per key generated (10,000 calls in 18.6 ms).
Startup already warms it, so no request pays the 2.6 ms.

**Why the commit alone was not enough, and the fallback is not `"unknown"`.** Two holes, both
closed by mixing the commit with a **content fingerprint of `backend/app/**/*.py`**:

1. `"unknown"` as the whole signal would collapse every build to one token and turn the guard off
   *silently* — the failure mode this repo has already shipped three times. The fingerprint is a
   real hash in every environment, with no git and no env.
2. **The incident itself was an uncommitted edit.** `HEAD` does not move when you fix a function
   and restart, so a commit-only key would have reproduced the exact bug that prompted this. The
   fingerprint moves on content, so it does. It is content-based rather than mtime-based on
   purpose: a git checkout rewrites mtimes without changing a line of code.

**Stale rows: the key change is sufficient for correctness, the purge is about accumulation.** An
old key never matches, so no stale body can be served regardless of what is on disk. But the table
has no size cap, and pinning keys to the build means every deploy starts a fresh key space — dead
rows would otherwise sit there until their TTL expired *and* the 10-minute cleanup loop happened to
run in a process that lived that long. `CacheManager.purge_foreign_versions` deletes every row
whose key does not carry the running token; the lifespan calls it at startup (which also warms the
fingerprint before the first request) and the existing cleanup loop calls it alongside
`cleanup_expired`. The version is a **key prefix** rather than another hashed field precisely so
this is one indexed `NOT LIKE`, not a table scan of opaque hashes.

**Proved red before green, per the standing rule.** `backend/tests/test_cache_is_keyed_to_the_running_build.py`,
17 tests. Reverting `generate_key` to the pre-fix line and rerunning: **12 failed, 5 passed**, the
headline one on
`assert '5ef39da162ff…' != '5ef39da162ff…'` — the same request hashing identically across two
different deployed SHAs. With the fix: **17 passed**. The suite covers the deploy round trip, that
the old body is unreachable by any key the new build can compute, the purge (and that it does not
touch the running build's own rows), that params still decide the key *within* a build, that
`/version` and the cache read one identity, that the fingerprint tracks content and not
timestamps — `touch` must not invalidate, an edit must — and that the guard still distinguishes
builds when **no** commit is available at all, which is the case that would otherwise be a check
that cannot fail. All seven cached scenario types are parametrised.

**Gates.** `ruff check app` and `mypy app` clean (77 source files). Targeted runs, since a CP-SAT
regeneration held the cores: `test_cache_is_keyed_to_the_running_build.py` 17 passed,
`test_resilience_api.py` 34 passed, `test_input_sensitivity_regressions.py` +
`test_sourcing.py` 42 passed, `test_security_hardening.py` + `test_benchmark_api.py` 31 passed.
`git status --porcelain backend/seeds/data/` empty. `backend/supply_chain.db` still at `0009`,
matching `HEAD`, 791 / 92 / 8,176, `PRAGMA integrity_check` ok — **not committed** as part of this.

---

## P0 — claims the code contradicts

| # | Item | Where | Status |
|---|---|---|---|
| 1 | `risk_score` published as a **percentage**. It is a verbatim passthrough of a HuggingFace column (`seed_db.py:248`), an additive hand-weighted flag sum on a **6-value support**, and **48.9% of the catalogue carries a flat 0.20 placeholder**. The `0.4/0.7` bands sit inside a gap in the support (nothing exists between 0.25 and 0.60) so both thresholds are unfalsifiable; `SchedulerPage` uses a *different* set (0.3/0.6), so the same 13 ESP8266 parts render **red on /components and amber on /dashboard**. `models/component.py:17` claims it comes from "Nexar analysis"; `nexar_client.py:418` hardcodes 0.0. | `Dashboard.tsx`, `SchedulerPage.tsx`, `lib/risk.ts`, `models/component.py` | **DONE** — no `%` anywhere; one flag-based tier shared by both pages; 12 tests |
| 2 | `/resilience` baseline **ETA describes a different plan from the baseline cost**. `_bom_eta_days` is max-over-lines of **min**-over-suppliers (the fastest supplier in the catalogue); `_price_bom` buys the **cheapest**. On the demo cart 4 of 5 lines price to Weyland (Singapore, 26.6 d), so the true ETA of the $166.94 plan is **26.6 days, not 2.8** — a 9.4× understatement. The honest story is better: losing Weyland *improves* delivery ~3 days while raising cost 25.2%. | `api/resilience.py` | **DONE** — ETA now computed over the plan's own suppliers; baseline 2.8 → **26.6 d**; distributor-failure delta flipped 0.0 → **−3.2 d at +28.5% cost**; 4 tests, RED/GREEN verified |
| 3 | `/map` **Network Risk colour channel is dead**. `MapPage.tsx:566` feeds raw betweenness into `riskLabel()`, whose thresholds are 0.4/0.7 — but max betweenness across all 92 distributors is **0.2915**, so every marker is always green/"low" and no data could change that. Size channel uses 29.2% of its range. Calibrated for a min-max-normalised score later removed from the builder. | `MapPage.tsx` | **DONE** — percentile bands over the live distribution (max 0.2915); legend says "top 10% by betweenness", not "high risk" *(this row read **0.2458** / 24.6% until 2026-09-03: that was measured on the graph built from only 80% of supplier-part links, before the dead holdout carve was removed from `graph/builder.py`. The fix is unaffected — the bands are percentile-based and read from the live distribution, and 0.2915 is still far below the retired 0.4 cutoff.)* |
| 4 | `noise_floor_pct` is a **hardcoded 2.0** rendered as "well above **this run's** 2.0% noise floor". The schema comment literally says `# hardcoded 2.0`. Not derived from solver tolerance or replicate variance. | `api/benchmark.py`, `BenchmarkPage.tsx` | **DONE** — could not be derived (deterministic solve, replicate variance 0), so relabelled `materiality_threshold_pct` with a served basis saying it is assumed, not measured |
| 5 | "change in **collapse probability** (0–1 scale)" — the field is `1 − median fraction of BOM lines fulfilled`, quantised to quarters on 4-line BOMs. No base rate, no exposure window: **not a probability**. | `BenchmarkPage.tsx`, `api/benchmark.py` | **DONE** — now "change in median unfulfilled-line share (0–1)", corrected at the source string too |
| 6 | Volume curve overlays the **withdrawn run-4 headline (48.09)** onto a curve whose own 1× point is **47.22**, generated post-fix with different `us_only` and 10 BOMs. The caption "Same solver, same offer pool, same objective" is false relative to that reference line, and the API's own `aggregate_definition` says so. `docs/volume_sweep.json` also ships a stale `meta.known_bug` block declaring the optimizer broken — fixed 2026-07-13. | `BenchmarkPage.tsx`, `docs/volume_sweep.json`, `seeds/run_volume_sweep.py`, `lib/volumeDecayCurveData.ts` | **DONE** — reference line is the curve's own 1× (47.22%); stale `known_bug` removed from artifact AND generator; fallback provenance date corrected |
| 7 | `collapsed_boms` is **documented but never computed** — `main.py` never writes the key, all 6 served points return `[]`, and the page invites "Click a point to see which BOMs collapse". | `api/benchmark.py`, `main.py` | **DONE** — now computed: Arrow removes `pcb_power_supply`, Newark also removes `automotive_ecu`; serves `boms_checked` + `bom_source` so an empty list can't be misread |

## P1 — correctness

| # | Item | Where | Status |
|---|---|---|---|
| 8 | MILP **quantises unit prices to whole cents** (`int(round(price*PRICE_SCALE))`) while the objective carries milli-cent resolution and uses it for freight. `MLG0603P43NHT000` at $0.0031 → **0 cents: free to the optimizer**. 15 components have sub-$0.10 offers, error up to ~6%. The greedy baseline prices on full floats, so **the two benchmark arms optimise at different price resolutions**. | `sourcing.py`, `greedy.py` | **DONE** — one `to_obj_units()` is now the sole USD→objective conversion, applied once per term. The three risk surcharges had the SAME defect and floored to zero on cheap parts. Measured impact on the benchmark: **0 of 80 cells** (no benchmark BOM holds a sub-cent part). 17 tests. |
| 9 | `is_chinese_origin` is **double-counted** in the stock-out premium: `0.3·is_chinese + 0.2·stock + 0.5·risk_score`, but `risk_score` is *itself* 0.6 exactly when that flag fires. One binary attribute contributes 0.30 directly and 0.30 again. Also uses "calibrated" for a hand-chosen weight. | `sourcing.py` | **DONE** — `risk_score` removed from the vulnerability index; confirmed `risk_score ∈ {0.60,0.70}` is *exactly* the 14 rows with `manufacturer_country == "China"`, the same predicate `is_chinese_origin` fires on. Now `0.6·is_chinese + 0.4·stock`, summing to 1.0 so `RISK_PREMIUM_RATE` means what it says. "Calibrated" removed. **Moves 1 of 80 cells** — and makes one published resilience row worse, so a re-run is owed. |
| 10 | Live-price "cheapest first" **sorts raw price with no currency normalisation**, so a "best price" pick can be a non-USD figure. | `api/live_prices.py:278,391,502` | **DONE** — no FX source exists in this repo, so ranking/best-price is scoped to USD offers only; every offer still returned, non-USD ones carry `price_comparable=false` and sort after; response states `price_comparison_basis`; sync endpoint no longer writes a non-USD price into the USD `price` column; 18 tests |
| 11 | DigiKey live lookup is a `Limit:1` keyword search with **no exact-match check** — `ESP8266EX` returned `ESP-WROOM-02U` at $3.30 and the cart printed "Live: $3.30 (+574.5%)" with no SKU shown. | `digikey_client.py:87-113`, `CartPage.tsx:158-160` | **DONE** (backend) — `search_mpn` now requires the returned MPN to equal the query after normalizing case/whitespace/separators (checks `ExactMatches` then `Products`); a non-matching hit returns `None`, an honest miss, instead of the nearest part. `CartPage.tsx` untouched (out of scope for this pass) — see report for the optional SKU-display follow-up |
| 12 | Benchmark deltas are **uninterval'd means over 9 BOMs, 2 structurally zero** (effective n = 7). *(Corrected 2026-09-02: this description said "4 structurally zero (effective n = 5–7)", which contradicted its own resolution of `n_effective = 7`. The live `/benchmark/summary` returns `n_boms: 9`, `n_effective_boms: 7` and `zero_plan_boms: [drone_flight_controller, rf_transceiver_module]` — two, and 9 − 7 = 2.)* No CI, SE or replicate anywhere; single seed 42; `−0.0072×` published to 4 dp. This contradicts the repo's own ship standard (paired bootstrap CI excluding zero) that the ML models are held to. | `api/benchmark.py`, `seeds/run_benchmark.py`, `BenchmarkPage.tsx` | **DONE** — paired bootstrap over BOM clusters, 10k resamples. **3 of 5 deltas survive; both stress deltas do NOT** (stress_cascade CI [−27.78, +5.56] pp covers zero). Page neutralises colour, prints the interval, and the Honest-finding panel now says "no measurable effect" instead of claiming −8 pp. `n_effective = 7`, BOMs named. |
| 13 | CVaR-95 under stress **saturates at a 1.15 ceiling** on 8 of 9 BOMs (`1 + (4/4)·0.15`), so the metric is structurally incapable of discriminating in the scenario designed to create saturation. The probability that *would* discriminate (`n_scenarios_with_shortfall / n`) is computed and never persisted. | `graph/simulation.py`, `models/optimization_run.py`, `migrations/0009`, `seeds/run_benchmark.py`, `api/benchmark.py`, `BenchmarkPage.tsx` | **DONE** — `p_shortfall` / `p_total_shortfall` / `cvar_95_ceiling` / `cvar_95_saturated` computed, **persisted** (migration 0009 → `mc_*` columns), **served** (`/benchmark/summary` saturation block + `p_total_shortfall_intervals`) and **shown** (the `/benchmark` CVaR tiles flag a ceiling tie and name the tied BOMs). **16/18 published rows are at the ceiling; 10/18 are bit-identical ties; `p_total_shortfall` breaks 8 of 10.** Proved re-basing CVaR would not help (exact affine map, 0/36 deviate) with a test to stop it being tried. Also corrected `CVAR_EFFICIENT_FRONTIER.md`, which claimed this was fixed on 2026-08-16. |

| 21 | **The test suite cannot be run concurrently.** Fixtures build a fixed-name `backend/test_hardening.db`, so two pytest processes clobber each other's data — observed live on 2026-08-28: a targeted run returned `component_id 5 not found` / `404` on 5 stochastic tests purely because a sibling process was mid-fixture. The loop's learnings log warns "never kill pytest mid-flight — it poisons test_hardening.db" but the fixed filename is the actual defect. Give the DB a per-process unique name (PID or `tmp_path_factory`), which also makes `pytest -n auto` possible and would cut the 10-minute suite substantially. **Discovered by the loop, 2026-08-28.** | `backend/tests/conftest.py` | **DONE** — per-process name + session teardown; proved with 3 concurrent runs (24+34+45 passing) |

| 22 | **Nav overflowed at 1280px — the third recurrence of this defect.** Adding a tenth link (`/newsvendor`) pushed the desktop row to **1371px** while it collapsed to a hamburger only *below* Tailwind's `xl` (1280px), so at exactly 1280 the full nav rendered into a bar 91px too narrow. The agent that added the link measured at 1440, where it fits, and concluded it was safe. **The gate would have missed it too** — it tested 390/768/1440, and the bug lived in the gap between a breakpoint and the width the content needs. **Discovered by the loop, 2026-08-28.** | `NavBar.tsx`, `gate.js` | **DONE** — breakpoint moved to a measured `min-[1400px]`; verified collapsing at 1399 and fitting at 1440/1536; **1280 added as a fourth gate viewport** |

## P0 — found 2026-08-28 by the post-sweep verification pass

| # | Item | Where | Status |
|---|---|---|---|
| 23 | **The resilience summary published a claim its own adjacent field refuted.** `/benchmark/summary` served "the graph-aware arm **lowered** both plan cascade risk and the CVaR-95 tail" while returning `stress_cascade_risk_reduction = -0.0833` — the arm had **raised** it — and `intervals.stress_cascade_risk_reduction.significant = False` in the same response object. The branch tested only whether a reduction was *exactly* `0.0` and never looked at sign, so every negative value fell through to a hardcoded "lowered". At HEAD, on public Swagger, in the endpoint the honesty sweep had just rewritten. | `api/benchmark.py` | **DONE** — interpretation is now COMPOSED from `reductions` + `intervals`, moved after the bootstrap so it can consult significance; names wrong-way metrics as WRONG WAY and marks any interval covering zero "not quotable as a result". 9 tests, all verified RED against the old code. |
| 24 | **Public Swagger advertised a retired series count.** `api/newsvendor.py:11,422` said "2,643 held-out series"; the endpoint returns **2,646**. Item 16 built a doc-vs-artifact gate for `RESEARCH_TECHNIQUES.md` §3.4 but nothing guarded the docstrings, which are the copy a reader meets first. | `api/newsvendor.py` | **DONE** — corrected, plus 2 guards reading `docs/newsvendor.json`; verified RED on the stale number. |
| 25 | **Three docs quoted a retired leakage vintage with no caveat.** Old 810-row / 27-manufacturer / `random_forest` figures (R² +0.638→+0.082→−0.550) against a deployed artifact of 1,879 rows / 28 manufacturers / 472 families, champion **`gradient_boosting`** (`metrics.joblib['best_lead_time_model']`). | `PROJECT_OVERVIEW.md`, `RESEARCH_TECHNIQUES.md` (and one unpublished note) | **DONE** — all figures traced to `leakage_progression.json` / `metrics.joblib` and corrected; the retired vintage is kept, dated and labelled as superseded. |
| 26 | **`/ml` labelled a GSCPI regime probability "Semiconductor shortage stress".** The served signal is `RegimeModel.stress_proba` = P(GSCPI z > band-hi), a general pressure regime; the semiconductor-specific `compute_stress_label` is marked legacy and is not wired to it. | `api/ml.py:441` | **DONE** — relabelled "Global supply-chain pressure (NY Fed GSCPI regime)". |
| 27 | **A served caveat no reader could see.** `docs/diversification_frontier.json` carries "these figures reproduce against the pre-fix solver only"; `benchmark.py` passed `caveats` through and `BenchmarkPage.tsx` rendered only 5 hand-picked fields, dropping 3 including that one. | `BenchmarkPage.tsx` | **DONE** — the full served caveat array is rendered. |
| 28 | **The frontier generator would have reintroduced a retired claim.** The artifact was hand-corrected after the `to_obj_units()` fix; `run_diversification_sweep.py` still held the present-tense "quantises unit prices to whole cents" text, so regenerating would undo the correction. | `seeds/run_diversification_sweep.py` | **DONE** — generator's 8 caveats now match the artifact byte-for-byte. |
| 29 | **Two tests could only ever fail on CI.** `test_cvar_saturation.py` compared an accumulated float mean to the closed-form `1.15` ceiling with `==`; exact locally, `1.149999999999999` on CI. Broke the build and blocked the deploy of `1536742`. Production was never affected — `simulation.py:327` already uses `>= ceiling - 1e-9`. | `tests/test_cvar_saturation.py` | **DONE** — the arm-vs-arm tie stays exact (that is the claim); the ceiling comparison uses the same `1e-9` the served flag uses. |
| 30 | **`graph_aware` / `us_only` never reached the live optimizer** — `api.ts` posted no body, so the page always ran both flags off. **Owner approved wiring, 2026-08-28.** | `api.ts`, `CheckoutPage.tsx` | **DONE** — two toggles, both defaulting off so the standard view is unchanged. Honest labels: `us_only` moves only Lowest Cost (3 of 4 strategies are already domestic-only), and `graph_aware` returns an identical plan on the demo cart, which the page states rather than hides. 7 solver-level tests + a gate interaction check. |

| 31 | **The CI caveat was set in smaller type than the claim it qualifies** — 11px prose against the significant branch's 12px, on `/benchmark`. The gate's rule is that sub-12px BODY text is the anti-pattern, and this text is prose, not a caption. **It could not be caught before the deploy:** the gate proxies API calls to the LIVE API, and the old deployed API returned no `intervals`, so the element never rendered and the check passed vacuously. A local build is only as good as the API it is pointed at. | `BenchmarkPage.tsx:402` | **DONE** — `text-xs`, matching its sibling branch; live gate 138/138. |

| 32 | **`/frontier` printed the literal string `undefined` to the reader** — "4 suppliers [9, 70, 81, 85] instead of the risk-neutral **undefined**". `${riskNeutral?.n_suppliers}` in a template literal renders the STRING when optional chaining short-circuits. Root cause was deeper than the interpolation: the backend measures `cvar_reduction_*` against the cheapest **non-dominated solved** point, not λ=0, so λ=0 was never the right denominator to name — and on a partial sweep it is absent entirely. | `FrontierPage.tsx:363-384,660-679` | **DONE** — the page now derives the baseline the same way the server does and names it, with a footnote when λ=0 is not on the frontier. |
| 33 | **No gate check looked at rendered WORDS.** 10 routes × 4 viewports of green said nothing about item 32, which was live in production. | `ui-gate.cjs` | **DONE** — a leaked-placeholder check (`undefined`/`NaN`/`null`/`[object Object]` in own text nodes, word-bounded) at every route and viewport. Verified RED against the real unfixed build: 4 failures, one per viewport. |
| 34 | **`fmtUsd` dropped cents and sign site-wide** — `$643.1` in a column of `$368.34`. No `minimumFractionDigits`, and `Math.abs` discarded the sign. | `BenchmarkPage.tsx:432-449` | **DONE** — all 21 call sites audited so restoring the sign does not double up. |
| 35 | **A chart's x-axis label sat on top of its own legend** at all four viewports (286×15px of overlap). Margin could not fix it: the label is positioned off the axis and recharts stacks the legend off the same axis, so they move together. | `BenchmarkPage.tsx:1792-1805` | **DONE** — legend moved above the plot. Gate now measures label-vs-legend-ITEM boxes at every viewport; verified RED. |
| 36 | **Chart legend contrast below AA** — measured against the real composited background, 3.34:1 and 3.55:1 against a 4.5:1 requirement. axe reports these "incomplete", not violations, so it never surfaced. | `BenchmarkPage.tsx:294-298` | **DONE** — 10.7:1 and 5.3:1, differing in lightness not only hue, plus a P10/P50/P90 text table so nothing depends on telling two colours apart. Gate check added, resolving colours through a canvas rather than parsing (the `oklch()` trap). |
| 37 | **Markdown backticks rendered literally** — 24 on screen in the caveat list. The split must be NESTED, not alternated: two caveats open with a bold title whose first word is itself a code span. | `BenchmarkPage.tsx:470-503` | **DONE** |
| 38 | **`frontier.verdict` was served but never rendered**, beneath a footer claiming "nothing on this page is a hardcoded copy of it". The text matched, so nothing on screen was false — but the claim about itself was. | `BenchmarkPage.tsx:508-539,1715` | **DONE** — the served string is rendered; the footer claim is now true. |
| 39 | **A cost-multiplier delta described as "percentage points"** — the tile said `-0.0309× change in cost multiplier`, the prose beside it said "3.09 pp of CVaR-95". | `BenchmarkPage.tsx:1563-1572` | **DONE** |
| 40 | **Volume-curve axis label clipped at 390** — ran to x=441 in a 390px viewport with no genuinely scrollable ancestor (the wrapper had `overflow-x: auto` but `scrollWidth === clientWidth`, so a naive ancestor check called it clean). | `VolumeDecayCurve.tsx:99-115` | **DONE** — the Price-of-Resilience scroll pattern. |


## P2 — surface work that is already built and invisible

| # | Item | Status |
|---|---|---|
| 14 | **Newsvendor has no UI.** | **DONE** — `/newsvendor` route: decision (τ, q*, quantile ladder, naive rules), evidence (CIs drawn as intervals against a zero rule, not colour alone), and the MASE-vs-decision-cost argument as its own section. Every figure read live, nothing hardcoded from docs. |
| 15 | **Price-of-resilience frontier has no UI.** "The second supplier removes 0.44 of targeted cascade risk for $58.88 (CI 0.22–0.67); the third costs 6.8× more and its CI covers zero." Exists only in `docs/DIVERSIFICATION_FRONTIER.md`. | **DONE** — `GET /benchmark/diversification-frontier` + a "Price of Resilience" section on /benchmark: chart with unit-labelled axes and dash-patterned series (nothing by colour alone), two tables with CI bars on one shared zero-containing domain, and the headline sentence COMPOSED BACKEND-SIDE from the fields it quotes — it returns empty if the first step covers zero. 23 tests. |
| 16 | No `seeds/run_newsvendor.py` + doc-match test, so the published newsvendor numbers are **not auto-checked** like every other artifact. **Now urgent and proven:** §3.4 says 2,643 series / 47,574 decisions; the live API returns **2,646 / 47,628** because the `_size_shape` Poisson-limit clamp (fixed 2026-08-28) means 3 previously-dropped series now survive. The doc drifted within a day of being written. | **DONE** — `seeds/run_newsvendor.py` + `docs/newsvendor.json` + a 17-test doc-vs-ARTIFACT match (never doc-vs-doc). Confirmed the drift independently and rewrote §3.4: the defect paragraph claimed the `_size_shape` fix "has not been made" when it landed in the same commit. |

## P3 — hygiene

| # | Item | Status |
|---|---|---|
| 17 | `MAINTENANCE-AND-KNOWN-ISSUES.md` carried two deferrals that were no longer true, one of which told a future reader NOT to re-run the benchmark — the exact fix that was needed. | **DONE** — both retired to a new "Resolved — do not re-open" section with what actually happened; the doc now points at this backlog as the live list |
| 18 | `serving.py` docstring said the regime artifacts are not git-tracked and absent in prod. They **are** tracked (`.gitignore` un-ignores both with `!`) and **are** what prod serves. | **DONE** |
| 19 | `stress_level` cutoffs were bare literals with no constant or comment, so "why those numbers?" had no answer. | **DONE** — named constants, documented as a display convention (the optimizer reads the probability, never the label), grounded against a measured base rate of **0.1681** (57/339 months): HIGH ≈ 4.2× base rate, MODERATE ≈ 2.1× |
| 20 | `api/stochastic.py` published a retired defect in the PRESENT tense on the public Swagger surface, telling readers a fixed bug was still live. | **DONE** — moved to past tense, kept as "what it replaced" |

## Owner decisions — not mine to take

- ~~`graph_aware` / `us_only` never sent by the live optimizer~~ — **RESOLVED 2026-08-28**, owner approved; see item 30. Both are now toggles on `/optimize`, defaulting off.
- Render Starter ($7/mo) to kill the 50–120 s cold start. **Owner said leave it on free, 2026-08-28.**
- ~~FRED write-on-read into a tracked CSV (~2 h).~~ **RESOLVED 2026-08-29** — this entry was
  stale: item 4 above records the same defect as VERIFIED LIVE, then FIXED, and the code agrees
  (`refresh_cache: bool = False` by default in `app/ml/fred_client.py` and `app/ml/regime_model.py`;
  only `seeds/train_ml_models.py:46` passes `True`). The document was listing it as both fixed and
  pending at the same time.
- Python 3.13/3.11 provenance skew (~1 h + retrain).
- ~~Six caller-less `/market/*` routes on public Swagger.~~ — **RESOLVED 2026-09-01**, owner
  chose removal; see item 55.

## Completion criteria — the promise `ALL GREEN` may ONLY be emitted when every line is true

This exists so the claim is falsifiable. Each line is a command with an expected
result, not a judgement call. If any one of them fails, the promise is false and
must not be said, regardless of how much work has been done.

1. **Backlog clear.** Every P0, P1 and P2 item above is marked `DONE` or
   `DEFERRED (owner)`. P3 items may remain `TODO`.
2. **Backend suite.** `cd backend && ./venv/bin/python -m pytest tests/ -q`
   → 0 failures, except the single documented local-only MLflow identity check
   (`test_the_served_estimator_is_the_one_the_metrics_describe`), which passes in CI.
3. **Lint and types.** `./venv/bin/ruff check app` and `./venv/bin/mypy app` both clean.
4. **Frontend.** `cd frontend && npx tsc -b --force && npm run build` both clean.
   **NOT `tsc --noEmit`** — item 41 above proves that invocation typechecks nothing and exits 0
   on any error. This criterion prescribed it until 2026-08-30.
5. **Browser gate against the LIVE site**, not a local build:
   `cd frontend && BASE=https://supply-chain-ui-bhwz.onrender.com npm run ui-gate`
   → **188 passed, 0 failed** across the 10 routes in `scripts/ui-gate.cjs` x 4 viewports
   (1440/1280/768/390), plus head/meta checks on `/login`. Covers overflow, emoji, type
   size, leaked placeholders in rendered words, clipped chart labels, chart geometry,
   legend overlap and contrast, touch targets, axe serious/critical, head tags, console errors.
6. **ML verified against the deployed artifacts.** An `ml-pipeline-verifier` pass
   returns no FAIL: every published figure traces to `metrics.joblib`, no raw score
   is published as a probability, no technique is claimed that the code does not
   implement, and no endpoint described as live is unreachable.
7. **Deploy is real.** Render API reports `status: live` on BOTH services for the
   current SHA, AND `/version` + `/version.json` + `git rev-parse HEAD` all agree.
   A green GitHub "Deploy to Render" step is NOT sufficient — it only means triggered.
8. **Tree is honest.** `git status --porcelain backend/seeds/data/` empty; nothing
   under `.claude/**` staged or committed.

A green run that produced nothing is a lie. If a check cannot be run, the promise
is false — "not checked" is a failure, not a pass.

## Standing gates — every change must pass

- `cd backend && ./venv/bin/python -m pytest tests/ -q` → expect **0 failures except** the one
  documented local-only MLflow identity check (`test_the_served_estimator_is_the_one_the_metrics_describe`),
  which is green in CI. An absolute pass count is deliberately not written here: it was stated as
  **997** while the suite actually collected **1,121** (2026-08-30), and it moves with every test added.
  The gate is "nothing red but that one", not a number.
- `./venv/bin/ruff check app` and `./venv/bin/mypy app` clean.
- `cd frontend && npx tsc -b --force && npm run build`. **Never `tsc --noEmit`** — see item 41.
- Browser gate: `cd frontend && npm run ui-gate` over **10 routes × 4 viewports** → **188 passed, 0 failed**.
  (The routes array in `frontend/scripts/ui-gate.cjs:66` holds 10 entries; `/login` is additionally
  checked for head/meta. "11 routes" written elsewhere is wrong — 188 is a check count, not routes×viewports.)
  The gate proxies API calls to the LIVE API, so a check can pass vacuously when the deployed
  API does not yet return the data the element needs. A green local gate is only as good as the
  API it is pointed at.
- `git status --porcelain backend/seeds/data/` empty — never let a seed CSV drift.
