# Benchmark Results — run_id=10

> **This file is generated** by `python -m seeds.run_benchmark`. Everything outside the `CURATED:BEGIN` / `CURATED:END` HTML-comment markers is overwritten on every run; everything inside them is preserved verbatim. Put prose, retractions and caveats there.

**Generated:** 2026-09-05 19:45 UTC
**Coverage:** **9 of 10 BOMs** — 1 excluded, see §0 for the reason.
**Rows:** 90 (9 BOMs × 10 rows: 6 arms×nominal + 2 milp×2 disruptions)
**Seed:** 42 · **Strategy:** balanced · **Holdout:** benchmark IS the holdout

Every arm's selection is scored through the SAME `landed_cost_breakdown` cost function, so MILP-vs-greedy is a fair comparison. Greedy arms are pure sourcing baselines with no route model — their ETA/CO2 are omitted; cost, supplier count and tail-risk are their story.

<!-- CURATED:BEGIN -->
> ### ⚠️ RETRACTION — do not quote the `save% vs greedy` column as a headline
>
> **Aggregate quoted in this retraction:** `-47.25%` — this must equal
> `headline.total_save_pct_vs_greedy` in
> [`benchmark_results.json`](benchmark_results.json), and
> `tests/test_benchmark_docs_match_artifacts.py` fails the build if it drifts.
>
> **Like-for-like aggregate quoted in this retraction:** `-18.79%` — this must equal
> `headline.primary_save_pct` in the same file, and the same test file fails the
> build if *it* drifts. Both numbers are pinned, because pinning only the
> retracted one is how it kept reading as the result.
>
> **The pools were not matched either (fixed 2026-09-03, run_id=9).** On top of the
> fee artifact described below, the `-47.25%` compared arms that did not shop the
> same catalogue: the greedy baseline was solved on the **full international**
> offer pool and opened **14 of its 29 suppliers abroad**, while the MILP's plan
> comes from the `balanced` strategy, which is **domestic-only** and opened **0**.
> A foreign supplier costs an air-freight base fee against an LTL fee domestically,
> so part of that gap was a shipping policy, not an optimization result.
>
> The benchmark now solves **four** baselines — `{naive, ADD} × {international,
> domestic}` — and the pooled saving over the same 9 BOMs against the same MILP
> plans runs `-47.25% → -37.10% → -34.99% → **-18.79%**`. The last of those is the
> only like-for-like comparison and is the number this project stands behind. The
> `-47.25%` is retained **only** as the contrast against a naive, globally-shopping
> baseline and must always be labelled as such.
>
> The optimizer's own restriction is **not** part of this: re-solved on greedy's
> full global pool the MILP returns an identical plan at an identical landed cost
> to the cent (`milp_matched` in [`volume_sweep.json`](volume_sweep.json)), so
> 0.00 points of the headline came from restricting the optimizer. The asymmetry
> was entirely on the baseline side, which is why the fix was to add domestic-pool
> **baselines** rather than to widen the optimizer's pool.
>
> That aggregate is **arithmetically correct and substantively meaningless.** Greedy is
> the component-cost minimum *by construction*, so the MILP can never beat it on
> component cost — it can only win on flat **per-supplier** freight fees
> (`LTL_BASE_FEE_USD = $75` domestic / `AIR_FREIGHT_BASE_USD = $150` international, each
> ×`transport_penalty_scale`). These BOMs are 4 parts / 4–9 units, so those fees dwarf
> the parts: the MILP pays **more** for components and funds it entirely from avoided
> supplier fees. Table A's own last column is the tell — supplier count collapses
> **29 → 12** across the 9 BOMs (30 → 12 for the matched `greedy_dom` arm), and the
> ~17 avoided suppliers × ~$112.50 *is* the "saving".
>
> At benchmark scale that saving is a constant (`fee × suppliers avoided`), not a rate,
> so it decays as volume grows — to **single digits** at any volume a real buyer would
> order. See **[BENCHMARK_VOLUME_CURVE.md](BENCHMARK_VOLUME_CURVE.md)** for the full
> decomposition and the measured decay curve; that document, not this one, is the
> authority on what the optimizer is worth. What survives at production volume is *not*
> the fee trick: it is a genuine low-single-digit edge from routing volume by
> **price + freight** rather than by unit price.
>
> **Second retraction — resilience is not free.** §B used to assert that the graph-aware
> arm "spends ~0 extra nominally". Run 5 shows that is false: its nominal cost premium
> runs from **+0.00% to +82.16%** (median **+12.25%**), because `require_dual_source`
> forbids exactly the single-supplier consolidation that makes the blind arm cheap. Nor
> does it always help — of 18 (BOM × scenario) cells, cascade-risk **worsens in 2**
> (`industrial_motor_driver` and `pcb_power_supply`, both under broad stress). The real,
> defensible pattern is narrower than "graph-aware is safer": it is that graph-awareness
> pays off under a **targeted** outage of a high-betweenness distributor (7 of 9 BOMs
> improve) and is roughly a coin-flip under undirected macro stress.
>
> **Provenance caveats for this specific run.** (a) `audio_dsp_board` is **excluded** —
> the blind MILP arm is INFEASIBLE on it (it has zero domestic stock, and the balanced
> strategy's `us_only_sourcing` makes that arm domestic-only). The benchmark is
> **9 of 10 BOMs**, and §0 now says so in the artifact instead of only in a log line.
> (b) This run read a *snapshot copy* of `backend/supply_chain.db` rather than the live
> file, because other work was writing to the database concurrently; the exact bytes are
> pinned by sha-256 in the Provenance section at the foot of this file.
<!-- CURATED:END -->

## 0) BOM inclusion — 9 of 10 catalog BOMs are in the tables below

An excluded BOM is one where at least one of the six arms failed to solve. The 10-row-per-BOM invariant is all-or-nothing, so a BOM that cannot be scored on every arm is dropped from **all** tables rather than compared unevenly. Exclusions are published here; they are not just a log line.

| BOM | in benchmark? | rows | offers | reason |
|-----|:-------------:|-----:|-------:|--------|
| iot_sensor_node | ✅ included | 10 | 44 | all 6 arms solved |
| drone_flight_controller | ✅ included | 10 | 75 | all 6 arms solved |
| pcb_power_supply | ✅ included | 10 | 20 | all 6 arms solved |
| industrial_motor_driver | ✅ included | 10 | 65 | all 6 arms solved |
| rf_transceiver_module | ✅ included | 10 | 53 | all 6 arms solved |
| automotive_ecu | ✅ included | 10 | 108 | all 6 arms solved |
| medical_monitoring_device | ✅ included | 10 | 65 | all 6 arms solved |
| smart_meter | ✅ included | 10 | 81 | all 6 arms solved |
| robotics_servo_driver | ✅ included | 10 | 65 | all 6 arms solved |
| audio_dsp_board | ❌ **EXCLUDED** | 0 | 46 | MILP arm `milp_blind` (graph_aware=False) raised ValueError: Insufficient stock to fill the BOM from domestic distributors only: GD25Q127CYIGR needs 1 but only 0 in stock |

## A) Value of optimization — MILP vs four greedy baselines (nominal)

**Read the `_dom` columns first.** A baseline is defined by its heuristic *and* by the catalogue it is allowed to shop. `greedy` and `greedy_add` shop the **full international** offer pool (`us_only=False`); the MILP's plan comes from the `balanced` strategy, which is **domestic-only** (`us_only_sourcing=True`). Comparing those two mixes an optimization result with a shipping policy the arms did not share. `greedy_dom` and `greedy_add_dom` are the same two heuristics re-solved on the MILP's own domestic pool, so **`save% vs greedy_add_dom` is the like-for-like number** and the columns to its left are the contrast against a naive, globally-shopping buyer.

The MILP's own restriction is **not** the asymmetry: re-solved on greedy's full global pool it returns the identical plan and the identical landed cost to the cent (the `milp_matched` arm in `volume_sweep.json`), so no part of the gap comes from restricting the optimizer.

| BOM | greedy $ | greedy_add $ | greedy_dom $ | greedy_add_dom $ | milp $ | save% vs greedy | save% vs greedy_add | save% vs greedy_dom | **save% vs greedy_add_dom** | suppliers greedy→greedy_dom→milp |
|-----|---------:|-------------:|-------------:|-----------------:|-------:|----------------:|--------------------:|--------------------:|----------------------------:|:--------------------------------:|
| automotive_ecu | 725.60 | 498.79 | 501.72 | 392.87 | 159.56 | -78.01% | -68.01% | -68.20% | **-59.39%** | 4→4→1 |
| drone_flight_controller | 952.57 | 952.57 | 795.01 | 795.01 | 794.72 | -16.57% | -16.57% | -0.04% | **-0.04%** | 3→3→3 |
| industrial_motor_driver | 732.04 | 732.04 | 583.71 | 471.52 | 421.68 | -42.40% | -42.40% | -27.76% | **-10.57%** | 3→3→1 |
| iot_sensor_node | 467.98 | 351.47 | 471.18 | 354.66 | 132.59 | -71.67% | -62.28% | -71.86% | **-62.61%** | 3→4→1 |
| medical_monitoring_device | 589.62 | 369.96 | 477.30 | 363.77 | 329.59 | -44.10% | -10.91% | -30.95% | **-9.40%** | 3→3→1 |
| pcb_power_supply | 356.85 | 241.34 | 356.85 | 241.34 | 137.13 | -61.57% | -43.18% | -61.57% | **-43.18%** | 3→3→1 |
| rf_transceiver_module | 586.69 | 586.69 | 365.55 | 252.46 | 252.46 | -56.97% | -56.97% | -30.94% | **+0.00%** | 3→3→2 |
| robotics_servo_driver | 1088.92 | 753.94 | 783.09 | 674.81 | 656.22 | -39.74% | -12.96% | -16.20% | **-2.75%** | 4→3→1 |
| smart_meter | 783.84 | 783.84 | 764.81 | 535.90 | 431.12 | -45.00% | -45.00% | -43.63% | **-19.55%** | 3→4→1 |
| **TOTAL** | 6284.11 | 5270.64 | 5099.22 | 4082.34 | 3315.07 | -47.25% | -37.10% | -34.99% | **-18.79%** | — |

### The like-for-like result: **18.79%**

Pooled over the 9 BOMs, the blind MILP costs **$3,315.07** against **$4,082.34** for the ADD heuristic on the same domestic catalogue — **18.79% cheaper**. That is the number this benchmark stands behind.

Against a **naive, globally-shopping** buyer the same MILP plans look **47.25%** cheaper. The gap between those two figures is not optimization; it is two handicaps stacked on the baseline:

- **10.15 points** come from the baseline being the *naive per-line* heuristic rather than the ADD heuristic — i.e. from the baseline being bad at consolidation, not from the MILP being good at it.
- **18.31 points** come from the baseline shopping a **wider catalogue** than the optimizer was allowed, opening international suppliers at the air-freight fixed fee. That is a shipping policy, not an optimization result.
- **18.79 points** remain once both handicaps are removed. Only these are the optimizer's.

*Negative save% = MILP is cheaper (the win). MILP jointly optimizes component price, per-distributor transport and consolidation, so it consolidates orders the myopic greedy baseline cannot.*

## B) Value of resilience — graph-aware MILP vs blind MILP

`plan_cascade_risk` = 1 − P50 fulfillment of the selected plan; `cvar_95` = mean emergency-cost multiplier of the worst-5% scenarios. A **positive** reduction means the graph-aware arm is the safer plan.

**What this run actually shows.** Resilience is **not** free here: the graph-aware arm's nominal cost premium is median **+12.2%**, range **+0.0% to +82.2%** across the 9 BOMs — it buys tail-risk protection with real money, because `require_dual_source` forbids the single-supplier consolidation that makes the blind arm cheap. And it does not win everywhere: of 18 (BOM × scenario) cells, cascade-risk improves in **5**, is unchanged in **9**, and gets **worse in 4**; CVaR-95 improves in **6** and worsens in **0** — but **8 of 18** cells have BOTH arms pinned at the CVaR-95 ceiling (1.15), where the metric is arithmetically incapable of separating them. Read the per-row signs below rather than the headline, and on a saturated row read `p_total_shortfall`, not `cvar_95`.

**READ THE SATURATION COLUMN BEFORE THE CVaR COLUMN.** `cvar_95` is a mean over the worst-5% tail of `1 + unfulfillable_share * 0.15`, which is bounded, so it tops out at **1.15** and stops moving. **8 of 18** cells below have BOTH arms on that ceiling: their `cvar_95` reduction is 0.0000 because the metric cannot go any higher, NOT because the two plans are equally exposed. On those rows read `p_total_shortfall` — P(every BOM line unfulfillable), a mean over ALL scenarios rather than the tail — which keeps resolving where `cvar_95` stops.

| BOM | scenario | nominal cost premium | cascade_risk (blind→graph, ↓) | cvar_95 (blind→graph, ↓) | cvar_95 saturated? | p_total_shortfall (blind→graph, ↓) |
|-----|----------|---------------------:|:-----------------------------:|:------------------------:|:------------------:|:----------------------------------:|
| automotive_ecu | stress | +69.65% | 0.0000→0.0000 (+0.0000) | 1.1500→1.1245 (+0.0255) | no | 0.3470→0.0330 (+0.3140) |
| automotive_ecu | targeted | +69.65% | 0.0000→0.0000 (+0.0000) | 1.1500→1.0810 (+0.0690) | no | 0.1140→0.0040 (+0.1100) |
| drone_flight_controller | stress | +0.00% | 0.2500→0.2500 (+0.0000) | 1.1208→1.1208 (+0.0000) | no | 0.0110→0.0110 (+0.0000) |
| drone_flight_controller | targeted | +0.00% | 0.0000→0.0000 (+0.0000) | 1.0810→1.0810 (+0.0000) | no | 0.0000→0.0000 (+0.0000) |
| industrial_motor_driver | stress | +11.82% | 0.0000→0.5000 (-0.5000) | 1.1500→1.1500 (+0.0000) | **AT CEILING** | 0.3680→0.1120 (+0.2560) |
| industrial_motor_driver | targeted | +11.82% | 1.0000→0.0000 (+1.0000) | 1.1500→1.0870 (+0.0630) | no | 1.0000→0.0080 (+0.9920) |
| iot_sensor_node | stress | +82.16% | 0.0000→0.0000 (+0.0000) | 1.1500→1.1500 (+0.0000) | **AT CEILING** | 0.3680→0.1390 (+0.2290) |
| iot_sensor_node | targeted | +82.16% | 1.0000→0.5000 (+0.5000) | 1.1500→1.1500 (+0.0000) | **AT CEILING** | 1.0000→0.1150 (+0.8850) |
| medical_monitoring_device | stress | +12.25% | 0.0000→0.0000 (+0.0000) | 1.1500→1.1500 (+0.0000) | **AT CEILING** | 0.3680→0.1100 (+0.2580) |
| medical_monitoring_device | targeted | +12.25% | 1.0000→0.2500 (+0.7500) | 1.1500→1.1500 (+0.0000) | **AT CEILING** | 1.0000→0.0910 (+0.9090) |
| pcb_power_supply | stress | +75.99% | 0.0000→0.2500 (-0.2500) | 1.1500→1.1500 (+0.0000) | **AT CEILING** | 0.3680→0.1330 (+0.2350) |
| pcb_power_supply | targeted | +75.99% | 1.0000→0.0000 (+1.0000) | 1.1500→1.0600 (+0.0900) | no | 1.0000→0.0100 (+0.9900) |
| rf_transceiver_module | stress | +0.00% | 0.0000→0.0000 (+0.0000) | 1.1200→1.1200 (+0.0000) | no | 0.0300→0.0300 (+0.0000) |
| rf_transceiver_module | targeted | +0.00% | 0.0000→0.0000 (+0.0000) | 1.0765→1.0765 (+0.0000) | no | 0.0010→0.0010 (+0.0000) |
| robotics_servo_driver | stress | +2.99% | 0.0000→0.2500 (-0.2500) | 1.1500→1.1500 (+0.0000) | **AT CEILING** | 0.3680→0.1120 (+0.2560) |
| robotics_servo_driver | targeted | +2.99% | 1.0000→0.0000 (+1.0000) | 1.1500→1.0870 (+0.0630) | no | 1.0000→0.0080 (+0.9920) |
| smart_meter | stress | +24.55% | 0.0000→0.2500 (-0.2500) | 1.1500→1.1500 (+0.0000) | **AT CEILING** | 0.3640→0.1120 (+0.2520) |
| smart_meter | targeted | +24.55% | 0.0000→0.0000 (+0.0000) | 1.1500→1.0870 (+0.0630) | no | 0.1180→0.0080 (+0.1100) |

*Annualization assumption: each BOM re-ordered ANNUAL_REORDERS=12×/yr (a stated modelling assumption, not measured cadence).*

## Reproduce

```bash
cd backend && source venv/bin/activate
python -m seeds.run_benchmark      # ~2-3 minutes
```

Writes this file and `docs/benchmark_results.json` (the machine-readable twin these tables are generated from). Both paths are anchored on the repo root, so the working directory does not matter.

## Provenance

- **Generated:** 2026-09-05T19:45:56Z (UTC)
- **Generator:** `seeds.run_benchmark`
- **Commit:** `6e88a69f4339f92879d5f5435f9c09b5826ada52` (clean tree)
- **Input `database`:** `backend/supply_chain.db` · sha256 `b60dc9ba058a6956…`
- **Python:** 3.13.5 · macOS-26.5-arm64-arm-64bit-Mach-O

