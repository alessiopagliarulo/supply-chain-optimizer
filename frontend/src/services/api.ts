import axios from 'axios';

/**
 * Absolute in production (Render sets VITE_API_URL to the API origin + /api/v1),
 * relative in local dev behind Vite's proxy. Exported so the cold-start warm-up in
 * ./warmup.ts derives its probe URL from the same value instead of a second copy.
 */
export const API_BASE_URL = import.meta.env.VITE_API_URL || '/api/v1';

/**
 * Render's free tier spins the backend down after ~15 minutes idle and a cold start
 * takes up to ~2 minutes, so every call gets a cold-start-sized budget. Buffer tuning
 * solves and simulates a whole grid of plans and can legitimately run that long too.
 */
export const COLD_START_TIMEOUT_MS = 150000;

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: COLD_START_TIMEOUT_MS,
  headers: { 'Content-Type': 'application/json' },
});

// ── Types: the shapes backend/app/api/routing.py returns ─────────────────────

/** One stop. Node 0 is the depot. Times are in the instance's own units. */
export interface PlanNode {
  x: number;
  y: number;
  demand: number;
  ready: number;
  due: number;
  service_time: number;
}

export interface InstanceSummary {
  id: string;
  name: string;
  source: string;
  num_customers: number;
  num_vehicles: number;
  vehicle_capacity: number;
}

export interface InstanceDetail extends InstanceSummary {
  nodes: PlanNode[];
}

/** The server's request caps, so the UI states them instead of retyping them. */
export interface RoutingLimits {
  max_customers: number;
  max_exact_customers: number;
  /** Auto uses the exact CP-SAT model up to this many customers, OR-Tools routing above. */
  auto_exact_max_customers: number;
  max_tuning_customers: number;
  max_time_limit_seconds: number;
  max_replications: number;
}

export interface InstanceList {
  instances: InstanceSummary[];
  limits: RoutingLimits;
}

export type SolverMethod = 'auto' | 'cpsat' | 'clarke_wright' | 'ortools';

export type SolveStatus = 'optimal' | 'feasible' | 'infeasible' | 'no_solution';

export interface RouteReport {
  customers: number[];
  load: number;
  distance: number;
  service_starts: number[];
  depart: number;
  return_time: number;
}

export interface SolveResponse {
  method: string;
  status: SolveStatus;
  routes: number[][];
  total_cost: number;
  proven_optimal: boolean;
  wall_seconds: number;
  validation: {
    feasible: boolean;
    violations: string[];
    total_cost: number;
    routes: RouteReport[];
  };
  feasible: boolean;
  vehicles_used: number;
}

/** The instance part of every plan request. Coordinates are always sent, scale 1. */
export interface PlanInstance {
  nodes: PlanNode[];
  num_vehicles: number;
  vehicle_capacity: number;
}

export type Distribution = 'lognormal' | 'triangular';

export interface SimulateRequest extends PlanInstance {
  routes: number[][];
  num_replications: number;
  variability: number;
  distribution: Distribution;
  seed: number;
}

export interface SimulateResponse {
  num_replications: number;
  variability: number;
  distribution: Distribution;
  seed: number;
  on_time_rate: number;
  mean_lateness: number;
  p95_lateness: number;
  mean_route_completion_time: number;
  max_route_completion_time: number;
  depot_close: number;
  vehicle_utilization_mean: number;
  vehicle_utilization_min: number;
  plan_feasible: boolean;
  plan_violations: string[];
}

export type TuningObjective = 'maximize_on_time' | 'minimize_cost_for_target';

export interface TuneBuffersRequest extends PlanInstance {
  schedule_buffer_max: number;
  schedule_buffer_step: number;
  capacity_buffer_max: number;
  capacity_buffer_step: number;
  num_replications: number;
  variability: number;
  distribution: Distribution;
  target_on_time_rate: number;
  objective: TuningObjective;
  base_seed: number;
  method: SolverMethod;
  time_limit_seconds: number;
}

export interface BufferCandidate {
  schedule_buffer_pct: number;
  capacity_buffer_pct: number;
  on_time_rate: number;
  mean_lateness: number;
  p95_lateness: number;
  total_distance: number;
  vehicles_used: number;
}

export interface TuneBuffersResponse {
  chosen_buffer: BufferCandidate;
  frontier: BufferCandidate[];
  num_replications: number;
  variability: number;
  distribution: string;
  objective: string;
  target_on_time_rate: number;
}

/**
 * The benchmark artifact is passed through as written, so its body is `unknown`
 * here and parsed row by row in lib/benchmarks.ts rather than trusted.
 */
export type BenchmarksResponse =
  | { available: false; artifact: string }
  | { available: true; artifact: string; schema_version?: unknown; provenance?: unknown; results: unknown[] };

// ── Calls ────────────────────────────────────────────────────────────────────

export const routingApi = {
  listInstances: () => api.get<InstanceList>('/routing/instances').then((r) => r.data),
  getInstance: (id: string) =>
    api
      .get<InstanceDetail>(`/routing/instances/${id.split('/').map(encodeURIComponent).join('/')}`)
      .then((r) => r.data),
  solve: (req: PlanInstance & { method: SolverMethod; time_limit_seconds: number }) =>
    api.post<SolveResponse>('/routing/solve', req).then((r) => r.data),
  simulate: (req: SimulateRequest) =>
    api.post<SimulateResponse>('/routing/simulate', req).then((r) => r.data),
  tuneBuffers: (req: TuneBuffersRequest) =>
    api.post<TuneBuffersResponse>('/routing/tune-buffers', req).then((r) => r.data),
  benchmarks: () => api.get<BenchmarksResponse>('/routing/benchmarks').then((r) => r.data),
};

// ── Catalogue: the frozen distributor snapshot (backend/app/api/distributors.py) ──

/** GET /catalogue/provenance: what the catalogue is. Read before labelling it anywhere. */
export interface CatalogueProvenance {
  is_live: boolean;
  snapshot_year: number;
  summary: string;
  dataset: string;
  dataset_url: string;
  license: string;
  original_source: string;
  counts: { components: number; distributors: number; offers: number };
  coordinates: {
    precision: string;
    tolerance_km: number;
    source: string;
    distinct_points: number;
    distributors_on_shared_points: number;
    unlocated_distributors: number;
  };
  documentation: { path: string; url: string };
}

export interface Distributor {
  id: number;
  name: string;
  latitude: number | null;
  longitude: number | null;
  city: string | null;
  state: string | null;
  country: string | null;
  is_domestic: boolean | null;
  total_offers: number;
  total_stock: number;
  /** How many OTHER distributors sit on exactly this point (coordinates are per city). */
  shares_location_with: number;
}

/** A distributor the map can place: coordinates known. */
export type LocatedDistributor = Distributor & { latitude: number; longitude: number };

export const isLocated = (d: Distributor): d is LocatedDistributor => d.latitude !== null && d.longitude !== null;

/** One component a distributor carries, with its 2024-snapshot offer. */
export interface CarriedComponent {
  component_id: number;
  mpn: string;
  manufacturer: string;
  category: string;
  price: number;
  currency: string | null;
  stock: number;
  moq: number;
  sku: string | null;
}

export const catalogueApi = {
  provenance: () => api.get<CatalogueProvenance>('/catalogue/provenance').then((r) => r.data),
  distributors: () =>
    api.get<Distributor[]>('/distributors', { params: { located_only: true } }).then((r) => r.data.filter(isLocated)),
  components: (distributorId: number) =>
    api
      .get<CarriedComponent[]>(`/distributors/${distributorId}/components`, { params: { limit: 1000 } })
      .then((r) => r.data),
};

// ── Real-place routing (backend/app/api/routing_places.py) ───────────────────

/** The example assumptions around the real places. None of these are real data. */
export interface PlacesScenario {
  stop_load: number;
  vehicle_capacity: number;
  num_vehicles: number;
  speed_kmh: number;
  service_minutes: number;
  max_route_hours: number;
}

export interface PlacesModel {
  road_factor: number;
  road_factor_rationale: string;
  earth_radius_km: number;
  /** Country (as the catalogue spells it) -> road-connected region. */
  road_regions: Record<string, string>;
  default_scenario: PlacesScenario;
  limits: {
    max_stops: number;
    max_vehicles: number;
    max_capacity: number;
    max_route_hours: number;
    max_time_limit_seconds: number;
    max_exact_customers: number;
    auto_exact_max_customers: number;
  };
  example: { depot_id: number; stop_ids: number[] } | null;
  documentation: string;
}

export interface Place {
  id: number;
  name: string;
  latitude: number;
  longitude: number;
  city: string | null;
  state: string | null;
  country: string | null;
}

export interface PlacedRoute {
  /** Indices into the response's `stops`, in visiting order. */
  stops: number[];
  load: number;
  distance_km: number;
  straight_line_km: number;
  duration_hours: number;
  service_start_hours: number[];
}

export interface PlacesSolveResponse {
  depot: Place;
  stops: Place[];
  scenario: PlacesScenario;
  road_factor: number;
  method: string;
  status: SolveStatus;
  feasible: boolean;
  proven_optimal: boolean;
  wall_seconds: number;
  vehicles_used: number;
  total_km: number;
  total_straight_line_km: number;
  routes: PlacedRoute[];
  violations: string[];
}

export interface PlacesSolveRequest {
  depot_id: number;
  stop_ids: number[];
  scenario: PlacesScenario;
  method: SolverMethod;
  time_limit_seconds: number;
}

/** POST /routing/places/candidates: where a truck from one depot can go. */
export interface PlaceCandidate {
  place: Place;
  /** Great-circle distance from the depot times the road factor, computed by the server. */
  road_km: number;
  round_trip_hours: number;
  fits_route_limit: boolean;
}

export interface PlacesCandidates {
  depot: Place;
  region: string | null;
  /** Nearest first. */
  candidates: PlaceCandidate[];
  /** A starting set the example fleet can serve; empty when nothing fits the route limit. */
  suggested: number[];
}

export const placesApi = {
  model: () => api.get<PlacesModel>('/routing/places/model').then((r) => r.data),
  candidates: (depot_id: number, scenario: PlacesScenario) =>
    api.post<PlacesCandidates>('/routing/places/candidates', { depot_id, scenario }).then((r) => r.data),
  solve: (req: PlacesSolveRequest) =>
    api.post<PlacesSolveResponse>('/routing/places/solve', req).then((r) => r.data),
};

/**
 * A reader-facing message for a failed call: the server's own `detail` when it sent
 * one (FastAPI's 422s say exactly which field is wrong), otherwise what went wrong
 * on the wire.
 */
export function errorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail: unknown = err.response?.data?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      const parts = detail
        .map((d: unknown) => {
          if (typeof d !== 'object' || d === null) return null;
          const { loc, msg } = d as { loc?: unknown; msg?: unknown };
          const where = Array.isArray(loc) ? loc.filter((p) => p !== 'body').join('.') : '';
          return typeof msg === 'string' ? (where ? `${where}: ${msg}` : msg) : null;
        })
        .filter((p): p is string => p !== null);
      if (parts.length) return parts.join('; ');
    }
    if (err.response) return `The server answered ${err.response.status} ${err.response.statusText}`.trim();
    if (err.code === 'ECONNABORTED') return 'The request timed out before the server answered.';
    return 'Could not reach the server. It may be starting up; try again in a minute.';
  }
  return err instanceof Error ? err.message : 'Something went wrong.';
}
