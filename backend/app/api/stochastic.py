"""
Cost-vs-CVaR efficient frontier API.

Two endpoints over `app/optimization/stochastic.py`'s two-stage stochastic program:

  POST /stochastic/frontier      Sweep the risk-aversion weight lambda for a BOM and
                                 return the (E[cost], CVaR_95[cost]) frontier, its
                                 knee, and the recommendation the knee implies.
  GET  /stochastic/calibration   The disruption probabilities themselves, per
                                 distributor, side by side with the raw betweenness
                                 they were derived from.

WHY THE CALIBRATION ENDPOINT EXISTS
-----------------------------------
The disruption probabilities are the weakest input in this whole subsystem: a cited
FIRM-level base rate reinterpreted per supplier, converted to an exposure window, and
re-shaped by a centrality rank transform. Every step is an assumption. Burying that
behind a single confident CVaR number would repeat exactly the mistake this work
exists to fix -- `graph/simulation.py` reads min-max normalized betweenness directly
as a failure probability, so its most central distributor fails in 100% of scenarios
and its `cvar_95` pins at a constant 1.15.

So the assumptions are a first-class, inspectable part of the API: `/calibration`
publishes every probability with its provenance, and `/frontier` lets the caller vary
`base_annual_prob`, `centrality_spread` and `horizon_days` and watch the knee move.
`centrality_spread = 1.0` disables centrality entirely and puts every supplier on the
flat base rate -- the "centrality tells us nothing about failure" arm.

DoS POSTURE (mirrors the T-02-03 mitigation in graph/simulation.py)
-------------------------------------------------------------------
This is a genuinely expensive endpoint -- a lambda sweep is one CP-SAT solve per point
over a scenario-expanded model. Every knob that drives cost is fixed server-side or
hard-capped: the Monte Carlo draw count and the lambda grid are NOT caller-controlled,
BOM size and per-line quantity are capped, and results are cached for an hour on a
deterministic key. The parameters a caller CAN set (base rate, spread, horizon, depot)
change the answer but not the amount of work.

Three ceilings bound the work, and they matter more than the per-solve limit alone:

  * `SOLVE_TIME_LIMIT_S`   per CP-SAT solve;
  * `SWEEP_TIME_BUDGET_S`  for the WHOLE sweep, so the worst case is one budget rather
                           than len(LAMBDA_GRID) x the per-point limit;
  * the adaptive scenario budget in `stochastic.fit_scenario_set`, which sizes the
    second stage to a variable ceiling. This is the one that actually bounds the cost:
    model size grows linearly in distinct scenarios x pool size, and it is what stops a
    55-supplier BOM from building a 29,000-variable model that no time limit rescues.

`MAX_EVALUATION_ATOMS` bounds the SCORING cost separately -- scoring is one exact
recourse solve per scenario atom per lambda, and an 18-distributor pool enumerates to
262,144 atoms.

Public, no auth -- consistent with `app/api/resilience.py`, which likewise returns
aggregate cost and risk metrics derived from the catalogue and no user data.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.cache import CacheManager
from app.core.database import get_db
from app.graph import ensure_graph_state, get_graph_state
from app.models.component import Component, DistributorOffer
from app.models.distributor import Distributor
from app.optimization import stochastic as stoch
from app.optimization.costs import haversine_km
from app.optimization.countries import _acled_country_key
from app.optimization.sourcing import BomLine, Offer
from app.optimization.strategies import get_strategy
from app.startup import wait_for_graph

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stochastic", tags=["stochastic"])

# ── Server-fixed compute budget (never caller-controlled) ────────────────────
MAX_BOM_LINES = 25
MAX_LINE_QUANTITY = 100_000
N_DRAWS = 200
SEED = 42

# The lambda grid includes 0.2 and 0.3 deliberately. The knee published in
# docs/CVAR_EFFICIENT_FRONTIER.md (archived at git tag archive/sourcing-v1) sits at lambda = 0.30, and the old six-point grid
# (0, 0.1, 0.25, 0.5, 0.75, 1.0) did not contain it -- so the endpoint could not land
# on the headline recommendation even in principle. A frontier whose published knee is
# not on its own grid is not a reproducible frontier.
LAMBDA_GRID: List[float] = [0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0]

# Per-point CP-SAT budget, raised from 5.0s. With the adaptive scenario budget below
# doing the heavy lifting, real solve times on the instances that used to fail are
# ~1-3s; the headroom is for the tail of hard instances, not the common case.
SOLVE_TIME_LIMIT_S = 12.0

# Hard ceiling on the WHOLE sweep, so a pathological BOM cannot hold a worker for
# len(LAMBDA_GRID) x SOLVE_TIME_LIMIT_S. Lambdas past the budget come back labelled
# NOT_ATTEMPTED in `unsolved_points` rather than silently missing.
SWEEP_TIME_BUDGET_S = 45.0

# Scoring on the exact enumerated support is strictly better than scoring on a sample
# -- no sampling error at all -- but it costs one recourse solve PER ATOM PER LAMBDA.
# `stochastic.MAX_ENUMERABLE_DISTRIBUTORS = 18` is the right ceiling for the offline
# script; for a request it is not. Measured: an 18-distributor pool enumerates to
# 262,144 atoms and turned a 0.1s solve into a 78s request that then blew the sweep
# budget and returned 1 of 7 points.
#
# 256 atoms = 8 distributors, x7 lambdas = 1,792 recourse solves, a few seconds worst
# case. It comfortably covers the published headline instance (6 distributors, 64
# atoms), so the archived docs/cvar_frontier.json stays exactly reproducible. Above it the endpoint
# falls back to the sampled evaluation set, which is what it always did.
MAX_EVALUATION_ATOMS = 256

CACHE_SCENARIO_TYPE = "cvar-frontier"

# Depot for freight distance. Same continental reference hub the resilience API uses
# (FedEx Memphis "WorldHub"), so freight distances are comparable across endpoints.
#
# IMPORTANT, and the reason this is now a request parameter: the offline study
# `docs/cvar_frontier.json` (and every number in docs/CVAR_EFFICIENT_FRONTIER.md), both
# archived at git tag archive/sourcing-v1 and not shipped on main, was
# produced by the offline frontier seed script, whose depot is San Francisco,
# 37.7749 / -122.4194. (Named deliberately in prose rather than by module path: the
# T-04-01 guard in the benchmark seed's test module forbids the served app from so
# much as mentioning those seed modules by name, so that nothing here can quietly grow
# an import of one.)
# The depot sets every `dist_km_from_depot`, which drives the freight model, which
# changes the optimum: on
# the headline BOM the Memphis depot gives E = $147,272 / 4 suppliers at lambda = 0,
# while the San Francisco depot gives the published $182,256 / 6 suppliers. Same BOM,
# same volume, same probabilities. So the depot is now explicit, echoed in the
# response, and settable -- otherwise the endpoint quietly answers a different question
# from the document and neither one says so.
DEPOT_LAT, DEPOT_LNG = 35.1495, -90.0490

# The depot the archived offline study was generated at. Exposed so callers can reproduce
# docs/cvar_frontier.json (git tag archive/sourcing-v1) from the live endpoint.
ARTIFACT_DEPOT_LAT, ARTIFACT_DEPOT_LNG = 37.7749, -122.4194

DEFAULT_STRATEGY = "balanced"


# ── Request / response models ────────────────────────────────────────────────

class BomItem(BaseModel):
    component_id: int = Field(..., ge=1)
    quantity: int = Field(..., ge=1, le=MAX_LINE_QUANTITY)


class FrontierRequest(BaseModel):
    items: List[BomItem] = Field(..., min_length=1, max_length=MAX_BOM_LINES)
    base_annual_prob: float = Field(
        stoch.DEFAULT_BASE_ANNUAL_PROB, gt=0.0, lt=1.0,
        description="Annual probability that a given supplier suffers a material "
                    "outage. Default 0.2368 = 1 - exp(-1/3.7), from McKinsey Global "
                    "Institute 2020 ('disruptions lasting a month or longer every 3.7 "
                    "years'). That is a FIRM-level frequency used here per supplier, "
                    "which likely overstates individual supplier risk -- vary it.",
    )
    horizon_days: int = Field(
        stoch.DEFAULT_HORIZON_DAYS, ge=1, le=365,
        description="Exposure window for one purchase order. The annual rate is "
                    "converted by 1 - (1 - p)**(days/365).",
    )
    centrality_spread: float = Field(
        stoch.DEFAULT_CENTRALITY_SPREAD, ge=1.0, le=10.0,
        description="How far betweenness rank is allowed to move a supplier's "
                    "probability around the base rate. The most central supplier gets "
                    "spread x the base rate, the least central 1/spread x, the median "
                    "exactly the base rate. 1.0 disables centrality entirely.",
    )
    us_only: bool = Field(False, description="Restrict to domestic distributors.")
    strategy: str = Field(DEFAULT_STRATEGY, description="Strategy weight profile id.")
    depot_lat: float = Field(
        DEPOT_LAT, ge=-90.0, le=90.0,
        description="Destination factory latitude. Sets every distributor's freight "
                    "distance, so it changes the optimum -- not a cosmetic parameter. "
                    "Defaults to the FedEx Memphis WorldHub reference the resilience "
                    "API uses. Pass 37.7749 / -122.4194 to reproduce the frontier "
                    "in the offline study docs/cvar_frontier.json, archived at git tag "
                    "archive/sourcing-v1.",
    )
    depot_lng: float = Field(
        DEPOT_LNG, ge=-180.0, le=180.0,
        description="Destination factory longitude. See depot_lat.",
    )


class FrontierPointOut(BaseModel):
    lambda_: float = Field(..., alias="lambda")
    expected_cost_usd: float
    cvar_95_usd: float
    var_95_usd: float
    tail_premium_usd: float
    first_stage_cost_usd: float
    expected_recourse_usd: float
    n_suppliers: int
    supplier_ids: List[int]
    solver_status: str
    mip_gap_pct: float
    solve_seconds: float
    n_variables: int
    dominated: bool

    model_config = {"populate_by_name": True}


class DistributorRisk(BaseModel):
    distributor_id: int
    distributor_name: str
    betweenness_normalized: float
    p_disruption_over_horizon: float
    legacy_simulator_p_fail: float = Field(
        ...,
        description="What graph/simulation.py would use for this distributor: the "
                    "min-max normalized betweenness itself. Published alongside so the "
                    "difference is auditable rather than asserted.",
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _graph(db: Session):
    """
    Live GraphState, built on demand when the app lifespan has not run (tests).

    Waits for the ONE background startup build (app/startup.py) rather than racing it,
    and caches whatever it does build. The previous version called `build_graph_state`
    and discarded the result — safe only while the lifespan populated the global before
    the first request could arrive, and a per-request denial of service the moment that
    build was deferred. See `app.graph.ensure_graph_state`.
    """
    wait_for_graph()
    gs = get_graph_state()
    if gs is None:
        gs = ensure_graph_state(db)
    return gs


# The ORM models in app/models/* use SQLAlchemy's legacy `Column(...)` declarative
# style with no `Mapped[...]` annotations, so the mypy plugin infers every column as
# nullable even for NOT NULL primary keys and coordinates. These two helpers narrow at
# the boundary AND add a real guard: a NULL id or coordinate would otherwise surface as
# a TypeError deep inside the freight model, which is a much worse failure than a clear
# 500 raised here naming the field.
def _req_int(value: Optional[int], field: str) -> int:
    if value is None:
        raise HTTPException(status_code=500, detail=f"{field} is unexpectedly NULL")
    return int(value)


def _req_float(value: Any, field: str) -> float:
    # `Any` rather than `Optional[float]`: SQLAlchemy types numeric columns with an
    # internal TypeVar (`_N | None`) that no concrete annotation matches. The runtime
    # narrowing below is real, and the return type is concrete, so nothing downstream
    # loses type information.
    if value is None:
        raise HTTPException(status_code=500, detail=f"{field} is unexpectedly NULL")
    return float(value)


def _load_bom_and_offers(
    db: Session,
    items: List[BomItem],
    depot_lat: float = DEPOT_LAT,
    depot_lng: float = DEPOT_LNG,
) -> tuple[List[BomLine], List[Offer]]:
    """Resolve component ids to a BOM and the full offer pool, as optimize.py does."""
    comp_ids = [i.component_id for i in items]
    components: Dict[int, Component] = {
        _req_int(c.id, "component.id"): c
        for c in db.query(Component).filter(Component.id.in_(comp_ids)).all()
    }

    bom: List[BomLine] = []
    for item in items:
        comp = components.get(item.component_id)
        if comp is None:
            raise HTTPException(
                status_code=404,
                detail=f"component_id {item.component_id} not found",
            )
        bom.append(BomLine(
            component_id=_req_int(comp.id, "component.id"),
            mpn=str(comp.mpn),
            quantity=int(item.quantity),
            category=str(comp.category) if comp.category is not None else None,
        ))

    offer_rows = db.query(DistributorOffer).filter(
        DistributorOffer.component_id.in_(comp_ids)
    ).all()
    dist_ids = {_req_int(o.distributor_id, "offer.distributor_id") for o in offer_rows}
    dist_by_id: Dict[int, Distributor] = {
        _req_int(d.id, "distributor.id"): d
        for d in (
            db.query(Distributor).filter(Distributor.id.in_(dist_ids)).all()
            if dist_ids else []
        )
    }

    offers: List[Offer] = []
    for row in offer_rows:
        dist = dist_by_id.get(_req_int(row.distributor_id, "offer.distributor_id"))
        price = row.price
        if dist is None or price is None or float(price) <= 0:
            continue
        comp = components.get(_req_int(row.component_id, "offer.component_id"))
        risk_factors = (comp.risk_factors if comp is not None else None) or []
        is_chinese = any("chinese" in str(f).lower() for f in risk_factors)
        offers.append(Offer(
            component_id=_req_int(row.component_id, "offer.component_id"),
            distributor_id=_req_int(row.distributor_id, "offer.distributor_id"),
            distributor_name=str(dist.name),
            price_usd=float(price),
            stock=int(row.stock or 0),
            moq=int(row.moq or 1),
            is_domestic=bool(dist.is_domestic),
            dist_km_from_depot=haversine_km(
                depot_lat, depot_lng,
                _req_float(dist.latitude, "distributor.latitude"),
                _req_float(dist.longitude, "distributor.longitude"),
            ),
            risk_score=float(comp.risk_score) if comp is not None and comp.risk_score is not None else 0.5,
            is_chinese_origin=is_chinese,
            distributor_country=_acled_country_key(dist.country),
        ))

    return bom, offers


def _point_out(p: stoch.FrontierPoint) -> Dict[str, Any]:
    return {
        "lambda": round(p.lam, 4),
        "expected_cost_usd": round(p.expected_cost_usd, 2),
        "cvar_95_usd": round(p.cvar_usd, 2),
        "var_95_usd": round(p.var_usd, 2),
        "tail_premium_usd": round(p.cvar_usd - p.expected_cost_usd, 2),
        "first_stage_cost_usd": round(p.first_stage_cost_usd, 2),
        "expected_recourse_usd": round(p.expected_recourse_usd, 2),
        "n_suppliers": p.n_suppliers,
        "supplier_ids": p.supplier_ids,
        "solver_status": p.status,
        "mip_gap_pct": round(p.gap_pct, 4),
        "solve_seconds": round(p.wall_seconds, 3),
        # Scoring is a separate cost from solving -- one exact recourse solve per
        # scenario atom -- and it dominated the request time before the evaluation
        # ceiling was introduced. Published so that is visible rather than inferred.
        "evaluate_seconds": round(p.evaluate_seconds, 3),
        "evaluation_kind": p.evaluation_kind,
        # What the OPTIMIZER weighted at THIS lambda. The weight denominator is chosen
        # per solve from the objective magnitude the int64 ceiling can carry, so it is
        # not constant across the grid, and neither is the mass below its resolution.
        "solve_kind": p.solve_kind,
        "n_atoms_weighted_in_solve": p.n_scenarios_weighted,
        "solve_weight_denominator": p.solve_weight_total,
        "solve_residual_mass": p.solve_residual_mass,
        "n_atoms_in_tail": p.n_atoms_in_tail,
        "n_variables": p.n_variables,
        "dominated": p.dominated,
    }


def _no_knee_recommendation(
    points: List[stoch.FrontierPoint], shape: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Say WHY there is no recommendation instead of returning null.

    Six identical frontier rows beside a `null` recommendation reads as a broken
    endpoint even when it is exactly right. It is right whenever the BOM has no
    resilience to buy -- one dominant supplier, or a volume below the point where the
    fixed per-supplier charge stops deciding everything. That is a finding, and this
    says it in words the caller can act on.
    """
    if shape.get("kind") == "flat":
        return {
            "available": False,
            "reason": "no_tradeoff_available",
            "knee_lambda": None,
            "statement": shape["statement"],
            "supplier_ids": shape.get("supplier_ids", []),
        }
    n_usable = len({
        (round(p.expected_cost_usd, 6), round(p.cvar_usd, 6))
        for p in points if not p.dominated
    })
    return {
        "available": False,
        "reason": "too_few_distinct_points",
        "knee_lambda": None,
        "statement": (
            f"A knee needs at least three distinct non-dominated points and this sweep "
            f"produced {n_usable}. The frontier does move, but not over enough distinct "
            f"plans to locate where the trade stops paying; nominating one of "
            f"{n_usable} points as 'the recommendation' would be inventing it."
        ),
        "distinct_non_dominated_points": n_usable,
    }


def _recommendation(points: List[stoch.FrontierPoint]) -> Optional[Dict[str, Any]]:
    """
    The knee, expressed as the decision it implies rather than as a coordinate.

    The two ratios are the whole point: how many dollars of tail exposure one extra
    dollar of expected cost removes BEFORE the knee, and how few it removes after.
    That contrast is the recommendation.

    Returns None when no knee exists; the endpoint replaces that with an explicit
    explanation via `_no_knee_recommendation` rather than publishing a bare null.
    """
    knee = stoch.find_knee(points)
    if knee is None:
        return None
    usable = sorted(
        [p for p in points if not p.dominated], key=lambda p: p.expected_cost_usd,
    )
    lo, hi = usable[0], usable[-1]

    d_e = knee.expected_cost_usd - lo.expected_cost_usd
    d_c = lo.cvar_usd - knee.cvar_usd
    d_e_after = hi.expected_cost_usd - knee.expected_cost_usd
    d_c_after = knee.cvar_usd - hi.cvar_usd

    return {
        "available": True,
        "knee_lambda": round(knee.lam, 4),
        "expected_cost_usd": round(knee.expected_cost_usd, 2),
        "cvar_95_usd": round(knee.cvar_usd, 2),
        "n_suppliers": knee.n_suppliers,
        "supplier_ids": knee.supplier_ids,
        "extra_expected_cost_usd": round(d_e, 2),
        "extra_expected_cost_pct": round(100.0 * d_e / lo.expected_cost_usd, 4)
        if lo.expected_cost_usd else 0.0,
        "cvar_reduction_usd": round(d_c, 2),
        "cvar_reduction_pct": round(100.0 * d_c / lo.cvar_usd, 4) if lo.cvar_usd else 0.0,
        "cvar_removed_per_dollar_spent": round(d_c / d_e, 3) if d_e > 1e-9 else None,
        "cvar_removed_per_dollar_spent_beyond_knee": round(d_c_after / d_e_after, 3)
        if d_e_after > 1e-9 else None,
        "statement": (
            f"Moving from the risk-neutral plan to lambda={knee.lam:g} costs "
            f"${d_e:,.0f} more in expectation and removes ${d_c:,.0f} of CVaR-95 "
            f"exposure. Past that point the trade stops paying."
        ),
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/frontier")
def compute_cvar_frontier(
    body: FrontierRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Sweep lambda in `min (1-lambda)*E[cost] + lambda*CVaR_95[cost]` and return the
    cost-vs-tail-risk efficient frontier, its knee, and the implied recommendation.

    The frontier replaces the flat risk surcharge `app/optimization/sourcing.py`
    applies today (`RISK_PREMIUM_RATE = 0.15` times a hand-weighted vulnerability
    score). Instead of one hard-coded risk appetite, the caller sees the whole
    price-of-resilience curve and where it stops being worth paying for.

    Caveats returned in the response, not buried here: the lambda grid is a weighted-sum
    scalarization and can only recover Pareto points on the convex hull of the
    (E, CVaR) image; and the disruption probabilities are an assumption whose
    parameters are exposed on this request precisely so they can be flexed.
    """
    try:
        weights = get_strategy(body.strategy)
    except KeyError:
        raise HTTPException(
            status_code=400, detail=f"Unknown strategy: {body.strategy}",
        ) from None

    cache_key = CacheManager.generate_key(CACHE_SCENARIO_TYPE, {
        "items": sorted((i.component_id, i.quantity) for i in body.items),
        "base_annual_prob": round(body.base_annual_prob, 6),
        "horizon_days": body.horizon_days,
        "centrality_spread": body.centrality_spread,
        "us_only": body.us_only,
        "strategy": body.strategy,
        "depot": (round(body.depot_lat, 6), round(body.depot_lng, 6)),
        "n_draws": N_DRAWS,
        "seed": SEED,
        "lambda_grid": LAMBDA_GRID,
        "time_limit_s": SOLVE_TIME_LIMIT_S,
        # Both of these change the ANSWER, not just the work, so they belong in the
        # key: entries cached before the solve set became the exact support, or while
        # CP-SAT was allowed to stop on a 0.1% tolerance, do not describe this solver.
        "solve_support": "exact-first-v2",
        "relative_gap": stoch.DEFAULT_RELATIVE_GAP,
    })
    try:
        cached = CacheManager.get(db, cache_key)
    except Exception as exc:  # noqa: BLE001 - a cache miss must never fail the request
        logger.warning("cvar-frontier cache get failed: %s", exc)
        cached = None
    if cached is not None:
        cached["cached"] = True
        return cached

    bom, offers = _load_bom_and_offers(db, body.items, body.depot_lat, body.depot_lng)
    if not offers:
        raise HTTPException(
            status_code=422,
            detail="No priced offers exist for the requested components",
        )

    gs = _graph(db)
    pool = sorted({o.distributor_id for o in offers})
    probs = stoch.build_failure_probabilities(
        pool,
        gs.betweenness,
        base_annual_prob=body.base_annual_prob,
        horizon_days=body.horizon_days,
        centrality_spread=body.centrality_spread,
    )

    # The entire 2**|D| support with exact probabilities, when the pool is small enough
    # to hold it. Disruption here is |D| independent Bernoulli variables, so this IS the
    # distribution -- not an approximation of it.
    exact: Optional[stoch.ScenarioSet] = None
    can_enumerate = (
        len(pool) <= stoch.MAX_ENUMERABLE_DISTRIBUTORS
        and 2 ** len(pool) <= MAX_EVALUATION_ATOMS
    )
    if can_enumerate:
        exact = stoch.enumerate_scenarios(probs)

    # The scenario set the PLAN IS CHOSEN ON, sized to the solver budget. The exact
    # support goes first when it fits: this endpoint used to SCORE on all 64 atoms
    # while CHOOSING on 200 draws that resolved 10 of them, so 54 atoms carried weight
    # zero in the decision and the alpha=0.95 tail the optimizer saw was 4 atoms wide
    # against an exact 49-54. The page then published "scenario support: exact, 64
    # atoms", which described the scoring and not the choosing.
    #
    # The draw ladder remains the fallback for pools too wide to enumerate. A
    # 55-supplier BOM turns 200 draws into 183 distinct scenarios and a ~29,000-variable
    # model that CP-SAT cannot even find a feasible point in -- which is precisely how
    # this endpoint came to tell six out of seven callers that their BOM had no
    # solution. See `stochastic.fit_scenario_set` for the measured table behind it.
    try:
        fit = stoch.fit_scenario_set(
            bom, offers, weights, probs,
            n_draws=N_DRAWS, seed=SEED, us_only=body.us_only,
            exact_set=exact,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scenarios = fit.scenario_set

    # The scenario set the plan is SCORED ON. Identical to the solve set whenever the
    # exact support fits the solver budget -- choice and score then read from the same
    # measure and there is no sampling error anywhere in the result.
    evaluation: Optional[stoch.ScenarioSet] = exact
    if evaluation is None and fit.thinned:
        # Too big to enumerate, but the solve set was thinned -- score on the full
        # sample so the risk numbers keep the statistical quality the thinning cost
        # the CHOICE, not the measurement.
        evaluation = stoch.sample_scenarios(probs, n_draws=N_DRAWS, seed=SEED)

    t0 = time.perf_counter()
    try:
        sweep = stoch.compute_frontier_sweep(
            bom, offers, weights, scenarios, LAMBDA_GRID,
            alpha=stoch.DEFAULT_ALPHA, us_only=body.us_only,
            time_limit_s=SOLVE_TIME_LIMIT_S,
            evaluation_set=evaluation,
            allow_partial=True,
            sweep_time_budget_s=SWEEP_TIME_BUDGET_S,
        )
    except stoch.ModelInfeasibleError as exc:
        # The ONLY case that is genuinely the caller's BOM: CP-SAT proved it.
        raise HTTPException(
            status_code=422,
            detail=f"No feasible sourcing plan exists for this BOM: {exc}",
        ) from exc
    except stoch.ModelInvalidError as exc:
        # Our bug. Never blame the request for it.
        logger.exception("cvar-frontier built an invalid model")
        raise HTTPException(
            status_code=500,
            detail=f"The sourcing model was built incorrectly: {exc}",
        ) from exc
    except ValueError as exc:
        # Infeasible input (no offers after filtering, empty BOM, bad alpha) is a
        # client-side problem; report it as one rather than a 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    wall = time.perf_counter() - t0

    points = sweep.points
    if not points:
        # Every lambda exhausted the budget. That is a statement about OUR solver, so
        # it is a 503 with the numbers that would let someone act on it -- never a 422
        # asserting the caller's BOM is infeasible.
        worst = sweep.unsolved[0] if sweep.unsolved else None
        raise HTTPException(
            status_code=503,
            detail={
                "error": "solver_budget_exhausted",
                "message": (
                    f"Solver budget exhausted at {scenarios.n_distinct} scenarios and a "
                    f"{SOLVE_TIME_LIMIT_S:g}s per-point limit: no lambda in the sweep "
                    f"produced a plan within budget. This is a limit on this service's "
                    f"search budget, NOT a finding that the BOM is infeasible -- a "
                    f"sourcing plan may well exist."
                ),
                "n_scenarios": scenarios.n_distinct,
                "n_draws_used": fit.n_draws_used,
                "second_stage_variables": fit.recourse_variables,
                "n_distributors_in_pool": len(pool),
                "time_limit_s_per_point": SOLVE_TIME_LIMIT_S,
                "sweep_time_budget_s": SWEEP_TIME_BUDGET_S,
                "solver_status": worst.solver_status if worst else "UNKNOWN",
                "suggestion": (
                    "Reduce the BOM to fewer lines, or narrow the supplier pool with "
                    "us_only=true; both shrink the second stage, which is what drives "
                    "the solve time."
                ),
            },
        )

    shape = stoch.frontier_shape(points)
    recommendation = _recommendation(points) or _no_knee_recommendation(points, shape)

    # The measure the published risk statistics were actually taken from.
    measure = evaluation if evaluation is not None else scenarios

    # The integer weight denominator is chosen per solve from the objective magnitude
    # the int64 ceiling can carry, so it varies across the lambda grid. Report the
    # WORST point on each axis rather than a flattering one.
    solve_weight_total = min(p.solve_weight_total for p in points)
    solve_residual_mass = max(p.solve_residual_mass for p in points)
    solve_atoms_weighted = min(p.n_scenarios_weighted for p in points)

    result: Dict[str, Any] = {
        "cached": False,
        "frontier": [_point_out(p) for p in points],
        "partial": not sweep.complete,
        "unsolved_points": [
            {
                "lambda": round(u.lam, 4),
                "reason": u.reason,
                "solver_status": u.solver_status,
                "detail": u.detail,
                "time_limit_s": round(u.time_limit_s, 3),
                "n_scenarios": u.n_scenarios,
            }
            for u in sweep.unsolved
        ],
        "frontier_shape": shape,
        "recommendation": recommendation,
        "instance": {
            "depot_lat": body.depot_lat,
            "depot_lng": body.depot_lng,
            "depot_note": (
                "Freight distance, and therefore the optimum, is measured from this "
                "point. The offline study docs/cvar_frontier.json (archived at git tag "
                "archive/sourcing-v1) was generated at "
                f"{ARTIFACT_DEPOT_LAT} / {ARTIFACT_DEPOT_LNG} (San Francisco); pass "
                "those to reproduce its numbers exactly."
                if (body.depot_lat, body.depot_lng) != (ARTIFACT_DEPOT_LAT, ARTIFACT_DEPOT_LNG)
                else "This is the depot the offline study docs/cvar_frontier.json "
                     "(archived at git tag archive/sourcing-v1) was generated at."
            ),
            "total_units": sum(i.quantity for i in body.items),
            "n_lines": len(body.items),
            "strategy": body.strategy,
            "us_only": body.us_only,
        },
        "calibration": {
            "base_annual_prob": round(body.base_annual_prob, 6),
            "horizon_days": body.horizon_days,
            "centrality_spread": body.centrality_spread,
            "p_disruption_min": round(min(probs.values()), 5) if probs else 0.0,
            "p_disruption_median": round(
                sorted(probs.values())[len(probs) // 2], 5) if probs else 0.0,
            "p_disruption_max": round(max(probs.values()), 5) if probs else 0.0,
            "n_distributors_in_pool": len(pool),
        },
        "scenarios": {
            # These two describe the MEASURE the published risk numbers were taken
            # from, and are read off that object rather than off whatever set happened
            # to be solved on. They used to be read off the 200-draw sample and printed
            # three rows under a label advertising exact enumeration: 0.690 against an
            # exact 0.70053, and 0.335 against an exact 0.34036.
            "kind": measure.kind,
            "n_draws": measure.n_draws,
            "n_distinct": measure.n_distinct,
            "seed": measure.seed,
            "p_no_disruption": round(measure.p_no_disruption, 5),
            "mean_failures_per_scenario": round(measure.mean_failures_per_scenario, 5),
            "solve_set": {
                # What the OPTIMIZER saw, which is a different question from what the
                # scores were measured on and is answered separately for that reason.
                "kind": fit.kind,
                "exact": fit.exact,
                "n_draws": fit.n_draws_used,
                "n_distinct": fit.n_distinct,
                "n_atoms_weighted": solve_atoms_weighted,
                "weight_denominator": solve_weight_total,
                "residual_mass": solve_residual_mass,
                "second_stage_variables": fit.recourse_variables,
                "variable_budget": fit.max_recourse_variables,
                "thinned": fit.thinned,
                "note": fit.note,
            },
            "evaluation_set": {
                "kind": evaluation.kind if evaluation is not None else scenarios.kind,
                "n_atoms": (
                    evaluation.n_distinct if evaluation is not None
                    else scenarios.n_distinct
                ),
                # 2**55 is a 17-digit number and 2**92 does not fit an int64, so the
                # exponent is always reported and the value only when it is readable.
                "support_size_log2": len(pool),
                "support_size": 2 ** len(pool) if len(pool) <= 32 else None,
                "note": (
                    (
                        "Expected cost and CVaR are scored on the ENTIRE 2**|D| support "
                        "with exact probabilities, and the plan is CHOSEN on that same "
                        "support with exact probability weights, so there is no sampling "
                        "error anywhere in this result and no SAA optimality gap to "
                        "bound."
                        if fit.exact else
                        "Expected cost and CVaR are scored on the ENTIRE 2**|D| support "
                        "with exact probabilities, so they carry no sampling error. The "
                        "PLAN was chosen on a sample of that support -- see "
                        "scenarios.solve_set."
                    )
                    if evaluation is not None and evaluation.kind == "exact"
                    else (
                        f"The support has 2**{len(pool)} atoms, above the "
                        f"{MAX_EVALUATION_ATOMS}-atom ceiling for exact scoring on a "
                        "request, so expected cost and CVaR are scored on the sampled "
                        "set. The residual sampling error is what saa_optimality_gap "
                        "bounds."
                    )
                ),
            },
        },
        "solver": {
            "engine": "OR-Tools CP-SAT",
            "num_search_workers": 1,
            "max_time_in_seconds_per_point": SOLVE_TIME_LIMIT_S,
            "sweep_time_budget_s": SWEEP_TIME_BUDGET_S,
            "sweep_wall_seconds": round(wall, 3),
            "worst_mip_gap_pct": round(max(p.gap_pct for p in points), 4),
            "any_point_hit_time_limit": any(p.status != "OPTIMAL" for p in points),
            "points_requested": len(LAMBDA_GRID),
            "points_solved": len(points),
            "points_unsolved": len(sweep.unsolved),
        },
        "caveats": [
            "The disruption probabilities are an ASSUMPTION, not a measurement. The "
            "base rate is a firm-level frequency (McKinsey Global Institute 2020) used "
            "here per supplier, which likely overstates individual supplier risk. Vary "
            "base_annual_prob, horizon_days and centrality_spread and re-read the knee.",
            "centrality_spread=1.0 removes centrality from the model entirely and puts "
            "every supplier on the flat base rate. If the recommendation changes a lot "
            "between spread=1 and spread=3, it is being driven by the centrality "
            "assumption rather than by the cost data.",
            "A lambda sweep is a weighted-sum scalarization: it recovers only Pareto "
            "points on the convex hull of the (E, CVaR) image, so this frontier is a "
            "subset of the true efficient set, never a superset.",
            "Supplier failures are drawn independently. Real disruptions are "
            "correlated, so the tail reported here is if anything optimistic.",
            "Expected cost and CVaR are computed by re-solving each scenario's second "
            "stage exactly for the returned plan, so they describe the plan even when a "
            "point did not close its MIP gap. Check solver.any_point_hit_time_limit.",
            "Freight distance is measured from instance.depot_lat/depot_lng, and the "
            "depot changes the optimum, not just the freight line. The frontier in the "
            "offline study docs/cvar_frontier.json (archived at git tag "
            "archive/sourcing-v1) was generated at the San Francisco depot "
            f"({ARTIFACT_DEPOT_LAT} / {ARTIFACT_DEPOT_LNG}); this endpoint defaults to "
            f"the Memphis reference hub ({DEPOT_LAT} / {DEPOT_LNG}). Pass depot_lat and "
            "depot_lng to compare like with like.",
        ],
    }
    if fit.exact:
        result["caveats"].append(
            "The plan is CHOSEN on the complete "
            f"{fit.n_distinct}-atom support, not on a sample of it: CP-SAT's integer "
            "objective weights are the exact probabilities scaled by a common "
            f"denominator (worst point on this sweep: {solve_weight_total:,}), not "
            "Monte Carlo draw counts. That is a quantization, not a sample -- it has no "
            "confidence interval and does not shrink with more draws. Atoms whose "
            "probability falls below the resolution carry no weight; that mass is "
            f"{solve_residual_mass:.2e} at the worst point on this sweep and is "
            "reported per point as solve_residual_mass rather than assumed away."
        )
    if fit.thinned:
        result["caveats"].append(
            "The plan was CHOSEN on a thinned scenario sub-sample to fit the solver "
            f"budget ({fit.n_draws_used} of {N_DRAWS} draws). Expected cost and CVaR "
            "are still scored on the full evaluation set, so the risk numbers are "
            "unaffected; what grows is the SAA error in the CHOICE of plan. See "
            "scenarios.solve_set.note."
        )
    if not sweep.complete:
        result["caveats"].append(
            f"PARTIAL FRONTIER: {len(points)} of {len(LAMBDA_GRID)} lambda points "
            "solved within budget. The unsolved points are listed in unsolved_points "
            "with their solver status. They are missing because this service's search "
            "budget ran out, NOT because no plan exists at those risk appetites. Note "
            "the sweep runs DESCENDING in lambda (the pure-CVaR end is much easier for "
            "CP-SAT and warm-starts its neighbour), so it is the RISK-NEUTRAL end that "
            "is lost first -- read any knee against a truncated baseline with that in "
            "mind."
        )

    try:
        CacheManager.set(db, cache_key, CACHE_SCENARIO_TYPE, result)
    except Exception as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("cvar-frontier cache set failed: %s", exc)

    return result


@router.get("/calibration")
def get_disruption_calibration(
    base_annual_prob: float = stoch.DEFAULT_BASE_ANNUAL_PROB,
    horizon_days: int = stoch.DEFAULT_HORIZON_DAYS,
    centrality_spread: float = stoch.DEFAULT_CENTRALITY_SPREAD,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Every disruption probability the stochastic program uses, with its provenance --
    and, next to each one, the RETIRED quantity it replaced.

    This endpoint exists to make the weakest assumption in the subsystem the easiest
    thing to inspect. The `legacy_simulator_p_fail` column is the min-max normalized
    betweenness that `graph/simulation.py` USED TO feed straight into a Bernoulli draw:
    it reaches exactly 1.0 for the most central distributor, so that distributor was
    modelled as down in 100% of scenarios.

    That is history, not current behaviour. `graph/simulation.py` now reads the
    calibrated `GraphState.p_disruption` produced by `build_failure_probabilities`
    (the same path this endpoint documents), and when a caller supplies a graph with
    an empty `p_disruption` it derives the calibrated probabilities on the spot rather
    than reverting to betweenness. The column is kept because showing what a number
    replaced is the clearest way to explain why the replacement exists -- but it is
    labelled `legacy_` precisely because nothing reads it any more.

    This docstring previously described the retired path in the present tense, which
    told a reader of the API that a defect the code had already fixed was still live.
    """
    if not 0.0 < base_annual_prob < 1.0:
        raise HTTPException(status_code=400, detail="base_annual_prob must be in (0, 1)")
    if not 1 <= horizon_days <= 365:
        raise HTTPException(status_code=400, detail="horizon_days must be in [1, 365]")
    if not 1.0 <= centrality_spread <= 10.0:
        raise HTTPException(status_code=400, detail="centrality_spread must be in [1, 10]")

    gs = _graph(db)
    dist_ids = sorted(gs.betweenness)
    names: Dict[int, str] = {
        _req_int(d.id, "distributor.id"): str(d.name)
        for d in db.query(Distributor).filter(Distributor.id.in_(dist_ids)).all()
    }
    probs = stoch.build_failure_probabilities(
        dist_ids, gs.betweenness,
        base_annual_prob=base_annual_prob,
        horizon_days=horizon_days,
        centrality_spread=centrality_spread,
    )
    legacy_max = max(gs.betweenness.values()) if gs.betweenness else 0.0

    return {
        "method": (
            "p_d = min(base_horizon_prob * spread**(2*rank_d - 1), max_failure_prob), "
            "where rank_d is the percentile rank of distributor d's betweenness (ties "
            "share the mean rank) and base_horizon_prob = "
            "1 - (1 - base_annual_prob)**(horizon_days/365)."
        ),
        "parameters": {
            "base_annual_prob": round(base_annual_prob, 6),
            "horizon_days": horizon_days,
            "centrality_spread": centrality_spread,
            "base_horizon_prob": round(
                stoch.annual_to_horizon_prob(base_annual_prob, horizon_days), 6),
            "max_failure_prob": stoch.MAX_FAILURE_PROB,
        },
        "base_rate_source": {
            "citation": "McKinsey Global Institute, 'Risk, resilience, and rebalancing "
                        "in global value chains', August 2020",
            "quote": "companies can now expect supply chain disruptions lasting a month "
                     "or longer to occur every 3.7 years",
            "derivation": "Poisson rate 1/3.7 per year -> 1 - exp(-1/3.7) = 0.2368",
            "known_weakness": "Firm-level, not per-supplier. Used per supplier here, "
                              "which likely overstates individual supplier risk. Treat "
                              "as an assumption and vary it.",
        },
        "contrast_with_existing_simulator": {
            "what_it_does": "graph/simulation.py:155-161 uses min-max normalized "
                            "betweenness DIRECTLY as p_fail.",
            "why_that_breaks": "A min-max normalization attains 1.0 at its maximum, so "
                               "the most central distributor fails in 100% of scenarios "
                               "and distributors at betweenness 0.0 never fail. There is "
                               "no base rate, no exposure window and no unit in that "
                               "expression.",
            "max_legacy_p_fail": round(legacy_max, 6),
            "max_calibrated_p_fail": round(max(probs.values()), 6) if probs else 0.0,
            "note": "graph/simulation.py is deliberately left unchanged; other published "
                    "documents depend on its numbers. This is the replacement used by "
                    "the stochastic program, published beside it.",
        },
        "distributors": [
            DistributorRisk(
                distributor_id=did,
                distributor_name=names.get(did, f"distributor-{did}"),
                betweenness_normalized=round(gs.betweenness.get(did, 0.0), 6),
                p_disruption_over_horizon=round(probs[did], 6),
                legacy_simulator_p_fail=round(gs.betweenness.get(did, 0.0), 6),
            ).model_dump()
            for did in dist_ids
        ],
    }
