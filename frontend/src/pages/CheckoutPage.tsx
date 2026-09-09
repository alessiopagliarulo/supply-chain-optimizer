import { useEffect, useState, useMemo, useRef } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer,
} from 'recharts';
import {
  DollarSign, Zap, Leaf, Scale, Check, TrendingDown, TrendingUp,
  Clock, Truck, ArrowRight, MapPin, ChevronDown, ChevronUp, Star,
  Activity, Cpu, AlertTriangle, Equal, RotateCcw, Minus,
} from 'lucide-react';
import { optimizeAPI, mlAPI, stressObservationMonth, type StressResponse, type VrpOptions } from '../services/api';
import { useCartStore } from '../store/cartStore';
import { useOptimizeStore, type MultiRouteResult } from '../store/optimizeStore';

const STRATEGY_META: Record<string, { icon: typeof DollarSign; color: string; gradient: string }> = {
  cheapest: { icon: DollarSign, color: 'text-green-400', gradient: 'from-green-500/20 to-green-600/5' },
  fastest:  { icon: Zap,        color: 'text-blue-400',  gradient: 'from-blue-500/20 to-blue-600/5' },
  greenest: { icon: Leaf,       color: 'text-emerald-400', gradient: 'from-emerald-500/20 to-emerald-600/5' },
  balanced: { icon: Scale,      color: 'text-purple-400', gradient: 'from-purple-500/20 to-purple-600/5' },
};

// ── Tie-aware ranking (C1) ──────────────────────────────────────────────────
// On a small BOM several strategies can resolve to literally the same plan. The
// backend still hands back 1/2/3/4 for every metric, so the UI used to imply a
// differentiation that does not exist. Everything below re-derives ranks from
// the numbers themselves and reports ties as ties.
//
// The tolerance used to be a flat 0.5% relative epsilon, which was far too loose:
// 0.5% of a $25k cart is $128, so $25,634 and $25,675 were called "tied" and every
// metric on all four cards wore an amber TIED badge — burying a real $41 saving.
// A tie now needs the gap to be under max(<per-metric absolute floor>, 0.05%):
//
//   metric    floor    at the audit's numbers            verdict
//   cost      $1       max($1, 0.05% × $25,675) = $12.84  $41 gap → NOT tied, $0.30 → tied
//   ETA       0.05 d   max(0.05, 0.05% × 12 d)  = 0.05 d  any printable day gap is real
//   CO2e      0.05 kg  max(0.05, 0.05% × 340)   = 0.17 kg
//   distance  0.5 km   max(0.5, 0.05% × 4,000)  = 2 km
//
// The floors are the smallest difference worth showing in each unit (dollars, days,
// kilograms, kilometres), so "TIED" now means the two plans really are the same plan
// on that metric — not "the difference was too small for the UI to bother with".
const TIE_REL_EPS = 0.0005; // 0.05%
const HIST_BINS = 24;       // bins in the Monte Carlo ETA histogram

type MetricKey = 'cost' | 'speed' | 'carbon' | 'distance';

/** Absolute tie floors, each in its own metric's unit: $, days, kg, km. */
const TIE_FLOOR: Record<MetricKey, number> = {
  cost: 1,        // dollars
  speed: 0.05,    // days
  carbon: 0.05,   // kilograms CO2e
  distance: 0.5,  // kilometres
};

function tieTolerance(a: number, b: number, floor: number): number {
  const scale = Math.max(Math.abs(a), Math.abs(b));
  return Math.max(floor, TIE_REL_EPS * scale);
}

function valuesTie(a: number, b: number, floor: number): boolean {
  if (!Number.isFinite(a) || !Number.isFinite(b)) return false;
  return Math.abs(a - b) <= tieTolerance(a, b, floor);
}

interface RankCell { rank: number; tied: boolean }

/** Competition ranking that collapses ties: [5, 5, 7] -> ranks 1, 1, 3. */
function tieAwareRanks(values: number[], floor: number): RankCell[] {
  const order = values.map((v, i) => ({ v, i })).sort((a, b) => a.v - b.v);
  const out: RankCell[] = values.map(() => ({ rank: 1, tied: false }));
  let groupStart = 0;
  order.forEach((entry, k) => {
    // Compare against the group's representative, never the neighbour, so a slow
    // drift across many values cannot chain everything into one bogus tie group.
    if (k > 0 && !valuesTie(entry.v, order[groupStart].v, floor)) groupStart = k;
    out[entry.i] = { rank: groupStart + 1, tied: false };
  });
  const counts = new Map<number, number>();
  out.forEach((r) => counts.set(r.rank, (counts.get(r.rank) ?? 0) + 1));
  out.forEach((r) => { r.tied = (counts.get(r.rank) ?? 1) > 1; });
  return out;
}

/** "Austin, TX" / "Austin" / "" — never a bare ", " when the row has no address. */
function formatStopLocation(stop: { city: string | null; state: string | null }): string {
  return [stop.city, stop.state].filter((part) => !!part && part.trim() !== '').join(', ');
}

/** The backend appends the return-to-depot leg with distributor_id 0 (solve.py). */
function isDepotStop(stop: { distributor_id: number }): boolean {
  return stop.distributor_id === 0;
}

function ordinal(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  return `${n}${['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'}`;
}

// ── Solver options ──────────────────────────────────────────────────────────
// `POST /optimize/vrp` has always accepted `us_only` and `graph_aware`; until now
// the page sent no body, so every plan it has ever shown was solved with both
// off. Both still default to off, so the page's default output is unchanged —
// nothing moves unless the reader turns something on here.
type SolverOptions = { us_only: boolean; graph_aware: boolean };

const DEFAULT_SOLVER_OPTIONS: SolverOptions = { us_only: false, graph_aware: false };

const SOLVER_OPTION_NAMES: Record<keyof SolverOptions, string> = {
  us_only: 'Domestic suppliers only',
  graph_aware: 'Graph concentration surcharge',
};

function isDefaultOptions(o: SolverOptions): boolean {
  return !o.us_only && !o.graph_aware;
}

/** Which flags differ between two runs, named as the reader sees them. */
function changedOptionNames(a: SolverOptions, b: SolverOptions): string[] {
  return (Object.keys(SOLVER_OPTION_NAMES) as Array<keyof SolverOptions>)
    .filter((k) => a[k] !== b[k])
    .map((k) => SOLVER_OPTION_NAMES[k]);
}

/**
 * Everything about a set of plans a reader could see. Used to answer one honest
 * question after a re-run: did the flag actually change the answer?
 *
 * It has to, because it often does not. `graph_aware` adds a surcharge weighted by
 * raw betweenness centrality, and across this catalogue that weight is small — on a
 * cart where no supplier's centrality outweighs a real price gap the solver returns
 * the identical optimum. A toggle that silently does nothing reads as a broken
 * control; a toggle that says "this changed nothing on this cart" is a finding.
 */
function planFingerprint(result: MultiRouteResult | null): string {
  if (!result) return '';
  return result.alternatives
    .map((a) => [
      a.id, a.total_cost_usd, a.eta_p50, a.total_co2e_kg, a.total_distance_km,
      (a.sourcing ?? []).map((s) => `${s.component_id}:${s.distributor_id}:${s.quantity}`).join(','),
    ].join('|'))
    .join(';');
}

/** FastAPI puts a string here for HTTPException and an array for validation errors. */
function serverDetail(err: unknown): string | null {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d: { msg?: string }) => d?.msg ?? '').filter(Boolean).join(', ');
  return null;
}

/**
 * One solver flag. The state is carried by the word "On"/"Off" and an icon as well
 * as the fill, because a toggle whose only "on" signal is a colour is unreadable to
 * anyone who cannot see the difference.
 */
function SolverOptionToggle({
  on, onToggle, title, testId, children,
}: {
  on: boolean;
  onToggle: () => void;
  title: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-3">
      <button
        type="button"
        onClick={onToggle}
        aria-pressed={on}
        data-testid={testId}
        className={`w-full min-h-[44px] flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 ${
          on
            ? 'bg-blue-600 border-blue-500 text-white'
            : 'bg-slate-800 border-slate-600 text-slate-200 hover:bg-slate-700'
        }`}
      >
        <span className="text-left">{title}</span>
        <span className="inline-flex items-center gap-1 shrink-0 text-xs">
          {on ? <Check className="w-3.5 h-3.5" /> : <Minus className="w-3.5 h-3.5" />}
          {on ? 'On' : 'Off'}
        </span>
      </button>
      <p className="text-xs leading-5 text-slate-400 mt-2">{children}</p>
    </div>
  );
}

function RankBadge({ rank, total, tied = false }: { rank: number; total: number; tied?: boolean }) {
  if (tied) {
    // A tie is now a real tie, so it is worth saying WHERE the tie sits: tied for
    // best is a very different read from tied for last.
    const label = rank === 1 ? 'TIED BEST' : `TIED ${ordinal(rank)}`;
    return (
      <span
        className="text-[11px] font-medium text-amber-300/90 bg-amber-400/10 px-1.5 py-0.5 rounded inline-flex items-center gap-0.5 whitespace-nowrap"
        title={`Matches at least one other strategy on this metric to within the tie tolerance — ranked ${ordinal(rank)} alongside it, not above it`}
      >
        <Equal className="w-2.5 h-2.5" /> {label}
      </span>
    );
  }
  // Strictly better than every other plan on this metric — no one else is within
  // the tolerance. Emerald + ring so it reads as a win next to the amber ties.
  if (rank === 1) {
    return (
      <span
        className="text-[11px] font-bold text-emerald-300 bg-emerald-500/15 ring-1 ring-emerald-400/40 px-1.5 py-0.5 rounded"
        title="Strictly the best of the alternatives on this metric — beats every other plan by more than the tie tolerance"
      >
        BEST
      </span>
    );
  }
  if (rank === total) return <span className="text-[11px] font-medium text-red-300 bg-red-400/10 px-1.5 py-0.5 rounded">{ordinal(rank)}</span>;
  return <span className="text-[11px] font-medium text-slate-400 bg-slate-600/30 px-1.5 py-0.5 rounded">{ordinal(rank)}</span>;
}

function DeltaIndicator({ value, baseline, unit, floor, invert = false }: {
  value: number; baseline: number; unit: string; floor: number; invert?: boolean;
}) {
  if (baseline === 0) return null;
  // "same" uses the SAME rule as the TIED badge, so a card can never show BEST
  // next to a grey "same" (the old 0.5% cut-off did exactly that for a $41 win).
  if (valuesTie(value, baseline, floor)) return <span className="text-[11px] text-slate-400">same</span>;
  const pct = ((value - baseline) / baseline) * 100;
  const isGood = invert ? pct > 0 : pct < 0;
  // A near-zero baseline (e.g. a route with almost no international leg) turns an
  // ordinary-sized absolute change into a percentage like "+5900.6%" — arithmetically
  // exact, but it reads as a typo next to every other single/double-digit badge on
  // the page. Past 10x, switch to a multiplier framing instead of a percentage.
  const asMultiple = Math.abs(pct) / 100;
  const useMultiplier = Math.abs(pct) >= 999;
  return (
    <span className={`text-[11px] font-medium flex items-center gap-0.5 ${isGood ? 'text-green-400' : 'text-red-400'}`}>
      {isGood ? <TrendingDown className="w-2.5 h-2.5" /> : <TrendingUp className="w-2.5 h-2.5" />}
      {useMultiplier
        ? `${pct > 0 ? '+' : '-'}${asMultiple >= 10 ? asMultiple.toFixed(0) : asMultiple.toFixed(1)}×`
        : `${pct > 0 ? '+' : ''}${Math.abs(pct) < 0.1 ? pct.toFixed(2) : pct.toFixed(1)}%`} {unit}
    </span>
  );
}

function MetricRow({ label, value, rank, total, tied = false, delta }: {
  label: string; value: string; rank: number; total: number; tied?: boolean; delta?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-xs text-slate-400">{label}</span>
      <div className="flex items-center gap-2">
        {!tied && delta}
        <span className="text-sm font-semibold text-white">{value}</span>
        <RankBadge rank={rank} total={total} tied={tied} />
      </div>
    </div>
  );
}

/**
 * Classify the solver's refusal.
 *
 * `POST /optimize/vrp` answers a cart it cannot source with **400** and a body of
 * exactly `{"detail": "<the solver's sentence>"}` (backend/app/api/optimize.py:145-150).
 * There is no `code`, no `pool`, no `strategy` field — so the solver's own sentence
 * is the ONLY thing that can be read for the kind of refusal and for the supplier
 * pool that was actually searched. Both messages are literals in
 * backend/app/optimization/sourcing.py: "No valid offers for components after
 * filtering: ..." (line 643) and "Insufficient stock to fill the BOM from
 * {domestic distributors only|all distributors}: ..." (line 677, where that pool
 * phrase is chosen by the effective `us_only` the solve actually ran with).
 */
type SolverFailureKind = 'no-offers' | 'stock-shortfall' | 'unknown';

function classifySolverFailure(message: string): {
  kind: SolverFailureKind;
  /** The pool the message itself names — never inferred from the toggles. */
  pool: 'domestic' | 'all' | null;
} {
  const pool = /domestic distributors only/i.test(message)
    ? 'domestic'
    : /all distributors/i.test(message)
      ? 'all'
      : null;
  if (/No valid offers/i.test(message)) return { kind: 'no-offers', pool };
  if (/Insufficient stock to fill the BOM/i.test(message)) return { kind: 'stock-shortfall', pool };
  return { kind: 'unknown', pool };
}

// Macro stress regime read-out. The optimizer prices a stock-out risk premium
// off `stress_probability` (backend/app/optimization/sourcing.py) — this banner
// is why the component/transport split above moved, not decoration. When the
// model's ship gate has failed it says so plainly: 0.0 is a documented
// fallback, never a "prediction," and the banner is honest about that too.
const STRESS_LEVEL_STYLE: Record<string, { border: string; bg: string; text: string; dot: string }> = {
  high:     { border: 'border-red-500/30',    bg: 'bg-red-500/5',    text: 'text-red-400',    dot: 'bg-red-400' },
  moderate: { border: 'border-amber-500/30',  bg: 'bg-amber-500/5',  text: 'text-amber-400',  dot: 'bg-amber-400' },
  low:      { border: 'border-emerald-500/30', bg: 'bg-emerald-500/5', text: 'text-emerald-400', dot: 'bg-emerald-400' },
  unavailable: { border: 'border-slate-700', bg: 'bg-slate-800/60', text: 'text-slate-400', dot: 'bg-slate-500' },
};

/**
 * ONE rounding call for `stress_probability`, so the pill and the prose beside it
 * cannot disagree. The `interpretation` sentence a few lines below is built by the
 * backend with Python's `{prob:.0%}` (backend/app/api/ml.py:545) and is served as
 * finished text this page cannot reformat — so the pill matches ITS precision
 * rather than the other way round. Before this, 0.8284 rendered as "82.8%" in the
 * pill and "(83%)" in the sentence directly underneath it.
 */
const formatStressPct = (probability: number): string => `${Math.round(probability * 100)}%`;

function MacroStressBanner({ stress }: { stress: StressResponse | null }) {
  if (!stress) return null;
  const style = STRESS_LEVEL_STYLE[stress.stress_level] || STRESS_LEVEL_STYLE.unavailable;
  // DATA VINTAGE. The probability beside it is one row of a MONTHLY frame and
  // is typically a month or two behind the wall clock, so it is shown at 12px —
  // LARGER than the 11px pill carrying the claim it qualifies. A caveat set in
  // smaller print than its claim has shipped from this repo before.
  const obsMonth = stressObservationMonth(stress);
  const vintageChip = obsMonth ? `as of ${obsMonth}` : 'vintage unknown';
  return (
    <div className={`rounded-xl border ${style.border} ${style.bg} p-4 mb-5`} data-testid="macro-stress-banner">
      <div className="flex items-start gap-3">
        <div className="p-2 rounded-lg bg-slate-900/40 shrink-0">
          <Activity className={`w-4 h-4 ${style.text}`} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-xs font-semibold uppercase tracking-wider ${style.text}`}>
              Macro Stress Regime
            </span>
            <span data-testid="stress-claim" className={`inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-full ${style.text} bg-slate-900/40`}>
              <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`} />
              {stress.available ? `${stress.stress_source} · ${formatStressPct(stress.stress_probability)}` : 'unavailable'}
            </span>
            {stress.available && (
              <span
                data-testid="stress-vintage"
                className={`inline-flex items-center text-xs font-semibold px-1.5 py-0.5 rounded-full ${
                  stress.vintage_is_stale ? 'text-amber-200 bg-amber-500/20' : 'text-amber-300 bg-amber-500/10'
                }`}
                title={stress.vintage_label}
              >
                {vintageChip}
              </span>
            )}
            {stress.ship_gate_passed !== null && (
              <span className={`text-[11px] px-1.5 py-0.5 rounded-full ${stress.ship_gate_passed ? 'text-emerald-400 bg-emerald-500/10' : 'text-slate-400 bg-slate-700/30'}`}>
                ship gate {stress.ship_gate_passed ? 'passed' : 'failed'}
              </span>
            )}
          </div>
          {stress.available && (
            <p data-testid="stress-vintage-note" className="text-xs text-amber-400/90 mt-1 leading-snug">
              {stress.vintage_label}
              {'. The stock-out premium priced into the costs above comes from that '}
              {'observation month, not from today.'}
              {stress.vintage_is_stale
                ? ` That is past the ${stress.max_observation_age_days}-day tolerance for this frame.`
                : ''}
            </p>
          )}
          <p className="text-xs text-slate-300 mt-1">{stress.interpretation}</p>
          {stress.brier !== null && stress.baseline_brier !== null && (
            <p className="text-xs text-slate-400 mt-1">
              Brier {stress.brier.toFixed(3)} vs persistence {stress.baseline_brier.toFixed(3)}
              {stress.climatology_brier !== null && ` vs climatology ${stress.climatology_brier.toFixed(3)}`}
              {stress.calibration_slope !== null && ` · calibration slope ${stress.calibration_slope.toFixed(3)}`}
              {' — '}
              <Link to="/model-card" className="text-slate-400 underline hover:text-white">full model card</Link>
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export default function CheckoutPage() {
  const navigate = useNavigate();
  const { items, clearCart, fetchCart, error: cartError } = useCartStore();
  const { multiResult, selectedId, setMultiResult, setSelectedId, clearResult } = useOptimizeStore();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Flags sent with the run whose result is on screen. Both false = the historical
  // behaviour, so a first visit is identical to what this page has always shown.
  const [options, setOptions] = useState<SolverOptions>(DEFAULT_SOLVER_OPTIONS);
  // Set when a re-run came back with plans identical to the ones it replaced.
  const [noChangeNotice, setNoChangeNotice] = useState<string | null>(null);
  // Monotonic run id: a slow first response must not overwrite a faster second one
  // and leave the cards disagreeing with the toggles that produced them.
  const runSeq = useRef(0);
  const [cartLoading, setCartLoading] = useState(true);
  const [expandedCard, setExpandedCard] = useState<string | null>(null);
  const [stress, setStress] = useState<StressResponse | null>(null);
  const [finishing, setFinishing] = useState(false);
  const [finishError, setFinishError] = useState<string | null>(null);

  // Fetch cart on mount
  useEffect(() => {
    setCartLoading(true);
    fetchCart().finally(() => setCartLoading(false));
  }, [fetchCart]);

  const retryCart = () => {
    setCartLoading(true);
    fetchCart().finally(() => setCartLoading(false));
  };

  // "Confirm Order" used to call clearCart() unawaited and navigate away, while
  // claiming to place an order. There is no order/PO endpoint on this API
  // (see /openapi.json — cart, optimize, resilience, ml only), so the action is
  // labelled for what it actually does and its one real side effect is awaited.
  const acceptPlan = async () => {
    setFinishing(true);
    setFinishError(null);
    try {
      await clearCart();
      navigate('/dashboard');
    } catch (err: unknown) {
      setFinishError(err instanceof Error ? err.message : 'Could not clear the cart — it is unchanged.');
    } finally {
      setFinishing(false);
    }
  };

  // Macro stress regime — explains the risk premium baked into the costs below.
  useEffect(() => {
    mlAPI.stress().then((res) => setStress(res.data)).catch(() => setStress(null));
  }, []);

  // Re-solve the cart under `next`. Used by the first automatic run (with the
  // defaults) and by every toggle after it, so there is one code path and the
  // toggles cannot drift from what was actually sent.
  const runOptimization = (next: SolverOptions) => {
    const seq = ++runSeq.current;
    const previousFingerprint = planFingerprint(multiResult);
    const changed = changedOptionNames(options, next);
    setOptions(next);
    setLoading(true);
    setError(null);
    setNoChangeNotice(null);
    optimizeAPI.vrp(next as VrpOptions)
      .then((res) => {
        if (seq !== runSeq.current) return;
        setMultiResult(res.data);
        // Report a no-op honestly instead of leaving the reader to wonder whether
        // the control works.
        const identical =
          previousFingerprint !== '' &&
          planFingerprint(res.data as MultiRouteResult) === previousFingerprint;
        setNoChangeNotice(identical && changed.length > 0 ? changed.join(' and ') : null);
      })
      .catch((err: unknown) => {
        if (seq !== runSeq.current) return;
        // Drop the previous plan rather than leave it under changed toggles: it was
        // solved with different flags and would be read as this run's answer.
        clearResult();
        setError(serverDetail(err) ?? 'Optimization failed');
      })
      .finally(() => {
        if (seq === runSeq.current) setLoading(false);
      });
  };

  // Auto-run optimization after cart loads
  useEffect(() => {
    if (cartLoading || items.length === 0 || multiResult) return;
    let cancelled = false;
    const seq = ++runSeq.current;
    setLoading(true);
    setError(null);
    optimizeAPI.vrp(DEFAULT_SOLVER_OPTIONS)
      .then((res) => {
        if (!cancelled && seq === runSeq.current) setMultiResult(res.data);
      })
      .catch((err: unknown) => {
        if (!cancelled && seq === runSeq.current) setError(serverDetail(err) ?? 'Optimization failed');
      })
      .finally(() => {
        if (!cancelled && seq === runSeq.current) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [items.length, cartLoading]);

  const alternatives = useMemo(() => multiResult?.alternatives ?? [], [multiResult]);
  const selected = alternatives.find((a) => a.id === selectedId) ?? null;
  const total = alternatives.length;

  // Find baseline (balanced) for delta comparisons
  const baseline = alternatives.find((a) => a.id === 'balanced');

  // Tie-aware ranks + degenerate-strategy detection (C1). Derived from the
  // numbers actually returned, so it self-corrects if the optimizer changes.
  const comparison = useMemo(() => {
    if (alternatives.length === 0) return null;
    const ranks = {
      cost: tieAwareRanks(alternatives.map((a) => a.total_cost_usd), TIE_FLOOR.cost),
      speed: tieAwareRanks(alternatives.map((a) => a.eta_p50), TIE_FLOOR.speed),
      carbon: tieAwareRanks(alternatives.map((a) => a.total_co2e_kg), TIE_FLOOR.carbon),
      distance: tieAwareRanks(alternatives.map((a) => a.total_distance_km), TIE_FLOOR.distance),
    };
    const METRICS = ['cost', 'speed', 'carbon', 'distance'] as const;
    const sameOnEveryMetric = (i: number, j: number) =>
      METRICS.every((m) => ranks[m][i].rank === ranks[m][j].rank);

    // Cluster the alternatives that are indistinguishable on every metric.
    const clusters: number[][] = [];
    alternatives.forEach((_, i) => {
      const existing = clusters.find((c) => sameOnEveryMetric(c[0], i));
      if (existing) existing.push(i);
      else clusters.push([i]);
    });
    const largest = clusters.reduce((best, c) => (c.length > best.length ? c : best), clusters[0] ?? []);

    return {
      ranks,
      allIdentical: alternatives.length > 1 && clusters.length === 1,
      convergedLabels: largest.length > 1 ? largest.map((i) => alternatives[i].label) : [],
      distinctCount: clusters.length,
    };
  }, [alternatives]);

  // Monte Carlo histogram for the selected route.
  //
  // B4: the plotted x-domain is built from the samples AND from every marker we
  // intend to draw, so a summary tile can never sit outside the chart it is
  // supposed to summarise. Anything still out of range is clamped to the edge and
  // flagged, and the caption reports the real sample count from the payload.
  // Depot-aware view of the selected route.
  //
  // `route` is not a list of supplier stops: the backend appends the
  // return-to-depot leg to it as a stop with distributor_id 0 and no city/state
  // (backend/app/optimization/solve.py), and stop_count counts that leg. That is
  // why a one-supplier plan announced "2 Stops" and why the list printed
  // "Factory (Depot) ," with an orphan comma. Split the two apart once, here.
  const routeView = useMemo(() => {
    const legs = selected?.route ?? [];
    return {
      supplierStops: legs.filter((s) => !isDepotStop(s)),
      returnLeg: legs.find(isDepotStop) ?? null,
    };
  }, [selected]);

  const distribution = useMemo(() => {
    if (!selected) return null;
    const samples = (selected.monte_carlo_samples ?? []).filter((s): s is number => Number.isFinite(s));
    const markers = [
      { key: 'p10', label: 'P10', value: selected.eta_p10, color: '#4ade80' },
      { key: 'p50', label: 'P50', value: selected.eta_p50, color: '#60a5fa' },
      { key: 'p90', label: 'P90', value: selected.eta_p90, color: '#f87171' },
    ].filter((m) => Number.isFinite(m.value));
    if (samples.length === 0) return { count: 0, bars: [], markers: [], outsideSupport: [], clamped: [], sampleMin: 0, sampleMax: 0 };

    const sampleMin = Math.min(...samples);
    const sampleMax = Math.max(...samples);
    let lo = Math.min(sampleMin, ...markers.map((m) => m.value));
    let hi = Math.max(sampleMax, ...markers.map((m) => m.value));
    if (!(hi > lo)) { lo -= 0.5; hi += 0.5; }        // degenerate spread — give it width
    const pad = (hi - lo) * 0.03;                    // breathing room so an edge marker isn't flush against the frame
    lo -= pad;
    hi += pad;
    const span = hi - lo;
    const binSize = span / HIST_BINS;

    const counts = new Array<number>(HIST_BINS).fill(0);
    samples.forEach((s) => {
      const idx = Math.min(HIST_BINS - 1, Math.max(0, Math.floor((s - lo) / binSize)));
      counts[idx] += 1;
    });
    const bars = counts.map((count, i) => ({
      bin: i,
      count,
      center: lo + (i + 0.5) * binSize,
      from: lo + i * binSize,
      to: lo + (i + 1) * binSize,
    }));

    const placed = markers.map((m) => {
      const clampedValue = Math.min(hi, Math.max(lo, m.value));
      return {
        ...m,
        // Percent across the plot area. Bin 0 starts at `lo` and bin N-1 ends at
        // `hi`, so this maps exactly onto the rendered bars.
        pct: ((clampedValue - lo) / span) * 100,
        wasClamped: clampedValue !== m.value,
        outsideSupport: m.value < sampleMin - 1e-9 || m.value > sampleMax + 1e-9,
      };
    });

    return {
      count: samples.length,
      bars,
      markers: placed,
      sampleMin,
      sampleMax,
      clamped: placed.filter((m) => m.wasClamped),
      outsideSupport: placed.filter((m) => m.outsideSupport),
    };
  }, [selected]);

  if (cartLoading) {
    return (
      <div className="min-h-full bg-slate-900 text-slate-100 overflow-y-auto h-full flex items-center justify-center">
        <div className="w-8 h-8 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
      </div>
    );
  }

  // C7: an empty `items` array can mean "the cart is empty" or "the cart request
  // failed". Those are different facts and must not share the cheerful empty state.
  if (cartError && items.length === 0 && !multiResult) {
    return (
      <div className="min-h-full bg-slate-900 text-slate-100 overflow-y-auto h-full flex items-center justify-center px-6">
        <div className="max-w-md w-full text-center bg-red-900/20 border border-red-700/50 rounded-xl p-6" data-testid="cart-load-error">
          <AlertTriangle className="w-10 h-10 text-red-400 mx-auto mb-3" />
          <div className="text-lg font-semibold text-red-200">Couldn&apos;t load your cart</div>
          <p className="text-sm text-red-200/80 mt-1.5">{cartError}</p>
          <p className="text-xs text-slate-400 mt-2">
            Your cart may still have items — this is a load failure, not an empty cart.
          </p>
          <div className="flex items-center justify-center gap-2 mt-4">
            <button
              onClick={retryCart}
              className="inline-flex items-center gap-1.5 bg-red-600/80 hover:bg-red-500 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            >
              <RotateCcw className="w-3.5 h-3.5" /> Retry
            </button>
            <button onClick={() => navigate('/cart')} className="text-sm text-slate-400 hover:text-white px-3 py-2 transition-colors">
              Go to Cart
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (items.length === 0 && !multiResult) {
    return (
      <div className="min-h-full bg-slate-900 text-slate-100 overflow-y-auto h-full flex items-center justify-center">
        <div className="text-center">
          <div className="text-5xl mb-4">
            <Truck className="w-12 h-12 text-slate-400 mx-auto" />
          </div>
          <div className="text-lg font-medium text-slate-400">No items in cart</div>
          <button onClick={() => navigate('/cart')} className="mt-4 bg-blue-600 hover:bg-blue-500 text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors">
            Go to Cart
          </button>
        </div>
      </div>
    );
  }

  return (
    // App.tsx puts every routed page inside `flex-1 overflow-hidden` under a
    // `h-screen` column. That parent is a fixed-height box that will NOT scroll,
    // so a page root has to own its own scrolling: `h-full` makes this div exactly
    // as tall as that box and `overflow-y-auto` gives it the scrollport. Without
    // them this page was 1557px of content inside an 852px window with no way to
    // move it — the route breakdown, the Monte Carlo histogram, the strategy
    // comparison and the Accept Plan button were all simply unreachable.
    // `min-h-full` (not `min-h-screen`): 100vh is TALLER than the parent by the
    // height of the nav bar, which pushed the last ~64px of the scrollport below
    // the parent's clip and stranded the final rows at maximum scroll.
    <div className="min-h-full bg-slate-900 text-slate-100 overflow-y-auto h-full">
      <div className="max-w-6xl mx-auto px-6 py-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-xl font-bold text-white">Route Optimization</h1>
            <p className="text-sm text-slate-400 mt-0.5">Four sourcing strategies, each solved with CP-SAT on this cart. Two can land on the same plan &mdash; when they do, the table says so.</p>
          </div>
          <button onClick={() => navigate('/cart')} className="text-xs text-slate-400 hover:text-white min-h-[44px] px-2 transition-colors">
            ← Back to Cart
          </button>
        </div>

        {/* Solver options — the two flags `POST /optimize/vrp` accepts. */}
        <div className="rounded-xl border border-slate-700 bg-slate-800 p-4 mb-5" data-testid="solver-options">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <h2 className="text-sm font-semibold text-white">Solver options</h2>
            <span className="text-xs text-slate-400">
              {isDefaultOptions(options)
                ? 'Both off — this is the standard run'
                : `Changed from the standard run: ${(Object.keys(SOLVER_OPTION_NAMES) as Array<keyof SolverOptions>)
                    .filter((k) => options[k])
                    .map((k) => SOLVER_OPTION_NAMES[k])
                    .join(', ')}`}
            </span>
          </div>
          <p className="text-xs leading-5 text-slate-400 mt-1">
            Changing either one re-runs the sourcing MILP and replaces all four plans below.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
            <SolverOptionToggle
              on={options.us_only}
              onToggle={() => runOptimization({ ...options, us_only: !options.us_only })}
              title="Domestic suppliers only"
              testId="toggle-us-only"
            >
              Removes every non-US distributor from the supplier pool. Only <b className="font-semibold text-slate-300">Lowest
              Cost</b> changes: Fastest Delivery, Lowest Carbon and Balanced already source
              domestically by definition, so their plans are the same either way.
            </SolverOptionToggle>
            <SolverOptionToggle
              on={options.graph_aware}
              onToggle={() => runOptimization({ ...options, graph_aware: !options.graph_aware })}
              title="Graph concentration surcharge"
              testId="toggle-graph-aware"
            >
              Adds a term to the solver&apos;s objective that charges each offer by its distributor&apos;s
              betweenness centrality times the cost of re-sourcing that line, pushing plans off
              highly connected suppliers. The centrality is a raw structural score used as a
              weight, not a fitted failure probability, and the quoted prices below are the real
              prices of whatever plan it picks — the surcharge is never billed. Betweenness runs
              small across this catalogue, so on many carts the optimum does not move; when that
              happens this panel says so rather than leaving you guessing.
            </SolverOptionToggle>
          </div>
          {!loading && noChangeNotice && (
            <div
              className="mt-3 rounded-lg border border-slate-600 bg-slate-900/60 p-3 flex items-start gap-2.5"
              data-testid="solver-options-no-change"
            >
              <Equal className="w-4 h-4 text-slate-300 shrink-0 mt-0.5" />
              <p className="text-xs leading-5 text-slate-300">
                <b className="font-semibold">Same answer.</b> The solver re-ran with {noChangeNotice}{' '}
                changed and returned plans identical to the ones it replaced — same suppliers, same
                cost, same ETA, same CO2e, same distance. The flag reached the solver; on this cart it
                does not move the optimum.
              </p>
            </div>
          )}
        </div>

        {/* Macro stress regime — why the costs below moved */}
        {!loading && alternatives.length > 0 && <MacroStressBanner stress={stress} />}

        {/* Loading state */}
        {loading && (
          <div className="flex flex-col items-center justify-center py-24">
            <div className="relative">
              <div className="w-16 h-16 rounded-full border-2 border-slate-700" />
              <div className="absolute inset-0 w-16 h-16 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
            </div>
            <p className="text-blue-400 text-sm mt-4 font-medium">Solving sourcing MILP and pickup tour...</p>
            <p className="text-slate-400 text-xs mt-1">Generating 4 route strategies with Monte Carlo simulation</p>
          </div>
        )}

        {/* Failure. The solver refuses a cart it cannot source, and the specific
            refusal matters — it is a fact about the CATALOGUE, not about the toggles
            above, and not about the server being broken. Three of the four strategies
            hard-code domestic-only sourcing (backend/app/optimization/strategies.py:55,
            97, 109), so a line with no US offer — or with less US stock than the BOM
            needs — stops the run whatever "Domestic suppliers only" is set to. Saying
            "turn domestic-only off and it will work" would be a guess, so the page
            reports the solver's own sentence, names the pool THAT SENTENCE names, and
            offers the two actions that are actually available.

            Every line below is a qualifier on the headline, so none of them is set in
            smaller type than the headline: this repo's standing rule is that a caveat
            is never smaller print than the claim it qualifies. */}
        {error && !loading && (() => {
          const failure = classifySolverFailure(error);
          return (
          <div
            className="bg-red-900/30 border border-red-700/50 rounded-lg p-4 mb-6"
            data-testid="optimize-error"
          >
            <div className="flex items-start gap-3">
              <AlertTriangle className="w-5 h-5 text-red-400 shrink-0 mt-0.5" />
              <div className="min-w-0">
                <div className="text-sm font-semibold text-red-200" data-testid="optimize-error-headline">
                  {failure.kind === 'no-offers'
                    ? 'The solver could not source this cart'
                    : failure.kind === 'stock-shortfall'
                      ? 'Not enough catalogue stock to fill this cart'
                      : 'Optimization failed'}
                </div>
                {failure.kind === 'no-offers' && (
                  <p className="text-sm leading-6 text-red-200/90 mt-1.5">
                    At least one line has no offer left once the price-outlier filter and the
                    domestic-supplier filter have run. Fastest Delivery, Lowest Carbon and
                    Balanced are domestic-only whether or not &quot;Domestic suppliers only&quot; is on,
                    so a part with no US offer stops the run in both states. The parts the solver
                    named are below.
                  </p>
                )}
                {failure.kind === 'stock-shortfall' && (
                  <p className="text-sm leading-6 text-red-200/90 mt-1.5">
                    The solver did not break. The sourcing MILP may only order what a distributor
                    reports on the shelf, so a line that needs more units than the pool actually
                    holds has no feasible plan and the run is refused rather than filled with a
                    quantity nobody can ship. The shortfall the solver measured is below.
                  </p>
                )}
                <p className="text-sm leading-6 text-red-100/90 mt-2 font-mono break-words">{error}</p>
                <p className="text-sm leading-6 text-slate-300 mt-2">
                  Solved with: {SOLVER_OPTION_NAMES.us_only} {options.us_only ? 'on' : 'off'},{' '}
                  {SOLVER_OPTION_NAMES.graph_aware} {options.graph_aware ? 'on' : 'off'}.
                  {failure.pool === 'domestic' && (
                    <>
                      {' '}That toggle is not what decided the pool named above: Fastest Delivery,
                      Lowest Carbon and Balanced source{' '}
                      <b className="font-semibold text-white">domestically whatever it is set to</b>,
                      and only Lowest Cost follows it.
                      {options.us_only
                        ? ' Turning it off would widen the pool for Lowest Cost alone.'
                        : ' So this run searched the domestic pool with the toggle already off, and'
                          + ' there is no setting of it that widens the pool for those three.'}
                    </>
                  )}
                  {failure.pool === 'all' && (
                    <>
                      {' '}The run above searched the{' '}
                      <b className="font-semibold text-white">full supplier pool</b>, so no setting
                      of that toggle widens it.
                    </>
                  )}
                </p>
                <div className="flex flex-wrap items-center gap-2 mt-3">
                  <button
                    onClick={() => runOptimization(options)}
                    className="inline-flex items-center gap-1.5 min-h-[44px] bg-red-600/80 hover:bg-red-500 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
                  >
                    <RotateCcw className="w-3.5 h-3.5" /> Try again
                  </button>
                  {!isDefaultOptions(options) && (
                    <button
                      onClick={() => runOptimization(DEFAULT_SOLVER_OPTIONS)}
                      className="inline-flex items-center min-h-[44px] text-sm text-slate-300 hover:text-white px-3 py-2 rounded-lg border border-slate-600 hover:border-slate-500 transition-colors"
                    >
                      Reset options and re-run
                    </button>
                  )}
                  <button
                    onClick={() => navigate('/cart')}
                    className="inline-flex items-center min-h-[44px] text-sm text-slate-400 hover:text-white px-3 py-2 transition-colors"
                  >
                    Edit cart
                  </button>
                </div>
              </div>
            </div>
          </div>
          );
        })()}

        {/* Route alternative cards */}
        {alternatives.length > 0 && (
          <>
            {/* C1: when strategies resolve to the same plan, say so instead of
                ranking identical numbers BEST / 2nd / 3rd / 4th. */}
            {comparison && comparison.convergedLabels.length > 1 && (
              <div
                className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 mb-5 flex items-start gap-3"
                data-testid="strategy-convergence-notice"
              >
                <div className="p-2 rounded-lg bg-slate-900/40 shrink-0">
                  <Equal className="w-4 h-4 text-amber-400" />
                </div>
                <div className="min-w-0">
                  <div className="text-xs font-semibold uppercase tracking-wider text-amber-400">
                    {comparison.allIdentical ? 'Strategies not differentiated' : 'Some strategies are tied'}
                  </div>
                  <p className="text-xs text-slate-300 mt-1">
                    {comparison.allIdentical ? (
                      <>
                        All {alternatives.length} strategies converge on the same plan for this BOM — identical cost,
                        median ETA, CO2e and distance. The cart is too small to differentiate them, so none is ranked
                        above another. Add more line items or distributors spread further apart to see the objectives pull apart.
                      </>
                    ) : (
                      <>
                        {comparison.convergedLabels.join(', ')} return the same plan on this BOM — matching cost, median
                        ETA, CO2e and distance to within $1, 0.05 days, 0.05 kg and 0.5 km. They are shown as tied rather
                        than ranked against each other;{' '}
                        {comparison.distinctCount} genuinely distinct plan{comparison.distinctCount === 1 ? ' was' : 's were'} found
                        across {alternatives.length} strategies. Anything outside those tolerances is ranked, and the
                        strict winner of each metric carries a BEST badge.
                      </>
                    )}
                  </p>
                </div>
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4 mb-6" data-testid="route-cards">
              {alternatives.map((alt, altIdx) => {
                const meta = STRATEGY_META[alt.id] || STRATEGY_META.balanced;
                const Icon = meta.icon;
                const isSelected = selectedId === alt.id;
                const isRecommended = multiResult?.recommended_id === alt.id;
                const isExpanded = expandedCard === alt.id;
                // `savings_vs_direct_pct` is SIGNED: positive = consolidating is cheaper
                // than direct pickup, negative = it costs MORE. The card used to print a
                // hardcoded minus in front of it (so -6.62 rendered as "--6.6%") and
                // always struck through the direct price while highlighting the hub
                // price, which said "you saved money" on a plan that costs $23 more.
                // Sign and treatment are both derived from the number now. The 0.05pp
                // dead-band keeps a rounding-noise difference from being called either.
                const cd = alt.cross_dock;
                const cdSaves = cd ? cd.savings_vs_direct_pct > 0.05 : false;
                const cdCostsMore = cd ? cd.savings_vs_direct_pct < -0.05 : false;

                return (
                  <div
                    key={alt.id}
                    className={`relative rounded-xl border transition-all cursor-pointer ${
                      isSelected
                        ? 'border-blue-500 bg-gradient-to-b from-blue-500/10 to-slate-800 shadow-lg shadow-blue-500/10'
                        : 'border-slate-700 bg-slate-800 hover:border-slate-600'
                    }`}
                    onClick={() => setSelectedId(alt.id)}
                  >
                    {/* Recommended badge */}
                    {isRecommended && (
                      <div className="absolute -top-2.5 left-4 flex items-center gap-1 bg-purple-600 text-white text-[11px] font-semibold px-2 py-0.5 rounded-full">
                        <Star className="w-2.5 h-2.5" /> RECOMMENDED
                      </div>
                    )}

                    <div className="p-4">
                      {/* Strategy header.
                          The four descriptions run anywhere from one line to four, which
                          used to push "Total Cost" to a different height on every card and
                          made the side-by-side comparison useless. Title and description are
                          now separate fixed-height blocks, so the metric rows below start on
                          the same baseline in all four cards. */}
                      <div className="flex items-center justify-between gap-2 h-7">
                        <div className="flex items-center gap-2 min-w-0">
                          <div className={`p-1.5 rounded-lg bg-gradient-to-br ${meta.gradient} shrink-0`}>
                            <Icon className={`w-4 h-4 ${meta.color}`} />
                          </div>
                          <div className="text-sm font-semibold text-white truncate">{alt.label}</div>
                        </div>
                        {isSelected && <Check className="w-4 h-4 text-blue-400 shrink-0" />}
                      </div>
                      {/* h-12 (48px) held exactly TWO 20px lines while `line-clamp-3`
                          permitted three, so "Lowest Carbon" and "Balanced" — the
                          RECOMMENDED card — were sliced horizontally through the middle
                          of the third line's letterforms, with no ellipsis to say so.
                          The block is now 3 lines tall (3 x leading-5 = 60px) and still
                          a FIXED height, so the metric rows underneath keep starting on
                          the same baseline in all four cards. */}
                      <p
                        className="text-xs leading-5 text-slate-400 mt-2 mb-3 h-[3.75rem] overflow-hidden line-clamp-3"
                        title={alt.description}
                      >
                        {alt.description}
                      </p>

                      {/* Key metrics */}
                      <div className="space-y-0.5 border-t border-slate-700/50 pt-3">
                        <MetricRow
                          label="Total Cost"
                          value={`$${alt.total_cost_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                          rank={comparison?.ranks.cost[altIdx].rank ?? alt.cost_rank}
                          total={total}
                          tied={comparison?.ranks.cost[altIdx].tied ?? false}
                          delta={baseline && alt.id !== 'balanced' ? <DeltaIndicator value={alt.total_cost_usd} baseline={baseline.total_cost_usd} unit="cost" floor={TIE_FLOOR.cost} /> : undefined}
                        />
                        <MetricRow
                          label="Median ETA"
                          value={`${alt.eta_p50.toFixed(1)}d`}
                          rank={comparison?.ranks.speed[altIdx].rank ?? alt.speed_rank}
                          total={total}
                          tied={comparison?.ranks.speed[altIdx].tied ?? false}
                          delta={baseline && alt.id !== 'balanced' ? <DeltaIndicator value={alt.eta_p50} baseline={baseline.eta_p50} unit="time" floor={TIE_FLOOR.speed} /> : undefined}
                        />
                        <MetricRow
                          label="CO2 Emissions"
                          value={`${alt.total_co2e_kg.toFixed(1)} kg`}
                          rank={comparison?.ranks.carbon[altIdx].rank ?? alt.carbon_rank}
                          total={total}
                          tied={comparison?.ranks.carbon[altIdx].tied ?? false}
                          delta={baseline && alt.id !== 'balanced' ? <DeltaIndicator value={alt.total_co2e_kg} baseline={baseline.total_co2e_kg} unit="CO2" floor={TIE_FLOOR.carbon} /> : undefined}
                        />
                        <MetricRow
                          label="Distance"
                          value={`${alt.total_distance_km.toLocaleString(undefined, { maximumFractionDigits: 0 })} km`}
                          rank={comparison?.ranks.distance[altIdx].rank ?? alt.distance_rank}
                          total={total}
                          tied={comparison?.ranks.distance[altIdx].tied ?? false}
                        />
                      </div>

                      {/* Expand toggle. py-3.5 brings the tap target to ~44px tall —
                          it was 70×15, gating the whole "More detail" panel behind a
                          target well under the 44px touch floor on a phone. */}
                      <button
                        onClick={(e) => { e.stopPropagation(); setExpandedCard(isExpanded ? null : alt.id); }}
                        className="flex items-center gap-1 text-[11px] text-slate-400 hover:text-slate-300 mt-1 py-3.5 transition-colors"
                      >
                        {isExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                        {isExpanded ? 'Less detail' : 'More detail'}
                      </button>

                      {/* Expanded details */}
                      {isExpanded && (
                        <div className="mt-3 pt-3 border-t border-slate-700/50 space-y-2 text-xs">
                          <div className="flex justify-between text-slate-400">
                            <span>Transport Cost</span>
                            <span className="text-white">${alt.total_transport_cost_usd.toFixed(0)}</span>
                          </div>
                          <div className="flex justify-between text-slate-400">
                            <span>Component Cost</span>
                            <span className="text-white">${alt.total_component_cost_usd.toFixed(0)}</span>
                          </div>
                          <div className="flex justify-between text-slate-400">
                            <span>Int'l Stops</span>
                            <span className="text-white">{alt.international_stops}</span>
                          </div>
                          <div className="flex justify-between text-slate-400">
                            <span>Best Case (P10)</span>
                            <span className="text-green-400">{alt.eta_p10.toFixed(1)}d</span>
                          </div>
                          <div className="flex justify-between text-slate-400">
                            <span>Worst Case (P90)</span>
                            <span className="text-red-400">{alt.eta_p90.toFixed(1)}d</span>
                          </div>
                          <div className="flex justify-between text-slate-400">
                            <span>Supplier Stops</span>
                            {/* stop_count includes the return-to-depot leg; count the
                                real pickups so this agrees with the route panel. */}
                            <span className="text-white">
                              {alt.route.length > 0 ? alt.route.filter((s) => !isDepotStop(s)).length : alt.stop_count}
                            </span>
                          </div>
                        </div>
                      )}

                      {/* Objective Breakdown (weight bar + cost table + citations) */}
                      {alt.strategy_math && (
                        <details className="mt-4 border-t border-slate-700" data-testid="objective-breakdown">
                          {/* Was 308×15 — far under the 44px touch floor, and this
                              disclosure gates the entire audit trail (weights, cost
                              breakdown, citations) behind it on a phone. */}
                          <summary className="cursor-pointer block py-3.5 text-[11px] text-slate-400 hover:text-white uppercase tracking-wider font-semibold">
                            Objective Breakdown
                          </summary>

                          <div className="mt-3 space-y-3">
                            {/* Stacked weight bar */}
                            <div>
                              <div className="flex items-center justify-between text-[11px] text-slate-400 mb-1 uppercase tracking-wider">
                                <span>Strategy Weights</span>
                              </div>
                              <div className="flex h-1.5 rounded-full overflow-hidden bg-slate-900/60 ring-1 ring-slate-700/50">
                                <div
                                  className="bg-emerald-500 transition-all"
                                  style={{ width: `${alt.strategy_math.weights.cost * 100}%` }}
                                  title={`Cost ${(alt.strategy_math.weights.cost * 100).toFixed(0)}%`}
                                />
                                <div
                                  className="bg-sky-500 transition-all"
                                  style={{ width: `${alt.strategy_math.weights.time * 100}%` }}
                                  title={`Time ${(alt.strategy_math.weights.time * 100).toFixed(0)}%`}
                                />
                                <div
                                  className="bg-purple-500 transition-all"
                                  style={{ width: `${alt.strategy_math.weights.carbon * 100}%` }}
                                  title={`Carbon ${(alt.strategy_math.weights.carbon * 100).toFixed(0)}%`}
                                />
                              </div>
                              <div className="flex items-center justify-between text-[11px] mt-1 font-mono">
                                <span className="text-emerald-400">
                                  {(alt.strategy_math.weights.cost * 100).toFixed(0)}% cost
                                </span>
                                <span className="text-sky-400">
                                  {(alt.strategy_math.weights.time * 100).toFixed(0)}% time
                                </span>
                                <span className="text-purple-400">
                                  {(alt.strategy_math.weights.carbon * 100).toFixed(0)}% carbon
                                </span>
                              </div>
                            </div>

                            {/* Cost breakdown table */}
                            {alt.cost_breakdown && (
                              <div className="space-y-1 text-[11px] border-t border-slate-700/40 pt-2">
                                <div className="flex justify-between text-slate-400">
                                  <span>Component cost</span>
                                  <span className="font-mono text-slate-200">${alt.cost_breakdown.component_cost.toFixed(2)}</span>
                                </div>
                                <div className="flex justify-between text-slate-400">
                                  <span>Transport cost</span>
                                  <span className="font-mono text-slate-200">${alt.cost_breakdown.transport_cost.toFixed(2)}</span>
                                </div>
                                <div
                                  className="flex justify-between text-slate-400 cursor-help"
                                  title="Inventory carrying cost over the lead time, at a 25%/yr electronics holding rate (Gartner IT Supply Chain Benchmarks 2022). Holding $ = component value × 25% × (lead-time days ÷ 365)."
                                >
                                  <span>Holding cost</span>
                                  <span className="font-mono text-slate-200">${alt.cost_breakdown.holding_cost.toFixed(2)}</span>
                                </div>
                                {/*
                                  Sum the ROUNDED rows rather than printing the unrounded
                                  backend total: 166.97 + 206.25 + 0.79 renders as 374.01
                                  while `total` printed 374.02, so the one block a reviewer
                                  actually adds up did not add up.
                                */}
                                <div className="flex justify-between border-t border-slate-700/40 pt-1 text-slate-200">
                                  <span className="font-semibold">Total</span>
                                  <span className="font-mono font-semibold">
                                    ${(
                                      Number(alt.cost_breakdown.component_cost.toFixed(2)) +
                                      Number(alt.cost_breakdown.transport_cost.toFixed(2)) +
                                      Number(alt.cost_breakdown.holding_cost.toFixed(2))
                                    ).toFixed(2)}
                                  </span>
                                </div>
                                <div className="flex justify-between text-slate-400 pt-1 text-[11px]">
                                  <span>Weighted objective</span>
                                  <span className="font-mono">{alt.strategy_math.weighted_total.toFixed(4)}</span>
                                </div>
                              </div>
                            )}

                            {/* Citations */}
                            <div
                              className="text-xs text-slate-400 leading-relaxed border-t border-slate-700/40 pt-2"
                              data-testid="citations"
                            >
                              Sources: {alt.strategy_math.citations.join(' · ')}
                            </div>
                          </div>
                        </details>
                      )}

                      {/* Cross-Dock consolidation panel (direct vs the charged figure).
                          One visual idiom used to cover both outcomes: the direct price
                          struck through and the hub price highlighted, under a pill that
                          prefixed a literal minus to an already-signed number. On the
                          RECOMMENDED card that rendered "−-6.6%" over a $340 direct cost
                          struck out in favour of a $363 hub cost — the language of a
                          saving on a plan that is $23 dearer. Now:
                            • the sign comes from the value, never from a glyph;
                            • the CHEAPER of the two figures gets the favoured treatment,
                              and only a genuinely superseded, dearer direct price is
                              struck through;
                            • the backend's own `rationale` is rendered rather than
                              dropped — it is the sentence that says, in the served text,
                              "it does NOT save money ... The charged figure is the
                              consolidated one". No wording here is invented. */}
                      {cd && cd.enabled && cd.hub_name && (
                        <div
                          className="mt-3 p-2.5 rounded-lg bg-amber-500/5 border border-amber-500/20"
                          data-testid="cross-dock-line"
                        >
                          <div className="flex items-center justify-between gap-2 mb-2">
                            <div className="text-[11px] text-amber-400/80 uppercase tracking-wider font-semibold">
                              Cross-Dock Consolidation
                            </div>
                            <div
                              data-testid="cross-dock-delta"
                              className={`text-[11px] font-mono font-bold px-1.5 py-0.5 rounded whitespace-nowrap ${
                                cdSaves
                                  ? 'text-emerald-300 bg-emerald-500/10'
                                  : cdCostsMore
                                    ? 'text-amber-200 bg-amber-500/20'
                                    : 'text-slate-300 bg-slate-700/40'
                              }`}
                            >
                              {cdSaves
                                ? `−${cd.savings_vs_direct_pct.toFixed(1)}% cost`
                                : cdCostsMore
                                  ? `+${Math.abs(cd.savings_vs_direct_pct).toFixed(1)}% cost`
                                  : 'same cost'}
                            </div>
                          </div>

                          <div className="flex items-stretch gap-1.5">
                            <div
                              className={`flex-1 text-center px-1.5 py-1 rounded border ${
                                cdCostsMore
                                  ? 'bg-emerald-500/10 border-emerald-500/40'
                                  : 'bg-slate-900/60 border-slate-700/60'
                              }`}
                            >
                              <div className={`text-[11px] uppercase tracking-wider ${cdCostsMore ? 'text-emerald-300/90' : 'text-slate-400'}`}>
                                Direct
                              </div>
                              <div
                                className={`text-[11px] font-mono font-semibold ${
                                  cdCostsMore
                                    ? 'text-emerald-200'
                                    : 'text-slate-400 line-through decoration-slate-600'
                                }`}
                              >
                                ${cd.direct_cost_usd.toFixed(0)}
                              </div>
                            </div>

                            <svg className="w-3 h-3 text-slate-400 shrink-0 self-center" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M13 5l7 7-7 7M5 12h15" />
                            </svg>

                            <div
                              className={`flex-1 text-center px-1.5 py-1 rounded border ${
                                cdCostsMore
                                  ? 'bg-amber-500/10 border-amber-500/40'
                                  : 'bg-emerald-500/10 border-emerald-500/40'
                              }`}
                            >
                              <div className={`text-[11px] uppercase tracking-wider ${cdCostsMore ? 'text-amber-300/90' : 'text-emerald-300/90'}`}>
                                Via Hub
                              </div>
                              <div className={`text-[11px] font-mono font-semibold ${cdCostsMore ? 'text-amber-200' : 'text-emerald-200'}`}>
                                ${cd.consolidated_cost_usd.toFixed(0)}
                              </div>
                            </div>
                          </div>

                          {/* Was `truncate` — a single clipped line for the one thing
                              that explains why the charged route differs from the
                              itemised legs above. This dataset has hub names longer than
                              any placeholder ("Weyland Electronics Group Pte. Ltd."), so
                              it wraps onto a second centered line instead of clipping;
                              `title` is a hover bonus, not the only way to read it. */}
                          <div
                            className="text-xs text-slate-400 mt-1.5 text-center break-words"
                            title={`${cd.hub_name} — ${cd.hub_city}, ${cd.hub_state}`}
                          >
                            {cd.hub_name} — {cd.hub_city}, {cd.hub_state}
                          </div>

                          {/* The served explanation, at 12px — larger than the 11px
                              figures it qualifies. It is the only place the page states
                              which of the two prices is actually charged, and the
                              backend has always sent it. */}
                          {cd.rationale && (
                            <p
                              className="text-xs leading-5 text-slate-300 mt-1.5"
                              data-testid="cross-dock-rationale"
                            >
                              {cd.rationale}
                            </p>
                          )}
                        </div>
                      )}

                      {/* ML Supply Risk — factory lead-time signal on this sourcing plan.
                          NOTE: with `standard_pack` populated on only ~7% of live offer
                          rows, "declined" is the common case in this dataset today, not
                          an edge case — so it gets a fully legible state, not a muted
                          one-liner, and always shows the model's own `rationale`. */}
                      {alt.supply_risk && (
                        <div
                          className={`mt-3 p-2.5 rounded-lg border ${
                            alt.supply_risk.model_available
                              ? 'bg-sky-500/5 border-sky-500/20'
                              : 'bg-slate-900/40 border-slate-700/50'
                          }`}
                          data-testid="supply-risk-panel"
                        >
                          <div className={`flex items-center justify-between gap-2 pb-1.5 mb-2 border-b ${
                            alt.supply_risk.model_available ? 'border-sky-500/20' : 'border-slate-700/60'
                          }`}>
                            <div className={`flex items-center gap-1.5 text-[11px] font-semibold tracking-tight ${
                              alt.supply_risk.model_available ? 'text-sky-300' : 'text-slate-400'
                            }`}>
                              <Cpu className="w-3 h-3" />
                              ML Supply Risk
                            </div>
                            {alt.supply_risk.model_available && alt.supply_risk.zero_buffer_lines > 0 ? (
                              <div className="text-[11px] font-mono font-bold text-amber-300 bg-amber-500/10 px-1.5 py-0.5 rounded">
                                {alt.supply_risk.zero_buffer_lines} zero-buffer
                              </div>
                            ) : alt.supply_risk.model_available ? (
                              /* The buffer the model added on top of the route ETA. Derived
                                 from the two numbers already shown below — not a new claim. */
                              <div
                                className="text-[11px] font-mono font-semibold text-sky-300/90 bg-sky-500/10 px-1.5 py-0.5 rounded cursor-help"
                                title={alt.supply_risk.rationale}
                              >
                                +{Math.max(0, alt.supply_risk.risk_adjusted_eta_days - alt.supply_risk.route_eta_days).toFixed(1)}d buffer
                              </div>
                            ) : (
                              /* Honest "no number" rather than a blank slot: the reason is
                                 spelled out in full in the panel body underneath. */
                              <div className="text-[11px] font-mono text-slate-400" title="The model declined to score this plan — see the reason below.">
                                —
                              </div>
                            )}
                          </div>

                          {alt.supply_risk.model_available ? (
                            <>
                              <div className="flex items-center gap-1.5">
                                <div className="flex-1 text-center px-1.5 py-1 rounded bg-slate-900/60 border border-slate-700/60">
                                  <div className="text-[11px] text-slate-400 uppercase tracking-wider">Route ETA</div>
                                  <div className="text-[11px] font-mono font-semibold text-slate-300">
                                    {alt.supply_risk.route_eta_days.toFixed(1)}d
                                  </div>
                                </div>
                                <svg className="w-3 h-3 text-sky-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M13 5l7 7-7 7M5 12h15" />
                                </svg>
                                <div
                                  className="flex-1 text-center px-1.5 py-1 rounded bg-sky-500/10 border border-sky-500/40 cursor-help"
                                  title={alt.supply_risk.rationale}
                                >
                                  <div className="text-[11px] text-sky-400/90 uppercase tracking-wider">Risk-Adjusted</div>
                                  <div className="text-[11px] font-mono font-semibold text-sky-200">
                                    {alt.supply_risk.risk_adjusted_eta_days.toFixed(1)}d
                                  </div>
                                </div>
                              </div>
                              {/* "Longest factory lead: STM32F103C8T6 — 286d" sitting beside
                                  "+0.0d buffer" read as a bug. It is not: the buffer is
                                  `zero_buffer_max_days`, raised only when a line takes 100% of
                                  its distributor's reported shelf (backend/app/optimization/
                                  solve.py:289-295), while the factory lead time is a separate
                                  unconditional max over every scored line. A long factory lead
                                  costs this shipment nothing while the distributor still has
                                  stock left over. Rather than paraphrase that, the panel now
                                  renders the model's served `rationale`, which states it in the
                                  backend's own words and already carries the driver MPN, the
                                  lead time and the scored/declined counts (solve.py:296-322) —
                                  so the hand-built summary line it replaces said strictly less.
                                  It was previously reachable only by hovering a tooltip. */}
                              {alt.supply_risk.rationale && (
                                <div
                                  className="text-xs text-slate-300 mt-1.5 text-center leading-relaxed break-words"
                                  data-testid="supply-risk-rationale"
                                >
                                  {alt.supply_risk.rationale}
                                </div>
                              )}
                            </>
                          ) : (
                            <div className="text-[11px] text-slate-400 text-center py-1 leading-relaxed">
                              {alt.supply_risk.rationale
                                || `Model declined to score this plan${alt.supply_risk.declined_reason ? ` — ${alt.supply_risk.declined_reason}` : ''}. `
                                  + `Delivery ETA is route-derived (handling + transit); no factory-lead-time risk is claimed.`}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Selected route detail section */}
            {selected && (
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
                {/* Route stops */}
                <div className="lg:col-span-2 bg-slate-800 border border-slate-700 rounded-xl p-5">
                  <div className="flex items-center justify-between mb-4">
                    <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                      <MapPin className="w-4 h-4 text-blue-400" />
                      {selected.label} Route — {routeView.supplierStops.length} Supplier
                      {routeView.supplierStops.length === 1 ? ' Stop' : ' Stops'}
                    </h3>
                    <button
                      onClick={() => navigate('/map')}
                      className="flex items-center gap-1.5 min-h-[44px] text-xs text-blue-400 hover:text-blue-300 transition-colors"
                    >
                      View on Map <ArrowRight className="w-3 h-3" />
                    </button>
                  </div>
                  {/*
                    Under a cross-dock plan the legs below are the PRE-consolidation pickup
                    legs — they sum to more distance, cost and CO2e than the plan is actually
                    charged (2,728 km / $405.77 vs 1,902 km / $362.51 on the balanced plan),
                    because the charged figures describe the consolidated route through the
                    hub. Without this line the panel silently contradicts the card above it.
                    The backend has always sent the explanation; it was simply never rendered.
                  */}
                  {selected.route_legs_note && selected.transport_cost_basis === 'cross_dock_consolidated' && (
                    <p className="text-xs text-slate-400 leading-relaxed mb-4 border-l-2 border-amber-500/50 pl-3">
                      {selected.route_legs_note}
                    </p>
                  )}

                  <div className="space-y-1">
                    {/* Depot start */}
                    <div className="flex items-center gap-3 py-2 px-3 rounded-lg bg-blue-500/10 border border-blue-500/20">
                      <div className="w-6 h-6 rounded-full bg-blue-600 text-white text-[11px] flex items-center justify-center shrink-0 font-bold">
                        HQ
                      </div>
                      <span className="text-xs text-blue-300 font-medium">Start — Your Factory (Depot)</span>
                    </div>

                    {/* Supplier stops only. The depot's own leg is rendered as the
                        closing row below, so it is never numbered as a stop. */}
                    {routeView.supplierStops.map((stop, i) => (
                      <div key={`${stop.distributor_id}-${stop.order}`} className="flex items-start gap-3 py-2 px-3 rounded-lg hover:bg-slate-700/30 transition-colors">
                        <div className="flex flex-col items-center">
                          <div className="w-6 h-6 rounded-full bg-slate-700 text-white text-[11px] flex items-center justify-center shrink-0 font-bold border border-slate-600">
                            {i + 1}
                          </div>
                          {i < routeView.supplierStops.length - 1 && (
                            <div className="w-px h-4 bg-slate-700 mt-1" />
                          )}
                        </div>
                        <div className="flex-1 min-w-0">
                          {/* The supplier's identity is the entire point of this row —
                              it used to truncate to two words ("Component ...",
                              "Tactical IC ..."). flex-wrap + break-words lets a long
                              name wrap onto its own line instead of being clipped; the
                              metrics drop to a second row on narrow screens rather than
                              stealing the name's space. */}
                          <div className="flex items-start justify-between flex-wrap gap-y-1">
                            <div className="min-w-0 break-words pr-2">
                              <span className="text-sm text-white font-medium">{stop.distributor_name}</span>
                              {/* Render the address only when there is one — a stop with no
                                  city and no state used to print a bare orphan comma. */}
                              {(formatStopLocation(stop) || (stop.country && stop.country !== 'USA')) && (
                                <span className="text-xs text-slate-400 ml-2">
                                  {formatStopLocation(stop)}
                                  {stop.country && stop.country !== 'USA' && (
                                    <span className="text-slate-400">{formatStopLocation(stop) ? ' ' : ''}({stop.country})</span>
                                  )}
                                </span>
                              )}
                            </div>
                            <div className="flex items-center gap-3 text-xs text-slate-400 shrink-0 ml-3">
                              <span>{stop.distance_km.toFixed(0)} km</span>
                              <span className="text-blue-300">${stop.leg_cost_usd.toFixed(0)}</span>
                              <span className="text-emerald-300">{stop.leg_co2e_kg.toFixed(2)} kg</span>
                            </div>
                          </div>
                          <div className="flex flex-wrap gap-1 mt-1">
                            {stop.components.map((c, j) => (
                              <span key={j} className="text-[11px] bg-slate-700 text-slate-300 px-1.5 py-0.5 rounded">
                                {c}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    ))}

                    {/* Return leg — the depot is a leg of the tour, not a stop on it.
                        When the backend priced the leg, show what it actually cost. */}
                    <div className="flex items-center justify-between gap-3 py-2 px-3 rounded-lg bg-slate-700/20 border border-slate-700/50">
                      <div className="flex items-center gap-3 min-w-0">
                        <div className="w-6 h-6 rounded-full bg-slate-700 text-slate-400 text-[11px] flex items-center justify-center shrink-0 font-bold border border-slate-600">
                          <ArrowRight className="w-3 h-3" />
                        </div>
                        <span className="text-xs text-slate-400">Return to Your Factory (Depot)</span>
                      </div>
                      {routeView.returnLeg && (
                        <div className="flex items-center gap-3 text-xs text-slate-400 shrink-0">
                          <span>{routeView.returnLeg.distance_km.toFixed(0)} km</span>
                          <span className="text-blue-300">${routeView.returnLeg.leg_cost_usd.toFixed(0)}</span>
                          <span className="text-emerald-300">{routeView.returnLeg.leg_co2e_kg.toFixed(2)} kg</span>
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                {/* Side panel: Monte Carlo + actions */}
                <div className="space-y-4">
                  {/* ETA distribution */}
                  <div className="bg-slate-800 border border-slate-700 rounded-xl p-4">
                    <h3 className="text-sm font-semibold text-white mb-1 flex items-center gap-2">
                      <Clock className="w-4 h-4 text-blue-400" />
                      Delivery Time Distribution
                    </h3>
                    {/* Caption reports what is actually plotted — the sample count
                        comes off the payload, never a hardcoded 1000. */}
                    <p className="text-xs text-slate-400 mb-3" data-testid="mc-sample-caption">
                      {distribution && distribution.count > 0
                        ? `Histogram of the ${distribution.count.toLocaleString()} Monte Carlo ETA sample${distribution.count === 1 ? '' : 's'} returned for this route`
                        : 'No Monte Carlo samples returned for this route'}
                    </p>

                    <div className="grid grid-cols-3 gap-2 mb-3">
                      {[
                        { label: 'Best (P10)', value: selected.eta_p10, color: 'text-green-400' },
                        { label: 'Median (P50)', value: selected.eta_p50, color: 'text-blue-400' },
                        { label: 'Worst (P90)', value: selected.eta_p90, color: 'text-red-400' },
                      ].map(({ label, value, color }) => (
                        <div key={label} className="bg-slate-700/40 rounded-lg p-2 text-center">
                          <div className="text-[11px] text-slate-400">{label}</div>
                          <div className={`text-lg font-bold ${color}`}>{value.toFixed(1)}d</div>
                        </div>
                      ))}
                    </div>

                    {distribution && distribution.count > 0 ? (
                      <>
                        {/* The overlay and the bars share one linear x-mapping: bin 0
                            starts at the domain min and the last bin ends at the domain
                            max, and the domain was widened to include every marker. */}
                        <div className="relative" data-testid="mc-histogram">
                          <ResponsiveContainer width="100%" height={120}>
                            <BarChart data={distribution.bars} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
                              <CartesianGrid strokeDasharray="3 3" stroke="#334155" vertical={false} />
                              <XAxis
                                dataKey="bin"
                                height={18}
                                interval={Math.ceil(HIST_BINS / 5)}
                                tick={{ fill: '#94a3b8', fontSize: 11 }}
                                tickFormatter={(b) => {
                                  const bar = distribution.bars[Number(b)];
                                  return bar ? `${bar.center.toFixed(1)}d` : '';
                                }}
                              />
                              <YAxis tick={false} axisLine={false} width={0} />
                              <Tooltip
                                contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: 6, fontSize: 11 }}
                                itemStyle={{ color: '#e2e8f0' }}
                                formatter={(v) => [Number(v), 'Samples']}
                                labelFormatter={(b) => {
                                  const bar = distribution.bars[Number(b)];
                                  return bar ? `${bar.from.toFixed(2)} – ${bar.to.toFixed(2)} days` : '';
                                }}
                              />
                              <Bar dataKey="count" radius={[2, 2, 0, 0]} fill="#3b82f6" />
                            </BarChart>
                          </ResponsiveContainer>

                          {/* Percentile markers, guaranteed inside the plotted range */}
                          <div className="pointer-events-none absolute inset-x-0 top-2" style={{ bottom: 16 }}>
                            {distribution.markers.map((m) => (
                              <div key={m.key} className="absolute top-0 bottom-0" style={{ left: `${m.pct}%` }}>
                                <div
                                  className="w-px h-full"
                                  style={{ backgroundColor: m.color, opacity: m.wasClamped ? 0.6 : 0.95 }}
                                />
                                <span
                                  className="absolute -top-3 text-[11px] font-mono whitespace-nowrap"
                                  style={{
                                    color: m.color,
                                    left: m.pct > 60 ? 'auto' : 3,
                                    right: m.pct > 60 ? 3 : 'auto',
                                  }}
                                >
                                  {m.label}{m.wasClamped ? '*' : ''}
                                </span>
                              </div>
                            ))}
                          </div>
                        </div>

                        <div className="flex items-center justify-center gap-3 mt-1.5">
                          {distribution.markers.map((m) => (
                            <span key={m.key} className="flex items-center gap-1 text-[11px] text-slate-400">
                              <span className="w-2 h-px" style={{ backgroundColor: m.color }} />
                              {m.label} {m.value.toFixed(1)}d
                            </span>
                          ))}
                        </div>

                        {/* Honesty rail: the markers come from the full simulation, the
                            bars from whatever sample array the API sent. If those two
                            disagree, say so rather than quietly drawing a mismatch. */}
                        {distribution.outsideSupport.length > 0 && (
                          <div
                            className="mt-2 flex items-start gap-1.5 text-[11px] text-amber-300/90 bg-amber-500/5 border border-amber-500/20 rounded-lg p-2 leading-relaxed"
                            data-testid="mc-marker-warning"
                          >
                            <AlertTriangle className="w-3 h-3 shrink-0 mt-px" />
                            <span>
                              {distribution.outsideSupport.map((m) => m.label).join(', ')}{' '}
                              {distribution.outsideSupport.length === 1 ? 'falls' : 'fall'} outside the{' '}
                              {distribution.sampleMin.toFixed(1)}–{distribution.sampleMax.toFixed(1)}d span of the{' '}
                              {distribution.count.toLocaleString()} samples returned, so the returned array is a subset of
                              the run the percentiles were computed from — not the full simulation. The x-range was widened
                              to keep every marker visible.
                            </span>
                          </div>
                        )}
                        {distribution.clamped.length > 0 && (
                          <div className="mt-1 text-[11px] text-amber-300/90" data-testid="mc-marker-clamped">
                            * {distribution.clamped.map((m) => `${m.label} (${m.value.toFixed(1)}d)`).join(', ')} could not be
                            placed and {distribution.clamped.length === 1 ? 'is' : 'are'} pinned to the chart edge.
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="text-[11px] text-slate-400 text-center py-6 border border-dashed border-slate-700 rounded-lg">
                        This route came back without a Monte Carlo sample array, so no distribution can be drawn. The
                        P10/P50/P90 above are the API&apos;s own percentiles.
                      </div>
                    )}
                  </div>

                  {/* Comparison summary */}
                  <div className="bg-slate-800 border border-slate-700 rounded-xl p-4">
                    <h3 className="text-sm font-semibold text-white mb-3">Strategy Comparison</h3>
                    <table className="w-full text-[11px]">
                      <thead>
                        <tr className="text-slate-400">
                          <th className="text-left pb-2 font-medium">Strategy</th>
                          <th className="text-right pb-2 font-medium">Cost</th>
                          <th className="text-right pb-2 font-medium">ETA</th>
                          <th className="text-right pb-2 font-medium">CO2 (kg)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {alternatives.map((alt, altIdx) => {
                          // Highlight "best" from the tie-aware ranks so a shared first
                          // place highlights every strategy that actually shares it.
                          const best = {
                            cost: (comparison?.ranks.cost[altIdx].rank ?? alt.cost_rank) === 1,
                            speed: (comparison?.ranks.speed[altIdx].rank ?? alt.speed_rank) === 1,
                            carbon: (comparison?.ranks.carbon[altIdx].rank ?? alt.carbon_rank) === 1,
                          };
                          return (
                            <tr
                              key={alt.id}
                              className={`border-t border-slate-700/50 cursor-pointer hover:bg-slate-700/20 ${
                                alt.id === selectedId ? 'bg-blue-500/5' : ''
                              }`}
                              onClick={() => setSelectedId(alt.id)}
                            >
                              <td className="py-1.5 text-slate-300 font-medium">
                                {alt.id === selectedId && <span className="text-blue-400 mr-1">●</span>}
                                {alt.label}
                              </td>
                              <td className={`py-1.5 text-right ${best.cost ? 'text-green-400 font-bold' : 'text-slate-400'}`}>
                                ${alt.total_cost_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                              </td>
                              <td className={`py-1.5 text-right ${best.speed ? 'text-green-400 font-bold' : 'text-slate-400'}`}>
                                {alt.eta_p50.toFixed(1)}d
                              </td>
                              <td className={`py-1.5 text-right ${best.carbon ? 'text-green-400 font-bold' : 'text-slate-400'}`}>
                                {alt.total_co2e_kg.toFixed(1)}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>

                  {/* Actions */}
                  <div className="space-y-2">
                    <button
                      onClick={() => navigate('/map')}
                      className="w-full min-h-[44px] bg-blue-600 hover:bg-blue-500 text-white py-2.5 rounded-lg font-semibold text-sm transition-colors flex items-center justify-center gap-2"
                    >
                      <MapPin className="w-4 h-4" /> View Route on Map
                    </button>
                    {/* C6: this API has no order/PO endpoint. The button says what it
                        does — accept the plan and empty the cart — and nothing more. */}
                    <button
                      onClick={() => { void acceptPlan(); }}
                      disabled={finishing}
                      className="w-full min-h-[44px] bg-green-600 hover:bg-green-500 disabled:bg-green-600/40 disabled:cursor-not-allowed text-white py-2.5 rounded-lg font-semibold text-sm transition-colors flex items-center justify-center gap-2"
                      data-testid="accept-plan-button"
                    >
                      <Check className="w-4 h-4" /> {finishing ? 'Clearing cart…' : 'Accept Plan & Clear Cart'}
                    </button>
                    <p className="text-xs text-slate-400 text-center leading-relaxed">
                      Records nothing with a supplier — no purchase order is raised. This clears your BOM and returns you
                      to the dashboard; export or screenshot the plan first if you need it.
                    </p>
                    {finishError && (
                      <div className="text-[11px] text-red-300 bg-red-900/20 border border-red-700/40 rounded-lg p-2" data-testid="accept-plan-error">
                        {finishError}{' '}
                        <button onClick={() => { void acceptPlan(); }} className="underline hover:text-white">Try again</button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
