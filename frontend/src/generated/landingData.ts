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
    "id": "cost-edge",
    "value": 18.79,
    "unit": "%",
    "label": "cheaper than a matched-pool heuristic",
    "detail": "CP-SAT against a greedy buyer shopping the same distributor pool — the honest comparison, not a strawman.",
    "source": "docs/benchmark_results.json → headline.primary_save_pct"
  },
  {
    "id": "cvar-leverage",
    "value": 4.266,
    "unit": "×",
    "label": "tail risk removed per $1 of expected cost",
    "detail": "At the knee of the CVaR frontier: every extra dollar budgeted buys $4.27 off the worst-case outcome.",
    "source": "docs/cvar_frontier.json → primary.x10000.knee.vs_risk_neutral.usd_of_cvar_removed_per_usd_of_expected_cost"
  },
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
    "id": "shortfall",
    "value": 55.555600000000005,
    "unit": " pts",
    "label": "shortfall risk removed by sourcing across 4 distributors",
    "detail": "Under targeted disruption, vs single-sourcing. 95% CI [25.3, 85.5] excludes zero across 9 BOMs.",
    "source": "docs/diversification_frontier.json → frontier[3].delta_targeted_expected_shortfall_vs_k1"
  },
  {
    "id": "latency",
    "value": 2.24,
    "unit": " ms",
    "label": "median inference, 8.65M-parameter forecaster",
    "detail": "p95 is 2.43 ms. Steady state, after warm-up.",
    "source": "docs/chronos_benchmark.json → chronos.steady_state.median_ms"
  }
]

export const landingProofPoints: LandingProofPoint[] = [
  {
    "label": "CP-SAT solves converged",
    "value": "347 of 387",
    "detail": "337 proved optimal outright; the rest within a 5% gap. 40 did not converge and are excluded from every published figure.",
    "source": "docs/cvar_frontier.json → solve_quality"
  },
  {
    "label": "Scenario distribution enumerated exactly",
    "value": "64 atoms",
    "detail": "Residual mass 0 — the full support, so no sampling error in the risk numbers.",
    "source": "docs/cvar_frontier.json → calibration.scenario_support"
  },
  {
    "label": "MILP costs independently reproduced",
    "value": "9 of 9",
    "detail": "Re-solved from scratch and matched to $0.01.",
    "source": "docs/diversification_frontier.json → run5_reproduction_check"
  },
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

// Kept verbatim from the artifact. The 47.25% figure was published once as the
// optimizer's edge, which it is not; if it is ever shown again it carries this text.
export const naiveBaselineCaveat = "Against a naive per-line-cheapest baseline that shops the FULL international catalogue the same plans are 47.25% cheaper. That figure is kept for contrast and must always be labelled as 'vs a naive, globally-shopping baseline' — it is not the optimizer's edge."
