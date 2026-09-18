"""
Newsvendor inventory-decision API -- the demand distribution turned into an order.

Three endpoints over `app/optimization/newsvendor.py`:

  GET  /newsvendor/assumptions   The two costs and the critical fractile they imply,
                                 with the provenance of every input. Nothing else.
  POST /newsvendor/decision      One order quantity for one demand history, with its
                                 expected cost decomposition and what the naive rules
                                 would have ordered instead.
  GET  /newsvendor/evaluation    The policy scored against every baseline on 2,646
                                 held-out car-parts series, with paired bootstrap CIs.

WHY THE ASSUMPTIONS ENDPOINT EXISTS
------------------------------------
The weakest input in this subsystem is not
the forecast, it is the COST ASYMMETRY: an expedite premium and a holding rate, each cited
but each an industry average rather than a measurement of any part this app sells. The
entire answer is a monotone function of their ratio. Burying that behind a confident order
quantity would be the same mistake the CVaR work was built to fix, so the ratio is a
first-class, inspectable resource and every knob that moves it is a request parameter.

WHAT DRIVES THE DECISION, STATED ONCE HERE AND AGAIN IN EVERY RESPONSE
----------------------------------------------------------------------
The DEMAND predictive distribution from `app/ml/intermittent.py` -- the compound
Bernoulli x zero-truncated NegBin law that `GET /demand/benchmark` already scores under
CRPS and the pinball loss. Not the lead-time model, and not a per-part forecast for the
electronic components in this catalogue: none exists, `docs/INTERMITTENT_DEMAND.md`
explains why the synthetic one was deleted, and this endpoint does not bring it back under
a new name. The panel is Monash car parts, real intermittent spare-parts demand used as a
labelled stand-in.

DoS POSTURE
-----------
`/decision` is closed-form -- one smoothing pass and one cdf lookup -- and its only
unbounded input, the demand history, is length-capped. `/evaluation` is the expensive one,
and the cost stated here used to be a DEV-MACHINE number that production contradicted:

    "~4 s over 2,674 series x 3 origins x 6 methods"

3.4 s is what this evaluation takes on an Apple-silicon laptop. It is NOT what it takes on
the deployed instance. Measured against https://supply-chain-api-qy8x.onrender.com on
2026-08-30:

    GET /evaluation                        wall_seconds 259.897   (cold container)
    GET /evaluation?forecast_method=croston  wall_seconds 106.589 (warm container, cold cache)

`render.yaml` starts ONE uvicorn worker on a 0.5-CPU free instance, so a 107-second
CPU-bound request does not merely make itself slow -- it is the only thing that process is
doing for those 107 seconds. Abandoning the request does not stop the computation either,
so a client timeout leaves the server still burning the CPU it no longer has a reader for.

WHAT THAT MEANS FOR THE PARAMETER SPACE. The space is small and fully enumerable, and
`/evaluation` takes NO unit price -- the critical fractile does not depend on price and the
dollars scale linearly -- so a caller cannot force a recomputation by perturbing a float.
That was always a real property. What was NOT true is the conclusion drawn from it: a whole
enumerable space behind a 32-entry LRU at ~107 s per miss is CPU-hours an anonymous caller
can reach by changing a dropdown, and calling that "not a denial-of-service surface" was an
inference from the wrong latency. Publishing four of those configurations and leaving the
rest to recompute did not close it either: an ordinary click still stalled the only worker
for nearly two minutes.

THE SPACE IS 72, NOT 144. Two earlier drafts of this comment said "6 methods x 12 review
periods x 2 shortage modes = 144". `run_panel_evaluation` scores decisions inside the
held-out horizon, splitting it into floor(horizon / L) non-overlapping blocks, and raises
`ValueError` when that count is zero. `PANEL_HORIZON` is 6, so review periods 7..12 never
returned an evaluation -- they returned an unhandled traceback and a 500, while
`MAX_REVIEW_PERIOD_MONTHS = 12` advertised them as valid and the UI offered "12 months" in a
dropdown. `EVALUATION_MAX_REVIEW_PERIOD_MONTHS` now derives the bound from `PANEL_HORIZON`
itself, so the 72 that exist are published and the 72 that do not are a 422 rather than a
crash. That halving is a correction to the count, not a reduction in what is computed.

SO NOTHING IS COMPUTED AT REQUEST TIME AT ALL. `docs/newsvendor.json` holds the finished
evaluation for every one of the 72 reachable configurations -- the four SERVABLE named runs
Section 3.4 quotes (the fifth, `negative_control_permuted`, answers no request and is never
indexed) plus a `grid` of the remaining 68 -- and `_artifact_evaluation` below serves those
blocks directly (a committed-artifact pattern). The served numbers are not an approximation of the computation:
`tests/test_newsvendor_evaluation_is_served_from_the_artifact.py` asserts every reachable
configuration is served and re-runs `run_panel_evaluation` the slow way on a sample of them,
comparing every leaf; `tests/test_artifacts_pinned_to_code.py` pins the primary block to
1e-9. The recompute path is KEPT and still tested, because it is what happens when the
artifact is absent (the local `backend/Dockerfile` has build context `backend/`, which loses
`../docs`), unreadable, or does not identify itself as this code's own output -- different
bootstrap count, different protocol constants, a different panel checksum. It can be slow,
or it can be wrong, and it is never wrong.

Public, no auth -- consistent with `/demand/benchmark`, which likewise
serve aggregate model results derived from committed data and no user data.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.ml.proper_scoring import DEFAULT_QUANTILE_LEVELS
from app.optimization import newsvendor as nv

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/newsvendor", tags=["newsvendor"])

# ── Server-fixed bounds (never caller-controlled beyond these) ───────────────

#: Longest demand history accepted. 600 monthly observations is 50 years; anything longer
#: is not a spare-parts history, and the smoothing recursions are O(n) so the cap is about
#: bounding request size rather than compute.
MAX_HISTORY = 600

#: Shortest history that can support a forecast at all. Croston needs at least one
#: non-zero observation and an inter-arrival interval; TSB needs a probability to smooth.
#: Below a year of monthly data none of them are saying anything.
MIN_HISTORY = 12

#: Largest single demand observation accepted, so a caller cannot allocate a huge
#: `climatology_dist` support (its length is max(train) + 1).
MAX_OBSERVATION = 100_000

#: Longest review period. Past a year the single-period newsvendor abstraction -- no
#: carry-over, no reorder -- has stopped describing anything real. This bounds `/decision`,
#: which is a closed-form order quantity for one history and has no held-out horizon to fit
#: inside.
MAX_REVIEW_PERIOD_MONTHS = 12

#: Longest review period `/evaluation` accepts, which is NOT the same number and never was.
#: `run_panel_evaluation` scores decisions on the held-out horizon, splitting it into
#: floor(horizon / L) non-overlapping blocks, and raises `ValueError` the moment that is
#: zero. With `PANEL_HORIZON == 6`, L in 7..12 produced no evaluation at all -- it produced
#: an unhandled ValueError and a 500, on values the query bound advertised as valid and the
#: UI offered in a dropdown. Derived from the protocol constant rather than restated, so it
#: cannot drift from the horizon it is a property of. `seeds/run_newsvendor.py::
#: MAX_REVIEW_PERIOD_MONTHS` publishes exactly this range.
EVALUATION_MAX_REVIEW_PERIOD_MONTHS = nv.PANEL_HORIZON

#: How many distinct `/evaluation` configurations stay warm IN THIS PROCESS. The whole
#: reachable space is 6 methods x 6 review periods x 2 shortage modes = 72, and ALL 72 are
#: published in `docs/newsvendor.json` and served from it, so on a healthy deployment
#: nothing ever enters this cache. It exists for the degraded case only: an artifact that is
#: missing, unreadable, or does not identify itself as this code's output falls back to
#: computing, at ~107 s of a 0.5-CPU worker per miss, and then at least the second caller
#: does not pay it again.
EVALUATION_CACHE_SIZE = 32

#: Bootstrap replications for the served evaluation. 5,000 is what
#: `app/ml/regime_model.py::_paired_brier` uses; matched so the two CIs are comparable.
#: `seeds/run_newsvendor.py::N_BOOT` MUST agree with this or the artifact is not served --
#: see `_artifact_index`, which checks it rather than assuming it.
EVALUATION_N_BOOT = 5000

#: Bootstrap seed for the served evaluation. Same agreement requirement as N_BOOT.
EVALUATION_SEED = 0


# ── The committed evaluation artifact ────────────────────────────────────────
#
# Repo root: app/api/newsvendor.py -> app -> backend -> <repo>. `../docs` resolves on
# the deployed instance because `render.yaml` uses `runtime: python` with
# `rootDir: backend`, which sets the working directory but still checks out the whole
# repository, so `../docs` is on disk.
#
# If it is NOT on disk (the local `backend/Dockerfile` has build context `backend/`, so a
# container built that way loses it) nothing breaks and nothing is faked: `_artifact_index`
# returns an empty index and every request recomputes exactly as it did before.
_REPO_ROOT = Path(__file__).resolve().parents[3]
EVALUATION_ARTIFACT_PATH = _REPO_ROOT / "docs" / "newsvendor.json"

#: Blocks that are not an answer to any request this endpoint can receive. The permuted
#: negative control is scored against ANOTHER series' forecast -- `/evaluation` has no
#: parameter that asks for it, and serving it for a real request would be a fabrication.
_ARTIFACT_NON_CONFIG_KEYS = frozenset({"provenance", "meta"})

#: The artifact's exhaustive sweep lives one level down, under this key, so the five named
#: runs stay readable at the top of the file and `test_newsvendor_docs_match_artifact.py`
#: keeps reading them by name. Matches `seeds/run_newsvendor.py::GRID_KEY`.
_ARTIFACT_GRID_KEY = "grid"


def _candidate_blocks(raw: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Every (name, block) in the artifact that might answer a request.

    Flattens the named runs and the `grid` into ONE list so that both go through the same
    self-identification checks below and share one duplicate guard. A grid entry that
    repeated a named configuration would otherwise be indexed as a second, unchallenged
    claim about a configuration that is already published.
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    for name, block in raw.items():
        if name in _ARTIFACT_NON_CONFIG_KEYS:
            continue
        if name == _ARTIFACT_GRID_KEY:
            if isinstance(block, dict):
                out += [
                    (f"{_ARTIFACT_GRID_KEY}.{sub}", body)
                    for sub, body in block.items()
                    if isinstance(body, dict)
                ]
            continue
        if isinstance(block, dict):
            out.append((name, block))
    return out


def _panel_sha256() -> Optional[str]:
    """SHA-256 of the panel this process would actually evaluate, or None if absent."""
    try:
        return hashlib.sha256(nv.PANEL_PATH.read_bytes()).hexdigest()
    except OSError:
        return None


@lru_cache(maxsize=1)
def _artifact_index(mtime_key: float, path_str: str) -> Dict[Tuple[str, int, str], Dict[str, Any]]:
    """Map (forecast_method, review_period_months, shortage_mode) -> a servable block.

    KEYED ON mtime SO REGENERATION INVALIDATES. Same shape as
    `app/api/demand.py::_load`: a long-lived process that had the old artifact parsed must
    not keep serving it after `seeds.run_newsvendor` rewrites the file underneath it.

    THE MAPPING IS DERIVED, NOT DECLARED. Every block carries the configuration that
    produced it -- `protocol.forecast_method`, `protocol.review_period_months`,
    `costs.shortage_mode` -- so this reads the key off the block instead of hard-coding
    "primary means tsb/1/expedite". A hand-written table is exactly the kind of second
    document that agrees with the first while both disagree with the code, and it would
    fail silently: a swapped entry serves a real evaluation of the WRONG configuration,
    which no schema check can catch.

    A BLOCK IS ONLY INDEXED IF IT IDENTIFIES ITSELF AS THIS CODE'S OWN OUTPUT. The
    published numbers must be the numbers `run_panel_evaluation` produces here, today, so
    every input that is not a request parameter is checked against the constant this
    process would have used: bootstrap replications and seed, the rolling-origin protocol,
    the unit price the dollars are quoted at, and the SHA-256 of the demand panel itself.
    Anything that disagrees is dropped from the index with a warning and that configuration
    recomputes. Slow is a bug; stale is a lie.
    """
    path = Path(path_str)
    if not path.is_file():
        logger.warning("newsvendor evaluation artifact not found at %s -- every request will recompute", path)
        return {}
    try:
        raw: Dict[str, Any] = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 -- a bad artifact must degrade, never 500
        logger.warning("newsvendor evaluation artifact unreadable (%s) -- every request will recompute", exc)
        return {}

    meta = raw.get("meta") or {}
    if meta.get("n_boot") != EVALUATION_N_BOOT or meta.get("bootstrap_seed") != EVALUATION_SEED:
        logger.warning(
            "newsvendor artifact describes n_boot=%r seed=%r but this code serves n_boot=%r seed=%r "
            "-- artifact not served, every request will recompute",
            meta.get("n_boot"), meta.get("bootstrap_seed"), EVALUATION_N_BOOT, EVALUATION_SEED,
        )
        return {}

    recorded_panel = ((raw.get("provenance") or {}).get("inputs") or {}).get("car_parts_panel") or {}
    actual_panel_sha = _panel_sha256()
    if actual_panel_sha is None or recorded_panel.get("sha256") != actual_panel_sha:
        logger.warning(
            "newsvendor artifact was generated from panel sha256=%r but this deployment carries %r "
            "-- artifact not served, every request will recompute",
            recorded_panel.get("sha256"), actual_panel_sha,
        )
        return {}

    index: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for name, block in _candidate_blocks(raw):
        protocol = block.get("protocol")
        costs = block.get("costs")
        if not isinstance(protocol, dict) or not isinstance(costs, dict):
            continue

        method = protocol.get("forecast_method")
        review = protocol.get("review_period_months")
        mode = costs.get("shortage_mode")
        if not isinstance(method, str) or not isinstance(review, int) or not isinstance(mode, str):
            continue
        if method not in nv.DIST_BUILDERS or mode not in nv.SHORTAGE_MODES:
            continue
        if not (1 <= review <= EVALUATION_MAX_REVIEW_PERIOD_MONTHS):
            continue

        # Everything below is an input the CALLER cannot set, so the artifact must have
        # used the value this process would have used, or it is answering a different
        # question from the one that was asked.
        mismatch = {
            k: (got, want)
            for k, got, want in (
                ("permutation_control", protocol.get("permutation_control"), False),
                ("horizon_months", protocol.get("horizon_months"), nv.PANEL_HORIZON),
                ("n_origins", protocol.get("n_origins"), nv.PANEL_N_WINDOWS),
                ("seasonality", protocol.get("seasonality"), nv.PANEL_SEASONALITY),
                ("distribution_source", protocol.get("distribution_source"), nv.DIST_BUILDERS[method][1]),
                ("unit_price_usd", costs.get("unit_price_usd"), 1.0),
            )
            if got != want
        }
        if mismatch:
            logger.info("newsvendor artifact block %r not servable: %s", name, mismatch)
            continue

        key = (method, review, mode)
        if key in index:
            # Two blocks claiming the same configuration means the generator changed shape.
            # Serving either is a coin flip, so serve neither.
            logger.warning("newsvendor artifact has two blocks for %r -- neither is served", key)
            index[key] = {}
            continue
        index[key] = {"block_name": name, "block": block, "provenance": raw.get("provenance") or {}, "meta": meta}

    servable = sum(1 for v in index.values() if v)
    reachable = len(nv.DIST_BUILDERS) * EVALUATION_MAX_REVIEW_PERIOD_MONTHS * len(nv.SHORTAGE_MODES)
    if servable < reachable:
        # Not an error -- the endpoint still answers, just slowly for the gap. But the gap
        # is the denial-of-service surface this artifact exists to close, so it is stated
        # at WARNING rather than left to be inferred from a count.
        logger.warning(
            "newsvendor artifact serves %d of %d reachable configurations -- the other %d "
            "will recompute at ~107 s of the single worker each",
            servable, reachable, reachable - servable,
        )
    else:
        logger.info("newsvendor evaluation artifact indexed: all %d reachable configurations", servable)
    return index


def _artifact_evaluation(
    forecast_method: str, review_period_months: int, shortage_mode: str
) -> Optional[Dict[str, Any]]:
    """The committed evaluation for this configuration, or None if it must be recomputed.

    Returns `{"block": <fresh deep copy>, "block_name": str, "provenance": dict}`.
    """
    try:
        mtime = EVALUATION_ARTIFACT_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    entry = _artifact_index(mtime, str(EVALUATION_ARTIFACT_PATH)).get(
        (forecast_method, review_period_months, shortage_mode)
    )
    if not entry:
        return None
    # Deep copy: the caller adds `units`, `reproduce`, `computation` and its own
    # `wall_seconds`, and the index is process-lifetime state. A shallow copy would let one
    # request's additions leak into the next one's payload.
    return {
        "block": copy.deepcopy(entry["block"]),
        "block_name": entry["block_name"],
        "provenance": entry["provenance"],
    }


class DecisionRequest(BaseModel):
    """One stocking decision. Either bring your own history or name a panel series."""

    demand_history: Optional[List[float]] = Field(
        default=None,
        description=(
            "Observed demand per period, oldest first, as non-negative counts. This is the "
            "training window: the predictive distribution is fitted to it and nothing else. "
            f"Between {MIN_HISTORY} and {MAX_HISTORY} observations."
        ),
    )
    series: Optional[str] = Field(
        default=None,
        description=(
            "Instead of a history, the id of a series in the committed Monash car-parts "
            "panel (e.g. 'T2674'). Real intermittent spare-parts demand, used as a labelled "
            "stand-in for electronic components -- there is no public per-SKU demand series "
            "for the parts in this catalogue."
        ),
    )
    train_periods: Optional[int] = Field(
        default=None,
        ge=MIN_HISTORY,
        le=MAX_HISTORY,
        description=(
            "When `series` is used, fit on only the first N months of it. Defaults to the "
            "whole series. Set it to 33, 39 or 45 to reproduce a rolling origin of the "
            "published backtest."
        ),
    )
    method: str = Field(
        default=nv.DEFAULT_METHOD,
        description=(
            "Which predictive distribution to decide on. 'tsb' / 'sba' / 'croston' are the "
            "parametric compound-Bernoulli laws; 'climatology' is the empirical in-sample "
            "distribution; 'naive_last' and 'zero' are degenerate point forecasts with no "
            "spread, on the list only because they are on the demand leaderboard."
        ),
    )
    unit_price_usd: float = Field(
        default=1.0,
        gt=0.0,
        le=1_000_000.0,
        description=(
            "Price of one unit. Both costs are proportional to it, so it scales every dollar "
            "figure linearly and cancels out of the critical fractile entirely. The default "
            "of $1.00 makes the response read as 'per dollar of unit price'."
        ),
    )
    review_period_months: int = Field(
        default=1,
        ge=1,
        le=MAX_REVIEW_PERIOD_MONTHS,
        description=(
            "How long the order has to cover. >1 aggregates the monthly predictive law by "
            "exact convolution, which is exact only under the model's i.i.d.-across-periods "
            "assumption, and lengthens the holding charge."
        ),
    )
    shortage_mode: str = Field(
        default="expedite",
        description=(
            "How a shortage is priced. 'expedite' (default) = 0.15 x unit price, the "
            "emergency-reprocurement premium: a spare part that is out of stock is "
            "re-procured, not lost. 'line_down' = 3.0 x unit price after Snyder & Daskin "
            "(2005), for a single-sourced part with no substitute -- a SENSITIVITY whose "
            "0.993 fractile is past what 45 monthly observations can resolve."
        ),
    )
    expedite_freight_usd_per_unit: float = Field(
        default=0.0,
        ge=0.0,
        le=10_000.0,
        description=(
            "Optional variable air-freight uplift per expedited unit ($0.25 at this repo's "
            "IATA-cited rate x 0.05 kg/unit). Defaults to 0.0, which understates Cu -- the "
            "fixed $150 consignment charge is per shipment and cannot be made per-unit."
        ),
    )


def _validate_history(values: List[float]) -> np.ndarray:
    if len(values) < MIN_HISTORY or len(values) > MAX_HISTORY:
        raise HTTPException(
            status_code=422,
            detail=f"demand_history must have between {MIN_HISTORY} and {MAX_HISTORY} observations, got {len(values)}",
        )
    arr = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(arr)):
        raise HTTPException(status_code=422, detail="demand_history contains a non-finite value")
    if np.any(arr < 0):
        raise HTTPException(status_code=422, detail="demand is a count; demand_history cannot contain a negative value")
    if np.any(arr > MAX_OBSERVATION):
        raise HTTPException(
            status_code=422, detail=f"demand_history contains an observation above {MAX_OBSERVATION}"
        )
    return arr


def _resolve_series(series_id: str, train_periods: Optional[int]) -> np.ndarray:
    try:
        names, mat = nv.load_panel()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "The Monash car-parts panel is not present in this deployment, so a series "
                "cannot be resolved. Send `demand_history` instead."
            ),
        ) from exc
    lookup = {n: i for i, n in enumerate(names)}
    if series_id not in lookup:
        raise HTTPException(
            status_code=404,
            detail=f"unknown series {series_id!r}; the panel holds {len(names)} series named T1..T{len(names)}",
        )
    row = np.asarray(mat[lookup[series_id]], dtype=float)
    if train_periods is not None:
        if train_periods > row.size:
            raise HTTPException(
                status_code=422,
                detail=f"train_periods={train_periods} exceeds the {row.size}-month series {series_id!r}",
            )
        row = row[:train_periods]
    return row


def _costs_or_422(req: DecisionRequest) -> nv.NewsvendorCosts:
    try:
        return nv.newsvendor_costs(
            unit_price_usd=req.unit_price_usd,
            review_period_months=float(req.review_period_months),
            shortage_mode=req.shortage_mode,
            expedite_freight_usd_per_unit=req.expedite_freight_usd_per_unit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/assumptions")
def get_assumptions(
    unit_price_usd: float = Query(1.0, gt=0.0, le=1_000_000.0),
    review_period_months: int = Query(1, ge=1, le=MAX_REVIEW_PERIOD_MONTHS),
    shortage_mode: str = Query("expedite"),
    expedite_freight_usd_per_unit: float = Query(0.0, ge=0.0, le=10_000.0),
) -> Dict[str, Any]:
    """The cost asymmetry behind every order quantity this API returns.

    Published separately and first because it is the weakest link: the fractile is a
    monotone function of Cu/Co, and both are cited industry averages rather than a
    measurement of any part in this catalogue. A reader who disagrees with 0.15 or 0.25
    should be able to see exactly what changes, which is what the query parameters are for.
    """
    try:
        costs = nv.newsvendor_costs(
            unit_price_usd=unit_price_usd,
            review_period_months=float(review_period_months),
            shortage_mode=shortage_mode,
            expedite_freight_usd_per_unit=expedite_freight_usd_per_unit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "critical_fractile": costs.as_dict(),
        "inputs": {
            "holding_rate_annual": {
                "value": nv.ANNUAL_HOLDING_RATE,
                "source": "Gartner IT Supply Chain Benchmarks 2022 -- electronics annual holding "
                          "cost (capital + obsolescence + warehousing + insurance)",
                "used_via": "app.optimization.costs.holding_cost_usd, the same function the "
                            "freight/holding cost model calls, so the two cannot drift",
            },
            "expedite_premium": {
                "value": nv.EXPEDITE_PREMIUM,
                "source": "the emergency-reprocurement premium already used by "
                          "app/graph/simulation.py",
                "justification": "A spare part that is out of stock is not a lost sale. The "
                                 "demand does not evaporate; the unit is re-procured on an "
                                 "emergency footing. The cost of the shortage is therefore the "
                                 "PREMIUM paid to recover it, not the margin on it -- which is "
                                 "why Cu is a fraction of unit price, not a multiple of it.",
            },
            "stockout_escalation_multiple": {
                "value": nv.STOCKOUT_ESCALATION_MULTIPLE,
                "source": "Snyder & Daskin (2005), Reliable Facility Location Models, "
                          "Transportation Science 39(3):400-416",
                "applies_when": "shortage_mode='line_down': a single-sourced part with no "
                                "substitutable offer, where the recourse is a line-down or "
                                "respin event rather than an expedite.",
            },
            "excluded_fixed_expedite_charge": {
                "value": 150.0,
                "source": "app/optimization/constants.py::AIR_FREIGHT_BASE_USD (DHL/FedEx "
                          "commercial minimum consignment handling charge)",
                "why_excluded": "It is per CONSIGNMENT, not per unit, so it cannot enter a "
                                "linear per-unit Cu without an assumption about how many short "
                                "units share a shipment. Excluding it pushes Cu down, tau down "
                                "and q* down: the published order quantities understock relative "
                                "to the true asymmetry, and the measured saving is a lower bound.",
            },
        },
        "derivation": {
            "formula": "q* = F^-1(Cu / (Cu + Co)); for integer demand, min{q : F(q) >= tau}",
            "why": "C(q) = Cu E[(D-q)+] + Co E[(q-D)+] is convex with first difference "
                   "(Cu + Co) F(q) - Cu, which first turns non-negative exactly at that q.",
            "price_invariance": "Cu and Co are both proportional to unit price, so tau does not "
                                "depend on it. That is what makes this computable on a real "
                                "demand panel that carries no prices, with nothing fabricated.",
            "dual_identity": "realized cost at q equals (Cu + Co) x pinball_loss(q, y, tau) "
                             "exactly -- so the scaled pinball loss already on "
                             "GET /demand/benchmark is this decision cost up to a constant.",
        },
        "caveats": [
            "These are INDUSTRY AVERAGES, not measurements of any part in this catalogue. "
            "0.25/yr is an electronics-sector holding rate and 0.15 is a generic expedite "
            "premium; neither was estimated from this repo's data, and neither could be.",
            "THIS IS A CARRYING-CHARGE NEWSVENDOR, NOT A PERISHABLE ONE. Unsold stock carries "
            "forward, so Co is one period of carrying charge (~2% of unit price per month), not "
            "a write-off of the whole unit. That single choice is what makes tau 0.88 rather "
            "than 0.13. If the part genuinely perishes or obsoletes inside the period, raise "
            "holding_rate_annual or lengthen review_period_months and re-read tau.",
        ],
    }


@router.post("/decision")
def post_decision(req: DecisionRequest) -> Dict[str, Any]:
    """How much to order, the fractile behind it, and what the naive rules would have done.

    Closed form: one smoothing pass over the history, one inverse-cdf lookup. There is no
    solver here and no approximation -- for an integer-valued demand the critical fractile
    IS the exact minimiser of expected cost, not a relaxation of it.
    """
    if (req.demand_history is None) == (req.series is None):
        raise HTTPException(
            status_code=422,
            detail="send exactly one of `demand_history` or `series` -- not both, not neither",
        )
    if req.method not in nv.DIST_BUILDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown method {req.method!r}; expected one of {sorted(nv.DIST_BUILDERS)}",
        )

    if req.series is not None:
        train = _resolve_series(req.series, req.train_periods)
        origin = {"kind": "panel_series", "series": req.series, "n_periods": int(train.size)}
    else:
        train = _validate_history(list(req.demand_history or []))
        origin = {"kind": "caller_history", "series": None, "n_periods": int(train.size)}

    costs = _costs_or_422(req)

    try:
        monthly_pmf, source = nv.predictive_distribution(train, method=req.method)
    except nv.PredictiveLawError as exc:
        # 422, not 500: the input window is what triggers the upstream numerical defect,
        # and the caller can act on it (different window, different method). Failing loudly
        # is the point -- the alternative is an order quantity 30x too large.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    pmf = nv.aggregate_pmf(monthly_pmf, req.review_period_months) if req.review_period_months > 1 else monthly_pmf
    decision = nv.decide_from_pmf(pmf, costs, method=req.method, distribution_source=source)

    mean, sd = nv.pmf_moments(pmf)
    quantiles = {
        f"q{int(round(level * 100)):02d}": nv.order_quantity_from_pmf(pmf, level)
        for level in DEFAULT_QUANTILE_LEVELS
    }

    payload = decision.as_dict()
    payload["input"] = {
        **origin,
        "method": req.method,
        "review_period_months": req.review_period_months,
        "unit_price_usd": req.unit_price_usd,
        "shortage_mode": req.shortage_mode,
        "observed_mean_per_month": round(float(np.mean(train)), 6),
        "observed_nonzero_fraction": round(float(np.mean(np.asarray(train) > 0)), 6),
    }
    payload["demand_distribution"] = {
        "family": "compound Bernoulli(p) x zero-truncated NegBin(mean z)"
        if source == "parametric"
        else ("in-sample empirical distribution" if source == "empirical" else "degenerate point mass"),
        "source": source,
        "driving_model": "app/ml/intermittent.py demand predictive distribution -- the same "
                         "law GET /demand/benchmark scores under CRPS and pinball loss. NOT the "
                         "lead-time model, and NOT a per-part forecast for this catalogue.",
        "periods_aggregated": req.review_period_months,
        "mean": round(mean, 6),
        "sd": round(sd, 6),
        "p_zero": round(float(np.asarray(pmf, dtype=float)[0] / float(np.sum(pmf))), 6),
        "support_max": int(np.asarray(pmf).size - 1),
        "quantiles": quantiles,
    }
    return payload


@lru_cache(maxsize=EVALUATION_CACHE_SIZE)
def _cached_evaluation(forecast_method: str, review_period_months: int, shortage_mode: str) -> Dict[str, Any]:
    started = time.perf_counter()
    result = nv.run_panel_evaluation(
        unit_price_usd=1.0,
        review_period_months=review_period_months,
        shortage_mode=shortage_mode,
        forecast_method=forecast_method,
        n_boot=EVALUATION_N_BOOT,
        seed=EVALUATION_SEED,
    )
    result["wall_seconds"] = round(time.perf_counter() - started, 3)
    logger.info(
        "newsvendor evaluation: method=%s L=%d mode=%s in %.2fs",
        forecast_method, review_period_months, shortage_mode, result["wall_seconds"],
    )
    return result


@router.get("/evaluation")
def get_evaluation(
    forecast_method: str = Query(nv.DEFAULT_METHOD, description="Which predictive distribution the policy runs on."),
    review_period_months: int = Query(
        1,
        ge=1,
        le=EVALUATION_MAX_REVIEW_PERIOD_MONTHS,
        description=(
            "Periods per decision. Bounded by the held-out horizon, not by taste: the "
            "evaluation splits a 6-month horizon into floor(horizon / L) non-overlapping "
            "blocks, so there is no evaluation to report past L = 6."
        ),
    ),
    shortage_mode: str = Query("expedite"),
) -> Dict[str, Any]:
    """The policy against every baseline on held-out demand, with paired bootstrap CIs.

    This endpoint exists because an order quantity on its own is not evidence. The house
    rule is that a policy ships only by beating a stated baseline, so the comparison ships
    with it: expected cost against six naive rules on 2,646 held-out series at three
    rolling origins, paired by series, with a 95% bootstrap CI and a win/tie/loss split.

    Read `ship_gate` before quoting anything. It fails closed, and it does fail -- at
    `shortage_mode=line_down` the margin over the toughest baseline stops being
    significant, which is the honest report on a fractile the data cannot resolve.

    NO UNIT PRICE PARAMETER, deliberately: the fractile does not depend on price and every
    dollar figure scales linearly in it, so the figures below are per $1.00 of unit price.
    Multiply. It also means the parameter space is finite and enumerable, which is what
    bounds the cost of the one thing here that is genuinely expensive.

    WHERE THESE NUMBERS COME FROM, PER REQUEST, IN `computation`. Every configuration this
    endpoint can be asked for is published in `docs/newsvendor.json` and served as-is, so a
    healthy deployment never recomputes. It still can: if the artifact is missing, unreadable
    or describes a different computation from this code's, the panel is evaluated on the
    spot, which on the deployed 0.5-CPU instance takes about 107 seconds of the only worker
    there is. `computation.recomputed` says which happened, and `wall_seconds` is the time
    THIS request took -- not a number copied out of the artifact and presented as if this
    server had just measured it.
    """
    started = time.perf_counter()
    if forecast_method not in nv.DIST_BUILDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown forecast_method {forecast_method!r}; expected one of {sorted(nv.DIST_BUILDERS)}",
        )
    if shortage_mode not in nv.SHORTAGE_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown shortage_mode {shortage_mode!r}; expected one of {sorted(nv.SHORTAGE_MODES)}",
        )

    served = _artifact_evaluation(forecast_method, review_period_months, shortage_mode)
    if served is not None:
        result: Dict[str, Any] = served["block"]
        provenance = served["provenance"]
        result["computation"] = {
            "recomputed": False,
            "source": f"docs/newsvendor.json :: {served['block_name']}",
            "generator": "seeds.run_newsvendor -> app.optimization.newsvendor.run_panel_evaluation",
            "artifact_generated_at_utc": provenance.get("generated_at_utc"),
            "artifact_git_commit": (provenance.get("git") or {}).get("commit"),
            "artifact_wall_seconds": result.get("wall_seconds"),
            "equality_guarantee": (
                "This block is not a summary or a rounding of the computation -- it IS the "
                "computation's output. backend/tests/test_newsvendor_evaluation_is_served_"
                "from_the_artifact.py asserts every reachable configuration is served from "
                "this artifact and re-runs run_panel_evaluation the slow way on a rotating "
                "sample of them, comparing every leaf exactly; "
                "backend/tests/test_artifacts_pinned_to_code.py pins the primary block to "
                "1e-9. If the code changes and the artifact is not regenerated, those gates "
                "go red -- the endpoint cannot quietly serve a stale number."
            ),
            "why": (
                "render.yaml runs ONE uvicorn worker on a 0.5-CPU free instance. Recomputing "
                "this on request measured wall_seconds 259.897 cold and 106.589 warm against "
                "the live API on 2026-08-30, during which that worker serves nothing else."
            ),
        }
    else:
        try:
            result = dict(_cached_evaluation(forecast_method, review_period_months, shortage_mode))
        except ValueError as exc:
            # The query bound above already rejects L > the horizon. This is the second
            # line: `run_panel_evaluation` raising for a configuration is a 422 about the
            # request, never a 500 about the server, and that distinction was worth a
            # dropdown value that returned an unhandled traceback for as long as it existed.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            # 503, not an empty body: "the panel is not deployed here" and "no policy beats its
            # baselines" are different claims and must not be able to look alike.
            raise HTTPException(
                status_code=503,
                detail=(
                    "The Monash car-parts panel is not present in this deployment, so the "
                    "newsvendor policy cannot be evaluated. It is committed at "
                    "backend/seeds/data/car_parts_monthly.npz."
                ),
            ) from exc
        result["computation"] = {
            "recomputed": True,
            "source": "app.optimization.newsvendor.run_panel_evaluation, run in this process",
            "generator": None,
            "artifact_generated_at_utc": None,
            "artifact_git_commit": None,
            "artifact_wall_seconds": None,
            "equality_guarantee": "n/a -- this response IS the computation.",
            "why": (
                "docs/newsvendor.json served no block for this configuration -- it is absent, "
                "unreadable, or does not identify itself as this code's own output -- so the "
                "panel was evaluated from scratch instead of a stale number being handed back. "
                "That takes about 107 seconds of the deployed instance's single 0.5-CPU "
                "worker. On a healthy deployment every reachable configuration is published "
                "and this branch is not taken."
            ),
        }

    # The time THIS request took. The artifact's own generation time is reported separately
    # in `computation.artifact_wall_seconds` rather than passed off as a live measurement.
    result["wall_seconds"] = round(time.perf_counter() - started, 3)

    result["units"] = {
        "cost": "USD per SKU per review period, at a unit price of $1.00 -- multiply by your "
                "part's unit price",
        "order_quantity": "units, integer",
        "mean_difference": "USD per SKU per review period; POSITIVE means the newsvendor "
                           "policy is cheaper than that baseline",
    }
    result["reproduce"] = (
        "python -c \"from app.optimization.newsvendor import run_panel_evaluation as r; "
        f"print(r(forecast_method='{forecast_method}', review_period_months={review_period_months}, "
        f"shortage_mode='{shortage_mode}'))\""
    )
    return result
