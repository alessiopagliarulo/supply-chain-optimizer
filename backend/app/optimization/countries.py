"""Distributor country → ACLED ISO-3166-1 alpha-3 key.

Shared by every code path that builds a `sourcing.Offer` -- the optimize and
stochastic API routers, and the offline benchmark seed script -- so the ACLED
conflict surcharge resolves the same way everywhere. (This module deliberately
names no seed script: T-04-01 forbids that reference inside backend/app/.)
"""

from typing import Optional

# ── Distributor country → ACLED country key ──────────────────────────────────
#
# WHY THIS EXISTS. `sourcing._feed_risk_obj_units` looks the distributor's country up in
# the ACLED 90-day conflict-event counts, and `feeds.fetchers.fetch_acled` aggregates
# those counts by **ISO-3166-1 alpha-3** (`{"USA": 12, "CHN": ..., "UKR": ...}` — it
# reads ACLED's `iso3` field and nothing else). The `Offer.distributor_country` field
# is documented as "ISO country code" but defaults to the alpha-TWO string "US", which
# can never match an alpha-3 key; and `distributors.country` holds human-readable
# names ("USA", "China", "UK", "Germany", ...), of which only "USA" happens to
# coincide with its ISO3 code. So the DB value has to be normalized to ISO3 here or
# every non-US distributor silently scores zero conflict risk.
#
# Covers every value present in the catalogue (verified against
# `SELECT DISTINCT country FROM distributors`: USA, China, UK, Germany, Singapore,
# Japan, Netherlands, Thailand, Poland, Norway, Canada) plus the common aliases and
# alpha-2 codes a future seeder might emit. Anything unrecognised is passed through
# upper-cased: if it is already ISO3 it matches, and if it is not it scores zero,
# which is the honest "no conflict data for this country" answer rather than a
# fabricated one.
_COUNTRY_TO_ISO3 = {
    # names as they appear in the seeded catalogue
    "USA": "USA", "UNITED STATES": "USA", "UNITED STATES OF AMERICA": "USA",
    "US": "USA", "U.S.": "USA", "U.S.A.": "USA",
    "CHINA": "CHN", "CN": "CHN", "PEOPLE'S REPUBLIC OF CHINA": "CHN",
    "HONG KONG": "HKG", "HK": "HKG",
    "TAIWAN": "TWN", "TW": "TWN",
    "UK": "GBR", "UNITED KINGDOM": "GBR", "GB": "GBR", "GREAT BRITAIN": "GBR",
    "GERMANY": "DEU", "DE": "DEU",
    "SINGAPORE": "SGP", "SG": "SGP",
    "JAPAN": "JPN", "JP": "JPN",
    "NETHERLANDS": "NLD", "NL": "NLD", "THE NETHERLANDS": "NLD",
    "THAILAND": "THA", "TH": "THA",
    "POLAND": "POL", "PL": "POL",
    "NORWAY": "NOR", "NO": "NOR",
    "CANADA": "CAN", "CA": "CAN",
    # further alpha-2 aliases for countries a reseeded catalogue could plausibly add
    "SOUTH KOREA": "KOR", "KOREA": "KOR", "KR": "KOR",
    "INDIA": "IND", "IN": "IND",
    "MEXICO": "MEX", "MX": "MEX",
    "FRANCE": "FRA", "FR": "FRA",
    "ITALY": "ITA", "IT": "ITA",
    "SPAIN": "ESP", "ES": "ESP",
    "ISRAEL": "ISR", "IL": "ISR",
    "MALAYSIA": "MYS", "MY": "MYS",
    "PHILIPPINES": "PHL", "PH": "PHL",
    "VIETNAM": "VNM", "VN": "VNM",
    "AUSTRALIA": "AUS", "AU": "AUS",
    "SWITZERLAND": "CHE", "CH": "CHE",
    "SWEDEN": "SWE", "SE": "SWE",
    "IRELAND": "IRL", "IE": "IRL",
    "AUSTRIA": "AUT", "AT": "AUT",
    "BELGIUM": "BEL", "BE": "BEL",
    "CZECH REPUBLIC": "CZE", "CZECHIA": "CZE", "CZ": "CZE",
    "TURKEY": "TUR", "TURKIYE": "TUR", "TR": "TUR",
    "BRAZIL": "BRA", "BR": "BRA",
}


def _acled_country_key(country: Optional[str]) -> str:
    """Map a `distributors.country` value to the ISO3 key the ACLED feed is keyed by.

    Returns "USA" when the column is empty — the catalogue's own column default is
    "USA" and the overwhelming majority of rows are US warehouses, so an unset value
    is treated the same way the schema treats it. Unknown non-empty values are
    upper-cased and passed through (see `_COUNTRY_TO_ISO3`).
    """
    raw = (country or "").strip()
    if not raw:
        return "USA"
    return _COUNTRY_TO_ISO3.get(raw.upper(), raw.upper())

