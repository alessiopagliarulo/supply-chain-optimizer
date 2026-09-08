"""
Feed fetcher functions. Each returns the parsed payload or raises on failure.
Stubs for fetch_portwatch and fetch_fred_freight are implemented in Plan 03-03.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import openpyxl

logger = logging.getLogger(__name__)

# ── GPR Index (Geopolitical Risk Index — Caldara & Iacoviello) ─────────────────
# T-03-05: URL hardcoded as constant — never accept user-provided URLs (SSRF mitigation)
GPR_URL = "https://www.matteoiacoviello.com/gpr_files/gpr_web_latest.xlsx"


async def fetch_gpr_observation() -> "tuple[float, Optional[datetime]]":
    """Download GPR XLSX and return (latest GPR value, its observation date).

    The GPR sheet has columns: Date (A), GPR (B), GPR_THREAT (C), GPR_ACT (D), ...
    Monthly data since 1985.

    WHY THE DATE IS RETURNED AT ALL. This fetcher used to return only the value,
    so the only freshness signal anything downstream had was "did the HTTP GET
    succeed" — and it always does. The file at GPR_URL is an archive whose newest
    row is dated 2021-09-01; the app reported the feed `live` and published its
    2021 number as a current reading. Reading column A alongside column B is what
    makes the difference between "we downloaded it recently" and "the number is
    recent" observable, so /feeds/status can tell them apart.

    Returns the date as None if column A holds nothing parseable — in which case
    the caller falls back to download recency and must not imply more.

    T-03-05: URL is hardcoded as constant — never user-provided (SSRF mitigation).
    T-03-10: httpx timeout=30s; openpyxl read_only=True; offloaded to thread.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(GPR_URL)
        resp.raise_for_status()
        content = resp.content

    # openpyxl is synchronous — offload to thread to avoid blocking the event loop
    def _parse(data: bytes) -> "tuple[float, Optional[datetime]]":
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb["GPR"]
        last_value = None
        last_date = None
        for row in ws.iter_rows(min_row=2, max_col=2, values_only=True):
            # T-03-08: only column B numeric values extracted; non-numeric skipped
            if row[1] is not None:
                last_value = row[1]
                # Keep the last date that actually PARSED, not the last one
                # attempted. Assigning unconditionally meant a single unreadable
                # cell in the final row threw away 440 perfectly good ones and
                # left the feed with no observation date at all -- which
                # fails OPEN, back to "live". Freshness must not be one bad
                # cell away from being unenforceable.
                parsed = _coerce_observation_date(row[0])
                if parsed is not None:
                    last_date = parsed
        wb.close()
        if last_value is None:
            raise ValueError("GPR XLSX contained no valid GPR values in column B")
        return float(last_value), last_date

    return await asyncio.to_thread(_parse, content)


def _coerce_observation_date(cell: object) -> "Optional[datetime]":
    """Best-effort parse of the GPR sheet's date column.

    openpyxl usually hands back a datetime for a date-formatted cell, but the
    same column has to survive the publisher re-saving the file with a different
    number format, which is exactly the kind of change nobody announces. So a
    raw Excel date SERIAL (44440 -> 2021-09-01), a YYYYMM integer, and several
    string spellings are all handled. Anything else returns None rather than a
    guess — an invented observation date would be worse than no observation
    date, because the whole point of the field is to be trusted.

    NOTE THE FAILURE DIRECTION. None means "this feed publishes no observation
    date I can read", and the caller then falls back to download recency, which
    reports `live`. That is fail-OPEN, and it is a deliberate trade: reporting a
    healthy feed as stale because we could not parse a cell would be its own
    false claim. The mitigation is breadth here, not a different default — every
    spelling this column has ever used is accepted, and the result is asserted in
    `tests/test_feeds.py` so a regression in this function is visible.

    Returns a NAIVE datetime. `feeds.py` compares against `datetime.utcnow()`,
    which is naive, and subtracting an aware datetime from a naive one raises
    TypeError — an exception in a status endpoint rather than a `stale`.
    """
    if isinstance(cell, datetime):
        return cell.replace(tzinfo=None)
    if isinstance(cell, date):
        return datetime(cell.year, cell.month, cell.day)
    if isinstance(cell, (int, float)):
        raw = int(cell)
        if 190001 <= raw <= 299912:  # YYYYMM
            return datetime(raw // 100, raw % 100, 1)
        # Excel serial date (1900 date system): day 1 is 1900-01-01, and Excel
        # wrongly treats 1900 as a leap year, hence the -2 rather than -1.
        if 1 <= raw <= 200000:
            try:
                return datetime(1900, 1, 1) + timedelta(days=raw - 2)
            except (OverflowError, ValueError):
                return None
        return None
    if isinstance(cell, str):
        text = cell.strip()
        for fmt in (
            "%Y-%m-%d", "%Y-%m", "%Y%m", "%d/%m/%Y", "%m/%d/%Y",
            "%Y/%m/%d", "%b-%Y", "%B %Y", "%b %Y", "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=None)
            except ValueError:
                continue
        # "2021M09" — the FRED/ALFRED monthly spelling.
        if len(text) == 7 and text[4] in "Mm":
            try:
                return datetime(int(text[:4]), int(text[5:]), 1)
            except ValueError:
                return None
    return None


async def fetch_gpr() -> float:
    """Latest GPR index value only. See `fetch_gpr_observation` for the date."""
    value, _ = await fetch_gpr_observation()
    return value


# ── ACLED Conflict Event Data ──────────────────────────────────────────────────
# T-03-06: URL hardcoded as constant — never accept user-provided URLs (SSRF mitigation)
ACLED_API_URL = "https://acleddata.com/acled/read"

# Free registration (approval is automatic/instant for an access key):
ACLED_REGISTER_URL = "https://acleddata.com/register/"


def acled_inactive_reason(email: Optional[str], key: Optional[str]) -> Optional[str]:
    """Return a human-readable reason if the ACLED feed cannot run, else None.

    Used by the scheduler and /feeds/status so a missing key surfaces as an
    explicit "inactive — ACLED_KEY not set" state rather than a mystery blank.
    """
    if email and key:
        return None
    return (
        f"{_missing_acled_vars(email, key)} not set — feed inactive. "
        f"Register free at {ACLED_REGISTER_URL}"
    )


def _missing_acled_vars(email: Optional[str], key: Optional[str]) -> str:
    missing = []
    if not email:
        missing.append("ACLED_EMAIL")
    if not key:
        missing.append("ACLED_KEY")
    return " + ".join(missing) if missing else ""


async def fetch_acled(email: str, key: str) -> Optional[dict[str, int]]:
    """Fetch 90-day conflict event counts by country ISO3 code.

    Returns {ISO3: count} e.g. {"SYR": 500, "UKR": 300, "USA": 12}.
    Returns None if ACLED_EMAIL or ACLED_KEY is not configured (graceful degradation).

    Auth: Simple query params — key + email appended to every request.
    NO OAuth, NO token endpoint, NO Bearer header.

    T-03-06: URL is hardcoded as constant — never user-provided (SSRF mitigation).
    T-03-07: email/key are NOT logged at any level. Logger calls use feed name only.
    T-03-09: Only iso3 string field extracted; aggregation counts integers only.
    """
    if not email or not key:
        # DORMANT, and we say so out loud. Returning None here means the feed is
        # inactive — no conflict data enters the optimizer. We never substitute a
        # placeholder or synthetic event count. Register (free) at
        # https://acleddata.com/register/ and set ACLED_EMAIL + ACLED_KEY.
        logger.warning(
            "ACLED feed INACTIVE: %s not set. No conflict data will be used "
            "(no fallback, no synthetic values). Register free at "
            "https://acleddata.com/register/ then set both env vars.",
            _missing_acled_vars(email, key),
        )
        return None  # graceful degradation — credentials not configured

    start = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    end = datetime.utcnow().strftime("%Y-%m-%d")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ACLED_API_URL, params={
            "key": key,
            "email": email,
            "event_date": f"{start}|{end}",
            "event_date_where": "BETWEEN",
            "fields": "iso3|event_type",
            "limit": 5000,
        })
        resp.raise_for_status()

    # T-03-09: Aggregate by country ISO3 only — no raw ACLED text flows to frontend
    events = resp.json().get("data", [])
    counts: dict[str, int] = {}
    for e in events:
        iso3 = e.get("iso3", "")
        if iso3:
            counts[iso3] = counts.get(iso3, 0) + 1
    return counts


# ── IMF PortWatch (ArcGIS Feature Service) ────────────────────────────────────
# T-03-11: URL hardcoded as constant — never accept user-provided URLs (SSRF mitigation)
PORTWATCH_URL = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/Daily_Ports_Data/FeatureServer/0/query"

MONITORED_PORTS = {
    "LA_LB": "port664",
    "NY_NJ": "port815",
    "SAVANNAH": "port1170",
}


async def fetch_portwatch() -> dict[str, float]:
    """Fetch port congestion proxy for 3 US ports.

    Returns {port_code: congestion_ratio} where ratio > 1.0 means congested
    (fewer recent port calls than baseline = ships waiting).

    Congestion = baseline_avg / recent_avg (inverted so higher = worse).
    Neutral = 1.0, congested > 1.0.

    T-03-11: URL hardcoded as constant — never user-provided (SSRF mitigation).
    T-03-16: portcalls values are numeric only; division by zero guarded.
    """
    results = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for code, portid in MONITORED_PORTS.items():
            try:
                params = {
                    "where": f"portid='{portid}'",
                    "outFields": "date,portcalls",
                    "orderByFields": "date DESC",
                    "resultRecordCount": 90,
                    "f": "json",
                }
                resp = await client.get(PORTWATCH_URL, params=params)
                resp.raise_for_status()
                features = resp.json().get("features", [])

                if len(features) < 14:
                    logger.warning("PortWatch %s: insufficient data (%d records)", code, len(features))
                    continue

                calls = [
                    f["attributes"]["portcalls"]
                    for f in features
                    if f.get("attributes", {}).get("portcalls") is not None
                ]

                if len(calls) < 14:
                    logger.warning("PortWatch %s: insufficient valid portcalls (%d records)", code, len(calls))
                    continue

                recent_avg = sum(calls[:7]) / 7
                baseline_avg = sum(calls) / len(calls)

                if baseline_avg > 0 and recent_avg > 0:
                    congestion = baseline_avg / recent_avg
                    results[code] = round(congestion, 3)
                else:
                    results[code] = 1.0  # neutral when no data to compare

            except Exception as exc:
                logger.warning("PortWatch %s fetch failed: %s", code, exc)

    if not results:
        raise ValueError("PortWatch: no port data retrieved for any monitored port")
    return results


# ── FRED TSIFRGHT (Freight Transportation Services Index) ─────────────────────
# T-03-12: URLs hardcoded as constants — never accept user-provided URLs (SSRF mitigation)
FRED_TSIFRGHT_URL = "https://api.stlouisfed.org/fred/series/observations"

# The St. Louis Fed also publishes every series through a PUBLIC CSV endpoint
# that requires NO API key. Same institution, same series, same observations —
# just a different transport. `app/ml/fred_client.py` already relies on it. So
# the freight feed does NOT have to be dormant while FRED_API_KEY is unset: we
# use the keyless endpoint and mark the feed live-but-keyless.
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_FREIGHT_SERIES_ID = "TSIFRGHT"
FRED_API_KEY_URL = "https://fredaccount.stlouisfed.org/apikey"


def _latest_from_fred_csv(raw: str) -> float:
    """Parse fredgraph CSV text and return the last non-missing observation.

    FRED encodes missing observations as ``.``; those rows are skipped.
    """
    latest: Optional[float] = None
    for line in raw.splitlines()[1:]:  # skip header
        parts = line.split(",")
        if len(parts) < 2:
            continue
        value = parts[1].strip()
        if not value or value == ".":
            continue
        try:
            latest = float(value)
        except ValueError:
            continue
    if latest is None:
        raise ValueError(f"FRED {FRED_FREIGHT_SERIES_ID}: no valid observations in CSV")
    return latest


async def fetch_fred_freight_keyless() -> float:
    """Fetch the latest TSIFRGHT value via FRED's public, keyless CSV endpoint.

    Real data from the real source — no API key, no fabrication, no fallback
    constant. Raises on network/parse failure so the caller records an honest
    error rather than a made-up number.
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(
            FRED_CSV_URL,
            params={"id": FRED_FREIGHT_SERIES_ID, "cosd": "2015-01-01"},
        )
        resp.raise_for_status()
        raw = resp.text
    return _latest_from_fred_csv(raw)


async def fetch_fred_freight(api_key: str = "") -> float:
    """Fetch latest TSIFRGHT (Freight Transportation Services Index) from FRED.

    Returns the latest observation value as a float.

    With ``FRED_API_KEY`` set, uses the authenticated JSON API (higher rate
    limits, the documented path). Without it, falls back to FRED's PUBLIC CSV
    endpoint, which serves the identical series with no key — so this feed is
    live either way, and never returns a fabricated value.

    T-03-12: URLs are hardcoded constants — never user-provided (SSRF mitigation).
    T-03-14: API key is NOT logged at any level.
    """
    if not api_key:
        logger.info(
            "FRED freight: FRED_API_KEY not set — using the keyless public CSV "
            "endpoint (same series, same source). Set FRED_API_KEY (free, %s) "
            "for the authenticated API and higher rate limits.",
            FRED_API_KEY_URL,
        )
        return await fetch_fred_freight_keyless()

    params = {
        "series_id": FRED_FREIGHT_SERIES_ID,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "1",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(FRED_TSIFRGHT_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    observations = data.get("observations", [])
    for obs in observations:
        if obs.get("value") != ".":
            return float(obs["value"])

    raise ValueError("FRED TSIFRGHT: no valid observations returned")
