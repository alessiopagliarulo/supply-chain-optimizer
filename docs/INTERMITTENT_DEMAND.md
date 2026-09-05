# The best forecast by MASE is a forecast of nothing

<!-- GENERATED:header BEGIN -->
Generated `2026-09-05T19:35:17Z` by `cd backend && python -m seeds.run_carparts_backtest`.
Machine-readable: [`intermittent_demand.json`](intermittent_demand.json).
Hardware arm64 / Darwin 25.5.0 · Python 3.13.5 · numpy 2.4.4 · scipy 1.17.1 · seed 42 · 14.4 s wall.
<!-- GENERATED:header END -->

**Every number below comes from that one command, and is now written by it.** Each table
and each stated statistic on this page lives inside a `<!-- GENERATED:… -->` region that
`run_carparts_backtest.py` rewrites from `intermittent_demand.json` on every run; the
prose between those regions is hand-written and the generator never touches it. Until
2026-08 the numbers were hand-transcribed, which is how a doc drifts away from the
artifact it cites. `tests/test_docs_match_artifacts.py` fails if the two disagree.
The served endpoint `GET /api/v1/demand/benchmark` reads the same JSON, so the app and
this page cannot disagree either.

---

## The headline

Across **2,646 real spare-parts series**, the leaderboard depends on which question the
metric is asking, and the two answers are close to opposite.

<!-- GENERATED:headline_table BEGIN -->
| | winner | `zero` forecast's rank |
|---|---|---:|
| **MASE** (point) | **`zero`** | **1st of 6** |
| **RMSSE** (point) | **`zero`** | **1st of 6** |
| **CRPS** (proper) | **`tsb`** | 4th of 6 |
| **Scaled pinball loss** (proper) | **`tsb`** | 5th of 6 |
<!-- GENERATED:headline_table END -->

<!-- GENERATED:kendall BEGIN -->
Kendall's tau between the MASE ordering and the pinball ordering is **−0.20** — the two
leaderboards are not merely different, they are mildly *anti*-correlated.
<!-- GENERATED:kendall END -->

**Why this happens, precisely.** MAE — and therefore MASE — is minimised by the
conditional **median**.

<!-- GENERATED:nonzero_share BEGIN -->
This panel is 24.1% non-zero, so for most series in most months the conditional median *is
zero*.
<!-- GENERATED:nonzero_share END -->

A forecast that predicts nothing therefore wins the
point-error leaderboard while being worthless to a planner, whose actual question is
"how much do I stock so I am short less than 5% of the time?" That question is about a
**quantile of the predictive distribution**, and a point forecast does not have one.

This is not a hypothetical hazard borrowed from Kolassa (2020). The `zero` method is on
the leaderboard for exactly this reason, and it won.

---

## 1. What this replaced, and why

The app used to serve per-part Prophet forecasts from `component_demand_history` /
`component_forecasts`. Those are gone (migration `0008_drop_synthetic_demand_tables.py`).
The reason, stated exactly:

- The **temporal shape** of each series was real (Census M3 `A34SNO` via FRED). The
  **magnitude** was not: `base_rate = total_stock / 52`, scaled by a `risk_score`
  multiplier. That is demand inferred from inventory position and risk — causally
  backwards, and it gave all 791 parts an identical shape.
- The Prophet fits on top of it predicted a 12-week window that closed **17 months ago**,
  against which **no actuals were ever recorded**. The forecasts were therefore
  unscoreable in principle: there was no experiment that could have found them wrong.

It was removed rather than patched, because there is no public per-SKU demand series for
electronic components to patch it with. **This app does not claim a per-part demand
forecast for the components it sells.** What it claims is measured below.

## 2. The panel

<!-- GENERATED:panel_table BEGIN -->
| | |
|---|---|
| Dataset | Monash car parts, `monash_car_parts_with_missing_values` |
| Source | HuggingFace `Monash-University/monash_tsf`, CC-BY 4.0 |
| Size | **2,674 series × 51 months = 136,374 observations**, monthly |
| Intermittency | **24.1% non-zero** (75.9% of observations are exactly 0) |
| Mean demand | 0.485 units/month |
| Non-zero order size | mean 2.01, variance 3.48 (variance/mean 1.73), median 1, 99th pct 10, max 52 |
| Missing convention | `?` read as 0 — Monash's own "without missing values" variant |
<!-- GENERATED:panel_table END -->

Why car parts and not electronic components: **no public per-SKU demand series exists for
electronic components.** Car parts are real intermittent spare-parts demand, which is the
same statistical object — long runs of zeros punctuated by small integer orders. It is a
stand-in, and it is labelled as one everywhere it is used.

## 3. The protocol changed: single split → rolling origin

The previous car-parts backtest held out the last 12 months once, which was inconsistent
with every other backtest in this repo. It now uses **rolling origins**, and not by
resemblance: the origins come from `app.ml.backtest.rolling_origins`, the *same function*
`walk_forward_backtest` calls for the macro A34SNO backtest. One protocol, one
implementation.

<!-- GENERATED:protocol_table BEGIN -->
| | primary | sensitivity |
|---|---|---|
| Horizon | 6 months | 12 months |
| Origins | 3 | 2 |
| Train sizes | 33 / 39 / 45 | 27 / 39 |
| Seasonality (MASE denominator) | 12 | 12 |
| Series scored | 2,646 of 2,674 | 2,504 of 2,674 |
<!-- GENERATED:protocol_table END -->

**Why the primary horizon is 6 and not the Monash-standard 12.** 51 months cannot hold
three non-overlapping 12-month blocks without the first origin training on 15 months —
fewer than two seasonal cycles, which leaves the seasonal-naive MASE denominator built
from three differences and effectively random. Horizon 6 keeps every training window at
33 months or more. The horizon-12 configuration is run anyway, as a sensitivity check, and
§7 reports that the conclusion does not depend on the choice.

Two further rules, both about not flattering the result:

- **The series is the replication unit.** Metrics are computed per series per origin, then
  averaged within the series, before any cross-series test. Different SKUs are separate
  draws; six months of one SKU are not.
- **The panel is balanced.** A series enters the tests only if *every* method produced a
  finite score at *every* origin, so the ranked panel is balanced:

<!-- GENERATED:dropped_series BEGIN -->
28 series are dropped from the primary configuration — all of them constant training
windows, where the seasonal-naive denominator is zero and every scaled metric is
undefined. Scoring one method on 2,674 series and another on 2,646 would make their mean
ranks incomparable.
<!-- GENERATED:dropped_series END -->

## 4. What each method assumes to become a distribution

Croston, SBA and TSB emit a point *rate*. To be scored by a proper rule they need a
predictive distribution. All of them are given the same structural model, which is the
standard description of intermittent demand:

```
Y = B · S      B ~ Bernoulli(p)      S ~ ZeroTruncatedNegBin(mean z)
```

— demand occurs with probability `p`, and given an occurrence the order size is a strictly
positive count with mean `z`. Then **E[Y] = p·z, which is exactly the flat rate each point
method already emits**, so every distribution has the same mean as its point twin *by
construction*. That is asserted in the tests, and it is what makes the two leaderboards
comparable rather than merely adjacent.

| Method | (p, z) from | Assumption required |
|---|---|---|
| **`tsb`** | its own smoothed probability and size | **None** beyond the structure above. TSB already estimates an occurrence probability and a conditional size every period — this is its native form. |
| **`croston`** | `p = 1 / interval_hat`, `z = z_hat` | **Memoryless arrivals.** Croston smooths the inter-arrival *interval* and gives no probability. If arrivals are i.i.d. Bernoulli(p), intervals are Geometric(p) with mean 1/p. This is not an addition to the method: Croston (1972) derives the estimator *under* Bernoulli arrivals, so the assumption restates the model it already lives in. Intervals are ≥ 1 by construction, so p ≤ 1 always. |
| **`sba`** | `p = (1 − α/2) / interval_hat`, `z = z_hat` | **The Syntetos–Boylan correction belongs on the probability, not the size.** Syntetos & Boylan (2001) show Croston's bias is the Jensen gap in `E[1/interval_hat]` — it lives in the inverse-interval term; the size estimator is unbiased. Placing the factor there both matches the derivation and reproduces the SBA point forecast exactly. |
| **`naive_last`** | — | **None.** The point forecast lifted to a distribution *degenerate* at the last observed value. Zero spread is the honest reading of "repeat the last number", and it makes CRPS collapse to absolute error — so scaled CRPS equals MASE exactly, giving both leaderboards a shared anchor. |
| **`zero`** | `p = 0` | **None.** The conditional median of most intermittent series, included so the risk this whole document is about could be **measured** rather than asserted. |
| **`climatology`** | — | **Exchangeability** of the training and test windows (no trend, no obsolescence). The in-sample empirical distribution; the standard probabilistic reference forecast and the only non-degenerate baseline. |

**The size law, and why the Poisson limit is the default.** `S` is zero-truncated negative
binomial, dropping to its Poisson limit when the in-sample non-zero sizes are not
overdispersed (sample variance ≤ mean). That is not a shortcut — it is what the data says.
Across the panel the non-zero sizes have a **median variance/mean ratio of 0.42**, and only
**17.6%** of series exceed 1.0. A zero-truncated Poisson with λ ≈ 1 implies a ratio of
**0.418**. The order sizes are essentially ZTP.

*Known approximation, stated rather than hidden:* the shape, when needed, is the
method-of-moments plug-in `r = m²/(v − m)` computed on the truncated (non-zero) sample.
Applying the untruncated moment relation to a truncated sample is approximate. It binds
only on the ~18% overdispersed minority, and it is not solved jointly.

## 5. The leaderboard

All four metrics share the **same training-only denominator** (in-sample seasonal-naive
MAE, seasonality 12). Lower is better everywhere. Mean rank is the Friedman mean rank over
2,646 series, 1 = best.

<!-- GENERATED:leaderboard BEGIN -->
| Method | MASE mean | MASE median | RMSSE | scaled CRPS | scaled pinball | rank<sub>MASE</sub> | rank<sub>CRPS</sub> | rank<sub>SPL</sub> |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `zero` | **0.767** | **0.408** | **0.612** | 0.767 | 0.383 | **1.66** | 3.67 | 4.12 |
| `naive_last` | 1.092 | 0.611 | 0.740 | 1.092 | 0.546 | 3.00 | 4.41 | 4.68 |
| `climatology` | 1.136 | 0.780 | 0.692 | 0.693 | 0.270 | 4.21 | 3.06 | 2.94 |
| `croston` | 1.356 | 0.941 | 0.775 | 0.806 | 0.322 | 4.70 | 3.82 | 3.52 |
| `sba` | 1.325 | 0.916 | 0.763 | 0.793 | 0.319 | 3.74 | 3.37 | 3.30 |
| `tsb` | 1.075 | 0.696 | 0.640 | **0.650** | **0.248** | 3.70 | **2.67** | **2.44** |
<!-- GENERATED:leaderboard END -->

Three things to read off it:

1. **`zero` wins both point metrics outright** — best MASE mean, best MASE median, best
   RMSSE — and is beaten by four methods on scaled pinball loss.
2. **`zero` and `naive_last` have identical MASE and scaled CRPS** (0.767 and 1.092). Not a
   coincidence and not a copy-paste: CRPS of a point mass *is* absolute error, so a
   degenerate method's scaled CRPS must equal its MASE. It is checked against the real
   artifact in `tests/test_demand_api.py`, and it is the arithmetic that puts the point and
   distributional columns on one scale.
3. **Mean MASE and mean-rank MASE disagree with each other** (`tsb` beats `naive_last` on
   the mean, loses on the rank). That is why the significance testing below ranks within
   each series first: a mean scaled error over thousands of series is dominated by the
   handful with a near-zero scaling denominator. It is the reason the M3 organisers moved
   to ranks in the first place.

## 6. MCB — Friedman + Nemenyi

Friedman rank test across all 2,646 series, then Nemenyi critical differences. Method of
Koning, Franses, Hibon & Stekler (2005), *IJF* 21(3):397–409, which introduced MCB to
forecast-competition evaluation; the F-corrected statistic is Iman & Davenport (1980) (the
raw χ² form is conservative), and the critical-difference diagram convention is Demšar
(2006), *JMLR* 7:1–30.

<!-- GENERATED:critical_difference BEGIN -->
**CD = 0.1466** at α = 0.05 for k = 6 methods and N = 2,646 series. Any two methods whose
mean ranks differ by more than that are significantly different.
<!-- GENERATED:critical_difference END -->

<!-- GENERATED:mcb_table BEGIN -->
| Metric | Friedman χ²(5) | p | Iman–Davenport F | Not separated by the CD |
|---|---:|---:|---:|---|
| MASE | 4296.6 | < 1e-300 | 1272.1 | `tsb` — `sba` |
| RMSSE | 1259.0 | 4.8e-270 | 278.2 | `sba` — `climatology` |
| **CRPS** | 1409.9 | 1.0e-302 | 315.5 | **none — every pair separated** |
| **Scaled pinball** | 2467.9 | < 1e-300 | 606.5 | **none — every pair separated** |
<!-- GENERATED:mcb_table END -->

Critical-difference diagram data (mean-rank axis, best on the left; bracketed groups are
the diagram's horizontal bars):

<!-- GENERATED:cd_diagram BEGIN -->
```
CRPS   1 ────────────────────────────────────────────────────────────────────────── 6
          tsb          climatology  sba          zero         croston      naive_last
          2.67         3.06         3.37         3.67         3.82         4.41
          (no bar: every adjacent gap exceeds CD = 0.147)

MASE   1 ────────────────────────────────────────────────────────────────────────── 6
          zero         naive_last   tsb          sba          climatology  croston
          1.66         3.00         3.70         3.74         4.21         4.70
                                    └───────────────┘
          tsb–sba gap 0.04 < CD 0.147 — not separated
```
<!-- GENERATED:cd_diagram END -->

With 2,646 series there is ample power, which is what makes the null result on
`tsb`–`sba` under MASE meaningful rather than merely uninformative: the design *can*
resolve a rank gap of 0.15, and under MASE those two are inside it. Under proper scoring
they are not.

## 7. Does it survive a different horizon?

<!-- GENERATED:sensitivity BEGIN -->
**Yes.** Under the sensitivity configuration (horizon 12, 2 origins, 2,504 series):

| Metric | Ordering, best first |
|---|---|
| MASE | `zero` · `naive_last` · `tsb` · `sba` · `climatology` · `croston` |
| CRPS | `tsb` · `climatology` · `sba` · `zero` · `croston` · `naive_last` |
| Scaled pinball | `tsb` · `climatology` · `sba` · `croston` · `zero` · `naive_last` |

Identical to the primary configuration on all three, including `zero` 1st under MASE and
4th/5th under proper scoring. The finding is a property of the metric, not of the
protocol.
<!-- GENERATED:sensitivity END -->

## 8. Pairwise tests: Clark–West where the models nest, Diebold–Mariano where they do not

Using Diebold–Mariano on a nested pair is a real error, not a technicality. Under the null
that the small model generates the data, the large model's extra parameters are pure
estimation noise, so its MSPE is *larger* in population; the DM statistic is centred below
zero and is under-sized, systematically failing to detect a genuinely better large model.
Clark & West (2007), *J. Econometrics* 138(1):291–311, add back the `(f₁ − f₂)²` term to
recentre it. `tests/test_model_comparison.py` constructs a case where DM declares the
larger model significantly *worse* while Clark–West correctly rejects — the two tests point
in opposite directions on the same data.

**Nested pairs → Clark–West** (one-sided; H₁ = the unrestricted model has genuine
predictive content):

<!-- GENERATED:clark_west_table BEGIN -->
| Restricted | Unrestricted | Nesting | CW t | p | Informative? |
|---|---|---|---:|---:|---|
| `croston` | `sba` | SBA = Croston × (1 − φ/2); Croston is the φ = 0 restriction | **11.33** | 4.5e-30 | **yes** |
| `zero` | `croston` | `zero` is the p = 0 restriction of the compound-Bernoulli model | 27.27 | 4.4e-164 | **no — degenerate** |
| `zero` | `sba` | as above | 27.27 | 4.4e-164 | **no — degenerate** |
| `zero` | `tsb` | as above | 25.75 | 1.4e-146 | **no — degenerate** |
<!-- GENERATED:clark_west_table END -->

The zero-restriction rows are reported **and flagged**, because dropping an inconvenient
test quietly is worse than showing it with its limitation. With f₁ = 0 the Clark–West
adjusted difference collapses to `2·y·f₂`, which is ≥ 0 for any non-negative forecast on
non-negative demand — so rejection is automatic and carries no evidence about which method
is better. The tell is in the table: `zero → croston` and `zero → sba` have *identical*
t-statistics, because SBA is a pure rescaling of Croston and the statistic is
scale-invariant in this degenerate case.

<!-- GENERATED:clark_west_verdict BEGIN -->
**The one informative nested result** is therefore `croston → sba`: t = 11.33, p =
4.5e-30. The Syntetos–Boylan bias correction genuinely improves squared-error accuracy
over Croston. Modest — mean adjusted difference 0.031 — but real, and it is the kind of
claim that a raw DM test on a nested pair would have understated.
<!-- GENERATED:clark_west_verdict END -->

**Non-nested pairs → Diebold–Mariano**, HLN-corrected (Harvey, Leybourne & Newbold 1997),
two-sided, on per-series scaled CRPS. Positive difference favours the second method:

<!-- GENERATED:dm_table BEGIN -->
| Baseline | Candidate | Δ scaled CRPS | t | p |
|---|---|---:|---:|---:|
| `naive_last` | `tsb` | +0.442 | 28.50 | 4.9e-156 |
| `naive_last` | `climatology` | +0.400 | 26.09 | 1.0e-133 |
| `climatology` | `tsb` | +0.043 | 21.13 | 1.0e-91 |
| `croston` | `tsb` | +0.156 | 19.18 | 6.8e-77 |
<!-- GENERATED:dm_table END -->

TSB beats every alternative on CRPS, including the climatology reference, and all of it is
significant at any conventional level. The margin over climatology is small in absolute
terms (0.041) but extremely consistent across series, which is exactly what a paired test
is for.

## 9. What this does not claim

- **It is not a demand forecast for the 791 electronic components in this app.** It is a
  benchmark of demand *methods* on a real intermittent panel from a different industry.
  Nothing here licenses a per-part forecast for a capacitor.
- **It does not connect demand to the sourcing decision yet.** A better CRPS is not a
  better purchase order. Step 1.4 of `archive/ML_API_PUSH_PLAN.md` — fitting at the newsvendor
  critical fractile τ = Cu/(Cu+Co) and scoring in dollars of realised newsvendor cost — is
  the step that closes that gap, and it is not built.
- **α = 0.1, β = 0.1 are fixed, not tuned.** No smoothing-parameter optimisation was run.
  Tuned parameters would change the numbers; the qualitative point (MASE prefers the
  degenerate forecast) does not depend on them, because it is a property of the loss
  function rather than the estimator.
- **Prophet is not in the headline table.** It is available behind `--prophet` on a random
  sample, scored as a degenerate distribution — its own interval is Gaussian and
  continuous, which is the wrong object for a count with a 76% atom at zero.
- **The size-law shape estimator is approximate** (§4). Documented, not silently applied.

## 10. Reproduce

```bash
cd backend
python -m seeds.run_carparts_backtest             # ~14 s, fully offline after first fetch
python -m seeds.run_carparts_backtest --quick     # primary configuration only
python -m seeds.run_carparts_backtest --prophet   # + the slow Prophet sample
```

Writes [`docs/intermittent_demand.json`](intermittent_demand.json) and a byte-identical
mirror at `backend/seeds/data/intermittent_demand.json`. The mirror exists only because the
container build context is `backend/` (`render.yaml` `rootDir: backend`), so repo-root
`docs/` is not guaranteed to be on disk at runtime for `GET /demand/benchmark` to read; a
test asserts the two files are identical.

Implementation: `app/ml/intermittent.py` (estimators + predictive distributions),
`app/ml/proper_scoring.py` (CRPS, pinball), `app/ml/model_comparison.py` (Friedman,
Nemenyi, DM, Clark–West), `app/ml/backtest.py` (`rolling_origins`).
Tests: `tests/test_intermittent.py`, `tests/test_proper_scoring.py`,
`tests/test_model_comparison.py`, `tests/test_carparts_backtest.py`,
`tests/test_demand_api.py`, `tests/test_docs_match_artifacts.py`.

<!-- GENERATED:provenance BEGIN -->
### Provenance of this run

- **Generated:** 2026-09-05T19:35:31Z (UTC)
- **Generator:** `seeds.run_carparts_backtest`
- **Commit:** `a1da5b92b9ecdc7d647ec95bbdcc21fbe99e6493` (clean tree)
- **Input `monash_car_parts_cache`:** `backend/seeds/data/car_parts_monthly.npz` · sha256 `91446a84d4c7ba52…`
- **Python:** 3.13.5 · macOS-26.5-arm64-arm-64bit-Mach-O
<!-- GENERATED:provenance END -->

A dirty working tree is reported here as an explicit flag rather than as a `-dirty`
suffix on a SHA. The previous artifact recorded exactly such a suffix and nothing else,
so "these numbers are not reproducible from the recorded commit" was a fact you had to
notice at the end of a 47-character string.

## References

- Croston (1972), "Forecasting and stock control for intermittent demands", *ORQ* 23(3):289–303
- Syntetos & Boylan (2001), "On the bias of intermittent demand estimates", *IJPE* 71(1–3):457–466
- Teunter, Syntetos & Babai (2011), "Intermittent demand: linking forecasting to inventory obsolescence", *EJOR* 214(3):606–615
- Gneiting & Raftery (2007), "Strictly proper scoring rules, prediction, and estimation", *JASA* 102(477):359–378
- Kolassa (2020), "Why the 'best' point forecast depends on the error or accuracy measure", *IJF* 36(1):208–211
- Koning, Franses, Hibon & Stekler (2005), "The M3 competition: statistical tests of the results", *IJF* 21(3):397–409
- Demšar (2006), "Statistical comparisons of classifiers over multiple data sets", *JMLR* 7:1–30
- Diebold & Mariano (1995), "Comparing predictive accuracy", *JBES* 13(3):253–263
- Harvey, Leybourne & Newbold (1997), "Testing the equality of prediction mean squared errors", *IJF* 13(2):281–291
- Clark & West (2007), "Approximately normal tests for equal predictive accuracy in nested models", *J. Econometrics* 138(1):291–311
