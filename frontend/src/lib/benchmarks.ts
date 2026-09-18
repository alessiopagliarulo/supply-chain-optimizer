/**
 * Reading `docs/benchmark_results.json` (written by the Solomon benchmark script,
 * served untouched by GET /routing/benchmarks).
 *
 * Nothing here fills a gap: a row missing a required field is counted as unreadable
 * and left out, and an optional field that is absent stays null and renders as a dash.
 */

export interface BenchmarkRow {
  instance: string;
  num_customers: number;
  solver: string;
  vehicles_used: number;
  total_distance: number;
  gap_percent: number | null;
  runtime_seconds: number;
  feasible: boolean;
  solver_status: string;
  best_known_distance: number | null;
  best_known_vehicles: number | null;
  best_known_source: string | null;
}

export interface BenchmarkProvenance {
  /** Where the instances came from. */
  source: string | null;
  /** Where the best-known values came from, when the artifact says separately. */
  best_known_source: string | null;
  time_limit_seconds: number | null;
  timestamp: string | null;
  platform: string | null;
  python_version: string | null;
}

export interface ParsedBenchmarks {
  rows: BenchmarkRow[];
  unreadable: number;
  provenance: BenchmarkProvenance;
}

type Json = Record<string, unknown>;

const isObject = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === 'string' && v.length > 0 ? v : null);

function parseRow(raw: unknown): BenchmarkRow | null {
  if (!isObject(raw)) return null;
  const instance = str(raw.instance);
  const solver = str(raw.solver);
  const numCustomers = num(raw.num_customers);
  const vehicles = num(raw.vehicles_used);
  const distance = num(raw.total_distance);
  const runtime = num(raw.runtime_seconds);
  const status = str(raw.solver_status);
  if (
    instance === null || solver === null || numCustomers === null || vehicles === null ||
    distance === null || runtime === null || status === null || typeof raw.feasible !== 'boolean'
  ) {
    return null;
  }
  return {
    instance,
    num_customers: numCustomers,
    solver,
    vehicles_used: vehicles,
    total_distance: distance,
    gap_percent: num(raw.gap_percent),
    runtime_seconds: runtime,
    feasible: raw.feasible,
    solver_status: status,
    best_known_distance: num(raw.best_known_distance),
    best_known_vehicles: num(raw.best_known_vehicles),
    best_known_source: str(raw.best_known_source),
  };
}

export function parseBenchmarks(results: unknown[], provenance: unknown): ParsedBenchmarks {
  const rows: BenchmarkRow[] = [];
  let unreadable = 0;
  for (const raw of results) {
    const row = parseRow(raw);
    if (row) rows.push(row);
    else unreadable += 1;
  }
  const p = isObject(provenance) ? provenance : {};
  return {
    rows,
    unreadable,
    provenance: {
      source: str(p.source) ?? str(p.data_source) ?? str(p.instance_source),
      best_known_source: str(p.best_known_source),
      time_limit_seconds: num(p.time_limit_seconds),
      timestamp: str(p.timestamp) ?? str(p.generated_at),
      platform: str(p.platform),
      python_version: str(p.python_version),
    },
  };
}

export type SortKey = keyof Pick<
  BenchmarkRow,
  'instance' | 'num_customers' | 'solver' | 'vehicles_used' | 'total_distance' | 'gap_percent' | 'runtime_seconds' | 'feasible'
>;

/** Sorts a copy. Nulls always sort last, whichever the direction. */
export function sortRows(rows: BenchmarkRow[], key: SortKey, ascending: boolean): BenchmarkRow[] {
  const dir = ascending ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    if (typeof av === 'string' && typeof bv === 'string') return dir * av.localeCompare(bv, undefined, { numeric: true });
    return dir * (Number(av) - Number(bv));
  });
}
