# Chronos (TSFM) Zero-Shot Benchmark vs Prophet

<!-- GENERATED FILE — do not hand-edit. Regenerate: `cd backend && python -m seeds.run_chronos_benchmark` -->

**Series:** Census M3 / FRED `A34SNO` (Manufacturers' New Orders: Computers & Electronic Products ($M)), monthly, 198 obs 2010-01-01 → 2026-06-01.

**Data vintage (pinned):** `2026-08-16` — the series exactly as it stood on that date, served by ALFRED and committed at `backend/seeds/data/a34sno_vintages/a34sno_20260816.csv`.

**Input hash:** file sha256 `b5e61299781f39ea…` · observation-values sha256 `0d06c2e215521b9a…`. Loaded via `alfred_vintage_pin_committed`.

**Why a pin:** Census revises M3 *in place* and FRED mirrors the revision, so an unpinned re-run of this backtest silently scores a different series. That is not hypothetical — it inverted this repo's Prophet-vs-Chronos headline (quantified in the vintage-sensitivity table of [CHRONOS_BENCHMARK.md](CHRONOS_BENCHMARK.md)). Re-running with the same `--as-of` reproduces the numbers below exactly.

**Method:** the IDENTICAL rolling-origin walk-forward as [FORECAST_BACKTEST.md](FORECAST_BACKTEST.md) — 3 non-overlapping origins, 12-month horizon, same WAPE/MAPE/RMSE/bias metrics (`app.ml.backtest`, `app.ml.forecast_metrics`).

**Scope, plainly:** n = 1 macro series, 3 origins, 36 scored points, no confidence intervals. This is a build-vs-buy probe, not a production model-selection study — do not read a single-series WAPE gap as "model X is better".

**Chronos model:** `amazon/chronos-bolt-tiny` (8.65 M params) — run **zero-shot** (no fit, no training on this series). Point forecast = 0.5 quantile. CPU, torch 2.12.1. Full timing breakdown below.

## Headline — real-time protocol (each origin sees only its own vintage)

**This is the result to quote.** The walk-forward table further down is *pseudo* real-time: it slices one fully revised series, so every origin is handed numbers that did not exist yet at that origin. Census revises this series in place, so that is a genuine leakage channel, not a technicality. Here each origin trains only on the ALFRED vintage that actually existed on its date — a forecast you could really have made at the time.

Training lengths and target months are identical between the two protocols by construction, and every model is scored against the same reference vintage (`2026-08-16`). So the difference between the tables is attributable to **data revision alone**.

| Origin vintage | Trains on | n obs | Forecasts |
|---|---|---:|---|
| `2023-08-01` | data through 2023-06-01 | 162 | 2023-07-01 → 2024-06-01 |
| `2024-08-01` | data through 2024-06-01 | 174 | 2024-07-01 → 2025-06-01 |
| `2025-08-01` | data through 2025-06-01 | 186 | 2025-07-01 → 2026-06-01 |

| Model | Real-time WAPE | Pseudo real-time WAPE | Revised data flatters by |
|---|---:|---:|---:|
| **Chronos** (zero-shot) | **0.0364** | 0.0293 | +19.5% |
| **Prophet** (fitted, seasonal) | **0.0413** | 0.0313 | +24.2% |
| Seasonal-naive (m=12) | **0.0587** | 0.0480 | +18.2% |

**Finding worth more than the model ranking:** scoring on revised data makes *every* model look substantially better than it could have been in real time — Chronos (zero-shot) 19.5%, Prophet (fitted, seasonal) 24.2%, Seasonal-naive (m=12) 18.2%. Any backtest on a revised macro series that does not pin per-origin vintages is quoting a number the forecaster could not have achieved.

### Per-origin breakdown (does the winner hold up?)

| Origin | Chronos (zero-shot) | Prophet (fitted, seasonal) | Seasonal-naive (m=12) | Winner |
|---|---:|---:|---:|---|
| `2023-08-01` | 0.0245 | 0.0346 | 0.0450 | Chronos (zero-shot) |
| `2024-08-01` | 0.0220 | 0.0165 | 0.0433 | Prophet (fitted, seasonal) |
| `2025-08-01` | 0.0602 | 0.0699 | 0.0848 | Chronos (zero-shot) |

Point-level, Chronos has the lower absolute error on **25 of 36** forecast points (sign-test two-sided p = 0.0288). Per-origin winners: `chronos`, `prophet`, `chronos`.

> The sign test assumes independent points. These are 12-step-ahead forecasts from 3 origins, so errors are strongly serially correlated within an origin and the effective sample size is far below 36. Read the p-value as descriptive, not as a hypothesis test.

**Verdict on the real-time protocol: Chronos has the lower error** (0.0364 vs 0.0413 WAPE, 11.9% relative). Both beat seasonal-naive (0.0587). **But the per-origin winner is not consistent** (chronos, prophet, chronos), and there are only 3 origins. With 36 correlated test points from one macro series, this is evidence of a modest edge, not a reliable ranking — do not present it as "model X is better".

**Contamination caveat, and it cuts against the zero-shot model:** Chronos is pretrained on a large public time-series corpus. The real-time protocol controls what Chronos is *shown at inference*, but it cannot control what was in its *pretraining* set — which may include these very months of this very series at their revised values. Prophet has no such channel: it only ever sees the vintage handed to it. So Chronos's edge here should be read as an upper bound.

## Secondary — pseudo real-time walk-forward (revised series)

> These numbers slice the latest fully revised series, so they are optimistic: each origin sees data that did not exist yet. Kept because the horizon breakdown and the latency instrumentation below are built on this run, and because the gap against the real-time table is itself the finding. **Quote the real-time table above, not this one.**

| Model | WAPE | MAPE | RMSE | Bias | Zero-shot? |
|---|---:|---:|---:|---:|:--:|
| **Prophet** (fitted, seasonal) | 0.0313 | 0.0291 | 1413.35 | -715.54 | no |
| Seasonal-naive (m=12) | 0.0480 | 0.0459 | 1688.46 | -1203.86 | n/a |
| **Chronos** chronos-bolt-tiny | 0.0293 | 0.0275 | 1279.91 | -766.23 | **yes** |

**Verdict: Chronos zero-shot WINS overall** (0.0293 WAPE vs Prophet 0.0313, +6.4%). A TSFM with no fitting beats a tuned Prophet on this series. Chronos does clear the seasonal-naive bar (0.0293 vs naive 0.0480).

## Vintage sensitivity — why the pin is not optional

Identical code, identical rolling origins, identical models. The ONLY thing that changes between these rows is the ALFRED data vintage of `A34SNO`. Census revises this series in place, so an unpinned re-run silently moves along this table.

| Vintage | n obs | Series ends | Prophet WAPE | Chronos WAPE | Naive WAPE | Winner |
|---|---:|---|---:|---:|---:|---|
| `2026-08-16` ← **PINNED** | 198 | 2026-06-01 | 0.0313 | 0.0293 | 0.0480 | **Chronos** by 0.20 pp |
| `2026-07-10` | 197 | 2026-05-01 | 0.0266 | 0.0293 | 0.0437 | **Prophet** by 0.27 pp |

**Is the headline robust?** The Prophet-vs-Chronos gap on the pinned vintage is **0.0020 WAPE**. Re-scoring the *same* Prophet on a different vintage of the *same* series moves it by **0.0047 WAPE**. The vintage effect is LARGER than the model effect, so the ranking of these two models on this series is **not a robust finding** — it is within the noise that one month of Census revision introduces. Report the pinned number, cite the vintage, and do not claim either model is better in general.

> **What this table replaced.** Until 2026-08-16 this benchmark refetched `A34SNO` live on every run and overwrote its own cache, so it had no vintage at all. The published headline ("Prophet 0.0266 beats Chronos 0.0293 — the foundation model lost, and I published it") was computed on the 2026-07-10 vintage and silently stopped reproducing when Census revised the series. It is superseded by the pinned row above, not deleted.

## WAPE by horizon (where each model degrades)

| Horizon (months ahead) | Prophet | Seasonal-naive | Chronos (zero-shot) |
|---:|---:|---:|---:|
| 1 | 0.013 | 0.035 | 0.005 |
| 2 | 0.012 | 0.026 | 0.007 |
| 3 | 0.011 | 0.022 | 0.011 |
| 4 | 0.013 | 0.028 | 0.013 |
| 5 | 0.012 | 0.028 | 0.012 |
| 6 | 0.027 | 0.043 | 0.023 |
| 7 | 0.035 | 0.052 | 0.033 |
| 8 | 0.036 | 0.054 | 0.035 |
| 9 | 0.046 | 0.066 | 0.043 |
| 10 | 0.046 | 0.067 | 0.044 |
| 11 | 0.052 | 0.071 | 0.054 |
| 12 | 0.066 | 0.078 | 0.067 |

## Cold-start: only 6 months of history (< 1 season)

The natural TSFM case: a brand-new part with almost no demand history. Each model sees only the most recent **6** points before each holdout block (same blocks as above).

**Two Prophet rows, deliberately.** Handing Prophet 6 points *with yearly seasonality still switched on* is a strawman — it is a misconfiguration, not a defeat, and an earlier version of this doc quietly used it to make Chronos look good. The honest comparator is Prophet configured the way you would actually configure it for 6 points (trend-only) — which is also the config the served per-part forecaster uses.

| Model | Cold-start WAPE | Cold-start RMSE | Cold-start bias |
|---|---:|---:|---:|
| Prophet (seasonal — MISCONFIGURED for 6 pts, shown for honesty) | 9.960 | 325369.70 | -30378.28 |
| Prophet (trend-only — the fair comparator) | 0.023 | 741.62 | +134.96 |
| Seasonal-naive | 0.041 | 1602.68 | -1082.58 |
| Chronos (zero-shot) | 0.041 | 1549.10 | -1080.34 |

Against the FAIR comparator the cold-start win **disappears**: Prophet trend-only 0.023 WAPE vs Chronos 0.041. The earlier "Chronos crushes Prophet cold" claim was an artifact of running Prophet with yearly seasonality on 6 points. Reported as-is.

## Cost / timing (measured this run, not quoted)

**Hardware:** macOS-26.5-arm64-arm-64bit-Mach-O · arm · Python 3.13.5 · torch 2.12.1 (4 threads) · device `cpu` · CUDA available: False.

**Chronos startup:** `import torch` + `import chronos` **2.68 s** · `from_pretrained` **0.4 s** (weights already in the HF cache: **True** — a cold machine must first download ~33 MB) · model size **8.65 M** parameters.

**Warm-up:** the first forward pass costs **73 ms** (lazy init). It is timed separately and EXCLUDED from the steady-state numbers below — reporting it inside a single wall-clock, as this benchmark used to, is what made the old "0.01 s inference" figure impossible to interpret.

Per-call cost over the walk-forward origins (warm-up excluded; the trend-only row is the short-context cold-start run and is timed separately so the medians are not mixed):

| Model | Calls | Context (pts) | Median / call | Mean | Min | Max | What one call does |
|---|---:|---:|---:|---:|---:|---:|---|
| Chronos (zero-shot) | 3 | 162–186 | **2.2 ms** | 2.3 ms | 2.2 ms | 2.5 ms | frozen forward pass, H=12 |
| Prophet (seasonal) | 3 | 162–186 | **22.4 ms** | 30.5 ms | 22.4 ms | 46.8 ms | full Stan fit + predict |
| Prophet (trend-only, cold-start ctx) | 3 | 6 | **22.4 ms** | 24.0 ms | 20.9 ms | 28.7 ms | full Stan fit + predict |
| Seasonal-naive | 3 | 162–186 | **0.0 ms** | 0.0 ms | 0.0 ms | 0.0 ms | array indexing |

**Chronos steady-state latency** (the walk-forward is only 3 calls — not a latency sample): the same forward pass repeated **20×** on the full 198-point context, after a discarded warm-up → median **2.24 ms**, mean 2.24 ms, p95 2.43 ms, range 2.07–2.43 ms (H=12, batch 1). An 8.65 M-parameter encoder-decoder doing ONE non-autoregressive forward pass over ~200 tokens really is single-digit milliseconds on this CPU — the number is small, but it is not a stub: dropping `chronos-forecasting` makes this script fail loudly and write "pending" rather than produce figures.

Chronos's per-forecast cost is **10× cheaper than Prophet's** here — but that compares a frozen forward pass against a full Stan fit, which is exactly the point: the TSFM's cost is the ~2 GB torch install and the one-off weight load, not the inference. (Horizon 12, single series, batch size 1, n=3 calls — this is NOT a throughput benchmark, and with so few calls the median is indicative, not a stable percentile.)

## Honest take (model selection)

- **Dependency cost is real:** Chronos pulls `torch` (~2 GB wheel) + `transformers` + `accelerate`. That is why it lives in `requirements-ml.txt`, NOT the core deploy image. Inference is CPU-cheap once loaded (see the timing table); the cost is install/image size, plus a one-off weight load, not per-forecast latency.

- **Chronos won on accuracy here (0.0293 vs Prophet 0.0313 WAPE), but read it carefully:** this is *one* macro series (n=1), not 791 parts. A single-series win is suggestive, not conclusive — Chronos's pretraining corpus likely contains manufacturing/orders-like signals, so this is close to in-distribution for it. The right read is "a TSFM is competitive-to-better with zero fitting", not "replace Prophet everywhere".

- **Prophet still earns its place** for production demand on long-history parts: it is interpretable (decomposable trend/seasonality), already validated, and adds no torch dependency to the deploy image. The accuracy gap (0.0293 vs 0.0313 WAPE) must be weighed against those operational costs.

- **The cold-start case for a TSFM is NOT established on this series** once Prophet is configured correctly for a short history. Do not claim it.

## Reproduce

```bash
cd backend
pip install -r requirements-ml.txt   # heavy: torch + chronos
python -m seeds.run_chronos_benchmark --as-of 2026-08-16 \
    --compare-vintage 2026-07-10
```

Timings are machine-specific (hardware stated above) and will differ on yours; the WAPE/RMSE figures are deterministic given the same series vintage — which is why `--as-of` is not optional if you want to reproduce them. Run recorded: `2026-09-05T19:38:55+00:00`.


## Provenance

- **Generated:** 2026-09-05T19:38:55Z (UTC)
- **Generator:** `seeds.run_chronos_benchmark`
- **Commit:** `5c462d4b8e553f69d5eada90834e3d7ce474dc69` (clean tree)
- **Input `demand_series`:** `backend/seeds/data/a34sno_vintages/a34sno_20260816.csv` · sha256 `b5e61299781f39ea…`
- **Data vintage pin:** `2026-08-16`
- **Python:** 3.13.5 · macOS-26.5-arm64-arm-64bit-Mach-O
