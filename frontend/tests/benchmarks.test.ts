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
import { SUPPORTED_SCHEMA_VERSION, parseBenchmarks } from '../src/lib/benchmarks';

const ARTIFACT = fileURLToPath(new URL('../../docs/benchmark_results.json', import.meta.url));
const artifact = JSON.parse(readFileSync(ARTIFACT, 'utf8')) as {
  schema_version: unknown;
  provenance: Record<string, unknown>;
  results: Record<string, unknown>[];
};
const parsed = parseBenchmarks(artifact.results, artifact.provenance, artifact.schema_version);

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

describe('parseBenchmarks on malformed input', () => {
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
