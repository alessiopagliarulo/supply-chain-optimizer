/**
 * Reading `docs/benchmark_results.json` (written by backend/scripts/benchmark_solomon.py,
 * served untouched by GET /routing/benchmarks).
 *
 * The shape read here is the script's `schema_version` 2 record, field for field. Nothing
 * here fills a gap: a row missing a required field is counted as unreadable and left out,
 * and a value the artifact leaves null (no solution, no published reference, no gap) stays
 * null and renders as such. Null is data, never an error.
 */

/** The artifact schema this reader understands (`schema_version` in the file). */
export const SUPPORTED_SCHEMA_VERSION = 2;

export interface BenchmarkReference {
  distance: number;
  vehicles: number | null;
  /** Short citation key of whoever found the value, e.g. "KDMSS". */
  citation: string | null;
  /** Which reference table the value comes from, e.g. "solomon_optimal". */
  source: string | null;
}

export interface BenchmarkRow {
  instance: string;
  family: string | null;
  num_customers: number;
  solver: string;
  /** optimal / feasible / infeasible / no_solution, as the script normalised it. */
  status: string;
  feasible: boolean;
  proven_optimal_scaled_model: boolean;
  vehicles_used: number;
  /** Null when the solver returned no routes. */
  distance: number | null;
  /** Which distance field `gap_percent` was computed on; null without a reference. */
  gap_measured_on: string | null;
  /** Null when there is no feasible solution or no published reference. */
  gap_percent: number | null;
  runtime_seconds: number;
  /** Null when no reference value is published for this instance and size. */
  reference: BenchmarkReference | null;
}

export interface BenchmarkProvenance {
  /** Where the instances came from. */
  data_source: string | null;
  /** The reference tables the best-known values come from. */
  best_known_sources: BestKnownSource[];
  time_limit_seconds: number | null;
  generated_at: string | null;
  git_commit: string | null;
  ortools_version: string | null;
  platform: string | null;
  python_version: string | null;
}

export interface BestKnownSource {
  title: string;
  /** Instance sizes (customer counts) the table covers. */
  applies_to: number[];
  /** How that table measures distance, which is what each gap is compared in. */
  distance_convention: string | null;
}

export interface ParsedBenchmarks {
  /** The file's `schema_version`, or null when it has none. */
  schema_version: number | null;
  /** False when the file declares a schema this reader was not written for. */
  schema_supported: boolean;
  rows: BenchmarkRow[];
  unreadable: number;
  provenance: BenchmarkProvenance;
}

type Json = Record<string, unknown>;

const isObject = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === 'string' && v.length > 0 ? v : null);

/** A reference object, null when absent, or undefined when present but malformed. */
function parseReference(raw: unknown): BenchmarkReference | null | undefined {
  if (raw === null) return null;
  if (!isObject(raw)) return undefined;
  const distance = num(raw.distance);
  if (distance === null) return undefined;
  return { distance, vehicles: num(raw.vehicles), citation: str(raw.reference), source: str(raw.source) };
}

function parseRow(raw: unknown): BenchmarkRow | null {
  if (!isObject(raw)) return null;
  const instance = str(raw.instance);
  const solver = str(raw.solver);
  const status = str(raw.status);
  const numCustomers = num(raw.num_customers);
  const vehicles = num(raw.vehicles_used);
  const runtime = num(raw.runtime_seconds);
  const reference = parseReference(raw.reference ?? null);
  if (
    instance === null || solver === null || status === null || numCustomers === null || vehicles === null ||
    runtime === null || typeof raw.feasible !== 'boolean' || reference === undefined
  ) {
    return null;
  }
  return {
    instance,
    family: str(raw.family),
    num_customers: numCustomers,
    solver,
    status,
    feasible: raw.feasible,
    proven_optimal_scaled_model: raw.proven_optimal_scaled_model === true,
    vehicles_used: vehicles,
    distance: num(raw.distance),
    gap_measured_on: str(raw.gap_measured_on),
    gap_percent: num(raw.gap_percent),
    runtime_seconds: runtime,
    reference,
  };
}

function parseProvenance(provenance: unknown): BenchmarkProvenance {
  const p = isObject(provenance) ? provenance : {};
  const data = isObject(p.data) ? p.data : {};
  const bestKnown = isObject(p.best_known) ? p.best_known : {};
  const sources = isObject(bestKnown.sources) ? bestKnown.sources : {};
  return {
    data_source: str(data.instances),
    best_known_sources: Object.values(sources).flatMap((s): BestKnownSource[] => {
      const title = isObject(s) ? str(s.title) : null;
      if (!isObject(s) || title === null) return [];
      const sizes = Array.isArray(s.applies_to) ? s.applies_to.map(num).filter((n): n is number => n !== null) : [];
      return [{ title, applies_to: sizes, distance_convention: str(s.distance_convention) }];
    }),
    time_limit_seconds: num(p.time_limit_seconds),
    generated_at: str(p.generated_at),
    git_commit: str(p.git_commit),
    ortools_version: str(p.ortools_version),
    platform: str(p.platform),
    python_version: str(p.python_version),
  };
}

export function parseBenchmarks(results: unknown[], provenance: unknown, schemaVersion: unknown): ParsedBenchmarks {
  const version = num(schemaVersion);
  const rows: BenchmarkRow[] = [];
  let unreadable = 0;
  for (const raw of results) {
    const row = parseRow(raw);
    if (row) rows.push(row);
    else unreadable += 1;
  }
  return {
    schema_version: version,
    schema_supported: version === SUPPORTED_SCHEMA_VERSION,
    rows,
    unreadable,
    provenance: parseProvenance(provenance),
  };
}

export type SortKey = keyof Pick<
  BenchmarkRow,
  'instance' | 'num_customers' | 'solver' | 'vehicles_used' | 'distance' | 'gap_percent' | 'runtime_seconds' | 'status'
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
