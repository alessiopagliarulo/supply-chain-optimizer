/**
 * Newsvendor API client — SEPARATE axios instance, because one of these
 * endpoints is slow.
 * ====================================================================
 *
 * `services/api.ts` creates one shared axios instance with a global
 * `timeout: 30000`. `GET /newsvendor/evaluation` cannot be relied on to answer
 * in 30s. Every configuration this page can ask for — all 72 of them — is now
 * served from the committed evaluation artifact and comes back in milliseconds,
 * so the ordinary path is fast. But the endpoint keeps a real fallback: when the
 * artifact is absent from the deployment, unreadable, or describes a different
 * computation from the server's own, it re-runs the whole panel — 2,646 balanced
 * series x 3 rolling origins x 6 forecast methods, then a 5,000-replication
 * paired bootstrap — and the server's own `wall_seconds` for that measured
 * **106.6s** against the live deployment on 2026-08-30 (259.9s on a cold
 * container). On top of that, Render's free tier adds a ~100s spin-up if the
 * container is asleep.
 *
 * Through the shared client that request aborts with `ECONNABORTED` while the
 * server happily finishes the evaluation and caches it — the UI would report a
 * failure for a computation that in fact succeeded. So this module owns its own
 * instance, and `evaluation()` raises the budget again on top of that.
 *
 * Auth behaviour is IMPORTED, not re-implemented: `tokenStorage` (cookie with a
 * localStorage fallback) and `isTimeoutError` both come from `services/api.ts`,
 * so this client cannot drift from the shared one. `/newsvendor/*` is a public,
 * unauthenticated router — the token is attached anyway so the two clients keep
 * behaving identically if that ever changes.
 *
 * EVERY TYPE BELOW WAS VERIFIED AGAINST THE LIVE API, not inferred from the
 * router source: https://supply-chain-api-qy8x.onrender.com/api/v1, 2026-08-28.
 */
import axios from 'axios';
import { isTimeoutError, tokenStorage } from './api';

const API_BASE_URL = import.meta.env.VITE_API_URL || '/api/v1';

export { isTimeoutError };

/**
 * `/assumptions` and `/decision` are closed form — one smoothing pass and one
 * inverse-cdf lookup, tens of milliseconds warm. The budget here is entirely
 * about surviving a cold Render container, so it matches the shared client's
 * own cold-start figure.
 */
export const DECISION_TIMEOUT_MS = 150_000;

/**
 * The panel evaluation. Milliseconds for a published configuration; 106.6s
 * measured warm-container-cold-cache for any other, plus room for a spin-up.
 * KEPT AT 240s even though the request this app makes on mount is now instant:
 * the budget has to cover the slowest thing a user can ask for from this page,
 * not the fastest, and a timeout here must still mean the request really died.
 */
export const EVALUATION_TIMEOUT_MS = 240_000;

const newsvendorApi = axios.create({
  baseURL: API_BASE_URL,
  headers: { 'Content-Type': 'application/json' },
  timeout: DECISION_TIMEOUT_MS,
});

// Same token read as the shared client, via the same helper.
newsvendorApi.interceptors.request.use((config) => {
  const token = tokenStorage.get();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// Same 401 handling as the shared client, including the "don't hard-reload the
// login page itself" guard.
newsvendorApi.interceptors.response.use(
  (r) => r,
  (error) => {
    if (error.response?.status === 401) {
      tokenStorage.clear();
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  }
);

// NOTE: deliberately NO automatic retry. Replaying a timed-out `/evaluation`
// would start a second full panel run on a backend already busy with the first.
// The page surfaces the timeout and offers a button; by then the server's LRU
// usually has the answer.

// ── Shared shapes ────────────────────────────────────────────────────────────

/** The six predictive laws the decision can run on. */
export const FORECAST_METHODS = [
  'tsb',
  'sba',
  'croston',
  'climatology',
  'naive_last',
  'zero',
] as const;
export type ForecastMethod = (typeof FORECAST_METHODS)[number];

/** How a shortage is priced. `line_down` is a SENSITIVITY, not the default. */
export const SHORTAGE_MODES = ['expedite', 'line_down'] as const;
export type ShortageMode = (typeof SHORTAGE_MODES)[number];

/**
 * The cost asymmetry. `critical_ratio` is tau = Cu / (Cu + Co) — the fractile of
 * the predictive demand law to order at. `cost_asymmetry` is Cu / Co, a RATIO
 * (render it with a multiplication sign, never a percent).
 *
 * `resolution_warning` is non-null exactly when tau sits above what the panel's
 * longest 45-month training window can resolve (1/45 = 0.022). It arrives from
 * the server; it is not computed here.
 */
export interface NewsvendorCosts {
  unit_price_usd: number;
  review_period_months: number;
  holding_rate_annual: number;
  shortage_mode: string;
  shortage_multiple: number;
  expedite_freight_usd_per_unit: number;
  /** Cu — cost of being one unit short, USD per unit. */
  underage_usd_per_unit: number;
  /** Co — cost of holding one unsold unit for one review period, USD per unit. */
  overage_usd_per_unit: number;
  critical_ratio: number;
  cost_asymmetry: number;
  resolution_warning: string | null;
  derivation: string;
}

// ── GET /newsvendor/assumptions ──────────────────────────────────────────────

export interface AssumptionInput {
  value: number;
  source: string;
  used_via?: string;
  justification?: string;
  applies_when?: string;
  why_excluded?: string;
}

export interface AssumptionsResponse {
  critical_fractile: NewsvendorCosts;
  inputs: {
    holding_rate_annual: AssumptionInput;
    expedite_premium: AssumptionInput;
    stockout_escalation_multiple: AssumptionInput;
    excluded_fixed_expedite_charge: AssumptionInput;
  };
  derivation: {
    formula: string;
    why: string;
    price_invariance: string;
    dual_identity: string;
  };
  caveats: string[];
}

export interface AssumptionsParams {
  unit_price_usd?: number;
  review_period_months?: number;
  shortage_mode?: ShortageMode;
  expedite_freight_usd_per_unit?: number;
}

// ── POST /newsvendor/decision ────────────────────────────────────────────────

/**
 * Exactly one of `demand_history` / `series`. The server rejects both or neither
 * with a 422, and caps a history at 12–600 observations.
 */
export interface DecisionRequest {
  demand_history?: number[];
  series?: string;
  train_periods?: number;
  method?: ForecastMethod;
  unit_price_usd?: number;
  review_period_months?: number;
  shortage_mode?: ShortageMode;
  expedite_freight_usd_per_unit?: number;
}

export interface DecisionResponse {
  /** q* — the smallest integer q with F(q) >= tau. Returned as a float. */
  order_quantity: number;
  costs: NewsvendorCosts;
  distribution_source: 'parametric' | 'empirical' | 'degenerate' | string;
  forecast_method: string;
  expected: {
    expected_units_short: number;
    expected_units_held: number;
    expected_underage_usd: number;
    expected_overage_usd: number;
    expected_total_usd: number;
    /** P(demand <= q) over ONE review period under the predictive law. */
    cycle_service_level: number;
    /** Fraction of UNITS met from stock over the same window. Not the same number. */
    fill_rate: number;
    expected_demand: number;
  };
  comparisons: {
    order_point_forecast: number;
    cost_of_ordering_point_forecast: number;
    /** mu + z_tau * sigma. Continuous — the server does not round it. */
    order_normal_approximation: number;
    cost_of_normal_approximation: number;
    /** Scarf (1958) min-max. The API returns the quantity but NOT its cost. */
    order_scarf_minmax: number;
    predictive_mean: number;
    predictive_sd: number;
  };
  caveats: string[];
  input: {
    kind: 'panel_series' | 'caller_history' | string;
    series: string | null;
    n_periods: number;
    method: string;
    review_period_months: number;
    unit_price_usd: number;
    shortage_mode: string;
    observed_mean_per_month: number;
    observed_nonzero_fraction: number;
  };
  demand_distribution: {
    family: string;
    source: string;
    driving_model: string;
    periods_aggregated: number;
    mean: number;
    sd: number;
    p_zero: number;
    support_max: number;
    /** Keys are `q01`,`q05`,`q10`,`q25`,`q50`,`q75`,`q90`,`q95`,`q99`. */
    quantiles: Record<string, number>;
  };
}

// ── GET /newsvendor/evaluation ───────────────────────────────────────────────

export interface PolicyResult {
  description: string;
  mean_cost_usd_per_sku_period: number;
  median_cost_usd_per_sku_period: number;
  mean_order_quantity: number;
  n_series: number;
}

/**
 * One paired bootstrap comparison. `mean_difference` is POSITIVE when the
 * newsvendor policy is cheaper. `significant` is the API's own verdict on
 * whether the 95% CI excludes zero — do not re-derive it in the UI.
 */
export interface PairedComparison {
  n: number;
  n_boot: number;
  mean_difference: number;
  ci95_low: number;
  ci95_high: number;
  significant: boolean;
  win_rate: number;
  tie_rate: number;
  loss_rate: number;
  baseline_mean_cost: number;
  policy_mean_cost: number;
  pct_cost_reduction: number;
}

export interface EvaluationResponse {
  costs: NewsvendorCosts;
  protocol: {
    panel: string;
    split: string;
    horizon_months: number;
    n_origins: number;
    train_sizes: number[];
    review_period_months: number;
    blocks_per_origin: number;
    seasonality: number;
    replication_unit: string;
    balance_rule: string;
    forecast_method: string;
    distribution_source: string;
    permutation_control: boolean;
  };
  panel: {
    n_series_available: number;
    n_series_considered: number;
    n_series_scored: number;
    n_series_dropped_unbalanced: number;
    /** Series whose fitted law failed the E[pmf] == point-forecast invariant. */
    n_series_dropped_pmf_invariant: number;
    n_decisions: number;
  };
  policies: Record<string, PolicyResult>;
  paired_vs_newsvendor: Record<string, PairedComparison>;
  baselines_beaten: Record<string, boolean>;
  toughest_baseline: string;
  paired_vs_toughest_baseline: PairedComparison;
  /**
   * Every method given the SAME newsvendor rule. `winner_changed` is true when
   * the MASE ranking and the decision-cost ranking disagree at the top — which
   * is the whole argument this page exists to make.
   */
  method_leaderboard: {
    decision_cost_usd_per_sku_period: Record<string, number>;
    mase_mean: Record<string, number>;
    order_by_decision_cost: string[];
    order_by_mase: string[];
    winner_changed: boolean;
    note: string;
  };
  caveats: string[];
  /**
   * Fails closed, and it DOES fail — at `shortage_mode=line_down` the margin
   * over the toughest baseline stops excluding zero. Read this before quoting
   * anything else in the payload.
   */
  ship_gate: {
    policy: string;
    toughest_baseline: string;
    baselines_beaten: Record<string, boolean>;
    paired: PairedComparison;
    passed: boolean;
    reason: string;
  };
  /** How long THIS request took on the server — not how long the evaluation took. */
  wall_seconds: number;
  /**
   * Where the numbers above came from. EVERY configuration this endpoint can be
   * asked for — 6 forecast methods × review periods 1..6 × 2 shortage modes = 72 —
   * is published by `seeds.run_newsvendor` into the committed `docs/newsvendor.json`
   * and served from it, so no request recomputes the panel. The served block is the
   * same `run_panel_evaluation` output the endpoint would otherwise compute; a
   * backend test re-runs it and asserts every leaf is equal. `recomputed` goes true
   * only in the degraded case (artifact absent, unreadable, or describing a
   * different computation), which measured 106.6 s against the live API on
   * 2026-08-30.
   */
  computation: {
    recomputed: boolean;
    source: string;
    generator: string | null;
    artifact_generated_at_utc: string | null;
    artifact_git_commit: string | null;
    artifact_wall_seconds: number | null;
    equality_guarantee: string;
    why: string;
  };
  units: {
    cost: string;
    order_quantity: string;
    mean_difference: string;
  };
  reproduce: string;
}

export interface EvaluationParams {
  forecast_method?: ForecastMethod;
  review_period_months?: number;
  shortage_mode?: ShortageMode;
}

// ── Calls ────────────────────────────────────────────────────────────────────

export const newsvendorAPI = {
  /** The cost asymmetry behind every order quantity. Cheap. */
  assumptions: (params?: AssumptionsParams) =>
    newsvendorApi.get<AssumptionsResponse>('/newsvendor/assumptions', { params }),

  /** One order quantity for one demand history. Closed form, no solver. */
  decision: (body: DecisionRequest) =>
    newsvendorApi.post<DecisionResponse>('/newsvendor/decision', body),

  /**
   * Used to be the expensive one — a full panel re-run per (method, review period,
   * mode), 106.6 s on the deployed 0.5-CPU worker. All 72 reachable settings are now
   * precomputed into `docs/newsvendor.json` and served from it, so this is a read.
   * `EVALUATION_TIMEOUT_MS` stays generous for the degraded case where the artifact
   * is missing and the server falls back to computing.
   */
  evaluation: (params?: EvaluationParams) =>
    newsvendorApi.get<EvaluationResponse>('/newsvendor/evaluation', {
      params,
      timeout: EVALUATION_TIMEOUT_MS,
    }),
};

export default newsvendorApi;
