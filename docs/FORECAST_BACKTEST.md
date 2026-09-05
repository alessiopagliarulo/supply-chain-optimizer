# Demand Forecast — Walk-Forward Backtest

<!-- GENERATED FILE — do not hand-edit. Regenerate: `cd backend && python -m seeds.run_forecast_backtest` -->

**Series:** Census M3 / FRED `A34SNO` (Manufacturers' New Orders: Computers & Electronic Products, $M), monthly, 198 obs 2010-01-01 → 2026-06-01.

**Data vintage (pinned):** `2026-08-16` — the series exactly as it stood on that date, served by ALFRED and committed at `backend/seeds/data/a34sno_vintages/a34sno_20260816.csv`.

**Input hash:** file sha256 `b5e61299781f39ea…` · observation-values sha256 `0d06c2e215521b9a…`. Loaded via `alfred_vintage_pin_committed`.

**Why a pin:** Census revises M3 *in place* and FRED mirrors the revision, so an unpinned re-run of this backtest silently scores a different series. That is not hypothetical — it inverted this repo's Prophet-vs-Chronos headline (quantified in the vintage-sensitivity table of [CHRONOS_BENCHMARK.md](CHRONOS_BENCHMARK.md)). Re-running with the same `--as-of` reproduces the numbers below exactly.

**Method:** rolling-origin walk-forward — 3 non-overlapping origins, 12-month horizon each. Models retrained at every origin.

**Baseline:** seasonal-naive (m=12). Prophet must beat this to justify its complexity.

**Scope — read this before quoting a number from here.** This is an *aggregate industry* series: one national monthly total, not per-part demand. It says nothing about how well any individual component can be forecast, and there is no per-part demand model in this app (the synthetic one was retired — see `docs/INTERMITTENT_DEMAND.md`). Per-SKU demand evidence lives there instead, on the Monash car-parts panel. With 3 origins this series also cannot support a significance test; that is why none is reported below.

## Headline

- **Prophet (seasonal) WAPE:** 0.0313  ·  MAPE 0.0291  ·  RMSE 1413.35
- **Prophet (trend-only ablation, no yearly term) WAPE:** 0.0296  ·  MAPE 0.0273  ·  RMSE 1391.30  ·  skill +38.3%
- **Seasonal-naive WAPE:** 0.0480  ·  MAPE 0.0459  ·  RMSE 1688.46
- **Skill score (1 − WAPE_prophet/WAPE_naive):** +34.8%
- **Verdict:** Prophet beats the seasonal-naive baseline.

## Real-time protocol — the number you could actually have achieved

The headline above is *pseudo* real-time: it slices the latest, fully revised series, so every origin is shown observations that did not exist yet at that origin. Census revises this series in place, so that is real leakage. Below, each origin trains only on the ALFRED vintage that existed on its date — same training lengths, same target months, same actuals, so the gap is data revision alone.

| Model | Real-time WAPE | Pseudo real-time WAPE | Revised data flatters by |
|---|---:|---:|---:|
| **Prophet** (seasonal) | **0.0413** | 0.0313 | +24.2% |
| Seasonal-naive (m=12) | **0.0587** | 0.0480 | +18.2% |

Prophet's skill score against the naive baseline under the real-time protocol is **+29.6%** (vs +34.8% on revised data). Prophet still beats the baseline — but every absolute WAPE quoted above the fold is optimistic. The three-model version of this table, including Chronos, is in [CHRONOS_BENCHMARK.md](CHRONOS_BENCHMARK.md).

## Accuracy degradation by horizon (WAPE)

| Horizon (months ahead) | Prophet WAPE | Naive WAPE | Prophet bias |
|---:|---:|---:|---:|
| 1 | 0.013 | 0.035 | -198.75 |
| 2 | 0.012 | 0.026 | -183.66 |
| 3 | 0.011 | 0.022 | -291.33 |
| 4 | 0.013 | 0.028 | -350.88 |
| 5 | 0.012 | 0.028 | -118.55 |
| 6 | 0.027 | 0.043 | -501.83 |
| 7 | 0.035 | 0.052 | -921.57 |
| 8 | 0.036 | 0.054 | -961.65 |
| 9 | 0.046 | 0.066 | -1242.74 |
| 10 | 0.046 | 0.067 | -915.25 |
| 11 | 0.052 | 0.071 | -1198.29 |
| 12 | 0.066 | 0.078 | -1702.01 |

## Notes

- WAPE (Σ|a−f|/Σ|a|) is the headline metric; it does not blow up on low-volume months the way MAPE can.
- `bias` is mean(forecast − actual): positive ⇒ systematic over-forecast.
- Reproduce exactly: `cd backend && python -m seeds.run_forecast_backtest --as-of 2026-08-16`. The vintage flag is what makes "exactly" true.


## Provenance

- **Generated:** 2026-09-05T19:37:09Z (UTC)
- **Generator:** `seeds.run_forecast_backtest`
- **Commit:** `f31ecfcd0f0d6edfff3353c8842315ca5e87e73a` (clean tree)
- **Input `demand_series`:** `backend/seeds/data/a34sno_vintages/a34sno_20260816.csv` · sha256 `b5e61299781f39ea…`
- **Data vintage pin:** `2026-08-16`
- **Python:** 3.13.5 · macOS-26.5-arm64-arm-64bit-Mach-O
