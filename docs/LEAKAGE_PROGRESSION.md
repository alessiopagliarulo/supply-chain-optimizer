# The part-family leakage collapse, measured

Generated `2026-09-10T04:34:38Z` by `python -m seeds.run_leakage_progression` (backend/, venv active). Machine-readable: [`leakage_progression.json`](leakage_progression.json).

**Every number below is produced by that one command.** Earlier revisions of `MODEL_CI.md` and `RESEARCH_TECHNIQUES.md` quoted two different progressions from memory; this artifact is now the only source either of them cites.

## The headline

The same estimator (`gradient_boosting`), the same 3351 rows, the same feature pipeline and the same seed. **The only thing that changes is what the fold boundary is allowed to cut through.**

| Split regime | R² mean | R² median | fold sd | p10 | p90 | min | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| random rows (**wrong**) | +0.839 | +0.842 | 0.018 | +0.816 | +0.858 | +0.782 | +0.876 |
| grouped by part-family key | +0.080 | +0.139 | 0.242 | -0.327 | +0.322 | -0.575 | +0.405 |
| grouped by manufacturer | -0.706 | -0.107 | 2.509 | -1.351 | +0.025 | -17.559 | +0.211 |

Mean R² **+0.839 → +0.080 → -0.706**; median R² **+0.842 → +0.139 → -0.107**. 3351 rows, 472 family grouping keys, 28 manufacturers.

**472 grouping keys is not 472 part families**, and the two numbers are kept apart everywhere below. The fold groups are the output of `lead_time_model._group_key`, which emits `family:{base_product}` when DigiKey returned a base product, `mpn:{mpn}` when it did not, and `row:{i}` as a last resort. A fallback can only ever split a group, never merge two real families, so the key count (472) is a strict refinement of the **361 distinct `base_product` values** in the panel. Read as "part families", the right number is **361**; read as "what the fold boundary actually respects", it is **472**.

**The effective sample size for generalisation is 28 manufacturers, not 3351 rows.** Three vendors (Analog Devices, Texas Instruments, STMicroelectronics) supply 62.0% of the panel, and 7 of the 28 vendors contribute 6 rows or fewer.

## What the negative number means, precisely

Holding out whole manufacturers, mean R² is **-0.706** and 40 of 50 folds score below zero. A negative R² is not a small positive one, and it is worth stating exactly what it is: R² is measured against the **held-out fold's own mean**, so R² < 0 means the model's squared error exceeds that vendor's entire label variance. On a vendor it has never quoted, the model has **no explanatory power at all** — it does not rank that vendor's parts correctly and its predicted level is biased for them.

It does **not** mean the model is beaten by every trivial predictor, and the honest version of the claim has to say so. Scored on those same manufacturer-held-out folds, `train_mean` gets -2.114 and `manufacturer_mean` gets -2.114 — both worse than the model's -0.706. The model is still the best of the set. It is simply the best member of a set in which **nothing generalises to an unseen vendor.**

The mechanism is visible in the baseline table below: under a random split, a lookup table keyed on nothing but the manufacturer scores +0.651 — almost the whole of the full model's +0.839. Vendor identity *is* the panel's signal, and holding a vendor out is precisely the operation that removes it. This is a statement about the dataset, not a bug in the estimator: 28 vendors is a small sample no matter how many rows they generate.

As a harness sanity check, `train_mean` scores -0.002 under the random regime — R² ≈ 0 for a constant predictor, which is what it must be if the scoring is right.

### Mean vs median, and why both are quoted

R² divides by the *test fold's own* label variance. Under the manufacturer regime a fold can be dominated by one vendor whose quotes barely vary, and that fold's R² blows up negative regardless of absolute error — which is why the mean (-0.706) sits far below the median (-0.107). RMSE has no such pathology, so it is reported alongside:

| Split regime | RMSE mean (days) | RMSE median (days) | fold sd |
|---|---:|---:|---:|
| random rows (**wrong**) | 43.75 | 43.04 | 2.68 |
| grouped by part-family key | 81.73 | 63.17 | 36.08 |
| grouped by manufacturer | 92.00 | 79.00 | 37.01 |

The RMSE progression tells the same story without the ratio artefact: error grows from 43.7 d to 92.0 d as the protocol gets honest.

## Naive baselines, on the identical folds

Every baseline is scored on exactly the folds above, so the comparison is paired. They come from `lead_time_model.baseline_predictors` — the same definitions the training path gates on — not from a copy living in this script.

| Predictor | random R² | family R² | manufacturer R² |
|---|---:|---:|---:|
| `ridge` | +0.774 | -0.401 | -2.845 |
| `random_forest` | +0.903 | +0.014 | -0.807 |
| `gradient_boosting` *(champion)* | +0.839 | +0.080 | -0.706 |
| `mlp` | +0.914 | -1.008 | -9.030 |
| `train_mean` *(baseline)* | -0.002 | -0.525 | -2.114 |
| `always_210d` *(baseline)* | -0.367 | -1.794 | -5.409 |
| `category_mean` *(baseline)* | +0.207 | -0.413 | -1.509 |
| `manufacturer_mean` *(baseline)* | +0.651 | -0.095 | -2.114 |

The collapse is not an artefact of one estimator — every model in the bake-off shows it, including the two linear/neural ones whose manufacturer-regime means are dominated by a handful of catastrophic folds. And the ordering does not invert: the champion beats every naive baseline under all three regimes. What changes is that under the manufacturer regime the whole table is negative.

The headline row is `gradient_boosting` — the served champion, read from data/ml_models/metrics.joblib. This run's own lowest family-regime RMSE belongs to `gradient_boosting`; the two agree.

## A DIFFERENT quantity: in-sample identity-column R²

**These are not model scores and not cross-validated.** Each row fits a per-level mean on all rows and scores it on those same rows (one-way ANOVA R²). It quantifies how much *redundancy* the panel contains — the reason the random split is inflated — and must never be quoted as a split-regime R². A column with one level per row would score 1.000 by construction, so the level count is shown next to every figure.

| Identity column | in-sample R² | levels | rows/level |
|---|---:|---:|---:|
| `mpn` | 0.962 | 742 | 4.52 |
| `base_product` | 0.862 | 361 | 9.28 |
| `series` | 0.694 | 94 | 35.65 |
| `manufacturer` | 0.656 | 28 | 119.68 |
| `package_case` | 0.676 | 123 | 27.24 |
| `dk_category` | 0.210 | 17 | 197.12 |
| `category` | 0.539 | 52 | 64.44 |
| `family_group_key` | 0.914 | 472 | 7.1 |

This is the table that got conflated with the progression. `base_product` explaining 0.862 in sample is why a random split leaks; it is **not** the model's random-split score, which is +0.839.

## Protocol

- **Folds:** 5-fold, 10 independent shuffles = 50 folds per regime.
- **Seed:** `random_state = 42 + repeat`, identical across all three regimes.
- **Splitters:** `KFold(shuffle=True)` for `random`; `GroupKFold(shuffle=True)` for `family` and `manufacturer`.
- **Feature pipeline:** `lead_time_model.build_training_design` + `build_design_matrix`, feature schema v3, 324 columns — the same path `retrain_lead_time` uses.
- **Panel:** `backend/seeds/data/lead_time_panel/observed_lead_times.csv`, sha256 `d94df904635a732c…`
- **Nothing is persisted.** All fits are in-memory; `backend/data/ml_models/` is not written by this script.

### Row accounting

| | rows |
|---|---:|
| rows in | 3406 |
| dropped no label | 3 |
| dropped bad match | 24 |
| rows used | 3379 |
| distinct family grouping keys | 475 |
| distinct snapshot dates | 6 |
| static cells filled | 3814 |
| parts enriched | 3403 |
| dropped unfillable | 28 |
| rows trained | 3351 |
| distinct family grouping keys trained | 472 |
| distinct manufacturers trained | 28 |

### Regimes

- **`random`** — KFold(shuffle=True) on rows. The naive, WRONG protocol: near-duplicate siblings of one part family straddle the fold boundary, so the score measures recognition of an already-seen family, not prediction.
- **`family`** — GroupKFold on lead_time_model._group_key — family:{base_product} where DigiKey returned one, mpn:{mpn} otherwise. This is the protocol the shipped model uses. No family can straddle a fold. Note the key count exceeds the base_product level count because the MPN fallback splits the Unknown bucket; it never merges two real families.
- **`manufacturer`** — GroupKFold on the manufacturer. Whole vendors are held out — the strictest and most honest generalisation test, and the one that matches deployment.

### Environment

- hardware `arm64 / Darwin 25.5.0`, python `3.13.5`
- scikit-learn 1.8.0, numpy 2.4.4, pandas 2.3.3, scipy 1.17.1
- wall time 399.3s

## Reproduce

```bash
cd backend && source venv/bin/activate
python -m seeds.run_leakage_progression
```

## Provenance

- **Generated:** 2026-09-10T04:41:17Z (UTC)
- **Generator:** `seeds.run_leakage_progression`
- **Commit:** `10002b76bce4554908fa5e5aba388c17557166f8` (clean tree)
- **Input `lead_time_panel`:** `backend/seeds/data/lead_time_panel/observed_lead_times.csv` · sha256 `d94df904635a732c…`
- **Python:** 3.13.5 · macOS-26.5-arm64-arm-64bit-Mach-O


