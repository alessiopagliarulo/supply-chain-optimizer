"""
APScheduler 3.x AsyncIOScheduler for 15-minute feed refresh cycle.

Credential-gated feeds are handled EXPLICITLY: if a feed's key is absent we do
not call it, we do not log a misleading "refreshed", and we never invent a
value. We mark the feed `inactive` with the exact env var that is missing, which
/feeds/status surfaces to the UI. Dormant reads as dormant.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.feeds import LiveDataCache, CachedFeed
from app.feeds.fetchers import (
    acled_inactive_reason,
    fetch_acled,
    fetch_fred_freight,
    fetch_gpr_observation,
    fetch_portwatch,
)
from app.core.config import settings

logger = logging.getLogger(__name__)


def build_scheduler(cache: LiveDataCache) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        refresh_all_feeds, 'interval', minutes=15,
        args=[cache], id='feed_refresh',
        max_instances=1, coalesce=True,
        # Fire IMMEDIATELY on startup, then every 15 minutes.
        #
        # APScheduler's `interval` trigger schedules its first run one whole interval
        # after the job is added, so without this the feeds stayed `unavailable` for
        # the first 15 minutes of every process. Render's free tier spins the service
        # down after ~15 minutes idle, so the deployed instance almost never survived
        # to its first tick — all four feeds read `unavailable` in production while
        # working perfectly in a long-running local process. Verified: locally the
        # feeds went live at exactly T+15m; with this argument they are live at T+0.
        #
        # `refresh_all_feeds` is already safe to run at startup: every fetch is wrapped
        # by `_safe_refresh`, credential-gated feeds are marked inactive rather than
        # called, and `max_instances=1` + `coalesce=True` keep the first run from
        # overlapping the next tick.
        next_run_time=datetime.now(),
    )
    return scheduler


async def refresh_all_feeds(cache: LiveDataCache) -> None:
    import asyncio

    # ACLED is the only feed that CANNOT run without a credential (GPR and
    # PortWatch are keyless; FRED falls back to the keyless CSV endpoint).
    acled_reason = acled_inactive_reason(settings.ACLED_EMAIL, settings.ACLED_KEY)

    await asyncio.gather(
        _refresh_gpr(cache.gpr),
        _refresh_acled(cache.acled, acled_reason),
        _safe_refresh(cache.portwatch, lambda: fetch_portwatch(), "portwatch"),
        _safe_refresh(
            cache.fred_freight,
            lambda: fetch_fred_freight(settings.FRED_API_KEY),
            "fred_freight",
        ),
    )


async def _refresh_gpr(feed: CachedFeed) -> None:
    """Refresh GPR, recording the observation date as well as the fetch time.

    GPR is the one feed where those two dates diverge by years: the file at
    GPR_URL still downloads on every tick, but its newest row is dated 2021-09.
    Storing both is what lets /feeds/status say "downloaded minutes ago,
    observation is four years old" instead of a bare "live".
    """
    await _safe_refresh(feed, fetch_gpr_observation, "gpr", splits_observation_date=True)


async def _refresh_acled(feed: CachedFeed, inactive_reason) -> None:
    """Refresh ACLED, or mark it explicitly inactive when creds are absent."""
    if inactive_reason:
        async with feed.lock:
            feed.data = None          # nothing fabricated to fill the gap
            feed.error = None         # not an error — a configuration state
            feed.inactive_reason = inactive_reason
        logger.warning("Feed acled INACTIVE: %s", inactive_reason)
        return
    await _safe_refresh(
        feed, lambda: fetch_acled(settings.ACLED_EMAIL, settings.ACLED_KEY), "acled"
    )


async def _safe_refresh(
    feed: CachedFeed, fetcher, name: str, splits_observation_date: bool = False
) -> None:
    import asyncio

    async with feed.lock:
        try:
            result = await fetcher()
            if splits_observation_date:
                feed.data, feed.observed_at = result
            else:
                feed.data = result
            feed.fetched_at = datetime.utcnow()
            feed.error = None
            feed.inactive_reason = None
            logger.info("Feed %s refreshed at %s", name, feed.fetched_at.isoformat())
        except asyncio.CancelledError:
            # The refresh now fires at T+0, so it is routinely still in flight when a
            # short-lived process (tests, a CLI run, a Render restart) shuts down. That
            # is a cancellation, not a feed failure: record it as such and let it
            # propagate, rather than logging a stack trace that reads like an outage.
            feed.error = None
            logger.info("Feed %s refresh cancelled (shutdown)", name)
            raise
        except Exception as exc:
            feed.error = str(exc)
            logger.warning("Feed %s refresh failed: %s", name, exc)
