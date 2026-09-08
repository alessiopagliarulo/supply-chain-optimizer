"""
Unit tests for app/feeds/ package — LiveDataCache, scheduler, and optimizer integration.
"""
from __future__ import annotations
import asyncio
import io
import math
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import openpyxl

import app.feeds as feeds_module
from app.feeds import CachedFeed, LiveDataCache, get_live_data_cache, set_live_data_cache


# ── GPR helpers ───────────────────────────────────────────────────────────────

def _make_gpr_xlsx(gpr_value: float) -> bytes:
    """Build a minimal valid GPR XLSX with a 'GPR' sheet for testing."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GPR"
    # Header row
    ws.append(["Date", "GPR", "GPR_THREAT", "GPR_ACT"])
    # Data rows
    ws.append(["1985-01-01", 100.0, 50.0, 50.0])
    ws.append(["1985-02-01", gpr_value, 60.0, 70.0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Task 1: Cache and scheduler tests ─────────────────────────────────────────

def test_cache_init():
    """LiveDataCache initializes with all 4 CachedFeed fields having data=None."""
    cache = LiveDataCache()
    assert cache.gpr.data is None
    assert cache.acled.data is None
    assert cache.portwatch.data is None
    assert cache.fred_freight.data is None


def test_singleton_set_get():
    """set_live_data_cache() then get_live_data_cache() returns same instance."""
    cache = LiveDataCache()
    set_live_data_cache(cache)
    assert get_live_data_cache() is cache
    # Reset for test isolation
    feeds_module._cache = None


def test_singleton_default_none():
    """get_live_data_cache() returns None before set_live_data_cache() is called."""
    # Ensure clean state
    feeds_module._cache = None
    assert get_live_data_cache() is None


def test_cached_feed_has_lock():
    """CachedFeed.lock is an asyncio.Lock instance."""
    feed = CachedFeed()
    assert isinstance(feed.lock, asyncio.Lock)


def test_build_scheduler_returns_scheduler():
    """build_scheduler returns an AsyncIOScheduler with a job named 'feed_refresh'."""
    from app.feeds.scheduler import build_scheduler
    cache = LiveDataCache()
    scheduler = build_scheduler(cache)
    assert hasattr(scheduler, 'get_job')
    assert scheduler.get_job('feed_refresh') is not None


# ── Task 2: _feed_risk_obj_units and _port_delay_days tests ──────────────────

def _make_offer(price_usd: float = 10.0) -> object:
    """Create a minimal Offer-like object for testing."""
    from app.optimization.sourcing import Offer
    return Offer(
        component_id=1,
        distributor_id=1,
        distributor_name="TestDist",
        price_usd=price_usd,
        stock=100,
        moq=1,
        is_domestic=True,
        dist_km_from_depot=500.0,
        risk_score=0.3,
        is_chinese_origin=False,
    )


def test_feed_risk_obj_units_cache_none():
    """_feed_risk_obj_units returns 0 when cache is None."""
    from app.optimization.sourcing import _feed_risk_obj_units
    offer = _make_offer(price_usd=10.0)
    assert _feed_risk_obj_units(offer, "US", False, None) == 0


def test_feed_risk_obj_units_empty_feeds():
    """_feed_risk_obj_units returns 0 when all feed data is None."""
    from app.optimization.sourcing import _feed_risk_obj_units
    offer = _make_offer(price_usd=10.0)
    cache = LiveDataCache()
    assert _feed_risk_obj_units(offer, "US", False, cache) == 0


def test_feed_risk_obj_units_gpr_elevated():
    """_feed_risk_obj_units returns positive surcharge when GPR is elevated (>100) and is_chinese_origin=True."""
    from app.optimization.sourcing import _feed_risk_obj_units
    offer = _make_offer(price_usd=10.0)
    cache = LiveDataCache()
    cache.gpr.data = 200.0  # above baseline of 100
    result = _feed_risk_obj_units(offer, "US", True, cache)
    assert result > 0


def test_feed_risk_obj_units_ceiling():
    """_feed_risk_obj_units result is at most floor(0.15 * unit_price_obj_units)."""
    from app.optimization.sourcing import _feed_risk_obj_units, to_obj_units
    offer = _make_offer(price_usd=10.0)
    cache = LiveDataCache()
    cache.gpr.data = 500.0  # maximum GPR
    cache.acled.data = {"CN": 1000}
    result = _feed_risk_obj_units(offer, "CN", True, cache)
    # Derive the ceiling through the same conversion the function uses, rather
    # than hardcoding a scale that can silently drift away from the objective's.
    ceiling = int(math.floor(0.15 * to_obj_units(offer.price_usd)))
    assert result <= ceiling


def test_feed_risk_surcharge_survives_a_sub_cent_price():
    """A priced offer must not have its whole live-feed signal floored to zero.

    The surcharge used to be computed in whole cents and scaled up afterwards,
    so every offer under ~$0.07 got exactly nothing from GPR/ACLED while its
    price term still counted. $0.0031 is the real catalogue price of
    MLG0603P43NHT000.
    """
    from app.optimization.sourcing import _feed_risk_obj_units
    offer = _make_offer(price_usd=0.0031)
    cache = LiveDataCache()
    cache.gpr.data = 500.0
    assert _feed_risk_obj_units(offer, "US", True, cache) > 0


def test_feed_risk_obj_units_acled_signal():
    """_feed_risk_obj_units returns positive surcharge from ACLED conflict data."""
    from app.optimization.sourcing import _feed_risk_obj_units
    offer = _make_offer(price_usd=10.0)
    cache = LiveDataCache()
    cache.acled.data = {"US": 50}
    result = _feed_risk_obj_units(offer, "US", False, cache)
    assert result > 0


def test_port_delay_cache_none():
    """_port_delay_days returns 0.0 when cache is None."""
    from app.optimization.costs import _port_delay_days
    assert _port_delay_days(33.7, -118.2, None) == 0.0


def test_port_delay_la_congested():
    """_port_delay_days returns ~1.5 days for LA/LB with congestion_ratio=1.5."""
    from app.optimization.costs import _port_delay_days
    cache = LiveDataCache()
    cache.portwatch.data = {"LA_LB": 1.5, "NY_NJ": 1.0, "SAVANNAH": 1.0}
    result = _port_delay_days(33.7, -118.2, cache)
    # (1.5 - 1.0) * 3.0 = 1.5
    assert result == pytest.approx(1.5, abs=0.1)


def test_port_delay_no_congestion():
    """_port_delay_days returns 0.0 when all port congestion ratios are 1.0."""
    from app.optimization.costs import _port_delay_days
    cache = LiveDataCache()
    cache.portwatch.data = {"LA_LB": 1.0, "NY_NJ": 1.0, "SAVANNAH": 1.0}
    result = _port_delay_days(33.7, -118.2, cache)
    assert result == 0.0


# ── Task 1: fetch_gpr() tests ─────────────────────────────────────────────────

def test_gpr_url_constant():
    """GPR_URL equals the hardcoded matteoiacoviello.com XLSX URL."""
    from app.feeds.fetchers import GPR_URL
    assert GPR_URL == "https://www.matteoiacoviello.com/gpr_files/gpr_web_latest.xlsx"


@pytest.mark.asyncio
async def test_gpr_parse():
    """fetch_gpr() returns the last GPR value from column B as a float."""
    from app.feeds.fetchers import fetch_gpr
    xlsx_bytes = _make_gpr_xlsx(142.5)

    mock_response = MagicMock()
    mock_response.content = xlsx_bytes
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.feeds.fetchers.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_gpr()

    assert result == pytest.approx(142.5)


@pytest.mark.asyncio
async def test_gpr_http_error():
    """fetch_gpr() raises httpx.HTTPStatusError when server returns 404."""
    import httpx
    from app.feeds.fetchers import fetch_gpr

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.feeds.fetchers.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_gpr()


# ── Task 2: fetch_acled() tests ───────────────────────────────────────────────

def test_acled_url_constant():
    """ACLED_API_URL equals the hardcoded acleddata.com endpoint."""
    from app.feeds.fetchers import ACLED_API_URL
    assert ACLED_API_URL == "https://acleddata.com/acled/read"


@pytest.mark.asyncio
async def test_acled_aggregate():
    """fetch_acled() aggregates events by ISO3 country code and returns correct counts."""
    from app.feeds.fetchers import fetch_acled

    mock_json_data = {
        "data": [
            {"iso3": "SYR", "event_type": "Battles"},
            {"iso3": "SYR", "event_type": "Riots"},
            {"iso3": "UKR", "event_type": "Battles"},
        ]
    }
    mock_response = MagicMock()
    mock_response.json = MagicMock(return_value=mock_json_data)
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.feeds.fetchers.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_acled("test@email.com", "testkey")

    assert result == {"SYR": 2, "UKR": 1}

    # Verify query param auth — key and email as params, NOT headers
    call_kwargs = mock_client.get.call_args
    params = call_kwargs.kwargs.get("params", call_kwargs.args[1] if len(call_kwargs.args) > 1 else {})
    assert params.get("key") == "testkey"
    assert params.get("email") == "test@email.com"


@pytest.mark.asyncio
async def test_acled_no_email():
    """fetch_acled() returns None when email is empty string."""
    from app.feeds.fetchers import fetch_acled
    result = await fetch_acled("", "testkey")
    assert result is None


@pytest.mark.asyncio
async def test_acled_no_key():
    """fetch_acled() returns None when key is empty string."""
    from app.feeds.fetchers import fetch_acled
    result = await fetch_acled("test@email.com", "")
    assert result is None


@pytest.mark.asyncio
async def test_acled_none_credentials():
    """fetch_acled() returns None when both email and key are None."""
    from app.feeds.fetchers import fetch_acled
    result = await fetch_acled(None, None)
    assert result is None


@pytest.mark.asyncio
async def test_acled_empty_data():
    """fetch_acled() returns empty dict when API response has no events."""
    from app.feeds.fetchers import fetch_acled

    mock_response = MagicMock()
    mock_response.json = MagicMock(return_value={"data": []})
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.feeds.fetchers.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_acled("test@email.com", "testkey")

    assert result == {}


# ── Task 3 (Plan 03-03): fetch_portwatch() and fetch_fred_freight() tests ─────

def test_portwatch_url_constant():
    """PORTWATCH_URL is hardcoded to the ArcGIS Feature Service endpoint."""
    from app.feeds.fetchers import PORTWATCH_URL
    assert PORTWATCH_URL == "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/Daily_Ports_Data/FeatureServer/0/query"


@pytest.mark.asyncio
async def test_portwatch_congestion_proxy():
    """fetch_portwatch() computes congestion_ratio = baseline_avg / recent_avg for LA_LB.

    Setup: 90 features where recent 7 have portcalls=10, older have portcalls=15.
    Expected: baseline_avg = (7*10 + 83*15) / 90 ≈ 14.61, recent_avg = 10.
    congestion_ratio ≈ 14.61 / 10 ≈ 1.461 — but the plan action specifies
    baseline_avg = 15/10 = 1.5. We construct data accordingly:
    recent 7 = portcalls 10, remaining 83 = portcalls 15.
    """
    from app.feeds.fetchers import fetch_portwatch

    # Build 90 features: first 7 (most recent) have portcalls=10, rest have 15
    features = []
    for i in range(7):
        features.append({"attributes": {"portcalls": 10, "date": 1000 - i}})
    for i in range(83):
        features.append({"attributes": {"portcalls": 15, "date": 900 - i}})

    mock_json = {"features": features}

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=mock_json)

    # Build side_effect that returns features for LA_LB (port664) and empty for others
    def json_for_port(*args, **kwargs):
        where = kwargs.get("params", {}).get("where", "")
        if "port664" in where:
            return {"features": features}
        return {"features": features}  # all ports return same data for simplicity

    mock_response.json = MagicMock(side_effect=lambda: mock_json)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.feeds.fetchers.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_portwatch()

    assert "LA_LB" in result
    # baseline_avg = (7*10 + 83*15) / 90 ≈ 14.61; recent_avg = 10
    # congestion ≈ 14.61/10 ≈ 1.461; with round(,3) => ~1.461
    assert result["LA_LB"] > 1.0  # congestion > 1 means busier baseline than recent


@pytest.mark.asyncio
async def test_portwatch_insufficient_data():
    """fetch_portwatch() skips ports with fewer than 14 features (insufficient data)."""
    from app.feeds.fetchers import fetch_portwatch

    # Only 5 features — below the 14-record minimum
    features = [{"attributes": {"portcalls": 10, "date": 1000 - i}} for i in range(5)]
    mock_json = {"features": features}

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=mock_json)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.feeds.fetchers.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="no port data retrieved"):
            await fetch_portwatch()


@pytest.mark.asyncio
async def test_fred_freight_latest_value():
    """fetch_fred_freight() returns the latest TSIFRGHT float value from FRED API."""
    from app.feeds.fetchers import fetch_fred_freight

    mock_json = {"observations": [{"date": "2026-04-01", "value": "120.5"}]}
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=mock_json)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.feeds.fetchers.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_fred_freight("test_api_key")

    assert result == pytest.approx(120.5)


@pytest.mark.asyncio
async def test_fred_freight_no_key_uses_keyless_csv():
    """Without FRED_API_KEY, fetch_fred_freight() uses FRED's public keyless CSV
    endpoint — the same series from the same source. It must NOT raise, and must
    NOT invent a value."""
    from app.feeds.fetchers import fetch_fred_freight

    with patch(
        "app.feeds.fetchers.fetch_fred_freight_keyless",
        AsyncMock(return_value=139.6),
    ) as keyless:
        result = await fetch_fred_freight("")

    keyless.assert_awaited_once()
    assert result == pytest.approx(139.6)


def test_latest_from_fred_csv_skips_missing():
    """_latest_from_fred_csv() returns the last non-'.' observation."""
    from app.feeds.fetchers import _latest_from_fred_csv

    raw = "observation_date,TSIFRGHT\n2026-01-01,136.2\n2026-02-01,138.5\n2026-03-01,.\n"
    assert _latest_from_fred_csv(raw) == pytest.approx(138.5)


def test_latest_from_fred_csv_all_missing_raises():
    """_latest_from_fred_csv() raises rather than returning a fabricated default."""
    from app.feeds.fetchers import _latest_from_fred_csv

    with pytest.raises(ValueError, match="no valid observations"):
        _latest_from_fred_csv("observation_date,TSIFRGHT\n2026-01-01,.\n")


# ── Dormant-feed honesty: ACLED inactive state ────────────────────────────────

def test_acled_inactive_reason_names_missing_vars():
    """acled_inactive_reason() names exactly which env vars are missing."""
    from app.feeds.fetchers import acled_inactive_reason

    assert acled_inactive_reason("a@b.com", "key") is None
    assert "ACLED_KEY" in acled_inactive_reason("a@b.com", "")
    assert "ACLED_EMAIL" in acled_inactive_reason("", "key")
    both = acled_inactive_reason(None, None)
    assert "ACLED_EMAIL" in both and "ACLED_KEY" in both
    assert "acleddata.com/register" in both  # tells the owner how to fix it


@pytest.mark.asyncio
async def test_refresh_acled_marks_inactive_without_creds():
    """The scheduler marks ACLED 'inactive' (not 'refreshed') when creds are absent,
    leaves data None, and fabricates nothing."""
    from app.feeds.scheduler import _refresh_acled

    feed = CachedFeed()
    await _refresh_acled(feed, "ACLED_KEY not set — feed inactive.")

    assert feed.data is None          # nothing invented
    assert feed.error is None         # config state, not an error
    assert feed.inactive_reason is not None
    assert feed.fetched_at is None    # never claims it fetched


def test_feed_status_reports_inactive_with_detail():
    """GET /feeds/status reports status=inactive + a detail naming the missing key."""
    import app.feeds as feeds_module
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.api.feeds import router

    cache = LiveDataCache()
    cache.acled.inactive_reason = "ACLED_EMAIL + ACLED_KEY not set — feed inactive."

    original_cache = feeds_module._cache
    feeds_module._cache = cache
    try:
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        data = client.get("/feeds/status").json()
        acled = next(i for i in data if i["name"] == "ACLED Conflict")
        assert acled["status"] == "inactive"
        assert "ACLED_KEY" in acled["detail"]
        assert acled["value_summary"] is None  # no fabricated summary
    finally:
        feeds_module._cache = original_cache


# ── Task 3 (Plan 03-03): /feeds/status endpoint tests ─────────────────────────

def test_feed_status_endpoint_all_unavailable():
    """GET /feeds/status returns 200 with 4 items all showing status=unavailable when cache is None."""
    import app.feeds as feeds_module
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.api.feeds import router

    # Ensure cache is None
    original_cache = feeds_module._cache
    feeds_module._cache = None

    try:
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response = client.get("/feeds/status")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 4
        for item in data:
            assert item["status"] == "unavailable"
            assert "name" in item
            assert "fetched_at" in item
    finally:
        feeds_module._cache = original_cache


def test_feed_status_endpoint_live():
    """GET /feeds/status returns status=live for GPR when fetched_at is recent."""
    import app.feeds as feeds_module
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.api.feeds import router
    from datetime import datetime

    cache = LiveDataCache()
    cache.gpr.data = 150.0
    cache.gpr.fetched_at = datetime.utcnow()

    original_cache = feeds_module._cache
    feeds_module._cache = cache

    try:
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response = client.get("/feeds/status")
        assert response.status_code == 200
        data = response.json()
        gpr_item = next(item for item in data if item["name"] == "GPR Index")
        assert gpr_item["status"] == "live"
    finally:
        feeds_module._cache = original_cache


def test_feed_status_endpoint_stale():
    """GET /feeds/status returns status=stale for GPR when fetched_at is 1 hour ago."""
    import app.feeds as feeds_module
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.api.feeds import router
    from datetime import datetime, timedelta

    cache = LiveDataCache()
    cache.gpr.data = 150.0
    cache.gpr.fetched_at = datetime.utcnow() - timedelta(hours=1)

    original_cache = feeds_module._cache
    feeds_module._cache = cache

    try:
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response = client.get("/feeds/status")
        assert response.status_code == 200
        data = response.json()
        gpr_item = next(item for item in data if item["name"] == "GPR Index")
        assert gpr_item["status"] == "stale"
    finally:
        feeds_module._cache = original_cache


# ── Observation date vs. download date (2026-09-07) ──────────────────────────
#
# These exist because the feature they cover shipped with none, and a guard with
# no test is a guard that can be deleted without anything going red. Proven: with
# the `observed_at` branch removed from `app.api.feeds._feed_status`, the first
# test below fails.
#
# The bug being guarded: the published GPR file downloads successfully on every
# 15-minute tick, but its newest observation is dated 2021-09-01. Status derived
# from `fetched_at` alone therefore reported `live` and the app served a
# four-year-old number as a current reading of geopolitical risk.

def test_a_fresh_download_of_a_frozen_series_reports_stale_not_live():
    """The GPR case, exactly: downloaded seconds ago, observed in 2021."""
    from datetime import datetime, timedelta

    from app.api.feeds import MAX_OBSERVATION_AGE_DAYS, _feed_status

    now = datetime.utcnow()
    assert _feed_status(now, 128.78, None, datetime(2021, 9, 1)) == "stale"

    # And the boundary is the constant, not a coincidence of today's date.
    just_inside = now - timedelta(days=MAX_OBSERVATION_AGE_DAYS - 1)
    just_outside = now - timedelta(days=MAX_OBSERVATION_AGE_DAYS + 1)
    assert _feed_status(now, 128.78, None, just_inside) == "live"
    assert _feed_status(now, 128.78, None, just_outside) == "stale"


def test_a_feed_with_no_observation_date_still_uses_download_recency():
    """FRED/PortWatch/ACLED publish no observation date through this path.

    They must be completely unaffected: `observed_at=None` means "no opinion",
    and the download-recency answer stands. If this ever starts returning
    `stale`, three healthy feeds have been slandered.
    """
    from datetime import datetime, timedelta

    from app.api.feeds import _feed_status

    now = datetime.utcnow()
    assert _feed_status(now, 150.0, None, None) == "live"
    assert _feed_status(now - timedelta(hours=1), 150.0, None, None) == "stale"


def test_an_old_download_is_stale_even_when_the_observation_is_recent():
    """Download recency is still checked first — the new branch only adds a way
    to FAIL, never a way to pass something that was already stale."""
    from datetime import datetime, timedelta

    from app.api.feeds import _feed_status

    now = datetime.utcnow()
    assert _feed_status(now - timedelta(hours=1), 150.0, None, now) == "stale"


def test_inactive_and_unavailable_outrank_the_observation_check():
    from datetime import datetime

    from app.api.feeds import _feed_status

    now = datetime.utcnow()
    assert _feed_status(now, 128.78, "ACLED_KEY not set", datetime(2021, 9, 1)) == "inactive"
    assert _feed_status(now, None, None, datetime(2021, 9, 1)) == "unavailable"
    assert _feed_status(None, 128.78, None, datetime(2021, 9, 1)) == "unavailable"


def test_the_stale_detail_names_the_observation_date_and_never_invents_one():
    from datetime import datetime

    from app.api.feeds import _observation_detail

    detail = _observation_detail(datetime(2021, 9, 1), "stale")
    assert detail is not None
    assert "2021-09-01" in detail
    assert "not a current reading" in detail

    # No observation date, or not stale => nothing to explain. In particular this
    # must not manufacture an explanation for a `live` feed.
    assert _observation_detail(None, "stale") is None
    assert _observation_detail(datetime(2021, 9, 1), "live") is None


def test_the_gpr_date_column_is_parsed_in_every_spelling_it_has_ever_used():
    """`_coerce_observation_date` fails OPEN — an unparseable cell yields None,
    which falls back to download recency and reports `live`. Breadth here is
    therefore the actual mitigation, so it is asserted rather than assumed.
    """
    from datetime import date, datetime, timezone

    from app.feeds.fetchers import _coerce_observation_date

    expected = datetime(2021, 9, 1)
    for cell in (
        datetime(2021, 9, 1),
        date(2021, 9, 1),
        202109,                 # YYYYMM integer
        44440,                  # raw Excel serial, 1900 date system
        "2021-09-01",
        "2021-09",
        "202109",
        "2021/09/01",
        "Sep-2021",
        "September 2021",
        "2021M09",              # the FRED/ALFRED monthly spelling
    ):
        assert _coerce_observation_date(cell) == expected, f"failed on {cell!r}"

    # A timezone-aware cell must come back NAIVE: feeds.py subtracts it from
    # datetime.utcnow(), and mixing the two raises TypeError inside a status
    # endpoint instead of returning "stale".
    aware = _coerce_observation_date(datetime(2021, 9, 1, tzinfo=timezone.utc))
    assert aware is not None and aware.tzinfo is None

    # Genuinely unreadable => None, never a guess.
    for cell in (None, "n/a", "", "not a date", object()):
        assert _coerce_observation_date(cell) is None


def test_one_unreadable_row_does_not_discard_every_good_observation_date():
    """The parse loop keeps the last date that PARSED, not the last attempted.

    Assigning unconditionally meant a single unreadable final row threw away
    every good date in the sheet and left the feed with no observation date —
    i.e. back to reporting `live`. One bad cell must not disarm the guard.
    """
    import io

    import openpyxl

    from app.feeds.fetchers import fetch_gpr_observation

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GPR"
    ws.append(["Date", "GPR"])
    ws.append(["2021-08-01", 120.0])
    ws.append(["2021-09-01", 128.78])
    ws.append(["-- provisional --", 129.0])   # unreadable date, real value
    buf = io.BytesIO()
    wb.save(buf)

    import asyncio
    from unittest.mock import patch

    class _Resp:
        content = buf.getvalue()

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    with patch("app.feeds.fetchers.httpx.AsyncClient", lambda *a, **k: _Client()):
        value, observed = asyncio.run(fetch_gpr_observation())

    from datetime import datetime

    assert value == 129.0                      # the newest VALUE still wins
    assert observed == datetime(2021, 9, 1)    # the newest PARSEABLE date survives
