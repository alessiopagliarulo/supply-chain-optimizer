# Dollar-Denominated Impact Framing (P3)

> **Sourcing work archived.** The sourcing optimizer this document refers to (CP-SAT
> sourcing MILP, two-stage stochastic program / CVaR frontier, MILP-vs-greedy benchmark)
> and the docs/artifacts it cites were removed from `main` and are preserved at git tag
> `archive/sourcing-v1`. The repo is being rebuilt as a logistics/routing engine.

Every abstract metric in this project is paired with a concrete financial
interpretation. This note documents how each dollar figure is derived, so the
numbers can be defended in an interview. **Real-data rule:** no dollar figure is
invented — each is computed from quantities already in the codebase, with any
conversion constant taken from a cited industry source and labeled as an
assumption.

---

## 1. CVaR-95 → "$X of procurement spend at risk"

**Metric.** CVaR-95 (`SimulationResult.cvar_95`, `backend/app/graph/simulation.py`)
is the *mean emergency-procurement cost multiplier over the worst-5% of the 1,000
Monte Carlo cascade-failure scenarios*. Each scenario inflates cost by
`1 + (unfulfillable / BOM_size) × 0.15` (15% emergency premium per unsourceable
line); CVaR-95 averages that multiplier across the worst-5% tail. It is ≥ 1.0.

**Dollar translation.**

```
procurement_spend_at_risk_usd = baseline_component_cost × (CVaR-95 − 1)
```

where `baseline_component_cost` is the sum of each BOM line's **average real
distributor offer price**, drawn from the 8,176-offer distributor dataset
originally sourced via the Nexar API — a static 2024 snapshot, not a live feed
(see [DATA_PROVENANCE.md](DATA_PROVENANCE.md)). Subtracting 1 strips the baseline
bill so the figure is the *extra* dollars a tail disruption would add.

- **Backend:** computed per BOM in `_compute_baseline_metrics`
  (`backend/app/api/resilience.py`) and returned on all three resilience
  endpoints as `procurement_spend_at_risk_usd` (alongside `baseline_cvar_95`).
- **Benchmark:** aggregated across the 10 reference BOMs as
  `baseline_spend_at_risk_usd` = mean(`total_cost_usd × (mc_cvar_95 − 1)`) in
  `backend/app/api/benchmark.py`.
- **UI:** amber "Procurement Spend at Risk · CVaR-95" banner on the Resilience
  page; "Tail risk · CVaR-95 spend at risk" tile on the Benchmark page.

**Assumptions/citations.** None external. The only constant is the 15% emergency
premium, which already lives in `simulation.py` (`EMERGENCY_COST_PREMIUM`).

**Status — the spend side is real, the probability side is now calibrated against
a cited base rate.** An earlier revision of this section, and of the README,
called the figure "fully data-derived," which overstated a version of this module
that read raw betweenness centrality directly as `p_fail` (the single most central
distributor modelled as down in 100% of scenarios). That has since been replaced;
see [`README.md`](../README.md), "CVaR-95 → dollars: the spend side is real, and
the probability side is now calibrated." Precisely what changed and what remains
an assumption:

- **The spend side is real.** `baseline_component_cost` multiplies real BOM spend —
  the sum of each line's average real distributor offer price. Nothing there is
  assumed.
- **The probability side is calibrated, not proxied.** `backend/app/graph/simulation.py`
  now calls `build_failure_probabilities` (`backend/app/graph/disruption.py`),
  which anchors to a cited base rate — McKinsey Global Institute (Aug 2020):
  disruptions lasting a month or longer roughly every 3.7 years — converted to an
  annual Poisson rate and then to a probability over a 60-day purchase-order
  exposure window. **Betweenness centrality** only rank-orders *relative* risk
  around that base rate (the most central distributor gets `spread`× it, the least
  central gets 1/`spread`×), and every probability is capped at 50%.
  `centrality_spread=1.0` — centrality's effect removed entirely — is a supported,
  published sensitivity arm, not a hidden knob.
- **The practical effect, stated rather than buried:** on the live headline BOM,
  calibrated `p_fail` ranges from 1.45% (least central distributor) to 13.04% (most
  central) — it no longer saturates at a fixed multiplier. What's still an
  assumption, not a measurement: the McKinsey rate is firm-level, not per-supplier,
  so applying it to one distributor is almost certainly too high; and nothing
  establishes that centrality actually predicts disruption likelihood at all (the
  code names this explicitly and ships the `spread=1.0` arm because of it). The
  calibration lives in `backend/app/graph/disruption.py`; the full calibration table
  and sensitivity sweep were in the CVaR frontier doc, archived at git tag
  `archive/sourcing-v1`.

---

## 2. Optimizer cost delta → "$Y saved per BOM run"

**Metric.** The benchmark compares the graph-aware optimizer to the baseline
optimizer over 10 reference BOMs (`backend/app/api/benchmark.py`).

**Dollar translation.**

```
cost_delta_usd = mean( graph_aware.total_cost_usd − baseline.total_cost_usd )
```

over the BOMs common to both arms. Negative ⇒ graph-aware is cheaper (money
saved). `total_cost_usd` is the real landed cost (component + transport + holding)
each optimizer run produced.

- **UI:** "Optimizer impact · $ / BOM run" tile on the Benchmark page.

**Honesty note.** This is intentionally surfaced as a *live, run-dependent* figure,
not a fixed marketing claim. On the current reference set the graph-aware vs
baseline delta sits near the ±2% noise floor (the page already flags this with a
"Low confidence" badge). The dollar tile reflects whatever the real run produced.

**Assumptions/citations.** None external — directly from run totals.

---

## 3. Demand forecasting — what changed, and what is measured now

**This section used to derive a "≈N weeks of safety stock" dollar figure from the
macro Census backtest and attribute it to the per-part forecast the app served.**
That per-part forecast is gone, and so is the dollar figure it fed — not
carried over, because it no longer has a live consumer. What follows is the
accurate replacement: what was removed and why, what the macro backtest still
shows (unchanged, real, kept), and what the new demand-method benchmark shows in
its place.

### 3a. What was removed

The per-part "demand" the app served was `total_stock / 52 × risk_score` —
a magnitude *inferred from inventory position and a risk multiplier*, not
measured, identical in shape across all 791 parts because it borrowed one macro
curve (Census `A34SNO`) for the temporal pattern. Prophet was fit on top of that
fabricated series and produced a 12-week forecast window that closed 17 months
before this line was written, against which no actuals were ever recorded —
unscoreable even in principle, because no public per-SKU demand series exists for
these components against which to check it.

It was removed rather than patched: migration
`0008_drop_synthetic_demand_tables.py` drops `component_demand_history` and
`component_forecasts`; `backend/seeds/train_forecasts.py`,
`backend/app/models/forecast.py`, `backend/app/api/forecasts.py`, and the
`GET /api/v1/forecasts/all` endpoint are deleted; the frontend per-part forecast
sparkline and "stock-out in ~N weeks" badge are gone with them.

### 3b. What the macro backtest still shows (unchanged, real)

The Census `A34SNO` walk-forward backtest ([FORECAST_BACKTEST.md](FORECAST_BACKTEST.md))
never depended on the deleted tables and is unaffected by the deletion:

**All figures below are pinned to ALFRED vintage `2026-08-16`** and must always be
quoted with that vintage — see §3d for why that is not pedantry.

| Model | WAPE | MAPE | RMSE |
|-------|-----:|-----:|-----:|
| Prophet — trend-only (no yearly term) | **0.0296 (3.0%)** | 0.0273 | 1391.30 |
| Prophet — seasonal | 0.0313 (3.1%) | 0.0291 | 1413.35 |
| Seasonal-naive | 0.0480 (4.8%) | 0.0459 | 1688.46 |

Skill score vs. seasonal-naive: **+38.3%** for the trend-only ablation, **+34.8%**
for the seasonal variant. This is a real measurement of Prophet on a real aggregate
industry series (198 monthly obs, 2010-01-01 → 2026-06-01, 3 rolling origins,
12-month horizon per origin).

**The table above is *pseudo* real-time and is therefore optimistic — quote it only
alongside the real-time pair.** It slices the latest, fully revised series, so each
origin sees observations that did not exist yet at that origin, and Census revises
this series in place. Re-scored so that each origin trains only on the ALFRED vintage
that existed on its date (same training lengths, same target months, same actuals —
so the gap is data revision alone):

| Model | Real-time WAPE | Pseudo real-time WAPE | Revised data flatters by |
|---|---:|---:|---:|
| **Prophet** (seasonal) | **0.0413 (4.1%)** | 0.0313 (3.1%) | +24.2% |
| Seasonal-naive (m=12) | **0.0587 (5.9%)** | 0.0480 (4.8%) | +18.2% |

Prophet's skill score under the real-time protocol is **+29.6%** (vs +34.8% on revised
data). Prophet still beats the baseline; every absolute WAPE in the first table is
optimistic by roughly a quarter.

Under either protocol, this backtest was never evidence about per-part accuracy —
that framing is retired along with the per-part forecast itself — and no dollar
translation is attached to it here anymore, since the safety-stock tooltip that
consumed it no longer exists in the UI.

### 3d. These numbers moved, and the reason is the point

An earlier revision of this table quoted Prophet at **0.0251 / 0.0266 WAPE** against
a **0.0438** seasonal-naive over **197** observations, and said the foundation model
(Chronos) lost to Prophet. That is stale, and it went stale without anyone editing a
number: `seeds/run_forecast_backtest.py` refetched Census M3 `A34SNO` live on every
run and overwrote its own cache, and **Census revises that series in place**. Same
code, same harness, different data.

On the pinned `2026-08-16` vintage, **Chronos (0.0293) beats Prophet (0.0313)** — the
opposite of what was published. And the honest reading is not "Chronos wins": the
model gap is **0.0020 WAPE**, while re-scoring the *same* Prophet across vintages of
the *same* series moves it by **0.0047 WAPE**. The revision effect is more than twice
the model effect, so **the Prophet-vs-Chronos ranking is not a robust finding** and
this document does not assert one. Full account in
[`RESEARCH_TECHNIQUES.md` §4.1](RESEARCH_TECHNIQUES.md) and the vintage-sensitivity
table in [`CHRONOS_BENCHMARK.md`](CHRONOS_BENCHMARK.md).

### 3c. What replaced the per-part demand claim

There is no public per-SKU demand series for electronic components, so this
project does not claim one. What it can measure instead is **which
intermittent-demand method to trust and why**, on a real analogous panel: Monash
car-parts sales (2,674 series × 51 months, 24.1% non-zero, CC-BY 4.0, via
HuggingFace `Monash-University/monash_tsf`) — served at
`GET /api/v1/demand/benchmark`, backed by the committed artifact
`docs/intermittent_demand.json` (produced by
`backend/seeds/run_carparts_backtest.py`), documented in
[docs/INTERMITTENT_DEMAND.md](INTERMITTENT_DEMAND.md).

Protocol: rolling origin, non-overlapping test blocks, refit at every origin
(primary config: horizon 6, 3 origins, train sizes 33/39/45, seasonality 12 —
shared with the macro backtest via `app.ml.backtest.rolling_origins`). Methods
(`zero`, `naive_last`, `climatology`, `croston`, `sba`, `tsb`) emit predictive
distributions (compound Bernoulli × zero-truncated negative binomial), scored
with MASE/RMSSE alongside CRPS and scaled pinball loss.

**The headline finding**, measured across **2,646** scored series: MASE ranks
the degenerate `zero` forecast **1st** (mean Friedman rank **1.66**) — because
MAE/MASE is minimized by the conditional median, and that median is usually zero
on a 24%-non-zero panel — while under proper scoring `zero` falls to **4th** on
CRPS (mean rank 3.67) and **5th** on scaled pinball loss (mean rank 4.12), and
`tsb` wins both. Friedman p < 1e-300; Nemenyi critical difference 0.1466 at
α=0.05. The point and distributional leaderboards disagree, and a horizon-12
sensitivity configuration (2,504 scored series) reproduces the same ordering.

**No dollar translation exists for this finding yet.** It says which forecasting
method is least wrong under a scoring rule that matches the decision's cost
structure — it does not by itself say what a stockout or an over-order costs in
dollars. Connecting the two (an explicit newsvendor critical fractile, a quantile
model fit at that fractile, evaluation in realised dollar cost rather than
forecast error) is Move 1 §1.4 in [docs/archive/ML_API_PUSH_PLAN.md](archive/ML_API_PUSH_PLAN.md)
and is not yet built. Stating that plainly is preferable to inventing an
illustrative dollar figure the way the retired per-part path did.

**Assumptions/citations (macro backtest only, §3b).**
- No dollar-conversion assumptions apply to this section anymore — see above.
- Gneiting & Raftery (2007), *JASA* 102(477):359–378 (proper scoring rules);
  Koning, Franses, Hibon & Stekler (2005), *IJF* 21(3):397–409 (MCB); full
  reference list in `docs/intermittent_demand.json` → `scoring`/`mcb`.

---

## 4. Per-route holding cost (already dollar-denominated)

The checkout cost breakdown already shows a real holding-cost line
(`cost_breakdown.holding_cost`). P3 adds a tooltip documenting its basis:

```
holding_cost = component_value × 25%/yr × (lead_time_days / 365)
```

cited to the same Gartner 2022 rate (`holding_cost_usd` in `costs.py`).

---

## Summary of constants introduced/reused

| Constant | Value | Source | Status |
|----------|-------|--------|--------|
| Inventory carrying rate | 25%/yr | Gartner IT Supply Chain Benchmarks 2022 | **Reused** (`ANNUAL_HOLDING_RATE`) |
| Service factor z | 1.645 (95%) | Standard normal / Silver-Pyke-Peterson | New, labeled assumption |
| Emergency premium | 15%/unsourceable line | Existing `EMERGENCY_COST_PREMIUM` | Reused |

No new hardcoded dollar figures were introduced into the application logic; all
dynamic dollar values are computed from real BOM spend and real simulation output.
