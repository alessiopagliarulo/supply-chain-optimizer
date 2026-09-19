"""
Pin an independent city centroid for every city named in DISTRIBUTOR_LOCATIONS.

The distributor coordinates in `seeds/seed_db.py` were compiled by hand, and an
audit on 2026-09-19 found six of them plotted 45-112 km from the city they
were labelled with (e.g. "Colchester, Essex" sat near Crawley). The labels were
right; the numbers were not. This script fetches each labelled city from
OpenStreetMap Nominatim and writes the result to
`seeds/data/distributor_city_centroids.json`, which
`tests/test_distributor_catalogue_api.py` uses to hold every seeded coordinate
to its own label.

The output is committed, so tests never touch the network. Re-run only when a
city label in DISTRIBUTOR_LOCATIONS changes:

    cd backend
    python3 -m seeds.geocode_distributor_cities

Data (c) OpenStreetMap contributors, ODbL. Nominatim usage policy: at most one
request per second and an identifying User-Agent, both honoured below.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seeds.seed_db import DISTRIBUTOR_LOCATIONS  # noqa: E402

OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "distributor_city_centroids.json")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "supply-chain-optimizer/1.0 (github.com/alessiopagliarulo/supply-chain-optimizer)"


def city_key(loc: dict) -> str:
    """The label a coordinate is held to: 'city|state|country'."""
    return f"{loc['city']}|{loc['state']}|{loc['country']}"


def _fetch(city: str, state: str, country: str) -> dict:
    q = urllib.parse.urlencode({
        "q": f"{city}, {state}, {country}",
        "format": "json",
        "limit": 1,
    })
    req = urllib.request.Request(f"{NOMINATIM}?{q}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        hits = json.load(resp)
    if not hits:
        raise SystemExit(f"Nominatim returned nothing for {city}, {state}, {country}")
    hit = hits[0]
    return {
        "lat": round(float(hit["lat"]), 4),
        "lng": round(float(hit["lon"]), 4),
        "osm_display_name": hit["display_name"],
        "osm_type": hit["osm_type"],
        "osm_id": int(hit["osm_id"]),
    }


def main() -> None:
    labels = sorted({city_key(loc) for loc in DISTRIBUTOR_LOCATIONS.values()})
    centroids = {}
    for i, key in enumerate(labels):
        if i:
            time.sleep(1.1)
        city, state, country = key.split("|")
        centroids[key] = _fetch(city, state, country)
        print(f"  {key:<45} {centroids[key]['lat']:>9}, {centroids[key]['lng']:>9}  {centroids[key]['osm_display_name'][:60]}")

    with open(OUT_PATH, "w") as f:
        json.dump({
            "source": "OpenStreetMap Nominatim (https://nominatim.openstreetmap.org), data (c) OpenStreetMap contributors, ODbL",
            "retrieved": date.today().isoformat(),
            "generator": "backend/seeds/geocode_distributor_cities.py",
            "centroids": centroids,
        }, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {len(centroids)} city centroids to {OUT_PATH}")


if __name__ == "__main__":
    main()
