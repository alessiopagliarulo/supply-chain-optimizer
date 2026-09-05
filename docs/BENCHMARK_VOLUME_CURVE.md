# The 44.7% benchmark number is an artifact. Here is the proof — and the freight bug it exposed.

<!-- GENERATED:header_meta:BEGIN -->
**Generated:** 2026-09-05 · **Script:** `backend/seeds/run_volume_sweep.py` · **Data:** `docs/volume_sweep.json`
**Hardware:** arm64 / Darwin 25.5.0 · **Solver:** OR-Tools CP-SAT, `num_search_workers=1`, 5s limit
**Runtime:** 1.2s for the full sweep (10 BOMs × 13 multipliers × 3 arms × 2 offer pools)
<!-- GENERATED:header_meta:END -->

**Aggregate definition used everywhere in this document: POOLED** — `sum(greedy costs) / sum(MILP costs)`
across the BOMs feasible at that volume. Not a mean of per-BOM percentages. (Mixing the two is how the
original inconsistency happened.)

---

## Headline finding

The project's benchmark claims the CP-SAT MILP is **44.7% cheaper than a greedy baseline**, with
`iot_sensor_node` quoted at **71.75% saved**.

**At prototype volume, that number is fee arithmetic, not optimization.**

<!-- GENERATED:headline_fee_arithmetic:BEGIN -->
The greedy baseline picks `min(price_usd)` per BOM line, so it is *the component-cost minimum by
construction* — the MILP can never beat it on component cost, and in fact loses to it on component
cost in **all 10 of 10 BOMs** at 1×. At 1× volume every dollar the MILP "saves" comes from avoiding
fixed, **per-supplier** charges: `LTL_BASE_FEE_USD = $75` (domestic) and `AIR_FREIGHT_BASE_USD = $150`
(international), each scaled by `transport_penalty_scale = 1.5` → **$112.50 / $225 per supplier**.

At the benchmark's toy volumes (4 BOM lines, quantities 1–4, **4–9 units total**) those fees are
larger than the parts. On `iot_sensor_node` the components cost **$6.96**; consolidating 3 suppliers
into 1 avoids **$337.50** of fees. That is the 71.75%.
<!-- GENERATED:headline_fee_arithmetic:END -->

<!-- GENERATED:headline_decay:BEGIN -->
The fee saving is roughly **constant in volume**. Component cost grows **linearly**. So the savings
*percentage* must decay — and it does, from **47.2% at 1× to ~2.6–8.0% at 500×–10,000×**.
<!-- GENERATED:headline_decay:END -->

**But the story does not end at "the edge is noise", and an earlier draft of this document was wrong
to say it did.** Chasing the plateau in that curve turned up a real bug in the freight model. Fixing
it (below) changed the *source* of the MILP's advantage at scale: the fixed-fee wedge goes to **zero
or negative** (the MILP happily opens *more* suppliers than greedy at high volume), and what remains
is a genuine, volume-scaling edge from **routing units by landed cost rather than by unit price**.

---

## The freight bug (FIXED — 2026-07-13)

`_transport_cost_by_did()` in `sourcing.py` computed **one representative shipment weight for the whole
BOM** and then charged **every opened distributor that same full weight**, regardless of how much it
actually shipped:

```python
avg_demand    = sum(b.quantity for b in bom) / max(len(bom), 1)
avg_weight_kg = avg_demand * AVG_KG_PER_UNIT          # ONE weight for the whole BOM
for did in all_distributors:
    cost = LTL_BASE + cwt(avg_weight_kg) * miles * LTL_RATE   # charged per SUPPLIER
```

Variable freight was therefore **replicated per supplier instead of allocated across them**. Both arms
of the benchmark were corrupted by it, because both score through the same helper (MILP objective;
`greedy.landed_cost_breakdown`). Two distortions, in opposite directions:

* Splitting a BOM across N suppliers was billed **N × a full BOM's variable freight** — a permanent,
  volume-scaling penalty on splitting that inflated consolidation's apparent value.
* Symmetrically, a **consolidated** plan was charged only *one* BOM-average shipment even though it
  shipped the *whole* BOM — under-charging the MILP's own preferred answer.
* And because the per-supplier charge did not depend on quantity at all, **distance was nearly free at
  volume**: greedy could source 50,000 units from a distributor 4,000 km away and barely pay for it.

### The corrected model

Freight is now a proper fixed-charge model, decomposed in `sourcing.py::_freight_model_by_did`:

```
freight_d = fixed[d] · y[d]  +  per_unit[d] · Σ_c q[c,d]
```

| | fixed[d] (per opened supplier) | per_unit[d] (per unit actually shipped from d) |
|---|---|---|
| Domestic (LTL) | `LTL_BASE_FEE_USD` | `AVG_KG_PER_UNIT × LBS_PER_KG × CWT_PER_LB × miles × LTL_RATE` |
| International (air) | `AIR_FREIGHT_BASE_USD` | `AVG_KG_PER_UNIT × AIR_FREIGHT_RATE_USD_PER_KG` |

Both still multiplied by `transport_penalty_scale`. The fixed part is **kept** — that IS the
fixed-charge economics the MILP exists to solve. Only the variable part was wrong.

This stays **linear**, so CP-SAT models it exactly (`y[d]` and `q[c,d]` are already decision
variables — no approximation, no linearization). The objective is now built in integer **milli-cents**
(`OBJ_SCALE = PRICE_SCALE × 1000`) because the per-unit rate is small (~$0.029/unit at 100 km) and
would round to **zero** in whole cents for nearby distributors, silently deleting the term.

`greedy.landed_cost_breakdown` scores the identical model and now returns `transport_fixed` and
`transport_variable` separately. A test (`tests/test_greedy.py::test_milp_objective_equals_landed_cost_breakdown`)
asserts the solver's own objective value equals the benchmark's score of the solver's own plan — the
anti-rigging invariant, now pinned rather than asserted in prose.

---

## The corrected volume curve

Pooled, deduplicated offer pool, `balanced` strategy, **`milp_matched`** (greedy and MILP see the
*same* offer pool — `us_only=False` for both, which the published benchmark does not do). Points where
greedy's plan orders more units than exist are excluded — greedy cannot be allowed to "win" with an
unexecutable plan.

<!-- GENERATED:volume_curve:BEGIN -->
| Multiplier | BOMs feasible | greedy $ | MILP $ | **Pooled saving** | from fixed fees | from component cost | from variable freight |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1× | 10 | 6,996 | 3,693 | **47.22%** | +$3,863 | −$561 | +$2 |
| 2× | 9 | 6,922 | 4,397 | 36.48% | +$2,840 | −$322 | +$7 |
| 5× | 8 | 7,642 | 5,415 | 29.14% | +$2,501 | −$301 | +$27 |
| 10× | 7 | 8,399 | 6,462 | 23.06% | +$2,274 | −$406 | +$69 |
| 25× | 6 | 9,409 | 7,809 | 17.01% | +$1,589 | −$363 | +$374 |
| 50× | 5 | 12,814 | 11,879 | 7.30% | +$910 | −$465 | +$490 |
| 100× | 5 | 24,480 | 22,398 | 8.51% | +$570 | +$743 | +$769 |
| 250× | 5 | 58,236 | 54,445 | 6.51% | +$342 | +$406 | +$3,043 |
| 500× | 5 | 115,650 | 109,090 | 5.67% | **−$116** | −$111 | +$6,788 |
| 1,000× | 5 | 230,528 | 219,036 | 4.99% | **−$458** | −$1,585 | +$13,536 |
| 2,500× | 4 | 495,160 | 482,120 | 2.63% | **−$231** | −$5,177 | +$18,449 |
| 5,000× | 3 | 900,787 | 877,309 | 2.61% | **−$460** | −$1,313 | +$25,251 |
| 10,000× | 2 | 330,298 | 303,987 | 7.97% | **−$574** | +$867 | +$26,019 |
<!-- GENERATED:volume_curve:END -->

*(+ = greedy pays more, i.e. the MILP wins on that term. − = the MILP pays more.)*

**Read the last three columns.** The composition of the win inverts completely across the curve:

<!-- GENERATED:curve_composition:BEGIN -->
* **At 1×** the entire saving is fixed fees (+$3,863 out of a $3,304 total saving — **117% of it**).
  The MILP *overpays* for components and funds it from avoided supplier fees. Supplier count
  33 → 14.
* **At ≥500×** the fixed-fee term goes **negative**: the MILP now opens **MORE** suppliers than greedy
  (16 → 18 at 500×, 16 → 20 at 1,000×) and pays *more* in per-visit fees on purpose — because it is
  buying down variable freight and staying inside stock caps. Essentially 100% of the win is now
  **variable freight**: greedy pays $26k more in freight at 10,000× because it sources on unit price
  and is blind to distance × quantity.
<!-- GENERATED:curve_composition:END -->

That second regime is real optimization, and it is the part that **survives volume**. It was invisible
before the fix, because the old model charged freight per *visit* rather than per *unit* — so distance
barely mattered and the only lever the MILP had was closing suppliers.

### What changed vs the buggy model

Same aggregation, old vs new:

<!-- GENERATED:old_vs_new:BEGIN -->
| Multiplier | Old (buggy freight) | **Corrected** |
|---:|---:|---:|
| 1× | 47.66% | **47.22%** |
| 10× | 24.75% | **23.06%** |
| 50× | 8.32% | **7.30%** |
| 250× | 6.66% | **6.51%** |
| 1,000× | 2.78% | **4.99%** |
| 5,000× | 2.40% | **2.61%** |
| 10,000× | 3.49% | **7.97%** |
<!-- GENERATED:old_vs_new:END -->

The fix **shaves the prototype-volume number slightly** (the MILP's consolidated plan now pays for the
whole BOM's freight, as it should) and **raises the production-volume number** (the MILP can now
actually optimize the term that matters at scale). An earlier draft of this document predicted the
corrected high-volume edge would collapse to **~0.68%**. That prediction was produced by re-scoring the
*old* MILP's plans under allocated freight — it never let the solver re-optimize against the corrected
objective. When it does, the edge does not collapse. **The honest correction runs in the MILP's favour
at production volume, and we are saying so.**

### Caveat on the high-volume cohort

<!-- GENERATED:high_volume_caveat:BEGIN -->
The high-volume rows are a *different, smaller* BOM set than the low-volume ones (10 BOMs at 1×, 2
at 10,000×) — stock ceilings knock BOMs out as volume rises. The 10,000× row is `pcb_power_supply`
(8.37%) and `iot_sensor_node` (7.35%) only. It is **not a like-for-like cohort** and must not be
read as one. The trustworthy statement is the *range*: at production volume (500×–10,000×, i.e.
2,000–60,000 units) the MILP's pooled cost edge is **~2.6%–8.0%**, dominated by variable freight.
<!-- GENERATED:high_volume_caveat:END -->

---

## The decomposition at 1× — where the 44.7% actually comes from

<!-- GENERATED:decomposition_1x:BEGIN -->
| Component of the saving | Amount |
|---|---:|
| Avoided fixed per-supplier fees ($75 LTL / $150 air, ×1.5) | **+$3,863** |
| Variable freight (weight × distance) | +$2 |
| **Component cost** | **−$561** ← *the MILP pays MORE for parts* |
| **Total saving** | **$3,304 (47.22%)** |

Supplier count across the 10 BOMs drops from **33 (greedy) → 14 (MILP)**. 19 suppliers avoided ×
$112.50–$225 per supplier ≈ the entire "win". This is not the optimizer finding cheaper parts. It is
the optimizer noticing that the cost model charges $112.50 every time you talk to a new distributor.
<!-- GENERATED:decomposition_1x:END -->

### Per-BOM, at 1× volume

<!-- GENERATED:per_bom_1x:BEGIN -->
| BOM | greedy $ | MILP $ | save % | suppliers | fee saving $ | component saving $ |
|---|---:|---:|---:|:---:|---:|---:|
| automotive_ecu | 725.60 | 159.56 | 78.0% | 4→1 | 568 | −8 |
| iot_sensor_node | 467.98 | 132.59 | **71.7%** | 3→1 | 342 | −6 |
| pcb_power_supply | 356.85 | 137.13 | 61.6% | 3→1 | 229 | −8 |
| rf_transceiver_module | 586.69 | 252.46 | 57.0% | 3→2 | 340 | −6 |
| audio_dsp_board | 712.00 | 377.49 | 47.0% | 4→2 | 342 | −10 |
| smart_meter | 783.84 | 431.12 | 45.0% | 3→1 | 454 | −99 |
| medical_monitoring_device | 589.62 | 329.59 | 44.1% | 3→1 | 342 | −86 |
| industrial_motor_driver | 732.04 | 421.68 | 42.4% | 3→1 | 454 | −139 |
| robotics_servo_driver | 1088.92 | 656.22 | 39.7% | 4→1 | 568 | −135 |
| drone_flight_controller | 952.57 | 794.72 | 16.6% | 3→3 | 225 | −65 |
<!-- GENERATED:per_bom_1x:END -->

Note the last column: **the component saving is negative in every single BOM.** That is not a bug, it
is arithmetic — greedy minimizes component cost by definition.

---

## The retraction: `iot_sensor_node`, the 71.75% case

<!-- GENERATED:iot_retraction:BEGIN -->
| Multiplier | Units | greedy $ | MILP $ | **Savings %** | Fee share of saving | Suppliers |
|---:|---:|---:|---:|---:|---:|:---:|
| 1× | 5 | 467.98 | 132.59 | **71.7%** | 102% | 3→1 |
| 2× | 10 | 479.97 | 150.68 | 68.6% | 104% | 3→1 |
| 5× | 25 | 515.92 | 204.96 | 60.3% | 110% | 3→1 |
| 10× | 50 | 575.83 | 295.41 | 48.7% | 122% | 3→1 |
| 25× | 125 | 755.58 | 542.11 | 28.3% | 106% | 3→2 |
| 50× | 250 | 1,055.17 | 839.93 | 20.4% | 53% | 3→2 |
| 100× | 500 | 1,654.34 | 1,338.35 | 19.1% | 36% | 3→2 |
| 250× | 1,250 | 3,451.85 | 2,833.63 | 17.9% | 19% | 3→2 |
| 500× | 2,500 | 6,987.81 | 5,482.91 | 21.5% | 0% | 3→3 |
| 1,000× | 5,000 | 13,519.63 | 10,940.40 | 19.1% | −4% | 3→4 |
| 2,500× | 12,500 | 33,453.56 | 27,484.85 | 17.8% | 0% | 4→5 |
| 5,000× | 25,000 | 66,224.13 | 57,047.60 | 13.9% | −1% | 4→6 |
| 10,000× | 50,000 | 131,765.25 | 122,079.42 | **7.4%** | −1% | 4→6 |
<!-- GENERATED:iot_retraction:END -->

<!-- GENERATED:iot_retraction_prose:BEGIN -->
**71.7% → 7.4%.** We are still retracting "71.75% cheaper" as a headline: at 1× it *is* the fee, and
the fee doesn't care how many units you buy. But watch the **fee share** column collapse from 102% to
−1% while the saving stays in double digits: past ~250× the MILP is winning on something else
entirely. It opens *more* suppliers than greedy (4→6) and still comes out 7–22% cheaper, because it
routes each line's volume to whichever distributor minimizes **price + freight**, and greedy only looks
at price.

The defensible statement is: *on a 5-unit prototype BOM the MILP avoids $337.50 of supplier onboarding
fees — that is fee arithmetic. At 50,000 units it is 7.4% cheaper on landed cost, and that part is real
freight optimization.*
<!-- GENERATED:iot_retraction_prose:END -->

---

## Bugs found while doing this — both now FIXED

### 1. Duplicate-offer variable collision (`sourcing.py`) — **FIXED**

CP-SAT decision variables are keyed on `(component_id, distributor_id)`, but the offer table contains
**509 duplicated `(component, distributor)` pairs** database-wide (41 inside the 10 benchmark BOMs) —
price-break tiers from the same distributor. When a distributor had *k* offers for one component, the
same `q[key]` variable was **summed k times** in the demand constraint (`k·q == demand` → spurious
INFEASIBLE whenever `demand % k != 0`) and **priced k times** in the objective (`PCM4202DBT` at
distributor 28 was costed at **$11.35 + $11.35 + $7.28 = $29.98/unit** instead of $7.28).

Greedy was unaffected (it scans a flat list and takes `min(price)`), so a corrupted MILP could *lose*
to greedy — a bug artifact, not a finding. `solve_sourcing` now collapses each
`(component, distributor)` pair to its cheapest tier before building the model. (Proper
quantity-dependent price breaks remain a separate, larger change.)

### 2. Freight replicated per supplier rather than allocated (`_transport_cost_by_did`) — **FIXED**

Described in full at the top of this document. This one distorted *both* arms of the benchmark, since
both score through the same helper. Now `_freight_model_by_did`: fixed per visit + per-unit on units
actually shipped.

---

## Feasibility ceilings — what the data can actually support

Stock is a hard cap in the MILP (`q ≤ stock`). We computed each BOM's maximum feasible multiplier from
total available stock per line and swept only within it. **The snapshot's stock levels cannot support
production volumes for most BOMs:**

<!-- GENERATED:feasibility_ceilings:BEGIN -->
| BOM | Base units | Max multiplier | Max total units | Duplicated offer pairs |
|---|---:|---:|---:|---:|
| pcb_power_supply | 6 | 22,051 | 132,306 | 0 |
| iot_sensor_node | 5 | 18,758 | 93,790 | 2 |
| rf_transceiver_module | 4 | 7,912 | 31,648 | 3 |
| medical_monitoring_device | 8 | 6,175 | 49,400 | 6 |
| audio_dsp_board | 7 | 2,430 | 17,010 | 3 |
| automotive_ecu | 7 | 92 | 644 | 7 |
| smart_meter | 4 | 34 | 136 | 6 |
| industrial_motor_driver | 7 | 11 | 77 | 3 |
| drone_flight_controller | 7 | 7 | 49 | 7 |
| robotics_servo_driver | 9 | **2** | **18** | 4 |
<!-- GENERATED:feasibility_ceilings:END -->

<!-- GENERATED:ceiling_summary:BEGIN -->
5 of the 10 BOMs cap out below 100× volume. `robotics_servo_driver` cannot be built more than
**twice** from this data. BOMs surviving at each multiplier: **10 at 1×, 9 at 2×, 8 at 5×, 7 at 10×,
6 at 25×, 5 at 50×–1,000×, 4 at 2,500×, 3 at 5,000×, 2 at 10,000×.**
<!-- GENERATED:ceiling_summary:END -->

`audio_dsp_board` has **zero domestic stock** (`domestic-only ceiling = 0`), which is why the existing
benchmark's domestic-only MILP arm cannot solve it and skips it (9 of 10 BOMs run). Expected, not a bug.

### Greedy's plans become physically impossible at scale

`solve_sourcing_greedy` falls back to "cheapest offer with *any* stock" when no single offer covers the
line. At high volume this produces plans that **order more units than exist**: greedy assigned 2,500
units of `AD7934BRUZ` from an offer holding **1 unit**. Those points are flagged
(`arms.greedy.stock_violations`) and **excluded from every number in this document** — greedy cannot be
allowed to "win" with a plan that cannot be executed.

---

## What the MILP is genuinely good for

1. **A real, volume-scaling cost edge — but ~3–8%, not 45%.** At production volume it beats greedy by
   routing volume to whichever supplier minimizes *price + freight*, which greedy structurally cannot
   see. That is worth having. It is not worth a 45% headline.
2. **It produces executable plans.** Greedy does not — its fallback happily orders 2,500 units from a
   distributor holding 1. The MILP enforces `q ≤ stock` and MOQ floors as hard constraints. At 100×+
   volume this is the difference between a purchase order and a fiction.
3. **It can split a BOM line across distributors.** Greedy structurally cannot (one offer per line).
   This is *why* it stays feasible where greedy doesn't, and part of why it wins at scale.
4. **It optimizes a multi-objective tradeoff** (cost/time/carbon weights, MOQ, domestic-sourcing
   constraints, risk surcharges). Greedy optimizes cost and nothing else. The resilience results
   (graph-aware vs blind MILP, `cvar_95` under disruption) are a separate story on a separate axis and
   are **not** affected by anything in this document.

What we should stop claiming: **that the optimizer delivers a ~45% landed-cost saving.** It delivers
that only on 5-unit prototype BOMs where it is an artifact of per-supplier onboarding fees. At any
volume a real buyer would order, the number is **single digits** — and honestly earned.

---

## Reproduce

```bash
cd backend
source venv/bin/activate
python -m seeds.run_volume_sweep      # ~1 second
```

Writes `docs/volume_sweep.json` (full per-BOM/per-multiplier/per-arm results with cost decomposition,
supplier counts, solver status, feasibility ceilings, and both offer pools), stamped with a
`provenance` block recording the git SHA, the dirty-tree flag, and the **sha256 of the sqlite snapshot
the sweep read** — so two runs can be told apart by their input bytes, not just their date.

It also rewrites this document's numeric blocks **in place**. Every table and figure sitting between a
`<!-- GENERATED:<id>:BEGIN -->` / `<!-- GENERATED:<id>:END -->` marker pair is emitted from
`docs/volume_sweep.json`; everything outside those markers — all of the argument, every caveat — is
hand-written and is never touched by the generator. Editing a generated block by hand is pointless (the
next run overwrites it); deleting a marker pair makes the generator log a warning and that section then
silently stops tracking the data, which is exactly the drift this markup exists to prevent.

**Arms** — all three scored through the *same* `landed_cost_breakdown()`, which calls the MILP's own
`_freight_model_by_did()`. No cost model is reimplemented anywhere:

| Arm | Solver | Offer pool |
|---|---|---|
| `greedy` | `solve_sourcing_greedy`, `us_only=False` | as the published benchmark calls it |
| `milp_matched` | `solve_sourcing`, `us_only=False` | **PRIMARY** — same pool as greedy, a fair fight |
| `milp_bench` | `solve_sourcing`, `us_only=True` | reproduces the published benchmark's MILP arm |

The published benchmark compares a **domestic-only MILP** against an **international-inclusive greedy**
(`balanced.us_only_sourcing = True` overrides the caller's `us_only=False` inside `optimize_bom`, while
`solve_sourcing_greedy` is called with `us_only=False` directly). The arms do not see the same offer
pool. `milp_matched` fixes that; `milp_bench` reproduces the original.

**Reconciled with `BENCHMARK_RESULTS.md` (2026-08-16).** That document used to publish **−44.66%** from
`run_id=4`, a run that predated both fixes above — and it could not be regenerated, because its generator
wrote to a CWD-relative, *hyphenated* `docs/BENCHMARK-RESULTS.md` while the committed file is underscored.
Running it the documented way produced a stray `backend/docs/` file and left the real doc untouched, so
its own "re-run to regenerate" instruction was a no-op. Both are now fixed: the generator is anchored on
the repo root, and `run_id=5` reproduces **−47.25%** — which is exactly this sweep's `milp_bench` arm at
1× on the same 9 BOMs. The two documents now agree, and
`tests/test_benchmark_docs_match_artifacts.py` fails the build if either drifts from its JSON artifact
again.

The direction of that correction is worth stating plainly: the honest number is **larger** than the
retracted one, not smaller. It changes nothing about the retraction — a bigger fee-arithmetic artifact is
still a fee-arithmetic artifact, and the decay curve above is still the number that matters.

> **Superseded framing (2026-09-03).** The **−47.25%** referenced above is measured against a
> greedy baseline shopping the **full international** offer pool while the MILP is restricted to
> domestic distributors — the pools were never matched. `seeds/run_benchmark.py` now also solves
> both heuristics on the optimizer's own domestic pool, and the like-for-like pooled figure is
> **−18.79%**, not −47.25%. See §A of [`BENCHMARK_RESULTS.md`](BENCHMARK_RESULTS.md) and
> `headline.primary_save_pct` in [`benchmark_results.json`](benchmark_results.json). The −47.25%
> remains correct for what it measures and is retained only as the contrast against a naive,
> globally-shopping baseline.

<!-- GENERATED:solver_hygiene:BEGIN -->
**Solver hygiene:** of 326 MILP solve attempts, 296 were feasible and **all 296 returned `OPTIMAL`** —
none hit the 5s time limit. The 30 infeasible attempts are the genuine stock/MOQ ceilings documented
above. No result in this document is a timeout artifact.
<!-- GENERATED:solver_hygiene:END -->

### Solver hygiene above is a run log, not a property of the problem

The counts in that block — 326 attempts, 296 feasible, all `OPTIMAL`, none hitting the **5-second**
limit — describe what one machine's CP-SAT achieved inside a 5-second budget with
`num_search_workers=1`. They are **machine- and load-dependent**: the same sweep on a busier or
slower machine can hit the limit and report a different count, exactly as the CVaR frontier's
breadth arm does (see `CVAR_EFFICIENT_FRONTIER.md` §0, where two regenerations disagreed).

**What this does NOT qualify:** every cost, saving, decay percentage and supplier count in this
document is a property of the problem and reproduces exactly — the artifact-vs-code pin
`test_volume_sweep_reproduces_from_the_live_optimizer` re-solves all 80 points and would fail if
any of them moved. The 30 infeasible attempts are likewise genuine stock/MOQ ceilings, not
timeouts. Only the solve-quality counters above are the run log.

---

<!-- GENERATED:provenance:BEGIN -->
## Provenance

- **Generated:** 2026-09-05T19:39:46Z (UTC)
- **Generator:** `seeds.run_volume_sweep`
- **Commit:** `4c19b5510d74d22fa5e2617acc66ca2a2b880960` (clean tree)
- **Input `supply_chain_db`:** `backend/supply_chain.db` · sha256 `b60dc9ba058a6956…`
- **Python:** 3.13.5 · macOS-26.5-arm64-arm-64bit-Mach-O
<!-- GENERATED:provenance:END -->
