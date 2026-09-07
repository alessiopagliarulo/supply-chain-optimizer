# Project Overview — what this is, what backs it, how to talk about it

Reference for interviews and applications. Kept current; every number here is reproducible from
a script in the repo.

---

## One sentence

**Given a bill of materials, decide which suppliers to buy from — at what quantity, balancing
cost against disruption risk.**

## The structure

Three questions feed one decision:

```
   How many do we need?  (demand)     ──┐
   When will it arrive?  (lead time)  ──┼──►  WHICH SUPPLIERS, AT WHAT QUANTITY?
   What if a supplier fails?  (risk)  ──┘
```

---

## Features → data → tools → concept

| Feature | What actually backs it | Tools | Concept demonstrated |
|---|---|---|---|
| Supplier sourcing optimizer | 791 components, 92 distributors, 8,176 real offers (Nexar/Octopart 2024 snapshot, CC-BY-4.0 via HuggingFace) | OR-Tools CP-SAT | Mixed-integer programming under MOQ / stock constraints |
| Two-stage stochastic program + CVaR efficient frontier | Same, plus disruption scenarios calibrated to a cited McKinsey base rate (disruption >1 month every 3.7 years) | CP-SAT, sample-average approximation | Optimization under uncertainty; Rockafellar–Uryasev CVaR linearization; Value of the Stochastic Solution |
| Route optimization | Real distributor geography | Exhaustive enumeration (≤8 stops), OR-Tools routing above that | Symmetric TSP; proven optimum on the sizes the site actually produces, guided local search beyond them |
| Network fragility analysis | The real distributor→component bipartite graph | NetworkX | Spectral graph theory: algebraic connectivity (Fiedler), betweenness, PageRank, k-core, HHI |
| Monte Carlo disruption simulation | 1,000 scenarios over that graph | NumPy | Percolation; tail risk (CVaR-95) |
| Lead-time prediction | **3,406 real DigiKey observations across 6 snapshots, collected by our own weekly pipeline** (75 on 2026-07-01, 742 on 2026-08-15, 363 on 2026-08-17, 742 on 2026-08-24, 742 on 2026-08-31, 742 on 2026-09-07); the served model is fitted on an earlier cut — 2,615 usable rows of the then five-snapshot panel / 324 API-derived features, retrained 2026-09-03, so the artifact is one snapshot behind the panel and `/ml/model-info` reports `stale: true` | scikit-learn, GroupKFold | Supervised regression; group-aware CV; leakage detection |
| Macro supply-stress regime model | NY Fed GSCPI + FRED, 343 monthly observations | scikit-learn | Walk-forward validation; proper scoring rules (Brier); calibration slope; ship gate vs persistence and climatology |
| Intermittent-demand benchmark | Monash car parts: 2,674 series × 51 months, 136,374 observations | Croston / SBA / TSB, custom CRPS | Distributional forecasting; proper scoring rules; Friedman + Nemenyi significance testing |
| Macro demand backtest | US Census M3 `A34SNO`, 198 monthly observations, ALFRED vintage `2026-08-16` (pinned, offline) | Prophet, Chronos-Bolt | Rolling-origin backtesting; time-series foundation models; data-vintage reproducibility |
| Live pricing & risk feeds | DigiKey and OEMsecrets return real offers on demand; Nexar is credentialled but currently returns errors on every path (open bug). FRED, IMF PortWatch, GPR index | httpx, OAuth2 client-credentials, GraphQL | API integration; auth flows; quota/rate-limit handling; graceful degradation |
| Model CI | — | GitHub Actions, pytest | ML engineering discipline: train/serve schema parity, baseline gates, coverage floors, artifact provenance |
| The application | — | FastAPI, React + TypeScript, SQLAlchemy, Alembic, Docker, Render | Full-stack delivery and deployment |

---

## Skills that are legitimately claimable

- **Operations Research** — MILP, two-stage stochastic programming, CVaR, TSP, efficient frontiers
- **ML engineering** — model serving, CI gates, train/serve parity, leakage detection, grouped CV,
  calibration, artifact provenance
- **Forecasting** — rolling-origin backtesting, distributional forecasting, proper scoring rules,
  intermittent demand, time-series foundation models
- **Data engineering** — resumable quota-aware API collection, scheduled pipelines, schema
  migrations, provenance tracking
- **Full-stack / DevOps** — FastAPI, React/TS, Docker, GitHub Actions CI/CD, live deployment

---

## The stories worth telling

These matter more than the feature list. Each is a real thing that happened, reproducible from a
script.

**1. I retracted my own headline.**
The benchmark claimed 44.7% cost savings vs a greedy baseline. Decomposed, the entire advantage
was a $75-per-supplier fixed freight fee on 4-part / 7-unit orders — fixed fees were 96.5% of the
cost being optimized. At realistic volume it falls to 3–8%. Published the volume curve showing the
decay. *(`docs/BENCHMARK_VOLUME_CURVE.md`)*

**2. My R² collapsed from 0.83 to −0.70, and that was the finding.**
Random split: +0.825. Grouped by part-family key: +0.073. Holding out whole manufacturers: **−0.697** —
worse than predicting the mean. The model learned how three vendors quote, not how parts behave.
Effective sample size is 28 manufacturers, not 2,615 rows. (Those three counts describe the
**2026-09-03 served artifact**, fitted on the then five-snapshot, 2,664-row cut of the panel —
2,615 of those rows survive the label and match-quality drops. The panel on disk has since
grown to 3,406 rows / 6 snapshots; the artifact has not been refitted. The fold groups are 472 *grouping keys*
from `lead_time_model._group_key`, over 361 distinct `base_product` values — the two counts are
different quantities and `LEAKAGE_PROGRESSION.md` keeps them apart.)
*(`docs/leakage_progression.json`, `python -m seeds.run_leakage_progression`)*

**3. My accuracy metric was rewarding a forecast that always predicts zero.**
On 2,646 intermittent-demand series, MASE ranks the degenerate `zero` forecast **first** (mean
Friedman rank 1.66; RMSSE also ranks it first, at 2.63) — because MAE/MASE is minimized by the
conditional median, and on a 24%-non-zero panel that median is usually zero. Under CRPS it falls
to 4th; under pinball loss, 5th. Kendall's τ between the MASE and pinball orderings is **−0.20** —
mildly anti-correlated. *(`docs/INTERMITTENT_DEMAND.md`)*

**4. Backtesting on revised data flattered every model by ~20% — and my headline result had
silently flipped because I never pinned the data vintage.**
The published comparison was Prophet 2.66% WAPE vs Chronos-Bolt 2.93% — a foundation model losing
to Prophet, reported rather than buried. Re-running the script today gives **Prophet 3.13% vs
Chronos 2.93%: Chronos now wins.**

The harness was not at fault — both scripts emit byte-identical numbers on identical input. The
cause was that `run_forecast_backtest.py` refetched Census M3 `A34SNO` live on every run and
overwrote its own cache, and **Census revises that series in place**. Two committed artifacts
covering an identical window disagreed in the third decimal because the series was revised
between them.

So the real finding is about reproducibility, not about foundation models: *a backtest that
refetches its input is not a backtest, it is a moving target, and a published result can invert
without a line of code changing.*

**Fixed 2026-08-16, and the fix produced a better finding than the original claim.** Both
backtests now pin an ALFRED vintage (`--as-of`), vintage files committed under
`backend/seeds/data/a34sno_vintages/` with SHA-256s in every artifact; a pinned run does no network
I/O. Beyond pinning, the backtest was rebuilt as a true **real-time** protocol: each origin sees
only the vintage that existed on that date.

**Scoring on revised data flatters every model by roughly 20%:**

| | Real-time | On revised data | Flattered by |
|---|---|---|---|
| Chronos-Bolt | 0.0364 | 0.0293 | 19.5% |
| Prophet | 0.0413 | 0.0313 | **24.2%** |
| Seasonal-naive | 0.0587 | 0.0480 | 18.2% |

Prophet's skill over the naive baseline falls from +34.8% to +29.6%. This repo quoted the flattered
numbers until today.

It is a controlled experiment: ALFRED's vintages align exactly with the pre-existing origins
(training sizes 162/174/186 under both protocols), so training length, target months and actuals
are identical and the *only* thing varying is revised-vs-real-time. A test asserts that alignment,
because if it breaks the two protocols stop being comparable.

**No winner is declared, deliberately.** Chronos has the lower real-time error (11.9% relative),
but the per-origin winner flips (Chronos, Prophet, Chronos); 36 points from 3 origins at a 12-step
horizon are heavily serially correlated, so the sign test (25/36, p=0.029) is reported as
descriptive rather than as a test; and Chronos may have seen these months in pretraining, a channel
Prophet does not have, so its edge is an upper bound. Decisively: **the protocol effect on Prophet
(0.0100) is twice the Prophet–Chronos gap (0.0049).** How you backtest mattered more than which
model you chose. *(`docs/CHRONOS_BENCHMARK.md`, `docs/RESEARCH_TECHNIQUES.md` §4.1)*

**5. My own pipeline caught a real supply-chain event.**
Between the 2026-07-01 and 2026-08-15 snapshots, 56 STMicroelectronics parts re-quoted from
exactly 30 weeks to 40–52 weeks
— a genuine ST-wide lead-time extension, with a timestamped before-and-after, because the
collector was mine.

**6. My CI gates come from my own bugs.**
A train/serve schema mismatch silently made every lead-time prediction the same constant while a
published R²=0.93 described a model that was never served. There are now 50 gates; each names the
bug it prevents. The subtlest: the contract test written to catch that bug had itself stopped
working, because the primary feature was renamed underneath it. *(`docs/MODEL_CI.md`)*

---

## What NOT to claim

Being precise here is what makes the rest credible.

- **Not** per-part demand forecasting. The benchmark scores demand *methods* on a public spare-parts
  panel; it is not a forecast of these components.
- **Not** a live Nexar/Octopart feed for the seeded catalogue — that is a frozen 2024 snapshot.
  (Live pricing *is* real, on demand, via `/live-prices/*`.)
- **Not** a validated lead-time point predictor. Family-grouped R² is +0.08; the honest product is
  an interval, which is what Move 2 builds.
- **Not** temporal validation of the lead-time model — the panel holds five snapshot dates
  spanning 2026-07-01 to 2026-08-31 (the served artifact was fitted on the first four), which is
  far too short a span for time-series features to be learnable. The weekly collector fixes this
  over time. *(This line read "there are two snapshots" until 2026-09-01; that matched neither the
  panel nor the artifact at any point.)*
- **Not** a calibrated disruption probability model. Scenario probabilities are anchored to a cited
  industry base rate and swept, not estimated from data.
