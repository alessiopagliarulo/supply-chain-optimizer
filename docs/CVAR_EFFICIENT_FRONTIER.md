# The price of resilience: a cost-vs-CVaR efficient frontier

**Script:** `backend/seeds/run_cvar_frontier.py` · **Data:** `docs/cvar_frontier.json`
**Model:** `backend/app/optimization/stochastic.py` · **API:** `POST /api/v1/stochastic/frontier`
**Solver:** OR-Tools CP-SAT, `num_search_workers=1` · **Run date, commit, input hashes and
hardware:** see [Provenance](#provenance) at the foot of this document, generated from the
artifact rather than typed here.

> ### ✅ Costs, plans, supplier sets, CVaR values and the frontier reproduce — and since 2026-09-01 so do the **solve-quality** figures. ⚠️ **Elapsed times still do not.**
>
> **Every converged count, MIP gap, per-BOM convergence flag and non-`OPTIMAL` return in
> this document now reproduces under any CPU load**, because the sweep runs on a
> *deterministic work budget* rather than a clock. Before 2026-09-01 they did not, and two
> honest regenerations of the artifact disagreed on them.
>
> **What is still a run log of one machine is every elapsed-time figure** — `solve_seconds`,
> `sweep_wall_seconds`, `meta.wall_seconds`, and the `Solve` / `Wall` / `λ-sweep wall time`
> columns at §4, §7 and §9. Those are labelled as such where they appear.
>
> ### Read §0 before quoting any of them.

---

## The curve

![CVaR efficient frontier — expected cost against CVaR-95, one point per lambda-solve, with the knee at lambda = 0.3](screenshots/current/11-frontier.png)

*Rendered by the app from `POST /api/v1/stochastic/frontier`, which reads the same
`cvar_frontier.json` this document is generated from. Expected cost runs along one axis and
CVaR-95 — the **mean cost of the worst 5% of scenarios** — along the other; every point is
one λ-solve. The knee at λ = 0.3 is where the chord ratio quoted below stops being worth
paying: $4.27 of tail risk removed per $1 of expected cost up to that point, $0.41 beyond
it. Both figures are averages over a stretch of the curve, not marginal rates at a point,
and both are specific to the headline BOM at 60,000 units — at 100× and 1,000× volume the
`knee` is `null` because the frontier is flat and there is no trade-off to price.*

---

## What this replaces

Every "resilience" number this repository produced before today was a **deterministic
surcharge**. `sourcing.py` prices supply risk as `RISK_PREMIUM_RATE = 0.15` times a
hand-weighted vulnerability score, plus a betweenness-weighted "expected recourse loss".
`graph/simulation.py` prices a disruption as a flat 15% cost inflation per unfulfillable
BOM share — which is why its `cvar_95` pins at 1.15 in nearly every published benchmark
row. Neither contains a **recourse decision**: nothing in either model re-optimizes after
a supplier goes dark.

This replaces that with a two-stage stochastic program and publishes the whole
**price-of-resilience curve** instead of one hard-coded risk appetite.

<!-- GENERATED:headline_pitch:BEGIN -->

> The old pitch: *"I added a 15% risk surcharge."*
> The new pitch: *"On a 60,000-unit BOM, spending **1.12% more in expectation** removes **3.88% of CVaR-95 exposure** — $2,044 buys $8,719 of tail reduction, a **4.27:1** return. Past that point the same trade returns **0.41:1**. The knee is at λ = 0.3 and that is my recommendation."*

<!-- GENERATED:headline_pitch:END -->

---

## 0. Solve quality — reproducible since 2026-09-01, and exactly what that does and does not buy

> ### ✅ THE SOLVE-QUALITY COUNTERS IN THIS SECTION REPRODUCE. BEFORE 2026-09-01 THEY DID NOT.
>
> ### Converged counts, MIP-gap percentiles, per-BOM convergence and non-`OPTIMAL` returns are now properties of the model and its compute budget — not of the machine or the CPU load it ran under.
>
> **The mechanism.** Every solve in this artifact runs under CP-SAT's
> `max_deterministic_time`: a **work** budget, not a clock — **15 units per solve in the
> `breadth` arm, 80 units in `primary`** (which also covers the sensitivity and SAA arms)
> — at `num_search_workers = 1` and `relative_gap_limit = 0.0`. CP-SAT accumulates its own
> deterministic measure of the search it has performed and stops at the **same node**
> however fast the machine got there, so the status, the bound, the gap, the objective and
> the plan are identical on a saturated laptop and an idle server. The wall clock is still
> applied, but only as a **runaway guard** (300 s breadth / 1,600 s primary — twenty times
> the work budget). See `meta.solver.budget_kind` in the artifact.
>
> **This is measured, not a design claim — and the measurement came BEFORE the
> regeneration, not after it.** A 15-solve verification sweep (3 instances × 5 λ, run as
> full `compute_frontier` sweeps so the λ-to-λ warm-start chain was exercised) was run five
> times in five separate interpreter processes, and every published field was hashed:
> `solver_status`, `mip_gap_pct`, expected cost, CVaR, VaR, first-stage cost, expected
> recourse, supplier count, supplier ids, variable count and tail atoms.
>
> | Run | Budget | Load average (1-min) | `OVERALL_SHA256` |
> |---|---|---:|---|
> | W1 | wall clock, 15 s | 2.45 | `8f6eeab5f6e22684…` |
> | W2 | wall clock, 15 s | **35.45** | `421cd46a86848a6d…` — **differs from W1** |
> | D1 | **deterministic, 15 units** | 2.45 | `10d34ccfae6868c0…` |
> | D2 | **deterministic, 15 units** | **43.47** | `10d34ccfae6868c0…` — **identical** |
> | D3 | **deterministic, 15 units** | 2.64 | `10d34ccfae6868c0…` — **identical** |
>
> The three deterministic runs also matched **per instance**, not merely in aggregate:
> `8a0f8211…`, `28023494…`, `74b538eb…` in all of D1, D2 and D3.
>
> ### ⚠️ These five digests are of the 15-solve VERIFICATION SWEEP, not of `docs/cvar_frontier.json`.
>
> The published artifact is a 387-solve run over ten BOMs, three volumes and a 36-cell
> sensitivity grid. **You cannot recompute `10d34ccfae6868c0…` from it**, and nothing in the
> artifact carries these hashes. They are evidence about the *budget mechanism*, gathered on
> a small controlled sweep so the two budget kinds could be compared at a saturated and an
> idle load without paying for a 27-minute regeneration each time.
>
> **What the wall-clock control cost, on that same sweep, purely from load:**
>
> * `smart_meter ×10` — gaps `[5.156, 0.0, 8.692, 12.390, 18.511]` with **one** converged λ
>   went to `[5.561, 8.179, 13.389, 20.854, 19.000]` with **none**. That is the mechanism by
>   which a published BOM row disappeared from the §8 table entirely: not a data change, a
>   busy CPU.
> * `rf_transceiver_module ×1` — worst gap **92.690% → 94.352%**.
> * `pcb_power_supply ×100` — **identical either way**. It converges long before any budget
>   binds, which is exactly why the primary arm was never affected.
>
> **The root cause, measured directly:** at the same 15 s clock, `smart_meter ×10` received
> **6.7–13.5** units of deterministic work when idle and only **1.8–4.7** when saturated.
> Same budget on paper; a third of the search in fact. A wall clock buys *time*, and time
> buys a variable amount of *work* — which is the thing the answer actually depends on.
>
> The regeneration that produced the current artifact then reported
> `solve_quality.deterministic_budget_in_force: true` and `n_wall_clock_bound: 0`.
>
> ### What a deterministic budget does NOT buy — three things, all of them load-bearing
>
> **1. Elapsed time is still not reproducible, and never will be.** Every `solve_seconds`,
> every `sweep_wall_seconds` and `meta.wall_seconds` measures how long the hardware in
> [Provenance](#provenance) (Python 3.13.5 · arm64 / Darwin 25.5.0) took to perform that
> fixed amount of work. Run it on a faster box, or on this box while something else is
> compiling, and every one of them moves. **Read every second in this document as a run log
> of that machine** — §4's `Solve` column, §7's `Wall` column and the whole of §9. The work
> those seconds bought is fixed; the seconds are not.
>
> **2. Truncation is now REPRODUCIBLE, not absent.** A deterministic budget does not make a
> hard instance converge; it makes it fail in the same place every time. Three instances in
> the `breadth` arm are genuinely hard and are still reported as such:
>
> * **`drone_flight_controller ×1`** — no converged λ at all (gaps 48.32–62.25% across all
>   five), so the row is marked **excluded** and no frontier is reported for it;
> * **`automotive_ecu ×1`** — likewise no converged λ (12.43–90.79%), also **excluded**;
> * **`rf_transceiver_module ×1`** — one of five λ proved, the other four left at
>   90.46–94.96%, including the worst single solve in the entire run (**94.955%** at
>   λ = 0.75).
>
> **That exclusion is a statement about the compute budget, not about the BOM.** The same
> `rf_transceiver_module` closes all five λ to a **0.000%** gap at ×100 and at ×1,000;
> nothing about the part list is intractable. And raising the budget does not rescue the
> hard end: a **20× budget sweep** on `rf_transceiver_module ×1` moved its worst gap only
> from **92.69% to 89.12%** — three percentage points for twenty times the compute. (Those
> two figures are a bench measurement of the budget, made against the previous wall-clock
> vintage; they are not fields of this artifact. This artifact's own worst gap for that
> instance, under the shipped 15-unit budget, is 94.955%.)
>
> **3. It did not move the economics — because those were never the problem.** Not one
> plan, not one supplier set, not one cost, not one CVaR value and not one frontier point
> differed when the budget kind changed, and none of them differed across the two
> wall-clock re-runs before it either. **The economics this document publishes were always
> reproducible.** What changed on 2026-09-01 is that the telemetry describing how hard they
> were to prove is now reproducible too.
>
> ### The falsifiable check: `solve_quality.n_wall_clock_bound` must be 0.
>
> A work budget only buys reproducibility while the **clock** never binds. So every solve
> records whether the runaway guard — rather than the work budget — is what stopped it, and
> the run-level count is published as `solve_quality.n_wall_clock_bound`. **It is 0 in the
> committed artifact.** A nonzero value would mean that many solves stopped wherever a
> stopwatch happened to land, that their counters are load-dependent again, and that every
> count in this section had reverted to being a run log of one machine. It is a check that
> can be made to go red: set the guard below the work budget and it is.
>
> **`hit_time_limit` and `solve_quality.n_time_limit_hits` are historical field names.**
> They count solves that returned a status other than `OPTIMAL` — that is, solves that
> exhausted the per-solve *budget* with the bound still open. Under a deterministic budget
> that budget is work, not elapsed time, so those counts are not clock- or load-dependent.
> The field that would report a clock-bound solve is `n_wall_clock_bound`.

> **Read this before any number below.** A frontier point is only a frontier point if
> CP-SAT actually *proved* its first-stage choice near-optimal. A previous full run of
> this script published 153 λ-points of which **49 carried an optimality gap above 5%
> (worst: 93.4%)** and 22 returned status `FEASIBLE` rather than `OPTIMAL` at the per-solve
> budget — and nothing in the artifact or in this document said so. A plan
> whose objective could be 93% away from the unknown optimum tells you nothing about the
> price of resilience.
>
> That is now fixed at the source. **Every point in `docs/cvar_frontier.json` carries
> `solver_status`, `mip_gap_pct`, `hit_time_limit`, `converged` and, when it failed to
> converge, an `excluded_reason`.** Non-converged points are *kept* — deleting them would
> hide the cost of the compute budget — but they are excluded from every knee, every
> reported spread and every headline figure in this document. The rule is
> `converged := status == OPTIMAL` (CP-SAT closed the bound to
> `relative_gap_limit = 0.0` — proved outright, not to a tolerance) `OR mip_gap_pct ≤ 5%`.
>
> **`OPTIMAL` used to mean "close enough".** The relative gap limit was `0.001` until
> 2026-08-27, so CP-SAT was entitled to stop 0.1% short of the bound and still return
> `OPTIMAL`. It did: points in artifacts generated before that date carry gaps of
> 0.04–0.08% under an `OPTIMAL` status. The limit is now `0.0`, so the two words mean the
> same thing, and every point in the §4 table below reports a gap of exactly 0.0000%.
>
> **The headline is unaffected.** Every λ-solve in the primary arm — the arm §4, §5 and
> the "$ of tail removed per $ spent" figure are built on — returned `OPTIMAL`, at the
> gaps shown per point in the §4 table. Whatever fails to converge does so in the breadth
> and sensitivity arms, on much larger supplier pools against a much smaller per-solve
> budget, and the tables below flag those instances individually. That the primary arm is
> fully proved is asserted by `test_the_headline_arm_is_fully_proved`, not assumed. Its
> 27-of-27 `OPTIMAL` held across every re-run above, and under the deterministic budget it
> is a statement about **80 units of search work** rather than about this machine's
> stopwatch — so a slower box closes it too, it just takes longer to do so. The test is
> what keeps the claim honest if it ever stops holding.

### ✅ Reproducible — 15-unit / 80-unit deterministic work budget, `n_wall_clock_bound = 0`, any CPU load. The **seconds** quoted in the worst-solve line are not. See the notice at the head of this section.

<!-- GENERATED:solve_quality:BEGIN -->

Across the whole run, **387 λ-solves** were performed. **347** converged; **40** did not and are excluded from every knee, spread and headline below. **50** returned a status other than `OPTIMAL` — that is, they exhausted the per-solve budget with the bound still open. That budget is DETERMINISTIC work, not elapsed time, and `n_wall_clock_bound = 0` records that the runaway guard stopped no solve, so every count in this block reproduces under any CPU load.

| | |
|---|---:|
| Solves | 387 |
| Converged (`OPTIMAL`, or gap ≤ 5%) | 347 |
| **Not converged — excluded from the frontier** | **40** |
| Non-`OPTIMAL` solver returns | 50 |
| MIP gap: median | 0.000% |
| MIP gap: p90 | 6.361% |
| MIP gap: p99 | 91.640% |
| **MIP gap: worst** | **95.057%** |
| Solves above a 1% gap | 45 |
| Solves above a 5% gap | 40 |
| Deterministic work budget in force | yes |
| **Solves the wall clock stopped — must be 0** | **0** |

Per arm:

| Arm | Solves | Converged | Non-`OPTIMAL` | Worst gap |
|---|---:|---:|---:|---:|
| `breadth` | 150 | 110 | 48 | 95.057% |
| `primary` | 27 | 27 | 0 | 0.000% |
| `saa_endpoint_stability` | 30 | 30 | 0 | 0.000% |
| `sensitivity` | 180 | 180 | 2 | 0.604% |

Worst single solve: arm `breadth`, instance `iot_sensor_node_x1`, λ = 0.25 — status `FEASIBLE` at a **95.057%** gap at the 15-unit deterministic-time budget (it used 16.068s of wall clock against a 300s runaway guard).

<!-- GENERATED:solve_quality:END -->

---

## 1. The formulation, stated precisely

**First stage** (here-and-now, before uncertainty resolves):

| Variable | Domain | Meaning |
|---|---|---|
| `y[d]` | {0,1} | qualify / open distributor `d` |
| `x[c,d]` | {0,1} | award BOM line `c` to distributor `d` |
| `q[c,d]` | ℤ₊ | units of `c` committed to `d` |

subject to, for every BOM line `c`:

```
Σ_d q[c,d] = demand_c                    demand coverage
q[c,d] ≤ stock[c,d] · x[c,d]             stock cap
q[c,d] ≥ moq[c,d]   · x[c,d]             MOQ floor
y[d]   ≥ x[c,d]                          a used supplier is an opened supplier
```

First-stage cost — **identical, term for term, to the deterministic MILP's objective**
(it calls the same `_freight_model_by_did` helper, so the two are directly comparable):

```
F = Σ_{c,d} price[c,d]·q[c,d]  +  Σ_d fixed[d]·y[d]
  + Σ_d per_unit[d]·Σ_c q[c,d] +  Σ_d consolidation·y[d]
```

**Uncertainty.** A scenario `s` is a set `F_s` of distributors that cannot deliver over
the sourcing horizon, drawn by independent Bernoulli trials — the same percolation
structure `graph/simulation.py` already uses, but with calibrated probabilities (§2).

**Second stage** (recourse, after `F_s` is observed):

| Variable | Domain | Meaning |
|---|---|---|
| `r[c,d,s]` | ℤ₊ | emergency units of `c` re-procured from surviving `d` |
| `u[c,s]` | ℤ₊ | units that cannot be re-procured at all |
| `e[d,s]` | {0,1} | an expedited consignment is raised on `d` in scenario `s` |

```
Σ_{d∉F_s} r[c,d,s] + u[c,s] = Σ_{d∈F_s} q[c,d]     cover the gap
r[c,d,s] + q[c,d] ≤ stock[c,d]                     emergency draws on RESIDUAL stock
Σ_c r[c,d,s] ≤ cap_d · e[d,s]                      an expedited consignment, or none
```

Scenario cost:

```
C_s = F
    + Σ_{c, d∉F_s} (price[c,d]·(1+π) + air_per_unit)·r[c,d,s]     emergency purchase
    + Σ_{d∉F_s} expedite_fixed·e[d,s]                             air consignment minimum
    + Σ_c unmet_unit[c]·u[c,s]                                    lost-sales penalty
    − Σ_{c, d∈F_s} (recovery·price[c,d] + per_unit[d])·q[c,d]     goods never delivered
```

**Objective.** `min (1−λ)·E[cost] + λ·CVaR_α[cost]`, with CVaR linearized by
Rockafellar & Uryasev (2000):

```
CVaR_α(Z) = min_η { η + 1/(1−α) · E[(Z − η)⁺] }
  ⟹  z_s ≥ R_s − η,  z_s ≥ 0,  η free
```

Everything stays linear, so **CP-SAT solves it exactly** — no piecewise approximation,
no quadratic term, no separate risk solver. Written out with integer coefficients (the
whole objective is scaled by `LAMBDA_DEN · W`, where `W = Σ_s w_s` is the total scenario
weight, then divided by the GCD of its three outer multipliers):

```
minimize   w_first·W·F
         + w_mean ·Σ_s w_s·R_s
         + w_cvar ·( W·η + ⌈1/(1−α)⌉·Σ_s w_s·z_s )
```

**Where the integer weights `w_s` come from, and why it matters.** On the *sampled* path
they are Monte Carlo draw counts and `W = n_draws`. On the *enumerated* path they are the
true probabilities quantized to a common denominator, `w_s = round(p_s · W)`, with `W`
chosen as large as the int64 objective ceiling permits. Both are exact integer weightings
of a discrete measure; only the sampled one carries sampling error. This is what allows an
enumerated support to be **optimized on** and not merely scored on — see §3.

Two implementation notes that matter:

* **Rockafellar–Uryasev is applied to the recourse cost `R_s = C_s − F`, not to `C_s`.**
  Both `E[·]` and `CVaR[·]` are translation invariant, so this is exactly equivalent —
  but the model never materialises 150+ copies of `F`'s ~80-term expression inside one
  equality constraint per scenario. On a 157-scenario instance that reformulation took
  the λ = 0 solve from a **60 s timeout at a 1.7% gap to sub-second OPTIMAL** — a timing on
  the machine in [Provenance](#provenance) (§0); the reformulation's *direction* is what
  transfers, not the seconds.
* **The CVaR block is only built when λ > 0.** At λ = 0 it carries zero objective weight,
  so `η` and every `z_s` become free variables in a large integer domain that CP-SAT must
  still search. Omitting it is not an approximation — at λ = 0 the problem *is*
  `min E[cost]` — and CVaR is still reported, computed post hoc.

### Why CVaR, and one thing worth knowing about it

CVaR-95 is the mean cost of the worst 5% of scenarios. Variance penalises upside as well
as downside and is quadratic (CP-SAT cannot take it). CVaR is **coherent** — monotone,
subadditive, positively homogeneous, translation invariant — which VaR is not (Artzner,
Delbaen, Eber & Heath 1999, *Mathematical Finance* 9(3):203–228). Subadditivity is the
practical one here: the model cannot be made to look safer by splitting one BOM in two.

More useful still, **CVaR is already a distributionally robust objective.** It has an
exact dual representation as a worst-case expectation over an ambiguity set:

```
CVaR_α(Z) = sup { E_Q[Z] : Q ≪ P,  dQ/dP ≤ 1/(1−α) }
```

— the highest expected cost achievable by *any* re-weighting of the assumed measure `P`
bounded by a likelihood ratio of `1/(1−α)` (Rockafellar & Uryasev 2002, *J. Banking &
Finance* 26(7):1443–1471). So minimising CVaR-95 is solving a DRO problem whose ambiguity
set is every scenario re-weighting up to **20×**.

That matters *specifically because the probabilities below are assumed rather than
measured*: the λ > 0 end of this frontier is already hedged against getting them wrong by
up to 20× on any single scenario. It does not excuse the assumption — the sensitivity
sweep in §6 still has to be run — but the risk-averse end degrades gracefully under
probability misspecification in a way the risk-neutral end does not.

---

## 2. The uncalibrated-probability problem, and what was done about it

**This is the part that would have made everything else meaningless, so it is first.**

Until 2026-08-16, `backend/app/graph/simulation.py` did this:

```python
failure_probs = {
    did: (1.0 if did in forced
          else min(betweenness.get(did, 0.0) * stress_factor, 1.0))
    for did in all_dist_ids
}
```

and `betweenness` was **min-max rescaled to [0,1]** on top of networkx's own
normalization in `graph/builder.py`. A min-max rescale always attains 1.0 at its
maximum. So *by construction*:

* the single most central distributor in this database (id 28) **failed in 100% of
  scenarios**;
* the 18 distributors sitting at a rescaled betweenness of 0.0 **never failed at all**;
* there was no base rate, no exposure window, and no unit anywhere in that expression — a
  centrality *rank* was being read as a *probability*.

Downstream, `cvar_95` therefore pinned at `1.0 + EMERGENCY_COST_PREMIUM = 1.15` in nearly
every row of `BENCHMARK_RESULTS.md`: a constant wearing a Monte Carlo costume. **A CVaR
objective built on those probabilities would be meaningless**, so this work never reused
them.

> **Status note, 2026-08-16, amended 2026-08-28.** Separate work removed the min-max
> rescale from `graph/builder.py` and pointed `graph/simulation.py` at the very
> `build_failure_probabilities` described below, so **the probability defect no longer
> ships**.
>
> **But the saturation it caused was NOT fixed by that, and this note previously implied
> it was.** Measured by exact out-of-tree replay of run 5 (generated 2026-08-27, all 36
> rows reproduced bit-for-bit): **16 of 18 published rows still have the blind arm pinned
> at exactly 1.15**, and **10 of 18 are a bit-identical tie between the two arms**. The
> ceiling is structural, not probabilistic — `inflation = 1 + (unfulfilled/n_lines) x 0.15`
> tops out at `1.15` whenever every line of a 4-line BOM is unsupplied, whatever the
> failure probabilities are. Calibrating the probabilities could never have removed it.
>
> `run_monte_carlo` now also reports `p_shortfall`, `p_total_shortfall`,
> `cvar_95_ceiling` and `cvar_95_saturated`. `p_total_shortfall` is the measure that
> keeps resolving: it separates **8 of the 10** ceiling ties. Re-basing CVaR onto the
> shortfall share would NOT help — the inflation map is exactly affine, so
> `CVaR(share) == (cvar_95 - 1)/premium` (verified: 0 of 36 rows deviate), and a test
> exists to stop that being attempted. The
> section is kept in the past tense rather than deleted: it is the reason this model
> calibrates its own probabilities instead of inheriting them, and the argument does not
> survive being quietly tidied away. One consequence for *this* document is that the
> "legacy `p_fail`" column that used to sit beside the calibrated one can no longer be
> reproduced from live code — the rescaled betweenness it was read off no longer exists —
> so the table below reports the betweenness and the calibrated probability the committed
> artifact actually contains, and nothing it cannot substantiate.

### The replacement

`build_failure_probabilities` separates the two things the old expression conflated —
the **level** of risk and its **shape** across suppliers:

```
p_d = min( p_base · spread^(2·u_d − 1),  MAX_FAILURE_PROB )

  p_base = 1 − (1 − base_annual_prob)^(horizon_days / 365)     ← the LEVEL, cited
  u_d    = percentile rank of distributor d's betweenness      ← the SHAPE, bounded
```

**Level — cited.** McKinsey Global Institute, *"Risk, resilience, and rebalancing in
global value chains"* (August 2020), verified 2026-08-15:

> "companies can now expect supply chain disruptions lasting a month or longer to occur
> every 3.7 years"

Treated as a Poisson rate: `λ = 1/3.7` per year ⟹ `P(≥1 event in a year) = 1 − e^(−1/3.7)
= 0.2368`. Over a 60-day purchase-order window that is **4.34%**.

**The honest caveat, stated in the code, the artifact and the API response:** that figure
is **firm-level** (a company sees a disruption *somewhere* in its value chain), not a
per-supplier failure rate. Using it per supplier almost certainly **overstates** individual
supplier risk. No per-supplier base rate could be verified from a citable public source.
It is therefore treated as an **assumption and swept from 5% to 40%** (§6), never
published as if it were measured.

**Shape — a bounded rank transform, not a magnitude.** Betweenness in this network is
pathologically skewed, with a long tail of distributors at or near zero. Multiplying a
base rate by that raw score would hand the hub an order-of-magnitude multiplier and
reinstate the very failure being fixed. Instead centrality only **rank-orders** relative
risk inside a bounded spread: the most central supplier gets `spread ×` the base rate,
the least central `1/spread ×`, and **the median supplier sits exactly on the cited base
rate**. Ties share the mean rank, so suppliers with identical betweenness all receive the
identical probability rather than an arbitrary ordering artefact. This is also why the
**`p_fail` column** below is unchanged by the `graph/builder.py` **normalization** fix: a
rank transform is invariant to any monotone rescaling of its input.

**Be precise about what that invariance does and does not cover.** On 2026-09-03 a second,
*structural* fix landed in the same file — a dead 20% holdout carve was removed, so the
graph is now built from all 8,176 offer rows instead of 6,602. That is not a rescale, and
the **Betweenness column below moved with it**: distributor 28 went 0.245752 → **0.291485**,
56 went 0.183436 → **0.154494**, 9 went 0.124957 → **0.116426**, 85 went 0.099427 →
**0.102407**, 81 went 0.025237 → **0.014208**, 70 went 0.017099 → **0.009876**. The
`p_fail` column survived intact only because the *rank order* of these six distributors
happened to be unchanged — it is not a guarantee. In the wider supplier pools of the
`breadth` arm the ordering did shift, which is why several breadth rows below now report
different scenario sets, costs and tradeoff verdicts than the 2026-09-01 vintage did.

**And the residual assumption is named, not hidden:** that more central suppliers are more
likely to be disrupted *at all*. Nothing in this repo or in the cited literature
establishes it, and the opposite is arguable — hub distributors are typically better
capitalised and more redundant than small ones. So `centrality_spread = 1.0` — centrality
ignored entirely, every supplier on the flat base rate — is a **supported setting, a
sensitivity arm run in every published frontier, and a parameter on the public API**.

### The result, for the headline BOM's six suppliers

<!-- GENERATED:calibration_table:BEGIN -->

| Distributor | Betweenness | **Calibrated `p_fail`** (60-day) |
|---:|---:|---:|
| 28 | 0.291485 | **0.1304** |
| 56 | 0.154494 | **0.0840** |
| 9 | 0.116426 | **0.0541** |
| 85 | 0.102407 | **0.0349** |
| 81 | 0.014208 | **0.0225** |
| 70 | 0.009876 | **0.0145** |

Base rate 0.236827 annual over 60 days, centrality spread 3.0, capped at `MAX_FAILURE_PROB` = 0.5. **No supplier is anywhere near probability 1.0** — which is the whole point. The resulting scenario set has P(no disruption) = 0.69 and 0.335 expected failures per scenario over 6 distributors.

<!-- GENERATED:calibration_table:END -->

`GET /api/v1/stochastic/calibration` publishes the same per-distributor figures live, so
the calibration is auditable rather than asserted.

---

## 3. Scenario support: why "n_distinct = 10" was the right thing to worry about, and the wrong thing to fix

A first draft of this artifact reported `n_draws = 200, n_distinct = 10, α = 0.95`. That
invites a fair objection: **0.05 × 10 = 0.5 distinct scenarios land in the tail**, so the
CVaR estimate is one scenario wide and reports solver precision, not statistical
precision.

The objection is correct about those numbers. The diagnosis, though, is more interesting
than "take more samples":

> Disruption is `|D|` **independent Bernoulli variables**, so the cost distribution has at
> most **2^|D| atoms in total**. The headline BOM is supplied by **six** distributors.
> Its entire support is **64 atoms** — of which 29 carry probability ≥ 1e-4 and the top 20
> carry **99.70%** of the mass. Drawing 200 samples from a 64-atom distribution recovers
> ~10 of them and adds nothing that enumerating all 64 gives exactly.

So the fix is not more Monte Carlo. **It is to stop sampling.** `enumerate_scenarios()`
holds the entire support with exact probabilities, and every expected cost and CVaR
published below carries **no sampling error at all**.

**And the same is now true of the plan itself.** Until 2026-08-27 only half of that
sentence was earned: the support was enumerated for *scoring* while the plan was still
*chosen* on the 200 draws. The optimizer therefore minimized a measure that resolved 10 of
the 64 atoms — a 95% tail **four atoms wide against an exact 49–54** — and this document
published the result against all 64. The asymmetry was not free: it put a **dominated
point on the published frontier** (§4). `fit_scenario_set` now hands CP-SAT the enumerated
support whenever its second stage fits the solver's variable budget, so choice and score
read the same measure and there is no SAA optimality gap left to bound on this instance.
It is the same function `app/api/stochastic.py` serves the live endpoint from, so the API
and this artifact describe one solver rather than two.

**What "complete support" does and does not claim.** CP-SAT needs integer objective
weights, so the exact probabilities are scaled by a denominator chosen *per solve* from
what the int64 ceiling can carry (§1). Atoms below that resolution carry no weight. So
"the solve set is the complete support" is true; "every atom carries weight at every λ" is
**not** — at the coarsest λ on the grid, 35 of the 64 atoms are weighted. That residual is
published per point as `solve_residual_mass` and per sweep beneath the §4 table. It is a
**deterministic rounding artefact of the quantization, not sampling error**: it has no
confidence interval, it is not an estimate of anything, and it does not shrink with more
draws.

<!-- GENERATED:exact_vs_saa_table:BEGIN -->

| | SAA, 200 draws | **Exact, 64 atoms** |
|---|---:|---:|
| Atoms in the α = 0.95 tail | 4 | **50–54** |
| CVaR-95 at λ = 0 | $227,977 | **$224,600** |
| CVaR-95 at λ = 1 | $213,046 | **$214,747** |
| CVaR-95 sampling error | **-0.79% … +1.50%** | — (none) |
| Residual probability mass | — | **0.0** |

The sampled tail was not merely thin, it was **biased by up to 1.50%** — in both directions, depending on λ. That is a real error, it was invisible without the exact computation, and it is now gone.

<!-- GENERATED:exact_vs_saa_table:END -->

**A note the objection actually strengthens:** the betweenness-as-probability defect (§2)
made scenario diversity *worse*, not better. With `p_fail = 1.0` for the most central
distributor, that supplier failed in every scenario and stopped being a source of
variation at all — mechanically collapsing the number of distinct outcomes. This model
never inherited those probabilities, and as of 2026-08-16 they are fixed at the source
too.

**Where enumeration is not possible.** `iot_sensor_node` draws on 26 distributors →
2^26 = 67,108,864 atoms. Above `MAX_ENUMERABLE_DISTRIBUTORS = 18` the model falls back to
sampling and bounds the residual error statistically instead (§5). Which mode each result
used is recorded per point as `evaluation_kind`.

---

## 4. The frontier

**`pcb_power_supply` at 10,000× volume — 60,000 units, `balanced` strategy, `us_only=False`,
depot San Francisco (37.7749 / −122.4194), scored on the exact 64-atom support.**

> **The depot is load-bearing, not scenery.** It sets every distributor's
> `dist_km_from_depot`, which sets the freight model, which changes the optimum — not
> just the freight line. The same BOM at the same volume against the Memphis reference
> hub (35.1495 / −90.0490, which `POST /stochastic/frontier` uses by default) gives
> E = $147,272 and **four** suppliers at λ = 0, against the $182,256 and six below.
> The endpoint therefore echoes the depot it used in `instance.depot_lat/depot_lng` and
> accepts it as a request parameter; pass San Francisco to reproduce this table.

### ⚠️ In the table below, only the `Solve` column is a stopwatch reading on one machine (§0). `Status` and `Gap` are **not** — they are what an 80-unit deterministic work budget closed, and they reproduce under any CPU load.

**`E[cost]`, `CVaR-95`, `Tail premium`, `Suppliers`, `Atoms in tail` and the CVaR-80/90/95/98
table reproduce too.** Those are the frontier; they were never machine-dependent.

<!-- GENERATED:frontier_table:BEGIN -->

| λ | E[cost] | CVaR-95 | Tail premium | Suppliers | Atoms in tail | Status | Gap | Solve | On frontier |
|---:|---:|---:|---:|:---:|---:|:---|---:|---:|:---:|
| 0.00 | $182,256 | $224,600 | $42,344 | 6 | 50 | OPTIMAL | 0.000% | 0.732 s | yes |
| 0.05 | $182,256 | $224,600 | $42,344 | 6 | 50 | OPTIMAL | 0.000% | 0.060 s | yes |
| 0.10 | $182,256 | $224,600 | $42,344 | 6 | 50 | OPTIMAL | 0.000% | 0.074 s | yes |
| 0.20 | $183,171 | $219,128 | $35,958 | 5 | 51 | OPTIMAL | 0.000% | 0.178 s | yes |
| **0.30** | **$184,300** | **$215,882** | **$31,582** | **4** | 53 | OPTIMAL | 0.000% | 0.095 s | yes ← **knee** |
| 0.50 | $184,300 | $215,882 | $31,582 | 4 | 53 | OPTIMAL | 0.000% | 1.165 s | yes |
| 0.70 | $184,702 | $215,639 | $30,937 | 4 | 54 | OPTIMAL | 0.000% | 0.438 s | yes |
| 0.85 | $187,077 | $214,747 | $27,670 | 4 | 50 | OPTIMAL | 0.000% | 0.467 s | yes |
| 1.00 | $187,077 | $214,747 | $27,670 | 4 | 50 | OPTIMAL | 0.000% | 1.483 s | yes |

CVaR is also reported at other tail levels, because a single α is not enough to read a tail:

| λ | CVaR-80 | CVaR-90 | CVaR-95 | CVaR-98 |
|---:|---:|---:|---:|---:|
| 0.00 | $192,985 | $203,808 | $224,600 | $266,764 |
| **0.30** | **$191,212** | **$199,693** | **$215,882** | **$246,381** |
| 1.00 | $193,099 | $200,481 | $214,747 | $242,683 |

*Solve quality for this sweep: 9 of 9 λ points converged, worst MIP gap 0.000%, statuses `OPTIMAL`, per-solve budget 80-unit deterministic-time budget (1600s wall-clock runaway guard).*

*Solved on the **complete 64-atom support** with exact probability weights — the same measure these points are scored on, so there is no sampling error anywhere in this table and no SAA optimality gap to bound. CP-SAT's integer objective weights are those probabilities scaled by a common denominator (smallest on this sweep: 12,714); atoms whose probability falls below that resolution carry no weight, and that mass is 2.45e-04 at the worst point on the grid. It is a deterministic rounding artefact of the quantization — not sampling error: it has no confidence interval and does not shrink with more draws. Published per point as `solve_residual_mass`.*

<!-- GENERATED:frontier_table:END -->

### λ = 1.00 used to be dominated — that was a solver artefact, and it is gone

This section previously read *"λ = 1.00 is dominated, and that is expected"*. It was not
expected, and calling it expected was the mistake.

The published λ = 1.00 point landed at CVaR **$215,171 — worse than λ = 0.70's $214,747 —
while paying $1,409 more in expectation.** It was dominated on *both* axes. At λ = 1 the
objective is CVaR alone, so any plan attaining the minimum CVaR is optimal and expected
cost is broken arbitrarily — which explains the *expected-cost* half. It does not explain
the CVaR half: a point that is beaten on the very quantity it exclusively minimizes has
not been optimized, it has been mis-measured. The cause was §3's asymmetry. The plan was
chosen against 10 sampled atoms and then scored against all 64, and on the true measure
the choice was simply wrong.

Choosing on the enumerated support removes it. **λ = 1.00 now attains the frontier's
minimum CVaR and no point on the sweep is flagged `dominated`.** A dominated point is
still *reported* rather than deleted if one ever appears again — the field stays — but it
is now treated as the diagnosis it is.

The genuine limitation of a **weighted-sum scalarization** is separate and still stands:
sweeping λ can only recover Pareto points on the **convex hull** of the (E, CVaR) image.
Integer programs routinely have *unsupported* efficient points that no λ exposes, so
**this frontier is a subset of the true efficient set, never a superset.** An ε-constraint
sweep would find the rest; it is not implemented.

---

## 5. The knee, and the recommendation it implies

<!-- GENERATED:knee_table:BEGIN -->

**Knee: λ = 0.3**, found by maximum perpendicular distance to the chord joining the extreme non-dominated points (the Kneedle / L-method criterion, Satopää et al. 2011), on min-max normalized axes so the answer does not depend on the currency unit — and computed on the **9 converged points only** (0 excluded).

| | Before the knee (λ 0 → 0.3) | Beyond the knee (λ 0.3 → 1) |
|---|---:|---:|
| Extra expected cost | **+$2,044** (+1.12%) | +$2,777 |
| CVaR-95 reduction | **−$8,719** (−3.88%) | −$1,135 |
| **$ of tail removed per $ spent** | **4.27** | 0.41 |

> **Recommendation.** Source this BOM at **λ = 0.3**: 4 suppliers (9, 70, 81, 85) rather than the risk-neutral 6. It costs **$2,044 more per 60,000-unit build in expectation — 1.12% of spend — and removes $8,719 of CVaR-95 exposure.** Every dollar of that premium buys **$4.27** of tail reduction. Past the knee the same dollar buys **$0.41**. Stop at the knee.

<!-- GENERATED:knee_table:END -->

### What is actually in the tail — and what the knee changes

<!-- GENERATED:tail_table:BEGIN -->

The α = 0.95 tail is not diffuse. **32% of it is one event: distributor 81 going dark.**

| Failed | Probability | Share of tail | Cost at λ=0 | **Cost at knee** | Emergency units (λ=0 → knee) | Unmet units (λ=0 → knee) |
|---|---:|---:|---:|---:|---:|---:|
| {81} | 1.61% | **32.2%** | $251,162 | **$227,648** | 167 → **37,356** | 4,290 → 4,288 |
| {85} | 2.53% | **23.8%** | $183,307 | **$183,742** | 10,847 → **10,847** | 0 → 0 |
| {70} | 1.03% | **20.6%** | $198,441 | **$198,875** | 4,858 → **4,860** | 978 → 978 |
| {28, 81} | 0.24% | **4.8%** | $275,597 | **$257,651** | 0 → **28,561** | 13,085 → 13,083 |

<!-- GENERATED:tail_table:END -->

**The mechanism, in one sentence:** the risk-neutral plan concentrates volume on
distributor 81 (cheap, deep stock, and a *low* disruption probability of 2.25%), so when
81 does fail there is not enough residual stock anywhere else and **4,290 units are simply
written off at the stockout penalty**. The knee plan deliberately leaves headroom at
surviving suppliers, so the same outage is covered by **37,356 emergency units instead of
167** — converting a write-off into an expensive-but-executable recovery, and taking
$23,514 out of the single worst-contributing scenario.

That is a genuinely non-obvious result: **the supplier driving the tail is not the one
with the highest failure probability.** Distributor 28 has the highest `p_fail` (13.04%)
and contributes only 4.8% of the tail; distributor 81 has one of the lowest (2.25%) and
contributes 32.2%. Concentration, not probability, is what makes the tail. A surcharge
proportional to centrality — which is what the code did before — gets this exactly
backwards.

### What the frontier is worth against what the repo did before

Every baseline is scored on the identical exact measure, and dominance is tested against
the converged frontier points only.

<!-- GENERATED:baselines_table:BEGIN -->

| Plan | E[cost] | CVaR-95 | Suppliers | Dominated by any λ? | Sits at λ ≈ |
|---|---:|---:|:---:|:---:|:---:|
| Mean-value (disruptions assumed away) | $182,932 | $220,085 | 6 | no | **0.2** |
| **Shipped MILP** (`sourcing.py`, heuristic surcharges live) | $182,932 | $220,085 | 6 | no | **0.2** |
| **Shipped MILP**, graph-aware (`sourcing.py`, betweenness term on) | $182,932 | $220,085 | 6 | no | **0.2** |
| Stochastic, λ = 0 (risk-neutral) | $182,256 | $224,600 | 6 | — | — |
| **Stochastic, λ = 0.3 (knee)** | **$184,300** | **$215,882** | **4** | — | — |

**Value of the stochastic solution: VSS = EEV − RP = $676 (0.37% of RP).** Ignoring uncertainty at plan time costs 0.37% *in expectation*; the deterministic plan is very nearly the risk-neutral optimum. **The value of this model is not in expected cost. It is entirely in the tail**, where the same comparison is $220,085 → $215,882.

<!-- GENERATED:baselines_table:END -->

That the VSS is **small** is the point, not an embarrassment: it says the deterministic
optimizer was already close to the risk-neutral optimum, and that everything this model
adds, it adds in the tail.

And the most useful honest finding here: **the shipped 15% heuristic surcharge is not
wrong.** It is not dominated by any point on the frontier — it lands *on* the curve, at
about λ ≈ 0.2 (`baselines[].nearest_lambda` in the artifact, and the `Sits at λ ≈` column
of the generated table above). What it cannot do is **tell you that**, or let you move.
The surcharge
encodes one unlabelled risk appetite chosen by a constant in a source file; the frontier
makes the appetite an explicit, auditable, movable dial and shows what each setting costs.
That is the whole argument, and it is a smaller and more defensible claim than "the
heuristic was wrong".

### The tradeoff only exists at volume

<!-- GENERATED:volume_table:BEGIN -->

| Volume | Units | Knee | VSS | λ points converged |
|---|---:|:---:|---:|:---:|
| 100× | 600 | **none** | $0.00 (0.00%) | 9/9 |
| 1,000× | 6,000 | **none** | $15.91 (0.10%) | 9/9 |
| 10,000× | 60,000 | **λ = 0.3** | $676 (0.37%) | 9/9 |

<!-- GENERATED:volume_table:END -->

At prototype and low-production volume there is **no cost-vs-CVaR tradeoff on this BOM at
all** — every λ returns the same plan. This is consistent with
[`BENCHMARK_VOLUME_CURVE.md`](BENCHMARK_VOLUME_CURVE.md): at low volume the fixed
per-supplier charge dominates everything, so the sourcing decision is fully determined by
fee arithmetic and there is no room left for risk to move it. The frontier is a
**production-volume instrument**, and pretending otherwise would be the same mistake as
the 44.7% headline.

---

## 6. Is any of this robust to the probabilities being wrong?

The disruption probabilities are an **assumption** (§2), so the deliverable is not a point
estimate of the knee — it is the range the knee moves over when the assumption is flexed.
The grid is `base_annual_prob ∈ {5%, 10%, 23.68%, 40%} × centrality_spread ∈ {1.0, 3.0,
6.0} × horizon_days ∈ {30, 60, 120}`, each cell a full λ sweep on the headline instance.

The arm that matters is `centrality_spread = 1.0`: centrality removed from the model
entirely, every supplier on the flat cited base rate. If the recommendation survives that,
it is being driven by the cost and stock data rather than by the graph assumption.

### ✅ In the table below, the `all λ converged` column — and the "36 of 36 sweeps had every λ point converge" line — are deterministic work-budget measurements and reproduce under any CPU load (§0). They were load-dependent before 2026-09-01.

**Every other column always reproduced:** `knee λ`, `knee suppliers`, `extra E[cost]`,
`CVaR-95 reduction` and `CVaR reduction available` are the sensitivity result itself, and
they did not move across any re-run, on either budget kind.

<!-- GENERATED:sensitivity:BEGIN -->

**36 full frontier sweeps** on the headline instance (`pcb_power_supply` ×10,000), over `base_annual_prob` × `centrality_spread` × `horizon_days`.

* A knee exists in **35 of 36** cells; the knee λ takes the values 0.25, 0.5.
* In the **centrality-ignored arm** (`centrality_spread = 1.0`, 12 cells — every supplier on the flat cited base rate), a knee exists in **12** of them.
* 36 of 36 sweeps had every λ point converge; **0** did not and their aggregates are built on the converged subset.

| base rate | spread | horizon | p_median | atoms solved | knee λ | knee suppliers | extra E[cost] | CVaR-95 reduction | CVaR reduction available | all λ converged |
|---:|---:|---:|---:|---:|:---:|:---:|---:|---:|---:|:---:|
| 5.00% | 1 | 30 d | 0.42% | 64 | 0.25 | 5 | 0.01% | 0.11% | 0.20% | yes |
| 5.00% | 1 | 60 d | 0.84% | 64 | 0.25 | 5 | 0.00% | 0.22% | 0.72% | yes |
| 5.00% | 1 | 120 d | 1.67% | 64 | 0.5 | 4 | 0.34% | 1.63% | 1.88% | yes |
| 5.00% | 3 | 30 d | 0.52% | 64 | **none** | — | — | — | 0.05% | yes |
| 5.00% | 3 | 60 d | 1.05% | 64 | 0.25 | 5 | 0.04% | 0.54% | 0.63% | yes |
| 5.00% | 3 | 120 d | 2.08% | 64 | 0.25 | 5 | 0.12% | 0.95% | 1.48% | yes |
| 5.00% | 6 | 30 d | 0.60% | 64 | 0.25 | 6 | 0.00% | 0.18% | 0.21% | yes |
| 5.00% | 6 | 60 d | 1.20% | 64 | 0.25 | 6 | 0.06% | 0.45% | 0.52% | yes |
| 5.00% | 6 | 120 d | 2.39% | 64 | 0.5 | 6 | 0.25% | 0.73% | 1.03% | yes |
| 10.00% | 1 | 30 d | 0.86% | 64 | 0.25 | 5 | 0.00% | 0.22% | 0.75% | yes |
| 10.00% | 1 | 60 d | 1.72% | 64 | 0.5 | 4 | 0.34% | 1.68% | 1.96% | yes |
| 10.00% | 1 | 120 d | 3.40% | 64 | 0.25 | 4 | 0.32% | 2.48% | 3.62% | yes |
| 10.00% | 3 | 30 d | 1.07% | 64 | 0.25 | 5 | 0.04% | 0.55% | 0.66% | yes |
| 10.00% | 3 | 60 d | 2.14% | 64 | 0.25 | 5 | 0.13% | 1.27% | 1.82% | yes |
| 10.00% | 3 | 120 d | 4.24% | 64 | 0.5 | 4 | 0.93% | 3.27% | 3.42% | yes |
| 10.00% | 6 | 30 d | 1.23% | 64 | 0.25 | 6 | 0.07% | 0.46% | 0.53% | yes |
| 10.00% | 6 | 60 d | 2.46% | 64 | 0.5 | 6 | 0.26% | 0.74% | 1.06% | yes |
| 10.00% | 6 | 120 d | 4.87% | 64 | 0.5 | 6 | 0.65% | 1.16% | 2.07% | yes |
| 23.68% | 1 | 30 d | 2.20% | 64 | 0.25 | 4 | 0.34% | 2.13% | 2.82% | yes |
| 23.68% | 1 | 60 d | 4.35% | 64 | 0.25 | 4 | 0.32% | 3.03% | 4.45% | yes |
| 23.68% | 1 | 120 d | 8.50% | 64 | 0.25 | 4 | 0.28% | 2.29% | 3.36% | yes |
| 23.68% | 3 | 30 d | 2.74% | 64 | 0.25 | 5 | 0.19% | 1.55% | 2.31% | yes |
| 23.68% | 3 | 60 d | 5.41% | 64 | 0.5 | 4 | 1.12% | 3.88% | 4.39% | yes |
| 23.68% | 3 | 120 d | 10.59% | 64 | 0.25 | 4 | 1.96% | 5.96% | 7.27% | yes |
| 23.68% | 6 | 30 d | 3.14% | 64 | 0.5 | 6 | 0.37% | 0.88% | 1.40% | yes |
| 23.68% | 6 | 60 d | 6.22% | 64 | 0.5 | 5 | 1.71% | 2.15% | 2.45% | yes |
| 23.68% | 6 | 120 d | 12.17% | 64 | 0.5 | 5 | 2.41% | 2.57% | 3.74% | yes |
| 40.00% | 1 | 30 d | 4.11% | 64 | 0.25 | 4 | 0.32% | 2.90% | 4.24% | yes |
| 40.00% | 1 | 60 d | 8.05% | 64 | 0.25 | 4 | 0.28% | 2.43% | 3.56% | yes |
| 40.00% | 1 | 120 d | 15.46% | 64 | 0.25 | 4 | 0.22% | 1.18% | 1.73% | yes |
| 40.00% | 3 | 30 d | 5.12% | 64 | 0.5 | 4 | 1.07% | 3.74% | 4.16% | yes |
| 40.00% | 3 | 60 d | 10.03% | 64 | 0.25 | 5 | 1.34% | 4.24% | 6.04% | yes |
| 40.00% | 3 | 120 d | 19.26% | 64 | 0.5 | 5 | 3.34% | 3.06% | 3.63% | yes |
| 40.00% | 6 | 30 d | 5.88% | 64 | 0.5 | 5 | 1.63% | 2.07% | 2.36% | yes |
| 40.00% | 6 | 60 d | 11.53% | 64 | 0.5 | 5 | 2.29% | 2.54% | 3.65% | yes |
| 40.00% | 6 | 120 d | 22.12% | 64 | 0.5 | 5 | 5.36% | 3.77% | 4.55% | yes |

<!-- GENERATED:sensitivity:END -->

---

## 7. SAA solution quality — the arm that is sampled on purpose

**This section no longer describes how the headline frontier is produced.** As of
2026-08-27 the primary, breadth and sensitivity arms choose on the enumerated support
wherever it fits the solver's variable budget (§3), so on the headline instance there is
no sampling error left in the choice and nothing here to bound. What this section bounds
is the **SAA fallback path** — the route taken by supplier pools too wide to enumerate,
which is most of §8.

It is also the one arm that still hands CP-SAT Monte Carlo draws, and that is deliberate.
Both of its experiments *measure what sampling costs*. The Mak–Morton–Wood lower bound
**is** the mean optimal value of M independent SAA replications: enumerate it and every
replication becomes the same solve, the variance is zero, and the reported gap is zero by
construction rather than by measurement. The endpoint-stability table sweeps N and the
seed precisely to expose the wobble; enumerate it and there is no wobble left to show.
A vacuous zero is not a stronger result than a measured one — it is a deleted experiment.

On the sampled path, the residual choice error is bounded the standard way:

* **Lower bound** — mean optimal value of **M = 12 independent SAA replications** at
  sample size N. Each solve optimizes against its own sample, so its optimal value is
  optimistically biased: `E[v_N] ≤ v*`. Reported with a one-sided Student-t confidence
  limit over the M replicates. *Mak, Morton & Wood (1999), "Monte Carlo bounding
  techniques for determining solution quality in stochastic programs", Operations Research
  Letters 24(1–2):47–56, doi:10.1016/S0167-6377(98)00054-6.*
* **Upper bound** — the true objective of the best candidate first-stage plan on the exact
  enumerated measure. Any feasible plan bounds the optimum from above, and with the exact
  support as reference **this is not an estimate at all**.
* **Gap** — swept over N ∈ {25, 50, 100, 200, 400}. Where it flattens is the sample size
  that is actually justified.

*Kleywegt, Shapiro & Homem-de-Mello (2002), "The Sample Average Approximation Method for
Stochastic Discrete Optimization", SIAM J. Optimization 12(2):479–502* is the convergence
result for SAA with discrete first-stage decisions, which is exactly this model.

A **small negative gap point-estimate is normal** and is not a broken bound: `E[v_N] ≤ v*`
is an expectation, and with finitely many replications the sample mean can land slightly
above the candidate plan's true value. That is the signal "the remaining gap is smaller
than the Monte Carlo noise in my estimate of it". The statement that must hold is the
interval one: `upper_bound ≥ lower_bound_ci_low`.

### ⚠️ In the tables below, the `Wall` column is a stopwatch reading on one machine (§0). The `all λ converged` column is **not** — it is a deterministic work-budget measurement and reproduces under any CPU load.

**The bounds reproduce too.** Lower bound, CI, upper bound, gap, gap %, and every cost and
CVaR figure in both tables are computed on the exact enumerated measure and reproduce
anywhere.

<!-- GENERATED:saa_quality:BEGIN -->

Reference measure: **exact**.

| N | λ | Lower bound (mean of M) | LB 95% CI low | Upper bound | Gap | Gap 95% CI high | Gap % | Wall |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 25 | 0 | $183,030 | $181,588 | $182,256 | −$775 | $668 | -0.425% | 0.3 s |
| 25 | 0.5 | $201,933 | $191,088 | $200,091 | −$1,842 | $9,003 | -0.921% | 0.3 s |
| 25 | 1 | $218,244 | $198,722 | $214,748 | −$3,496 | $16,026 | -1.628% | 0.3 s |
| 50 | 0 | $182,643 | $181,662 | $182,256 | −$387 | $594 | -0.212% | 0.3 s |
| 50 | 0.5 | $199,966 | $192,606 | $200,091 | $124 | $7,485 | 0.062% | 0.3 s |
| 50 | 1 | $214,763 | $201,765 | $214,747 | −$15.91 | $12,981 | -0.007% | 0.3 s |
| 100 | 0 | $182,425 | $181,604 | $182,256 | −$169 | $652 | -0.093% | 0.5 s |
| 100 | 0.5 | $197,777 | $193,142 | $200,091 | $2,313 | $6,948 | 1.156% | 0.5 s |
| 100 | 1 | $210,286 | $202,118 | $214,747 | $4,460 | $12,629 | 2.077% | 0.5 s |
| 200 | 0 | $182,417 | $181,931 | $182,256 | −$161 | $325 | -0.088% | 1.2 s |
| 200 | 0.5 | $200,393 | $197,877 | $200,091 | −$303 | $2,214 | -0.151% | 0.8 s |
| 200 | 1 | $215,109 | $210,853 | $214,747 | −$362 | $3,894 | -0.169% | 0.6 s |
| 400 | 0 | $182,249 | $181,909 | $182,256 | $6.65 | $347 | 0.004% | 3.0 s |
| 400 | 0.5 | $199,807 | $197,013 | $200,091 | $283 | $3,078 | 0.142% | 1.5 s |
| 400 | 1 | $214,230 | $209,374 | $214,747 | $516 | $5,373 | 0.240% | 0.9 s |

The interval statement that must hold — `upper_bound ≥ lower_bound_ci_low` — holds in **15 of 15** cells.

**Endpoint stability** over N ∈ [50, 100, 200, 400, 800] × seed ∈ [42, 1337, 2718] (15 sweeps): the risk-neutral expected cost spans $182,256 – $182,723 (0.26% of the low), and the minimum CVaR-95 spans $214,747 – $215,746 (0.47%).

| N draws | seed | distinct scenarios | risk-neutral E | risk-neutral CVaR-95 | min CVaR-95 | E at min CVaR | scored on | all λ converged |
|---:|---:|---:|---:|---:|---:|---:|:---|:---:|
| 50 | 42 | 7 | $182,256 | $224,600 | $215,171 | $188,486 | `exact` | yes |
| 50 | 1337 | 6 | $182,723 | $221,224 | $215,632 | $184,722 | `exact` | yes |
| 50 | 2718 | 6 | $182,723 | $221,224 | $215,171 | $188,486 | `exact` | yes |
| 100 | 42 | 8 | $182,256 | $224,600 | $215,171 | $188,486 | `exact` | yes |
| 100 | 1337 | 8 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |
| 100 | 2718 | 11 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |
| 200 | 42 | 10 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |
| 200 | 1337 | 12 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |
| 200 | 2718 | 15 | $182,256 | $224,600 | $215,171 | $188,486 | `exact` | yes |
| 400 | 42 | 14 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |
| 400 | 1337 | 17 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |
| 400 | 2718 | 16 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |
| 800 | 42 | 18 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |
| 800 | 1337 | 18 | $182,256 | $224,600 | $215,746 | $184,477 | `exact` | yes |
| 800 | 2718 | 20 | $182,256 | $224,600 | $214,747 | $187,077 | `exact` | yes |

*The M x N replication solves inside `saa_optimality_gap` are run by app/optimization/stochastic.py, which does not surface their per-solve CP-SAT status, so they are NOT represented in the run-level solve_quality block. The `endpoint_stability` rows below are, and each carries its own statuses / worst_mip_gap_pct / all_points_converged.*

<!-- GENERATED:saa_quality:END -->

---

## 8. Where a cost-vs-CVaR tradeoff exists at all

All 10 reference BOMs across their feasible volume range on the coarse λ grid. Reported
honestly including the BOMs where the answer is **no tradeoff exists** — a flat frontier
is a finding, not a failure — and including the instances where the solver did not
converge inside the 15-unit deterministic breadth budget, which are marked and excluded
rather than quietly averaged in.

> ### ⚠️ THIS IS THE ARM WHERE THE BUDGET BITES — BUT IT NOW BITES IN THE SAME PLACE EVERY TIME
>
> ### The `Worst gap` column, the `all λ converged` column, the **excluded** markings — and which rows appear in the table at all — are a record of what a **15-unit deterministic work budget** proved. They reproduce under any CPU load. They are still statements about the compute budget, not about these BOMs.
>
> **44 of this arm's 150 solves returned a status other than `OPTIMAL`**, i.e. exhausted
> that work budget with the bound still open. The leading counts in the paragraph below
> (**2** instances excluded, **28** producing a frontier, a tradeoff in **10** of them,
> spread over **5 of 10** BOMs) are therefore budget-dependent — they are derived from
> which solves converged — but they are no longer *load*-dependent. Before 2026-09-01 they
> were: a re-run under CPU load moved 16 of these worst-gap values and **removed
> `smart_meter` from this table entirely**. Under the deterministic budget the identical
> sweep hashed identically at load averages 2.5, 43.5 and 2.6. See §0.
>
> **A bigger budget would not rescue the excluded rows.** `drone_flight_controller ×1`
> (48.32–62.25%) and `automotive_ecu ×1` (12.43–90.79%) are genuinely hard at this size;
> the same BOMs converge to 0.000% at higher volumes, and a 20× budget sweep on the
> hardest instance bought three percentage points of gap (§0).
>
> ### What is NOT budget-dependent at all: `CVaR-95 reduction available` and `Price of it`.
>
> **No cost, no plan and no supplier set differed between any of the re-runs, on either
> budget kind.** Where a row reports a tradeoff, the dollars are real and are not caveated.

<!-- GENERATED:breadth:BEGIN -->

**10 reference BOMs**, 30 (BOM × volume) instances. On **3** of them no λ point converged inside the 15-unit deterministic-time budget (300s wall-clock runaway guard), so no frontier can honestly be reported and the row is marked **excluded**. Of the **27** instances that did produce a frontier, a cost-vs-CVaR tradeoff exists in **10**, spread over **5 of 10 BOMs** (`audio_dsp_board`, `iot_sensor_node`, `medical_monitoring_device`, `pcb_power_supply`, `rf_transceiver_module`).

| BOM | Distributors | Support | ×volume | Units | Atoms solved | Tradeoff? | CVaR-95 reduction available | Price of it | Worst gap | all λ converged |
|---|---:|:---|---:|---:|---:|:---:|---:|---:|---:|:---:|
| `iot_sensor_node` | 26 | sampled (2^26) | 1× | 5 | 98 | no | $0.00 (0.00%) | $0.00 | 95.06% | **NO** |
| `iot_sensor_node` | 26 | sampled (2^26) | 10× | 50 | 98 | **yes** | $14.72 (2.96%) | $24.90 | 73.08% | **NO** |
| `iot_sensor_node` | 26 | sampled (2^26) | 100× | 500 | 98 | no | $0.00 (0.00%) | $0.00 | 0.00% | yes |
| `iot_sensor_node` | 26 | sampled (2^26) | 1,000× | 5,000 | 98 | **yes** | $59.10 (0.41%) | $42.44 | 1.13% | yes |
| `iot_sensor_node` | 26 | sampled (2^26) | 10,000× | 50,000 | 98 | **yes** | $198 (0.07%) | $335 | 0.22% | yes |
| `drone_flight_controller` | 44 | sampled (2^44) | 1× | 7 | 77 | **excluded** | — | — | 62.93% | **NO** |
| `pcb_power_supply` | 6 | exact, 64 atoms | 1× | 6 | 64 | no | $0.00 (0.00%) | $0.00 | 0.00% | yes |
| `pcb_power_supply` | 6 | exact, 64 atoms | 10× | 60 | 64 | no | $0.00 (0.00%) | $0.00 | 0.17% | yes |
| `pcb_power_supply` | 6 | exact, 64 atoms | 100× | 600 | 64 | **yes** | $81.80 (4.36%) | $116 | 0.00% | yes |
| `pcb_power_supply` | 6 | exact, 64 atoms | 1,000× | 6,000 | 64 | **yes** | $86.73 (0.52%) | $134 | 1.13% | yes |
| `pcb_power_supply` | 6 | exact, 64 atoms | 10,000× | 60,000 | 64 | **yes** | $9,854 (4.39%) | $4,821 | 0.07% | yes |
| `industrial_motor_driver` | 46 | sampled (2^46) | 1× | 7 | 89 | no | $0.00 (0.00%) | $0.00 | 7.80% | **NO** |
| `industrial_motor_driver` | 46 | sampled (2^46) | 10× | 70 | 89 | no | $0.00 (0.00%) | $0.00 | 11.64% | **NO** |
| `rf_transceiver_module` | 29 | sampled (2^29) | 1× | 4 | 114 | no | $0.00 (0.00%) | $0.00 | 93.71% | **NO** |
| `rf_transceiver_module` | 29 | sampled (2^29) | 10× | 40 | 114 | no | $0.00 (0.00%) | $0.00 | 71.81% | **NO** |
| `rf_transceiver_module` | 29 | sampled (2^29) | 100× | 400 | 114 | no | $0.00 (0.00%) | $0.00 | 24.97% | **NO** |
| `rf_transceiver_module` | 29 | sampled (2^29) | 1,000× | 4,000 | 114 | **yes** | $1,006 (2.91%) | $7,765 | 0.00% | yes |
| `automotive_ecu` | 57 | sampled (2^57) | 1× | 7 | 68 | **excluded** | — | — | 91.07% | **NO** |
| `automotive_ecu` | 57 | sampled (2^57) | 10× | 70 | 68 | no | $0.00 (0.00%) | $0.00 | 63.09% | **NO** |
| `medical_monitoring_device` | 44 | sampled (2^44) | 1× | 8 | 81 | no | $0.00 (0.00%) | $0.00 | 57.83% | **NO** |
| `medical_monitoring_device` | 44 | sampled (2^44) | 10× | 80 | 81 | no | $0.00 (0.00%) | $0.00 | 26.53% | **NO** |
| `medical_monitoring_device` | 44 | sampled (2^44) | 100× | 800 | 81 | **yes** | $1,556 (8.50%) | $1,430 | 0.00% | yes |
| `medical_monitoring_device` | 44 | sampled (2^44) | 1,000× | 8,000 | 81 | **yes** | $3,414 (1.10%) | $5,155 | 0.00% | yes |
| `smart_meter` | 51 | sampled (2^51) | 1× | 4 | 88 | **excluded** | — | — | 64.67% | **NO** |
| `smart_meter` | 51 | sampled (2^51) | 10× | 40 | 88 | no | $0.00 (0.00%) | $0.00 | 17.92% | **NO** |
| `robotics_servo_driver` | 46 | sampled (2^46) | 1× | 9 | 84 | no | $0.00 (0.00%) | $0.00 | 0.00% | yes |
| `audio_dsp_board` | 31 | sampled (2^31) | 1× | 7 | 116 | no | $0.00 (0.00%) | $0.00 | 88.30% | **NO** |
| `audio_dsp_board` | 31 | sampled (2^31) | 10× | 70 | 116 | **yes** | $30.66 (2.99%) | $0.00 | 3.08% | yes |
| `audio_dsp_board` | 31 | sampled (2^31) | 100× | 700 | 116 | no | $0.00 (0.00%) | $0.00 | 0.00% | yes |
| `audio_dsp_board` | 31 | sampled (2^31) | 1,000× | 7,000 | 116 | no | $0.00 (0.00%) | $0.00 | 0.00% | yes |

<!-- GENERATED:breadth:END -->

---

## 9. Solve times and problem sizes

### ⚠️ The `λ-sweep wall time` column below is a stopwatch reading on one machine, not a problem property.

**It measures the hardware in [Provenance](#provenance), under whatever CPU load it was
under, and it will not reproduce on your machine — that is permanent, and no budget change
can fix it.** `Worst gap` and `λ not converged` are a different thing: they are what a
deterministic work budget closed at `num_search_workers = 1`, and since 2026-09-01 they
**do** reproduce under any CPU load (§0). `Distributors`, `Distinct scenarios`, `Variables`
and `λ points` are problem sizes and always reproduced.

<!-- GENERATED:solve_times:BEGIN -->

| Instance | Distributors | Distinct scenarios | Variables | λ points | λ-sweep wall time | Worst gap | λ not converged |
|---|---:|---:|---:|---:|---:|---:|---:|
| `pcb_power_supply` ×100 (primary arm) | 6 | 64 (exact support) | 1110 | 9 | **2.0 s** | 0.000% | 0 |
| `pcb_power_supply` ×1,000 (primary arm) | 6 | 64 (exact support) | 1073 | 9 | **32.8 s** | 0.000% | 0 |
| `pcb_power_supply` ×10,000 (primary arm) | 6 | 64 (exact support) | 1029 | 9 | **4.9 s** | 0.000% | 0 |
| `smart_meter` ×10 (breadth arm) | 51 | 88 (SAA, 100 draws) | 8616 | 5 | 107.9 s | 17.92% | 3 |
| `industrial_motor_driver` ×10 (breadth arm) | 46 | 89 (SAA, 100 draws) | 7192 | 5 | 88.3 s | 11.64% | 1 |
| `iot_sensor_node` ×10 (breadth arm) | 26 | 98 (SAA, 200 draws) | 4764 | 5 | 84.8 s | 73.08% | 2 |
| `rf_transceiver_module` ×10 (breadth arm) | 29 | 114 (SAA, 200 draws) | 6360 | 5 | 75.5 s | 71.81% | 3 |
| `smart_meter` ×1 (breadth arm) | 51 | 88 (SAA, 100 draws) | 8616 | 5 | 68.1 s | 64.67% | 5 |
| `iot_sensor_node` ×1 (breadth arm) | 26 | 98 (SAA, 200 draws) | 4764 | 5 | 57.4 s | 95.06% | 1 |
| `iot_sensor_node` ×1,000 (breadth arm) | 26 | 98 (SAA, 200 draws) | 4764 | 5 | 43.3 s | 1.13% | 0 |
| `iot_sensor_node` ×10,000 (breadth arm) | 26 | 98 (SAA, 200 draws) | 4764 | 5 | 23.5 s | 0.22% | 0 |
| `iot_sensor_node` ×100 (breadth arm) | 26 | 98 (SAA, 200 draws) | 4764 | 5 | 14.3 s | 0.00% | 0 |

*The five slowest breadth instances are listed, plus every volume of `iot_sensor_node` (the instance this section used to quote stale figures for). The full set is in `docs/cvar_frontier.json` → `breadth`. A `—` in the Variables column is an instance where no λ point converged at all, so the entry carries its `excluded_reason` instead of a frontier.*

<!-- GENERATED:solve_times:END -->

**Honest reporting of what is hard.** The model size is driven by the per-scenario
expedited-consignment binaries `e[d,s]`: on a large-pool BOM with a hundred-plus distinct
scenarios that is thousands of extra booleans, and it is the difference between a 0.02 s
solve and a time-limit hit. Removing that term makes the model far faster and changes
CVaR-95 materially — so it is kept, and the cost of keeping it is reported rather than
hidden, per instance, in the table above and in the §0 solve-quality summary.

**Three specific things were measured and are stated rather than smoothed over — all three
are timings and gaps on the machine in [Provenance](#provenance), so read the seconds and
the percentages as that machine's, not as the problem's (§0). What survives a change of
machine is the *ordering* each one establishes, not the figure.**

1. **λ = 0 is the hardest point on every frontier.** Without the CVaR block the objective
   is a sum of 150+ loosely-coupled recourse subproblems that CP-SAT finds easy to solve
   and hard to *prove* optimal. Mitigations applied: the recourse-only RU reformulation,
   and frontier continuation (each λ warm-starts from its already-proved neighbour,
   sweeping **descending** because the pure-CVaR end is by far the easiest). A third
   mitigation — a 0.1% relative gap limit — was **removed** on 2026-08-27: it bought
   solve time by letting `OPTIMAL` mean "within a tolerance", which is not what the word
   should mean in a document that leans on it. The limit is now `0.0` and λ = 0 still
   proves out on the primary arm. Every point still reports its achieved gap.
2. **Reported statistics never come from the solver's own recourse variables.** A non-zero
   MIP gap here mostly means the *second-stage* variables are near-optimal, and reading
   E and CVaR off them would publish a number worse than the plan actually is. Every
   published figure comes from re-solving each scenario's second stage exactly and
   independently for the returned plan.
3. **An int64 overflow guard fired for real** at N = 800 draws on the 60,000-unit BOM and
   is left in place. Objective coefficients are bounded before the solve; if the bound
   exceeds 4×10^17 the model raises with an actionable message instead of silently
   returning nonsense.

---

## 10. What is NOT modelled

Every omission with its **direction of bias**, because a tail number without this list is
not honest:

| Not modelled | Direction of bias |
|---|---|
| Partial capacity loss — outages are binary | Overstates individual scenario severity |
| **Correlated / common-cause failures** — draws are independent across suppliers | **Understates tail risk** (one typhoon takes out several warehouses) |
| Disruption duration and multi-period recovery | Understates |
| Qualification time/cost for a supplier not opened in stage 1 | **Understates** cost of recourse |
| MOQ on emergency buys | Slightly understates |
| Price movement under stress — emergency prices are catalogue + fixed premium | **Understates** tail cost |
| Demand uncertainty — only supply is stochastic | Neither; out of scope |

The independence assumption is the most consequential. Correlated disruptions are exactly
what makes real tails fat, and modelling them would require a dependence structure this
repo has no data to calibrate. **Net: the tail reported here is, if anything, optimistic.**

Two model choices worth flagging explicitly rather than leaving in the code:

* **`recovery_rate = 1.0`** — you do not pay for goods you never received. That is the
  economically correct default for a purchase order, and it means the modelled loss from a
  disruption is the *price gap plus expediting*, not the whole committed spend. It also
  means a scenario's recourse cost can be slightly **negative** when an expensive supplier
  fails and a cheap survivor covers the gap. Lower the parameter to model deposits or
  cancellation fees.
* **The unmet-demand penalty is `3.0 × the dearest emergency route`**, not 3× catalogue
  price. Anchoring on catalogue price alone made unmet demand *cheaper* than recourse on
  small residual quantities — 30 units at 3×$2.50 = $225 beat a $150 expedited consignment
  plus $94 of parts — so the model would leave a line unfilled while stock sat on a
  surviving shelf. `STOCKOUT_PENALTY_MULTIPLE = 3.0` itself is reused verbatim from
  `sourcing.py` (Snyder & Daskin 2005), so the stochastic program and the heuristic it
  replaces share their constants and the comparison is about **structure**, not tuning.

---

## Reproduce

```bash
cd backend
source venv/bin/activate
python -m seeds.run_cvar_frontier              # full artifact (~20 min)
python -m seeds.run_cvar_frontier --quick      # primary + calibration only (~4 s)
python -m seeds.run_cvar_frontier --render-only # re-render this doc from the JSON, no solves
```

Writes `docs/cvar_frontier.json` — full per-λ frontier, knee, tail decomposition,
exact-vs-SAA comparison, SAA optimality gaps, sensitivity grid, breadth sweep, the
run-level solve-quality distribution, and the per-distributor calibration with provenance.

> **This document has a generator.** Every numeric block above sits between
> `GENERATED:<block>:BEGIN` and `GENERATED:<block>:END` HTML-comment markers and is written
> from `docs/cvar_frontier.json` by `render_doc()` in the same script — because these
> numbers used to be hand-transcribed, and hand transcription is exactly how §6/§7/§8 came
> to point at artifact keys that did not exist and how the §9 `iot_sensor_node` row came to
> quote a run that no longer existed. Prose, caveats and derivations live *outside* the
> markers and the generator never touches them. `backend/tests/test_cvar_doc_matches_artifact.py`
> fails the build if the two ever disagree again.
>
> A `--quick` artifact is **partial** by design: it contains `primary` and `calibration`
> only, and the generated §6/§7/§8 blocks then say so explicitly instead of pointing at
> keys that are not there.

Live, with the probability assumptions exposed as request parameters:

```bash
curl -X POST /api/v1/stochastic/frontier -d '{
  "items": [{"component_id": 1, "quantity": 15000}],
  "base_annual_prob": 0.2368, "horizon_days": 60, "centrality_spread": 3.0
}'
curl /api/v1/stochastic/calibration          # every p_fail, beside the legacy value
```

**Reproducing §4 and §5 from the endpoint rather than the script.** The λ grid must
contain 0.3 (it does), the pool must be small enough to score on the exact support (6
distributors → 64 atoms, it is), and the depot must be the artifact's:

```bash
curl -X POST /api/v1/stochastic/frontier -d '{
  "items": [{"component_id": 429, "quantity": 20000},   
            {"component_id": 431, "quantity": 10000},   
            {"component_id": 457, "quantity": 20000},   
            {"component_id": 442, "quantity": 10000}],  
  "depot_lat": 37.7749, "depot_lng": -122.4194
}'
# ids are LM317DCY / TPS767D325PWP / UA78M33CDCY / OPA861ID at 10,000x the
# pcb_power_supply BOM in seeds/run_benchmark.py -- resolve them by MPN if they move
# -> recommendation.knee_lambda                        0.3
#    recommendation.cvar_removed_per_dollar_spent      4.266
#    recommendation.extra_expected_cost_usd            2043.83
#    recommendation.cvar_reduction_usd                 8718.79
#    recommendation.cvar_removed_per_dollar_spent_beyond_knee   0.409
```

Re-verified 2026-08-27 against `compute_cvar_frontier` on this commit: all five figures come
back identical to `docs/cvar_frontier.json`, and the endpoint reports `OPTIMAL` at a 0.0000%
gap with no `dominated` point (**the five figures reproduce; the status and the gap are the
server's own budget measurement on the day — §0**) — because the endpoint and the generator now select the solve
set through the same `fit_scenario_set` call.

**The last of those five used to depend on the λ grid.** The endpoint sweeps 7 λ values and
the artifact sweeps 9, and while the plan was chosen on a sample the beyond-knee ratio came
back **0.342** from the endpoint against **0.409** from the artifact — the same instance,
the same knee, two different answers to "what does the next dollar buy?". Solving on the
exact support removes the disagreement: **both grids now report 0.409.** A statistic that
moves when you add two λ points was measuring the sweep, not the frontier.

Omit `depot_lat`/`depot_lng` and the endpoint answers the same question from Memphis —
a real frontier, but not this one.

Tests: `backend/tests/test_stochastic_sourcing.py` (50) and
`backend/tests/test_stochastic_api.py` (22). The load-bearing ones:

* `test_no_supplier_ever_saturates_at_probability_one` — the regression guard for §2.
* `test_with_no_uncertainty_it_reproduces_the_deterministic_landed_cost` — the
  anti-rigging invariant: with nothing to be stochastic about, the stochastic program's
  cost must equal `greedy.landed_cost_breakdown` exactly.
* `test_exact_evaluation_puts_many_atoms_in_the_tail_where_sampling_puts_few` — §3.
* `test_saa_optimality_gap_brackets_the_optimum_and_shrinks_with_sample_size` — §7.
* `test_risk_aversion_moves_the_award_to_a_lower_probability_supplier` — that buying risk
  aversion actually buys a lower tail, and is not free.
* `test_a_timeout_is_a_budget_error_and_never_claims_infeasibility` and
  `test_a_proven_infeasibility_is_a_distinct_error_type` — the regression guards for the
  status-mapping defect described below.
* `test_a_flat_frontier_is_described_rather_than_left_for_the_reader_to_infer` — a flat
  frontier must SAY it is flat, not arrive as a null recommendation beside identical rows.

### A defect this endpoint shipped with, and what it taught

Until 2026-08-16 the model treated **any** CP-SAT status outside {OPTIMAL, FEASIBLE} as
infeasible, and the API turned that into
`422 "No feasible sourcing plan exists for this BOM"`. CP-SAT reports `UNKNOWN` when the
time limit expires **before finding any solution** — a statement about the search budget
that carries no information about feasibility. An audit against a live instance found
**6 of 7 realistic BOMs returning that 422**; every one of them was in fact solvable.

The cause was not the time limit. Model size grows linearly in distinct scenarios × pool
size, and on a 55-supplier BOM 200 draws deduplicate to 183 distinct scenarios and a
~29,000-variable model. Measured at λ = 0.5 on one worker, on the machine in
[Provenance](#provenance): 60 draws → 9,424 variables → **OPTIMAL in 2.6 s**; 200 draws →
28,937 variables → **no feasible solution at all**, and tripling the limit to 15 s only
reached a 21% gap. **The seconds and the gap are that machine's (§0); the finding is the
direction — more scenarios, exponentially worse search — which holds on any machine.** Tripling the budget does not
rescue it; sizing the scenario set does, and lands on the same plan.

Three lessons, now encoded rather than remembered:

1. **A solver status is a diagnosis. Collapsing four statuses into one error blames the
   user for the service's own budget.** `INFEASIBLE`, `UNKNOWN` and `MODEL_INVALID` are
   now distinct exception types mapping to 422, 503 and 500.
2. **The scenario count is the lever, not the clock.** `fit_scenario_set` sizes the
   *solve* set to a variable budget while the *evaluation* set stays full. It now takes
   the **exact support first** and only falls back to thinning a sample when that support
   will not fit — so on enumerable instances there is nothing left to thin and no choice
   error to bound. Where it does fall back, thinning costs SAA choice error, which
   `saa_optimality_gap` bounds, and leaves the published E and CVaR untouched.
3. **A partial frontier beats an error.** Four of six λ points, clearly labelled, is a
   usable answer; a confident wrong 422 is not.

---

## Provenance

<!-- GENERATED:provenance:BEGIN -->

- **Generated:** 2026-09-04T02:21:15Z (UTC)
- **Generator:** `seeds.run_cvar_frontier`
- **Commit:** `ffc9014ea86d830c26c820f540e5789774f808be` (clean tree)
- **Input `component_database`:** `backend/supply_chain.db` · sha256 `b60dc9ba058a6956…`
- **Input `ml_metrics`:** `backend/data/ml_models/metrics.joblib` · sha256 `7dea84c6f76e5d0e…`
- **Input `ml_regime_model`:** `backend/data/ml_models/regime.joblib` · sha256 `fdfc675c04ee54cc…`
- **Input `ml_lead_time_models`:** `backend/data/ml_models/lead_time.joblib` · sha256 `a1d3343485c499de…`
- **Python:** 3.13.5 · macOS-26.5-arm64-arm-64bit-Mach-O
- **Run mode:** full
- **Wall clock:** 1570.2 s
- **Hardware:** arm64 / Darwin 25.5.0

<!-- GENERATED:provenance:END -->
