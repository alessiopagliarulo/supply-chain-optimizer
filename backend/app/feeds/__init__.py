"""
Live Data Feeds Cache.

Four external signals cached in-memory with TTL:
  1. GPR Index (Caldara-Iacoviello) — geopolitical risk        [keyless]
  2. ACLED conflict events — 90-day rolling by country          [NEEDS ACLED_EMAIL + ACLED_KEY]
  3. IMF PortWatch — port congestion proxy                      [keyless]
  4. FRED TSIFRGHT — freight transportation index               [keyless CSV; FRED_API_KEY optional]

Feed states (see `CachedFeed`):
  * live/stale    — real data was fetched; `data` holds it.
  * inactive      — the feed's credentials are NOT configured, so it was never
                    called. `inactive_reason` says exactly which env var is
                    missing. We NEVER substitute a fabricated value for a feed
                    that could not be fetched — a dormant feed reads as dormant.
  * unavailable   — configured and attempted, but the fetch failed (`error`).

Call get_live_data_cache() to get the current cache, or None if feeds
have not been initialized yet (initializes at startup via lifespan).
"""
from __future__ import annotations
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
import asyncio


@dataclass
class CachedFeed:
    data: object = None
    fetched_at: Optional[datetime] = None
    # When the underlying OBSERVATION is dated, as opposed to when we downloaded
    # it. These are not the same thing and conflating them is how a frozen
    # archive got published as a "live" feed: the GPR file downloads fine every
    # 15 minutes, but its newest row is from September 2021. A successful
    # download says the publisher's server answered; it says nothing about
    # whether the publisher is still updating the series. Left None by feeds
    # whose payload carries no usable observation date.
    observed_at: Optional[datetime] = None
    error: Optional[str] = None
    # Set when the feed cannot run at all because its credentials are absent.
    # Non-None => the feed is DORMANT by configuration, not broken. Its `data`
    # stays None; nothing is ever invented to fill the gap.
    inactive_reason: Optional[str] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class LiveDataCache:
    gpr: CachedFeed = field(default_factory=CachedFeed)
    acled: CachedFeed = field(default_factory=CachedFeed)
    portwatch: CachedFeed = field(default_factory=CachedFeed)
    fred_freight: CachedFeed = field(default_factory=CachedFeed)


_cache: Optional[LiveDataCache] = None


def get_live_data_cache() -> Optional[LiveDataCache]:
    return _cache


def set_live_data_cache(cache: LiveDataCache) -> None:
    global _cache
    _cache = cache
