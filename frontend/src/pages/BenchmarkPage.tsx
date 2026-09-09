import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import { AlertTriangle, CheckCircle2, Ban, TrendingUp, Split } from 'lucide-react';
import api, { benchmarkAPI } from '../services/api';
import { RISK_COLORS, riskLabel } from '../lib/risk';
import VolumeDecayCurve from '../components/VolumeDecayCurve';
// The SAME interval renderer the Newsvendor page's paired-bootstrap table uses.
// Two pages drawing bootstrap CIs must draw them identically, so there is one
// definition and both import it.
import CiStrip from '../components/CiStrip';
import {
  VOLUME_SWEEP_FALLBACK,
  VOLUME_SWEEP_FALLBACK_SOURCE,
  normalizeVolumeCurve,
  productionVolumeRange,
  PRODUCTION_VOLUME_MIN_MULTIPLIER,
} from '../lib/volumeDecayCurveData';

// ── Types (mirrors backend/app/api/benchmark.py response_model) ──────────────

/**
 * A 95% paired percentile-bootstrap interval around one published delta.
 *
 * The resample unit is the BOM CLUSTER: both arms of every per-BOM delta come
 * from the same BOM under the same simulation seed, so the BOM is the
 * independent unit. 10,000 resamples, seed 42, computed by the API from the
 * per-BOM values the run already stored — the benchmark is not re-solved and no
 * published mean moves.
 *
 * `significant` is true ONLY when the interval excludes zero. A delta whose
 * interval covers zero is not a result and this page must not render it as one.
 */
interface PairedBootstrapCI {
  metric: string;
  units: string;                       // "share_0_1" | "cost_multiplier" | "percent"
  mean: number;
  ci95_low: number | null;
  ci95_high: number | null;
  significant: boolean;
  n: number;
  n_effective: number;
  mean_effective: number | null;
  ci95_low_effective: number | null;
  ci95_high_effective: number | null;
  significant_effective: boolean;
  zero_plan_boms: string[];
  n_boot: number;
  seed: number;
  method: string;
}

interface ResilienceSection {
  nominal_cost_premium_pct: number;
  stress_cascade_risk_reduction: number;
  stress_cvar95_reduction: number;
  targeted_cascade_risk_reduction: number;
  targeted_cvar95_reduction: number;
  /**
   * CVaR-95 saturation (backlog item 13). `mc_cvar_95` is a mean over the
   * worst-5% tail of a BOUNDED quantity, so it tops out at `cvar95_ceiling`
   * (1 + EMERGENCY_COST_PREMIUM) and stops moving. Where a scenario is
   * saturated, its `*_cvar95_reduction` above is 0.0 BY ARITHMETIC — the metric
   * ran out of room — and rendering that as "no difference between the arms" is
   * exactly the misreading the flag exists to prevent.
   *
   * `undefined`/`null` means the run predates the columns and the question is
   * unanswered — which is NOT the same as `false`, so the UI must not collapse
   * the two. `*_p_total_shortfall_reduction` is the measure that keeps
   * resolving past the ceiling.
   */
  cvar95_ceiling?: number | null;
  stress_cvar95_saturated?: boolean | null;
  targeted_cvar95_saturated?: boolean | null;
  /** The BOMs whose delta is 0.0 because BOTH arms hit the ceiling — named, so
   *  the count on screen can be checked against something. */
  stress_cvar95_ceiling_tied_boms?: string[] | null;
  targeted_cvar95_ceiling_tied_boms?: string[] | null;
  cvar95_saturated_rows?: number | null;
  cvar95_rows_measured?: number | null;
  stress_p_total_shortfall_reduction?: number | null;
  targeted_p_total_shortfall_reduction?: number | null;
  /** Same paired bootstrap the published deltas carry, keyed by the field it
   *  qualifies. A separate dict from `intervals` so the published-delta
   *  significant/non-significant partition is unchanged. */
  p_total_shortfall_intervals?: Record<string, PairedBootstrapCI>;
  saturation_note?: string;
  /**
   * Paired bootstrap CIs keyed by the exact scalar field they qualify. Optional
   * so an older API build still renders — but when it IS served, every scalar
   * above must be read through it: `significant === false` means the interval
   * covers zero and the number is not distinguishable from zero on this panel.
   */
  intervals?: Record<string, PairedBootstrapCI>;
  n_boms?: number;
  n_effective_boms?: number;
  n_effective_definition?: string;
  significant_metrics?: string[];
  non_significant_metrics?: string[];
  inference_note?: string;
}

interface MonteCarloSummary {
  baseline_p10: number;
  baseline_p50: number;
  baseline_p90: number;
  graph_aware_p10: number;
  graph_aware_p50: number;
  graph_aware_p90: number;
  baseline_cvar_95: number | null;
  graph_aware_cvar_95: number | null;
}

interface TradeoffEntry {
  bom_name: string;
  losing_axis: string;
  baseline_value: number;
  graph_aware_value: number;
  delta_pct: number;
  /**
   * Derived from the two stored plans as of 2026-09-03. It used to be a
   * hardcoded template claiming graph-aware "routes around" the cheapest
   * distributor — false on run 7, where the blind plan is {DigiKey} and the
   * graph-aware plan is {DigiKey, Verical}. The plan fields below ship with it
   * so the sentence can be checked against the data that produced it.
   */
  narrative: string;
  blind_distributors?: string[] | null;
  graph_aware_distributors?: string[] | null;
  distributors_kept?: string[] | null;
  distributors_dropped?: string[] | null;
  distributors_added?: string[] | null;
  /** Which ingredient of the graph-aware arm actually moved the plans. */
  mechanism?: string | null;
}

interface BomDelta {
  bom_name: string;
  cost_delta_pct: number;
  eta_delta_pct: number;
  co2_delta_pct: number;
  cascade_risk_delta_pct: number;
}

interface BenchmarkSummary {
  run_id: number;
  run_tag: string;
  timestamp: string;
  n_boms: number;
  // Value of optimization: MILP vs greedy baseline (nominal)
  savings_pct: number;
  savings_usd_per_bom: number;
  savings_usd_annualized: number;
  annual_reorders: number;
  avg_suppliers_greedy: number;
  avg_suppliers_milp: number;
  // Value of resilience: graph-aware vs blind MILP (nominal + disruption)
  resilience: ResilienceSection;
  // Legacy graph-aware-vs-blind A/B fields (arm='milp', scenario='nominal')
  cost_delta_pct: number;
  cost_delta_usd: number;
  baseline_spend_at_risk_usd: number;
  eta_delta_pct: number;
  co2_delta_pct: number;
  cascade_risk_delta_pct: number;
  monte_carlo: MonteCarloSummary;
  tradeoff: TradeoffEntry;
  bom_deltas: BomDelta[];
  feeds_fallback: boolean;
  /**
   * Materiality cut the page reports against, in percent. Renamed from
   * `noise_floor_pct` (2026-08) because it never was a noise floor: nothing
   * measures it. `materiality_threshold_basis` is the API's own sentence
   * saying where it comes from — render it, never paraphrase it.
   */
  materiality_threshold_pct: number;
  materiality_threshold_basis?: string | null;

  // ── Optional / forward-compatible fields ───────────────────────────────────
  // The /benchmark/summary payload is actively being extended. Everything below
  // is read defensively: if the endpoint starts serving the volume sweep or the
  // cost decomposition, we render the API's numbers; otherwise we fall back to
  // the checked-in docs/volume_sweep.json artifact and say so in the UI.
  // Never hardcode a figure the API can supply.
  volume_curve?: unknown;
  volume_sweep?: unknown;
  savings_volume_curve?: unknown;
  /** Pooled cost edge at production volume, if the API computes it. */
  realistic_savings_pct_low?: number | null;
  realistic_savings_pct_high?: number | null;
  /** Share of the greedy baseline's landed cost that is fixed per-supplier fees. */
  fixed_fee_share_of_cost_pct?: number | null;
  /** Share of the headline saving attributable to avoided fixed per-supplier fees. */
  fixed_fee_share_of_savings_pct?: number | null;
  /** Per-supplier fixed freight fee actually charged by the cost model, in USD. */
  fixed_fee_per_supplier_usd?: number | null;
  /** Mean units per BOM in the benchmarked orders — the "tiny order" evidence. */
  mean_units_per_bom?: number | null;
  /** If the backend ships its own retraction note, it takes precedence. */
  headline_retracted?: boolean | null;
  retraction_note?: string | null;
  /**
   * `savings_pct` is POOLED as of 2026-09-03 — (Σ greedy − Σ MILP) / Σ greedy.
   * It used to be the unweighted mean of per-BOM percentages (50.67 on run 7)
   * while the volume curve beside it was pooled (47.22 at 1×), so the page
   * showed two different averages of the same quantity and labelled neither.
   * `savings_pct_mean_of_boms` is the old statistic, kept and named, never led with.
   */
  savings_pct_aggregation?: string | null;
  savings_pct_mean_of_boms?: number | null;
  savings_pct_mean_of_boms_note?: string | null;
  /** Every baseline arm in the run, not just the weakest one. */
  baselines?: BaselineComparison[] | null;
  /** The comparison's biggest known flaw — render it beside the number. */
  pool_asymmetry?: PoolAsymmetry | null;
  /** The like-for-like figure: MILP vs the ADD heuristic on the MATCHED pool.
   *  Null on a run written before the matched arms existed — render "not
   *  measured", never a zero and never the unmatched number in its place. */
  savings_pct_matched_pool?: number | null;
  savings_pct_matched_pool_arm?: string | null;
  savings_pct_matched_pool_note?: string | null;
  primary_claim?: string | null;
}

/** Mirrors `BaselineComparison` in backend/app/api/benchmark.py. */
interface BaselineComparison {
  arm: string;
  label: string;
  description: string;
  /** The catalogue this baseline shopped, in words. */
  pool: string;
  /** True when it shopped the SAME catalogue the optimizer was restricted to. */
  matched_pool: boolean;
  /** Exactly one baseline carries this: the like-for-like comparison. */
  is_primary: boolean;
  n_boms: number;
  total_cost_usd: number;
  milp_total_cost_usd: number;
  pooled_savings_pct: number;
  mean_of_boms_savings_pct: number;
  savings_usd_per_bom: number;
  savings_usd_annualized: number;
  avg_suppliers: number;
  suppliers_opened: number;
  international_suppliers_opened: number;
}

/** Mirrors `PoolAsymmetry` in backend/app/api/benchmark.py. */
interface PoolAsymmetry {
  matched: boolean;
  statement: string;
  greedy_pool: string;
  milp_pool: string;
  greedy_suppliers_opened: number;
  greedy_international_suppliers_opened: number;
  milp_suppliers_opened: number;
  milp_international_suppliers_opened: number;
  domestic_fixed_fee_usd?: number | null;
  international_fixed_fee_usd?: number | null;
  control_source?: string | null;
  control_n_boms?: number | null;
  control_greedy_cost_usd?: number | null;
  control_milp_domestic_pool_cost_usd?: number | null;
  control_milp_full_pool_cost_usd?: number | null;
  control_savings_pct_domestic_pool?: number | null;
  control_savings_pct_full_pool?: number | null;
  control_finding?: string | null;
  unmatched_side?: string | null;
  /** The greedy side of the match, MEASURED from this run's own rows (run >= 8). */
  matched_baseline_arm?: string | null;
  matched_n_boms?: number | null;
  matched_greedy_cost_usd?: number | null;
  matched_greedy_add_cost_usd?: number | null;
  matched_milp_cost_usd?: number | null;
  matched_savings_pct_vs_greedy?: number | null;
  matched_savings_pct_vs_greedy_add?: number | null;
  /** Percentage points of the unmatched headline, one per handicap. Sums to it. */
  points_from_weaker_heuristic?: number | null;
  points_from_wider_baseline_catalogue?: number | null;
  points_from_optimizer?: number | null;
  matched_finding?: string | null;
}

// ── The price-of-resilience frontier ─────────────────────────────────────────
// Mirrors `DiversificationFrontierResponse` in backend/app/api/benchmark.py.
// Everything is optional-tolerant: an older backend that does not serve
// `/benchmark/diversification-frontier` simply hides the section rather than
// falling back to a checked-in copy of numbers that could drift.

interface FrontierInterval {
  n: number;
  mean: number;
  ci95_low: number | null;
  ci95_high: number | null;
  significant: boolean;      // true ONLY when the interval EXCLUDES zero
  n_boot: number;
  seed: number;
  method: string;
}

interface FrontierPoint {
  k: number;
  n_boms_feasible: number;
  n_effective: number;
  boms_infeasible: string[];
  n_keeps_k1_suppliers: number;
  mean_total_cost_usd: number;
  mean_suppliers: number;
  mean_stress_cascade_risk: number | null;
  mean_stress_expected_shortfall: number | null;
  mean_targeted_cascade_risk: number | null;
  mean_targeted_expected_shortfall: number | null;
  delta_cost_usd: FrontierInterval | null;
  delta_stress_cascade_risk: FrontierInterval | null;
  delta_stress_expected_shortfall: FrontierInterval | null;
  delta_targeted_cascade_risk: FrontierInterval | null;
  delta_targeted_expected_shortfall: FrontierInterval | null;
  usd_per_unit_targeted_cascade_risk: number | null;
  usd_per_unit_targeted_cascade_risk_note: string | null;
  // Non-null ONLY where that k's risk change is significant in the WRONG
  // direction: diversification added risk, so there is no price of protection.
  // The magnitude is republished as dollars paid per unit of risk ADDED rather
  // than printed as a negative number under a heading that says "removed".
  usd_per_unit_targeted_cascade_risk_added: number | null;
  usd_per_unit_stress_cascade_risk: number | null;
  usd_per_unit_stress_cascade_risk_note: string | null;
  usd_per_unit_stress_cascade_risk_added: number | null;
}

interface FrontierStep {
  label: string;
  from_k: number;
  to_k: number;
  marginal_cost_usd: FrontierInterval | null;
  marginal_targeted_cascade_risk_removed: FrontierInterval | null;
  marginal_stress_cascade_risk_removed: FrontierInterval | null;
  marginal_targeted_expected_shortfall_removed: FrontierInterval | null;
  marginal_stress_expected_shortfall_removed: FrontierInterval | null;
  usd_per_unit_targeted_cascade_risk: number | null;
  usd_per_unit_targeted_cascade_risk_note: string | null;
  usd_per_unit_targeted_cascade_risk_added: number | null;
  usd_per_unit_stress_expected_shortfall: number | null;
  usd_per_unit_stress_expected_shortfall_note: string | null;
  usd_per_unit_stress_expected_shortfall_added: number | null;
  cost_multiple_vs_first_step: number | null;
}

interface FrontierNonMonotoneExample {
  bom: string;
  from_k: number;
  to_k: number;
  // WHICH broad-stress risk rises. This was expected shortfall and nothing else
  // until the supply graph was corrected to use all 8,176 supplier-part links;
  // on the fuller, more redundant graph expected shortfall falls monotonically
  // in k on every BOM and the counter-example is a cascade-risk one. The values
  // are generic and the measure travels with them so the page can never label a
  // p50 quantity as a mean.
  measure: string;
  measure_label: string;
  scenario: string;
  value_before: number;
  value_after: number;
  n_suppliers_before: number;
  n_suppliers_after: number;
  keeps_k1_suppliers: boolean;
}

interface DiversificationFrontier {
  available: boolean;
  source: string;
  unavailable_reason: string | null;
  generated_utc: string | null;
  finding: string;
  verdict: string;
  // The k the finding and the verdict are about, served by the API. This page
  // used to highlight `k === 2` with a bare numeral: nothing on screen was
  // false, because `_frontier_finding()` anchors on the same step, but a
  // literal in the client cannot follow the frontier if the frontier moves.
  // null = the API recommends no k (and `finding` / `verdict` are empty).
  recommended_k: number | null;
  recommended_k_basis: string;
  strategy: string | null;
  mc_scenarios: number | null;
  mc_seed: number | null;
  stress_factor: number | null;
  bootstrap_n: number | null;
  bootstrap_seed: number | null;
  n_boms_in_catalog: number | null;
  n_boms_included: number | null;
  boms_excluded: Record<string, string>;
  baseline_check: string;
  baseline_check_passed: boolean;
  aggregate_definition: string;
  points: FrontierPoint[];
  steps: FrontierStep[];
  // How far the price column reaches, and the sentence the API composes from
  // those counts. The "cheap second supplier, expensive third" collapse needs
  // TWO priced steps to be a claim at all; rendering only the multiple would
  // print nothing where there is one and leave the retracted story standing.
  n_steps_total: number;
  n_priced_steps: number;
  price_coverage: string;
  mean_suppliers_at_k1: number | null;
  nesting_caveat: string;
  non_monotone_example: FrontierNonMonotoneExample | null;
  // Always non-empty when the frontier is available — including when the scan
  // found nothing, which is a published retraction rather than a silent null.
  non_monotone_status: string;
  cost_axis_caveat: string;
  seed_caveat: string;
  quantisation_caveat: string;
  independence_caveat: string;
  n_effective_definition: string;
  caveats: string[];
}

interface FiedlerPoint {
  step: number;
  removed: number | null;
  removed_name: string | null;
  lambda2: number;
  delta_pct: number;
  collapsed_boms: string[];
}

interface FiedlerCurveData {
  points: FiedlerPoint[];
  baseline_lambda2: number;
  /** How many reference BOMs the collapse check covered. 0 = it did not run. */
  boms_checked?: number;
  /** Where those BOMs came from, and which graph they were checked against. */
  bom_source?: string;
}

// ── Formatting helpers ────────────────────────────────────────────────────────
// Resilience reductions arrive as raw fractions. plan_cascade_risk is a SHARE on
// 0-1 — 1 minus the median fraction of a BOM's lines that stay fulfillable across
// the Monte Carlo trials — and is NOT a probability: no base rate, no exposure
// window, and on the 4-line reference BOMs it can only be 0, .25, .5, .75 or 1.
// mc_cvar_95 is a ~1.0-2.0 cost multiplier. We display both as percentage points
// and treat anything under this magnitude as "no material difference".
//
// This cut is an ASSUMPTION, not a measured noise level: the benchmark is a single
// deterministic solve with no replicates, so there is no run-to-run variance to
// estimate one from. Never render it as "noise".
const RESILIENCE_MATERIALITY = 0.01; // 1 percentage point, assumed

// Monte Carlo ETA series colours. These are not decorative: recharts paints the
// legend LABEL with the series fill, so each one is 12px body text and must clear
// WCAG AA's 4.5:1 against the card it sits on. Measured there: slate-300 = 10.7:1,
// indigo-400 = 5.3:1. They also differ in lightness rather than only hue.
const MC_BASELINE_COLOR = '#cbd5e1';
const MC_GRAPH_AWARE_COLOR = '#818cf8';

/** Pull `source` / `generated_utc` off the API's volume-curve object, if served. */
function readCurveMeta(raw: unknown): { source?: string; generated?: string } {
  if (!raw || typeof raw !== 'object') return {};
  const o = raw as Record<string, unknown>;
  return {
    source: typeof o.source === 'string' ? o.source : undefined,
    generated: typeof o.generated_utc === 'string' ? o.generated_utc : undefined,
  };
}

function isMaterial(x: number | null | undefined): boolean {
  return typeof x === 'number' && Number.isFinite(x) && Math.abs(x) >= RESILIENCE_MATERIALITY;
}

/**
 * Percentage-POINT formatter. Only valid for quantities that are genuinely
 * SHARES on a 0–1 scale (here: plan_cascade_risk, the median unfulfilled-line
 * share). pp scaling is arithmetically fine on a share; calling the underlying
 * quantity a probability is not. Pass the signed CHANGE in the metric —
 * negative means the metric went down.
 */
function fmtPP(changeFraction: number | null | undefined): string {
  if (typeof changeFraction !== 'number' || !Number.isFinite(changeFraction)) return '—';
  const pp = changeFraction * 100;
  return `${pp > 0 ? '+' : ''}${pp.toFixed(2)} pp`;
}

/**
 * CVaR-95 is a cost MULTIPLIER (~1.0–2.0), not a percentage. A delta between two
 * multipliers is therefore measured in multiplier units ("×"), not percentage
 * points — calling it "pp" was wrong. Pass the signed change; negative is better.
 */
function fmtMultiplierDelta(change: number | null | undefined): string {
  if (typeof change !== 'number' || !Number.isFinite(change)) return '—';
  return `${change > 0 ? '+' : ''}${change.toFixed(4)}×`;
}

/** The same multiplier change expressed relative to the baseline multiplier. */
function fmtRelativeToBaseline(
  change: number | null | undefined,
  baseline: number | null | undefined,
): string | null {
  if (typeof change !== 'number' || !Number.isFinite(change)) return null;
  if (typeof baseline !== 'number' || !Number.isFinite(baseline) || baseline === 0) return null;
  const rel = (change / baseline) * 100;
  return `${rel > 0 ? '+' : ''}${rel.toFixed(2)}% of baseline`;
}

/**
 * Direction glyph derived from the SIGN of the value. Never hardcode an arrow
 * next to a signed number — that is how "↓" ends up sitting beside "+19.44".
 */
function deltaGlyph(change: number | null | undefined): string {
  if (typeof change !== 'number' || !Number.isFinite(change) || change === 0) return '→';
  return change < 0 ? '↓' : '↑';
}

/** Improvement = the metric went DOWN. Green when down, amber when up, grey at 0. */
function improvementColor(change: number | null | undefined, material: boolean): string {
  if (!material || typeof change !== 'number' || !Number.isFinite(change)) return '#94a3b8';
  return change < 0 ? '#10b981' : '#f59e0b';
}

/**
 * Format one bootstrap interval in the SAME units and the SAME sign convention
 * as the number it sits beneath.
 *
 * The API publishes each delta as a REDUCTION (blind − graph-aware); the tiles
 * render the signed CHANGE (graph-aware − blind), which is the negation. Flipping
 * a sign on an interval also swaps its endpoints — `flip` does both, so the low
 * bound stays the low bound. Getting this wrong is how an interval ends up
 * printed backwards under a number it is supposed to qualify.
 */
function fmtCiBand(
  ci: PairedBootstrapCI | undefined,
  fmt: (x: number | null | undefined) => string,
  flip: boolean,
): string | null {
  if (!ci || ci.ci95_low === null || ci.ci95_high === null) return null;
  const lo = flip ? -ci.ci95_high : ci.ci95_low;
  const hi = flip ? -ci.ci95_low : ci.ci95_high;
  return `${fmt(lo)} to ${fmt(hi)}`;
}

/**
 * The interval line under a resilience figure.
 *
 * Two states, and the amber one is the point of the component: when the CI
 * covers zero the page says so in words, in place, next to the number — it does
 * not quietly print a mean and let the reader assume it survived. The mean stays
 * visible (hiding a measurement is its own dishonesty) but it is labelled as not
 * distinguishable from zero, and the tile's colour is neutralised by the caller.
 */
function CiNote({
  ci,
  fmt,
  flip = false,
}: {
  ci: PairedBootstrapCI | undefined;
  fmt: (x: number | null | undefined) => string;
  flip?: boolean;
}) {
  const band = fmtCiBand(ci, fmt, flip);
  if (!ci || !band) return null;
  const panel = `n=${ci.n} BOMs${ci.n_effective !== ci.n ? ` (${ci.n_effective} effective)` : ''}`;
  if (!ci.significant) {
    return (
      // 12px, not 11px: this is prose, not a caption, and the gate's rule is that
      // sub-12px BODY text is the anti-pattern. It also matches the significant
      // branch below — the caveat must not read as smaller print than the claim.
      <p
        className="text-xs text-amber-400/90 mt-1.5 leading-snug"
        title={ci.method}
      >
        95% CI {band} — <span className="font-semibold">covers zero</span>. Not
        distinguishable from no effect on {panel}; not a result.
      </p>
    );
  }
  return (
    <p className="text-xs text-slate-400 mt-1.5 leading-snug" title={ci.method}>
      95% CI {band} · excludes zero · {panel}
    </p>
  );
}

/**
 * The saturation line under a CVaR-95 figure — backlog item 13.
 *
 * CVaR-95 here is a mean over the worst-5% tail of
 * `1 + unfulfillable_share × EMERGENCY_COST_PREMIUM`, which is bounded, so it
 * tops out at the served `cvar95_ceiling` and stops moving. The unit is the BOM
 * PAIR: where BOTH arms of a BOM are at the ceiling, that BOM's contribution to
 * the delta above is 0.0 BECAUSE THE METRIC RAN OUT OF ROOM — and a reader
 * looking at a 0.0000× delta with no flag would reasonably conclude the two
 * plans are equally exposed. They are not; the measurement simply cannot tell.
 * This says so, in place, names the tied BOMs, and prints the measure that still
 * discriminates.
 *
 * Three states, and they must stay three: `true` (pinned), `false` (measured and
 * not pinned — nothing to say), and `null`/`undefined` (this run predates the
 * columns, so the question is UNANSWERED, which is not the same as "no").
 */
function CvarSaturationNote({
  saturated,
  ceiling,
  tiedBoms,
  nBoms,
  shortfallReduction,
}: {
  saturated: boolean | null | undefined;
  ceiling: number | null | undefined;
  tiedBoms: string[] | null | undefined;
  nBoms: number | null | undefined;
  shortfallReduction: number | null | undefined;
}) {
  if (saturated === false) return null;
  if (saturated === null || saturated === undefined) {
    return (
      <p className="text-xs text-slate-400 mt-1.5 leading-snug">
        Saturation not measured on this run — a 0.0000&times; delta here may be the
        metric&rsquo;s ceiling rather than equal exposure.
      </p>
    );
  }
  // The count comes from the served list, never from a guess about the panel:
  // the flag fires on ONE ceiling-tied pair, so "every row is pinned" would be
  // an overstatement of exactly the kind this note exists to correct.
  const nTied = tiedBoms?.length ?? 0;
  const denom = typeof nBoms === 'number' && nBoms > 0 ? ` of ${nBoms}` : '';
  return (
    // `break-words`: the tied BOM names are unbreakable snake_case tokens
    // ("medical_monitoring_device" is ~152px at 12px) and this paragraph renders
    // inside a two-column grid cell that is ~155px wide at a 390px viewport.
    // Without it a long name is the fourth recurrence of horizontal overflow.
    <p className="text-xs text-amber-400/90 mt-1.5 leading-snug break-words">
      <span className="font-semibold">
        Pinned at the CVaR-95 ceiling
        {typeof ceiling === 'number' ? ` (${ceiling.toFixed(4)}×)` : ''}
      </span>{' '}
      on {nTied}
      {denom} BOM{nTied === 1 ? '' : 's'}
      {tiedBoms && tiedBoms.length > 0 ? (
        <span className="text-slate-400"> ({tiedBoms.join(', ')})</span>
      ) : null}
      : both arms there sit at the most this metric can report, so those BOMs contribute
      an exact 0 to the delta above by arithmetic, not because the plans are equally
      exposed.
      {typeof shortfallReduction === 'number' ? (
        <>
          {' '}The measure that still resolves: graph-aware sourcing changes P(every BOM
          line unfulfillable) by{' '}
          <span className="tabular-nums font-semibold">
            {/* served as blind − graph, so the change under graph-aware is its negation */}
            {shortfallReduction > 0 ? '−' : shortfallReduction < 0 ? '+' : ''}
            {Math.abs(shortfallReduction).toFixed(4)}
          </span>
          {shortfallReduction > 0
            ? ' — a real reduction the tail metric cannot see.'
            : shortfallReduction < 0
              ? ' — it is WORSE off, which the tied CVaR figure hides.'
              : ' — genuinely unchanged on this measure too.'}
        </>
      ) : null}
    </p>
  );
}

function fmtPct(x: number | null | undefined, digits = 1): string {
  if (typeof x !== 'number' || !Number.isFinite(x)) return '—';
  return `${x > 0 ? '+' : ''}${x.toFixed(digits)}%`;
}

/**
 * Money, to the cent, with its sign intact.
 *
 * Two bugs shipped here at once. `maximumFractionDigits` with no
 * `minimumFractionDigits` renders 643.1 as "$643.1", so a column of costs read
 * "$368.34 / $427.22 / $527.57 / $643.1" — a ragged decimal that looks like a
 * typo and breaks `tabular-nums` alignment. And `Math.abs()` silently DELETED
 * the sign, so a negative cost-per-unit-of-risk-removed rendered identically to
 * a positive one. Callers that supply their own direction (a "cheaper" /
 * "more expensive" word beside the figure) must pass `Math.abs(x)` explicitly;
 * callers wanting an explicit `+` on positives want `fmtSignedUsd`.
 */
function fmtUsd(x: number | null | undefined): string {
  if (typeof x !== 'number' || !Number.isFinite(x)) return '—';
  const sign = x < 0 ? '−' : '';
  return `${sign}$${Math.abs(x).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

// ── Frontier helpers ─────────────────────────────────────────────────────────

/** A risk share on 0-1, three decimals. Never a percentage: it is not a rate. */
function fmtShare(x: number | null | undefined, digits = 3): string {
  if (typeof x !== 'number' || !Number.isFinite(x)) return '—';
  return x.toFixed(digits);
}

/** Dollars with an explicit sign, so a cost INCREASE never reads as a saving. */
function fmtSignedUsd(x: number | null | undefined): string {
  if (typeof x !== 'number' || !Number.isFinite(x)) return '—';
  const sign = x > 0 ? '+' : x < 0 ? '−' : '';
  return `${sign}$${Math.abs(x).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

/** Backtick code spans, in the same chip treatment this page uses for paths. */
function renderCodeSpans(text: string, keyPrefix: string): ReactNode[] {
  return text.split(/(`[^`]+`)/g).map((part, i) =>
    part.startsWith('`') && part.endsWith('`') && part.length > 2 ? (
      <code key={`${keyPrefix}-${i}`} className="bg-slate-800 px-1 rounded text-slate-300">
        {part.slice(1, -1)}
      </code>
    ) : (
      <span key={`${keyPrefix}-${i}`}>{part}</span>
    )
  );
}

/**
 * The published caveat strings carry markdown emphasis: one leading
 * `**bold title.**` segment AND backtick code spans naming the constants and
 * fields being caveated (see `backend/seeds/run_diversification_sweep.py`
 * CAVEATS). Splitting on `**` alone handled the title and printed all twelve
 * code spans as literal backticks — `LTL_BASE_FEE_USD` with the marks showing.
 *
 * The split is nested rather than alternated because backticks also run INSIDE
 * the bold segment: two caveats open with a bold title whose first word is a
 * code span, and treating the two markers as alternatives in one split left
 * those printing their own backticks inside the title.
 */
function renderCaveatProse(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith('**') && part.endsWith('**') && part.length > 4 ? (
      <strong key={i} className="text-slate-200 font-semibold">
        {renderCodeSpans(part.slice(2, -2), `b${i}`)}
      </strong>
    ) : (
      <span key={i}>{renderCodeSpans(part, `t${i}`)}</span>
    )
  );
}

/**
 * Render the SERVED verdict, not a copy of it.
 *
 * These two lines used to be hardcoded JSX literals — "Buy the second supplier."
 * and "Do not buy the third." — gated only on `frontier.verdict` being truthy.
 * They happened to match what `_frontier_finding()` sends, so nothing on screen
 * was wrong, but the section footer claims "nothing on this page is a hardcoded
 * copy of it" and with those literals in place that claim was FALSE: a wording
 * change in the backend would have diverged silently while the page kept
 * asserting fidelity. Every glyph rendered here now comes out of the response.
 *
 * The affirmative/negative styling is derived from each sentence's OWN wording
 * rather than from its position, so an unrecognised sentence falls back to the
 * affirmative treatment instead of being mislabelled.
 */
function renderVerdict(verdict: string): ReactNode[] {
  return verdict
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((sentence, i) => {
      const negative = /^(do\s+not|don't|never|avoid)\b/i.test(sentence);
      return (
        <span
          key={i}
          className={`inline-flex items-center gap-2 ${negative ? 'text-red-300' : 'text-emerald-300'}`}
        >
          {negative ? <Ban size={18} aria-hidden="true" /> : <CheckCircle2 size={18} aria-hidden="true" />}
          {sentence}
        </span>
      );
    });
}

/**
 * One cell of a "$ per unit of risk removed" column.
 *
 * Three states, and the third is the one that used to be wrong. A price is only
 * a price of PROTECTION when protection was bought. Where the interval covers
 * zero there is no denominator; where it excludes zero on the OTHER side the
 * plan ADDED risk, and the API withholds the removed-price and republishes the
 * magnitude under `_added`. Rendering that as a signed number in a column headed
 * "removed" is how the doc came to print `$-1,910.71` as a price of protection.
 */
function PriceCell({
  removed,
  added,
  emphasis,
  baselineLabel,
}: {
  removed: number | null | undefined;
  added: number | null | undefined;
  emphasis?: boolean;
  baselineLabel?: string;
}) {
  if (typeof removed === 'number') {
    return (
      <span className={`tabular-nums font-semibold ${emphasis ? 'text-emerald-300' : 'text-slate-200'}`}>
        {fmtUsd(removed)}
      </span>
    );
  }
  if (typeof added === 'number') {
    return (
      <span className="inline-flex flex-col items-end gap-0.5">
        <span className="inline-flex items-center gap-1 text-xs font-semibold text-red-300">
          <AlertTriangle size={13} aria-hidden="true" />
          risk ADDED
        </span>
        <span className="text-xs text-slate-400 tabular-nums">
          {fmtUsd(added)} per unit added
        </span>
      </span>
    );
  }
  if (baselineLabel) {
    return <span className="text-xs text-slate-400">{baselineLabel}</span>;
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-amber-300">
      <AlertTriangle size={13} aria-hidden="true" />
      no price — CI covers zero
    </span>
  );
}

/** The literal endpoints under a strip, so the picture is never the only record. */
function fmtIntervalText(
  ci: FrontierInterval | null | undefined,
  fmt: (x: number | null | undefined) => string,
): string {
  if (!ci || ci.ci95_low === null || ci.ci95_high === null) return '—';
  return `[${fmt(ci.ci95_low)}, ${fmt(ci.ci95_high)}]`;
}

/**
 * A single domain shared by every strip in one column, always containing zero.
 *
 * Strips drawn on per-row domains are not comparable to each other and the zero
 * line moves between rows, which defeats the entire point of drawing them.
 */
function ciDomain(intervals: (FrontierInterval | null | undefined)[]): [number, number] {
  const vals: number[] = [0];
  for (const ci of intervals) {
    if (!ci) continue;
    for (const v of [ci.ci95_low, ci.ci95_high, ci.mean]) {
      if (typeof v === 'number' && Number.isFinite(v)) vals.push(v);
    }
  }
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const pad = (hi - lo || 1) * 0.08;
  return [lo - pad, hi + pad];
}

/**
 * The verdict in words beside every interval, because the strip is a picture
 * and a picture is not a claim. "covers zero" is said out loud, never implied
 * by a colour.
 */
function CiVerdict({ ci }: { ci: FrontierInterval | null | undefined }) {
  if (!ci || ci.ci95_low === null || ci.ci95_high === null) {
    return <span className="text-xs text-slate-400">—</span>;
  }
  return ci.significant ? (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-300">
      <CheckCircle2 size={13} aria-hidden="true" />
      excludes zero
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-amber-300">
      <AlertTriangle size={13} aria-hidden="true" />
      covers zero
    </span>
  );
}

/**
 * The five series on the frontier chart.
 *
 * LINE STYLE carries the measure and COLOUR carries the scenario, so no series
 * is identified by colour alone — every dash pattern is distinct, the key below
 * the chart reproduces the exact pattern, and every plotted value is repeated as
 * text in the tables underneath.
 */
const FRONTIER_SERIES: Array<{
  key: string; name: string; color: string; dash: string; axis: 'cost' | 'risk';
}> = [
  { key: 'cost', name: 'Mean landed cost (USD per BOM)', color: '#818cf8', dash: '', axis: 'cost' },
  { key: 'targetedCascade', name: 'Cascade risk, targeted outage (share 0–1)', color: '#f87171', dash: '9 4', axis: 'risk' },
  { key: 'stressCascade', name: 'Cascade risk, broad stress (share 0–1)', color: '#fbbf24', dash: '2 4', axis: 'risk' },
  { key: 'targetedShortfall', name: 'E[shortfall], targeted outage (share 0–1)', color: '#fb7185', dash: '12 4 3 4', axis: 'risk' },
  { key: 'stressShortfall', name: 'E[shortfall], broad stress (share 0–1)', color: '#fcd34d', dash: '5 4 1 4', axis: 'risk' },
];

/**
 * The chart's legend, drawn with the real stroke patterns rather than swatches.
 *
 * Mounted with `verticalAlign="top"`, ABOVE the plot. Recharts stacks a
 * bottom-aligned legend immediately under the x-axis — exactly where an
 * `insideBottom` axis label lands — and the two collided at every viewport (the
 * whole 286x15px caption sat inside the legend's box, not a near miss). Margin
 * cannot fix that: the label is positioned off the axis and the legend is
 * stacked off the same axis, so both move together. Getting the legend out of
 * the label's band is the fix.
 */
function FrontierChartKey() {
  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-2 px-1">
      {FRONTIER_SERIES.map((s) => (
        <li key={s.key} className="flex items-center gap-2 text-xs text-slate-400">
          <svg width="26" height="8" aria-hidden="true" className="shrink-0">
            <line
              x1="0" y1="4" x2="26" y2="4"
              stroke={s.color} strokeWidth="2"
              strokeDasharray={s.dash || undefined}
            />
          </svg>
          {s.name}
        </li>
      ))}
    </ul>
  );
}

// ── KPI Card ──────────────────────────────────────────────────────────────────
function KpiCard({
  title, value, sub, accent, delay = 0,
}: {
  title: string; value: string | number; sub: string; accent: string; delay?: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.4, ease: 'easeOut' }}
      className={`bg-slate-800/70 border rounded-xl p-4 flex flex-col gap-1 backdrop-blur-sm ${accent}`}
    >
      <span className="text-slate-400 text-xs font-semibold uppercase tracking-wider">{title}</span>
      <span className="text-3xl font-semibold text-white tabular-nums">{value}</span>
      <span className="text-slate-400 text-xs">{sub}</span>
    </motion.div>
  );
}

// ── Custom Dot for Fiedler LineChart ──────────────────────────────────────────
function FiedlerDot(props: {
  cx?: number; cy?: number; payload?: FiedlerPoint;
  selectedStep: number | null;
  onSelect: (step: number) => void;
}) {
  const { cx = 0, cy = 0, payload, selectedStep, onSelect } = props;
  if (!payload) return null;

  const isSelected = payload.step === selectedStep;
  const r = isSelected ? 7 : 5;
  const stroke = isSelected ? '#6366f1' : 'none';
  const strokeWidth = isSelected ? 3 : 0;

  const handleClick = () => onSelect(payload.step);
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect(payload.step);
    }
  };

  return (
    <g>
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill="#ef4444"
        stroke={stroke}
        strokeWidth={strokeWidth}
        style={{ cursor: 'pointer' }}
      />
      {/* Invisible larger hit area for accessibility */}
      <circle
        cx={cx}
        cy={cy}
        r={12}
        fill="transparent"
        role="button"
        tabIndex={0}
        aria-label={payload.removed_name
          ? `Remove ${payload.removed_name}, lambda2 ${payload.lambda2.toFixed(3)}, delta ${payload.delta_pct.toFixed(1)}%`
          : `Baseline, lambda2 ${payload.lambda2.toFixed(3)}`}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        style={{ cursor: 'pointer', outline: 'none' }}
      />
    </g>
  );
}

// ── Main BenchmarkPage ────────────────────────────────────────────────────────
export default function BenchmarkPage() {
  const [summary, setSummary] = useState<BenchmarkSummary | null>(null);
  const [fiedler, setFiedler] = useState<FiedlerCurveData | null>(null);
  const [frontier, setFrontier] = useState<DiversificationFrontier | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<'empty' | 'error' | null>(null);
  // The λ₂ curve is OPTIONAL and gets its own outcome. It used to share a
  // Promise.all — and therefore a single .catch — with the summary, so one failed
  // fiedler request replaced the whole page (retraction headline, resilience
  // findings, frontier, Monte Carlo chart, per-BOM table) with a full-viewport
  // "Benchmark summary unavailable", which was simply untrue in that case, and whose
  // Retry button reissued the same coupled call.
  const [fiedlerLoading, setFiedlerLoading] = useState(true);
  const [fiedlerError, setFiedlerError] = useState(false);
  const [selectedStep, setSelectedStep] = useState<number | null>(null);

  const loadSummary = useCallback(async () => {
    setError(null);
    setLoading(true);
    try {
      const s = await benchmarkAPI.summary();
      setSummary(s.data);
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      setError(status === 404 ? 'empty' : 'error');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadFiedler = useCallback(async () => {
    setFiedlerError(false);
    setFiedlerLoading(true);
    try {
      const f = await benchmarkAPI.fiedlerCurve();
      setFiedler(f.data);
    } catch {
      setFiedler(null);
      setFiedlerError(true);
    } finally {
      setFiedlerLoading(false);
    }
  }, []);

  useEffect(() => { void loadSummary(); }, [loadSummary]);
  useEffect(() => { void loadFiedler(); }, [loadFiedler]);

  // The frontier is fetched SEPARATELY and never blocks the page: it reads a
  // committed artifact, so a deployment that predates the artifact should lose
  // this one section rather than the whole benchmark.
  useEffect(() => {
    api
      .get<DiversificationFrontier>('/benchmark/diversification-frontier')
      .then((r) => setFrontier(r.data))
      .catch(() => setFrontier(null));
  }, []);

  // ── Loading state ────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="min-h-full bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 overflow-y-auto h-full flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 rounded-full border-2 border-indigo-500 border-t-transparent animate-spin" />
          <span className="text-slate-400 text-sm">Loading benchmark results…</span>
        </div>
      </div>
    );
  }

  // ── Empty state ──────────────────────────────────────────────────────────────
  if (error === 'empty') {
    return (
      <div className="min-h-full bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 overflow-y-auto h-full flex items-center justify-center">
        <div className="flex flex-col items-center justify-center h-96 gap-4">
          <h2 className="text-3xl font-semibold text-slate-300">No benchmark run found</h2>
          <p className="text-sm text-slate-400 text-center max-w-md">
            This deployment has no recorded benchmark runs yet, so there is nothing to compare.
          </p>
        </div>
      </div>
    );
  }

  // ── Error state ──────────────────────────────────────────────────────────────
  if (error === 'error') {
    return (
      <div className="min-h-full bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 overflow-y-auto h-full flex items-center justify-center">
        <div className="flex flex-col items-center justify-center h-96 gap-4">
          <h2 className="text-amber-400 text-3xl font-semibold">Benchmark summary unavailable</h2>
          <p className="text-sm text-slate-400 text-center max-w-md">
            Benchmark summary unavailable. Confirm the backend is running and optimization_runs has rows.
          </p>
          <button
            onClick={() => { void loadSummary(); }}
            className="bg-slate-800 border border-slate-700 px-3 py-2 rounded text-sm text-slate-300 hover:bg-slate-700 transition-colors focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 focus:ring-offset-slate-950"
          >
            Retry Loading Benchmark
          </button>
        </div>
      </div>
    );
  }

  if (!summary) return null;

  // ── Derived values ────────────────────────────────────────────────────────────
  const formattedTimestamp = summary.timestamp
    ? new Date(summary.timestamp).toLocaleDateString('en-US', {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })
    : '—';

  // Resilience materiality: are graph-aware's disruption protections real, or ~0
  // on this catalog? Render the honest callout either way — never fake a win.
  const resilienceReductions = [
    summary.resilience.stress_cascade_risk_reduction,
    summary.resilience.stress_cvar95_reduction,
    summary.resilience.targeted_cascade_risk_reduction,
    summary.resilience.targeted_cvar95_reduction,
  ];
  const resilienceHasMaterialEffect = resilienceReductions.some(isMaterial);

  // Run 5 (2026-08-27) splits by scenario: graph-aware is materially SAFER under a
  // targeted outage and materially WORSE under broad stress. The page previously had
  // only two states — "measurably lowers risk" or "within noise" — so a directional
  // split rendered as an unqualified win. It is not one, and saying so is the finding.
  const stressReductions = [
    summary.resilience.stress_cascade_risk_reduction,
    summary.resilience.stress_cvar95_reduction,
  ];
  const targetedReductions = [
    summary.resilience.targeted_cascade_risk_reduction,
    summary.resilience.targeted_cvar95_reduction,
  ];

  // Monte Carlo chart data (ETA distribution, baseline/blind vs graph-aware MILP)
  const mcData = [
    {
      name: 'P10',
      Baseline: summary.monte_carlo.baseline_p10,
      'Graph-Aware': summary.monte_carlo.graph_aware_p10,
    },
    {
      name: 'P50',
      Baseline: summary.monte_carlo.baseline_p50,
      'Graph-Aware': summary.monte_carlo.graph_aware_p50,
    },
    {
      name: 'P90',
      Baseline: summary.monte_carlo.baseline_p90,
      'Graph-Aware': summary.monte_carlo.graph_aware_p90,
    },
  ];

  // Fiedler chart data
  const fiedlerData = fiedler?.points.map((pt) => ({
    name: pt.step === 0 ? 'Baseline' : (pt.removed_name ?? `Step ${pt.step}`),
    lambda2: pt.lambda2,
    step: pt.step,
    delta_pct: pt.delta_pct,
    collapsed_boms: pt.collapsed_boms,
    removed_name: pt.removed_name,
  })) ?? [];

  const selectedPoint = fiedler?.points.find((p) => p.step === selectedStep) ?? null;
  // Provenance for the collapse column. `boms_checked === 0` means the check did
  // not run, which is NOT the same as "nothing collapsed" — the page said the
  // latter for both cases while the backend never wrote the key at all.
  const fiedlerBomsChecked = fiedler?.boms_checked ?? 0;
  const fiedlerBomSource = fiedler?.bom_source ?? '';

  // Risk color for tradeoff losing-axis severity
  const tradeoffRiskScore = Math.min(1.0, Math.abs(summary.tradeoff.delta_pct) / 20.0);
  const tradeoffRiskLevel = riskLabel(tradeoffRiskScore);
  const tradeoffColor = RISK_COLORS[tradeoffRiskLevel];

  const suppliersAccent = summary.avg_suppliers_milp < summary.avg_suppliers_greedy
    ? 'border-emerald-500/30'
    : 'border-slate-700';

  // ── The retraction ──────────────────────────────────────────────────────────
  // summary.savings_pct is the headline this project publicly retracted. It is
  // measured on the benchmark's own toy orders (4 BOM lines, single-digit unit
  // counts) where a fixed per-supplier freight fee dominates landed cost. Prefer
  // the API's own volume curve if it serves one; otherwise use the checked-in
  // docs/volume_sweep.json artifact. Either way we show the decay, not the peak.
  const apiCurve =
    normalizeVolumeCurve(summary.volume_curve) ??
    normalizeVolumeCurve(summary.volume_sweep) ??
    normalizeVolumeCurve(summary.savings_volume_curve);
  const curveIsFromApi = apiCurve !== null;
  const volumeCurve = apiCurve ?? VOLUME_SWEEP_FALLBACK;
  // The curve is NOT a benchmark run. It is its own sweep artifact — both arms on
  // the same international offer pool, 10 BOMs — so labelling it "run N" (it used
  // to say "run 4") attributed it to an experiment that never produced it.
  // `_load_volume_curve()` takes no run argument and is cached for the process.
  const curveMeta = readCurveMeta(
    summary.volume_curve ?? summary.volume_sweep ?? summary.savings_volume_curve,
  );
  const curveSource = curveIsFromApi
    ? `GET /benchmark/summary — pooled live by the API from ${
        curveMeta.source ?? 'docs/volume_sweep.json'
      }${curveMeta.generated ? `, generated ${curveMeta.generated}` : ''}. A standalone sweep, not a benchmark run: greedy and MILP both see the full international offer pool.`
    : VOLUME_SWEEP_FALLBACK_SOURCE;
  // The reference line must come from THIS curve, not from the withdrawn headline
  // of a different experiment (us_only MILP vs international greedy, 9 BOMs,
  // pre-fix solver). Anchor it on the curve's own 1× point.
  const curveAnchorPoint =
    volumeCurve.find((pt) => pt.multiplier === 1) ?? volumeCurve[0] ?? null;

  // Honest headline: the pooled edge across the production-volume tail of the curve.
  const curveRange = productionVolumeRange(volumeCurve);
  const honestLow = typeof summary.realistic_savings_pct_low === 'number'
    ? summary.realistic_savings_pct_low
    : curveRange?.low ?? null;
  const honestHigh = typeof summary.realistic_savings_pct_high === 'number'
    ? summary.realistic_savings_pct_high
    : curveRange?.high ?? null;
  const honestRangeLabel = honestLow !== null && honestHigh !== null
    ? `${honestLow.toFixed(1)}–${honestHigh.toFixed(1)}%`
    : '—';

  // Decomposition at the smallest (benchmarked) order size on the curve.
  const tinyOrderPoint = volumeCurve[0] ?? null;
  const feeShareOfSavings = typeof summary.fixed_fee_share_of_savings_pct === 'number'
    ? summary.fixed_fee_share_of_savings_pct
    : tinyOrderPoint?.fee_share_of_saving_pct ?? null;
  const feeShareOfCost = typeof summary.fixed_fee_share_of_cost_pct === 'number'
    ? summary.fixed_fee_share_of_cost_pct
    : tinyOrderPoint?.greedy_fixed_share_of_cost_pct ?? null;
  const tinyOrderUnitsLabel = typeof summary.mean_units_per_bom === 'number'
    ? `${summary.mean_units_per_bom.toLocaleString()} units per BOM`
    : (tinyOrderPoint?.units_min !== undefined && tinyOrderPoint?.units_max !== undefined
        ? `${tinyOrderPoint.units_min}–${tinyOrderPoint.units_max} units per BOM`
        : 'single-digit unit counts per BOM');
  const perSupplierFeeUsd = typeof summary.fixed_fee_per_supplier_usd === 'number'
    ? summary.fixed_fee_per_supplier_usd
    : null;

  // ── Baselines and the offer-pool asymmetry ────────────────────────────────
  // Both are served by /benchmark/summary as of 2026-09-03 and both are rendered
  // defensively: an older backend simply hides the block rather than the page
  // inventing a number for it. `greedy_add` has existed in the benchmark database
  // on every run since the 2.0 schema and was never surfaced anywhere, so the
  // published figure was the optimizer's edge over the weakest available baseline.
  const baselines = Array.isArray(summary.baselines) ? summary.baselines : [];
  const greedyBaseline = baselines.find((b) => b.arm === 'greedy') ?? null;
  const greedyAddBaseline = baselines.find((b) => b.arm === 'greedy_add') ?? null;
  // The like-for-like baseline: same heuristic class, same catalogue, same cost
  // function. Matched by the backend's own `is_primary` flag rather than by an
  // arm name hard-coded here, so the page can never disagree with the API about
  // which of the four comparisons is the claim.
  const primaryBaseline = baselines.find((b) => b.is_primary) ?? null;
  const matchedPoolPct =
    typeof summary.savings_pct_matched_pool === 'number'
      ? summary.savings_pct_matched_pool
      : null;
  const poolAsymmetry = summary.pool_asymmetry ?? null;
  const meanOfBoms = typeof summary.savings_pct_mean_of_boms === 'number'
    ? summary.savings_pct_mean_of_boms
    : null;

  // Resilience metrics, rendered as signed CHANGES (negative = the metric fell).
  const stressCascadeChange = -summary.resilience.stress_cascade_risk_reduction;
  const stressCvarChange = -summary.resilience.stress_cvar95_reduction;
  const targetedCascadeChange = -summary.resilience.targeted_cascade_risk_reduction;
  const targetedCvarChange = -summary.resilience.targeted_cvar95_reduction;

  // ── Paired bootstrap intervals (item 12) ──────────────────────────────────
  // Every figure in this section is a mean over 9 BOMs, several of which select
  // an identical plan in both arms and contribute a hard zero. The lead-time
  // model may only ship by beating its baselines with a paired bootstrap CI
  // excluding zero; until 2026-08-28 this page published the benchmark's means
  // with no interval at all. `ciOf` reads the API's interval for a metric and
  // `sigOf` says whether it cleared zero — a metric that did NOT clear zero is
  // rendered neutral, never green, so a null finding cannot read as a win.
  const resilienceIntervals = summary.resilience.intervals;
  const ciOf = (metric: string): PairedBootstrapCI | undefined =>
    resilienceIntervals ? resilienceIntervals[metric] : undefined;
  // Fallback is `true` ONLY when the API served no intervals at all (older
  // build): in that case the page falls back to its previous materiality-only
  // behaviour rather than blanking every figure. When intervals ARE served, an
  // absent or non-significant one is treated as not significant.
  const sigOf = (metric: string): boolean =>
    resilienceIntervals ? Boolean(resilienceIntervals[metric]?.significant) : true;

  // Materiality alone cannot claim a DIRECTION — that needs the interval, so this
  // must sit AFTER sigOf. The paired bootstrap (10,000 resamples over the BOM
  // clusters) puts stress_cascade at [-27.78, +5.56] pp and stress_cvar95 at
  // [0.0000, +0.0043]: both cover zero. "Graph-aware is 8.33 pp WORSE under stress"
  // is therefore not supportable in either direction — it is a mean with a sign and
  // an interval that spans no-effect.
  const STRESS_KEYS = ['stress_cascade_risk_reduction', 'stress_cvar95_reduction'];
  const TARGETED_KEYS = ['targeted_cascade_risk_reduction', 'targeted_cvar95_reduction'];
  const stressIsWorse = stressReductions.some((v, i) => isMaterial(v) && v < 0 && sigOf(STRESS_KEYS[i]));
  const targetedIsBetter = targetedReductions.some((v, i) => isMaterial(v) && v > 0 && sigOf(TARGETED_KEYS[i]));
  const stressIsIndistinguishable = !STRESS_KEYS.some((k) => sigOf(k));
  const resilienceIsSplit = targetedIsBetter && (stressIsWorse || stressIsIndistinguishable);
  const fmtPct2 = (x: number | null | undefined) => fmtPct(x, 2);
  const nSignificant = summary.resilience.significant_metrics?.length ?? null;
  const nNonSignificant = summary.resilience.non_significant_metrics?.length ?? null;
  const nEffectiveBoms = summary.resilience.n_effective_boms ?? null;
  const zeroPlanBoms = ciOf('targeted_cascade_risk_reduction')?.zero_plan_boms ?? [];
  const bootstrapResamples = ciOf('targeted_cascade_risk_reduction')?.n_boot ?? null;
  const bootstrapSeed = ciOf('targeted_cascade_risk_reduction')?.seed ?? null;

  // Is the nominal cost premium itself material? If it is, we must NOT also claim
  // cost was "held roughly flat" — that was the page contradicting itself.
  const nominalPremiumPct = summary.resilience.nominal_cost_premium_pct;
  const materialityPct = summary.materiality_threshold_pct ?? 2;
  const materialityBasis = summary.materiality_threshold_basis ?? null;
  const nominalPremiumIsMaterial =
    Number.isFinite(nominalPremiumPct) && Math.abs(nominalPremiumPct) > materialityPct;

  // ── Price-of-resilience frontier: derived views over the API payload ───────
  // Every figure below is read off the response. Nothing is recomputed here that
  // the endpoint already publishes, and nothing is invented when it does not.
  const frontierPoints = frontier?.points ?? [];
  const frontierSteps = frontier?.steps ?? [];

  const frontierChartData = frontierPoints.map((p) => ({
    k: p.k,
    cost: p.mean_total_cost_usd,
    targetedCascade: p.mean_targeted_cascade_risk,
    stressCascade: p.mean_stress_cascade_risk,
    targetedShortfall: p.mean_targeted_expected_shortfall,
    stressShortfall: p.mean_stress_expected_shortfall,
  }));

  // One domain per column of strips, shared across rows and always containing
  // zero — strips on per-row domains are not comparable and the zero line moves.
  const costDomain = ciDomain(frontierPoints.map((p) => p.delta_cost_usd));
  const riskDomain = ciDomain([
    ...frontierPoints.map((p) => p.delta_targeted_cascade_risk),
    ...frontierPoints.map((p) => p.delta_stress_cascade_risk),
  ]);
  const marginalRiskDomain = ciDomain([
    ...frontierSteps.map((s) => s.marginal_targeted_cascade_risk_removed),
    ...frontierSteps.map((s) => s.marginal_stress_expected_shortfall_removed),
  ]);

  // How far the price column reaches is READ, not recomputed: `price_coverage`
  // and `n_priced_steps` come from the API, which composes the sentence from the
  // same counts. The page used to derive a first/second priced step here and
  // render a hardcoded "the next one costs N× more" paragraph; with one priced
  // step that paragraph simply vanished and the reader was left with the
  // collapse story the section's prose still implied.

  // k values where the MILP could not be solved for every BOM — the k = 5 row is
  // a smaller panel than the rows above it and must not be read as the same
  // comparison.
  const infeasibleKs = frontierPoints.filter((p) => p.boms_infeasible.length > 0);
  const infeasibleNote = infeasibleKs.length
    ? `${infeasibleKs
        .map(
          (p) =>
            `At k=${p.k}, ${p.boms_infeasible.length} BOM${
              p.boms_infeasible.length === 1 ? '' : 's'
            } (${p.boms_infeasible.join(', ')}) are infeasible`,
        )
        .join('; ')}. Those rows are a SMALLER panel than the rows above them and are not the same comparison — every row publishes its own panel size.`
    : null;

  // ── Render ────────────────────────────────────────────────────────────────────
  return (
    <div className="min-h-full bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 overflow-y-auto h-full">
      <div className="max-w-7xl mx-auto px-6 py-8">

        {/* ── Page Header ──────────────────────────────────────────────────────── */}
        <motion.div
          initial={{ opacity: 0, y: -12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
          className="flex items-start justify-between mb-8"
        >
          <div>
            <h1 className="text-3xl font-semibold text-white">Benchmark: Optimization &amp; Resilience</h1>
            <p className="text-sm text-slate-400 mt-1">
              All {summary.n_boms} reference BOMs · seed=42 · run {summary.run_id} — {formattedTimestamp}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1.5 bg-green-500/10 border border-green-500/30 text-green-400 text-xs px-3 py-1.5 rounded-full font-semibold uppercase tracking-wider">
              <span className="w-1.5 h-1.5 rounded-full bg-green-400" />
              All {summary.n_boms} BOMs · Seed 42
            </span>
            {summary.feeds_fallback && (
              <span className="inline-flex items-center gap-1.5 bg-amber-500/10 border border-amber-500/30 text-amber-400 text-xs px-3 py-1.5 rounded-full font-semibold uppercase tracking-wider">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                Static Feeds
              </span>
            )}
          </div>
        </motion.div>

        {/* ── Stale-feed banner ─────────────────────────────────────────────────── */}
        {summary.feeds_fallback && (
          <div className="mb-4 text-xs text-amber-400 bg-amber-500/10 border border-amber-500/30 rounded-lg px-3 py-2">
            Benchmark generated with static-fallback feeds — live data was unavailable at run time.
          </div>
        )}

        {/* ══════════════════════════════════════════════════════════════════════
            SECTION 1 — VALUE OF OPTIMIZATION
            The headline this project retracted, and the number that replaced it.
            The retracted figure is deliberately NOT the largest thing on screen.
           ══════════════════════════════════════════════════════════════════════ */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0, duration: 0.4, ease: 'easeOut' }}
          className="bg-slate-800/70 border border-amber-500/40 rounded-xl overflow-hidden mb-5"
        >
          {/* Retraction banner — the first thing the eye lands on */}
          <div className="bg-amber-500/15 border-b border-amber-500/30 px-6 py-3 flex items-start gap-3">
            <Ban className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" aria-hidden="true" />
            <div>
              <span className="text-xs font-bold uppercase tracking-widest text-amber-300">
                Retracted headline — do not quote this number
              </span>
              <p className="text-sm text-amber-100/80 mt-1 leading-relaxed">
                {summary.retraction_note ?? (
                  <>
                    This page used to lead with the figure below as the value of optimization. We audited it and
                    withdrew it. It is arithmetically correct and substantively meaningless: it is measured on
                    orders of {tinyOrderUnitsLabel}, where a fixed per-supplier freight fee
                    {perSupplierFeeUsd !== null ? ` (${fmtUsd(perSupplierFeeUsd)} per supplier)` : ''} is almost the
                    entire cost being optimized. The optimizer wins by consolidating suppliers and dodging that fee,
                    not by sourcing better.
                  </>
                )}
              </p>
            </div>
          </div>

          <div className="p-6 grid grid-cols-1 lg:grid-cols-[auto_1fr] gap-6 items-start">
            {/* The retracted number — demoted: small, grey, struck through, labelled */}
            <div className="flex-shrink-0">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Withdrawn figure (tiny-order regime)
              </span>
              <div
                className="text-2xl font-semibold leading-tight tabular-nums mt-1 text-slate-400 line-through decoration-amber-500/70 decoration-2"
                aria-live="polite"
              >
                {fmtPct(summary.savings_pct)}
              </div>
              <span className="text-xs text-slate-400">
                run {summary.run_id} · {summary.n_boms} BOMs · 1× order size · pooled
              </span>
              {/* The disclosure travels with the number, in the same visual breath.
                  It used to live only in the response's `caveats` array, which this
                  page never rendered. */}
              {poolAsymmetry && (
                <p className="text-xs text-amber-200/80 mt-2 leading-snug max-w-[22rem]">
                  <span className="font-semibold text-amber-300">And not like-for-like:</span>{' '}
                  the greedy baseline behind this number shops the full international catalogue and opened{' '}
                  <span className="tabular-nums">{poolAsymmetry.greedy_international_suppliers_opened}</span> of its{' '}
                  <span className="tabular-nums">{poolAsymmetry.greedy_suppliers_opened}</span> suppliers abroad; the
                  optimizer was restricted to domestic distributors and opened{' '}
                  <span className="tabular-nums">{poolAsymmetry.milp_international_suppliers_opened}</span>.
                  {typeof poolAsymmetry.international_fixed_fee_usd === 'number'
                    && typeof poolAsymmetry.domestic_fixed_fee_usd === 'number' && (
                    <>
                      {' '}Every foreign supplier costs{' '}
                      {fmtUsd(poolAsymmetry.international_fixed_fee_usd)} in air-freight fee against{' '}
                      {fmtUsd(poolAsymmetry.domestic_fixed_fee_usd)} domestic, so part of the gap is a shipping
                      policy the two arms did not share.
                    </>
                  )}
                  {matchedPoolPct !== null && (
                    <>
                      {' '}That gap is now measured, not argued:{' '}
                      <span className="text-emerald-300 tabular-nums font-semibold">
                        {fmtPct(matchedPoolPct)}
                      </span>{' '}
                      is the same comparison with the catalogues matched.
                    </>
                  )}
                </p>
              )}
              {/* The like-for-like figure, standing on its own beside the withdrawn
                  one. Deliberately NOT folded into `savings_pct`: the withdrawn
                  number keeps its name so a reader can see which is which. */}
              {matchedPoolPct !== null && primaryBaseline && (
                <div className="mt-4 rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3 max-w-[22rem]">
                  <span className="text-xs font-semibold uppercase tracking-wider text-emerald-300">
                    Like-for-like, same offer pool
                  </span>
                  <div className="text-3xl font-semibold leading-tight tabular-nums mt-1 text-emerald-300">
                    {fmtPct(matchedPoolPct)}
                  </div>
                  <span className="text-xs text-slate-400">
                    vs the ADD heuristic on the optimizer's own domestic pool · {primaryBaseline.n_boms} BOMs ·
                    1× order size · pooled
                  </span>
                  {summary.primary_claim && (
                    <p className="text-xs text-slate-400 mt-2 leading-snug">{summary.primary_claim}</p>
                  )}
                </div>
              )}
              {matchedPoolPct === null && (
                <p className="text-xs text-amber-200/80 mt-3 leading-snug max-w-[22rem]">
                  <span className="font-semibold text-amber-300">Not measured on this run.</span>{' '}
                  {summary.savings_pct_matched_pool_note
                    ?? 'This run predates the pool-matched baselines, so no like-for-like figure exists for it.'}
                </p>
              )}
              {meanOfBoms !== null && (
                <p className="text-xs text-slate-500 mt-2 leading-snug max-w-[22rem]">
                  Pooled — total saved over total spent. The unweighted mean of the per-BOM percentages is{' '}
                  <span className="tabular-nums">{fmtPct(meanOfBoms)}</span>; it reads higher because the biggest
                  percentages land on the smallest BOMs, and it is not a share of spend. This page served{' '}
                  <em>that</em> statistic as the headline until 2026-09-03, beside the pooled curve below.
                </p>
              )}
            </div>

            {/* The honest number — the biggest thing in this card */}
            <div className="lg:border-l lg:border-slate-700 lg:pl-6">
              <span className="text-xs font-semibold uppercase tracking-wider text-emerald-400">
                Honest cost edge at production volume
              </span>
              <div className="text-5xl font-semibold leading-tight tabular-nums mt-2 text-emerald-400">
                {honestRangeLabel}
              </div>
              <p className="text-sm text-slate-400 mt-2 leading-relaxed">
                Pooled MILP-vs-greedy landed-cost advantage once the same BOMs are re-solved at{' '}
                {PRODUCTION_VOLUME_MIN_MULTIPLIER.toLocaleString()}× the benchmark's quantities and above.{' '}
                <span className="text-slate-300">Within the sweep below</span> the solver, the offer pool and the
                objective are held fixed and only the order size changes. Both this and the withdrawn figure on the
                left are now the same <em>pooled</em> statistic, but they are still not two points on one line: the
                sweep runs a 10-BOM cohort with both arms on the same offer pool, the benchmark run a 9-BOM cohort
                with the arms on different pools, and the production-volume tail is a smaller cohort again (stock
                ceilings drop BOMs as volume rises). This is the number to quote.
              </p>
            </div>
          </div>
        </motion.div>

        {/* ── Volume-decay curve: watch the headline evaporate ─────────────────── */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.06, duration: 0.4, ease: 'easeOut' }}
          className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5"
          aria-label="Cost advantage versus order volume — the pooled advantage over the naive, globally-shopping greedy baseline decays from roughly 47 percent on toy orders to low single digits at production volume. Against a like-for-like baseline on the same offer pool the toy-order figure is far smaller; see the baselines table."
        >
          <h2 className="text-2xl font-semibold text-slate-300">Why the headline was withdrawn</h2>
          <p className="text-xs text-slate-400 mt-1 mb-4">
            The optimizer's cost advantage is a function of how small the order is. Re-solve the same BOMs at
            larger quantities and it decays monotonically until the fixed fee stops mattering.
          </p>
          <VolumeDecayCurve
            points={volumeCurve}
            headlineValue={curveAnchorPoint?.savings_pct ?? null}
            headlineLabel={`This curve at ${(curveAnchorPoint?.multiplier ?? 1).toLocaleString()}×`}
            source={curveSource}
          />
          <p className="text-xs text-slate-400 mt-2 leading-relaxed">
            The dashed line is this curve's own {(curveAnchorPoint?.multiplier ?? 1).toLocaleString()}× point
            {curveAnchorPoint ? ` (${curveAnchorPoint.savings_pct.toFixed(2)}%)` : ''} — the anchor the decay is
            measured from, and deliberately not the withdrawn {fmtPct(summary.savings_pct)} headline, which comes
            from a different cohort ({summary.n_boms} BOMs, arms on different offer pools).
          </p>
          {/* The matched-pool control, in the API's own words — this sweep carries a
              third arm that re-solves the benchmark's MILP on greedy's full pool, so
              "how much of the headline is the shipping policy?" has a measured answer
              rather than an argument. Rendered, never paraphrased. */}
          {(poolAsymmetry?.control_finding || poolAsymmetry?.matched_finding) && (
            <div className="mt-3 rounded-lg border border-slate-700 bg-slate-900/40 p-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Matched-pool control
                {poolAsymmetry.control_source ? ` · ${poolAsymmetry.control_source}` : ''}
              </span>
              <p className="text-xs text-slate-300 mt-1 leading-relaxed">{poolAsymmetry.control_finding}</p>
              {/* The other direction of the match — the greedy arm re-solved on
                  the optimizer's pool. Served in the API's own words so the page
                  cannot paraphrase a measured result into a different one. */}
              {poolAsymmetry.matched_finding && (
                <p className="text-xs text-emerald-200/80 mt-2 leading-relaxed">
                  {poolAsymmetry.matched_finding}
                </p>
              )}
              {poolAsymmetry.unmatched_side && (
                <p
                  className={
                    poolAsymmetry.matched
                      ? 'text-xs text-slate-400 mt-2 leading-relaxed'
                      : 'text-xs text-amber-200/70 mt-2 leading-relaxed'
                  }
                >
                  {poolAsymmetry.unmatched_side}
                </p>
              )}
            </div>
          )}
        </motion.div>

        {/* ── Decomposition: where the saving actually came from ───────────────── */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.09, duration: 0.4, ease: 'easeOut' }}
          className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5"
        >
          <h2 className="text-2xl font-semibold text-slate-300">Decomposition of the withdrawn saving</h2>
          <p className="text-xs text-slate-400 mt-1 mb-4">
            Breaking the 1×-order saving into its cost terms. Positive = the greedy baseline paid more, i.e. the
            optimizer won on that term.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="bg-slate-900/50 border border-amber-500/25 rounded-lg p-4">
              <span className="text-xs font-semibold uppercase tracking-wider text-amber-400">
                Avoided fixed per-supplier fees
              </span>
              <div className="text-2xl font-semibold text-amber-300 tabular-nums mt-1">
                {tinyOrderPoint?.fixed_fee_usd !== undefined
                  ? fmtSignedUsd(tinyOrderPoint.fixed_fee_usd)
                  : '—'}
              </div>
              <span className="text-xs text-slate-400">
                {feeShareOfSavings !== null
                  ? `${feeShareOfSavings.toFixed(0)}% of the entire saving`
                  : 'the dominant term'}
              </span>
            </div>
            <div className="bg-slate-900/50 border border-slate-700 rounded-lg p-4">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Component cost
              </span>
              <div
                className="text-2xl font-semibold tabular-nums mt-1"
                style={{ color: (tinyOrderPoint?.component_usd ?? 0) < 0 ? '#f87171' : '#10b981' }}
              >
                {tinyOrderPoint?.component_usd !== undefined
                  ? fmtSignedUsd(tinyOrderPoint.component_usd)
                  : '—'}
              </div>
              <span className="text-xs text-slate-400">
                {(tinyOrderPoint?.component_usd ?? 0) < 0
                  ? 'negative — the optimizer pays MORE for the parts themselves'
                  : 'the optimizer also bought parts cheaper'}
              </span>
            </div>
            <div className="bg-slate-900/50 border border-slate-700 rounded-lg p-4">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Variable freight (weight × distance)
              </span>
              <div className="text-2xl font-semibold text-slate-300 tabular-nums mt-1">
                {tinyOrderPoint?.variable_freight_usd !== undefined
                  ? fmtSignedUsd(tinyOrderPoint.variable_freight_usd)
                  : '—'}
              </div>
              <span className="text-xs text-slate-400">
                rounding error at 1× — it only becomes the real lever at volume
              </span>
            </div>
          </div>

          <p className="text-sm text-slate-300 leading-relaxed mt-4">
            The greedy baseline picks the cheapest offer per BOM line, which makes it the component-cost minimum
            by construction — the optimizer <em>cannot</em> beat it on parts, and doesn't. It only wins on fixed
            charges.
            {feeShareOfSavings !== null && feeShareOfSavings > 100 && (
              <>
                {' '}Avoided fees are <span className="text-amber-300 font-semibold">{feeShareOfSavings.toFixed(0)}%</span>{' '}
                of the total saving — over 100%, because every other term is a net loss.
              </>
            )}
            {feeShareOfCost !== null && (
              <>
                {' '}At 1×, fixed per-supplier fees are{' '}
                <span className="text-amber-300 font-semibold">{feeShareOfCost.toFixed(1)}%</span> of the greedy
                baseline's entire landed cost. Optimizing that basket is optimizing a freight fee, not a supply chain.
              </>
            )}
          </p>
        </motion.div>

        {/* ── Optimization stat row (all tiny-order-regime figures) ─────────────── */}
        <div className="flex items-center gap-2 mb-2 px-1">
          <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0" aria-hidden="true" />
          <span className="text-xs font-semibold uppercase tracking-wider text-amber-400">
            The three cards below inherit the retraction — same 1× toy-order regime
          </span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-2">
          <KpiCard
            title="MILP vs greedy · $ / BOM run"
            value={fmtUsd(summary.savings_usd_per_bom)}
            sub="mean landed-cost gap, optimizer vs greedy baseline, one reorder at 1× order size — mostly avoided freight fees"
            accent="border-amber-500/30"
            delay={0.05}
          />
          <KpiCard
            title="MILP vs greedy · $ / year (est.)"
            value={fmtUsd(summary.savings_usd_annualized)}
            sub={`the card to its left × ${summary.annual_reorders} reorders/yr — a disclosed assumption, not a measured cadence, on a retracted per-run figure`}
            accent="border-amber-500/30"
            delay={0.1}
          />
          <KpiCard
            title="Suppliers consolidated"
            value={`${summary.avg_suppliers_greedy.toFixed(1)} → ${summary.avg_suppliers_milp.toFixed(1)}`}
            sub="avg distinct suppliers per BOM, greedy → MILP — this consolidation is real, and it is the entire mechanism behind the withdrawn number"
            accent={suppliersAccent}
            delay={0.15}
          />
        </div>
        {/* ── Which baseline? All four of them. ───────────────────────────────
            A baseline is defined by its heuristic AND by the catalogue it may
            shop, and the withdrawn headline was handicapped on both axes: the
            weakest heuristic, shopping a wider catalogue than the optimizer was
            allowed. The pipeline now solves {naive, ADD} x {international,
            MATCHED domestic} and all four are shown pooled, side by side, so the
            descent from the retracted number to the defensible one is visible
            rather than argued. */}
        {baselines.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.18, duration: 0.4, ease: 'easeOut' }}
            className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5 overflow-x-auto"
          >
            <h2 className="text-2xl font-semibold text-slate-300">Which baseline is it beating?</h2>
            <p className="text-xs text-slate-400 mt-1 mb-4 max-w-3xl leading-relaxed">
              The withdrawn headline is measured against the naive greedy baseline shopping the full
              international catalogue — the weakest comparison available on both axes. The pipeline also solves
              an ADD heuristic, and both heuristics again on the optimizer's own domestic-only pool. Only the
              last row compares like with like: same catalogue, same cost function, nothing different but the
              algorithm. That is the number worth defending, and it is the smallest of the four.
            </p>
            <table className="w-full text-sm min-w-[46rem]">
              <thead>
                <tr className="text-left text-xs text-slate-400 uppercase tracking-wider border-b border-slate-700">
                  <th className="py-2 pr-4">Baseline</th>
                  <th className="py-2 pr-4">Offer pool</th>
                  <th className="py-2 pr-4 text-right">Pooled saving</th>
                  <th className="py-2 pr-4 text-right">Mean of BOMs</th>
                  <th className="py-2 pr-4 text-right">Baseline spend</th>
                  <th className="py-2 pr-4 text-right">Suppliers opened</th>
                  <th className="py-2 pr-4 text-right">of which abroad</th>
                </tr>
              </thead>
              <tbody>
                {baselines.map((b) => (
                  <tr
                    key={b.arm}
                    className={
                      b.is_primary
                        ? 'border-b border-emerald-500/40 bg-emerald-500/5 text-slate-200 align-top'
                        : 'border-b border-slate-800/70 text-slate-300 align-top'
                    }
                  >
                    <td className="py-2 pr-4">
                      <span className={b.is_primary ? 'text-emerald-300 font-semibold' : 'text-slate-200'}>
                        {b.label}
                      </span>
                      {b.is_primary && (
                        <span className="ml-2 align-middle text-xs font-semibold uppercase tracking-wider text-emerald-300 border border-emerald-500/40 rounded px-1.5 py-0.5">
                          the claim
                        </span>
                      )}
                      <span className="block text-xs text-slate-500 mt-0.5 max-w-md leading-snug">
                        {b.description}
                      </span>
                    </td>
                    <td className="py-2 pr-4 align-top">
                      <span
                        className={
                          b.matched_pool
                            ? 'text-xs font-semibold uppercase tracking-wider text-emerald-300 border border-emerald-500/40 rounded px-1.5 py-0.5 whitespace-nowrap'
                            : 'text-xs font-semibold uppercase tracking-wider text-amber-300 border border-amber-500/40 rounded px-1.5 py-0.5 whitespace-nowrap'
                        }
                      >
                        {b.matched_pool ? 'matched' : 'wider than MILP'}
                      </span>
                      <span className="block text-xs text-slate-500 mt-1 max-w-[16rem] leading-snug">
                        {b.pool}
                      </span>
                    </td>
                    <td
                      className={
                        b.is_primary
                          ? 'py-2 pr-4 text-right tabular-nums text-emerald-300 font-semibold'
                          : 'py-2 pr-4 text-right tabular-nums text-amber-300 font-semibold'
                      }
                    >
                      {fmtPct(b.pooled_savings_pct)}
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums text-slate-500">
                      {fmtPct(b.mean_of_boms_savings_pct)}
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums">{fmtUsd(b.total_cost_usd)}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">{b.suppliers_opened}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">
                      {b.international_suppliers_opened}
                    </td>
                  </tr>
                ))}
                <tr className="text-slate-400">
                  <td className="py-2 pr-4">Optimizer (blind MILP), domestic pool only</td>
                  <td className="py-2 pr-4 text-xs text-slate-500 leading-snug">
                    domestic (US) distributors only — the catalogue every matched baseline above shares
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums">—</td>
                  <td className="py-2 pr-4 text-right tabular-nums">—</td>
                  <td className="py-2 pr-4 text-right tabular-nums">
                    {greedyBaseline ? fmtUsd(greedyBaseline.milp_total_cost_usd) : '—'}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums">
                    {poolAsymmetry ? poolAsymmetry.milp_suppliers_opened : '—'}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums">
                    {poolAsymmetry ? poolAsymmetry.milp_international_suppliers_opened : '—'}
                  </td>
                </tr>
              </tbody>
            </table>
            {greedyBaseline && greedyAddBaseline && primaryBaseline && (
              <p className="text-xs text-slate-400 mt-3 leading-relaxed max-w-3xl">
                Removing the two handicaps one at a time takes the pooled figure from{' '}
                <span className="text-slate-200 tabular-nums">
                  {fmtPct(greedyBaseline.pooled_savings_pct)}
                </span>{' '}
                to{' '}
                <span className="text-slate-200 tabular-nums">
                  {fmtPct(greedyAddBaseline.pooled_savings_pct)}
                </span>{' '}
                (competent heuristic) and then to{' '}
                <span className="text-emerald-300 tabular-nums font-semibold">
                  {fmtPct(primaryBaseline.pooled_savings_pct)}
                </span>{' '}
                (same catalogue as the optimizer), on the same {primaryBaseline.n_boms} BOMs against the same
                optimizer plans.
                {poolAsymmetry
                  && typeof poolAsymmetry.points_from_weaker_heuristic === 'number'
                  && typeof poolAsymmetry.points_from_wider_baseline_catalogue === 'number'
                  && typeof poolAsymmetry.points_from_optimizer === 'number' && (
                  <>
                    {' '}Of the {fmtPct(greedyBaseline.pooled_savings_pct)}, about{' '}
                    <span className="text-slate-200 tabular-nums">
                      {poolAsymmetry.points_from_weaker_heuristic.toFixed(2)} points
                    </span>{' '}
                    were the baseline being bad at consolidation and{' '}
                    <span className="text-slate-200 tabular-nums">
                      {poolAsymmetry.points_from_wider_baseline_catalogue.toFixed(2)} points
                    </span>{' '}
                    were it shopping a catalogue the optimizer was never allowed — a shipping policy, not an
                    optimization result. The remaining{' '}
                    <span className="text-emerald-300 tabular-nums font-semibold">
                      {poolAsymmetry.points_from_optimizer.toFixed(2)} points
                    </span>{' '}
                    are the optimizer's.
                  </>
                )}
              </p>
            )}
            {baselines.length > 0 && !primaryBaseline && (
              <p className="text-xs text-amber-200/80 mt-3 leading-relaxed max-w-3xl">
                This run carries no pool-matched baseline, so no like-for-like figure is shown. The matched arms
                were added to the benchmark pipeline on 2026-09-03 and exist only from run 8 onward; every
                percentage above is measured against a baseline shopping a wider catalogue than the optimizer.
              </p>
            )}
            {poolAsymmetry && (
              <p className="text-xs text-amber-200/80 mt-3 leading-relaxed max-w-3xl">
                {poolAsymmetry.statement}
              </p>
            )}
            <p className="text-xs text-slate-500 mt-3 leading-relaxed">
              Fleet-wide across {summary.n_boms} BOMs (run {summary.run_id}) at the benchmark's own 1× order size.
              Per-BOM ledger in{' '}
              <code className="bg-slate-800 px-1 rounded">docs/BENCHMARK_RESULTS.md</code>; the volume sweep behind
              the retraction is <code className="bg-slate-800 px-1 rounded">docs/volume_sweep.json</code>, written
              up in <code className="bg-slate-800 px-1 rounded">docs/BENCHMARK_VOLUME_CURVE.md</code>.
            </p>
          </motion.div>
        )}

        {/* ══════════════════════════════════════════════════════════════════════
            SECTION 2 — VALUE OF RESILIENCE (graph-aware vs blind MILP, honest)
           ══════════════════════════════════════════════════════════════════════ */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.15, duration: 0.4, ease: 'easeOut' }}
          className="mt-8 mb-4"
        >
          <span className="text-xs font-semibold uppercase tracking-wider text-indigo-400">
            Value of Resilience
          </span>
          <h2 className="text-white text-2xl font-semibold mt-1">Graph-aware vs blind MILP under disruption</h2>
          <p className="text-sm text-slate-400 mt-1">
            Both arms are already MILP-optimized, so this is a different question from the section above: does
            routing around high-centrality distributors lower tail risk when one is disrupted, and what does that
            protection cost in the nominal (no-disruption) world? On run {summary.run_id} it is{' '}
            {nominalPremiumIsMaterial ? 'not free' : 'roughly free'} — see the premium below.
          </p>
        </motion.div>

        {/* ── Nominal premium + dollar framing ─────────────────────────────────── */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-5">
          <div
            className="bg-slate-800/70 border border-slate-700 rounded-xl p-4"
            title="Mean (graph-aware − blind) / blind landed cost in the nominal (no-disruption) world. Expect ~0: graph-aware should not cost materially more when nothing is disrupted."
          >
            <span className="text-slate-400 text-xs font-semibold uppercase tracking-wider">
              Nominal cost premium {deltaGlyph(summary.resilience.nominal_cost_premium_pct)}
            </span>
            <div
              className="text-3xl font-semibold tabular-nums mt-1"
              style={{
                color:
                  isMaterial(summary.resilience.nominal_cost_premium_pct / 100)
                  && sigOf('nominal_cost_premium_pct')
                    ? '#f59e0b'
                    : '#94a3b8',
              }}
            >
              {fmtPct(summary.resilience.nominal_cost_premium_pct, 2)}
            </div>
            <CiNote ci={ciOf('nominal_cost_premium_pct')} fmt={fmtPct2} />
            <span className="text-slate-400 text-xs">
              graph-aware vs blind MILP, no disruption ·{' '}
              <span className="text-amber-400/90">
                {/* The word carries the direction, so the figure is a magnitude —
                    "−$12.34 cheaper" would read as a double negative. */}
                {fmtUsd(Math.abs(summary.cost_delta_usd))}{' '}
                {summary.cost_delta_usd <= 0 ? 'cheaper' : 'more expensive'} / BOM run
              </span>
            </span>
            <p className="text-xs text-slate-400 mt-2 leading-relaxed border-t border-slate-700/60 pt-2">
              Different comparison from the cards above. Those measure{' '}
              <span className="text-slate-400">optimizer vs greedy baseline</span> ({fmtUsd(summary.savings_usd_per_bom)}{' '}
              gap). This measures <span className="text-slate-400">graph-aware MILP vs blind MILP</span> — both
              already optimized. The two figures are not the same quantity and do not net against each other:
              paying {fmtUsd(Math.abs(summary.cost_delta_usd))} more for graph-aware routing is the price of the resilience
              below, not a reversal of the optimization result.
            </p>
          </div>
          <div
            className="bg-slate-800/70 border border-amber-500/20 rounded-xl p-4"
            title="CVaR-95 (Conditional VaR / Expected Shortfall) = mean emergency-procurement cost multiplier over the worst-5% Monte Carlo scenarios. Field: mc_cvar_95 / baseline_cvar_95."
          >
            <span className="text-slate-400 text-xs font-semibold uppercase tracking-wider">
              CVaR-95 / Expected Shortfall · spend at risk
            </span>
            <div className="text-3xl font-semibold text-amber-400 tabular-nums mt-1">
              {fmtUsd(summary.baseline_spend_at_risk_usd)}
            </div>
            <span className="text-slate-400 text-xs">
              extra spend exposed in worst-5% scenarios · mean per blind-MILP BOM
            </span>
          </div>
        </div>

        {/* ── Stress / Targeted scenario reductions ────────────────────────────── */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-5">
          <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">Stress scenario</span>
            <p className="text-xs text-slate-400 mt-1 mb-3">Broad disruption (stress_factor=3) applied to every distributor</p>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <span className="text-xs text-slate-400 uppercase tracking-wider">
                  Cascade risk {deltaGlyph(stressCascadeChange)}
                </span>
                <div
                  className="text-2xl font-semibold tabular-nums mt-1"
                  style={{
                    color: improvementColor(
                      stressCascadeChange,
                      isMaterial(stressCascadeChange) && sigOf('stress_cascade_risk_reduction'),
                    ),
                  }}
                >
                  {fmtPP(stressCascadeChange)}
                </div>
                <span
                  className="text-xs text-slate-400"
                  title="plan_cascade_risk = 1 − the median fraction of the BOM's lines that stay fulfillable across the Monte Carlo trials. A share on 0–1, not a probability: it has no base rate and no exposure window, and on 4-line BOMs it can only take the values 0, .25, .5, .75, 1."
                >
                  change in median unfulfilled-line share (0–1)
                </span>
                <CiNote ci={ciOf('stress_cascade_risk_reduction')} fmt={fmtPP} flip />
              </div>
              <div>
                <span className="text-xs text-slate-400 uppercase tracking-wider">
                  CVaR-95 {deltaGlyph(stressCvarChange)}
                </span>
                <div
                  className="text-2xl font-semibold tabular-nums mt-1"
                  style={{
                    color: improvementColor(
                      stressCvarChange,
                      isMaterial(stressCvarChange) && sigOf('stress_cvar95_reduction'),
                    ),
                  }}
                >
                  {fmtMultiplierDelta(stressCvarChange)}
                </div>
                <span className="text-xs text-slate-400">
                  change in cost multiplier
                  {fmtRelativeToBaseline(stressCvarChange, summary.monte_carlo?.baseline_cvar_95)
                    ? ` · ${fmtRelativeToBaseline(stressCvarChange, summary.monte_carlo?.baseline_cvar_95)}`
                    : ''}
                </span>
                <CiNote ci={ciOf('stress_cvar95_reduction')} fmt={fmtMultiplierDelta} flip />
                <CvarSaturationNote
                  saturated={summary.resilience.stress_cvar95_saturated}
                  ceiling={summary.resilience.cvar95_ceiling}
                  tiedBoms={summary.resilience.stress_cvar95_ceiling_tied_boms}
                  nBoms={summary.resilience.n_boms ?? summary.n_boms}
                  shortfallReduction={summary.resilience.stress_p_total_shortfall_reduction}
                />
              </div>
            </div>
          </div>
          <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">Targeted scenario</span>
            <p className="text-xs text-slate-400 mt-1 mb-3">Single highest-betweenness distributor in the BOM's pool goes fully offline</p>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <span className="text-xs text-slate-400 uppercase tracking-wider">
                  Cascade risk {deltaGlyph(targetedCascadeChange)}
                </span>
                <div
                  className="text-2xl font-semibold tabular-nums mt-1"
                  style={{
                    color: improvementColor(
                      targetedCascadeChange,
                      isMaterial(targetedCascadeChange) && sigOf('targeted_cascade_risk_reduction'),
                    ),
                  }}
                >
                  {fmtPP(targetedCascadeChange)}
                </div>
                <span
                  className="text-xs text-slate-400"
                  title="plan_cascade_risk = 1 − the median fraction of the BOM's lines that stay fulfillable across the Monte Carlo trials. A share on 0–1, not a probability: it has no base rate and no exposure window, and on 4-line BOMs it can only take the values 0, .25, .5, .75, 1."
                >
                  change in median unfulfilled-line share (0–1)
                </span>
                <CiNote ci={ciOf('targeted_cascade_risk_reduction')} fmt={fmtPP} flip />
              </div>
              <div>
                <span className="text-xs text-slate-400 uppercase tracking-wider">
                  CVaR-95 {deltaGlyph(targetedCvarChange)}
                </span>
                <div
                  className="text-2xl font-semibold tabular-nums mt-1"
                  style={{
                    color: improvementColor(
                      targetedCvarChange,
                      isMaterial(targetedCvarChange) && sigOf('targeted_cvar95_reduction'),
                    ),
                  }}
                >
                  {fmtMultiplierDelta(targetedCvarChange)}
                </div>
                <span className="text-xs text-slate-400">
                  change in cost multiplier
                  {fmtRelativeToBaseline(targetedCvarChange, summary.monte_carlo?.baseline_cvar_95)
                    ? ` · ${fmtRelativeToBaseline(targetedCvarChange, summary.monte_carlo?.baseline_cvar_95)}`
                    : ''}
                </span>
                <CiNote ci={ciOf('targeted_cvar95_reduction')} fmt={fmtMultiplierDelta} flip />
                <CvarSaturationNote
                  saturated={summary.resilience.targeted_cvar95_saturated}
                  ceiling={summary.resilience.cvar95_ceiling}
                  tiedBoms={summary.resilience.targeted_cvar95_ceiling_tied_boms}
                  nBoms={summary.resilience.n_boms ?? summary.n_boms}
                  shortfallReduction={summary.resilience.targeted_p_total_shortfall_reduction}
                />
              </div>
            </div>
          </div>
        </div>

        {/* ── How the intervals were made, and what survived them ─────────────── */}
        {resilienceIntervals && (
          <div className="bg-slate-900/60 border border-slate-700 rounded-xl p-4 mb-5">
            <span className="text-xs font-semibold uppercase tracking-wider text-indigo-400">
              Statistical treatment
            </span>
            <p className="text-xs text-slate-400 mt-2 leading-relaxed">
              Each figure above is a <span className="text-slate-300">mean over{' '}
              {summary.resilience.n_boms ?? summary.n_boms} BOMs</span>, and every one now carries a 95%{' '}
              <span className="text-slate-300">paired percentile-bootstrap interval</span>
              {bootstrapResamples !== null && bootstrapSeed !== null
                ? ` (${bootstrapResamples.toLocaleString()} resamples, seed ${bootstrapSeed})`
                : ''}
              . The resample unit is the <span className="text-slate-300">BOM</span>, not the row: both arms of a
              per-BOM delta come from the same BOM under the same simulation seed, so the BOM is the independent
              cluster. Nothing was re-solved — the intervals are computed from the per-BOM values run{' '}
              {summary.run_id} already stored, so no published mean moved.
            </p>
            {nEffectiveBoms !== null && zeroPlanBoms.length > 0 && (
              <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                <span className="font-semibold text-slate-300">Effective n is smaller than n.</span>{' '}
                {zeroPlanBoms.length} of {summary.resilience.n_boms ?? summary.n_boms} BOMs ({' '}
                {zeroPlanBoms.map((b, i) => (
                  <span key={b}>
                    {i > 0 ? ', ' : ''}
                    <code className="bg-slate-800 px-1 rounded">{b}</code>
                  </span>
                ))}{' '}
                ) select an identical plan in both arms, so they contribute an exact 0.0 to every delta and carry no information
                about the treatment. They drag every mean toward zero <em>and</em> narrow the apparent spread. The
                effective panel is <span className="text-slate-300">n = {nEffectiveBoms}</span>; both panels' intervals
                are served by the API and neither is hidden.
              </p>
            )}
            {nSignificant !== null && nNonSignificant !== null && (
              <p className="text-xs text-slate-400 mt-2 leading-relaxed border-t border-slate-700/60 pt-2">
                <span className="font-semibold text-slate-300">
                  {nSignificant} of {nSignificant + nNonSignificant} deltas clear zero.
                </span>{' '}
                {nNonSignificant > 0 ? (
                  <>
                    The other {nNonSignificant} —{' '}
                    <span className="text-amber-400">
                      {(summary.resilience.non_significant_metrics ?? []).join(', ')}
                    </span>{' '}
                    — have intervals that cover zero and are marked as such above. They are shown, not deleted, and
                    they are not counted as findings. On a nine-BOM benchmark that is the expected outcome, and
                    publishing it is the point: this is the same bar the lead-time model is held to on the model
                    card, and a benchmark exempt from it would be a double standard rather than a result.
                  </>
                ) : (
                  <>Every delta's interval excludes zero on this panel.</>
                )}
              </p>
            )}
          </div>
        )}

        {/* ── Honest callout: real win vs ~0 finding, never fabricated ─────────── */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2, duration: 0.4 }}
          className={`rounded-xl p-5 mb-5 border ${
            resilienceHasMaterialEffect
              ? 'bg-emerald-500/5 border-emerald-500/20'
              : 'bg-amber-500/5 border-amber-500/20'
          }`}
        >
          <div className="flex items-start gap-3">
            {resilienceHasMaterialEffect && !resilienceIsSplit ? (
              <CheckCircle2 className="w-5 h-5 text-emerald-400 flex-shrink-0 mt-0.5" />
            ) : (
              <AlertTriangle className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" />
            )}
            <div>
              <span className={`text-xs font-semibold uppercase tracking-wider ${resilienceHasMaterialEffect && !resilienceIsSplit ? 'text-emerald-400' : 'text-amber-400'}`}>
                Honest finding
              </span>
              {resilienceIsSplit ? (
                <p className="text-sm text-slate-300 leading-relaxed mt-2">
                  Graph-aware sourcing protects against a{' '}
                  <span className="text-emerald-400 font-semibold">targeted</span> supplier outage and does{' '}
                  <span className="text-amber-400 font-semibold">not</span> protect against broad systemic stress.
                  Against a targeted outage it removes{' '}
                  {Math.abs(summary.resilience.targeted_cascade_risk_reduction * 100).toFixed(2)} pp of cascade risk
                  {/* cascade risk IS a share on 0-1, so pp is arithmetically fine there. CVaR-95
                      is a cost MULTIPLIER (~1.0-2.0) — a delta between two multipliers is measured
                      in multiplier units, which is what the tile above prints as "-0.0309× change
                      in cost multiplier". Calling that same delta "3.09 pp" was a unit slip. */}
                  {' '}and {Math.abs(summary.resilience.targeted_cvar95_reduction).toFixed(4)}× off the CVaR-95
                  cost multiplier.{' '}
                  {stressIsIndistinguishable ? (
                    <>
                      Under broad systemic stress there is{' '}
                      <span className="text-amber-400 font-semibold">no measurable effect</span>: the mean is{' '}
                      {(summary.resilience.stress_cascade_risk_reduction * 100).toFixed(2)} pp, but the paired
                      bootstrap interval covers zero, so it cannot be claimed in either direction — not as a win,
                      and not as the loss an unqualified −8 pp would imply.{' '}
                    </>
                  ) : (
                    <>
                      Under broad stress it is{' '}
                      {Math.abs(summary.resilience.stress_cascade_risk_reduction * 100).toFixed(2)} pp{' '}
                      <span className="text-amber-400 font-semibold">worse</span> than blind MILP.{' '}
                    </>
                  )}
                  These are the disclosed deltas from run {summary.run_id}; none is rounded toward the answer we
                  wanted, and each carries the interval that decides whether it may be claimed at all.{' '}
                  {nominalPremiumIsMaterial && (
                    <>
                      The protection is not free either — a {fmtPct(nominalPremiumPct, 2)} nominal premium
                      ({fmtUsd(summary.cost_delta_usd)} per BOM run).{' '}
                    </>
                  )}
                  The defensible claim is therefore narrow: this mitigation is worth buying against the threat it
                  was designed for, and on nine BOMs there is not enough evidence to say anything at all about a
                  correlated shock. Note also
                  that 2 of the {summary.n_boms} benchmarked BOMs select an identical plan in both arms and
                  contribute a hard zero to every mean, so the effective n behind these figures is{' '}
                  {summary.n_boms - 2}.
                </p>
              ) : resilienceHasMaterialEffect ? (
                <p className="text-sm text-slate-300 leading-relaxed mt-2">
                  Graph-aware sourcing measurably lowers cascade risk and/or CVaR-95 under disruption — the numbers
                  above are the real, disclosed deltas from run {summary.run_id}.{' '}
                  {nominalPremiumIsMaterial ? (
                    <>
                      It is <span className="text-amber-400 font-semibold">not free</span>: it costs a{' '}
                      {fmtPct(nominalPremiumPct, 2)} nominal premium ({fmtUsd(summary.cost_delta_usd)} per BOM run),
                      which is above the {materialityPct.toFixed(1)}% materiality threshold this page reports
                      against — <span title={materialityBasis ?? undefined}>an assumed cut fixed before the run,
                      not a measured noise floor</span>. That is a real trade — buying tail-risk reduction with
                      nominal cost — and we state it as one rather than claiming cost was held flat.
                    </>
                  ) : (
                    <>
                      The nominal premium ({fmtPct(nominalPremiumPct, 2)}) sits under the{' '}
                      {materialityPct.toFixed(1)}% materiality threshold this page reports against, so nominal cost
                      is roughly flat here — flat against a cut we{' '}
                      <span title={materialityBasis ?? undefined}>assumed rather than measured</span>.
                    </>
                  )}
                </p>
              ) : (
                <p className="text-sm text-slate-300 leading-relaxed mt-2">
                  On this catalog, the reductions above all fall under the{' '}
                  {(RESILIENCE_MATERIALITY * 100).toFixed(0)} pp materiality cut this page reports against (an
                  assumption, not a measured noise level) —
                  cost-optimal consolidation dominates the graph surcharge, so graph-aware selects essentially the
                  same plan as blind MILP. The consolidated single-hub plan that wins on cost is itself the
                  concentration risk the targeted scenario exposes. We report this as a real ~0 finding rather than
                  manufacturing a resilience win: on the current supplier catalog, cost and graph-aware routing do
                  not diverge enough to trade one for the other. This still quantifies the cost-vs-resilience
                  trade-off honestly — it just shows the trade-off is currently slack in one direction.
                </p>
              )}
              <p className="text-xs text-slate-400 mt-3 leading-relaxed border-t border-slate-700/60 pt-2">
                <span className="font-semibold text-slate-300">
                  On the {materialityPct.toFixed(1)}% threshold:
                </span>{' '}
                {materialityBasis ??
                  `it is an assumed reporting cut, not a measured noise floor. The benchmark is a single deterministic solve (seed 42, one CP-SAT worker, no gap limit), so re-running it reproduces every figure exactly and there are no replicates to estimate run-to-run variance from. Read it as "smaller than this is not worth acting on", not as "smaller than this is indistinguishable from noise".`}
              </p>
            </div>
          </div>
        </motion.div>


        {/* ══════════════════════════════════════════════════════════════════════
            SECTION 2b — THE PRICE OF RESILIENCE (the diversification frontier)
            ──────────────────────────────────────────────────────────────────────
            The section above reports a SPLIT — graph-aware sourcing wins against a
            targeted outage and shows nothing under broad stress — and cannot say
            why. This one prices the trade and explains the split: the constraint
            bounds a supplier COUNT while the objective stays pure cost, so the
            cheapest way to satisfy it is often to abandon the incumbent. Sourced
            from GET /benchmark/diversification-frontier; nothing here is hardcoded.
           ══════════════════════════════════════════════════════════════════════ */}
        {frontier && (
          <motion.section
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.22, duration: 0.4, ease: 'easeOut' }}
            className="mb-6"
            aria-labelledby="price-of-resilience-heading"
          >
            <div className="mt-8 mb-4">
              <span className="text-xs font-semibold uppercase tracking-wider text-indigo-400">
                Price of Resilience
              </span>
              <h2 id="price-of-resilience-heading" className="text-white text-2xl font-semibold mt-1">
                What a second supplier costs, and what it buys
              </h2>
              <p className="text-sm text-slate-400 mt-1 max-w-4xl leading-relaxed">
                The split above — better against a named single point of failure, nothing measurable under broad
                stress — is a finding only if you can price it. This sweep does. The same sourcing MILP is
                re-solved subject to a hard <span className="text-slate-300">open at least k distinct
                distributors</span> constraint, each plan is costed, and each is simulated under this
                benchmark&rsquo;s own scenarios.
                {typeof summary.avg_suppliers_greedy === 'number' && frontier.mean_suppliers_at_k1 !== null && (
                  <>
                    {' '}The MILP consolidates suppliers from{' '}
                    <span className="tabular-nums">{summary.avg_suppliers_greedy.toFixed(2)}</span> per BOM under
                    the greedy baseline to{' '}
                    <span className="text-slate-300 tabular-nums">
                      {frontier.mean_suppliers_at_k1.toFixed(2)}
                    </span>{' '}
                    at the unconstrained optimum. This sweep prices what un-consolidating costs.
                  </>
                )}
              </p>
            </div>

            {!frontier.available ? (
              <div className="bg-amber-500/5 border border-amber-500/20 rounded-xl p-4">
                <span className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-amber-400">
                  <AlertTriangle size={14} aria-hidden="true" />
                  Frontier unavailable
                </span>
                <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                  {frontier.unavailable_reason ??
                    'The sweep artifact is not present in this deployment. No cost-per-unit-of-risk figure is quoted.'}
                </p>
              </div>
            ) : (
              <>
                {/* ── The one sentence ──────────────────────────────────────── */}
                <div className="bg-emerald-500/5 border border-emerald-500/25 rounded-xl p-5 mb-5">
                  <span className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-emerald-400">
                    <TrendingUp size={14} aria-hidden="true" />
                    The finding
                  </span>
                  <p className="text-slate-100 text-lg sm:text-xl font-semibold mt-2 leading-snug">
                    {frontier.finding}
                  </p>
                  {frontier.verdict && (
                    <p className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-base font-semibold">
                      {renderVerdict(frontier.verdict)}
                    </p>
                  )}
                  <p className="text-xs text-slate-400 mt-3 leading-relaxed border-t border-emerald-500/20 pt-3">
                    <span className="font-semibold text-slate-300">n and n_effective are both quoted, and they
                    differ.</span>{' '}
                    {frontier.n_effective_definition}
                  </p>
                  {frontier.baseline_check && (
                    <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                      <span className="font-semibold text-slate-300">The control arm is the published one.</span>{' '}
                      {frontier.baseline_check}
                    </p>
                  )}
                </div>

                {/* ── Cost and risk against k ───────────────────────────────── */}
                <div
                  className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5"
                  aria-label="Mean landed cost in US dollars per BOM and cascade risk as a share from 0 to 1, plotted against k, the minimum number of distinct distributors per BOM"
                >
                  <h3 className="text-base font-semibold text-slate-200">
                    Cost and risk against the minimum supplier count
                  </h3>
                  <p className="text-xs text-slate-400 mt-1 mb-4 max-w-4xl leading-relaxed">
                    Left axis is dollars, right axis is a risk share on 0&ndash;1. Line style distinguishes the
                    measure and colour distinguishes the scenario; every value plotted here is repeated as text in
                    the tables below, so no reading depends on telling two colours apart.
                  </p>
                  <div className="w-full overflow-x-auto">
                    <div className="min-w-[520px]">
                      <ResponsiveContainer width="100%" height={360}>
                        <LineChart
                          data={frontierChartData}
                          margin={{ top: 8, right: 40, left: 26, bottom: 34 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                          <XAxis
                            dataKey="k"
                            tick={{ fill: '#94a3b8', fontSize: 12 }}
                            stroke="#94a3b8"
                            label={{
                              value: 'k — minimum distinct distributors per BOM (count)',
                              position: 'insideBottom', offset: -18,
                              fill: '#94a3b8', fontSize: 12,
                            }}
                          />
                          <YAxis
                            yAxisId="cost"
                            tick={{ fill: '#94a3b8', fontSize: 12 }}
                            stroke="#94a3b8"
                            label={{
                              value: 'mean landed cost (USD per BOM)',
                              angle: -90, position: 'insideLeft', offset: 8,
                              style: { textAnchor: 'middle' }, fill: '#94a3b8', fontSize: 12,
                            }}
                          />
                          <YAxis
                            yAxisId="risk"
                            orientation="right"
                            domain={[0, 1]}
                            tick={{ fill: '#94a3b8', fontSize: 12 }}
                            stroke="#94a3b8"
                            label={{
                              value: 'risk (share of BOM lines, 0–1)',
                              angle: 90, position: 'insideRight', offset: 8,
                              style: { textAnchor: 'middle' }, fill: '#94a3b8', fontSize: 12,
                            }}
                          />
                          <Tooltip
                            contentStyle={{
                              backgroundColor: '#0f172a', border: '1px solid #475569',
                              borderRadius: '8px', padding: '12px', fontSize: '12px',
                            }}
                            labelFormatter={(k) => `k = ${k} distinct distributors`}
                          />
                          {/* `paddingBottom` is load-bearing, not styling: recharts
                              measures the legend WRAPPER to decide how much of the
                              chart to give up, and then puts the plot immediately
                              under it with no gap of its own. Two things reach up
                              out of the plot area — the rotated y-axis captions,
                              which overlapped the wrapped legend's last row by 15px
                              at 390, and the topmost y tick label, which is centred
                              on its tick and so always sits half its height above
                              the plot. 24px clears both. */}
                          <Legend
                            content={<FrontierChartKey />}
                            verticalAlign="top"
                            wrapperStyle={{ paddingBottom: 24 }}
                          />
                          {FRONTIER_SERIES.map((s) => (
                            <Line
                              key={s.key}
                              yAxisId={s.axis}
                              type="monotone"
                              dataKey={s.key}
                              name={s.name}
                              stroke={s.color}
                              strokeWidth={2}
                              strokeDasharray={s.dash || undefined}
                              dot={{ r: 3, fill: s.color, stroke: s.color }}
                              activeDot={{ r: 5 }}
                              connectNulls
                            />
                          ))}
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                  <p className="text-xs text-slate-400 mt-3 leading-relaxed border-t border-slate-700/60 pt-3">
                    <span className="font-semibold text-amber-300">Read the risk axis, not the cost axis.</span>{' '}
                    {frontier.cost_axis_caveat}
                  </p>
                </div>

                {/* ── The frontier, cumulative vs k = 1 ─────────────────────── */}
                <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5">
                  <h3 className="text-base font-semibold text-slate-200">
                    The frontier — every k, paired against its own k&nbsp;=&nbsp;1 plan
                  </h3>
                  <p className="text-xs text-slate-400 mt-1 mb-4 max-w-4xl leading-relaxed">
                    {frontier.aggregate_definition} Each interval is drawn against a shared zero line so
                    &ldquo;excludes zero&rdquo; is something you see, and the verdict is written out beside it.
                  </p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm min-w-[1020px]">
                      <caption className="sr-only">
                        Diversification frontier: mean landed cost and paired change in cascade risk at each
                        minimum supplier count k
                      </caption>
                      <thead>
                        <tr className="text-left text-xs text-slate-400 uppercase tracking-wider border-b border-slate-700">
                          <th scope="col" className="py-2 pr-4">k</th>
                          <th scope="col" className="py-2 pr-4 text-right">BOMs (n / n_eff)</th>
                          <th scope="col" className="py-2 pr-4 text-right">Mean cost (USD/BOM)</th>
                          <th scope="col" className="py-2 pr-4">&Delta; cost vs k=1 (USD)</th>
                          <th scope="col" className="py-2 pr-4">Cascade risk removed — targeted (share)</th>
                          <th scope="col" className="py-2 pr-4">Cascade risk removed — broad stress (share)</th>
                          <th scope="col" className="py-2 pr-4 text-right">Cumulative USD per unit removed (targeted)</th>
                          {/* The stress column is shown BESIDE the targeted one
                              because it is where the sign can go the other way:
                              a k whose broad-stress risk is significantly WORSE
                              than k = 1 has no price of protection to quote, and
                              this cell has to say so rather than print a
                              negative dollar figure under this heading. */}
                          <th scope="col" className="py-2 text-right">Cumulative USD per unit removed (broad stress)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {frontier.points.map((p) => (
                          <tr
                            key={p.k}
                            className={`border-b border-slate-800/70 text-slate-300 ${
                              p.k === frontier.recommended_k ? 'bg-emerald-500/10' : ''
                            }`}
                          >
                            <th scope="row" className="py-2 pr-4 font-semibold text-slate-200 text-left">
                              {p.k}
                              {p.k === 1 && (
                                <span className="block text-xs font-normal text-slate-400">control arm</span>
                              )}
                              {/* The green row needs a word, not only a colour: the
                                  highlight is invisible to a screen reader and to
                                  anyone who cannot separate the two greens. */}
                              {p.k === frontier.recommended_k && (
                                <span
                                  className="block text-xs font-normal text-emerald-300"
                                  title={frontier.recommended_k_basis}
                                >
                                  recommended
                                </span>
                              )}
                            </th>
                            <td className="py-2 pr-4 text-right tabular-nums">
                              {p.n_boms_feasible} / {p.n_effective}
                              {p.boms_infeasible.length > 0 && (
                                <span className="block text-xs text-amber-300">
                                  {p.boms_infeasible.length} infeasible
                                </span>
                              )}
                            </td>
                            <td className="py-2 pr-4 text-right tabular-nums">
                              {fmtUsd(p.mean_total_cost_usd)}
                              <span className="block text-xs text-slate-400 tabular-nums">
                                {p.mean_suppliers.toFixed(2)} suppliers
                              </span>
                            </td>
                            <td className="py-2 pr-4">
                              <span className="block tabular-nums">
                                {fmtSignedUsd(p.delta_cost_usd?.mean)}
                              </span>
                              {p.k > 1 && (
                                <>
                                  <CiStrip
                                    low={p.delta_cost_usd?.ci95_low ?? 0}
                                    high={p.delta_cost_usd?.ci95_high ?? 0}
                                    mean={p.delta_cost_usd?.mean ?? 0}
                                    domainMin={costDomain[0]}
                                    domainMax={costDomain[1]}
                                    excludesZero={Boolean(p.delta_cost_usd?.significant)}
                                    favourable={false}
                                  />
                                  <span className="block text-xs text-slate-400 tabular-nums">
                                    {fmtIntervalText(p.delta_cost_usd, fmtSignedUsd)}
                                  </span>
                                </>
                              )}
                            </td>
                            <td className="py-2 pr-4">
                              <span className="block tabular-nums">
                                {fmtShare(p.delta_targeted_cascade_risk?.mean)}
                              </span>
                              {p.k > 1 && (
                                <>
                                  <CiStrip
                                    low={p.delta_targeted_cascade_risk?.ci95_low ?? 0}
                                    high={p.delta_targeted_cascade_risk?.ci95_high ?? 0}
                                    mean={p.delta_targeted_cascade_risk?.mean ?? 0}
                                    domainMin={riskDomain[0]}
                                    domainMax={riskDomain[1]}
                                    excludesZero={Boolean(p.delta_targeted_cascade_risk?.significant)}
                                    favourable={(p.delta_targeted_cascade_risk?.mean ?? 0) > 0}
                                  />
                                  <span className="block text-xs text-slate-400 tabular-nums">
                                    {fmtIntervalText(p.delta_targeted_cascade_risk, fmtShare)}
                                  </span>
                                  <CiVerdict ci={p.delta_targeted_cascade_risk} />
                                </>
                              )}
                            </td>
                            <td className="py-2 pr-4">
                              <span className="block tabular-nums">
                                {fmtShare(p.delta_stress_cascade_risk?.mean)}
                              </span>
                              {p.k > 1 && (
                                <>
                                  <CiStrip
                                    low={p.delta_stress_cascade_risk?.ci95_low ?? 0}
                                    high={p.delta_stress_cascade_risk?.ci95_high ?? 0}
                                    mean={p.delta_stress_cascade_risk?.mean ?? 0}
                                    domainMin={riskDomain[0]}
                                    domainMax={riskDomain[1]}
                                    excludesZero={Boolean(p.delta_stress_cascade_risk?.significant)}
                                    favourable={(p.delta_stress_cascade_risk?.mean ?? 0) > 0}
                                  />
                                  <span className="block text-xs text-slate-400 tabular-nums">
                                    {fmtIntervalText(p.delta_stress_cascade_risk, fmtShare)}
                                  </span>
                                  <CiVerdict ci={p.delta_stress_cascade_risk} />
                                </>
                              )}
                            </td>
                            <td className="py-2 text-right">
                              <PriceCell
                                removed={p.usd_per_unit_targeted_cascade_risk}
                                added={p.usd_per_unit_targeted_cascade_risk_added}
                                baselineLabel={p.k === 1 ? 'baseline' : 'not reported'}
                              />
                            </td>
                            <td className="py-2 text-right">
                              <PriceCell
                                removed={p.usd_per_unit_stress_cascade_risk}
                                added={p.usd_per_unit_stress_cascade_risk_added}
                                baselineLabel={p.k === 1 ? 'baseline' : 'not reported'}
                              />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p className="text-xs text-slate-400 mt-3 leading-relaxed">
                    A price per unit of risk removed is printed only where that k&rsquo;s risk change has a paired
                    95% CI excluding zero <span className="text-slate-300 font-semibold">and the change is a
                    reduction</span>. Where the CI covers zero the denominator is indistinguishable from zero and
                    the ratio would be an artifact of division, not a price. Where it excludes zero on the other
                    side, diversification at that k <span className="text-red-300 font-semibold">added</span>{' '}
                    risk: there is no price of protection to quote, so the cell says so and reports the dollars
                    paid per unit of risk added instead of a negative number under a heading that says removed.
                  </p>
                </div>

                {/* ── Marginal return: where the curve collapses ────────────── */}
                <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5">
                  <h3 className="text-base font-semibold text-slate-200">
                    Marginal return — what the k-th supplier alone buys
                  </h3>
                  <p className="text-xs text-slate-400 mt-1 mb-4 max-w-4xl leading-relaxed">
                    Each row is the step from k&minus;1 to k, paired on the BOMs feasible at both ends. This is the
                    column that says where to stop: the price per unit of targeted cascade risk removed, and how
                    many times the first step&rsquo;s price that is.
                  </p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm min-w-[820px]">
                      <caption className="sr-only">
                        Marginal cost and marginal risk removed at each step from k minus one to k
                      </caption>
                      <thead>
                        <tr className="text-left text-xs text-slate-400 uppercase tracking-wider border-b border-slate-700">
                          <th scope="col" className="py-2 pr-4">Step</th>
                          <th scope="col" className="py-2 pr-4 text-right">Marginal cost (USD/BOM)</th>
                          <th scope="col" className="py-2 pr-4">Cascade risk removed — targeted (share)</th>
                          <th scope="col" className="py-2 pr-4 text-right">USD per unit removed</th>
                          <th scope="col" className="py-2 pr-4 text-right">vs the first step</th>
                          <th scope="col" className="py-2 pr-4">E[shortfall] removed — broad stress (share)</th>
                          <th scope="col" className="py-2 text-right">USD per unit removed</th>
                        </tr>
                      </thead>
                      <tbody>
                        {frontier.steps.map((s) => {
                          const collapsed = s.usd_per_unit_targeted_cascade_risk === null;
                          return (
                            <tr
                              key={s.label}
                              className={`border-b border-slate-800/70 text-slate-300 ${
                                s.to_k === frontier.recommended_k
                                  ? 'bg-emerald-500/10'
                                  : collapsed
                                    ? 'bg-amber-500/5'
                                    : ''
                              }`}
                            >
                              <th scope="row" className="py-2 pr-4 font-semibold text-slate-200 text-left tabular-nums">
                                {s.label}
                              </th>
                              <td className="py-2 pr-4 text-right tabular-nums">
                                {fmtSignedUsd(s.marginal_cost_usd?.mean)}
                                <span className="block text-xs text-slate-400 tabular-nums">
                                  {fmtIntervalText(s.marginal_cost_usd, fmtSignedUsd)}
                                </span>
                                {/* Each step is paired only on the BOMs feasible at BOTH ends,
                                    so a step can be a smaller panel than the one above it. */}
                                <span className="block text-xs text-slate-400 tabular-nums">
                                  paired on n={s.marginal_cost_usd?.n ?? 0} BOMs
                                </span>
                              </td>
                              <td className="py-2 pr-4">
                                <span className="block tabular-nums">
                                  {fmtShare(s.marginal_targeted_cascade_risk_removed?.mean)}
                                </span>
                                <CiStrip
                                  low={s.marginal_targeted_cascade_risk_removed?.ci95_low ?? 0}
                                  high={s.marginal_targeted_cascade_risk_removed?.ci95_high ?? 0}
                                  mean={s.marginal_targeted_cascade_risk_removed?.mean ?? 0}
                                  domainMin={marginalRiskDomain[0]}
                                  domainMax={marginalRiskDomain[1]}
                                  excludesZero={Boolean(s.marginal_targeted_cascade_risk_removed?.significant)}
                                  favourable={(s.marginal_targeted_cascade_risk_removed?.mean ?? 0) > 0}
                                />
                                <span className="block text-xs text-slate-400 tabular-nums">
                                  {fmtIntervalText(s.marginal_targeted_cascade_risk_removed, fmtShare)}
                                </span>
                                <CiVerdict ci={s.marginal_targeted_cascade_risk_removed} />
                              </td>
                              <td className="py-2 pr-4 text-right">
                                <PriceCell
                                  removed={s.usd_per_unit_targeted_cascade_risk}
                                  added={s.usd_per_unit_targeted_cascade_risk_added}
                                  emphasis={s.to_k === frontier.recommended_k}
                                />
                              </td>
                              <td className="py-2 pr-4 text-right">
                                {s.cost_multiple_vs_first_step !== null ? (
                                  <span
                                    className={`tabular-nums font-semibold ${
                                      s.cost_multiple_vs_first_step >= 2 ? 'text-red-300' : 'text-slate-300'
                                    }`}
                                  >
                                    {s.cost_multiple_vs_first_step.toFixed(1)}&times;
                                  </span>
                                ) : (
                                  <span className="text-xs text-slate-400">—</span>
                                )}
                              </td>
                              <td className="py-2 pr-4">
                                <span className="block tabular-nums">
                                  {fmtShare(s.marginal_stress_expected_shortfall_removed?.mean)}
                                </span>
                                <CiStrip
                                  low={s.marginal_stress_expected_shortfall_removed?.ci95_low ?? 0}
                                  high={s.marginal_stress_expected_shortfall_removed?.ci95_high ?? 0}
                                  mean={s.marginal_stress_expected_shortfall_removed?.mean ?? 0}
                                  domainMin={marginalRiskDomain[0]}
                                  domainMax={marginalRiskDomain[1]}
                                  excludesZero={Boolean(s.marginal_stress_expected_shortfall_removed?.significant)}
                                  favourable={(s.marginal_stress_expected_shortfall_removed?.mean ?? 0) > 0}
                                />
                                <span className="block text-xs text-slate-400 tabular-nums">
                                  {fmtIntervalText(s.marginal_stress_expected_shortfall_removed, fmtShare)}
                                </span>
                                <CiVerdict ci={s.marginal_stress_expected_shortfall_removed} />
                              </td>
                              <td className="py-2 text-right">
                                <PriceCell
                                  removed={s.usd_per_unit_stress_expected_shortfall}
                                  added={s.usd_per_unit_stress_expected_shortfall_added}
                                />
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  {/* The API's own sentence, composed from the counts beside it.
                      This paragraph used to be assembled here from the first two
                      priced steps and rendered ONLY when a second one existed —
                      so when the corrected supply graph left a single priced
                      step, the collapse claim disappeared from the page without
                      ever being retracted on it. It is now unconditional and
                      states whichever of the two is true. */}
                  {frontier.price_coverage && (
                    <p className="text-sm text-slate-300 mt-4 leading-relaxed border-t border-slate-700/60 pt-3">
                      <span className="font-semibold text-white">How far the price column reaches.</span>{' '}
                      <span className="tabular-nums text-slate-200">
                        {frontier.n_priced_steps} of {frontier.n_steps_total}
                      </span>{' '}
                      steps carry a price. {frontier.price_coverage}
                    </p>
                  )}
                  <p className="text-xs text-slate-400 mt-3 leading-relaxed">
                    <span className="font-semibold text-slate-300">Why E[shortfall] is shown beside
                    cascade&nbsp;risk.</span>{' '}
                    {frontier.quantisation_caveat}
                  </p>
                </div>

                {/* ── The mechanism ─────────────────────────────────────────── */}
                <div className="bg-slate-900/60 border border-slate-700 rounded-xl p-5 mb-5">
                  <span className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-indigo-400">
                    <Split size={14} aria-hidden="true" />
                    The mechanism — why a supplier count is not a resilience constraint
                  </span>
                  <p className="text-xs text-slate-400 mt-2 leading-relaxed max-w-4xl">
                    {frontier.nesting_caveat}
                  </p>
                  <ul className="flex flex-wrap gap-2 mt-3">
                    {frontier.points
                      .filter((p) => p.k > 1)
                      .map((p) => (
                        <li
                          key={p.k}
                          className="text-xs text-slate-300 tabular-nums bg-slate-800/80 border border-slate-700 rounded px-2 py-1"
                        >
                          k={p.k}:{' '}
                          <span
                            className={
                              p.n_keeps_k1_suppliers * 2 <= p.n_boms_feasible
                                ? 'text-amber-300 font-semibold'
                                : 'text-slate-200'
                            }
                          >
                            {p.n_keeps_k1_suppliers} of {p.n_boms_feasible}
                          </span>{' '}
                          plans keep every k=1 supplier
                        </li>
                      ))}
                  </ul>
                  {/* What the scan ACTUALLY found, including where it found
                      nothing. Rendered unconditionally: the example used to be
                      the only thing on screen, so when the corrected supply
                      graph removed the expected-shortfall counter-example the
                      whole paragraph vanished and the section kept its heading
                      with no evidence under it. */}
                  {frontier.non_monotone_status && (
                    <p className="text-sm text-slate-300 mt-4 leading-relaxed border-t border-slate-700/60 pt-3">
                      <span className="font-semibold text-white">
                        Is risk actually non-monotone in k here?
                      </span>{' '}
                      {frontier.non_monotone_status}
                    </p>
                  )}
                  {frontier.non_monotone_example && (
                    <p className="text-sm text-slate-300 mt-3 leading-relaxed">
                      <code className="bg-slate-800 px-1 rounded text-slate-200">
                        {frontier.non_monotone_example.bom}
                      </code>{' '}
                      goes from{' '}
                      <span className="tabular-nums text-emerald-300">
                        {fmtShare(frontier.non_monotone_example.value_before)}
                      </span>{' '}
                      to{' '}
                      <span className="tabular-nums text-red-300">
                        {fmtShare(frontier.non_monotone_example.value_after)}
                      </span>{' '}
                      {frontier.non_monotone_example.measure_label} under {frontier.non_monotone_example.scenario}
                      {' '}going from k={frontier.non_monotone_example.from_k} to
                      k={frontier.non_monotone_example.to_k}
                      {frontier.non_monotone_example.keeps_k1_suppliers ? (
                        <> — it keeps its incumbent and still ends up more exposed</>
                      ) : (
                        <>
                          {' '}— it drops a low-hazard incumbent for a cheaper set of{' '}
                          {frontier.non_monotone_example.n_suppliers_after} whose combined hazard is higher
                        </>
                      )}
                      . Under a{' '}
                      <span className="text-slate-100 font-semibold">targeted</span> outage the effect is
                      one-directional, because spreading always shrinks the blast radius of losing one named hub.
                      That asymmetry is exactly the split the section above reports, and it is a property of the
                      constraint, not of resilience.
                    </p>
                  )}
                </div>

                {/* ── What this frontier cannot tell you ────────────────────── */}
                <div className="bg-slate-900/60 border border-slate-700 rounded-xl p-5 mb-5">
                  <span className="text-xs font-semibold uppercase tracking-wider text-indigo-400">
                    What this frontier cannot tell you
                  </span>
                  <dl className="mt-3 space-y-3">
                    <div>
                      <dt className="text-xs font-semibold text-slate-300">
                        The cost axis is largely a fixed-charge artifact
                      </dt>
                      <dd className="text-xs text-slate-400 leading-relaxed mt-1">
                        {frontier.cost_axis_caveat}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs font-semibold text-slate-300">
                        One seed — these intervals have no Monte-Carlo error term
                      </dt>
                      <dd className="text-xs text-slate-400 leading-relaxed mt-1">{frontier.seed_caveat}</dd>
                    </div>
                    <div>
                      <dt className="text-xs font-semibold text-slate-300">
                        Cascade risk is quantised to {'{'}0, .25, .5, .75, 1{'}'}
                      </dt>
                      <dd className="text-xs text-slate-400 leading-relaxed mt-1">
                        {frontier.quantisation_caveat}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs font-semibold text-slate-300">
                        Failures are independent beyond a shared stress multiplier
                      </dt>
                      <dd className="text-xs text-slate-400 leading-relaxed mt-1">
                        {frontier.independence_caveat}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs font-semibold text-slate-300">Coverage</dt>
                      <dd className="text-xs text-slate-400 leading-relaxed mt-1">
                        {frontier.n_boms_included} of {frontier.n_boms_in_catalog} catalogue BOMs are swept.
                        {Object.entries(frontier.boms_excluded).map(([bom, reason]) => (
                          <span key={bom} className="block mt-1">
                            <code className="bg-slate-800 px-1 rounded text-slate-300">{bom}</code> excluded:{' '}
                            {reason}
                          </span>
                        ))}
                        {infeasibleNote && <span className="block mt-1">{infeasibleNote}</span>}
                      </dd>
                    </div>
                    {frontier.caveats.length > 0 && (
                      <div>
                        <dt className="text-xs font-semibold text-slate-300">
                          Full caveat list, as published with the artifact
                        </dt>
                        <dd className="text-xs text-slate-400 leading-relaxed mt-1">
                          <ul className="space-y-2">
                            {frontier.caveats.map((caveat, i) => (
                              <li key={i}>{renderCaveatProse(caveat)}</li>
                            ))}
                          </ul>
                        </dd>
                      </div>
                    )}
                  </dl>
                  <p className="text-xs text-slate-400 mt-4 leading-relaxed border-t border-slate-700/60 pt-3">
                    Source:{' '}
                    <code className="bg-slate-800 px-1 rounded text-slate-300">{frontier.source}</code>
                    {frontier.generated_utc ? ` · generated ${frontier.generated_utc}` : ''}
                    {frontier.strategy ? ` · strategy ${frontier.strategy}` : ''}
                    {frontier.mc_scenarios !== null && frontier.mc_seed !== null
                      ? ` · ${frontier.mc_scenarios.toLocaleString()} Monte Carlo scenarios at seed ${frontier.mc_seed}`
                      : ''}
                    {frontier.stress_factor !== null ? ` · stress factor ${frontier.stress_factor}` : ''}
                    {frontier.bootstrap_n !== null && frontier.bootstrap_seed !== null
                      ? ` · ${frontier.bootstrap_n.toLocaleString()} bootstrap resamples at seed ${frontier.bootstrap_seed}`
                      : ''}
                    . Served by <code className="bg-slate-800 px-1 rounded text-slate-300">
                      GET /benchmark/diversification-frontier
                    </code>; nothing on this page is a hardcoded copy of it.
                  </p>
                </div>
              </>
            )}
          </motion.section>
        )}

        {/* ── Monte Carlo ETA distribution ──────────────────────────────────────── */}
        <motion.div
          initial={{ opacity: 0, scale: 0.97 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.25, duration: 0.5 }}
          className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5"
          aria-label="Monte Carlo ETA distribution, blind vs graph-aware MILP across P10 P50 P90"
        >
          <div className="mb-4">
            <h2 className="text-2xl font-semibold text-slate-300">Monte Carlo ETA distribution</h2>
            <p className="text-xs text-slate-400 mt-1">P10 · P50 · P90 delivery days, blind vs graph-aware MILP (n={summary.n_boms} BOMs)</p>
          </div>
          {summary.monte_carlo ? (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={mcData} margin={{ top: 8, right: 16, left: 16, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis dataKey="name" tick={{ fill: '#94a3b8', fontSize: 12 }} />
                <YAxis
                  tick={{ fill: '#94a3b8', fontSize: 12 }}
                  label={{ value: 'ETA (days)', angle: -90, position: 'insideLeft', offset: 8,
                           style: { textAnchor: 'middle' }, fill: '#94a3b8', fontSize: 12 }}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#0f172a',
                    border: '1px solid #475569',
                    borderRadius: '8px',
                    padding: '12px',
                    fontSize: '12px',
                  }}
                />
                {/* Recharts colours the legend TEXT with the series fill, so a bar
                    colour is also 12px body text and has to clear 4.5:1 against
                    this card. The old pair did not: slate-500 #64748b measured
                    3.34:1 and indigo-500 #6366f1 measured 3.55:1. slate-300
                    (10.7:1) and indigo-400 (5.3:1) both clear it, and they differ
                    in LIGHTNESS rather than only hue — near-white against mid
                    indigo survives a greyscale print and the common colour-vision
                    deficiencies. The exact figures are written out as text under
                    the chart as well, so no reading here depends on telling two
                    colours apart. */}
                <Legend wrapperStyle={{ fontSize: '12px' }} />
                <Bar dataKey="Baseline" fill={MC_BASELINE_COLOR} radius={[4, 4, 0, 0]} />
                <Bar dataKey="Graph-Aware" fill={MC_GRAPH_AWARE_COLOR} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-52 flex items-center justify-center text-slate-400 text-sm">
              Monte Carlo data not available for this run.
            </div>
          )}
          {summary.monte_carlo && (
            <div className="overflow-x-auto mt-4">
              {/* Every plotted value repeated as text — the same rule the frontier
                  chart above states. Nothing here may depend on telling the two
                  bar colours apart. */}
              <table className="w-full text-xs border-t border-slate-700/60 pt-3">
                <caption className="sr-only">
                  Monte Carlo ETA percentiles in days, blind (baseline) versus graph-aware MILP
                </caption>
                <thead>
                  <tr className="text-left text-slate-400 uppercase tracking-wider">
                    <th scope="col" className="py-2 pr-4 font-semibold">Percentile</th>
                    <th scope="col" className="py-2 pr-4 font-semibold text-right">Baseline (days)</th>
                    <th scope="col" className="py-2 font-semibold text-right">Graph-Aware (days)</th>
                  </tr>
                </thead>
                <tbody>
                  {mcData.map((row) => (
                    <tr key={row.name} className="border-t border-slate-800/70 text-slate-300">
                      <th scope="row" className="py-2 pr-4 text-left font-semibold text-slate-200">
                        {row.name}
                      </th>
                      <td className="py-2 pr-4 text-right tabular-nums">{row.Baseline.toFixed(2)}</td>
                      <td className="py-2 text-right tabular-nums">{row['Graph-Aware'].toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </motion.div>

        {/* ── Per-BOM: graph-aware vs blind MILP (nominal) ─────────────────────── */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.28, duration: 0.4 }}
          className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5 overflow-x-auto"
        >
          <h2 className="text-2xl font-semibold text-slate-300 mb-1">Per-BOM: graph-aware vs blind MILP</h2>
          <p className="text-xs text-slate-400 mb-4">Nominal-world deltas per reference BOM. Negative = graph-aware is cheaper/faster/less risky.</p>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-400 uppercase tracking-wider border-b border-slate-700">
                <th className="py-2 pr-4">BOM</th>
                <th className="py-2 pr-4 text-right">Cost Δ</th>
                <th className="py-2 pr-4 text-right">ETA Δ</th>
                <th className="py-2 pr-4 text-right">CO2 Δ</th>
                <th className="py-2 pr-4 text-right">Cascade risk Δ</th>
              </tr>
            </thead>
            <tbody>
              {summary.bom_deltas.map((d) => (
                <tr key={d.bom_name} className="border-b border-slate-800/70 text-slate-300">
                  <td className="py-2 pr-4">{d.bom_name}</td>
                  <td className="py-2 pr-4 text-right tabular-nums" style={{ color: d.cost_delta_pct < 0 ? '#10b981' : d.cost_delta_pct > 0 ? '#ef4444' : '#94a3b8' }}>
                    {fmtPct(d.cost_delta_pct)}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums" style={{ color: d.eta_delta_pct < 0 ? '#10b981' : d.eta_delta_pct > 0 ? '#ef4444' : '#94a3b8' }}>
                    {fmtPct(d.eta_delta_pct)}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums" style={{ color: d.co2_delta_pct < 0 ? '#10b981' : d.co2_delta_pct > 0 ? '#ef4444' : '#94a3b8' }}>
                    {fmtPct(d.co2_delta_pct)}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums" style={{ color: d.cascade_risk_delta_pct < 0 ? '#10b981' : d.cascade_risk_delta_pct > 0 ? '#ef4444' : '#94a3b8' }}>
                    {fmtPct(d.cascade_risk_delta_pct)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </motion.div>

        {/* ── Tradeoff Card ────────────────────────────────────────────────────── */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.3, duration: 0.4 }}
          className="bg-slate-800/60 border border-amber-500/20 rounded-xl p-5 mb-5"
        >
          <span className="text-xs font-semibold uppercase tracking-wider text-amber-400">
            HONEST TRADEOFF
          </span>
          <h2 className="text-white text-2xl font-semibold mt-1">Where Graph-Aware Loses</h2>
          {/* This paragraph used to be a hardcoded template asserting the arm
              "routes around" the cheapest distributor. It is now derived from the
              two stored plans, and the plans are rendered beneath it so the claim
              can be checked against the data without leaving the page. */}
          <p className="text-sm text-slate-300 leading-relaxed mt-3">
            {summary.tradeoff.narrative}
          </p>
          {Array.isArray(summary.tradeoff.blind_distributors)
            && summary.tradeoff.blind_distributors.length > 0
            && Array.isArray(summary.tradeoff.graph_aware_distributors) && (
            <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <span className="text-slate-400 uppercase tracking-wider">Plan</span>
              <span className="text-slate-200">
                {summary.tradeoff.blind_distributors.join(', ')}
              </span>
              <span className="text-slate-500" aria-label="becomes">→</span>
              <span className="text-amber-300">
                {summary.tradeoff.graph_aware_distributors.join(', ')}
              </span>
              {summary.tradeoff.distributors_dropped
                && summary.tradeoff.distributors_dropped.length > 0 && (
                <span className="text-slate-400">
                  (dropped {summary.tradeoff.distributors_dropped.join(', ')})
                </span>
              )}
            </div>
          )}
          {summary.tradeoff.mechanism && (
            <p className="text-xs text-slate-400 leading-relaxed mt-3 border-t border-slate-700/60 pt-3">
              {summary.tradeoff.mechanism}
            </p>
          )}
          <div className="mt-4 flex items-center gap-6 text-sm">
            <div className="flex flex-col gap-1">
              <span className="text-xs text-slate-400 uppercase tracking-wider">Blind MILP ({summary.tradeoff.losing_axis})</span>
              <span className="text-white tabular-nums font-semibold">
                {summary.tradeoff.losing_axis === 'cost'
                  ? fmtUsd(summary.tradeoff.baseline_value)
                  : summary.tradeoff.baseline_value.toFixed(2)}
              </span>
            </div>
            <div className="text-slate-400">→</div>
            <div className="flex flex-col gap-1">
              <span className="text-xs text-amber-400 uppercase tracking-wider">Graph-Aware ({summary.tradeoff.losing_axis})</span>
              <span className="text-amber-400 tabular-nums font-semibold">
                {summary.tradeoff.losing_axis === 'cost'
                  ? fmtUsd(summary.tradeoff.graph_aware_value)
                  : summary.tradeoff.graph_aware_value.toFixed(2)}
                {summary.tradeoff.delta_pct !== 0 && (
                  <span className="text-xs ml-1" style={{ color: tradeoffColor }}>
                    ({fmtPct(summary.tradeoff.delta_pct)} {deltaGlyph(summary.tradeoff.delta_pct)})
                  </span>
                )}
              </span>
            </div>
          </div>
        </motion.div>

        {/* ══════════════════════════════════════════════════════════════════════
            SECTION 3 — Network structure (independent of arm/scenario)
           ══════════════════════════════════════════════════════════════════════ */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.35, duration: 0.4 }}
          className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5"
          aria-label="Network resilience under sequential distributor removal, 6 steps from 0 to 5 distributors removed"
        >
          <div className="mb-4">
            <h2 className="text-2xl font-semibold text-slate-300">
              Network resilience (λ₂) under sequential removal
            </h2>
            <p className="text-xs text-slate-400 mt-1">
              Top-5 highest-betweenness distributors removed in order. Click a point to see its fulfillability
              detail — which BOMs (if any) collapse. A BOM collapses when one of its lines has no supplier left
              in the graph after the removals up to that step.
            </p>
            {fiedlerBomSource && (
              <p className="text-xs text-slate-400 mt-1">
                {fiedlerBomsChecked === 0
                  ? `Fulfillability check did not run — ${fiedlerBomSource}`
                  : `Checked on ${fiedlerBomsChecked} reference BOM${
                      fiedlerBomsChecked === 1 ? '' : 's'
                    } · ${fiedlerBomSource}`}
              </p>
            )}
          </div>

          {fiedlerLoading ? (
            <div className="h-52 flex items-center justify-center gap-3 text-slate-400 text-sm">
              <div className="w-5 h-5 rounded-full border-2 border-indigo-500 border-t-transparent animate-spin" />
              Loading the λ₂ curve…
            </div>
          ) : fiedlerError ? (
            /* The request failed — which is NOT the same claim as "not computed for
               this run", the message this branch used to show either way. */
            <div className="h-52 flex flex-col items-center justify-center gap-3 text-sm">
              <p className="text-amber-400">
                The λ₂ curve request failed — this section only. Everything above is live.
              </p>
              <button
                onClick={() => { void loadFiedler(); }}
                className="bg-slate-800 border border-slate-700 px-3 py-1.5 rounded text-xs text-slate-300 hover:bg-slate-700 transition-colors focus:outline-none focus:ring-2 focus:ring-indigo-500"
              >
                Retry this chart
              </button>
            </div>
          ) : fiedler && fiedler.points.length > 0 ? (
            <>
              <ResponsiveContainer width="100%" height={300}>
                <LineChart
                  data={fiedlerData}
                  margin={{ top: 28, right: 16, left: 8, bottom: 28 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                  <XAxis
                    dataKey="name"
                    tick={{ fill: '#94a3b8', fontSize: 12 }}
                    label={{ value: 'Distributors removed', position: 'insideBottom', offset: -16, fill: '#94a3b8', fontSize: 12 }}
                  />
                  <YAxis
                    tick={{ fill: '#94a3b8', fontSize: 12 }}
                    label={{ value: 'λ₂ (algebraic connectivity)', angle: -90, position: 'insideLeft', offset: 8,
                      style: { textAnchor: 'middle' }, fill: '#94a3b8', fontSize: 12 }}
                  />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#0f172a',
                      border: '1px solid #475569',
                      borderRadius: '8px',
                      padding: '12px',
                      fontSize: '12px',
                    }}
                    formatter={(value) => [typeof value === 'number' ? value.toFixed(4) : '—', 'λ₂']}
                  />
                  <Line
                    type="monotone"
                    dataKey="lambda2"
                    stroke="#ef4444"
                    strokeWidth={2}
                    dot={(dotProps) => (
                      <FiedlerDot
                        key={dotProps.payload?.step}
                        {...dotProps}
                        selectedStep={selectedStep}
                        onSelect={setSelectedStep}
                      />
                    )}
                    activeDot={false}
                  />
                </LineChart>
              </ResponsiveContainer>

              {/* Annotation strip */}
              <div className="mt-3 min-h-[1.5rem]">
                {selectedPoint ? (
                  <p className="text-amber-400 text-sm">
                    {selectedPoint.removed_name
                      ? `Remove ${selectedPoint.removed_name} → ${selectedPoint.delta_pct.toFixed(1)}%`
                      : 'Baseline (no removal)'}
                  </p>
                ) : (
                  <p className="text-slate-400 text-sm">Click a point for its fulfillability detail.</p>
                )}
              </div>

              {/* Click-reveal drawer */}
              <AnimatePresence>
                {selectedStep !== null && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.2, ease: 'easeOut' }}
                    className="overflow-hidden"
                    aria-live="polite"
                  >
                    <div className="mt-4 border-t border-slate-700 pt-4">
                      <p className="text-sm font-semibold text-white mb-3">
                        BOMs that collapse after this removal
                      </p>
                      {fiedlerBomsChecked === 0 ? (
                        <p className="text-amber-400 text-xs">
                          Not computed for this deployment — {fiedlerBomSource || 'no benchmarked BOMs are loaded'}.
                          An empty list here would mean nothing, so we do not show one.
                        </p>
                      ) : selectedPoint && selectedPoint.collapsed_boms.length === 0 ? (
                        <p className="text-emerald-500 text-xs">
                          All {fiedlerBomsChecked} reference BOMs remain fulfillable after this removal.
                        </p>
                      ) : (
                        <div className="space-y-1.5">
                          {(selectedPoint?.collapsed_boms ?? []).map((bom) => (
                            <div
                              key={bom}
                              className="bg-slate-900/50 rounded-lg px-3 py-2 text-xs text-slate-300 border-l-2 border-red-500"
                            >
                              {bom}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </>
          ) : (
            <div className="h-52 flex items-center justify-center text-slate-400 text-sm">
              No connectivity curve recorded for this run. It tracks the Fiedler value &mdash; how hard
              the distributor network is to fragment &mdash; as suppliers are removed.
            </div>
          )}
        </motion.div>

      </div>
    </div>
  );
}
