"""
Where the component / distributor / offer catalogue comes from, stated once.

`seeds/seed_db.py` loads the catalogue from these constants and
`GET /api/v1/catalogue/provenance` serves them, so the seeder and the API can
never describe the data differently. The long-form record, including how each
fact was verified, is docs/DATA_PROVENANCE.md sections 1 and 2.

The short version every consumer must respect: this is a FROZEN 2024 SNAPSHOT,
not a live feed. Prices, stock and suppliers are real observations from 2024,
not current quotes.
"""

DATASET_REPO = "mdnh/electronic-components-supply-chain"
DATASET_URL = f"https://huggingface.co/datasets/{DATASET_REPO}"
DATASET_LICENSE = "CC-BY-4.0"
DATASET_UPLOADER = "mdnh (independent HuggingFace user; not affiliated with Nexar or Octopart)"
DATASET_ORIGINAL_SOURCE = "Nexar API (which itself aggregates Octopart data)"
DATASET_COLLECTED = "2024 (per dataset card: 404 general components + 387 telecom components)"
DATASET_PUBLISHED = "2026-01-01 (HuggingFace createdAt timestamp)"
DATASET_RETRIEVED = "2026-07-12 (this audit; original seed date not recorded)"
DATASET_ROW_COUNT = 791  # matches dataset card; DB offer count varies (offers with price<=0 are dropped)

SNAPSHOT_YEAR = 2024
IS_LIVE = False

# Distributor coordinates are one point per HQ / warehouse CITY, not a street
# address. Each is held to within COORDINATE_TOLERANCE_KM of an independent
# OpenStreetMap point for the city it is labelled with
# (seeds/data/distributor_city_centroids.json, tests/test_distributor_catalogue_api.py).
COORDINATE_PRECISION = "city"
COORDINATE_TOLERANCE_KM = 25.0
COORDINATE_SOURCE = (
    "Hand-compiled HQ/warehouse city per distributor (company websites, SEC filings, "
    "press releases); coordinates checked against OpenStreetMap Nominatim city points"
)

PROVENANCE_DOC = "docs/DATA_PROVENANCE.md"
PROVENANCE_DOC_URL = (
    "https://github.com/alessiopagliarulo/supply-chain-optimizer/blob/main/docs/DATA_PROVENANCE.md"
)
