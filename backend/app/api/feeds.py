"""
Feed status endpoint — public (no auth required per ASVS V4 assessment).

Returns freshness status for all 4 live data feeds.

Four states, deliberately distinct so a dormant feed can never be mistaken for
a working one (or for a transient outage):
  live        — downloaded within 2x the refresh interval AND, where the payload
                dates its own observations, that observation is recent too
  stale       — real data, but the download or the observation is older than that
  inactive    — required credential absent; feed never ran. `detail` names the
                missing env var. No value is fabricated to cover the gap.
  unavailable — configured and attempted, but the fetch failed / not yet run

DOWNLOAD RECENCY IS NOT DATA RECENCY (fixed 2026-09-07)
------------------------------------------------------
This endpoint used to derive `status` purely from `fetched_at`, i.e. from whether
the HTTP GET had succeeded recently. For three of the four feeds that is fine —
they are actively maintained series. For GPR it was not: the published file at
`fetchers.GPR_URL` still downloads on every 15-minute tick, but the newest row in
it is dated **September 2021**. The endpoint therefore reported the feed `live`
and served a four-year-old number as a current reading of geopolitical risk.

A successful download proves the publisher's server answered. It does not prove
the publisher is still updating the series. So any feed that dates its own
observations now also has that date checked (`CachedFeed.observed_at`), and a
feed whose newest observation is older than `MAX_OBSERVATION_AGE_DAYS` reads
`stale` no matter how recently it was fetched, with `detail` naming the
observation date. Feeds that carry no usable observation date fall back to
download recency and claim nothing more.
"""
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.feeds import get_live_data_cache

router = APIRouter(prefix="/feeds", tags=["feeds"])


class FeedStatus(BaseModel):
    """One live feed's freshness. Declared so generated clients see the shape."""
    name: str
    fetched_at: Optional[str] = None
    status: str            # live | stale | inactive | unavailable
    value_summary: Optional[str] = None
    detail: Optional[str] = None   # missing env var, the fetch error, or why the
                                   # observation is stale — never a value
    # The date of the newest OBSERVATION in the payload, where the payload dates
    # itself. None means the feed publishes no usable observation date, and then
    # `status` describes download recency only.
    observation_date: Optional[str] = None

# TTL for freshness calculation (feeds refresh every 15 min)
FEED_TTL_MINUTES = 15

# How old the newest OBSERVATION may be before a feed reads `stale` regardless of
# download recency. GPR is a monthly series, so three missed months is not a
# publication lag — it means the series has stopped being maintained at the URL
# we read. 90 days is deliberately generous: the failure this catches is measured
# in years (the GPR archive's newest row is from September 2021), not weeks.
#
# WHICH FEEDS THIS ACTUALLY PROTECTS, stated rather than implied: **GPR only.**
# `fetch_gpr_observation` is the one fetcher that returns an observation date;
# FRED and PortWatch parse a date out of their payloads and discard it
# (`fetchers.py`), and ACLED is a rolling 90-day window with no single "as of".
# Those three therefore fall back to download recency and this constant never
# applies to them — so do not read a `live` on them as a claim about the age of
# their DATA. Extending the guard to FRED needs its own threshold, not this one:
# FRED's freight index publishes with a real multi-month lag, so a 90-day rule
# would mark a perfectly healthy series stale.
MAX_OBSERVATION_AGE_DAYS = 90


def _feed_status(fetched_at: Optional[datetime], data: object,
                 inactive_reason: Optional[str] = None,
                 observed_at: Optional[datetime] = None) -> str:
    if inactive_reason:
        return "inactive"
    if data is None:
        return "unavailable"
    if fetched_at is None:
        return "unavailable"
    if datetime.utcnow() - fetched_at > timedelta(minutes=FEED_TTL_MINUTES * 2):
        return "stale"
    # Downloaded recently. Now ask the harder question: is the DATA recent?
    if observed_at is not None:
        if datetime.utcnow() - observed_at > timedelta(days=MAX_OBSERVATION_AGE_DAYS):
            return "stale"
    return "live"


def _observation_detail(observed_at: Optional[datetime], status: str) -> Optional[str]:
    """Explain a `stale` verdict that download recency alone would not predict."""
    if observed_at is None or status != "stale":
        return None
    age_days = (datetime.utcnow() - observed_at).days
    if age_days <= MAX_OBSERVATION_AGE_DAYS:
        return None
    return (
        f"Downloaded successfully, but the newest observation in the published "
        f"file is dated {observed_at.date().isoformat()} ({age_days} days old). "
        f"The source is reachable; the series is not being updated there. The "
        f"value shown is that historical observation, not a current reading."
    )


def _value_summary(name: str, data: object) -> Optional[str]:
    if data is None:
        return None
    if name == "gpr":
        return f"GPR: {data:.1f}"
    if name == "acled":
        total = sum(data.values()) if isinstance(data, dict) else 0
        return f"{total} events across {len(data) if isinstance(data, dict) else 0} countries"
    if name == "portwatch":
        if isinstance(data, dict):
            parts = [f"{k}: {v:.2f}" for k, v in data.items()]
            return "; ".join(parts)
        return None
    if name == "fred_freight":
        return f"TSIFRGHT: {data:.1f}"
    return None


@router.get("/status", response_model=List[FeedStatus])
async def feed_status():
    """Return freshness status for all 4 live feeds.

    No auth required — this is public dashboard data (per ASVS V4 assessment).
    T-03-13: Returns only names, timestamps, and value summaries — no credentials,
    no raw API responses.
    """
    cache = get_live_data_cache()
    if cache is None:
        return [
            {"name": n, "fetched_at": None, "status": "unavailable",
             "value_summary": None, "detail": None, "observation_date": None}
            for n in ["GPR Index", "ACLED Conflict", "IMF PortWatch", "FRED Freight"]
        ]

    feed_map = [
        ("GPR Index", "gpr", cache.gpr),
        ("ACLED Conflict", "acled", cache.acled),
        ("IMF PortWatch", "portwatch", cache.portwatch),
        ("FRED Freight", "fred_freight", cache.fred_freight),
    ]
    results = []
    for display_name, key, feed in feed_map:
        reason = getattr(feed, "inactive_reason", None)
        observed_at = getattr(feed, "observed_at", None)
        status = _feed_status(feed.fetched_at, feed.data, reason, observed_at)
        results.append({
            "name": display_name,
            "fetched_at": feed.fetched_at.isoformat() + "Z" if feed.fetched_at else None,
            "status": status,
            "value_summary": _value_summary(key, feed.data),
            # Why the feed isn't live: the missing env var, the fetch error, or a
            # download that succeeded against a series that stopped being
            # published. Never a stand-in value.
            "detail": reason or feed.error or _observation_detail(observed_at, status),
            "observation_date": observed_at.date().isoformat() if observed_at else None,
        })
    return results
