/**
 * Reading `docs/benchmark_results.json` (written by backend/scripts/benchmark_solomon.py,
 * served untouched by GET /routing/benchmarks).
 *
 * The shape read here is the script's `schema_version` 3 record, field for field. Nothing
 * here fills a gap: a row missing a required field is counted as unreadable and left out,
 * and a value the artifact leaves null (no solution, no published reference, no gap) stays
 * null and renders as such. Null is data, never an error.
 */

/** The artifact schema this reader understands (`schema_version` in the file). */
export const SUPPORTED_SCHEMA_VERSION = 3;

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
  /**
   * Distance gap in the reference's convention. Null when there is no feasible solution, no
   * published reference, or the gap is not comparable (see `gap_comparable`).
   */
  gap_percent: number | null;
  /**
   * False when the reference ranks fewest vehicles first (SINTEF, 100 customers) and the solver
   * used a different number of vehicles: its distance then says nothing about beating the
   * reference, and `vehicle_gap` is the comparison. Null when there is nothing to compare.
   */
  gap_comparable: boolean | null;
  /** Vehicles used minus the reference's vehicles; null when there is nothing to compare. */
  vehicle_gap: number | null;
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
  /** True when the table ranks fewest vehicles first (SINTEF), false for distance only; null if unrecorded. */
  ranks_vehicles_first: boolean | null;
}

/** One `summary` entry: per instance size and solver, the averages the script computed. */
export interface BenchmarkSummary {
  num_customers: number;
  solver: string;
  runs: number;
  feasible: number;
  /** Runs with a reference to compare against. */
  with_reference: number;
  /** Of those, runs whose distance gap is comparable: the runs `mean_gap_percent` averages. */
  gap_comparable: number;
  more_vehicles_than_reference: number;
  fewer_vehicles_than_reference: number;
  /** Mean of the comparable gaps; null when no run was comparable. */
  mean_gap_percent: number | null;
  mean_runtime_seconds: number;
}

export interface ParsedBenchmarks {
  /** The file's `schema_version`, or null when it has none. */
  schema_version: number | null;
  /** False when the file declares a schema this reader was not written for. */
  schema_supported: boolean;
  rows: BenchmarkRow[];
  unreadable: number;
  /** The script's per-size, per-solver averages; entries missing a required field are left out. */
  summary: BenchmarkSummary[];
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
    gap_comparable: typeof raw.gap_comparable === 'boolean' ? raw.gap_comparable : null,
    vehicle_gap: num(raw.vehicle_gap),
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
      const ranks = typeof s.ranks_vehicles_first === 'boolean' ? s.ranks_vehicles_first : null;
      return [{ title, applies_to: sizes, distance_convention: str(s.distance_convention), ranks_vehicles_first: ranks }];
    }),
    time_limit_seconds: num(p.time_limit_seconds),
    generated_at: str(p.generated_at),
    git_commit: str(p.git_commit),
    ortools_version: str(p.ortools_version),
    platform: str(p.platform),
    python_version: str(p.python_version),
  };
}

function parseSummary(raw: unknown): BenchmarkSummary | null {
  if (!isObject(raw)) return null;
  const solver = str(raw.solver);
  const fields = [
    'num_customers', 'runs', 'feasible', 'with_reference', 'gap_comparable', 'more_vehicles_than_reference',
    'fewer_vehicles_than_reference', 'mean_runtime_seconds',
  ] as const;
  const values = fields.map((f) => num(raw[f]));
  if (solver === null || values.some((v) => v === null)) return null;
  const [numCustomers, runs, feasible, withReference, comparable, more, fewer, runtime] = values as number[];
  return {
    num_customers: numCustomers,
    solver,
    runs,
    feasible,
    with_reference: withReference,
    gap_comparable: comparable,
    more_vehicles_than_reference: more,
    fewer_vehicles_than_reference: fewer,
    mean_gap_percent: num(raw.mean_gap_percent),
    mean_runtime_seconds: runtime,
  };
}

export function parseBenchmarks(
  results: unknown[],
  provenance: unknown,
  schemaVersion: unknown,
  summary: unknown = [],
): ParsedBenchmarks {
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
    summary: (Array.isArray(summary) ? summary : []).map(parseSummary).filter((s): s is BenchmarkSummary => s !== null),
    provenance: parseProvenance(provenance),
  };
}

export type SortKey = keyof Pick<
  BenchmarkRow,
  | 'instance'
  | 'num_customers'
  | 'solver'
  | 'vehicles_used'
  | 'vehicle_gap'
  | 'distance'
  | 'gap_percent'
  | 'runtime_seconds'
  | 'status'
>;

/**
 * The gap cell: the distance gap when it is comparable, "not comparable" when the solver used a
 * different number of vehicles than a vehicles-first reference, and a dash when there is nothing
 * to compare. A distance gap from a different fleet size is never shown as a number.
 */
export function gapLabel(row: BenchmarkRow): string {
  if (row.gap_comparable === false) return 'not comparable';
  // A row that compares vehicles but does not say whether its distance gap is comparable (an older
  // artifact) could be a different-fleet gap: never show it as a number.
  if (row.gap_comparable === null && row.vehicle_gap !== null) return '-';
  if (row.gap_percent === null) return '-';
  return `${row.gap_percent >= 0 ? '+' : ''}${row.gap_percent.toFixed(2)}%`;
}

/** The vehicles-vs-reference cell: "same", "+4", "-1", or a dash when there is nothing to compare. */
export function vehicleGapLabel(row: BenchmarkRow): string {
  if (row.vehicle_gap === null) return '-';
  if (row.vehicle_gap === 0) return 'same';
  return `${row.vehicle_gap > 0 ? '+' : ''}${row.vehicle_gap}`;
}

/** True when a reference table ranks fewest vehicles first, as the SINTEF best known does. */
export const ranksVehiclesFirst = (source: BestKnownSource) => source.ranks_vehicles_first === true;

/** Sizes as a suspended-hyphen list for "<list>-customer": "100", "25- and 50", "25-, 50- and 100". */
export function sizesLabel(sizes: number[]): string {
  const sorted = [...new Set(sizes)].sort((a, b) => a - b).map(String);
  if (sorted.length <= 1) return sorted.join('');
  return `${sorted.slice(0, -1).join('-, ')}- and ${sorted[sorted.length - 1]}`;
}

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
