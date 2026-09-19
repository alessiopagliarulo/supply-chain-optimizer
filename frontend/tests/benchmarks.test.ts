/**
 * The Benchmarks page reader against the REAL committed artifact.
 *
 * The page once shipped a reader written against a guessed row shape (`total_distance`,
 * `best_known_distance`, string `solver_status`) before the benchmark script existed, so
 * on the live site every one of the artifact's rows was rejected as unreadable. These
 * tests read docs/benchmark_results.json itself, so a reader/artifact mismatch fails here.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  SUPPORTED_SCHEMA_VERSION,
  gapLabel,
  parseBenchmarks,
  ranksVehiclesFirst,
  sizesLabel,
  vehicleGapLabel,
} from '../src/lib/benchmarks';

const ARTIFACT = fileURLToPath(new URL('../../docs/benchmark_results.json', import.meta.url));
const artifact = JSON.parse(readFileSync(ARTIFACT, 'utf8')) as {
  schema_version: unknown;
  provenance: Record<string, unknown>;
  summary: Record<string, unknown>[];
  results: Record<string, unknown>[];
};
const parsed = parseBenchmarks(artifact.results, artifact.provenance, artifact.schema_version, artifact.summary);

describe('parseBenchmarks on docs/benchmark_results.json', () => {
  it('reads the schema version the artifact declares', () => {
    expect(parsed.schema_version).toBe(SUPPORTED_SCHEMA_VERSION);
    expect(parsed.schema_supported).toBe(true);
  });

  it('reads every row, none unreadable', () => {
    expect(artifact.results.length).toBeGreaterThan(0);
    expect(parsed.unreadable).toBe(0);
    expect(parsed.rows).toHaveLength(artifact.results.length);
  });

  it('carries each row value through unchanged', () => {
    parsed.rows.forEach((row, i) => {
      const raw = artifact.results[i];
      const ref = raw.reference as Record<string, unknown> | null;
      expect(row.instance).toBe(raw.instance);
      expect(row.num_customers).toBe(raw.num_customers);
      expect(row.solver).toBe(raw.solver);
      expect(row.status).toBe(raw.status);
      expect(row.feasible).toBe(raw.feasible);
      expect(row.proven_optimal_scaled_model).toBe(raw.proven_optimal_scaled_model);
      expect(row.vehicles_used).toBe(raw.vehicles_used);
      expect(row.distance).toBe(raw.distance);
      expect(row.gap_percent).toBe(raw.gap_percent);
      expect(row.gap_measured_on).toBe(raw.gap_measured_on);
      expect(row.gap_comparable).toBe(raw.gap_comparable);
      expect(row.vehicle_gap).toBe(raw.vehicle_gap);
      expect(row.runtime_seconds).toBe(raw.runtime_seconds);
      expect(row.reference?.distance ?? null).toBe(ref?.distance ?? null);
      expect(row.reference?.vehicles ?? null).toBe(ref?.vehicles ?? null);
    });
  });

  it('keeps rows with no published reference, as null rather than a number', () => {
    const noReference = artifact.results.filter((r) => r.reference === null);
    expect(noReference.length).toBeGreaterThan(0);
    for (const raw of noReference) {
      const row = parsed.rows.find(
        (r) => r.instance === raw.instance && r.num_customers === raw.num_customers && r.solver === raw.solver,
      );
      expect(row).toBeDefined();
      expect(row!.reference).toBeNull();
      expect(row!.gap_percent).toBeNull();
    }
  });

  it('keeps rows where the solver found no solution, with a null distance', () => {
    const noSolution = parsed.rows.filter((r) => r.distance === null);
    expect(noSolution.length).toBe(artifact.results.filter((r) => r.distance === null).length);
    expect(noSolution.length).toBeGreaterThan(0);
  });

  it('reads the provenance the artifact records', () => {
    const p = parsed.provenance;
    const data = artifact.provenance.data as Record<string, unknown>;
    expect(p.data_source).toBe(data.instances);
    const sources = (artifact.provenance.best_known as { sources: Record<string, { title: string; applies_to: number[] }> }).sources;
    expect(p.best_known_sources.map((b) => b.title)).toEqual(Object.values(sources).map((b) => b.title));
    expect(p.best_known_sources.map((b) => b.applies_to)).toEqual(Object.values(sources).map((b) => b.applies_to));
    expect(p.time_limit_seconds).toBe(artifact.provenance.time_limit_seconds);
    expect(p.generated_at).toBe(artifact.provenance.generated_at);
  });
});

describe('vehicles first, then distance', () => {
  // The SINTEF best known for RC202/100 uses 3 vehicles. Before this rule the page showed an
  // OR-Tools run with 7 vehicles at -16.0%, as if it beat the best known by driving less.
  const rc202 = parsed.rows.filter((r) => r.instance === 'RC202' && r.num_customers === 100);

  it('shows RC202/100 runs with extra vehicles as not comparable, with the vehicle gap', () => {
    expect(rc202.length).toBeGreaterThan(0);
    for (const r of rc202) expect(r.reference?.vehicles).toBe(3);
    const extra = rc202.filter((r) => r.feasible && r.vehicles_used > 3);
    expect(extra.length).toBeGreaterThan(0);
    for (const r of extra) {
      expect(r.gap_comparable).toBe(false);
      expect(r.gap_percent).toBeNull();
      expect(gapLabel(r)).toBe('not comparable');
      expect(vehicleGapLabel(r)).toBe(`+${r.vehicles_used - 3}`);
    }
  });

  it('never shows a distance gap for a different vehicle count on 100 customers', () => {
    for (const r of parsed.rows.filter((row) => row.num_customers === 100 && row.vehicle_gap !== null)) {
      expect(r.gap_comparable).toBe(r.vehicle_gap === 0);
      if (r.vehicle_gap !== 0) expect(gapLabel(r)).toBe('not comparable');
    }
  });

  it('has no plotted gap below zero', () => {
    const plotted = parsed.rows.filter((r) => r.gap_percent !== null);
    expect(plotted.length).toBeGreaterThan(0);
    expect(plotted.filter((r) => r.gap_percent! < 0)).toEqual([]);
  });

  it('reads which reference ranks vehicles first from the provenance', () => {
    const vehiclesFirst = parsed.provenance.best_known_sources.filter(ranksVehiclesFirst);
    expect(vehiclesFirst.map((s) => s.applies_to)).toEqual([[100]]);
  });

  it('labels comparable gaps and missing comparisons', () => {
    const comparable = parsed.rows.find((r) => r.gap_comparable === true && r.gap_percent !== null)!;
    expect(gapLabel(comparable)).toBe(`+${comparable.gap_percent!.toFixed(2)}%`);
    const none = parsed.rows.find((r) => r.reference === null)!;
    expect(gapLabel(none)).toBe('-');
    expect(vehicleGapLabel(none)).toBe('-');
  });
});

describe('the per-solver averages', () => {
  it('reads every summary entry, carrying the counts each mean covers', () => {
    expect(parsed.summary).toHaveLength(artifact.summary.length);
    parsed.summary.forEach((s, i) => {
      const raw = artifact.summary[i];
      expect(s.num_customers).toBe(raw.num_customers);
      expect(s.solver).toBe(raw.solver);
      expect(s.runs).toBe(raw.runs);
      expect(s.gap_comparable).toBe(raw.gap_comparable);
      expect(s.mean_gap_percent).toBe(raw.mean_gap_percent);
      expect(s.more_vehicles_than_reference).toBe(raw.more_vehicles_than_reference);
      expect(s.fewer_vehicles_than_reference).toBe(raw.fewer_vehicles_than_reference);
    });
  });

  it('counts fleet differences at every size, not only where the gap is not comparable', () => {
    for (const s of parsed.summary) {
      const runs = parsed.rows.filter((r) => r.num_customers === s.num_customers && r.solver === s.solver);
      const scored = runs.filter((r) => r.vehicle_gap !== null);
      expect(s.more_vehicles_than_reference).toBe(scored.filter((r) => r.vehicle_gap! > 0).length);
      expect(s.fewer_vehicles_than_reference).toBe(scored.filter((r) => r.vehicle_gap! < 0).length);
      expect(s.gap_comparable).toBe(runs.filter((r) => r.gap_percent !== null).length);
    }
    // Clarke-Wright at 25 customers uses more vehicles than the optimum on some runs, all comparable.
    const cw25 = parsed.summary.find((s) => s.num_customers === 25 && s.solver === 'clarke_wright')!;
    expect(cw25.more_vehicles_than_reference).toBeGreaterThan(0);
  });
});

describe('sizesLabel', () => {
  it('writes a suspended-hyphen list for one, two or three sizes', () => {
    expect(sizesLabel([100])).toBe('100');
    expect(sizesLabel([50, 25])).toBe('25- and 50');
    expect(sizesLabel([25, 50, 100])).toBe('25-, 50- and 100');
    expect(sizesLabel([])).toBe('');
  });
});

describe('parseBenchmarks on malformed input', () => {
  it('never shows a number for a gap an older artifact does not mark comparable', () => {
    const rc202 = artifact.results.find((r) => r.instance === 'RC202' && r.num_customers === 100 && r.gap_comparable === false)!;
    const v2 = { ...rc202, gap_percent: -16.031 };
    delete (v2 as Record<string, unknown>).gap_comparable;
    const [row] = parseBenchmarks([v2], {}, 2).rows;
    expect(row.gap_comparable).toBeNull();
    expect(gapLabel(row)).toBe('-');
  });

  it('counts a row missing a required field as unreadable', () => {
    const [good] = artifact.results;
    const bad = { ...good };
    delete bad.instance;
    const out = parseBenchmarks([good, bad], {}, SUPPORTED_SCHEMA_VERSION);
    expect(out.rows).toHaveLength(1);
    expect(out.unreadable).toBe(1);
  });

  it('flags a schema version it was not written for', () => {
    expect(parseBenchmarks([], {}, SUPPORTED_SCHEMA_VERSION + 1).schema_supported).toBe(false);
  });
});
