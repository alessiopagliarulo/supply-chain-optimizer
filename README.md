# Electronics Supply Chain Optimizer

[![CI](https://github.com/ApagPlayz/supply-chain-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/ApagPlayz/supply-chain-optimizer/actions/workflows/ci.yml)

A full-stack supply chain intelligence platform for electronic component procurement. Built on real market data: **791 components, 92 distributors, 8,176 price offers** — a static 2024 snapshot originally collected via the Nexar API (which aggregates Octopart), redistributed on HuggingFace under CC-BY-4.0. It is real, but it is a **frozen snapshot, not a live feed** ([docs/DATA_PROVENANCE.md](docs/DATA_PROVENANCE.md)).

## Headline results

Three results, each produced by a command in this repo and written down in a committed JSON
artifact you can open. **What is gated and what is not, precisely:**
`tests/test_docs_match_artifacts.py` regenerates each linked document's `<!-- GENERATED: -->`
regions from its artifact and fails on any difference, so the figures *in those documents* cannot
drift. The restatements *on this page* are hand-written and are not diffed figure-by-figure —
they were checked against the artifacts on 2026-09-07 and the link beside each one is where the
gated version lives.

- **Cost-vs-tail-risk frontier — 387 λ-solves** across four arms: 150 breadth (10 BOMs ×
  3 volumes), 27 on the headline BOM, 180 sensitivity and 30 SAA-endpoint-stability
  (347 converged; the 40 that did not are excluded from every figure).
  On the headline BOM **at 60,000 units**, moving from risk-neutral to the knee at λ = 0.3
  spends **$2,044 more in expectation and removes $8,719 of CVaR-95** — a chord ratio of
  **$4.27** of tail risk removed per $1 spent across that stretch. Past the knee the same
  trade returns **$0.41**. Both are averages over a stretch of the curve, not a marginal
  rate at a point. CVaR-95 is the **mean cost of the worst 5% of scenarios**, not a
  worst case. At 100× and 1,000× volume the `knee` is `null` — the frontier is flat there
  and there is no trade-off to price, which is why the volume condition is never dropped.
  → [docs/CVAR_EFFICIENT_FRONTIER.md](docs/CVAR_EFFICIENT_FRONTIER.md)
- **Intermittent-demand benchmark — 2,646 real spare-parts series**, where `zero` (forecast
  nothing) ranks **1st of 6 by MASE** and 4th–5th of 6 under proper scoring rules; Kendall's
  τ between the MASE and pinball orderings is **−0.20**, i.e. mildly *anti*-correlated.
  → [docs/INTERMITTENT_DEMAND.md](docs/INTERMITTENT_DEMAND.md)
- **Macro supply-stress regime model — 219 walk-forward folds** (2008–2026), Brier
  **0.393** against persistence 0.539 and climatology 0.673, calibration slope 0.629.
  **It ties persistence on accuracy — 0.7306 vs 0.7306, a dead heat** — and ships anyway,
  because accuracy is not the gate: the optimizer consumes a probability, and persistence
  can only ever emit 0 or 1. Its own ship-gate record says both halves; so does this line.
  An earlier version lost on the proper score too, and was refused rather than shipped.
  → [docs/MODEL_CI.md](docs/MODEL_CI.md)

[![Cost vs. CVaR-95 efficient frontier: expected cost on the x-axis, CVaR-95 on the y-axis, nine λ-solves falling from the risk-neutral plan down to the knee at λ = 0.3 and then flattening. A dashed chord marks the $4.27-per-$1 stretch.](docs/cvar_frontier.png)](docs/CVAR_EFFICIENT_FRONTIER.md)

*Drawn from `docs/cvar_frontier.json` by `backend/seeds/render_cvar_frontier_chart.py` — the
same artifact the prose above is checked against, so the picture and the sentence cannot
disagree. Every figure on it is read from the artifact; none is typed.*

---

> ### ▶ Live demo — [supply-chain-ui-bhwz.onrender.com](https://supply-chain-ui-bhwz.onrender.com)
>
> API reference (Swagger): **[supply-chain-api-qy8x.onrender.com/docs](https://supply-chain-api-qy8x.onrender.com/docs)**
>
> No signup — the login page has a one-click **Demo Login** button.
> **The page loads instantly. The first *data* request may take 50–120 s.** Two different services sit behind those two links, and only one of them sleeps. The UI is a Render **static site** — it never spins down, and every route answers in well under a second (measured: 0.04–0.50 s, SPA rewrites included). The API is a Render **free-tier web service**, which spins down when idle, so the first call after a quiet spell waits for it to wake. The login screen says so itself: an amber *"Free-tier backend is waking up"* banner appears after 3 seconds and stays until the response lands. Once awake, the API answers in well under a second too.

**Live demo flow:** Login → browse components → add to cart → run multi-objective VRP optimization → explore resilience scenarios.

![Live walkthrough: dashboard, adding a component to cart, the 4-strategy VRP optimizer, the CVaR efficient frontier, and a distributor-failure resilience scenario](docs/screenshots/demo-walkthrough.gif)

*Recorded from the live deployment above — dashboard → add to cart → optimizer results → CVaR efficient frontier → resilience scenario. Static screenshots of each page are further down.*

---

## What it does

**For a PCB manufacturer sourcing a BOM of electronic components across 92 real distributors:**

| Feature | Technical approach |
|---------|-------------------|
| Supplier selection | CP-SAT MILP (OR-Tools) — minimize landed cost under stock constraints (MOQ constraint implemented; inert on this catalogue, where MOQ is uniformly 1) |
| Route optimization | Exact TSP by exhaustive enumeration for tours ≤ 8 stops (every live request); OR-Tools routing with PATH_CHEAPEST_ARC + Guided Local Search above that threshold, not exercised on the current catalogue |
| 4 strategies on the cost/time/carbon frontier | Multi-objective weighted sum. Distinct when the BOM is big enough to separate them — the demo cart returns **3 distinct plans across 4 strategies** — and the UI names the collapse when it happens instead of showing four cards as four answers (`strategy_divergence` in the response) |
| Delivery uncertainty | Monte Carlo simulation (1,000 scenarios) → P10/P50/P90 ETA bands. **The input distribution is assumed, not fitted**: a Normal(1.0, 0.15) transit multiplier on the route ETA plus a 4-point disruption mixture (0/1/3/7 days at 0.85/0.08/0.05/0.02). This repo holds DigiKey *factory* lead times and no record of realised delivery dates, so there is nothing here to calibrate it against — read the band as a seeded sensitivity range, not an empirical service level. Every response carries the same caveat in `monte_carlo_assumptions` |
| Network fragility | Graph ML: Fiedler algebraic connectivity, betweenness centrality, HHI, k-core decomposition |
| Resilience scenarios | Distributor failure cascade, geopolitical risk overlay, delivery target optimization |
| Demand-method benchmark | Croston/SBA/TSB scored on CRPS + scaled pinball loss, not just MASE, across 2,646 Monash car-parts series — MASE and proper scoring pick different winners |
| Live risk feeds | IMF PortWatch port congestion and FRED freight indices (live, actively published); GPR index (downloaded live on a 15-min tick, but the published archive's newest observation is **September 2021** — `/feeds/status` now reads the observation date, not just the download time, and reports it `stale` with the date rather than `live`); ACLED conflict data (needs a key, labelled `inactive` without one) |

---

## I audited my own headline and retracted it

The benchmark used to claim the optimizer was **44.7% cheaper than a greedy buyer**.
That number is arithmetically correct and substantively meaningless, so rather than
quietly deleting it, here is the decomposition.

The greedy baseline buys each BOM line from whoever is cheapest, which makes it the
**component-cost minimum by construction** — the MILP *cannot* beat it on component
cost. It can only win on fixed charges. And every distinct supplier you open costs a
flat **$75** (LTL) or **$150** (air) freight fee. Now look at the scale the benchmark
ran at:

| `iot_sensor_node`, as benchmarked (4 parts, 5 units) | |
|---|---:|
| Component cost | **$6.96** |
| Fixed freight fees | **$450.00** |
| Variable freight + consolidation | $11.02 |
| Total "landed cost" | $467.98 |

**Fixed fees are 96.2% of the cost being optimized.** Consolidating 3 suppliers into 1
avoids $337.50 of fees and books a **71.7% saving** — on a *seven-dollar* order.

Aggregated across all 10 BOMs (pooled: sum of greedy costs vs sum of MILP costs), the
decomposition is damning:

| Source of the $3,304 "saving" at benchmark scale | |
|---|---:|
| Avoided fixed per-supplier fees | **+$3,863** |
| Variable freight | +$2 |
| **Component cost** | **−$561** ← *the MILP pays **more** for the parts* |

**Fixed fees are 117% of the saving.** The MILP loses on component cost in **10 of 10**
BOMs — it must, since greedy is the component-cost minimum — and funds that loss, plus
the entire headline, out of avoided supplier fees.

That saving is a **constant** (`$112.50–$225 per supplier avoided`, after the 1.5×
`transport_penalty_scale`), not a rate — so as volume grows, only the denominator moves:

| Volume | Savings vs greedy (pooled) |
|---|---:|
| 4–9 units *(as benchmarked)* | **47.2%** |
| ~50 units | 23.1% |
| ~500 units | 8.5% |
| ~5,000 units | 5.0% |
| 2,000–60,000 units *(500×–10,000×)* | **2.6% – 8.0%** |

(`iot_sensor_node`, the BOM that books **71.7%** at prototype scale, goes **71.7% → 7.4%** on its own.)

**The 45% headline is dead. Do not quote it.** At any volume a real manufacturer would
order, the cost edge is single digits.

### The audit found a real bug — and fixing it cut *against* the retraction

Chasing that decaying curve turned up a genuine defect in the freight model: it computed
one representative shipment weight for the whole BOM and then charged **every** opened
supplier that full weight, so splitting an order across 3 suppliers was billed 3× a full
BOM's variable freight instead of dividing one BOM's freight across 3 shipments. It
corrupted **both** arms (they share the cost function by design), and it made distance
almost free at volume.

Freight is now `fixed[d]·opened(d) + per_unit[d]·units_shipped_from(d)` — still linear,
so CP-SAT models it exactly. And the corrected model makes the optimizer look **better**
at scale, not worse: the fixed-fee wedge collapses to zero (at ≥500× the MILP opens
*more* suppliers than greedy on purpose), and the residual 2.6–8.0% edge comes from
**routing volume by price + freight** rather than by unit price alone — something greedy
structurally cannot do. That part scales with volume and is honestly earned.

Reporting a correction that helps my own number is the same discipline as reporting one
that hurts it.

What the optimizer genuinely provides beyond that: *feasibility and flexibility* — it
respects stock, it can split a line across distributors, and it proves optimality on the
cost/time/carbon tradeoff.

Full decomposition, methodology and the reproduce script:
**[docs/BENCHMARK_VOLUME_CURVE.md](docs/BENCHMARK_VOLUME_CURVE.md)**.

> Auditing this also surfaced a genuine production bug: `sourcing.py` keyed its CP-SAT
> variables on `(component, distributor)` while the offer table stores one row per
> price-break tier, so 509 duplicated pairs were being summed into the demand constraint
> and priced into the objective. `STM32F103C8T6` from Verical was costed at **$30.03/unit
> against a true $2.86**, so the solver had been systematically avoiding multi-tier
> distributors. Fixed, with regression tests.

---

## Dollar-denominated impact

Every headline metric is paired with a concrete financial interpretation, derived
from real computed quantities — never an invented figure. The conversions are
surfaced live in the dashboard (resilience banner, benchmark strip, holding-cost
tooltips) and summarized here.

| Metric | Where it comes from | Dollar translation |
|--------|---------------------|--------------------|
| **CVaR-95** (tail-risk) | Mean emergency-procurement cost multiplier over the worst-5% of 1,000 Monte Carlo cascade scenarios (`graph/simulation.py`) | **"$X of procurement spend at risk"** = real baseline BOM spend × (CVaR-95 − 1). Computed per BOM in `resilience.py` (`procurement_spend_at_risk_usd`) and shown on the Resilience page; aggregated per reference BOM on the Benchmark page (`baseline_spend_at_risk_usd`). **Caveat — read this before trusting the number:** the *spend* side is real, and the *probability* side is now calibrated, not proxied. Distributor failure probability is anchored to a cited base rate — McKinsey Global Institute (Aug 2020): disruptions lasting a month or longer roughly every 3.7 years — converted to an annual Poisson rate and then to a probability over a 60-day purchase-order exposure window; betweenness centrality only rank-orders *relative* risk around that base rate (a `centrality_spread=1.0` sensitivity arm removes centrality's effect entirely), and every probability is capped at 50%. On the live headline BOM this puts calibrated `p_fail` between 1.45% and 13.04% across its six suppliers — it no longer saturates near a fixed number. What's still assumed, not measured: the McKinsey rate is firm-level, so applying it to one distributor is almost certainly too high, and nothing establishes that centrality actually predicts disruption likelihood (the code names this and ships the `spread=1.0` arm precisely because of it). See [docs/CVAR_EFFICIENT_FRONTIER.md](docs/CVAR_EFFICIENT_FRONTIER.md). |
| **Optimizer cost delta** | Graph-aware MILP vs blind MILP total landed cost, over the **9 of 10** reference BOMs the run actually scores (`benchmark.py`). `audio_dsp_board` is excluded because the blind arm, which sources domestically only, raises `ValueError: Insufficient stock` before the solver runs (GD25Q127CYIGR needs 1, 0 in stock at any US distributor); the exclusion and its reason are recorded in `bom_inclusion` in [docs/benchmark_results.json](docs/benchmark_results.json), and `/benchmark/summary` reports `n_boms: 9` | **"$Y per BOM run"** = mean(graph-aware − blind `total_cost_usd`), served live as `cost_delta_usd`. Surfaced as a real, run-dependent figure rather than a fixed claim — and on the current reference set **it is a cost, not a saving**: graph-aware runs **$59.99 more expensive per BOM** (`cost_delta_usd`, the mean of the nine per-BOM dollar deltas), while the mean of the nine per-BOM percentage premiums is **+31.0%** (`cost_delta_pct`). Those are two different aggregations and are deliberately no longer printed as one figure's percentage of the other: a mean of differences and a mean of ratios do not divide into each other, and `$59.99 / 31.045%` implies a $193.23 base that is no arm's cost. The spread is what breaks it — two BOMs sit at 0% and `iot_sensor_node` at 82.16% (`benchmark.py:1414-1420`, `value_of_resilience[].nominal_premium_pct`). The Benchmark page prints exactly that ("nominal cost premium … $59.99 more expensive / BOM run") and says it is *the price of the resilience below, not a reversal of the optimization result*. Note the 2% materiality threshold this is measured against is a reporting convention fixed a priori — the API states in `materiality_threshold_basis` that it is **not** a measured noise floor, because the benchmark is a single deterministic solve (seed 42, one search worker) with no replicates from which run-to-run variance could be estimated. |
| **Forecast WAPE** (macro backtest, kept) | Walk-forward backtest (3 rolling origins, 12-month horizon) on Census M3 `A34SNO` (Manufacturers' New Orders: Computers & Electronic Products), 198 monthly obs, **pinned to ALFRED vintage 2026-08-16**: Prophet **3.13%** vs seasonal-naive **4.80%** — skill score **+34.8%**. Under the **real-time protocol** — each origin trained only on the vintage that existed on its date, because Census revises this series *in place* — Prophet is **4.13%** vs naive **5.87%**, skill **+29.6%**. The revised-data figures are optimistic by ~24%; the real-time pair is the number you could actually have achieved, and it is the one to quote ([docs/FORECAST_BACKTEST.md](docs/FORECAST_BACKTEST.md)) | **No dollar translation.** This number used to feed a "≈N weeks of safety stock" tooltip on a per-part forecast — that forecast is gone (its magnitude was `total_stock/52 × risk_score`, inferred from inventory, not measured), and the safety-stock dollar figure went with it rather than being carried over with no live consumer. The macro WAPE above is real and stands on its own as a Prophet-vs-naive comparison on an aggregate industry series; it says nothing about per-part accuracy. **What now measures demand-forecast quality:** an intermittent-demand method benchmark on 2,646 Monash car-parts series — MASE ranks the degenerate `zero` forecast 1st (mean rank 1.66) while proper scoring ranks it 4th on CRPS / 5th on scaled pinball loss, and `tsb` wins both (Friedman p < 1e-300). See [docs/INTERMITTENT_DEMAND.md](docs/INTERMITTENT_DEMAND.md). That benchmark doesn't translate to dollars yet — connecting it to the sourcing decision is open work ([docs/archive/ML_API_PUSH_PLAN.md](docs/archive/ML_API_PUSH_PLAN.md) §1.4). |

### Conversion assumptions & citations

- **Inventory carrying cost = 25%/yr.** Reused from the existing optimizer constant
  `ANNUAL_HOLDING_RATE = 0.25` (`backend/app/optimization/costs.py`), cited to
  **Gartner IT Supply Chain Benchmarks 2022** (electronics annual holding rate). The
  same rate already drives the per-route holding cost shown at checkout. Industry
  ranges are typically 20–25%/yr (Richardson, *Harvard Business Review*; APICS).
- **Service level z = 1.645** (95%, one-sided normal) for the safety-stock buffer.
  WAPE is used as a σ/μ forecast-error proxy over the planning horizon — a standard
  textbook safety-stock framing (Silver, Pyke & Peterson, *Inventory Management and
  Production Planning*).
- **CVaR-95 → dollars: the spend side is real, and the probability side is now
  calibrated against a cited base rate — precisely which parts, and which parts
  are still assumed.** The *spend* side is real: it multiplies by the real BOM
  spend (sum of each line's average real offer price). The *probability* side
  (`optimization/stochastic.py`) starts from McKinsey Global Institute (Aug 2020):
  "companies can now expect supply chain disruptions lasting a month or longer to
  occur every 3.7 years," treated as a Poisson rate and converted to a probability
  over the 60-day purchase-order exposure window. Betweenness centrality
  (`graph/simulation.py`) only rank-orders *relative* risk around that calibrated
  base rate — the most central supplier gets `spread`× it, the least central gets
  1/`spread`×, capped at 50% — and `centrality_spread=1.0` (centrality ignored
  entirely) is a supported, published sensitivity arm. On the live headline BOM
  this gives calibrated `p_fail` of 1.45%–13.04% across the six suppliers, not a
  saturated constant. What's still an assumption, not a measurement: the McKinsey
  rate is firm-level, not per-supplier, so applying it to a single distributor is
  almost certainly too high; and nothing establishes that centrality actually
  predicts disruption likelihood at all (arguable either way — the code names this
  explicitly and ships the `spread=1.0` arm because of it). Earlier drafts of this
  README called the pre-calibration version "fully data-derived," which was an
  overstatement that was corrected; this is the current, calibrated state.

### What this model can't do

Stated up front, because an interviewer will find these anyway and it is better that
they hear it from me:

- **There is no per-part demand forecast in this app, and there wasn't a good one
  before.** It used to compute `total_stock / 52 × risk_score` and call the result
  "demand," then fit Prophet on top — a magnitude *inferred from inventory position
  and a risk multiplier*, not measured, and identical in shape across all 791 parts.
  The forecast window it produced also closed 17 months before this line was
  written, against which no actuals were ever recorded — unscoreable even in
  principle. It was removed rather than patched (migration
  `0008_drop_synthetic_demand_tables.py`), because no public per-SKU demand series
  exists for electronic components. The Census `A34SNO` backtest above is real and
  unaffected by the deletion — it never depended on those tables — but it measures
  an aggregate industry series, not this app's parts, so the 3.13% WAPE (4.13%
  under the real-time protocol) is still not evidence about per-part accuracy.
  What replaced the per-part claim is a method
  benchmark on a real intermittent-demand panel (Monash car parts) — see the
  demand-method row above and [docs/INTERMITTENT_DEMAND.md](docs/INTERMITTENT_DEMAND.md).
- **Disruption probabilities are structural, not empirical** (see the CVaR caveat above).
- **The lead-time panel is 3,406 real observations across six snapshot dates**
  (75 on 2026-07-01, 742 on 2026-08-15, 363 on 2026-08-17, 742 on 2026-08-24, 742 on 2026-08-31, 742 on 2026-09-07), all from DigiKey — one distributor, not a cross-distributor consensus. 791
  of 791 parts were polled on 2026-08-15; 6.2% missed (43 not in DigiKey's catalog, 6 in
  the catalog with no published lead time), and that miss list is in
  `seeds/data/lead_time_panel/collection_log.csv`.
- **The served lead-time model is one snapshot behind the panel, and that gap is
  published rather than hidden.** The collector runs weekly; the model is retrained by
  hand, so the two drift apart between retrains. The deployed artifact was trained
  **2026-09-03** on the **2,615** usable rows of the then 2,664-row, five-snapshot cut of
  the panel, with **324** features — every `2,615` / `472` / `28` / `324` figure below
  describes *that artifact*, not the panel on disk today.
  `GET /api/v1/ml/model-info` publishes both sides: the training count, and a
  `training_data_staleness` block that compares the panel sha256 the artifact recorded at
  fit time (`c68e2891…`) with the file on disk (`d94df904…` since the 2026-09-07
  collector run). They differ, so it currently reports `stale: true` and names the
  retrain command. The tripwire is deliberately a warning and not a build failure, so a
  scheduled collector commit cannot turn CI red by itself.
- **Any lead-time R² must come from a *grouped* split, not a random one.** The dataset
  contains large near-duplicate part families (456 STM32F103 rows, 147 ATMEGA328),
  and `base_product` alone explains **R²=0.856 of the target in sample** (361 levels
  over 2,615 rows — an in-sample identity-column figure, not a model score and not
  cross-validated). A random split therefore scores memorization of a part family, not
  prediction. Measured over 50 folds — same estimator, same 2,615 rows, same feature
  pipeline, only the grouping changes (all four figures below are properties of the
  **2026-09-03 artifact vintage** described above — 2,615 rows, 5 snapshots):

  | Split regime | R² mean | R² median |
  |---|---:|---:|
  | random rows (**the wrong protocol**) | **+0.825** | +0.826 |
  | `GroupKFold` by part family (**472 family grouping keys**) | **+0.073** | +0.140 |
  | `GroupKFold` by manufacturer | **−0.697** | −0.104 |

  The effective sample size for generalization is **28 manufacturers, not 2,615 rows**.
  A negative R² on held-out manufacturers means the model's squared error exceeds
  that vendor's whole label variance — no explanatory power at all on a vendor it has
  never quoted. The family split groups on the same `_group_key` the shipped model
  uses — `base_product` where it exists, MPN or row otherwise — which is **472
  grouping keys**, not the 361 raw `base_product` levels quoted above for the
  in-sample identity check; the two numbers count different things and both come from
  `docs/leakage_progression.json` (`counts.n_family_group_keys` = 472,
  `identity_column_in_sample_r2.base_product.n_levels` = 361) and from the served
  `metrics.joblib['lead_time_leakage_audit']['n_families']` = 472. Grouped by part
  family is the only split I would defend, and even that one is optimistic relative to
  how the model is deployed. (An earlier revision
  of this table quoted an 810-row, 27-manufacturer, `random_forest` vintage — R²
  +0.638 / +0.082 / −0.550 — that two retrains had already superseded by 2026-08-26;
  those numbers are retired.) The served artifact
  (`metrics.joblib['lead_time_leakage_audit']`, 20 repeated `GroupShuffleSplit`
  holdouts rather than 50 `GroupKFold` folds) reports the same collapse on the same
  2,615 rows: +0.8341 → +0.1255 → **−0.4422**, and that is the figure
  `GET /api/v1/ml/model-comparison` serves. Full protocol, per-fold scores and the
  naive baselines on identical folds:
  [docs/LEAKAGE_PROGRESSION.md](docs/LEAKAGE_PROGRESSION.md) /
  [docs/leakage_progression.json](docs/leakage_progression.json), regenerated with
  `cd backend && python -m seeds.run_leakage_progression`.
- **Prices are a frozen 2024 snapshot**, so nothing here reflects today's market.

See [docs/IMPACT_FRAMING.md](docs/IMPACT_FRAMING.md) for the full derivations.

---

## Quick Start (no Docker required)

Nothing to install if you just want to look: use the **[live demo](https://supply-chain-ui-bhwz.onrender.com)** above.
To run it locally, see **[QUICK_START.md](QUICK_START.md)** for step-by-step setup.

**TL;DR:**
```bash
# Terminal 1 — backend
cd backend && source venv/bin/activate
python -m uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend && npm run dev
```

Open http://localhost:5173 → click **Demo Login**.

---

## Tech Stack

**Backend:** Python 3.11 · FastAPI · SQLAlchemy · SQLite (dev **and** current production — `render.yaml` pins `DATABASE_URL=sqlite:///./supply_chain.db`; PostgreSQL support exists in the SQLAlchemy layer via `psycopg`, but nothing is deployed on it) · OR-Tools · NetworkX · scikit-learn (Prophet is installed and used by the offline `seeds/` backtests; no API route imports it)  
**Frontend:** React 19 · TypeScript · Vite · Tailwind CSS v4 · Recharts · Zustand · MapLibre GL + deck.gl (map)  
**Algorithms:** CP-SAT MILP, TSP, Monte Carlo simulation, Spectral Graph Theory  
**Data:** Nexar/Octopart static 2024 snapshot (real component pricing), DigiKey API (3,406 real observed lead times + live pricing), Nexar & OEMsecrets live pricing, FRED and IMF PortWatch (live), GPR index (downloaded live, but the published archive's newest observation is **September 2021** — see the feeds note below), ACLED (needs a key — reports as inactive without one)

---

## Architecture

```
frontend/src/
  pages/          Dashboard, Map, Scheduler, Cart, Checkout, Resilience, Benchmark, Frontier,
                  Newsvendor, ModelCard, Login, Register, NotFound
  components/     NavBar, ScenarioCard, DeltaCard, MonteCarloChart, BOMImpactTable, CiStrip,
                  BomCostBreakdownTable, CriticalitySweepTable, DualSourcingTable, TornadoChart,
                  VolumeDecayCurve, DistributorSelector, ErrorBoundary, map/
  store/          Zustand: authStore, cartStore, optimizeStore
  services/api.ts Axios client for all backend endpoints
frontend/scripts/
  ui-gate.cjs     the automated browser gate — 256 checks against the live site (see Tests)

backend/app/
  api/            FastAPI routers: auth, cart, components, distributors, optimize, stochastic,
                  resilience, graph, benchmark, demand, newsvendor, ml, feeds, live_prices
  optimization/   CP-SAT sourcing MILP, OR-Tools TSP, cross-dock facility location
  graph/          NetworkX bipartite supply graph, Fiedler curve, centrality metrics
  feeds/          Live data fetchers: GPR, ACLED, IMF PortWatch, FRED freight
  ml/             Prophet macro (A34SNO) backtest + Chronos comparison, sklearn lead-time
                  prediction, FRED regime model, intermittent-demand method benchmark
                  (Croston/SBA/TSB, shared rolling-origin protocol)
  cache.py        SHA256-keyed scenario cache, 1h TTL, background cleanup
  supply_chain.db SQLite — 791 components, 92 distributors, 8,176 price offers (real data)
```

```mermaid
flowchart TB
    subgraph FE["Frontend — React + TypeScript"]
        UI["Pages: Dashboard, Cart, Optimizer,<br/>Resilience, Frontier, Benchmark, Model Card<br/>(Zustand store, Axios client)"]
    end

    subgraph BE["Backend — FastAPI"]
        API["REST routers:<br/>auth · cart · components · distributors<br/>optimize · stochastic · resilience · graph<br/>benchmark · demand · newsvendor · ml<br/>feeds · live_prices"]
    end

    subgraph OPT["Optimization & Risk — OR-Tools"]
        SOURCING["CP-SAT sourcing MILP<br/>optimization/sourcing.py"]
        ROUTING["TSP routing<br/>(guided local search)<br/>optimization/routing.py"]
        STOCH["Two-stage stochastic program<br/>+ CVaR efficient frontier<br/>optimization/stochastic.py"]
        GRAPHSIM["Bipartite supply graph +<br/>Monte Carlo cascade sim<br/>graph/builder.py, graph/simulation.py"]
    end

    subgraph ML["Forecasting & ML"]
        LEADTIME["Lead-time regression<br/>(scikit-learn, GroupKFold)<br/>ml/lead_time_model.py"]
        DEMAND["Demand: Croston/SBA/TSB benchmark<br/>ml/intermittent.py — GET /demand/benchmark<br/>serves committed docs/intermittent_demand.json"]
        OFFLINE["Prophet macro (A34SNO) backtest +<br/>Chronos-Bolt comparison<br/>seeds/ scripts, run offline —<br/>not called by the API, not in the deploy image"]
        SERVING["Lead-time model serving<br/>MLflow champion → joblib fallback<br/>ml/serving.py<br/>Live: local_joblib (no MLflow server deployed)"]
    end

    subgraph DATA["Data Layer"]
        DB[("SQLite<br/>791 components · 92 distributors<br/>8,176 offers<br/>(render.yaml pins sqlite:/// in prod)")]
        ARTIFACTS[("Model artifacts<br/>data/ml_models/*.joblib (served)<br/>+ local MLflow store (training only,<br/>not present in the deployed image)")]
    end

    subgraph EXT["External Data Sources"]
        NEXAR["Nexar / Octopart<br/>(frozen 2024 snapshot, seeded)"]
        DIGIKEY["DigiKey API<br/>(live lead times + pricing)"]
        MACRO["FRED · IMF PortWatch<br/>GPR index · ACLED"]
    end

    subgraph CICD["CI / Model Governance"]
        CI["ci.yml — tests + lint<br/>(gates merges to main)"]
        MODELCI["model-ci.yml — 52 gates<br/>retrain, schema parity,<br/>baseline, coverage, provenance"]
        COLLECTOR["collect-lead-times.yml<br/>weekly DigiKey collector"]
    end

    UI -->|Axios / REST| API
    API --> SOURCING
    API --> ROUTING
    API --> STOCH
    API --> GRAPHSIM
    API --> LEADTIME
    API --> DEMAND
    STOCH --> GRAPHSIM
    SOURCING --> DB
    ROUTING --> DB
    GRAPHSIM --> DB
    API --> DB
    LEADTIME --> SERVING
    SERVING --> ARTIFACTS
    NEXAR -->|seeded once| DB
    DIGIKEY -->|live calls + weekly collection| DB
    DIGIKEY --> LEADTIME
    MACRO -->|live feeds| API
    COLLECTOR --> DB
    COLLECTOR --> MODELCI
    MODELCI --> ARTIFACTS

    classDef offline stroke-dasharray: 5 5;
    class OFFLINE offline;
```

Lead-time and demand-forecast training runs are tracked with MLflow (params, real backtest metrics, model artifacts, champion promotion) — 21 runs across 2 experiments. The regime model and the benchmark/newsvendor/leakage scripts write committed JSON artifacts instead — see [docs/MLFLOW.md](docs/MLFLOW.md).

---

## Screenshots

Both are captures of the **live deployment** at `85b2890`, taken from the demo cart a
one-click Demo Login gives you (5 lines, 225 units). Every figure in them was a field of
the response the API returned *at capture time*, and you can reproduce either one against
the live API in about a minute — but **both PNGs now sit behind a later fix and neither is
current.** The prose and alt text beside each one carry the re-derived figures; the
blockquote under each says exactly what its PNG still shows and why. Where they disagree,
the API is right and the image is stale.

### `/optimize` — four strategies, three genuinely distinct plans

![The Route Optimization page comparing four sourcing strategies side by side. Lowest Cost: $374, 7.0d median ETA, 89.6 kg CO2. Fastest Delivery: $747, 4.6d, 1.6 kg. Lowest Carbon: $735, 5.7d, 0.9 kg. Balanced (recommended): $747, 4.6d, 1.6 kg. An amber banner above the cards reads "SOME STRATEGIES ARE TIED" and explains that Fastest Delivery and Balanced returned the same plan, so 3 distinct plans were found across 4 strategies.](docs/screenshots/optimize-four-strategies.png)

The trade-off is the point: **$374.02 / 6.9 d / 89.6 kg** buys everything from one cheap
Singapore distributor, and **$747.44 / 4.5 d / 1.65 kg** splits it across three domestic
suppliers — roughly **2× the cost for 2.4 days and 54× less carbon**. Lowest Carbon holds
a third, distinct position (**$735.01 / 5.5 d / 0.936 kg**).

> **The CO₂ figures above were re-measured on 2026-09-03 and the screenshot has not yet
> caught up.** `costs.py::co2_kg` was dividing freight weight by a metric 1000 while the
> 161.8 g truck factor is per US *short* ton-mile, so every truck CO₂ number this project
> published was 9.28% low. Fixing it multiplied all of them by exactly ×1.102311: the two
> truck-only plans moved 1.49 → 1.65 kg and 0.849 → 0.936 kg. The air-dominated Lowest Cost
> plan is unchanged at 89.6 kg, because the air factor is already per metric tonne-km — and
> that is why the headline ratio fell from 60× to **54×**: only the denominator moved. The
> costs and ETAs above re-run to the cent and the day. **The PNG still shows the pre-fix
> 1.5 / 0.8 kg and is queued for re-capture.**

Note the amber banner. Two of the four strategies (Fastest Delivery and Balanced) return
the *same* plan on this BOM, and the page says so out loud rather than presenting four
cards as four answers — the strategies are ranked only where they actually differ, and
`strategy_divergence.distinct_plans` in the response is the number the banner prints.

### `/resilience` — losing the distributor the cart leans on

![The Resilience Scenarios page after simulating the failure of Weyland Electronics Group Pte. Ltd. A headline banner reads "SUBSTITUTION COST - NO BOM LINE ORPHANED, $42.11 (+25.2%)" beside "MODELLED FULFILMENT (P50) 100% to 80% (-20 pts)". Four delta cards below show Total Cost 167.61 to 215.33 USD (up 28.5%), Fulfilment P50 100% to 80% (down 20 points), Delivery ETA 26.6 to 23.4 days (down 3.2 d), and Risk Score 0.220 to 0.420 (up 0.200). This capture is STALE in every figure except the substitution banner and the ETA card — the note below the following paragraphs says which and why.](docs/screenshots/resilience-distributor-failure.png)

The scenario fails the distributor four of the five cart lines are sourced from. Cost
rises **25.4%** ($167.19 → $209.63) — re-sourcing **4 of 5 lines** to the next-cheapest
surviving offer, **$42.11** of substitution on a $166.94 goods bill, the single largest
line being `ESP32-WROOM-32UE-N4` at **+$32.95**. CVaR-95 procurement spend at risk is
**$5.01**. Modelled fulfilment is **unchanged at 100%** (P10/P50/P90 all 1.000 on both
sides) and the risk score is **unchanged at 0.220**.

The ETA *improves* (26.6 → 23.4 days) and the page explains why instead of hiding it: the
ETA is the slowest line of the plan priced beside it, and the cheap Singapore supplier is
also the distant one, so being forced onto the next-cheapest surviving offer lands the BOM
sooner while costing more. **No** BOM line is orphaned, all 5 keep a supplier, and here
that really does mean no fulfilment impact — the endpoint says so in its own `hedging`
block rather than leaving zeros to be read as a broken computation. The honest summary is
that losing this distributor costs money and *buys* time, and nothing else.

> **These figures were re-derived on 2026-09-04 from the live API at `56f439e` and the
> screenshot has not caught up.** The PNG was captured while `graph/builder.py` still
> held out 20% of the supplier–part links (the 20% holdout carve described under *How
> this was built*, below), so the Monte Carlo ran on a graph of **5,789 edges instead of
> 7,363** — 1,574 of the 8,176 offer rows never entered it — and it starved BOM lines of
> suppliers that really exist. That manufactured a **20-point median-fulfilment drop**,
> and because `scenario_risk = baseline_risk + fulfilment_drop`, it manufactured the
> **+0.200 risk rise** on top of it. On the corrected graph both deltas are exactly
> **0.0** — so the PNG still shows a `MODELLED FULFILMENT (P50) 100% → 80% (-20 pts)`
> headline, a `$167.61 → $215.33 (+28.5%)` cost card, a `Risk Score 0.220 → 0.420` card,
> and a `$9.01` CVaR-95 spend-at-risk line — **none of which the API returns any more**
> (it now returns `167.19 → 209.63`, fulfilment flat at 1.000, risk flat at 0.220, and
> `procurement_spend_at_risk_usd = 5.01`). The `$42.11 (+25.2%)` substitution banner
> beside them is **not** stale, and an earlier revision of this section wrongly listed it
> as retired: the live endpoint still returns `substitution_delta_usd = 42.11` exactly
> (`209.05 − 166.94`), re-verified 2026-09-08. It is the cards around that figure the
> corrected graph moved, not the figure itself. The fulfilment headline does not render at
> all now, because there is no impact for it to report; only the ETA card (26.6 → 23.4 d)
> survives unchanged. **The PNG is queued for re-capture.**

---

## Key API Endpoints

```
POST /api/v1/auth/demo                       # one-click demo login
GET  /api/v1/components                      # 791 real electronic components
POST /api/v1/optimize/vrp                    # 4-strategy VRP: cheapest/fastest/greenest/balanced
GET  /api/v1/graph/metrics                   # Fiedler value, centrality, HHI, k-core
POST /api/v1/resilience/distributor-failure  # simulate distributor outage -> cost/ETA/risk delta
POST /api/v1/resilience/geopolitical-risk    # what-if risk-score stress dial (reads no live feed)
POST /api/v1/resilience/delivery-target      # "who can hit 14 days?" -> supplier capability list
POST /api/v1/stochastic/frontier             # two-stage stochastic program -> CVaR-95 efficient frontier (the 387-solve artifact)
GET  /api/v1/ml/stress                       # macro supply-stress regime model (219 walk-forward folds, Brier 0.393)
GET  /api/v1/ml/model-info                   # served lead-time artifact provenance + training_data_staleness
GET  /api/v1/demand/benchmark                # intermittent-demand method benchmark (Croston/SBA/TSB, CRPS+MASE, Monash car parts)
GET  /api/v1/feeds/status                    # feed freshness: download date, plus observation date where the feed publishes one (today: GPR)
GET  /api/v1/benchmark/summary               # network resilience metrics snapshot
```

Full API reference (live Swagger UI): **https://supply-chain-api-qy8x.onrender.com/docs** — or http://localhost:8000/docs when running locally  
Scenario API reference: [docs/archive/SCENARIO_API.md](docs/archive/SCENARIO_API.md)

---

## Tests

### Backend

```bash
cd backend
./venv/bin/python -m pytest tests/ -q
```

**Measured 2026-09-08 on the committed database with `-n auto --dist loadfile`:
`1 failed, 1335 passed, 4 skipped` in 496 s.** The parallel flags change the selection not
at all (verified node id by node id on 2026-09-05), so the serial command above reports the
same outcomes and takes about three times as long. What CI runs is `-m "not slow"`, which is
`1331 passed, 3 skipped, 0 failed` in 527 s — note that this selection is **green**, because
the one failing test below is marked `slow` and so neither CI workflow can see it.
The one red is named here rather than buried:

| Failing test | Why, and what it means |
| --- | --- |
| `test_artifacts_pinned_to_code.py::test_leakage_progression_reproduces_from_the_live_lead_time_model` | **The drift tripwire doing its job.** The weekly collector committed the 2026-09-07 snapshot, which moved the panel from 2,664 rows / 5 snapshots to 3,406 / 6. The served artifact was fitted on the earlier cut, so the published leakage figures no longer reproduce from the live model — a retrain (`python -m seeds.train_ml_models`, then `python -m seeds.run_leakage_progression`) is owed and has not been done. It is marked `slow`, so CI deselects it and a fresh collector commit cannot turn the badge red on its own; that is deliberate, and the gap is published on `GET /api/v1/ml/model-info` as `training_data_staleness: stale: true` rather than hidden. It is cleared by retraining, **never** by editing an artifact. |

A suite that reported "all passed" while a published figure had stopped reproducing would
be worse than this.

`test_model_ci_gates.py::test_the_served_estimator_is_the_one_the_metrics_describe` used
to stand here as a second, permitted failure — a local-only MLflow registry identity check
that was always green in CI. The 2026-09-03 retrain cleared it and it now passes locally
too, so it is no longer an exception the reader has to hold in their head.

Coverage: optimization solver (sourcing, routing, cross-dock), graph metrics, ML models
and their published-artifact pins, resilience API, auth guards, feed integrations.

### Frontend

The frontend **does** have an automated test suite. It is a browser gate, not a unit-test
runner, and it runs against the **live deployment**:

```bash
cd frontend
BASE=https://supply-chain-ui-bhwz.onrender.com npm run ui-gate
# -> 256 passed, 0 failed
```

Also a real run, 2026-09-07, against the live deployment. `scripts/ui-gate.cjs` drives a real
Chromium over **all 10 routes at 4 viewports** (390 / 768 / **1280** / 1440 — 1280 is
there because a nav regression lived exactly at that breakpoint) and asserts what a human
would otherwise have to notice:

- **horizontal overflow**, measured against the real scroll container — every route renders
  inside `overflow-y-auto`, so `document.scrollWidth` reports a false clean
- **clipped SVG chart labels** — `scrollWidth > clientWidth` never fires on SVG text, which
  is how three clipped axis labels shipped unnoticed
- **chart geometry** (the tallest bar must clear 8px) and **legend-vs-axis-label overlap**
- **chart legend contrast**, hand-rolled by compositing alpha through a 1px canvas, because
  axe-core returns *incomplete* rather than a violation for recharts legend labels
- **axe-core serious/critical** — and deliberately *not* hand-rolled contrast for Tailwind
  colours: Tailwind v4 emits `oklch()`, and a naive rgb parser returned a false clean on 32
  real failures
- **leaked JS placeholders** in user-visible text, **text under 11px** (prose under 12px),
  **touch targets ≥ 44px** (with WCAG 2.5.5's inline-link exemption), emoji in product UI,
  **head tags**, and **console/page errors**
- a route rendering the 404 page **fails** — a missing route trivially passes every other
  check, so without this the gate reports a clean sheet on a dead page

Type-checking and the production build:

```bash
cd frontend
npx tsc -b --force && npm run build
```

> **Use `tsc -b`, never `tsc --noEmit`.** The root `tsconfig.json` is a *solution* file
> (`"files": []` plus `references`), so `tsc --noEmit` typechecks **nothing** and exits 0 on
> any error. Verified again on 2026-09-02 by planting `const x: number = "not a number"` in
> `src/pages/NotFoundPage.tsx`: `npx tsc --noEmit` exited **0**, `npx tsc -b --force`
> reported `error TS2322`. `npm run build` uses `tsc -b`, which is why CI catches what a
> bare `--noEmit` would wave through.

---

## Model CI — gates derived from bugs that actually shipped

A second workflow, [`model-ci`](.github/workflows/model-ci.yml), asks a different
question from `ci.yml`: not *"does the code work?"* but *"is the model fit to
serve?"*. It retrains the lead-time model on the committed observed panel and
fails the build on any of the following. **Every gate is a postmortem, not a best
practice** — each one names a defect that was live in this repo and was found by
hand, never by a test:

| Gate | The bug it prevents |
| --- | --- |
| **Train/serve schema parity** | Training and serving built different feature schemas; the aligner zero-filled the difference, so **every** prediction was the constant 62.1085 days — while `/ml/model-comparison` published R²=0.9291 for a configuration that was never served. |
| **Beats its stated baseline** | `beats_baselines` was computed and the model was persisted regardless of the answer. The regime model shipped at 0.733 accuracy against a 0.833 persistence baseline. Now a losing model is refused *and* any stale artifact is deleted. |
| **Serve-time coverage floor** | Feature admission asked "does this column exist?" instead of "is it ever populated?". Two columns filled on 7.0% of rows were admitted, and the model then declined to predict on 93% of real inputs — false on 6 of 6 sampled optimizer runs. Now ≥80% of real `(offer, component)` pairs must get an answer, measured against the shipped database. |
| **Not a near-constant predictor** | The other half of the schema bug, and the reason it survived: nothing ran the committed artifact over real inputs and measured the spread. Now it does. |
| **Endpoint declares its model's inputs** | The schema grew a `parameter_count` requirement, the `/ml/lead-time` signature did not follow, and FastAPI returned **422 on every call** before the model was consulted. |
| **Artifact carries provenance** | `metrics.joblib` had no `trained_at`, no training-data hash, no row count, no git SHA — so which data produced which model was unanswerable, which is why the R² mismatch went unnoticed. |
| **A gate can't silently stop testing** | A contract test kept passing while testing nothing, because the primary feature was renamed underneath it. Meta-tests now assert the variance tests still vary the model's *actual* primary feature — and `MODEL_CI_STRICT=1` turns a **skipped** gate into a failure, because a skipped gate is a green gate. |

Provenance (`trained_at`, `git_sha`, `sklearn_version`, `training_data_sha256`,
`n_training_rows`, `n_distinct_families`, …) is stamped at fit time and published
at `GET /api/v1/ml/model-info`. A **staleness check** warns — never fails — when
the weekly collector has grown the panel past what the served artifact was
trained on, so that growth is visible rather than silently ignored.

```bash
cd backend
MODEL_CI_STRICT=1 ./venv/bin/python -m pytest tests/ -m model_ci -q
# -> 52 passed, 1288 deselected in 525.67s (0:08:45)
```

**52 gates, all green** — measured 2026-09-08. This block read `1 failed, 50 passed` until
today, describing a local-only MLflow registry identity check that the 2026-09-03 retrain
had already cleared; the number outlived the condition it described, which is the failure
mode this whole section exists to catch.

One of those 52 gates emits a warning rather than a failure right now, and that is by
design: `test_training_data_staleness_is_reported_never_ignored` reports **STALE** —
the served artifact was fitted on the panel at `c68e2891…` and the file on disk is
`d94df904…` since the 2026-09-07 collector run. A scheduled data commit must not be able
to turn the build red by itself, so the tripwire warns, names the retrain command, and the
gap is served on `/api/v1/ml/model-info`. See [Tests](#tests).

Full write-up, including what these gates deliberately do **not** claim:
**[docs/MODEL_CI.md](docs/MODEL_CI.md)**.

---

## Lint & type-check

CI runs a dedicated `backend-lint` job (ruff + mypy) alongside tests, plus `tsc -b`
for the frontend (part of `npm run build`). Config lives in `backend/pyproject.toml`.

```bash
cd backend
source venv/bin/activate
pip install -r requirements-dev.txt   # ruff + mypy, dev-only

ruff check app          # lint (E/F/I/UP/B core rules)
ruff format app --check # formatting — not yet wired into CI (see note below)
mypy app                 # type-check (non-strict)
```

Both `ruff check app` (`All checks passed!`) and `mypy app` (`Success: no issues found in 77 source files`) are green today — re-run 2026-09-02. Deliberately deferred, tracked
in `pyproject.toml` comments so they can be picked up later without fighting
in-flight edits elsewhere in the repo:

- **`ruff format`**: **69 of 77** backend files would be reformatted (`ruff format app
  --check`) — the codebase predates a formatter convention. Not added as a CI gate yet:
  running it would touch nearly every file. Run locally and land as its own PR when
  convenient.
- **Typing-modernization rules** (`UP006`, `UP035`, `UP037`, `UP045` — `List`/`Optional[X]`
  → `list`/`X | None`) and **import sorting** (`I001`): large, repo-wide, low-risk-but-
  noisy sweeps. Ignored in `[tool.ruff.lint]` for now; safe to re-enable and `--fix`
  once other in-flight branches land.
- **Eight per-file rule ignores**, in `app/ml/regime_model.py` (`F401`, `B905`),
  `app/ml/lead_time_model.py` (`E402`), `app/ml/serving.py` (`UP017`),
  `app/optimization/recommendations.py` (`F401`), `app/optimization/solve.py` (`F841`),
  `app/optimization/sourcing.py` (`F841`), `app/graph/simulation.py` (`B905`) and
  `app/core/clients/oemsecrets_client.py` (`B007`) — small, real lint findings (unused
  imports and locals, `zip()` without `strict=`, an import below the top of the module,
  an unused loop variable, and `datetime.timezone.utc` where `datetime.UTC` now exists),
  left untouched because those files are owned by concurrent work; see
  `[tool.ruff.lint.per-file-ignores]`.
- **mypy** is fully strict-by-default-off (`ignore_missing_imports`, no `--strict`) and
  has a `[[tool.mypy.overrides]]` block that turns off checking for **22** modules — mostly
  `app/api/*` and `app/optimization/*` — where the codebase's untyped SQLAlchemy
  `Column(...)` declarative models (no `Mapped[...]` annotations) produce large numbers
  of `Column[T]` vs `T` false positives rather than real bugs. `mypy app` reports
  `Success: no issues found in 77 source files`, so **55 of those 77** are fully
  type-checked today. Migrating `app/models/*` to SQLAlchemy 2.0 `Mapped[]` typing would
  let those overrides be removed.

---

## The short version

**The 30-second pitch:**

> "Supply chain resilience is a graph problem, so I measured it spectrally — and the
> measurement talked me out of my own thesis. I expected one dominant distributor and a
> network one failure from collapse. What the data actually says: DigiKey is the largest
> single distributor at **11.2%** of offers, not 40%; killing DigiKey outright orphans
> **2** of 791 components (ids 11 and 290 — both DigiKey-only, and both already
> zero-stock there, so nothing sourceable is lost) and moves landed cost by **~0%**,
> because the per-line redundancy
> is genuinely there. The whole-graph Fiedler value is exactly 0.0 — but that's a floor
> by construction, since the graph fragments into 34 components. The number that means
> something is λ₂ = **0.279** on the giant component, which holds **95.9%** of the
> network: moderately connected, not fragile. The real single-point risk is the other
> 4% — **36 nodes** with no path into the main network at all. That's the list worth
> acting on, and it's short."

*(An earlier version of this pitch claimed "DigiKey handles 40% of offers" and "12
components have no alternative source." Neither is true of this data. They are left
documented here rather than quietly deleted, because catching it is the more
interesting story than never having written it.)*

*(Same story, second instance — **2026-09-03**: this pitch read "43 components", "λ₂ =
0.238", "95%" until a self-audit found the graph was being built from **80%** of the
supplier–part links. A deliberate 20% holdout carve (commit `da16157`, 2026-04-16) had
been added as a **leakage guard**, so a planned benchmark phase could not evaluate on
links it had already seen. Four days later, when that benchmark actually landed
(`885f436`, 2026-04-20), it declined to use the holdout — `run_benchmark.py` uses ALL
offers, because the benchmark **is** the holdout evaluation — and the carve then sat
inert for ~4.5 months, silently
excluding **1,574 of 8,176** offers from every published topology figure. Removing it
made the network measurably **less** fragmented, not more: 43 → 34 components, λ₂
0.238 → 0.279, giant component 95.0% → 95.9%. The honest one-line framing: **a leakage
guard for an evaluation step that was later designed not to need it.** The graph now
satisfies an exact invariant that makes this class of silent drop impossible to repeat:
`n_edges + n_duplicate_offer_rows == n_offer_rows` (7,363 + 813 = 8,176).)*

**Key talking points:**
- Fiedler value as a fragility metric — including *why the naive whole-graph reading of it is a trap* on a disconnected graph
- Monte Carlo shows distribution tails, not just means — that's where supply chain risk lives
- CP-SAT separates cost, time and carbon because they are not scalar multiples of each other — on the demo cart that yields 3 distinct plans from 4 strategies, and the UI says which two collapsed rather than implying four answers
- Live geopolitical data overlay: PortWatch/FRED/GPR feeds inform the optimizer — and the feed panel distinguishes *downloaded recently* from *observed recently*, which is how the GPR archive's frozen September-2021 reading stopped being published as a current one (ACLED is wired but needs a key — the UI labels it "Inactive" rather than faking a healthy feed)

---

## How this was built

Most of the code in this repo was written by AI agents — Claude, running under
`.github/workflows/claude-*.yml` — not by me typing it line by line. I'm not going to
pretend otherwise; the `claude-*.yml` workflows that ran them are public and readable.

What I actually did: framed the problem, decided what to build and in what order,
reviewed every pull request before merging it, and did the verification — including the
audit above that found the 44.7% headline was arithmetically real and substantively
meaningless, and killed it rather than leaving it in. That retraction is the clearest
evidence of what "direction" means here: an agent produced the number, and I'm the one
who checked it, didn't like what I found, and published the correction instead of the
headline.

There's also an autonomous loop running on a schedule — Scout files proposals, Builder
opens PRs against them, an independent Auditor reviews each one, and I'm the only one who
can merge to `main` (see `docs/archive/AUTONOMOUS-LOOP.md`). I keep a running list of that
loop's own failures as a maintainer note — it is not published here — because early runs failed
in ways a green checkmark didn't catch: a run that filed zero issues and still reported success,
subagents killed mid-task when their parent job ended. The specific failures that produced a
standing rule are written into the thing they constrain instead, in the test and workflow
comments, where they cannot rot away from it.

Nothing in this README is asserted on trust. The optimizer and ML numbers are checked by
CI gates (`model-ci`, [docs/MODEL_CI.md](docs/MODEL_CI.md)) that fail the build rather
than let a bad number ship quietly.

---

## Data Sources

| Source | What it provides |
|--------|-----------------|
| Nexar / Octopart (**static 2024 snapshot**, via HuggingFace `mdnh/electronic-components-supply-chain`, CC-BY-4.0) | Real component pricing, stock levels, distributor offers (791 components, 92 distributors, 8,176 offers). Real data, but a **frozen snapshot** — not a live API feed. See [docs/DATA_PROVENANCE.md](docs/DATA_PROVENANCE.md). |
| DigiKey API (**live**) | **3,406 real observed lead times across six snapshots** (75 on 2026-07-01, 742 on 2026-08-15, 363 on 2026-08-17, 742 on 2026-08-24, 742 on 2026-08-31, 742 on 2026-09-07), collected from all 791 catalogued components — 6.19% miss rate on the full 2026-08-15 sweep, logged per attempt. The served model is fitted on an earlier cut of this panel (2,615 usable rows of the then 2,664-row, five-snapshot cut, trained 2026-09-03) — see the lead-time bullets above. Collected by [`app/ml/lead_time_collector.py`](backend/app/ml/lead_time_collector.py) (resumable, quota-aware, honours `X-RateLimit-Remaining` and `Retry-After`) and scheduled weekly via [`.github/workflows/collect-lead-times.yml`](.github/workflows/collect-lead-times.yml). Also supplies live pricing/stock through `/api/v1/live-prices/*`. |
| FRED (Federal Reserve) | Freight index, PPI, macro stress regime |
| ACLED | Conflict event counts by country (distributor risk) |
| IMF PortWatch | Port call frequency (congestion delay) |
| GPR Index (**2021 archive snapshot**) | Geopolitical risk index (Chinese-origin component risk). The file at the published URL still downloads on every 15-minute tick, but its newest observation is dated **September 2021**, so the value it yields is a four-year-old reading rather than a current one. `/feeds/status` reads that observation date and reports the feed `stale`, naming the date, instead of `live`. |

---

## License

**MIT** for the code — see [LICENSE](LICENSE).

The bundled electronic-components dataset is licensed separately under **CC-BY-4.0**
(attribution required); the split is documented in
[docs/DATA_PROVENANCE.md](docs/DATA_PROVENANCE.md#licensing--code-vs-data).
