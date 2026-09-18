// GENERATED FILE — DO NOT EDIT BY HAND.
// Regenerate with: cd frontend && node scripts/build-landing-data.mjs
//
// Every value below was read out of a committed artifact by explicit path. The path
// travels with the value and is rendered on the page. Hand-editing a number here is
// exactly the failure this file exists to prevent, and the backend contract test
// (backend/tests/test_landing_data_contract.py) will fail if you do.
//
// Catalogue counts read from: sqlite3

export interface LandingStat {
  id: string
  value: number
  unit: string
  label: string
  detail: string
  source: string
  derived?: boolean
}

export interface LandingProofPoint {
  label: string
  value: string
  detail: string
  source: string
}

export const landingStats: LandingStat[] = [
  {
    "id": "forecast-mape",
    "value": 2.91,
    "unit": "%",
    "label": "demand forecast error, 12 months out-of-sample",
    "detail": "MAPE on held-out data. Tracking signal is −31.1, so the model runs systematically low — stated, not hidden.",
    "source": "docs/forecast_backtest.json → prophet.overall.mape"
  },
  {
    "id": "wape-reduction",
    "value": 38.958333333333336,
    "unit": "%",
    "label": "less forecast error than seasonal-naive",
    "detail": "Derived: (0.048 − 0.0293) / 0.048. Both WAPEs measured on the same held-out horizon.",
    "source": "docs/chronos_benchmark.json → chronos.overall.wape vs seasonal_naive.overall.wape (derived)",
    "derived": true
  },
  {
    "id": "latency",
    "value": 5.4,
    "unit": " ms",
    "label": "median inference, 8.65M-parameter forecaster",
    "detail": "p95 is 6.88 ms. Steady state, after warm-up.",
    "source": "docs/chronos_benchmark.json → chronos.steady_state.median_ms"
  }
]

export const landingProofPoints: LandingProofPoint[] = [
  {
    "label": "Real catalogue, no synthetic data",
    "value": "768 priced parts",
    "detail": "768 of 791 catalogued parts carry a price, quoted by 83 of 92 distributors across 8,176 offers — a static 2024 Nexar/Octopart snapshot, not a live feed. Where real data does not exist, the site says so.",
    "source": "backend/supply_chain.db"
  }
]

export const catalogue = {
  parts: 791,
  distributors: 92,
  offers: 8176,
  partsPriced: 768,
  distributorsQuoting: 83,
}
