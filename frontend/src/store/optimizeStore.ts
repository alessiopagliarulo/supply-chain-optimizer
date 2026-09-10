import { create } from 'zustand';

export interface RouteStop {
  order: number;
  distributor_id: number;
  distributor_name: string;
  city: string | null;
  state: string | null;
  country: string | null;
  lat: number;
  lng: number;
  components: string[];
  distance_km: number;
  leg_cost_usd: number;
  leg_co2e_kg: number;
  // Freight actually aboard while this leg is driven. A pickup tour leaves the
  // depot empty, so the outbound leg is 0 and only the return leg carries the
  // whole order — leg_cost_usd and leg_co2e_kg are derived from THIS, not from
  // the order total. Optional because older cached responses lack it.
  leg_carried_kg?: number;
}

export interface CostBreakdown {
  component_cost: number;
  transport_cost: number;
  holding_cost: number;
  total: number;
}

export interface StrategyMath {
  weights: { cost: number; time: number; carbon: number };
  raw_objective_values: { cost: number; time: number; carbon: number };
  normalized_objective_values: { cost: number; time: number; carbon: number };
  weighted_total: number;
  citations: string[];
}

export interface CrossDockInfo {
  enabled: boolean;
  hub_id?: number | null;
  hub_name?: string | null;
  hub_city?: string | null;
  hub_state?: string | null;
  hub_lat?: number | null;
  hub_lng?: number | null;
  savings_vs_direct_pct: number;
  direct_cost_usd: number;
  consolidated_cost_usd: number;
  rationale: string;
}

export interface SupplyRiskInfo {
  model_available: boolean;
  model_name?: string | null;
  model_source?: string | null;      // mlflow_registry | local_joblib | none
  lines_scored: number;
  lines_declined: number;
  declined_reason?: string | null;
  max_factory_lead_time_days?: number | null;
  driver_mpn?: string | null;
  zero_buffer_lines: number;
  route_eta_days: number;
  risk_adjusted_eta_days: number;
  rationale: string;
}

export interface SourcingAssignment {
  component_id: number;
  mpn: string;
  distributor_id: number;
  distributor_name: string;
  quantity: number;
  unit_price_usd: number;
  line_total_usd: number;
}

export interface OutlierDropLog {
  component_id: number;
  mpn: string;
  dropped_distributor_id: number;
  dropped_price_usd: number;
  median_price_usd: number;
  reason: string;
}

export interface RouteAlternative {
  id: string;
  label: string;
  description: string;
  route: RouteStop[];
  total_cost_usd: number;
  total_transport_cost_usd: number;
  total_component_cost_usd: number;
  total_co2e_kg: number;
  total_distance_km: number;
  base_eta_days: number;
  eta_p10: number;
  eta_p50: number;
  eta_p90: number;
  monte_carlo_samples: number[];
  stop_count: number;
  international_stops: number;
  cost_rank: number;
  speed_rank: number;
  carbon_rank: number;
  distance_rank: number;
  cost_breakdown?: CostBreakdown | null;
  strategy_math?: StrategyMath | null;
  cross_dock?: CrossDockInfo | null;
  supply_risk?: SupplyRiskInfo | null;
  sourcing?: SourcingAssignment[];
  // Sent by the backend on every alternative and, until now, never rendered.
  // Under `cross_dock_consolidated` the legs in `route` are the PRE-consolidation
  // pickup legs and do NOT sum to the charged totals above — `route_legs_note`
  // is the backend's own sentence explaining that.
  transport_cost_basis?: 'direct_pickup_tour' | 'cross_dock_consolidated' | string;
  route_legs_note?: string | null;
  route_leg_distance_km?: number | null;
  route_leg_cost_usd?: number | null;
  route_leg_co2e_kg?: number | null;
}

export interface MultiRouteResult {
  alternatives: RouteAlternative[];
  recommended_id: string;
  outlier_drops?: OutlierDropLog[];
}

interface OptimizeState {
  multiResult: MultiRouteResult | null;
  selectedId: string | null;
  setMultiResult: (r: MultiRouteResult) => void;
  setSelectedId: (id: string) => void;
  clearResult: () => void;
  /** Convenience: get the currently selected alternative */
  getSelected: () => RouteAlternative | null;
}

export const useOptimizeStore = create<OptimizeState>((set, get) => ({
  multiResult: null,
  selectedId: null,
  setMultiResult: (r) => set({ multiResult: r, selectedId: r.recommended_id }),
  setSelectedId: (id) => set({ selectedId: id }),
  clearResult: () => set({ multiResult: null, selectedId: null }),
  getSelected: () => {
    const { multiResult, selectedId } = get();
    if (!multiResult || !selectedId) return null;
    return multiResult.alternatives.find((a) => a.id === selectedId) ?? null;
  },
}));
