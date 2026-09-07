# Research Backlog — Statistical & OR Techniques Worth Adding

**Compiled 2026-08-15/16** from a literature scan (2023–2026) plus verification against this
repo's actual data artifacts. Every citation below was checked to exist; nothing here is
recalled from memory.

**How to use this doc:** each item states whether *our real data can actually support it*.
That column is the point. A technique this project cannot honestly support is not a backlog
item — it is a trap, and the "Do not build" section at the bottom is as valuable as the rest.

---

## The strategic finding that reorders everything

Verified against the artifacts on disk:

| Asset | Reality | Consequence |
|---|---|---|
| **Monash car parts** (`docs/intermittent_demand.json`) | **2,674 series × 51 months**, 24.1% non-zero, **2,646 scored** under the rolling-origin protocol | **Now carrying the demand story** — proper scoring rules and significance testing shipped (1.1/1.2 below); still supports a newsvendor study (1.4) and conformal calibration. |
| Census M3 A34SNO (`docs/forecast_backtest.json`) | `n_obs=198` at the pinned `2026-08-16` vintage, **`n_windows=3`**, horizon 12 → **36 test points from 3 origins** | The weakest evidence in the repo. No significance test is possible. And the series is *revised in place* — see 4.1; it is now vintage-pinned, and the revision moves WAPE more than the model choice does. Saying so is worth more than another model. |
| CVaR frontier (`docs/cvar_frontier.json`) | tail atoms now 31–53 after calibration work; largest single atom still 32–80% of tail mass | Tail estimate improved but remains atom-dominated at low volume. Report it. |
| Lead-time panel | **3,406 rows / 6 snapshots on disk** (sha256 `d94df904…`); the **served model is fitted on an earlier cut of it** — 2,615 usable rows of the then 2,664-row, five-snapshot panel (sha256 `c68e2891…`), retrained 2026-09-03 — one distributor | Supports the ST-extension *event narrative*; supports almost no inference. The staleness tripwire reports `stale: true` — the 2026-09-07 collector run moved the panel past the artifact, and a retrain is owed. Every `2,615` / `472` / `28` / `324` figure below is a property of that artifact, not of the panel. |

**Therefore: stop pointing new statistics at the 197-point macro series. Point them at car parts.**
Nearly every item below gets cheaper and more defensible under that reframe.

---

## Track 1 — Demand forecasting (Monash car-parts panel)

### 1.1 Re-score the intermittent-demand benchmark distributionally — **done**
*Effort: estimated 2–3 days; actual ~1. Data support: full.*

The hypothesis was that **MAE/MASE is minimized by the conditional median, and with 24%
non-zero demand that median is frequently zero**, so a MASE leaderboard on intermittent
demand can reward a degenerate near-zero forecast. **Confirmed, and more starkly than
expected.** Every method (Croston/SBA/TSB/naive, plus a `climatology` reference and an
explicit `zero` forecast added to test the hypothesis directly) now emits a compound
Bernoulli × zero-truncated negative binomial predictive distribution, scored by CRPS and
scaled pinball loss alongside MASE/RMSSE. Across 2,646 series:

- **`zero` — forecasting nothing, every period — wins both MASE and RMSSE outright**
  (mean Friedman rank 1.66 of 6).
- Under CRPS it falls to 4th, under scaled pinball loss to 5th; `tsb` wins both.
- Kendall's τ between the MASE and pinball orderings is **−0.20** — the leaderboards are
  mildly anti-correlated, not merely reshuffled.

The original guess in this section ("TSB wins on MASE may be partly a metric artifact") was
half right: TSB does not win MASE at all once the degenerate forecast is on the board.
Full write-up: [`docs/INTERMITTENT_DEMAND.md`](INTERMITTENT_DEMAND.md).

- Kolassa, S. (2020). "Why the 'best' point forecast depends on the error or accuracy measure."
  *IJF* 36(1):208–211. doi:10.1016/j.ijforecast.2019.02.017
- Kolassa, S. (2016). "Evaluating predictive count data distributions in retail sales
  forecasting." *IJF* 32(3):788–803. doi:10.1016/j.ijforecast.2015.12.004
- Gneiting, T. & Raftery, A.E. (2007). "Strictly Proper Scoring Rules, Prediction, and
  Estimation." *JASA* 102(477):359–378.
  https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf

**Why it lands:** "I noticed my accuracy metric was structurally biased toward zero forecasts on
intermittent demand, re-scored the benchmark distributionally, and the winner changed" is a
senior observation *and* a correction of our own work.

### 1.2 Significance testing across series (MCB / Nemenyi) + rolling origin — **done**
*Effort: 1–2 days. Data support: full on car parts; explicitly NOT on A34SNO.*

The single split is gone: the car-parts protocol is now rolling origin (horizon 6, three
origins, train sizes 33/39/45), sharing `app.ml.backtest.rolling_origins` with the macro
backtest so the two cannot drift apart. MCB ships across 2,646 series: Friedman χ²(5) with
the Iman–Davenport F correction, plus Nemenyi critical differences (**CD = 0.147** at
α = 0.05), with the critical-difference diagram data — mean ranks, CD, and the
non-separated cliques — in the artifact. Under CRPS and scaled pinball loss **every pair is
separated**; under MASE `tsb` and `sba` are not.

**One claim in the original plan was wrong, and the implementation disproved it.** This
section asserted that "naive nests inside several of our methods". It does not: Croston at
α → 0 returns its *initialisation*, not the sample mean, so neither `naive_last` nor
`climatology` is a parameter restriction of any Croston-family method. The two nestings
that genuinely exist are `croston ⊂ sba` (SBA = Croston × (1 − φ/2), Croston is φ = 0) and
`zero ⊂ {croston, sba, tsb}` (the p = 0 restriction of the compound-Bernoulli model).
Clark–West is applied to those and Diebold–Mariano to the rest. The zero-restriction case
turns out to be **degenerate** — with f₁ = 0 the CW adjustment collapses to 2·y·f₂, which is
≥ 0 for any non-negative forecast, so rejection is automatic — and is reported flagged
rather than dropped. The one informative nested result is `croston → sba`: t = 11.33,
p = 4.5e-30.

- Koning, Franses, Hibon & Stekler (2005). "The M3 competition: Statistical tests of the
  results." *IJF* 21(3):397–409. — origin of MCB.
- Tashman, L.J. (2000). "Out-of-sample tests of forecasting accuracy." *IJF* 16(4):437–450.
- Harvey, Leybourne & Newbold (1997). "Testing the equality of prediction mean squared errors."
  *IJF* 13(2):281–291 — small-sample correction to Diebold–Mariano (1995).
- Clark, T.E. & West, K.D. (2007), *J. Econometrics* 138(1):291–311 — **required instead of DM
  when models are nested**; naive nests inside several of our methods, so raw DM is invalid there.

**The honest half matters as much:** state that MCB is right for 2,658 series and *wrong* for
A34SNO, where 3 origins cannot support any test. Refusing to run an unpowered test is the
differentiator; most portfolio projects run a t-test on three numbers.

### 1.3 Retire the synthetic per-part demand — **done**
*Effort: 0.5 day (actual). Data support: n/a — this was a deletion.*

`component_demand_history` (791 × 52 weeks) and `component_forecasts` are gone —
migration `0008_drop_synthetic_demand_tables.py` drops both, along with
`backend/seeds/train_forecasts.py`, `backend/app/models/forecast.py`,
`backend/app/api/forecasts.py`, and the `GET /api/v1/forecasts/all` endpoint. The
series had a real temporal *shape* (borrowed from Census M3 `A34SNO`) but a
fabricated *magnitude*: `base_rate = total_stock / 52 × risk_score` — demand
inferred from inventory position and a risk multiplier, causally backwards, and
identical in shape across all 791 parts. The Prophet fit on top of it predicted a
12-week window that closed 17 months before this line was written, against which
no actuals were ever recorded — unscoreable even in principle. There is no public
source of real per-part demand for these components, so it was removed rather
than patched, and Monash now carries the demand story:
`GET /api/v1/demand/benchmark`, backed by `docs/intermittent_demand.json`
(script: `backend/seeds/run_carparts_backtest.py`), documented in
[`docs/INTERMITTENT_DEMAND.md`](INTERMITTENT_DEMAND.md).

---

## Track 2 — Lead-time model

> **Every lead-time number in this track is measured by
> `python -m seeds.run_leakage_progression` and published in
> [`leakage_progression.json`](leakage_progression.json) /
> [`LEAKAGE_PROGRESSION.md`](LEAKAGE_PROGRESSION.md).** An earlier revision of this
> file quoted the progression from memory as `0.95 → 0.19–0.29 → 0.06` on "684 rows,
> 28 manufacturers". All five of those figures were wrong. They are corrected below.

### 2.1 Conformal prediction intervals, grouped by part family
*Effort: 1–2 days. Data support: yes (n=2,615 rows trained, **472 family *group keys***,
28 manufacturers — all three straight from `leakage_progression.json` →
`counts.n_rows` / `counts.n_family_group_keys` / `counts.n_manufacturers`, which that file pins
to `meta.panel_sha256` `c68e2891…`, i.e. the **2026-09-03 artifact vintage**, fitted on the
then 2,664-row cut of a panel that now holds 3,406 rows).*

> **Use the right noun for 472.** It is the count of `_group_key` values, not of part
> families. Grouping on `base_product` collapses 742 MPNs into **361** base_product levels;
> 472 is what you get once the MPN and row-index fallbacks are counted (360 real families + 1
> `Unknown` level = 361, and 360 real families + 112 MPNs split out of `Unknown` = 472).
> `app/ml/lead_time_model.py:1655` states this. Several docs used to say "467 part
> families" (an 810-row, 27-manufacturer vintage two retrains have since replaced), which
> overstates the family count by about 31% at the current panel size.

Nothing in the repo currently makes a *calibrated uncertainty* claim. The lead-time point
estimate is weak by construction (family-grouped R² = **+0.140 median / +0.073 mean** over
50 `GroupKFold` folds, fold sd 0.246; the served `metrics.joblib`, which uses repeated
`GroupShuffleSplit` instead over 20 splits, reports +0.154 median / +0.126 mean — same story,
different resampler), but a calibrated
80% interval on the same data is genuinely useful — and it is what the optimizer actually wants
to consume. Adaptive Conformal Inference is a short online update, distribution-free, and holds
under distribution shift. **Must be grouped/Mondrian by family**, matching the CV design.

- Gibbs, I. & Candès, E. (2021). "Adaptive Conformal Inference Under Distribution Shift."
  *NeurIPS 34*:1660–1672. Journal version *JMLR* 25 (2024),
  https://www.jmlr.org/papers/v25/22-1218.html
- Angelopoulos, Candès & Tibshirani (2023). "Conformal PID Control for Time Series Prediction."
  *NeurIPS 2023*.
- Romano, Patterson & Candès (2019). "Conformalized Quantile Regression." *NeurIPS 32*.
- MAPIE (scikit-learn-contrib): https://github.com/scikit-learn-contrib/MAPIE — implements
  EnbPI/CQR/`MapieTimeSeriesRegressor`, so this may be config rather than code.

Report **coverage vs nominal** and a **PIT histogram**. A lumpy PIT is itself an honest result.

### 2.2 Partial pooling / mixed effects with a manufacturer random effect
*Effort: 1–2 days. Data support: yes, and it is the statistically correct tool.*

Effective sample for generalization is **28 manufacturers**, not 2,615 rows, and **9 of them have
≤6 rows** — while three vendors (Analog Devices, TI, STMicroelectronics) supply 61.1% of the panel.
Shrinkage handles the tiny groups that a one-hot GBM either memorizes or ignores. This
addresses the family-leakage problem directly rather than routing around it. `statsmodels`
MixedLM or a small Bayesian model.

### 2.3 Present the leakage collapse as a headline result
*Effort: ~0 (already measured). Data support: [`leakage_progression.json`](leakage_progression.json).*

Same estimator, same 2,615 rows, same feature pipeline, same seed — only the fold boundary moves:

| Split regime | R² mean | R² median | fold sd |
|---|---:|---:|---:|
| random rows (**the wrong protocol**) | **+0.825** | +0.826 | 0.025 |
| `GroupKFold` by part family (`base_product`) | **+0.073** | +0.140 | 0.246 |
| `GroupKFold` by manufacturer | **−0.697** | −0.104 | 2.642 |

50 folds per regime (5-fold × 10 shuffles, seed 42), champion `gradient_boosting`. Full protocol,
per-fold scores and the naive baselines on the identical folds:
[`LEAKAGE_PROGRESSION.md`](LEAKAGE_PROGRESSION.md).

**The negative number is the interesting one, and it should be stated exactly.** R² is scored
against the *held-out fold's own* mean, so R² < 0 means the model's squared error exceeds that
vendor's entire label variance: on a manufacturer it has never quoted, the model has no
explanatory power at all. It is not beaten by the trivial predictors there — `train_mean` scores
−2.177 on the same folds — so the honest claim is that the model is the best member of a set in
which **nothing generalises to an unseen vendor**, not that the model is uniquely bad.

Most candidates never discover this about their own project. The collapse, with the diagnostic
that found it, is worth more than any model.

> **Do not confuse this with in-sample identity-column R².** Fitting a per-level mean on all
> rows and scoring it on those same rows gives `base_product` **0.856** (361 levels over 2,615
> rows) and `mpn` 0.957. Those are not cross-validated, not model scores, and not the
> random-split figure — they are the *measurement of the redundancy* that makes a random split
> leak in the first place. Quoting one of them as the random-split R² is exactly the error this
> document used to contain. (Earlier revisions quoted +0.638 / +0.082 / −0.550 on an 810-row,
> 27-manufacturer, `random_forest` vintage, and then +0.804 / +0.084 / −0.784 on the 1,879-row,
> four-snapshot cut — three retrains have since replaced that panel; if you see either set
> elsewhere, they are the retired figures.)

---

## Track 3 — Optimization / OR

### 3.1 CVaR *is* distributionally robust optimization — free reframe
*Effort: 0 (one paragraph). Data support: n/a.*

CVaR_α(Z) = sup{E_Q[Z] : Q ≪ P, dQ/dP ≤ 1/α} — a worst-case expectation over a
likelihood-ratio-bounded ambiguity set. Stating this turns "I used a risk measure" into "I solved
a distributionally robust program," which is the same work described in the vocabulary an OR
interviewer listens for.

- Rockafellar, R.T. & Uryasev, S. (2002). "Conditional value-at-risk for general loss
  distributions." *J. Banking & Finance* 26(7):1443–1471.
  https://sites.math.washington.edu/~rtr/papers/rtr187-CVaR2.pdf
- Artzner, Delbaen, Eber & Heath (1999). "Coherent Measures of Risk." *Mathematical Finance*
  9(3):203–228.

### 3.2 Bertsimas–Sim price of robustness — a second frontier
*Effort: 1–2 days. Data support: yes; stays in CP-SAT.*

A budget-of-uncertainty parameter Γ caps how many coefficients simultaneously hit worst case, and
**the robust counterpart of a MILP stays a MILP of essentially the same size** — no conic solver,
so it plugs into the existing stack. Sweep Γ, plot cost-vs-Γ beside cost-vs-CVaR, and explain the
distinction: **CVaR trades expected cost against tail cost; Bertsimas–Sim trades expected cost
against worst-case constraint violation.** Complements, not substitutes.

- Bertsimas, D. & Sim, M. (2004). "The Price of Robustness." *Operations Research* 52(1):35–53.
  doi:10.1287/opre.1030.0065

### 3.3 SAA optimality-gap confidence interval
*Effort: ~1 day. Data support: yes; partially built already.*

M independent SAA replications give a statistically optimistic lower bound; evaluating the chosen
first-stage plan on a much larger held-out scenario sample gives the upper bound; report the
**gap with a confidence interval** and sweep N to show where it stabilizes. `out_of_sample_seeds`
and the exact-vs-SAA comparison already exist in `docs/cvar_frontier.json` — this finishes it.

- Mak, W-K., Morton, D.P. & Wood, R.K. (1999). "Monte Carlo bounding techniques for determining
  solution quality in stochastic programs." *OR Letters* 24(1–2):47–56.
  doi:10.1016/S0167-6377(98)00054-6
- Kleywegt, Shapiro & Homem-de-Mello (2002). "The Sample Average Approximation Method for
  Stochastic Discrete Optimization." *SIAM J. Optimization* 12(2):479–502.

### 3.4 Newsvendor critical fractile — **done**
*Effort: estimated 3–4 days; actual ~1. Data support: full, on car parts.*

Shipped: `backend/app/optimization/newsvendor.py` (the decision and its evaluation),
`backend/app/api/newsvendor.py` (`GET /newsvendor/assumptions`, `POST /newsvendor/decision`,
`GET /newsvendor/evaluation`), `backend/tests/test_newsvendor.py` (46 tests). The numbers in
this section are generated by `backend/seeds/run_newsvendor.py` into `docs/newsvendor.json`
and checked against it by `backend/tests/test_newsvendor_docs_match_artifact.py` — this
section is no longer hand-transcribed and unchecked.

**The decision.** `C(q) = Cu·E[(D−q)⁺] + Co·E[(q−D)⁺]` is convex on the integers with first
difference `(Cu+Co)·F(q) − Cu`, so the optimum is the smallest integer `q` with
`F(q) ≥ τ = Cu/(Cu+Co)`. That is exactly `proper_scoring.quantile_from_pmf`, which already
existed — the decision layer is a lookup in the predictive cdf, not a solver. The dual
identity is what makes the forecasting track load-bearing rather than decorative: realised
newsvendor cost at `q` equals `(Cu+Co) × pinball_loss(q, y, τ)` **exactly**, so the scaled
pinball loss already on the leaderboard *is* this decision cost up to a constant.

**Where each cost comes from — nothing invented for the occasion.**

| | value | source |
|---|---|---|
| `Co` overage | `price × 0.25 × L/12` | `app.optimization.costs.holding_cost_usd`, the *same function* the freight model calls. 25%/yr electronics holding rate cited to Gartner IT Supply Chain Benchmarks 2022. |
| `Cu` underage (default) | `price × 0.15` | the emergency-reprocurement premium already in `sourcing.py` and `graph/simulation.py`; a test pins all three equal. |
| `Cu` underage (sensitivity) | `price × 3.0` | `sourcing.STOCKOUT_PENALTY_MULTIPLE`, after Snyder & Daskin (2005) — a single-sourced line-down event. |
| excluded | `$150` fixed air consignment | per *consignment*, not per unit, so it cannot enter a linear per-unit `Cu`. Excluding it pushes `Cu`, `τ` and `q*` **down**: the published saving is a lower bound. |

**τ = 0.15 / (0.15 + 0.0208) = 0.8780** at a one-month review period. Two framing choices do
all the work and both are stated in the code rather than buried:

- A spare part that is out of stock is **not a lost sale** — the demand does not evaporate,
  the unit is expedited. So `Cu` is a *fraction* of unit price, not a multiple of it.
- This is a **carrying-charge** newsvendor, not a perishable one. Unsold stock carries
  forward, so `Co` is one period of carrying charge (~2% of price/month), not a write-off.
  That single choice is what makes τ 0.88 rather than 0.13, and it is the first thing a
  reader should attack.

**τ does not depend on the unit price** — both costs are proportional to it, so it cancels.
That is not a convenience, it is the precondition: the Monash panel is real demand with **no
prices attached**, and this repo does not fabricate data. Every dollar below is per SKU per
month **per $1.00 of unit price** and scales linearly.

**The result.** 2,646 of 2,674 series (28 dropped by the leaderboard's own balance rule; the
numerical defect described below is fixed and drops none on this run), three rolling origins
from the same `app.ml.backtest.rolling_origins`, **47,628 held-out decisions**. Paired across
series, 5,000-replication bootstrap, seed 0.

<!-- numbers below are generated by `cd backend && ./venv/bin/python -m seeds.run_newsvendor`
     into `docs/newsvendor.json` (`primary` key) and checked against it by
     `backend/tests/test_newsvendor_docs_match_artifact.py` -->

| policy | cost | mean q | Δ vs newsvendor | 95% CI | reduction | win / tie / loss |
|---|---:|---:|---:|---|---:|---|
| **newsvendor fractile (τ=0.878)** | **0.03810** | 1.28 | — | — | — | — |
| Scarf min-max (same two moments) | 0.03969 | 1.48 | 0.00159 | [0.00115, 0.00205] | **4.0%** | .27 / .58 / .15 |
| normal safety stock `μ + z_τ σ` | 0.03974 | 1.49 | 0.00164 | [0.00120, 0.00210] | **4.1%** | .27 / .59 / .14 |
| fixed safety multiple `2 × mean` | 0.04114 | 1.56 | 0.00304 | [0.00259, 0.00349] | 7.4% | .43 / .33 / .24 |
| order the point forecast | 0.04346 | 0.42 | 0.00537 | [0.00450, 0.00624] | 12.3% | .38 / .22 / .40 |
| order last period's demand | 0.05051 | 0.43 | 0.01242 | [0.01129, 0.01359] | 24.6% | .48 / .25 / .28 |
| order nothing (the MASE winner) | 0.05977 | 0.00 | 0.02167 | [0.02002, 0.02336] | **36.3%** | .49 / .23 / .28 |

Every CI excludes zero, so the ship gate — `beats_all_baselines_significantly`, the same gate
shape as the lead-time and regime models — passes.

**Read the tie column, not just the reduction.** Against the two moment-based rules the policy
wins on 27% of series, loses on 15%, and **ties on 58%**: on a panel that is 76% zeros the
fractile and the normal safety stock frequently round to the same integer. The saving is real
and significant in the paired mean; it is not a win on most SKUs, and saying "beats the
textbook rule by 4%" without that sentence would be the flattering version.

**The headline this was built for, now measured rather than argued.** Give every method on the
demand leaderboard the *same* newsvendor rule and rank by realised decision cost:

| method | decision cost | MASE |
|---|---:|---:|
| `tsb` | **0.03810** | 1.0755 |
| `climatology` | 0.04406 | 1.1359 |
| `naive_last` | 0.05051 | 1.0924 |
| `sba` | 0.05400 | 1.3246 |
| `croston` | 0.05415 | 1.3558 |
| `zero` | 0.05977 | **0.7667** |

`zero` — forecasting nothing every period, which **wins MASE and RMSSE outright** on this
panel (§1.1) — produces the **worst decision cost of the six**, 57% above the winner. The
method that wins on accuracy is not the method that wins on decision cost, and the repo can
now show it in dollars instead of asserting it. (The MASE column is recomputed in the same
pass from each method's own point forecaster via `intermittent.mase`, and a test asserts it
reproduces the published leaderboard to 1%.)

**Two honest failures, kept as tests rather than tuned away.**

- **`shortage_mode="line_down"` (τ = 0.9931) does not pass the gate.** Margin over the
  toughest baseline is +0.00149 with 95% CI **[−0.00330, +0.00600]** — indistinguishable from
  zero. The longest training window here is 45 monthly observations, so the finest empirical
  quantile resolution is 1/45 = 0.022; a 99.3rd percentile is an *extrapolation of the assumed
  compound-Bernoulli tail*, not a quantile the data can resolve. The endpoint still returns it,
  with that warning attached, and the gate still refuses it.
- **At a 3-month review period (τ = 0.7059) the policy LOSES** to ordering the point forecast
  by 0.00351/SKU-period, CI [−0.00479, −0.00221]. Aggregating the monthly law to a quarter is
  an exact convolution *only* under the model's i.i.d.-across-periods assumption; the loss is
  the price of that assumption on serially-clustered spare-parts demand. At L = 6 it wins again
  (+0.00548, CI [+0.00334, +0.00761]). The advantage is a function of the review period, and
  quoting the L = 1 number without that is quoting the best cell of a sweep.

**The negative control, which is the test that matters.** Score every series against *another*
series' predictive distribution — same cost function, same panel, same policy, only the
series↔forecast pairing destroyed. Cost rises 0.0381 → 0.0559 (**+47%**) and the gate fails.
Without it, none of the above would be evidence: an asymmetric cost rewards almost any positive
order over ordering nothing, so a harness like this will happily produce a confident saving
from a forecast containing no information at all.

**A defect in our own forecasting code, found by pointing a decision at it — and since fixed.**
`intermittent._size_shape` computed the negative-binomial shape as `r = m²/(v − m)`. When the
non-zero order sizes were overdispersed by a few parts in 10¹⁶, `r` evaluated to ~10¹⁶ instead
of collapsing to the Poisson limit, and `_size_pmf`'s mean-matching search returned a size law
whose mean was tens of times too large — violating the invariant `E[pmf] == the method's own
point forecast` that `docs/INTERMITTENT_DEMAND.md` says holds "by construction". It fired on
**3 of 8,022 (series, origin) pairs — 0.04%**. A scoring rule barely notices a defect like this:
CRPS gets a little worse. A *decision* reads the 0.878 quantile of that broken law and **orders
70 units where the right answer is 2**, and two such series were measured at the time to be
worth ~24% of the margin over the toughest baseline when scored with the defect present.
`newsvendor.predictive_distribution` refused any law that violated the invariant, and the
evaluation dropped and counted those series while the underlying defect in
`app/ml/intermittent.py` remained.

**The fix has since landed** — `_size_shape` now guards the Poisson limit *numerically*, not
only exactly (any dispersion below one part in 10⁹ of the mean now collapses to it), in the
same change that shipped this newsvendor layer. Every number in this section is measured
against that fixed code: `n_series_dropped_pmf_invariant = 0` on the current panel, which is
why it is 2,646 series above rather than 2,643 — an earlier draft of this section described the
pre-fix run and was never re-generated after the fix landed, which is exactly the silent-drift
failure `test_newsvendor_docs_match_artifact.py` now exists to catch. The invariant guard stays
in `newsvendor.predictive_distribution` regardless of whether it is currently firing — it is
insurance against a recurrence, not dead code — and the general lesson holds either way: a
proper scoring rule averages a bad tail away, a decision reads it.

**Scarf's min-max newsvendor shipped with it** — `scarf_order_quantity`, closed form
`μ + (σ/2)(√(Cu/Co) − √(Co/Cu))`, worst-case optimal over *every* law with those two moments.
It is the same distributionally-robust move `stochastic.py` makes with CVaR, in one dimension
and with no solver, and it turned out to be the toughest baseline in the table.

**Deliberately NOT built: the decision-focused *learning* half.** Ban & Rudin's LightGBM
`objective="quantile", alpha=τ` is a *feature-conditional* quantile regressor. The Monash panel
ships series ids and 51 monthly points with **no covariates**, so there is nothing to condition
on and a fitted quantile regressor would be estimating a constant; `lightgbm` is also not in
the deploy image. The unconditional fractile of a fitted predictive law is the whole of what
this data supports, and saying so is worth more than importing a gradient booster to predict a
number we already have in closed form.

- Ban, G-Y. & Rudin, C. (2019). "The Big Data Newsvendor: Practical Insights from Machine
  Learning." *Operations Research* 67(1):90–108.
  https://pubsonline.informs.org/doi/10.1287/opre.2018.1757
- Bertsimas, D. & Kallus, N. (2020). "From Predictive to Prescriptive Analytics." *Management
  Science* 66(3):1025–1044.
- Oroojlooyjadid, Snyder & Takáč (2020). "Applying Deep Learning to the Newsvendor Problem."
  *IISE Transactions* 52(4):444–463. https://arxiv.org/abs/1607.02177
- Arrow, K.J., Harris, T. & Marschak, J. (1951). "Optimal Inventory Policy." *Econometrica*
  19(3):250–272 — the critical-fractile result itself.
- Scarf, H. (1958). "A Min-Max Solution of an Inventory Problem", in Arrow, Karlin & Scarf,
  *Studies in the Mathematical Theory of Inventory and Production*, ch. 12. Orig. RAND P-910,
  https://www.rand.org/pubs/papers/P910.html
- Snyder, L.V. & Daskin, M.S. (2005). "Reliable Facility Location Models." *Transportation
  Science* 39(3):400–416 — the line-down escalation multiple.

---

## Track 4 — Evaluation integrity (cuts across everything)

### 4.1 Real-time / vintage data via ALFRED — **done (2026-08-16), and it was not optional**
*Shipped. Data support: yes, keyless, verified against the live service.*

Census M3 and FRED series are **revised after first publication**. Backtesting on the currently
revised series uses data that did not exist at the forecast origin — an optimistic bias, and a
leakage class distinct from the usual ML ones. ALFRED (ArchivaL FRED, https://alfred.stlouisfed.org)
serves every historical vintage through the same API via a `vintage_dates` parameter.

**This entry was previously filed as "high novelty, low effort" — a nice-to-have — and
`docs/archive/ML_API_PUSH_PLAN.md` dismissed it outright as "genuinely novel, but tangential to the
decision spine." That judgement was wrong, and the repo proved it wrong the hard way.**

`seeds/run_forecast_backtest.py` refetched `A34SNO` live on every run and overwrote its own
cache. That is a write-through cache, not a pin, so nothing published from it was reproducible
past the next Census revision. The published Prophet-vs-Chronos headline
(*"Prophet 0.0266 beats Chronos 0.0293 — the foundation model lost, and I published it"*)
**silently inverted**: on the current vintage Prophet scores 0.0313 and Chronos wins. Same code,
same harness, same seed — different data. Vintage pinning was never a garnish on the evaluation;
it was **the precondition for any published number being checkable at all**.

**What shipped** (`app/ml/fred_client.py`, `seeds/macro_demand.py`):

- `--as-of YYYY-MM-DD` on both `run_forecast_backtest` and `run_chronos_benchmark`, resolving
  through one shared loader so the two scripts cannot diverge on inputs again.
- Vintages fetched keyless from `alfredgraph.csv` and committed verbatim under
  `seeds/data/a34sno_vintages/`, hash-recorded in `VINTAGE_SHA256`. A pinned run does **no**
  network I/O.
- The value column ALFRED returns is named `A34SNO_<YYYYMMDD>`; the client asserts on it, so an
  ignored pin fails loudly instead of passing as honoured.
- Nothing overwrites the legacy snapshot unless a human passes `--refresh-cache`.
- Every artifact records the vintage, the file hash and an observation-values hash.

**The finding it bought** — measured, not asserted, in the vintage-sensitivity table of
[`CHRONOS_BENCHMARK.md`](CHRONOS_BENCHMARK.md): the Prophet-vs-Chronos gap on the pinned vintage
is **0.0020 WAPE**, while re-scoring the *same* Prophet across vintages of the *same* series
moves it by **0.0047 WAPE**. The revision effect is more than twice the model effect, so the
ranking of these two models on this series is not a robust finding. That is a stronger and more
honest result than either "Prophet won" or "Chronos won".

**The transferable lesson:** "reproducible" is a property of the *inputs*, not of the code. A
deterministic harness over a mutable source is not reproducible, and it will not tell you when it
stops being right. Verify the vintage; hash the bytes; pin by a rule chosen before you see the
answer (here: *the latest vintage on the day of republication*), not by which vintage flatters.

- Croushore, D. & Stark, T. (2001). "A real-time data set for macroeconomists."
  *J. Econometrics* 105(1):111–130. Philadelphia Fed RTDSM:
  https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/real-time-data-set-for-macroeconomists
- "Forecast Evaluation for Data Scientists: Common Pitfalls and Best Practices,"
  https://arxiv.org/abs/2203.10716

**The real-time study is done too — and X was not small.** The original framing under this
heading was "quantify how much using revised data flattered WAPE." Built and published: each
rolling origin now trains ONLY on the ALFRED vintage that existed on that origin's date, scored
against one common reference vintage. Origin vintages `2023-08-01` / `2024-08-01` / `2025-08-01`
contain exactly 162 / 174 / 186 observations — precisely the training sizes the pseudo-real-time
origins produce — so training lengths, target months and actuals are identical across the two
protocols and the difference is attributable to **data revision alone**.

| Model | Real-time WAPE | Pseudo real-time WAPE | Revised data flatters by |
|---|---:|---:|---:|
| Chronos (zero-shot) | 0.0364 | 0.0293 | **19.5%** |
| Prophet (seasonal) | 0.0413 | 0.0313 | **24.2%** |
| Seasonal-naive | 0.0587 | 0.0480 | **18.2%** |

Every model looks ~20% better than it could actually have been. Prophet's skill score over the
naive baseline drops from +34.8% to **+29.6%**. **Any backtest on a revised macro series that
does not pin per-origin vintages is quoting a number the forecaster could not have achieved** —
and this repo was doing exactly that until 2026-08-16. Generated by
`seeds.run_forecast_backtest` / `seeds.run_chronos_benchmark`; protocol lives in
`app/ml/backtest.py::backtest_folds`, which both the pseudo and real-time harnesses run, so they
cannot drift apart.

- Croushore, D. & Stark, T. (2001). "A real-time data set for macroeconomists."
  *J. Econometrics* 105(1):111–130 — the canonical statement of why this matters.

### 4.2 Reframe the Chronos result with contamination awareness
*Effort: ~0.5 day, mostly writing. Data support: n/a.*

**Do not add a fourth foundation model** — a 3-window benchmark cannot support it. The valuable
move is the skeptical framing: A34SNO is a public, FRED-mirrored macro series that Chronos's
pretraining corpus may contain, so a zero-shot result on it could partly reflect memorization.
Report it as illustrative, not as evidence of generalization.

- Karaouli et al. (2025). "How Foundational are Foundation Models for Time Series Forecasting?"
  *NeurIPS 2025*, https://arxiv.org/abs/2510.00742
- Toner et al. (2025). "Performance of Zero-Shot Time Series Foundation Models on Cloud Data,"
  ICLR 2025 workshop, https://arxiv.org/abs/2502.12944
- fev-bench (Amazon/AWS), https://arxiv.org/abs/2509.26468 — reports win rates with **bootstrapped
  confidence intervals** rather than point ranks; worth copying.
- GIFT-Eval, https://arxiv.org/abs/2410.10393 — ships a certified non-leaking pretraining corpus.

**Genuine opening:** no one appears to have rigorously benchmarked TSFMs zero-shot on intermittent
spare-parts demand — GIFT-Eval and fev-bench are dominated by dense series. Chronos-2
(https://arxiv.org/abs/2510.15821) against Croston/SBA/TSB on 2,674 car-parts series, scored with
CRPS and tested with Nemenyi, would be novel *and* fix the sample-size problem at once.

---

## Do NOT build — and say why, in the docs

Stating these is worth real credibility.

- **SPO+ / PyEPO.** Elmachtoub & Grigas (2022), *Management Science* 68(1):9–26,
  https://arxiv.org/abs/1710.08005 — applies only when uncertainty sits in the **objective
  coefficients** with a fixed feasible region. Our uncertain demand sits in **constraints**, so
  plain SPO+ is the wrong tool. The constraint-side extension (https://arxiv.org/abs/2311.08022)
  is research-grade. Survey: Mandi et al. (2024), *JAIR* 81:1623–1701,
  https://arxiv.org/abs/2307.13565. **Being able to say why it does not apply beats bolting it on.**
  Counterweight worth citing: Zalando's June 2025 production inventory system
  (https://engineering.zalando.com/posts/2025/06/inventory-optimisation-system.html) runs
  LightGBM + Monte Carlo + black-box optimization at 5M SKUs — plain predict-then-optimize, not DFL.
- **Causal inference / DML / causal forests on the distributor panel.** No randomization, no
  instrument, no treatment/control panel. Chernozhukov et al. (2018) and Wager & Athey (2018)
  both need unconfoundedness we cannot defend. *Narrow exception:* the observed ST lead-time
  extension is a genuine dated shock, and an event-study with non-ST parts as controls would be
  defensible — but with only the two snapshots that bracket the shock (2026-07-01 and
  2026-08-15) it is a descriptive before/after, and must be labelled so.
- **Hierarchical reconciliation (MinT/ERM).** No hierarchy to reconcile — Monash car parts ships
  series IDs with no product taxonomy, and temporal reconciliation over 51 months yields 4 annual
  points.
- **Wasserstein DRO** (Mohajerin Esfahani & Kuhn, https://arxiv.org/abs/1505.05116). Highest
  technical ceiling, but the Euclidean-ball form needs an SOCP solver and CP-SAT cannot do conic
  constraints — a second solver stack. Stretch goal only.
- **SPCI** (Xu & Xie, ICML 2023) — fits a secondary quantile regressor on lagged residuals;
  nowhere near enough residual history on a 197-point series.
- **Any deep learning on this data.** The MLP already in our own model zoo scores
  `cv_r2_mean = −0.791` on family-grouped folds ([`leakage_progression.json`](leakage_progression.json)).
  That is the answer.
- **Hyperparameter optimization (Optuna).** Fold spread on the family-grouped folds is
  **±0.246 R²** (sd over 50 folds; range −0.67 to +0.39) against a champion mean of +0.073.
  Any tuning gain is a fraction of the noise band — a number we could not defend.
- **SHAP on the 324-feature lead-time model** (324 = the served 2026-09-03 artifact, which is
  what the panel on disk recomputes to)**.** Most one-hot columns carry 1–5 observations; it
  would narrate noise with a nice waterfall. SHAP on a 6-feature model, or on manufacturer
  effects from the mixed model, is defensible.
- **Censored / Tobit / AFT regression.** Already retired: the censoring was a 75-row artifact,
  and only 5 of 742 new rows sit at the 30-week ceiling.

---

## Suggested sequencing

**If only three:** 1.2 (significance testing), 1.1 (distributional re-scoring), 3.4 (newsvendor).
All three run on the car-parts panel at genuine scale and tell one coherent story:
*"I found my metric was measuring the wrong functional, tested the ranking properly across 2,658
series, then showed the forecast that wins on accuracy is not the forecast that wins on decision
cost."*

**Week 1:** 1.2 → 1.1 → 3.1 (free) → 1.3 (deletion).
**Week 2:** ~~3.4~~ (done, above) → 2.1 → 3.3.
**Opportunistic:** 4.1 (cheap, high novelty), 4.2 (mostly writing), 3.2 (second frontier chart).
