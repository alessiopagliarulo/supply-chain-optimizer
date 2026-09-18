import axios, { type InternalAxiosRequestConfig } from 'axios';
import Cookies from 'js-cookie';

/**
 * Absolute in production (Render sets VITE_API_URL to the API origin + /api/v1),
 * relative in local dev behind Vite's proxy. Exported so the cold-start warm-up in
 * ./warmup.ts derives its probe URL from the same value instead of a second copy.
 */
export const API_BASE_URL = import.meta.env.VITE_API_URL || '/api/v1';

/**
 * Render's free tier spins the backend down after ~15 minutes idle, and this repo
 * measures the resulting cold start at ~100s (see scripts/verify_backend.py and the
 * README's "allow up to ~2 minutes" note). The old blanket 30s axios timeout aborted
 * that very first request every single time, so a visitor arriving at a sleeping
 * backend saw "Demo login failed" — the request never actually failed, we hung up on
 * it. Auth calls get a cold-start-sized budget; GETs get one automatic retry.
 */
export const COLD_START_TIMEOUT_MS = 150000;
const DEFAULT_TIMEOUT_MS = 30000;

/** Rough "the backend is probably still waking up" test for error handling in pages. */
export const isTimeoutError = (err: unknown): boolean => {
  const e = err as { code?: string; response?: unknown; message?: string };
  if (e?.response) return false;
  return e?.code === 'ECONNABORTED' || e?.code === 'ETIMEDOUT' || e?.message === 'Network Error';
};

const TOKEN_KEY = 'access_token';

/**
 * Token storage with a localStorage fallback.
 *
 * The JWT used to live *only* in a js-cookie cookie. Any browser that refuses
 * `document.cookie` writes — Safari with "Block all cookies", hardened privacy
 * extensions, some locked-down corporate profiles — dropped it silently: login
 * "succeeded", the request interceptor then sent no Authorization header, /auth/me
 * returned 401 and bounced the user straight back to /login with no error shown.
 * Writing to both places (and reading either) makes auth survive blocked cookies.
 */
/** Last resort: survives a locked-down browser for the life of the tab. */
let inMemoryToken: string | undefined;

export const tokenStorage = {
  get(): string | undefined {
    const fromCookie = Cookies.get(TOKEN_KEY);
    if (fromCookie) return fromCookie;
    try {
      const fromLocal = window.localStorage.getItem(TOKEN_KEY);
      if (fromLocal) return fromLocal;
    } catch {
      /* storage unreadable — fall through to the in-memory copy */
    }
    return inMemoryToken;
  },
  set(token: string): void {
    inMemoryToken = token;
    try {
      Cookies.set(TOKEN_KEY, token, {
        expires: 7,
        sameSite: 'Lax',
        secure: window.location.protocol === 'https:',
      });
    } catch {
      /* cookies blocked — localStorage below is the fallback */
    }
    try {
      window.localStorage.setItem(TOKEN_KEY, token);
    } catch {
      /* storage blocked (private mode quota) — the cookie above is the fallback */
    }
  },
  clear(): void {
    inMemoryToken = undefined;
    try {
      Cookies.remove(TOKEN_KEY);
    } catch {
      /* nothing to remove */
    }
    try {
      window.localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* nothing to remove */
    }
  },
};

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: { 'Content-Type': 'application/json' },
  timeout: DEFAULT_TIMEOUT_MS,
});

api.interceptors.request.use((config) => {
  const token = tokenStorage.get();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// Cold-start retry. Only GETs are retried automatically — replaying a POST could
// duplicate a cart item or an optimization run. The auth POSTs don't need this
// because they already carry COLD_START_TIMEOUT_MS on the first attempt.
type RetryableConfig = InternalAxiosRequestConfig & { __coldStartRetry?: boolean };
api.interceptors.response.use(undefined, (error) => {
  const config = error?.config as RetryableConfig | undefined;
  if (
    config &&
    isTimeoutError(error) &&
    !config.__coldStartRetry &&
    (config.method ?? 'get').toLowerCase() === 'get'
  ) {
    config.__coldStartRetry = true;
    config.timeout = COLD_START_TIMEOUT_MS;
    return api.request(config);
  }
  return Promise.reject(error);
});

api.interceptors.response.use(
  (r) => r,
  (error) => {
    if (error.response?.status === 401) {
      tokenStorage.clear();
      // Don't hard-reload the login page itself: that wipes the error message the
      // user needs to read and makes a failed demo login look like a dead button.
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  }
);

// ── Auth ──────────────────────────────────────────────────────────────────────
// Shape verified against the live `GET /auth/me` payload (2026-08).
export interface AuthUser {
  id: number;
  email: string;
  factory_name: string;
  latitude: number;
  longitude: number;
}

// Every auth call is potentially the first request against a sleeping free-tier
// backend, so all four get the cold-start timeout rather than the 30s default.
const authOpts = { timeout: COLD_START_TIMEOUT_MS };

export const authAPI = {
  register: (data: { email: string; password: string; factory_name: string; latitude: number; longitude: number }) =>
    api.post<{ access_token: string }>('/auth/register', data, authOpts),
  login: (data: { email: string; password: string }) =>
    api.post<{ access_token: string }>('/auth/login', data, authOpts),
  demoLogin: () => api.post<{ access_token: string }>('/auth/demo', undefined, authOpts),
  me: () => api.get<AuthUser>('/auth/me', authOpts),
};

// ── Components ───────────────────────────────────────────────────────────────
export const componentsAPI = {
  list: (params?: { category?: string; manufacturer?: string; search?: string }) =>
    api.get('/components', { params }),
  categories: () => api.get('/components/categories'),
  manufacturers: () => api.get('/components/manufacturers'),
  stats: () => api.get('/components/stats'),
  get: (id: number) => api.get(`/components/${id}`),
  offers: (id: number, params?: { sort_by?: string; domestic_only?: boolean }) =>
    api.get(`/components/${id}/offers`, { params }),
};

// ── Distributors ─────────────────────────────────────────────────────────────
export const distributorsAPI = {
  list: (params?: { domestic_only?: boolean }) => api.get('/distributors', { params }),
  get: (id: number) => api.get(`/distributors/${id}`),
};

// ── Cart ──────────────────────────────────────────────────────────────────────
export const cartAPI = {
  get: () => api.get('/cart'),
  add: (data: { component_id: number; distributor_id: number; quantity: number; unit_price?: number }) =>
    api.post('/cart', data),
  remove: (itemId: number) => api.delete(`/cart/${itemId}`),
  clear: () => api.delete('/cart'),
};

// ── Feeds ─────────────────────────────────────────────────────────────────────
export const feedsAPI = {
  getStatus: () => api.get('/feeds/status'),
};

// ── Live Prices ────────────────────────────────────────────────────────────────
// Backing endpoints: backend/app/api/live_prices.py. Real multi-distributor
// live pricing (Nexar / DigiKey / OEMsecrets / TrustedParts) fetched on demand
// against the static HuggingFace snapshot — see SchedulerPage.tsx's
// "Refresh Live Price" panel.
export interface LiveOffer {
  distributor: string;
  sku: string | null;
  stock: number;
  moq: number;
  price: number;
  currency: string;
  is_authorized: boolean;
  price_breaks: Array<Record<string, unknown>>;
  lead_time_weeks: number | null;
  lifecycle_status: string | null;
  datasheet_url: string | null;
  source: string;
}

// Per-source outcome for a live-pricing fetch (backend: SourceReport in
// live_prices.py). Lets the UI show *why* only 2 of 4 sources answered —
// "not_configured" vs "errored" vs "ok with N offers" — instead of collapsing
// every non-answer into a single generic "sources_used" list.
export type LiveSourceStatus = 'ok' | 'error' | 'skipped' | 'not_configured';

export interface LiveSourceReport {
  name: string;
  configured: boolean;
  status: LiveSourceStatus;
  offer_count: number;
  error: string | null;
}

export interface LivePriceResponse {
  mpn: string;
  total_offers: number;
  sources_used: string[];
  offers: LiveOffer[];
  cached: boolean;
  sources: LiveSourceReport[];
  all_sources_failed: boolean;
}

export interface BomPriceResponse {
  results: Record<string, LivePriceResponse>;
  total_mpns: number;
  sources_used: string[];
}

export interface SyncPricesResponse {
  mpn?: string;
  live_offers_found?: number;
  db_offers_updated?: number;
  db_offers_created?: number;
  sources?: string[];
  updated?: number;
  message?: string;
}

export const livePricesAPI = {
  get: (mpn: string) => api.get<LivePriceResponse>(`/live-prices/${encodeURIComponent(mpn)}`),
  bom: (items: Array<{ mpn: string; quantity?: number }>) =>
    api.post<BomPriceResponse>('/live-prices/bom', { items }),
  sync: (mpn: string) => api.post<SyncPricesResponse>(`/live-prices/${encodeURIComponent(mpn)}/sync`),
};

// ── Demand Benchmark ────────────────────────────────────────────────────────────
// Backing endpoint: backend/app/api/demand.py — GET /demand/benchmark. Replaces
// the retired per-part forecasts API surface (Prophet fits whose demand
// magnitudes were derived from inventory position and a risk score — demand
// inferred from stock, causally backwards — and unscoreable in principle;
// removed in migration 0008). This is a fleet-wide benchmark of intermittent-
// demand *methods* on the real Monash car-parts panel, not a per-part demand
// forecast — there is no public per-SKU demand series for electronic
// components. Returns 503 with `detail` when the artifact isn't generated in
// this deployment.
export interface DemandDatasetInfo {
  name: string;
  source: string;
  license: string;
  n_series: number;
  series_length: number;
  frequency: string;
  nonzero_fraction: number;
  why_this_panel?: string;
  [key: string]: unknown;
}

export interface DemandMethodRow {
  name: string;
  family: string;
  assumption: string;
  mase_mean: number;
  mase_median: number;
  rmsse_mean: number;
  crps_mean: number;
  spl_mean: number;
  //: Mean Friedman rank (1 = best) — the ranking the MCB test compares.
  rank_mase: number;
  rank_rmsse: number;
  rank_crps: number;
  rank_spl: number;
}

export interface DemandMcbSummary {
  metric: string;
  n_series: number;
  alpha: number;
  friedman_chi2: number;
  friedman_p: number;
  critical_difference: number;
  mean_ranks: Record<string, number>;
  cliques: string[][];
}

export interface DemandSignificanceRow {
  test: string;
  a: string;
  b: string;
  statistic: number;
  p_value: number;
  note: string;
}

export interface DemandBenchmarkResponse {
  headline: string;
  generated_utc: string;
  git_sha: string | null;
  dataset: DemandDatasetInfo;
  protocol: Record<string, unknown>;
  scoring: Record<string, unknown>;
  methods: DemandMethodRow[];
  //: True when the leaderboard order differs between point and proper scoring.
  ranking_changed: boolean;
  winner_changed: boolean;
  point_winner: string;
  distributional_winner: string;
  mcb: DemandMcbSummary[];
  significance: DemandSignificanceRow[];
  artifact: string;
  reproduce_command: string;
}

export const demandAPI = {
  benchmark: () => api.get<DemandBenchmarkResponse>('/demand/benchmark'),
};

// ── Resilience Scenarios ──────────────────────────────────────────────────────
export interface ScenarioResponse {
  baseline_cost_usd: number;
  scenario_cost_usd: number;
  cost_delta_pct: number;
  baseline_eta_days: number;
  scenario_eta_days: number;
  eta_delta_days: number;
  baseline_risk_score: number;
  scenario_risk_score: number;
  risk_delta: number;
  // Dollar-denominated tail-risk framing (P3): CVaR-95 cost multiplier of the
  // worst-5% Monte Carlo scenarios, and the extra USD it puts at risk on this BOM.
  baseline_cvar_95: number;
  procurement_spend_at_risk_usd: number;
  // Served prose describing what the two figures above/below MEAN. Both have
  // always been on the wire (`ScenarioResponse` in backend/app/api/resilience.py);
  // the UI kept its own retyped copies instead, which is exactly how a page ends
  // up publishing a sentence the API no longer agrees with. Optional because a
  // response cached before they were added can still be served.
  spend_at_risk_basis?: string;
  eta_basis?: string;
  baseline_fulfillment_p10: number;
  baseline_fulfillment_p50: number;
  baseline_fulfillment_p90: number;
  scenario_fulfillment_p10: number;
  scenario_fulfillment_p50: number;
  scenario_fulfillment_p90: number;
  affected_bom_ids: number[];
  affected_suppliers: string[];
  // BOM-WIDE alternative suppliers: the distributors still serving ANY line of the
  // BOM. Do NOT attach this to a single affected row — see `affected_components`.
  alternative_suppliers: Array<{ name: string; lead_time_days: number }>;
  // Per-component detail for the BOM impact table: one entry per `affected_bom_ids`
  // id, in the same order, with the component's REAL mpn and current supplier and
  // the alternatives that can still serve THAT line under the scenario (empty for a
  // line the scenario orphans). Optional only because a response cached before this
  // field existed can still be served for up to an hour.
  affected_components?: AffectedComponentDetail[];
  // Stated when the caller sent `items`; "assumed_one_unit_per_line" when it sent
  // only `bom_component_ids`, in which case every dollar figure is a ONE-UNIT
  // figure, not a build.
  quantity_source?: 'explicit' | 'assumed_one_unit_per_line';
  total_units?: number;
  // Whether the BOM is structurally exposed to THIS scenario at all — a diversified
  // BOM can legitimately show zero fulfillment impact when one distributor goes down
  // because every line has an alternate. Optional only because a response cached
  // before this field existed can still be served for up to an hour.
  hedging?: HedgingSummary;
  // Line-by-line re-pricing against the offers that survive the scenario. This is
  // where the real cost lands when the BOM is fully hedged: the Monte Carlo's
  // CVaR-95 spend-at-risk is correctly $0 (nothing becomes unavailable), but every
  // substituted line still costs more than its baseline offer.
  cost_substitution?: CostSubstitution;
}

export interface HedgingSummary {
  n_bom_lines: number;
  n_lines_with_alternate: number;
  n_lines_orphaned: number;
  orphaned_component_ids: number[];
  n_single_source_lines: number;
  fully_hedged: boolean;
  // The fulfilment pair `statement` is composed from, echoed by the backend so the
  // claim and the numbers that justify it travel together. Optional: null when the
  // caller did not measure them.
  baseline_fulfillment_p50?: number | null;
  scenario_fulfillment_p50?: number | null;
  fulfillment_p50_delta_pts?: number | null;
  statement: string;
}

export interface CostSubstitution {
  baseline_component_cost_usd: number;
  scenario_component_cost_usd: number;
  substitution_delta_usd: number;
  n_lines_repriced: number;
  n_lines_unpriceable: number;
  largest_line_increase_usd: number;
  largest_line_component_id: number | null;
  basis: string;
}

export interface AffectedComponentDetail {
  component_id: number;
  mpn: string;
  current_supplier: string | null;
  alternative_suppliers: Array<{ name: string; lead_time_days: number }>;
}

/** One BOM line WITH its build quantity. */
export interface BomLine {
  component_id: number;
  quantity: number;
}

// Verified against the live `POST /resilience/delivery-target` payload (2026-08).
// `suppliers_capable` carries a *cost adjustment percentage* — the expedite premium a
// supplier charges to hit the window (0.0 when it meets the window natively). It has
// never carried a per-component average cost; the old `cost_per_component_avg`
// declaration was fiction and calling .toFixed() on it white-screened the app.
export interface DeliveryTargetResponse extends ScenarioResponse {
  suppliers_capable: Array<{ name: string; lead_time_days: number; cost_adjustment_pct: number }>;
  suppliers_cannot_meet: Array<{ name: string; min_lead_time_days: number; reason: string }>;
}

// `items` carries the real build quantities and is what these endpoints price on.
// `bom_component_ids` is the legacy quantity-free form: the API can only price it at
// ONE UNIT PER LINE, so a 50-unit cart came back valued as a 1-unit prototype while
// the table under the tiles used the cart's real quantities. Always send `items`.
export interface DistributorFailureRequest {
  distributor_id: number;
  bom_component_ids?: number[];
  items?: BomLine[];
}

export interface GeopoliticalRiskRequest {
  risk_multiplier: number;
  bom_component_ids?: number[];
  items?: BomLine[];
}

export interface DeliveryTargetRequest {
  target_delivery_days: number;
  bom_component_ids?: number[];
  items?: BomLine[];
}

// ── Recommendation Engine (Phase 6, tab 4) ────────────────────────────────────
export interface CriticalityEntry {
  distributor_id: number;
  name: string;
  country: string | null;
  is_domestic: boolean;
  orphan_component_count: number;
  orphan_component_ids: number[];
  components_supplied: number;
  spend_at_risk_usd: number;
  betweenness: number;
  rei: number;
}

export interface CriticalitySweepResponse {
  entries: CriticalityEntry[];
  max_spend_at_risk_usd: number;
  network_wide: boolean;
}

export interface CriticalitySweepRequest {
  bom_component_ids?: number[] | null;
  top_n?: number;
}

export interface DualSourceEntry {
  component_id: number;
  mpn: string;
  category: string;
  current_supplier: string;
  current_price_usd: number;
  recommended_second_source: string | null;
  second_source_price_usd: number | null;
  incremental_unit_cost_usd: number;
  p_fail_current: number;
  p_fail_second: number | null;
  expected_disruption_cost_usd: number;
  risk_reduction_usd: number;
  risk_reduction_per_dollar: number | null;
  tier: string;
}

export interface DualSourcingResponse {
  entries: DualSourceEntry[];
  no_regret_count: number;
  hedge_count: number;
  supplier_development_count: number;
}

export interface DualSourcingRequest {
  bom_component_ids?: number[] | null;
  qualification_cost_usd?: number;
  top_n?: number;
}

export interface TornadoBar {
  lever: string;
  low_label: string;
  high_label: string;
  low_output: number;
  high_output: number;
  spread: number;
}

export interface SensitivityResponse {
  baseline_output: number;
  metric: string;
  bars: TornadoBar[];
}

export interface SensitivityRequest {
  bom_component_ids: number[];
  metric?: 'cost' | 'cvar';
}

// Abort controller helper for requests
function withAbortController<T>(
  promise: Promise<T>,
  signal?: AbortSignal
): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) => {
      if (signal) {
        signal.addEventListener('abort', () => {
          reject(new Error('Request aborted'));
        });
      }
    }),
  ]);
}

// ── ML Intelligence ───────────────────────────────────────────────────────────
// Backing endpoints: backend/app/api/ml.py. All four are read-only GETs whose
// numbers are self-describing — every response carries its own baselines /
// caveats, so the UI renders them as returned rather than re-deriving claims.
export interface StressResponse {
  available: boolean;
  stress_probability: number;
  stress_source: string;          // "model" | "unavailable_*"
  stress_level: 'low' | 'moderate' | 'high' | 'unavailable';
  regime_active: boolean;
  // ── DATA VINTAGE ────────────────────────────────────────────────────────
  // `stress_probability` is scored from ONE row of a MONTHLY feature frame, and
  // that row is not this month. These fields say which month it is and how far
  // behind the clock it has fallen; the backend derives them from the frame it
  // scored (backend/app/api/ml.py, backend/app/ml/serving.py). Rendering the
  // probability without them republishes the defect they were added to close.
  observation_date: string | null;        // ISO date, e.g. "2026-07-01"
  observation_frequency: string | null;   // "monthly"
  observation_age_days: number | null;
  observation_age_months: number | null;
  vintage_is_stale: boolean | null;
  max_observation_age_days: number;
  vintage_label: string;                  // pre-rendered, printable verbatim
  brier: number | null;
  baseline_brier: number | null;
  climatology_brier: number | null;
  log_loss: number | null;
  climatology_log_loss: number | null;
  calibration_slope: number | null;
  expected_calibration_error: number | null;
  val_accuracy: number | null;
  baseline_accuracy: number | null;
  accuracy_delta_vs_baseline: number | null;
  shortage_recall: number | null;
  ship_gate_passed: boolean | null;
  ship_gate_policy: string | null;
  ship_gate_reason: string | null;
  interpretation: string;
}

/**
 * "Jul 2026" — the observation month behind a stress reading, or null when the
 * backend could not name one.
 *
 * Shared deliberately: the probability is rendered in two places and neither
 * may print it without its month. Formatted in UTC so the month never slips a
 * day either side depending on the reader's timezone.
 */
export function stressObservationMonth(
  stress: Pick<StressResponse, 'observation_date'> | null | undefined,
): string | null {
  const iso = stress?.observation_date;
  if (!iso) return null;
  const d = new Date(`${String(iso).slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' });
}

export interface ModelMetrics {
  name: string;
  kind: 'model' | 'naive_baseline';
  rmse: number;
  mae: number;
  r2: number;
  cv_splits: number | null;
  cv_rmse_mean: number | null;
  cv_rmse_std: number | null;
  cv_r2_mean: number | null;
  cv_r2_std: number | null;
  cv_r2_median: number | null;
  is_served: boolean;
}

export interface PairedComparison {
  mean_rmse_reduction_days?: number;
  std_error?: number;
  folds_model_won?: number;
  n_folds?: number;
  paired_t_p_value?: number;
  [key: string]: unknown;
}

export interface ModelComparisonResponse {
  models: ModelMetrics[];
  baselines: ModelMetrics[];
  served_model: string | null;
  served_metrics: ModelMetrics | null;
  metrics_describe_served_model: boolean;
  model_source: string;
  selection_metric: string;
  beats_all_baselines: boolean | null;
  toughest_baseline: string | null;
  skill_vs_toughest_baseline: number | null;
  paired_vs_toughest_baseline: PairedComparison;
  training_samples: number | null;
  n_features: number | null;
  feature_schema_version: number | null;
  feature_columns: string[];
  feature_exclusions: Array<Record<string, unknown>>;
  evaluation: string;
  caveat: string;
}

export interface LeadTimePrediction {
  dk_category: string;
  manufacturer: string | null;
  lifecycle_status: string | null;
  unit_price: number;
  predicted_factory_lead_time_days: number;
  features_used: string[];
  quantity_predicted: string;
  base_days: number;
  model_used: string;
  model_source: string;
  model_version: string | null;
  feature_schema_version: number;
}

export interface ModelInfoResponse {
  model_source: string;           // mlflow_registry | local_joblib | none
  model_name: string | null;
  registered_model: string | null;
  model_version: string | null;
  alias: string | null;
  run_id: string | null;
  model_uri: string | null;
  tracking_uri: string | null;
  selection_metric: string | null;
  selection_value: string | null;
  artifact_mtime: string | null;
  resolved_at: string | null;
  fallback_reason: string | null;
  n_training_samples: number | null;
  n_features: number | null;
  detail: string;
}

export const mlAPI = {
  stress: () => api.get<StressResponse>('/ml/stress'),
  modelComparison: () => api.get<ModelComparisonResponse>('/ml/model-comparison'),
  modelInfo: () => api.get<ModelInfoResponse>('/ml/model-info'),
  leadTime: (params?: {
    dk_category?: string;
    manufacturer?: string;
    lifecycle_status?: string;
    unit_price?: number;
  }) => api.get<LeadTimePrediction>('/ml/lead-time', { params }),
};

// Turn a timeout or an abort into a readable message; rethrow anything else unchanged.
function rethrowRequestError(error: unknown): never {
  const { code, message } = (error ?? {}) as { code?: string; message?: string };
  if (code === 'ECONNABORTED' || message?.includes('timeout')) {
    throw new Error('Request timeout — please try again');
  }
  if (message?.includes('aborted')) {
    throw new Error('Request cancelled');
  }
  throw error;
}

export const resilienceAPI = {
  distributorFailure: async (
    req: DistributorFailureRequest,
    signal?: AbortSignal
  ): Promise<ScenarioResponse> => {
    try {
      const response = await withAbortController(
        api.post<ScenarioResponse>('/resilience/distributor-failure', req, { signal }),
        signal
      );
      return response.data;
    } catch (error) {
      rethrowRequestError(error);
    }
  },

  geopoliticalRisk: async (
    req: GeopoliticalRiskRequest,
    signal?: AbortSignal
  ): Promise<ScenarioResponse> => {
    try {
      const response = await withAbortController(
        api.post<ScenarioResponse>('/resilience/geopolitical-risk', req, { signal }),
        signal
      );
      return response.data;
    } catch (error) {
      rethrowRequestError(error);
    }
  },

  deliveryTarget: async (
    req: DeliveryTargetRequest,
    signal?: AbortSignal
  ): Promise<DeliveryTargetResponse> => {
    try {
      const response = await withAbortController(
        api.post<DeliveryTargetResponse>('/resilience/delivery-target', req, { signal }),
        signal
      );
      return response.data;
    } catch (error) {
      rethrowRequestError(error);
    }
  },

  criticalitySweep: async (
    req: CriticalitySweepRequest = {},
    signal?: AbortSignal
  ): Promise<CriticalitySweepResponse> => {
    try {
      const response = await withAbortController(
        api.post<CriticalitySweepResponse>('/resilience/criticality-sweep', req, { signal }),
        signal
      );
      return response.data;
    } catch (error) {
      rethrowRequestError(error);
    }
  },

  dualSourcingPlan: async (
    req: DualSourcingRequest = {},
    signal?: AbortSignal
  ): Promise<DualSourcingResponse> => {
    try {
      const response = await withAbortController(
        api.post<DualSourcingResponse>('/resilience/dual-sourcing-plan', req, { signal }),
        signal
      );
      return response.data;
    } catch (error) {
      rethrowRequestError(error);
    }
  },

  sensitivity: async (
    req: SensitivityRequest,
    signal?: AbortSignal
  ): Promise<SensitivityResponse> => {
    try {
      const response = await withAbortController(
        api.post<SensitivityResponse>('/resilience/sensitivity', req, { signal }),
        signal
      );
      return response.data;
    } catch (error) {
      rethrowRequestError(error);
    }
  },
};

export default api;
