# Sub-Project A — Real Industrial Optimization for Electronic Components Routing

**Status:** As-built. Describes the optimization pipeline that is implemented and running.
**Originally written:** 2026-04-10 (as a forward-looking design)
**Last reconciled against the code:** 2026-09-03
**Author:** Claude (with user direction)

---

## 0. How to read this document

This started life as a *plan* written on 2026-04-10, before the code existed. It has
since been reconciled against what was actually built. Read it with these conventions:

- Sections **1–5** describe the **shipped** pipeline. Where the original plan named a
  technique the code does not implement, the section now says so explicitly rather than
  keeping the flattering name.
- **NOT BUILT** marks something that was planned, is still a reasonable future direction,
  and is *not* in the code today. Do not cite anything under that marker as implemented.
- Sections **8, 12, 14** are the original working plan and are kept for provenance. They
  are history, not a description of current behaviour.

Three corrections are large enough to flag up front, because earlier revisions of this doc
overstated them and an OR-literate reader will check:

1. **Cross-dock hub selection is exhaustive enumeration over 10 fixed candidates — not
   Lagrangian relaxation, and there is no capacity constraint anywhere.** See §3.4.
2. **The Stage 1 sourcing MILP minimizes landed COST only.** It is not a tri-objective
   program. Time and carbon enter through three named proxy levers and through a
   weighted-sum scalarization applied *after* sourcing. See §3.2.
3. **International air freight is ≈4.51× the CO₂ per kg-km of domestic truck, not 30–40×.**
   The larger figure compares air to *ocean*. Derived from this repo's own two cited
   emission factors, with the arithmetic shown, in §3.2.

## 1. Executive Summary

Replace the current "multi-objective VRP" — which is structurally a single TSP with four cosmetic labels — with a genuine **Component Sourcing + Pickup Routing + Cross-Dock Consolidation** system grounded in published industrial logistics math. The new system must:

- Solve a real constrained integer program for supplier selection (OR-Tools CP-SAT) composed with a single-vehicle TSP for the pickup route.
- Use freight cost constants from published industry sources (ATRI, EPA SmartWay, BLS).
- Produce four materially different routes under the four strategy weight profiles.
- Include cross-dock consolidation through a set of ten real US freight hubs, with hub selection by exhaustive enumeration over that fixed candidate set (exact, because the set is tiny — see §3.4).
- Be verifiable end-to-end through pytest unit tests and a Playwright E2E test.
- Be defensible in a whiteboard interview at McKinsey/BCG/Amazon Ops — every number traceable to a citable source, every constraint mapped to a business reason.

This is **Option C from brainstorming**: ship the Sourcing-LP + TSP formulation today (Stage A), with the code structured so a future Two-Echelon MILP (Stage B) is a drop-in replacement for a single module.

## 2. Background: What's Broken Today

A verification pass on 2026-04-10 confirmed the current `POST /api/v1/optimize/vrp` endpoint has three fundamental flaws:

1. **The four strategies return identical routes.** All four "objectives" in the cost matrix (`backend/app/api/optimize.py:84-103`) reduce to scaled haversine distance (`transport_cost = d × FUEL_COST_PER_KM`, `time_cost = d / TRUCK_SPEED_KMH`, `carbon_cost = d × kg × factor`). Weighting these three proportional quantities produces the same optimal tour for every weight combination. Verified: all four alternatives returned `cost=$10,459.24, dist=30,752 km, route=63→89→37→1`.

2. **No component cost in the objective.** The current code charges only transport cost; it never considers that the same MPN has different prices at different distributors (the whole point of the Nexar dataset). The "cheapest" strategy therefore cannot pick the cheapest supplier — it picks the geographically nearest one regardless of sticker price.

3. **Demo data is internationally contaminated.** The demo user's cart contains items sourced from Shenzhen and Leeds, producing a 30,752 km "optimal" route for $40 of parts. Realistic for a global sourcing problem, absurd as a portfolio demo aimed at US-based recruiters.

Additionally:
- Stale pre-pivot modules exist: `backend/app/api/hubs.py`, `backend/app/api/materials.py`, and the `materials`/`suppliers`/`production_hubs` tables.
- `/auth/demo` endpoint works but the demo user carries crufty orders/cart from earlier sessions.
- `backend/app/api/components.py:204` uses deprecated `regex=` parameter.

## 3. The Problem, Formally

### 3.1 Inputs

- **BOM** `B = {(c_i, q_i) : i = 1..m}` — ordered components and required quantities
- **Offer catalog** `O = {(c, d, p, s, moq)}` — each tuple represents distributor `d` selling component `c` at unit price `p` USD with `s` units in stock and minimum order `moq`
- **Distributor set** `D` with locations `(lat_d, lng_d)` and `is_domestic` flag
- **Candidate hub set** `H` — ten real US freight hubs (section 5.4)
- **Depot** `F = (lat_F, lng_F)` — the demo user's factory location (Greenville SC: 34.8526, -82.3940)

### 3.2 Stage 1 — Sourcing integer program (OR-Tools CP-SAT)

CP-SAT is Google OR-Tools' constraint-programming-over-SAT solver; it accepts linear integer objectives and constraints, so it solves this problem as a MILP-equivalent in practice while being more robust than CBC on small combinatorial inputs. It is already a transitive dependency via the existing routing code.

**Decision variables:**
- `x[c,d] ∈ {0,1}` — is component `c` sourced from distributor `d`?
- `q[c,d] ∈ ℤ≥0` — quantity of component `c` ordered from distributor `d`
- `y[d] ∈ {0,1}` — is distributor `d` visited at all?

**Constraints:**

```
Demand coverage:        Σ_d q[c,d] = demand[c]              ∀c ∈ B
Stock cap:              q[c,d] ≤ stock[c,d] · x[c,d]        ∀(c,d) ∈ O
MOQ floor:              q[c,d] ≥ moq[c,d] · x[c,d]          ∀(c,d) ∈ O
Distributor linking:    y[d] ≥ x[c,d]                       ∀c, ∀d
US-only filter:        x[c,d] = 0   if distributor d is international
Offer existence:        x[c,d] = 0   if (c,d) ∉ O
Line-count cap:         Σ_c x[c,d] ≤ max_lines_cap            ∀d   (conditional)
```

The line-count cap is only posted on the `require_dual_source` escalation path: the model
is solved blind first, and the cap is added and re-solved only if the blind solve
consolidated the whole BOM onto a single distributor. There is **no capacity constraint**
on distributors or on hubs anywhere in the pipeline.

**Objective — AS BUILT** (`sourcing.py::solve_sourcing`, the `model.Minimize(...)` call):

The Stage 1 program minimizes **landed cost only**. There is no lead-time term and no
carbon term in it. Every term below is denominated in dollars (built in integer
milli-cents so CP-SAT stays exact):

```
minimize   Σ_(c,d)  price[c,d]              · q[c,d]     # component spend
         + Σ_d      fixed_freight[d]        · y[d]       # fixed-charge freight …
         + Σ_(c,d)  per_unit_freight[d]     · q[c,d]     #   … and its variable part
         + Σ_d      consolidation_bonus     · y[d]       # charge per supplier opened
         + Σ_(c,d)  stockout_risk_premium   · x[c,d]     # deterministic risk surcharge
         + Σ_(c,d)  graph_surcharge[c,d]    · q[c,d]     # graph_aware mode only
         + Σ_(c,d)  feed_risk_surcharge     · q[c,d]     # live GPR / ACLED feeds
```

The freight split into a fixed per-supplier charge plus a per-unit rate is a genuine
**fixed-charge transportation model** (Balinski 1965; Kuehn & Hamburger 1963), and CP-SAT
models it exactly rather than approximating it — see `_freight_model_by_did`.

`w_cost`, `w_time` and `w_carbon` **never reach this solver.**

**So where do time and carbon actually enter?** Through three per-strategy proxy levers
carried on `StrategyWeights` (`strategies.py`), applied before/inside Stage 1:

| Lever | Mechanism | Where |
|---|---|---|
| `us_only_sourcing` | Hard pre-filter dropping international offers. The single biggest carbon *and* lead-time lever, because international legs are modelled as air freight (**≈4.51× the CO₂ per kg-km of domestic truck** — see the derivation below). | `sourcing.py` offer pre-filter |
| `transport_penalty_scale` | Multiplies both freight terms above, so a higher value pushes the argmin toward nearby distributors — fewer km means fewer transit days and fewer tonne-miles. | `sourcing.py` freight model |
| `consolidation_bonus_usd` | A USD charge per distributor opened, so a higher value buys fewer pickup stops. Each extra stop adds a handling window *and* at least one transit day, because leg transit is `ceil(km / 800)` — even a 50 km leg costs a full day. | `sourcing.py` `consolidation_terms` |

**Derivation of the air-vs-truck CO₂ ratio (corrected — this doc previously said
"≈30–40×", which the code's own constants refute).** Both factors are already in the
repo; put them on the same basis of kg CO₂e per kg of freight per km:

```
truck  constants.py    CO2_G_PER_TON_MILE = 161.8 g / (US SHORT ton · mile)
       costs.py::co2_kg computes  short_tons = weight_kg / 907.18474  (KG_PER_SHORT_TON)
                                  miles      = km / 1.60934
       so per kg per km:  161.8 / (907.18474 × 1.60934) = 0.110824 g = 0.000110824 kg

air    solve.py         CO2_AIR_KG_PER_KG_KM = 0.0005   (GLEC Framework v3.2,
                        long-haul dedicated-freighter tank-to-wheel, 503 g CO2e/tonne-km)
       so per kg per km:                                  0.00050000 kg

ratio  0.00050000 / 0.000110824 = 4.512  →  ≈ 4.51×, i.e. about 4½×, not 30–40×
```

The 30–40× figure is a real number from the literature, but it compares air freight to
**ocean**, not to road. Substituting one for the other overstated the carbon case for
US-only sourcing by six to eight times. `strategies.py` carries this same derivation in
a comment beside the `greenest` profile, and its `description` string says "~4.5×".

Three notes an OR-literate reader will raise:

1. **The short-ton denominator was a live bug until 2026-09-03.** `costs.py::co2_kg` used
   to divide weight by a metric 1000 while the 161.8 g factor is per **US short ton**-mile
   (907.18474 kg), under-charging every truck leg by 9.28%. It now divides by the named
   `costs.KG_PER_SHORT_TON`. **Every truck CO₂ figure this project publishes therefore rose
   by exactly ×1.102311 (+10.231%)**, and the air-vs-truck ratio fell from the 4.97× this
   document previously printed to **4.51×**. Air CO₂ is unaffected — its factor is per
   metric tonne-km, where the mass unit cancels. Figures captured before that date and not
   re-run are stale by that factor.
2. **The two factors are not equally conservative, so 4.51× is a lower bound.** The truck
   figure is a genuine SmartWay number but from the **2013** technical documentation (via
   EDF's 2014 Green Freight Handbook p.11) — it appears in no edition of EPA's GHG Emission
   Factors Hub, which prints 170 (2023), 168 (2024) and 186 (2025) g per short ton-mile.
   The air figure is GLEC v3.2's **optimistic** default: combustion-only (tank-to-wheel),
   long-haul, full dedicated freighter. GLEC's well-to-wheel equivalent is 608, DEFRA UK
   2023 long-haul CO₂-only is 643, and DEFRA with radiative forcing is 1,099 g/tonne-km;
   belly-hold and short-haul run 2–3× higher. A like-for-like comparison would widen the
   ratio, not narrow it. (ICAO, which this repo previously credited, publishes no static
   air-freight table at all — its calculator is per-flight.)
3. Air legs carry a floored weight (`max(weight, 0.1) kg`) and no fixed CO₂ term, and
   empty truck legs emit zero because ton-miles are zero. Those affect a *realized* route
   total, not the per-kg-km factor ratio.

The three weights are used in exactly two places, both **downstream of sourcing**:

- **`cross_dock.py::_weighted_objective`** — choosing a consolidation hub and applying the
  5% accept/reject threshold. Un-normalized; the 100× and 10× factors there are ad-hoc
  unit bridges, not derived weights.
- **`solve.py`** — min-max normalizing the four finished alternatives against each other
  and ranking them (`strategies.py::normalize_objectives` + `weighted_objective`). This is
  the weighted-sum scalarization described in §5.2, and it is real — but it *selects among*
  four already-computed plans, it does not shape any of them.

**Honest one-line summary:** a cost-minimizing MILP with strategy-specific proxy levers,
plus a weighted-sum scalarization used for hub choice and for ranking. Calling it a
tri-objective MILP would be wrong.

**NOT BUILT:** an explicit lead-time term inside the Stage 1 objective. That is the clean
fix, and it is flagged in-source in `strategies.py` next to the calibrated proxy values.

**Output:** assignment `A = {(c_i, d*_i, q*_i, unit_price*_i)}` — for each BOM line, which distributor fills it, at what quantity, at what price. Plus the set of distinct distributors visited `D* = {d : y[d] = 1}`.

### 3.3 Stage 2 — Pickup TSP

Given `D*` from Stage 1 plus depot `F`, solve a **single-vehicle, uncapacitated,
symmetric TSP**: one vehicle leaves `F`, visits every distributor in `D*` exactly once,
and returns to `F` (a closed tour). Implemented in `routing.py::solve_pickup_tsp`.

**It is a TSP, not a VRP, and it is symmetric, not asymmetric.** Both of those were
misstated in earlier revisions of this doc:

- **Single vehicle, no capacity, no time windows.** The model has one vehicle
  (`RoutingIndexManager(n, 1, 0)`), registers exactly one transit callback, and adds no
  capacity dimension and no time dimension. Nothing about it is a vehicle *routing*
  problem in the multi-vehicle/capacitated sense.
- **The endpoint is nevertheless named `POST /api/v1/optimize/vrp`.** That name is
  historical — it predates the pivot and is deliberately kept so the public API and the
  frontend do not break. Treat the route name as a legacy label, not as a claim about the
  model.
- **The distance matrix is haversine**, rounded to integer metres, so `d(i,j) == d(j,i)`
  by construction. A symmetric matrix cannot produce an asymmetric TSP. Asymmetry would
  require directional costs (one-way streets, real road distances, traffic) that this
  model does not have.

**Solver, exactly as configured — two paths, and the response says which one ran.**
Every alternative carries `routing_solver.method` and `routing_solver.proven_optimal`, so
nothing on screen can call a tour optimal unless the field says it is.

1. **≤ 8 stops → exhaustive enumeration (`method = "exact_enumeration"`,
   `proven_optimal = true`).** Every distinct tour is evaluated on the same integer-metre
   haversine matrix and the cheapest wins, so the answer is a **proven** optimum. With the
   depot pinned at both ends and reversal symmetry folded away that is `n!/2` tours —
   20,160 at 8 stops, **measured at 7 ms**. The winner is then oriented to leave the depot
   toward the nearer end stop, which is a display choice and costs nothing: on a symmetric
   matrix a tour and its reverse are the same length to the metre.
2. **> 8 stops → OR-Tools routing (`pywrapcp.RoutingModel`), `PATH_CHEAPEST_ARC`
   first solution, `GUIDED_LOCAL_SEARCH` metaheuristic (`method =
   "guided_local_search"`, `proven_optimal = false`).** A metaheuristic returns a good
   local optimum and no certificate, and this path never claims one.

**Why the cut is at 8 stops.** Measured on this machine: 8 stops = 7 ms, 9 stops = 66 ms,
10 stops = 0.70 s. Render runs **one uvicorn worker on 0.5 CPU**, so this loop blocks the
whole API while it runs; the worst case has to stay in the tens of milliseconds, and 10
stops does not.

**Time limit on the metaheuristic path: 1 s up to 25 stops, 3 s above.** GUIDED_LOCAL_SEARCH
has **no convergence criterion** — it always spends its entire budget (measured: wall time
equals the limit at 9, 12, 20 and 40 stops, for limits from 100 ms to 10 s). Against a 5 s
reference, every limit from 250 ms up found the *same* tour at 9, 12, 16 and 25 stops, so
the old flat 3 s bought nothing there; at 40 and 60 stops the short limits lost up to 2.8%,
so the long budget is kept for those and only those. (An earlier revision of this doc, and
the code comment beside it, said the search "converges long before" its limit. It does not,
and it never did — that claim is what let `POST /optimize/vrp` spend ~9 s of a ~10 s
response heuristically ordering tours of 1 and 3 stops.)

If the solver returns no solution at all there is a **greedy nearest-neighbour fallback**
(`method = "greedy_nearest_neighbour"`, `proven_optimal = false`) so the caller always gets
a usable order.

**NOT BUILT: OSRM (or any) road driving distances in the optimizer.** The map page does
call the public OSRM service, but only to fetch a road-shaped polyline for *display*. The
optimizer never sees a road distance — every kilometre, dollar, day and kg of CO₂ in the
pipeline is derived from great-circle distance.

### 3.4 Cross-Dock Analysis (post-Stage-2)

For each strategy, after computing the direct-pickup TSP cost `C_direct`, evaluate consolidation:

```
for each candidate hub h ∈ H:
    C_leg1[h]  = Σ_{d ∈ D*} LTL_cost(d, h, weight_d)      # distributor → hub, N legs
    C_leg2[h]  = TL_cost_or_LTL(h, F, total_weight)        # hub → depot, 1 consolidated leg
    C_hub[h]   = C_leg1[h] + C_leg2[h] + HUB_HANDLING_FEE  # $50 per consolidation (see 5.1)
    T_hub[h]   = max(transit_d_to_h for d ∈ D*) + HUB_DWELL_DAYS + transit_h_to_F
    CO2_hub[h] = Σ LTL tonne-miles (leg 1) + TL tonne-miles (leg 2, consolidated weight)

h* = argmin over h of weighted_objective(C_hub[h], T_hub[h], CO2_hub[h])

if weighted_objective(h*) < 0.95 · weighted_objective(direct):
    use hub h* (≥5% improvement threshold)
else:
    use direct pickup
```

**What this actually is: exhaustive enumeration over a fixed 10-candidate set.** The ten
hubs are hardcoded in `freight_hubs.py`. Every one is scored, the argmin of the strategy's
weighted objective wins, and it is accepted only if it clears the 5% threshold. Because
the candidate set is fixed and tiny, that enumeration is **exact** — it returns the global
optimum over the modelled hub set, with no heuristic and no gap, in microseconds. That is
a real property worth stating; it is just not a sophisticated one, and it holds *because*
the set is small, not because of any clever decomposition.

**What it is not.** There are no Lagrangian multipliers, no relaxed constraints, and no
capacity constraints anywhere in `cross_dock.py`. Hubs are modelled as uncapacitated and
always available, so there is nothing to relax. Earlier revisions of this doc and of the
module docstring described this as "Lagrangian relaxation of the Capacitated Facility
Location Problem (Daskin 2013, Ch. 4)". That was never true of this code and has been
removed.

**NOT BUILT — genuine future direction.** If the hub set grew to hundreds of candidates,
or if hubs gained per-hub throughput capacity, this *would* become a Capacitated Facility
Location Problem, and Lagrangian relaxation with subgradient ascent inside
branch-and-bound (Daskin, *Network and Discrete Location*, 2013, Ch. 4) is the standard
treatment. That is the upgrade path, not a description of today's module.

**Two percentages, deliberately kept separate** (`CrossDockDecision`, and worth pointing
at in an interview — it is where a naive implementation lies to the user):

- `objective_savings_pct` — improvement on the strategy's **weighted objective**. This,
  and only this, is what the 5% accept/reject threshold tests.
- `savings_vs_direct_pct` — the transport-**cost** reduction actually taken. It is `0.0`
  whenever the hub was rejected, because a saving nobody banks is not a saving, and it can
  legitimately be **negative**: a time- or carbon-weighted strategy may rationally pick a
  hub that costs *more* and buys speed or tonne-miles with the difference.

When the second case fires, the emitted `rationale` says so in words — *"it does NOT save
money: transport cost charged is \$X vs \$Y direct, i.e. N% MORE"* — instead of printing a
negative number labelled "savings". `candidate_cost_savings_pct` additionally reports what
the best hub *would* have saved even when the decision rejected it, so a sub-threshold
near-miss stays visible.

A further correctness detail: the hub plan and the direct plan are compared on the **same
scope**. International air-freight consignments cannot be consolidated at a domestic hub,
so they are passed in as a `parallel` stream and added to *both* sides. Omitting them (as
this module originally did) compared a domestic-only hub plan against a direct plan that
also paid for transpacific air freight, and reported the difference as a consolidation
saving.

**Why cross-dock changes per strategy:**
- `cheapest` uses hub iff cost savings > 5%
- `fastest` almost never uses hub (adds hub dwell time, no time benefit)
- `greenest` prefers hub whenever ≥3 distributors are visited (tonne-miles reduction)
- `balanced` depends on the combined weighted objective

This guarantees the four strategies produce materially different routes — not just different labels.

### 3.5 Two-stage stochastic sourcing with CVaR (built later, genuinely implemented)

Added after the original Stage A scope and worth stating precisely, because unlike §3.2
and §3.4 the named techniques here **are** in the code (`optimization/stochastic.py`,
exposed at `POST /api/v1/stochastic/frontier`):

- **A real two-stage stochastic program.** First stage is here-and-now (`y[d]` qualify a
  distributor, `x[c,d]` award a BOM line, `q[c,d]` commit units). A scenario `s` is a set
  of distributors that cannot deliver. Second stage is genuine recourse: `r[c,d,s]`
  emergency re-procurement from survivors, `u[c,s]` unmet demand, `e[d,s]` an expedited
  consignment. The model re-optimizes after the disruption is observed — which is exactly
  what the deterministic risk surcharges in §3.2 cannot do.
- **CVaR objective, Rockafellar–Uryasev linearization.** Minimizes
  `(1 − λ)·E[cost] + λ·CVaR_α[cost]`, with CVaR linearized as
  `min_η { η + 1/(1−α) · E[(Z − η)⁺] }` — one free scalar `η` plus one non-negative `z_s`
  per scenario with `z_s ≥ C_s − η`. Everything stays linear, so CP-SAT solves it exactly:
  no piecewise approximation, no quadratic term, no separate risk solver.
- **CVaR rather than variance** because variance penalizes upside as well as downside and
  is quadratic (CP-SAT cannot take it), while CVaR is coherent — monotone, subadditive,
  positively homogeneous, translation invariant — per Artzner, Delbaen, Eber & Heath
  (1999), *Coherent Measures of Risk*, Mathematical Finance 9(3):203–228. Subadditivity is
  the load-bearing one: the model cannot be made to look safer by splitting one BOM in two.
- **Sample Average Approximation with exact enumeration** where the distributor count
  permits it, and **Mak–Morton–Wood optimality gap bounds** on the SAA solution.
- **Kneedle knee detection** on the resulting risk/cost frontier.

Refer to `optimization/stochastic.py` and `api/stochastic.py` for the authoritative
statement of each; the module docstrings carry the citations.

## 4. Architecture

### 4.1 Backend package structure

```
backend/app/optimization/              # as built (2026-08)
  __init__.py
  constants.py        # Cited freight/unit constants, defined once
  costs.py            # Cost + time + CO2 functions over those constants
  sourcing.py         # Stage 1 CP-SAT sourcing MILP (cost-only objective — see §3.2)
  routing.py          # Stage 2 single-vehicle symmetric TSP
  greedy.py           # Greedy baselines the MILP is scored against
  cross_dock.py       # Cross-dock analysis + hub selection by enumeration
  stochastic.py       # Two-stage stochastic program with CVaR (added later — see §3.5)
  newsvendor.py       # Newsvendor decision layer: demand distribution → order quantity
  recommendations.py  # Post-solve advisory output
  strategies.py       # The four weight profiles + proxy levers + scalarization helpers
  freight_hubs.py     # Static data: 10 US freight hubs
  countries.py        # Distributor country → ACLED ISO-3166-1 alpha-3 key
  solve.py            # Orchestrator: run all 4 strategies, rank, return alternatives
  schemas.py          # Pydantic response models (RouteAlternative, CostBreakdown, StrategyMath...)

backend/app/api/optimize.py            # 221 lines as built. The original plan said this
                                       # would "shrink to ~50 lines: endpoint wiring only";
                                       # it did not. Besides the two route handlers it still
                                       # builds the BOM from cart rows, derives offer /
                                       # Chinese-origin fields, assembles distributor metadata
                                       # (including _distributor_tier, see §5.1) and persists
                                       # the Order. Moving that out is unclaimed work.
backend/seeds/seed_demo_cart.py        # NEW: curated 5-part BOM for demo user
backend/seeds/cleanup_stale.py         # NEW: one-shot drop of materials/suppliers/production_hubs
```

### 4.2 Module interfaces (public API contract for Stage-B upgrade)

```python
# sourcing.py
def solve_sourcing(
    bom: List[BomLine],
    offers: List[DistributorOffer],
    weights: StrategyWeights,
    us_only: bool = True,
) -> SourcingResult:
    """Stage 1: pick which distributor fills each BOM line."""

# routing.py
def solve_pickup_tsp(
    depot: GeoPoint,
    distributors: List[Distributor],
) -> List[int]:  # ordered indices into distributors
    """Stage 2: TSP over the selected distributors."""

# cross_dock.py
def evaluate_cross_dock(
    direct_route: RouteMetrics,
    distributors: List[Distributor],
    depot: GeoPoint,
    weights: StrategyWeights,
    hubs: List[FreightHub],
) -> CrossDockDecision:
    """Pick best hub or decide direct is better."""

# solve.py
def optimize_bom(
    bom: List[BomLine],
    offers: List[DistributorOffer],
    distributors: Dict[int, Distributor],
    depot: GeoPoint,
) -> MultiRouteResponse:
    """Orchestrator — runs all 4 strategies end-to-end."""
```

When sub-project B (two-echelon MILP) lands, only `solve_sourcing` + `solve_pickup_tsp` + `evaluate_cross_dock` get replaced by a single `solve_two_echelon` call. The orchestrator, API endpoint, frontend, and response schema are unchanged.

### 4.3 Data model changes

**New table:**
```sql
CREATE TABLE cross_dock_hubs (
    id              INTEGER PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    operator        VARCHAR(100),
    hub_type        VARCHAR(50),   -- 'air', 'intermodal', 'truck'
    city            VARCHAR(100),
    state           VARCHAR(10),
    latitude        FLOAT NOT NULL,
    longitude       FLOAT NOT NULL,
    annual_throughput_desc  TEXT,
    source_citation VARCHAR(300)
);
```

**Dropped tables** (via one-shot SQL, not Alembic):
- `materials`
- `suppliers`
- `production_hubs`
- `price_history`
- `price_forecasts`

**Existing tables:** no schema changes. `distributors.is_domestic` already exists.

**API additions:**
- `GET /api/v1/optimize/hubs` — list of 10 cross-dock hubs for map display
- `GET /api/v1/distributors?domestic_only=true` — hard filter at the API layer
- Extended `RouteAlternative` response schema:

```python
class CostBreakdown(BaseModel):
    component_cost: float        # sum of (price × quantity)
    transport_cost: float        # sum of LTL + TL legs
    holding_cost: float          # 25% annualized × lead time

class StrategyMath(BaseModel):
    weights: Dict[str, float]                    # {cost: 0.4, time: 0.35, carbon: 0.25}
    raw_objective_values: Dict[str, float]       # {cost: 10459, time: 26, carbon: 0.318}
    normalized_objective_values: Dict[str, float]  # normalized 0-1 for weighting
    weighted_total: float                        # final objective value
    citations: List[str]                         # ["ATRI 2023", "EPA SmartWay Tech Doc 2013", "GLEC v3.2", ...]

class CrossDockInfo(BaseModel):
    enabled: bool
    hub_id: Optional[int]
    hub_name: Optional[str]
    savings_vs_direct_pct: float
    direct_cost_usd: float
    consolidated_cost_usd: float

class RouteAlternative(BaseModel):
    # ... existing fields ...
    cost_breakdown: CostBreakdown
    strategy_math: StrategyMath
    cross_dock: CrossDockInfo
```

## 5. The Math, in Detail

### 5.1 Cost functions (all citations)

**Transport cost — Truckload (TL):**
```
C_TL(distance_km, weight_kg) = distance_miles × $2.271
```
Source: **American Transportation Research Institute (ATRI), *An Analysis of the Operational Costs of Trucking: 2023 Update*** — average marginal cost per mile across fuel ($0.688), driver wages+benefits ($0.861), equipment+maintenance ($0.365), insurance+tolls+permits ($0.250), and other ($0.107). Applies when `weight_kg ≥ 4,536` (10,000 lbs industry FTL threshold).

**Transport cost — Less-than-Truckload (LTL):**
```
C_LTL(distance_km, weight_kg) = base_fee + (weight_cwt × distance_miles × per_cwt_mile_rate)
  where base_fee = $75                # minimum pickup charge (Old Dominion 2023 tariff)
        weight_cwt = weight_lbs / 100
        per_cwt_mile_rate = $0.43     # FreightWaves SONAR Q4 2023 national LTL benchmark
```
Source: **FreightWaves SONAR public LTL benchmarks, Old Dominion Freight Line published tariffs.** LTL applies when `weight_kg < 4,536`.

For a cart of electronic components (typical weight ~5 kg for 150 IC parts), all direct-pickup legs will be LTL. Cross-dock consolidation enables the final leg (hub → depot) to cross into TL territory if multiple distributor shipments are combined — this is the core economic rationale for cross-docking.

**Lead time:**
```
T_leg(distance_km, distributor_tier) = handling_days[tier] + transit_days(distance_km)
  where handling_days = {'major': 1, 'mid': 2, 'broker': 3}
        transit_days(d_km) = ceil(d_km / 800)     # 800 km/day effective ground freight
```
Sources:
- **BTS Commodity Flow Survey 2022** — average LTL speed calculated at 800 km/day.
- Distributor tier classification — **AS BUILT** (`api/optimize.py::_distributor_tier`, duplicated in `seeds/run_benchmark.py`): an **absolute threshold on `total_offers`**, not a rank. `total_offers ≥ 500 → major`, `≥ 100 → mid`, else `broker`. On the served DB that yields **2 major / 31 mid / 59 broker** across the 92 distributors. (An earlier revision of this doc said "top 10 by offer count = major, next 30 = mid" — a rank-based scheme that has never existed in the code.) Tiering is a proxy either way — the data doesn't include SLAs, so we approximate.

**Cross-dock dwell (`HUB_DWELL_DAYS`):** 0.5 day per hub (BTS Intermodal Freight Transportation Model assumption).

**Hub handling fee (`HUB_HANDLING_FEE`):** $50 per consolidation. Covers unloading, sorting, and reloading at the cross-dock terminal. Sourced from ATA Cross-Docking Best Practices (2019) reported range of $30–$80 per consolidation for electronics/small-parcel freight; midpoint used.

**Carbon:**
```
CO2_kg = (weight_kg / 907.18474) × distance_miles × 0.1618
```
The denominator is a **US short ton** (`costs.KG_PER_SHORT_TON`), not a metric tonne. Source: **"EPA SmartWay Shipper Partner Tool: Technical Documentation" (2013)**, as cited in **EDF's Green Freight Handbook (2014) p.11**, heavy-duty truck factor: **161.8 g CO2e / short ton-mile**. This value is *not* in EPA's GHG Emission Factors Hub (Table 8 prints 170 / 168 / 186 for 2023 / 2024 / 2025), so the "EPA SmartWay 2023" label this doc previously used named the wrong vintage.

**Holding cost (time → dollars):**
```
H(component_cost, lead_time_days) = component_cost × 0.25 × (lead_time_days / 365)
```
Source: **Gartner IT Supply Chain Benchmarks 2022** — electronics/semiconductor annual holding cost ≈ 25% of inventory value (capital cost + obsolescence + warehousing + insurance). This lets us express time in dollars for proper multi-objective weighting.

### 5.2 Strategy weight profiles

| Strategy | w_cost | w_time | w_carbon | Industry basis |
|---|---|---|---|---|
| **Lowest Cost** | 1.00 | 0.00 | 0.00 | Pure procurement optimization (Weber, 1991) |
| **Fastest** | 0.15 | 0.80 | 0.05 | JIT/lean procurement (Toyota Production System literature) |
| **Greenest** | 0.25 | 0.05 | 0.70 | ESG-compliant procurement (CDP Supply Chain Disclosure framework) |
| **Balanced** | 0.40 | 0.35 | 0.25 | Ghodsypour & O'Brien (1998), *A decision support system for supplier selection using an integrated analytic hierarchy process and linear programming*, Int'l Journal of Production Economics 56-57, 199-212. Weights derived from the Weighted Point Method section of the paper. |

**How the weights are actually used — read §3.2 first.** These weights do **not** enter the
Stage 1 MILP, which minimizes cost only. They drive (a) cross-dock hub selection and (b)
the min-max-normalized ranking of the four finished alternatives. What differentiates the
four *sourcing plans* is the three proxy levers, whose as-built values are:

| Strategy | `us_only_sourcing` | `transport_penalty_scale` | `consolidation_bonus_usd` |
|---|---|---|---|
| **Lowest Cost** | false | 1.0 | 0.5 |
| **Fastest Delivery** | true | 1.0 | 150.0 |
| **Lowest Carbon** | true | 2.5 | 2.5 |
| **Balanced** | true | 1.5 | 2.0 |

**These numbers were measured, not guessed (re-tuned 2026-08-16).** The previous values
for "Fastest Delivery" were `transport_penalty_scale = 0.0` and
`consolidation_bonus_usd = $3.00` — i.e. the strategy was effectively blind to both
distance and supplier count, so it just minimized component price among domestic offers.
Measured on real BOMs, it consequently produced the **longest** tour of the four
strategies: 4th of 4 on ETA at both 12 and 40 BOM lines (9.5 days vs 5.5 days for
"Lowest Carbon"), across 13 pickup stops. A strategy named "Fastest" that was reliably the
slowest is exactly the kind of thing an interviewer finds by clicking once.

The fix: charge real unscaled transport cost (`1.0`, so the plan is not distance-blind)
and price the *time* cost of opening one more supplier at `$150` — 2× the \$75 LTL base
fee. Swept against real BOMs of 2 / 5 / 12 / 25 / 40 lines, "Fastest Delivery" now has the
lowest ETA of all four strategies on **every** one, while remaining a distinct plan
wherever the strategies diverge at all. Raising the distance penalty instead (≥1.5) also
makes it fastest, but collapses it onto "Lowest Carbon" — same lever, same answer.

This is honest calibration of a proxy, not a lead-time model. The clean fix remains a real
lead-time term in the Stage 1 objective (**NOT BUILT**, flagged in-source).

**Normalization:** Because cost ($), time (days), and carbon (kg CO2) have incompatible units, the objective function normalizes each term to [0,1] against the min/max observed across a baseline solve, then applies the weights. The raw values are still reported to the user — the normalization is only for the comparison step. This is the standard **weighted sum scalarization** technique from multi-objective optimization (Marler & Arora, 2004, *Survey of multi-objective optimization methods for engineering*, Structural & Multidisciplinary Optimization 26(6)).

### 5.3 Why this matters mathematically

The old code computed `cost = time = carbon = α·distance`, so every weighted combination collapsed to the same function. The new code:
- `cost` depends on **which offer** you picked (post-outlier-filter price varies 3.8×–17× for the same MPN across distributors, dominating any distance contribution), not just distance
- `time` depends on **lead time tier of the distributor** plus **ceiling of distance/800 km** (discrete days), not a continuous proportional quantity
- `carbon` depends on **actual weight** per shipment, varying by quantity and component, not a constant

None of the three objectives can be expressed as a scalar multiple of any other, so the
three reported metrics are genuinely independent quantities rather than three labels on
one number — which is what was broken in the old code.

**Caveat, stated plainly:** independence of the three *reported metrics* is not the same
as four distinct optima of a tri-objective program, and this pipeline does not solve one
(§3.2). The four plans differ because the three proxy levers differ, and on a small or
geographically concentrated BOM two strategies can and do land on the same plan. That is a
real limitation, not a bug: with a cost-only Stage 1 objective, the levers are the only
thing separating the strategies. The regression test in §8.1 asserts the strategies are
distinguishable on the curated demo BOM, not that they are distinct on every input.

### 5.4 Outlier filtering (robust preprocessing)

Real Nexar/Octopart data has a small number of clearly bad records per MPN — obsolete inventory from defunct brokers, mis-keyed SKUs, rare-packaging variants mislabeled under the base MPN. An unfiltered MILP will mostly ignore them (it's minimizing cost, so high outliers don't bind), but *any* offer can be selected if a tight stock or MOQ constraint forces it, producing absurd results. The filter is also the first thing a procurement analyst would do by hand, so it belongs in the pipeline.

**Rule (applied per-MPN before Stage 1):**
```
Let M = median({price_i : i ∈ offers(c)})
Drop offer i iff price_i > k · M   with k = 5
```

Rationale: `k = 5` is the standard cut in procurement analytics (see Aberdeen Group 2020 "Data Quality in Direct Materials Sourcing" — outliers defined as >5× the median unit price for a given part number). It's a one-sided filter — low outliers are real discounts and stay in. Empirically on our five demo parts, this drops 0–5 offers per MPN (0 for ESP32, 3 for STM32, 1 for GD25, 1 for ESP8266, 5 for ATMEGA) and leaves clean spreads between 3.8× and 17×.

**Implementation:** 8 lines in `sourcing.py`, runs in O(n log n) per MPN. Filtered offers are logged with reason (`"dropped: price 1447.87 > 5×median 2.71"`) so the removal is auditable.

**Interview talking point:** "I ran the median-multiplier outlier filter before the integer program because the Nexar data has known quality issues — a few records per MPN with wrong prices or mis-linked SKUs. Robust preprocessing is part of any production sourcing system. The full audit log is in the response payload."

### 5.5 The ten freight hubs

| # | Name | Operator | Type | City | State | Lat | Lng |
|---|---|---|---|---|---|---|---|
| 1 | Memphis International SuperHub | FedEx Express | air | Memphis | TN | 35.0424 | -89.9767 |
| 2 | UPS Worldport | UPS | air | Louisville | KY | 38.1744 | -85.7360 |
| 3 | DFW Alliance Global Logistics Center | BNSF/Hillwood | intermodal | Fort Worth | TX | 32.9876 | -97.3187 |
| 4 | CenterPoint Intermodal Center–Joliet | BNSF | intermodal | Joliet | IL | 41.4988 | -87.9865 |
| 5 | Hartsfield–Jackson Cargo | Multiple | air | Atlanta | GA | 33.6407 | -84.4277 |
| 6 | Port of Long Beach Intermodal | Multiple | marine/rail | Long Beach | CA | 33.7406 | -118.2757 |
| 7 | Rickenbacker Intermodal Terminal | Norfolk Southern | intermodal | Columbus | OH | 39.8130 | -82.9279 |
| 8 | Kansas City SmartPort | BNSF/KCS | intermodal | Kansas City | MO | 39.2976 | -94.7139 |
| 9 | FedEx Indianapolis Hub | FedEx Express | air | Indianapolis | IN | 39.7173 | -86.2944 |
| 10 | Ontario International Intermodal | Multiple | air/intermodal | Ontario | CA | 34.0559 | -117.6005 |

All coordinates verified against Google Maps / public airport databases. All ten are real, operationally active freight hubs. Hub 5 (Atlanta) is ~240 km from the Greenville SC depot, making it the geographically preferred consolidation point for most demo scenarios; but distributor geography will often push the optimizer toward Louisville, Memphis, or Columbus depending on the strategy.

## 6. Curated Demo BOM

The demo user ("Greenville Advanced Manufacturing", depot 34.8526, -82.3940) will have a pre-seeded cart representing a production run of wireless IoT sensor nodes. All five MPNs verified present in the current DB with ≥15 distributor offers each:

| # | MPN | Component ID | Manufacturer | Category | Qty | Offers | Clean offers | Clean price spread |
|---|---|---|---|---|---|---|---|---|
| 1 | ESP32-WROOM-32UE-N4 | 314 | Espressif Systems | System on Chip | 50 | 18 | 18 | $1.47–$5.59 |
| 2 | STM32F103C8T6 | 37 | STMicroelectronics | Microcontrollers | 50 | 56 | 53 | $0.49–$8.40 |
| 3 | GD25Q64CSIGR | 363 | GigaDevice | Memory (64Mb flash) | 50 | 17 | 16 | $0.18–$1.66 |
| 4 | ESP8266EX | 1 | Espressif Systems | RF Transceiver | 50 | 20 | 19 | $0.49–$2.12 |
| 5 | ATMEGA328P-PU | 130 | Microchip | Microcontrollers | 25 | 55 | 50 | $1.41–$11.47 |

"Clean" columns are the post-outlier-filter values actually used by the sourcing model (see §5.4). The raw data includes a handful of clearly broken records — e.g., one ATMEGA offer at $1447 from a distributor with an unrelated SKU (`ST63735664`), and two STM32 listings at $722/$766 that are obsolete or rare-packaging variants. Those get filtered before the integer program ever sees them.

**Narrative:** "Build 50 wireless sensor nodes + 25 bootloader spare MCUs." Even after outlier removal, the spreads stay meaningful — 3.8× on ESP32, 17× on STM32, 9.2× on GD25, 4.3× on ESP8266, 8.1× on ATMEGA. Plenty of room for the four strategies to differentiate: "cheapest" picks the low-price offers from discount distributors, while "fastest" pays a premium for top-tier distributors (DigiKey/Mouser/Arrow) with 1-day handling.

## 7. Frontend Changes

### 7.1 CheckoutPage — Math & Sources panel

Each of the four route cards gains a new expandable **"Objective Breakdown"** section (collapsed by default, expand on click) showing:

```
Objective function (Balanced strategy):
  0.40 × cost + 0.35 × time + 0.25 × carbon

Raw values:
  Component cost:    $  412.50
  Transport cost:    $   89.20   (3 LTL legs, 1 TL leg via Atlanta hub)
  Holding cost:      $    8.40   (4.2 days × 25%/yr × $2,920 inventory)
  Total cost:        $  510.10
  Lead time:         4.2 days
  CO2 emissions:     3.51 kg

Normalized (across alternatives):
  Cost:   0.32      × 0.40  =  0.128
  Time:   0.68      × 0.35  =  0.238
  Carbon: 0.12      × 0.25  =  0.030
  ────────────────────────────────
  Weighted objective:         0.396

Sources: ATRI 2023 · EPA SmartWay Tech Doc 2013 (via EDF 2014) · GLEC v3.2 · Gartner 2022
```

### 7.2 CheckoutPage — Cross-Dock comparison

For each card where cross-dock is used, display a two-column mini-chart:

```
  Direct Pickup         →  Consolidated via Atlanta Hub
  ─────────────         ─────────────────────────────
  4 LTL legs               3 LTL + 1 TL leg
  $1,245                   $987            (−20.7%)
  4.2 days                 4.7 days        (+0.5 dwell)
  7.06 kg CO2              4.52 kg CO2     (−35.9%)
```

### 7.3 MapPage — Cross-Dock hub layer

- New static layer for the 10 hubs, amber diamond markers, visible always at zoom ≥ 4.
- Tooltip on hover: hub name, operator, type.
- When a cross-docked route is selected in the sidebar:
  - Thin dashed lines from each distributor to the chosen hub (LTL segments)
  - One thick solid line from the hub to the depot (consolidated TL segment)
  - The chosen hub marker grows and becomes highlighted
- When a direct-pickup route is selected, existing road-path rendering unchanged.

### 7.4 Data sources footer

Every route card and the interview walkthrough doc include a "Data sources" line citing ATRI 2023, EPA SmartWay Technical Documentation 2013 (via EDF Green Freight Handbook 2014), GLEC Framework v3.2, Gartner 2022, FreightWaves SONAR, and the academic references. This is the single most important signal for interview audiences.

## 8. Verification Plan

### 8.1 Unit tests (pytest)

**`backend/tests/test_sourcing.py`:**
- `test_outlier_filter_drops_price_above_5x_median` — construct offers with prices `[1.40, 1.50, 2.00, 2.50, 2.80, 1447.87]`, assert the $1447 is dropped and the median-multiplier log entry is present
- `test_outlier_filter_keeps_low_outliers` — construct offers `[0.20, 2.00, 2.10, 2.20]`, assert the $0.20 is kept (it's a real discount, not a data error)
- `test_sourcing_picks_cheapest_offer_when_stock_available` — construct a 1-line BOM with 3 offers, assert the $0.49 one is chosen under the `cheapest` strategy
- `test_sourcing_respects_moq` — set MOQ=100 on the cheapest offer, demand=5, assert solver either picks a more expensive offer or orders 100 at the cheap one
- `test_sourcing_rejects_international_when_us_only_true` — include an intl offer cheaper than all US offers, assert it's not picked
- `test_sourcing_splits_across_distributors_when_stock_insufficient` — cheap offer has stock=10, demand=50, assert the solver splits

**`backend/tests/test_cross_dock.py`:**
- `test_cross_dock_chosen_only_when_savings_exceed_5pct` — construct scenario where hub saves exactly 4%, assert direct is chosen; scenario where hub saves 10%, assert hub is chosen
- `test_cross_dock_never_chosen_for_single_distributor_route` — single stop, assert cross-dock is never beneficial
- `test_cross_dock_prefers_geographically_central_hub` — distributors spread across the Midwest, assert Columbus or KC is chosen over LA or Ontario

**`backend/tests/test_strategies.py`:**
- `test_four_strategies_produce_different_routes_on_curated_bom` — **the regression test for the current bug.** Run the solver on the demo BOM, assert all four `total_cost_usd` values are distinct AND the `distributor_ids` lists differ between at least two strategies.
- `test_cheapest_strategy_minimizes_component_cost` — run cheapest, verify no other strategy has a lower `component_cost`
- `test_fastest_strategy_minimizes_lead_time` — run fastest, verify no other strategy has a lower `eta_p50`
- `test_greenest_strategy_minimizes_tonne_miles` — run greenest, verify no other strategy has a lower `total_co2e_kg`

### 8.2 Playwright E2E test

One test at `tests/e2e/sub-project-a.spec.ts` executed via the Playwright MCP:

1. Open `http://localhost:5173/login` → click "Demo Login"
2. Wait for dashboard navigation
3. Navigate to `/cart` → assert 5 line items visible with the 5 curated MPNs
4. Click "Optimize & Checkout" → wait for `[data-testid="route-cards"]`
5. Assert 4 distinct route cards visible
6. Assert no two cards have identical `total_cost_usd` text (regression for bug)
7. Click "Objective Breakdown" on the Balanced card → assert citation line includes "ATRI" and "EPA"
8. Assert at least one card shows "Consolidated via" text (cross-dock was chosen by at least one strategy)
9. Click "View on Map" → assert ≥ 1 amber diamond marker for the chosen hub is visible
10. Take screenshot → `test-screenshots/sub-project-a-demo.png`

### 8.3 Ship checklist (end of day must be green)

- [ ] All pytest tests in `test_sourcing.py`, `test_cross_dock.py`, `test_strategies.py` pass
- [ ] Playwright E2E runs without error, screenshot saved
- [ ] Four strategies return four distinct `total_cost_usd` values on the demo BOM
- [ ] At least one strategy selects a real cross-dock hub
- [ ] Cross-dock visualization visible on the map
- [ ] Objective Breakdown panel shows citations on each card
- [ ] A one-page whiteboard walkthrough exists covering all sections in 9.1
      (kept as a maintainer working note, not published)
- [ ] This design doc exists and is committed
- [ ] No references to `materials`, `suppliers`, `production_hubs`, `hubs.py`, or `materials.py` in the current codebase
- [ ] `git commit` clean

## 9. One-page whiteboard walkthrough

### 9.1 Structure

A one-page markdown (kept as a maintainer working note rather than published) that lets a
reader reconstruct the problem from scratch on a whiteboard. Sections:

1. **Business framing** (1 paragraph) — PCB manufacturer sourcing electronic components for a production run, needs to balance cost / delivery time / carbon across 92 distributors offering 8,000+ competitive price offers
2. **Decision variables** (math notation)
3. **Objective function** (all three terms + strategy weights with citations)
4. **Constraints** (list)
5. **Why CP-SAT, not pure LP or pure OR-Tools routing** — integer quantities, combinatorial supplier selection, hybrid with TSP
6. **Cross-dock hub selection** — enumeration over 10 fixed candidates is exact because the candidate set is small and uncapacitated; say that, and say what would change (CFLP + Lagrangian relaxation) if the set grew. Do not claim the relaxation is implemented.
7. **Extensions (sub-project B)** — two-echelon joint MILP, time windows, stochastic demand, real OSRM road distances, weather+traffic-adjusted ETAs

## 10. Hygiene & Cleanup

### 10.1 Delete

- `backend/app/api/hubs.py`
- `backend/app/api/materials.py`
- `backend/app/models/material.py` (verify no imports first)
- `backend/app/models/supplier.py`
- `backend/app/models/production_hub.py`
- All references from `backend/app/api/__init__.py` and `backend/app/models/__init__.py`

### 10.2 Drop tables (one-shot SQL, not Alembic)

`backend/seeds/cleanup_stale.py` runs:
```sql
DROP TABLE IF EXISTS materials;
DROP TABLE IF EXISTS suppliers;
DROP TABLE IF EXISTS production_hubs;
DROP TABLE IF EXISTS price_history;
DROP TABLE IF EXISTS price_forecasts;
DELETE FROM cart_items;
DELETE FROM orders;
```

Alembic skipped intentionally — the stale tables came from a pre-pivot schema that was never formally migrated away from, so writing a migration would cement history we don't want. SQLite is dev-only; production uses a fresh seed anyway.

### 10.3 US-only API filter

`GET /api/v1/distributors` and `GET /api/v1/components/:id/offers` both accept a `domestic_only` query parameter — but **AS BUILT it defaults to `false`** (`api/distributors.py`, `api/components.py`: `domestic_only: bool = Query(False)`), so the full international set is returned unless a client passes `?domestic_only=true`. An earlier revision of this doc had the default the other way round. The domestic restriction that actually shapes published plans is the per-strategy `us_only_sourcing` pre-filter in the optimizer (§3.2), not an API default. The database keeps all 92 distributors for traceability and for future relaxation.

### 10.4 Minor fix

`backend/app/api/components.py:204` — replace deprecated `regex=` with `pattern=`.

### 10.5 Deliberately NOT deleted

- `/api/v1/auth/demo` — kept for portfolio walkthrough convenience
- `backend/app/api/live_prices.py` — scaffolding for future features, untouched in Sub-Project A
- `market_intelligence.py` was in this list too. It was **deleted on 2026-09-01** along with its six
  `/market/*` routes: the upstream REST path it targeted 404s with or without a token, so the routes
  had never once returned data, and nothing in `frontend/src` called them. See
  `docs/OUTSTANDING_WORK.md` item 55.

## 11. Risks & Mitigations

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| 1 | CP-SAT too slow on full offer set | Low | BOM has 5 items × ~20 offers each = ~100 binary vars. CP-SAT handles this in milliseconds. |
| 2 | Curated MPNs missing or data drift | Low | Already verified present in DB at design time. Seed script validates on run, fails loudly if missing. |
| 3 | Frontend schema breaks on response extension | Low | Extensions are additive optional fields. `CheckoutPage.tsx` only reads existing fields plus new ones it renders directly. |
| 4 | Playwright test flaky on loading states | Medium | Use `data-testid` hooks, explicit `waitFor` on network idle + DOM selector, not time-based sleeps |
| 5 | Cross-dock math produces worse routes than direct in all cases | Medium | Test fixture constructed specifically with 4+ distributors spread across the East Coast so Atlanta/Louisville hub is clearly beneficial. If not, tune the 5% threshold or increase LTL base fee. |
| 6 | Running out of time | High | Work order (section 12). Drop the stretch goals first, then the Math & Sources UI polish, then the E2E test → unit tests alone. |
| 7 | Dropped tables cause SQLAlchemy startup errors | Medium | After drop, restart backend process and verify `app.main:app` imports cleanly before moving on. |

## 12. Work Order (time-ordered, cut points marked)

Rough target times are not commitments; the important thing is the **order** and the **cut points**.

1. **Core math** — `optimization/costs.py`, `freight_hubs.py`, `strategies.py` (constants + data, no logic)
2. **Stage 1 solver** — `sourcing.py` with CP-SAT MILP
3. **Stage 2 solver** — `routing.py` (port + clean from existing code)
4. **Cross-dock** — `cross_dock.py`
5. **Orchestrator + API wire** — `solve.py` + shrunk `optimize.py` endpoint
6. **Cleanup script** — `cleanup_stale.py`, run it, drop tables, delete stale files
7. **Demo cart seed** — `seed_demo_cart.py`, run it
8. **Unit tests** — all three test files, get them green
9. **Curated BOM end-to-end smoke test** — hit the API, assert four distinct routes manually
10. **Frontend: extend response types + Objective Breakdown panel**
11. **Frontend: Cross-Dock comparison on checkout cards**
12. **Frontend: Map cross-dock hub layer + LTL/TL line rendering**
13. **Playwright E2E test**
14. **Interview walkthrough doc**
15. **Final commit + screenshot**

**Cut points** (if running out of time, in this order drop):
- Step 14 → inline walkthrough into this design doc instead
- Step 13 → keep unit tests only, skip E2E (highest-complexity cut)
- Step 12 → ship cross-dock on checkout page only, no map layer
- Step 11 → ship objective breakdown but not cross-dock comparison
- Step 10 → ship API-level correctness only, no new frontend visualization (acceptable minimum — the API is testable, screenshots can show raw JSON)

The minimum-viable shipping cut is **steps 1–9 + at least a screenshot of the new API response showing four distinct routes and a cross-dock selection**. Everything after step 9 is increasing visual polish on correct underlying math.

## 13. Out of Scope (Explicit)

These are NOT part of Sub-Project A. Most are planned follow-ups:

- ❌ **Sub-Project B — Two-echelon MILP** (joint facility + routing optimization)
- ❌ Live weather data integration (stretch goal if time permits — see section 14)
- ❌ Live traffic data integration (stretch goal — see section 14)
- ❌ Weather per-leg ETA adjustment (the user's chosen weather target when time permits)
- ❌ Real register/login UX polish (kept as demo JWT shortcut)
- ❌ OSRM driving distances **in the optimizer** — still NOT BUILT. Every distance the optimizer uses is haversine. (The map page does call public OSRM, but only to draw a road-shaped polyline for display; see §3.3.)
- ❌ Air freight expediting option — **partly built since.** International distributors are now modelled as parallel air-freight consignments in `solve.py` (flat IATA-derived base + per-kg rate), and the stochastic program in §3.5 carries an explicit expedite decision `e[d,s]` in its recourse stage. It is still NOT a decision variable in the deterministic Stage 1 MILP.
- ❌ Digital twin scenario simulator changes
- ❌ LTL rate table sophistication (using simplified single-rate; real LTL uses NMFC class tariffs)
- ❌ Multi-depot / multi-factory extension
- ❌ Stochastic demand or lead time — **this one has since been built.** A two-stage
  stochastic sourcing program with a CVaR objective now lives in
  `backend/app/optimization/stochastic.py` and is exposed at
  `POST /api/v1/stochastic/frontier`. See §3.5.

## 14. Stretch Goals — NOT BUILT

Neither of the following was implemented. There is no weather client and no traffic
client in the codebase; nothing reads `OPENWEATHER_API_KEY`. Kept here as the original
plan, not as a description of behaviour.

1. **Weather overlay + ETA adjustment (user's preferred target, Q9=ii):**
   - Add `OpenWeatherMapClient` using the already-configured `OPENWEATHER_API_KEY`
   - Pull severe weather alerts for each distributor location and each cross-dock hub
   - For affected legs, apply `+N_days` to the transit time based on alert severity
   - Visual: semi-transparent storm cells on the map, red pulse on affected route segments
   - Update the route card with "⚠ Weather delay: +1.4 days (storm near Memphis)"

2. **Traffic overlay** (if weather is done): HERE Traffic Flow API or OSRM congestion approximation on the current road-path lines.

Both stretch items are explicitly deferrable without affecting the core shipping checklist.

## 15. Success Criteria (summary)

Sub-Project A is DONE when:

1. The `POST /api/v1/optimize/vrp` endpoint returns four routes with **different total costs, distributor selections, and/or cross-dock decisions** (regression test for the root bug).
2. Every dollar, hour, and kilogram of CO2 in the response can be traced back to a constant defined in `costs.py`, which cites a published source.
3. The Objective Breakdown panel in CheckoutPage shows the weighted-sum math in plain view on each card, with source citations.
4. At least one strategy on the curated demo BOM selects a real cross-dock hub and shows measurable savings.
5. The map page renders the cross-dock hub layer and a consolidated route when one is selected.
6. All three pytest test files pass.
7. The Playwright E2E test passes and produces `test-screenshots/sub-project-a-demo.png`.
8. This design doc is committed, and the §9 whiteboard walkthrough is written.
9. `backend/app/api/hubs.py`, `materials.py`, and the three stale tables are gone.
10. A reader can whiteboard the math from the §9 walkthrough without consulting external references.

---

**End of design.**
