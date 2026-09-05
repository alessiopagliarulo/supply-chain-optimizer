/**
 * /newsvendor — the demand distribution turned into an order quantity.
 * ====================================================================
 *
 * Three things have to land here, in this order:
 *
 *   1. THE DECISION.  q*, the critical fractile tau behind it, and WHY tau is
 *      that number (the cost asymmetry), with the naive rules beside it.
 *   2. THE EVIDENCE.  Every baseline on the held-out panel with paired
 *      bootstrap CIs. A CI that excludes zero IS the claim, so it is drawn as
 *      an interval against a zero line, not buried in five decimal places.
 *   3. THE ARGUMENT.  Give all six forecasting methods the same newsvendor
 *      rule and `zero` — which wins MASE outright on this panel — produces the
 *      WORST decision cost of the six. That is the proper-scoring-rules
 *      argument in dollars instead of in prose, and it is the reason the
 *      forecasting track in this repo is load-bearing rather than decorative.
 *
 * Every number on this page comes from a live response of
 * `/newsvendor/assumptions`, `/newsvendor/decision` or `/newsvendor/evaluation`.
 * Nothing is hardcoded from the docs — including the failures, which the API
 * reports on itself: switch the shortage mode to `line_down`, or the review
 * period to 3 months, and the ship gate below turns red on its own.
 *
 * HONESTY OBLIGATIONS, all discharged on the page rather than in this comment:
 *   - The Monash car-parts panel carries NO PRICES. Every dollar is per $1.00
 *     of unit price and scales linearly; tau does not depend on price at all.
 *   - The panel is a labelled STAND-IN for electronic components, not a demand
 *     forecast for anything in this catalogue.
 *   - `line_down` (tau = 0.9931) FAILS the ship gate: 45 monthly observations
 *     cannot resolve a 99.3rd percentile.
 *   - At a 3-month review period the policy LOSES to the point forecast.
 *   - The policy TIES the moment-based baselines on ~58% of series. The mean
 *     saving is real; it is not a win on most SKUs.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Info,
  Loader2,
  Scale,
  XCircle,
} from 'lucide-react';
import {
  FORECAST_METHODS,
  SHORTAGE_MODES,
  isTimeoutError,
  newsvendorAPI,
  type AssumptionsResponse,
  type DecisionResponse,
  type EvaluationResponse,
  type ForecastMethod,
  type ShortageMode,
} from '../services/newsvendor';
// One definition, shared with the Benchmark page's diversification frontier —
// two pages drawing bootstrap intervals must draw them identically.
import CiStrip from '../components/CiStrip';

// ── Formatters ───────────────────────────────────────────────────────────────
// Local by convention in this repo. Every one falls back to an em dash rather
// than rendering a fake 0.

const fmtNum = (v: number | null | undefined, dp: number): string =>
  v == null || !Number.isFinite(v) ? '—' : v.toFixed(dp);

const fmtUsd = (v: number | null | undefined, dp = 4): string =>
  v == null || !Number.isFinite(v) ? '—' : `$${v.toFixed(dp)}`;

/** Order quantities are integers when the policy produced them, floats when a
 *  closed form did. Render each as what it actually is. */
const fmtQty = (v: number | null | undefined): string => {
  if (v == null || !Number.isFinite(v)) return '—';
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
};

/** Only ever applied to genuine proportions (win rates, service levels). */
const fmtRate = (v: number | null | undefined, dp = 0): string =>
  v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(dp)}%`;

const fmtInt = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(v) ? '—' : v.toLocaleString('en-US');

const errText = (err: unknown): string => {
  const e = err as { response?: { data?: { detail?: unknown } }; message?: string };
  const detail = e?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (isTimeoutError(err)) {
    return 'The request timed out. The backend runs on a free tier that sleeps after ~15 minutes idle and takes up to ~2 minutes to wake — try again.';
  }
  return e?.message ?? 'Unknown error';
};

// ── Constants ────────────────────────────────────────────────────────────────

/** The order the API returns predictive quantiles in, with their levels. */
const QUANTILE_KEYS: Array<{ key: string; level: number }> = [
  { key: 'q01', level: 1 },
  { key: 'q05', level: 5 },
  { key: 'q10', level: 10 },
  { key: 'q25', level: 25 },
  { key: 'q50', level: 50 },
  { key: 'q75', level: 75 },
  { key: 'q90', level: 90 },
  { key: 'q95', level: 95 },
  { key: 'q99', level: 99 },
];

/**
 * A handful of panel series that behave differently, so the reader can see the
 * rule respond to the shape of the demand rather than to a single example.
 * The descriptions are properties of the committed panel
 * (`backend/seeds/data/car_parts_monthly.npz`); the numbers on screen all come
 * back from the API, which reads that same file.
 */
const PRESET_SERIES: Array<{ id: string; note: string }> = [
  { id: 'T2674', note: 'busiest series in the panel' },
  { id: 'T2672', note: 'lumpy — long silences, then large orders' },
  { id: 'T2661', note: 'bursty, order sizes clustered at 4 and 8' },
  { id: 'T2649', note: 'almost always 0 or exactly 5' },
  { id: 'T42', note: 'near-silent slow mover' },
];

/**
 * Bounded by the panel's held-out horizon, not by taste. The evaluation splits a 6-month
 * horizon into floor(horizon / L) non-overlapping blocks, so L > 6 has no evaluation to
 * report — and the API used to answer an unhandled 500 for the "12 months" this list once
 * offered. Every value here is published in `docs/newsvendor.json` and served instantly.
 */
const REVIEW_PERIODS = [1, 2, 3, 4, 5, 6];

/** Human labels for the policy keys the evaluation returns. */
const POLICY_LABELS: Record<string, string> = {
  newsvendor_fractile: 'Newsvendor fractile',
  point_forecast: 'Order the point forecast',
  naive_last: "Order last period's demand",
  safety_multiple_2x: 'Fixed safety multiple, 2 × mean',
  normal_safety_stock: 'Normal safety stock, μ + z σ',
  scarf_minmax: 'Scarf (1958) min-max',
  order_nothing: 'Order nothing, ever',
};

const METHOD_NOTES: Record<string, string> = {
  tsb: 'Teunter–Syntetos–Babai',
  sba: 'Syntetos–Boylan approximation',
  croston: "Croston's method",
  climatology: 'in-sample empirical distribution',
  naive_last: 'last observed value',
  zero: 'forecast nothing, every period',
};

const COLOR = {
  indigo: '#6366f1',
  indigoLight: '#a5b4fc',
  slate: '#64748b',
  amber: '#f59e0b',
  emerald: '#10b981',
  red: '#ef4444',
  axis: '#94a3b8',
  grid: '#334155',
};

const TOOLTIP_STYLE = {
  backgroundColor: '#0f172a',
  border: '1px solid #475569',
  borderRadius: '8px',
  padding: '12px',
  fontSize: '12px',
} as const;

// ── Small presentational pieces ──────────────────────────────────────────────

function StatTile({
  label,
  value,
  sub,
  accent = 'border-slate-700',
  valueClass = 'text-xl text-white',
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: string;
  /** Carries BOTH the size and the colour — Tailwind cannot resolve two
   *  competing `text-*` size utilities by class order, so a caller that wants a
   *  smaller value must say so here rather than appending a second size. */
  valueClass?: string;
}) {
  return (
    <div className={`bg-slate-900/50 border ${accent} rounded-lg p-4 flex flex-col gap-1`}>
      <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">{label}</span>
      <span className={`font-semibold tabular-nums break-words ${valueClass}`}>{value}</span>
      {sub && <span className="text-xs text-slate-400 leading-relaxed">{sub}</span>}
    </div>
  );
}

/** Explicit key for every chart — no series is identified by colour alone. */
function ChartKey({ items }: { items: Array<{ color: string; label: string; dashed?: boolean }> }) {
  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-2 mt-3">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-2 text-xs text-slate-400">
          <span
            aria-hidden="true"
            className="inline-block w-4 shrink-0"
            style={
              it.dashed
                ? { borderTop: `2px dashed ${it.color}`, height: 0 }
                : { backgroundColor: it.color, height: '10px', borderRadius: '2px' }
            }
          />
          {it.label}
        </li>
      ))}
    </ul>
  );
}

function SectionHeading({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mb-4">
      <span className="text-xs font-semibold uppercase tracking-wider text-indigo-400">{eyebrow}</span>
      <h2 className="text-2xl font-semibold text-slate-200 mt-1">{title}</h2>
      {children && <p className="text-sm text-slate-400 mt-2 max-w-4xl leading-relaxed">{children}</p>}
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">{label}</span>
      {children}
      {hint && <span className="text-xs text-slate-400">{hint}</span>}
    </label>
  );
}

const SELECT_CLASS =
  'min-h-[44px] w-full bg-slate-900 border border-slate-700 text-slate-200 text-sm rounded px-3 py-2 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-indigo-500';

const INPUT_CLASS = SELECT_CLASS;

function Segmented<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: Array<{ value: T; label: string }>;
  value: T;
  onChange: (v: T) => void;
  ariaLabel: string;
}) {
  return (
    <div role="group" aria-label={ariaLabel} className="flex flex-wrap gap-2">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          aria-pressed={value === o.value}
          className={`min-h-[44px] px-3 py-2 rounded border text-sm font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-indigo-500 ${
            value === o.value
              ? 'bg-indigo-600 border-indigo-500 text-white'
              : 'bg-slate-900 border-slate-700 text-slate-300 hover:bg-slate-800'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

type SourceMode = 'series' | 'history';

export default function NewsvendorPage() {
  // Levers the API actually exposes.
  const [unitPriceText, setUnitPriceText] = useState('1.00');
  const [reviewPeriod, setReviewPeriod] = useState(1);
  const [shortageMode, setShortageMode] = useState<ShortageMode>('expedite');
  const [freightText, setFreightText] = useState('0.00');
  const [method, setMethod] = useState<ForecastMethod>('tsb');

  // Which demand the decision is made on.
  const [sourceMode, setSourceMode] = useState<SourceMode>('series');
  const [seriesId, setSeriesId] = useState('T2674');
  const [historyText, setHistoryText] = useState('');

  const [assumptions, setAssumptions] = useState<AssumptionsResponse | null>(null);
  const [decision, setDecision] = useState<DecisionResponse | null>(null);
  const [decisionLoading, setDecisionLoading] = useState(true);
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const [evaluation, setEvaluation] = useState<EvaluationResponse | null>(null);
  const [evalLoading, setEvalLoading] = useState(true);
  const [evalError, setEvalError] = useState<string | null>(null);
  const [evalElapsed, setEvalElapsed] = useState(0);

  const reqId = useRef(0);

  const unitPrice = Number.parseFloat(unitPriceText);
  const freight = Number.parseFloat(freightText);
  const unitPriceOk = Number.isFinite(unitPrice) && unitPrice > 0 && unitPrice <= 1_000_000;
  const freightOk = Number.isFinite(freight) && freight >= 0 && freight <= 10_000;

  const parsedHistory = historyText
    .split(/[\s,;]+/)
    .filter((t) => t.length > 0)
    .map((t) => Number.parseFloat(t));
  const historyValid =
    parsedHistory.length >= 12 &&
    parsedHistory.length <= 600 &&
    parsedHistory.every((v) => Number.isFinite(v) && v >= 0);
  const historyMessage =
    sourceMode !== 'history' || historyText.trim() === ''
      ? null
      : parsedHistory.some((v) => !Number.isFinite(v))
        ? 'Some entries are not numbers.'
        : parsedHistory.some((v) => v < 0)
          ? 'Demand is a count — it cannot be negative.'
          : parsedHistory.length < 12
            ? `${parsedHistory.length} observations — the API needs at least 12.`
            : parsedHistory.length > 600
              ? `${parsedHistory.length} observations — the API accepts at most 600.`
              : `${parsedHistory.length} observations parsed.`;

  const seriesOk = /^T\d{1,4}$/.test(seriesId.trim().toUpperCase());
  const canDecide = unitPriceOk && freightOk && (sourceMode === 'series' ? seriesOk : historyValid);

  // ── Assumptions + decision. Cheap, so they follow the levers directly. ─────
  useEffect(() => {
    if (!canDecide) return;
    const id = ++reqId.current;
    const timer = window.setTimeout(() => {
      setDecisionLoading(true);
      setDecisionError(null);
      const shared = {
        unit_price_usd: unitPrice,
        review_period_months: reviewPeriod,
        shortage_mode: shortageMode,
        expedite_freight_usd_per_unit: freight,
      };
      Promise.all([
        newsvendorAPI.assumptions(shared),
        newsvendorAPI.decision({
          ...shared,
          method,
          ...(sourceMode === 'series'
            ? { series: seriesId.trim().toUpperCase() }
            : { demand_history: parsedHistory }),
        }),
      ])
        .then(([a, d]) => {
          if (id !== reqId.current) return;
          setAssumptions(a.data);
          setDecision(d.data);
        })
        .catch((err) => {
          if (id !== reqId.current) return;
          setDecisionError(errText(err));
          setDecision(null);
        })
        .finally(() => {
          if (id === reqId.current) setDecisionLoading(false);
        });
    }, 400);
    return () => window.clearTimeout(timer);
    // parsedHistory is derived from historyText; depending on the text keeps the
    // array identity out of the dependency list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unitPrice, reviewPeriod, shortageMode, freight, method, sourceMode, seriesId, historyText, canDecide]);

  // ── Evaluation. Expensive, so it is explicit. ─────────────────────────────
  const runEvaluation = useCallback(
    (m: ForecastMethod, L: number, mode: ShortageMode) => {
      setEvalLoading(true);
      setEvalError(null);
      setEvalElapsed(0);
      const started = Date.now();
      const ticker = window.setInterval(
        () => setEvalElapsed(Math.round((Date.now() - started) / 1000)),
        1000
      );
      newsvendorAPI
        .evaluation({ forecast_method: m, review_period_months: L, shortage_mode: mode })
        .then((r) => setEvaluation(r.data))
        .catch((err) => setEvalError(errText(err)))
        .finally(() => {
          window.clearInterval(ticker);
          setEvalLoading(false);
        });
    },
    []
  );

  useEffect(() => {
    runEvaluation('tsb', 1, 'expedite');
  }, [runEvaluation]);

  const evalStale =
    evaluation != null &&
    (evaluation.protocol.forecast_method !== method ||
      evaluation.protocol.review_period_months !== reviewPeriod ||
      evaluation.costs.shortage_mode !== shortageMode);

  // ── Derived view models ───────────────────────────────────────────────────

  const costs = decision?.costs ?? assumptions?.critical_fractile ?? null;

  const quantileData =
    decision != null
      ? QUANTILE_KEYS.map(({ key, level }) => ({
          label: `${level}`,
          units: decision.demand_distribution.quantiles[key] ?? 0,
        }))
      : [];

  const baselineKeys = evaluation
    ? Object.keys(evaluation.paired_vs_newsvendor).sort(
        (a, b) =>
          (evaluation.paired_vs_newsvendor[a]?.mean_difference ?? 0) -
          (evaluation.paired_vs_newsvendor[b]?.mean_difference ?? 0)
      )
    : [];

  let ciMin = 0;
  let ciMax = 0;
  if (evaluation) {
    for (const k of baselineKeys) {
      const c = evaluation.paired_vs_newsvendor[k];
      ciMin = Math.min(ciMin, c.ci95_low);
      ciMax = Math.max(ciMax, c.ci95_high);
    }
    const pad = (ciMax - ciMin) * 0.06 || 0.001;
    ciMin -= pad;
    ciMax += pad;
  }

  const lb = evaluation?.method_leaderboard;
  const maseRank: Record<string, number> = {};
  const costRank: Record<string, number> = {};
  lb?.order_by_mase.forEach((m, i) => (maseRank[m] = i + 1));
  lb?.order_by_decision_cost.forEach((m, i) => (costRank[m] = i + 1));
  const maseWinner = lb?.order_by_mase[0];
  const costWinner = lb?.order_by_decision_cost[0];
  const maseWinnerCost = maseWinner ? lb?.decision_cost_usd_per_sku_period[maseWinner] : undefined;
  const costWinnerCost = costWinner ? lb?.decision_cost_usd_per_sku_period[costWinner] : undefined;
  const excessOfMaseWinner =
    maseWinnerCost != null && costWinnerCost != null && costWinnerCost > 0
      ? maseWinnerCost / costWinnerCost - 1
      : null;

  const maseChart = lb
    ? lb.order_by_mase.map((m, i) => ({
        method: m,
        value: lb.mase_mean[m],
        rank: `#${i + 1}`,
        isMaseWinner: m === maseWinner,
      }))
    : [];
  const costChart = lb
    ? lb.order_by_decision_cost.map((m, i) => ({
        method: m,
        value: lb.decision_cost_usd_per_sku_period[m],
        rank: `#${i + 1}`,
        isMaseWinner: m === maseWinner,
      }))
    : [];

  const priceLabel = unitPriceOk ? `$${unitPrice.toFixed(2)}` : '$1.00';

  return (
    <div className="min-h-full bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 overflow-y-auto h-full">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-8">
        {/* ── Header ───────────────────────────────────────────────────── */}
        <header className="mb-8">
          <span className="text-xs font-semibold uppercase tracking-wider text-indigo-400">
            Inventory decision under asymmetric cost
          </span>
          <h1 className="text-3xl font-semibold text-white mt-1">Newsvendor</h1>
          <p className="text-base text-slate-300 mt-3 max-w-4xl leading-relaxed">
            A forecast is not a decision. This page turns the demand predictive law into the one
            number a planner actually needs — how many units to order — by minimising{' '}
            <span className="text-slate-200 font-mono text-sm">
              C(q) = Cu·E[(D−q)⁺] + Co·E[(q−D)⁺]
            </span>
            . That is convex on the integers, so the optimum is the smallest{' '}
            <span className="font-mono text-sm text-slate-200">q</span> with{' '}
            <span className="font-mono text-sm text-slate-200">F(q) ≥ τ = Cu/(Cu+Co)</span>: a
            lookup in the predictive cdf, not a solver.
          </p>
          <p className="text-sm text-slate-400 mt-3 max-w-4xl leading-relaxed">
            Every figure below is fetched live from{' '}
            <code className="bg-slate-800 px-1 rounded text-slate-300">/newsvendor/assumptions</code>,{' '}
            <code className="bg-slate-800 px-1 rounded text-slate-300">/newsvendor/decision</code> and{' '}
            <code className="bg-slate-800 px-1 rounded text-slate-300">/newsvendor/evaluation</code>{' '}
            — including the two settings where the policy fails its own ship gate.
          </p>
        </header>

        {/* ── Levers ───────────────────────────────────────────────────── */}
        <section className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-8">
          <div className="flex items-start gap-3 mb-4">
            <Scale size={20} className="text-indigo-400 shrink-0 mt-0.5" aria-hidden="true" />
            <div>
              <h2 className="text-lg font-semibold text-slate-200">Move the levers</h2>
              <p className="text-sm text-slate-400 mt-1 max-w-4xl leading-relaxed">
                τ is a monotone function of the cost ratio and of nothing else. These are the same
                query parameters the API exposes, so the trade-off is explorable rather than
                asserted.
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <Field label="Unit price (USD)" hint="Scales every dollar. τ does not move.">
              <input
                type="number"
                min="0.01"
                step="0.01"
                value={unitPriceText}
                onChange={(e) => setUnitPriceText(e.target.value)}
                className={INPUT_CLASS}
                aria-invalid={!unitPriceOk}
              />
            </Field>

            <Field
              label="Review period (months)"
              hint="How long the order must cover. Capped at the 6-month held-out horizon the evidence below is scored on."
            >
              <select
                value={reviewPeriod}
                onChange={(e) => setReviewPeriod(Number(e.target.value))}
                className={SELECT_CLASS}
              >
                {REVIEW_PERIODS.map((m) => (
                  <option key={m} value={m}>
                    {m} {m === 1 ? 'month' : 'months'}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              label="Expedite freight (USD per unit)"
              hint="Variable air uplift. The fixed $150 consignment charge cannot go here."
            >
              <input
                type="number"
                min="0"
                step="0.05"
                value={freightText}
                onChange={(e) => setFreightText(e.target.value)}
                className={INPUT_CLASS}
                aria-invalid={!freightOk}
              />
            </Field>

            <Field label="Forecast method" hint="Which predictive law the rule reads.">
              <select
                value={method}
                onChange={(e) => setMethod(e.target.value as ForecastMethod)}
                className={SELECT_CLASS}
              >
                {FORECAST_METHODS.map((m) => (
                  <option key={m} value={m}>
                    {m} — {METHOD_NOTES[m]}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <div className="mt-4">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-400 block mb-2">
              Shortage mode
            </span>
            <Segmented<ShortageMode>
              ariaLabel="Shortage mode"
              value={shortageMode}
              onChange={setShortageMode}
              options={SHORTAGE_MODES.map((m) => ({
                value: m,
                label: m === 'expedite' ? 'expedite — re-procure at a premium' : 'line_down — sensitivity',
              }))}
            />
            <p className="text-xs text-slate-400 mt-2 max-w-4xl leading-relaxed">
              A spare part that is out of stock is not a lost sale — the demand does not evaporate,
              the unit is expedited. That is why Cu is a <em>fraction</em> of unit price rather than
              a multiple of it. <span className="font-mono">line_down</span> is the single-sourced,
              no-substitute case after Snyder &amp; Daskin (2005), and the API flags it as beyond
              what the data can resolve.
            </p>
          </div>

          {!unitPriceOk && (
            <p className="text-xs text-amber-300 mt-3">
              Unit price must be a number above 0 and at most 1,000,000.
            </p>
          )}
          {!freightOk && (
            <p className="text-xs text-amber-300 mt-1">
              Expedite freight must be a number between 0 and 10,000.
            </p>
          )}
        </section>

        {/* ══════════════════ 1. THE DECISION ══════════════════════════ */}
        <section className="mb-10">
          <SectionHeading eyebrow="1 — The decision" title="How much to order, and why that number">
            The cost asymmetry does all the work. Order one unit too few and you pay the expedite
            premium; order one too many and you pay a single period of carrying charge. Those are
            not the same size, so the mean is the wrong answer and τ is the right one.
          </SectionHeading>

          {/* Cost asymmetry */}
          {costs && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-5">
              <StatTile
                label="Critical fractile τ"
                value={fmtNum(costs.critical_ratio, 4)}
                sub={`Order at the ${(costs.critical_ratio * 100).toFixed(1)}th percentile of the predictive demand law.`}
                accent="border-indigo-500/60"
                valueClass="text-xl text-indigo-300"
              />
              <StatTile
                label="Cost asymmetry Cu / Co"
                value={`${fmtNum(costs.cost_asymmetry, 1)}×`}
                sub="Being one unit short costs this many times more than holding one unit."
              />
              <StatTile
                label="Cu — underage"
                value={`${fmtUsd(costs.underage_usd_per_unit, 4)} / unit`}
                sub={`${fmtNum(costs.shortage_multiple, 2)} × unit price (${costs.shortage_mode})${
                  costs.expedite_freight_usd_per_unit > 0
                    ? ` + ${fmtUsd(costs.expedite_freight_usd_per_unit, 2)} freight`
                    : ''
                }`}
              />
              <StatTile
                label="Co — overage"
                value={`${fmtUsd(costs.overage_usd_per_unit, 4)} / unit`}
                sub={`${fmtRate(costs.holding_rate_annual, 0)} annual holding rate × ${fmtNum(costs.review_period_months, 0)}/12. Gartner 2022 electronics.`}
              />
            </div>
          )}

          {costs?.resolution_warning && (
            <div className="bg-amber-500/15 border border-amber-400/70 rounded-lg px-4 py-3 text-amber-200 flex gap-3 mb-5">
              <AlertTriangle size={20} className="shrink-0 mt-0.5" aria-hidden="true" />
              <div>
                <p className="text-sm font-semibold">The data cannot resolve this fractile</p>
                <p className="text-sm mt-1 leading-relaxed">{costs.resolution_warning}</p>
              </div>
            </div>
          )}

          {/* Demand source */}
          <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5">
            <h3 className="text-base font-semibold text-slate-200 mb-1">The demand history</h3>
            <p className="text-sm text-slate-400 mb-4 max-w-4xl leading-relaxed">
              Pick a series from the committed Monash car-parts panel — real intermittent
              spare-parts demand, used here as a <strong className="text-slate-300">labelled
              stand-in</strong> for electronic components — or paste your own counts.
            </p>

            <div className="mb-4">
              <Segmented<SourceMode>
                ariaLabel="Demand source"
                value={sourceMode}
                onChange={setSourceMode}
                options={[
                  { value: 'series', label: 'Panel series' },
                  { value: 'history', label: 'Paste a history' },
                ]}
              />
            </div>

            {sourceMode === 'series' ? (
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <div className="lg:col-span-2">
                  <span className="text-xs font-semibold uppercase tracking-wider text-slate-400 block mb-2">
                    Presets
                  </span>
                  <div className="flex flex-wrap gap-2">
                    {PRESET_SERIES.map((p) => (
                      <button
                        key={p.id}
                        type="button"
                        onClick={() => setSeriesId(p.id)}
                        aria-pressed={seriesId.trim().toUpperCase() === p.id}
                        className={`min-h-[44px] px-3 py-2 rounded border text-left transition-colors focus:outline-none focus:ring-2 focus:ring-indigo-500 ${
                          seriesId.trim().toUpperCase() === p.id
                            ? 'bg-indigo-600 border-indigo-500 text-white'
                            : 'bg-slate-900 border-slate-700 text-slate-300 hover:bg-slate-800'
                        }`}
                      >
                        <span className="block text-sm font-semibold font-mono">{p.id}</span>
                        <span
                          className={`block text-xs ${
                            seriesId.trim().toUpperCase() === p.id ? 'text-indigo-100' : 'text-slate-400'
                          }`}
                        >
                          {p.note}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
                <Field label="Or any series id" hint="T1 – T2674, 51 monthly observations each.">
                  <input
                    type="text"
                    value={seriesId}
                    onChange={(e) => setSeriesId(e.target.value)}
                    className={`${INPUT_CLASS} font-mono`}
                    aria-invalid={!seriesOk}
                  />
                </Field>
              </div>
            ) : (
              <div>
                <Field
                  label="Demand per period, oldest first"
                  hint="12 to 600 non-negative counts, separated by commas or spaces."
                >
                  <textarea
                    value={historyText}
                    onChange={(e) => setHistoryText(e.target.value)}
                    rows={4}
                    placeholder="0, 0, 2, 0, 1, 0, 0, 0, 3, 0, 0, 1, 0, 4, 0, 0, 1, 0"
                    className={`${SELECT_CLASS} font-mono resize-y`}
                  />
                </Field>
                {historyMessage && (
                  <p className={`text-xs mt-2 ${historyValid ? 'text-slate-400' : 'text-amber-300'}`}>
                    {historyMessage}
                  </p>
                )}
              </div>
            )}
          </div>

          {/* The answer */}
          {decisionError && (
            <div className="bg-red-500/20 border border-red-400 rounded-lg px-4 py-3 text-red-300 flex gap-3 mb-5">
              <AlertTriangle size={20} className="shrink-0 mt-0.5" aria-hidden="true" />
              <div className="flex-1">
                <p className="font-semibold">The decision could not be computed</p>
                <p className="text-sm mt-1 leading-relaxed">{decisionError}</p>
              </div>
            </div>
          )}

          {decisionLoading && !decision && !decisionError && (
            <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-6 flex items-center gap-3 mb-5">
              <Loader2 className="w-5 h-5 animate-spin text-indigo-400" aria-hidden="true" />
              <span className="text-sm text-slate-300">Fitting the predictive law…</span>
            </div>
          )}

          {decision && (
            <>
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 mb-5">
                {/* q* hero */}
                <div className="bg-slate-800/70 border border-indigo-500/50 rounded-xl p-6 flex flex-col justify-center">
                  <span className="text-xs font-semibold uppercase tracking-wider text-indigo-300">
                    Order quantity q*
                  </span>
                  <span className="text-3xl font-semibold text-white tabular-nums mt-2">
                    {fmtQty(decision.order_quantity)}{' '}
                    <span className="text-base font-normal text-slate-400">
                      {decision.order_quantity === 1 ? 'unit' : 'units'}
                    </span>
                  </span>
                  <span className="text-sm text-slate-400 mt-2 leading-relaxed">
                    The smallest integer q with F(q) ≥ τ, on the{' '}
                    {decision.demand_distribution.periods_aggregated === 1
                      ? 'monthly'
                      : `${decision.demand_distribution.periods_aggregated}-month`}{' '}
                    predictive law fitted to{' '}
                    {decision.input.kind === 'panel_series' ? (
                      <span className="font-mono text-slate-300">{decision.input.series}</span>
                    ) : (
                      'your history'
                    )}{' '}
                    ({decision.input.n_periods} observations).
                  </span>
                  {decisionLoading && (
                    <span className="flex items-center gap-2 text-xs text-slate-400 mt-3">
                      <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> updating…
                    </span>
                  )}
                </div>

                <div className="lg:col-span-2 grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <StatTile
                    label="Expected cost at q*"
                    value={`${fmtUsd(decision.expected.expected_total_usd, 4)} / SKU-period`}
                    sub={`${fmtUsd(decision.expected.expected_underage_usd, 4)} shortage + ${fmtUsd(
                      decision.expected.expected_overage_usd,
                      4
                    )} holding, at a ${priceLabel} unit price.`}
                  />
                  <StatTile
                    label="Cycle service level"
                    value={fmtRate(decision.expected.cycle_service_level, 1)}
                    sub="P(demand ≤ q*) over one review period under the predictive law — a model probability with a stated window, not a measured service rate."
                  />
                  <StatTile
                    label="Fill rate"
                    value={fmtRate(decision.expected.fill_rate, 1)}
                    sub="Fraction of UNITS met from stock over the same window. A different number from the line above; they must not be quoted for each other."
                  />
                  <StatTile
                    label="Observed history"
                    value={`${fmtNum(decision.input.observed_mean_per_month, 2)} units/month`}
                    sub={`Non-zero in ${fmtRate(decision.input.observed_nonzero_fraction, 0)} of months. Predictive law: ${decision.demand_distribution.family}.`}
                  />
                </div>
              </div>

              {/* Quantile ladder */}
              <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5">
                <h3 className="text-base font-semibold text-slate-200">
                  Where τ lands on the predictive demand law
                </h3>
                <p className="text-sm text-slate-400 mt-1 mb-4 max-w-4xl leading-relaxed">
                  The bars are the quantiles of the fitted demand distribution for one review
                  period. The order quantity is not the middle of that distribution and it is not
                  its mean — it is the point below which demand falls with probability τ.
                </p>
                <div className="overflow-x-auto">
                  <div className="min-w-[440px]">
                    <ResponsiveContainer width="100%" height={280}>
                      <BarChart
                        data={quantileData}
                        margin={{ top: 24, right: 16, bottom: 34, left: 16 }}
                      >
                        <CartesianGrid strokeDasharray="3 3" stroke={COLOR.grid} />
                        <XAxis
                          dataKey="label"
                          tick={{ fill: COLOR.axis, fontSize: 12 }}
                          stroke={COLOR.axis}
                          label={{
                            value: 'percentile of the predictive demand law',
                            position: 'insideBottom',
                            offset: -18,
                            fill: COLOR.axis,
                            fontSize: 12,
                          }}
                        />
                        <YAxis
                          tick={{ fill: COLOR.axis, fontSize: 12 }}
                          stroke={COLOR.axis}
                          allowDecimals={false}
                          label={{
                            value: 'demand (units)',
                            angle: -90,
                            position: 'insideLeft',
                            offset: 6,
                            style: { textAnchor: 'middle' },
                            fill: COLOR.axis,
                            fontSize: 12,
                          }}
                        />
                        <Tooltip
                          contentStyle={TOOLTIP_STYLE}
                          cursor={{ fill: 'rgba(148,163,184,0.12)' }}
                          formatter={(v: unknown) => [`${Number(v)} units`, 'demand quantile'] as [string, string]}
                          labelFormatter={(l: unknown) => `${String(l)}th percentile`}
                        />
                        <Bar dataKey="units" name="demand quantile (units)" fill={COLOR.slate} radius={[3, 3, 0, 0]} />
                        <ReferenceLine
                          y={decision.comparisons.predictive_mean}
                          stroke={COLOR.amber}
                          strokeDasharray="4 4"
                          label={{
                            value: `mean forecast = ${fmtNum(decision.comparisons.predictive_mean, 2)} units`,
                            position: 'insideBottomRight',
                            fill: '#fcd34d',
                            fontSize: 12,
                          }}
                        />
                        <ReferenceLine
                          y={decision.order_quantity}
                          stroke={COLOR.indigo}
                          strokeWidth={2}
                          label={{
                            value: `q* = ${fmtQty(decision.order_quantity)} units at τ = ${fmtNum(
                              decision.costs.critical_ratio,
                              3
                            )}`,
                            position: 'insideTopRight',
                            fill: COLOR.indigoLight,
                            fontSize: 12,
                          }}
                        />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </div>
                <ChartKey
                  items={[
                    { color: COLOR.slate, label: 'demand quantile (units)' },
                    { color: COLOR.indigo, label: `order quantity q* (units)`, dashed: true },
                    { color: COLOR.amber, label: 'mean of the predictive law (units)', dashed: true },
                  ]}
                />
              </div>

              {/* Naive rules beside it */}
              <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5">
                <h3 className="text-base font-semibold text-slate-200">
                  What the naive rules would have ordered
                </h3>
                <p className="text-sm text-slate-400 mt-1 mb-4 max-w-4xl leading-relaxed">
                  Same history, same costs, four different rules. Expected cost is under the same
                  predictive law, in USD per SKU per review period at a {priceLabel} unit price.
                </p>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm min-w-[560px]">
                    <thead>
                      <tr className="text-left text-xs text-slate-400 uppercase tracking-wider border-b border-slate-700">
                        <th className="py-2 pr-4">Rule</th>
                        <th className="py-2 pr-4 text-right">Order (units)</th>
                        <th className="py-2 pr-4 text-right">Expected cost (USD)</th>
                        <th className="py-2">What it gets wrong</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr className="border-b border-slate-800/70 text-slate-200 bg-indigo-500/10">
                        <td className="py-2 pr-4 font-semibold">Newsvendor fractile</td>
                        <td className="py-2 pr-4 text-right tabular-nums font-semibold">
                          {fmtQty(decision.order_quantity)}
                        </td>
                        <td className="py-2 pr-4 text-right tabular-nums font-semibold text-emerald-300">
                          {fmtUsd(decision.expected.expected_total_usd, 4)}
                        </td>
                        <td className="py-2 text-slate-400">
                          The exact minimiser of expected cost for integer demand.
                        </td>
                      </tr>
                      <tr className="border-b border-slate-800/70 text-slate-300">
                        <td className="py-2 pr-4">Order the point forecast</td>
                        <td className="py-2 pr-4 text-right tabular-nums">
                          {fmtQty(decision.comparisons.order_point_forecast)}
                        </td>
                        <td className="py-2 pr-4 text-right tabular-nums">
                          {fmtUsd(decision.comparisons.cost_of_ordering_point_forecast, 4)}
                        </td>
                        <td className="py-2 text-slate-400">
                          Ignores the asymmetry entirely — right only if τ happened to equal F(mean).
                        </td>
                      </tr>
                      <tr className="border-b border-slate-800/70 text-slate-300">
                        <td className="py-2 pr-4">Normal safety stock, μ + z<sub className="text-[11px]">τ</sub> σ</td>
                        <td className="py-2 pr-4 text-right tabular-nums">
                          {fmtQty(decision.comparisons.order_normal_approximation)}
                        </td>
                        <td className="py-2 pr-4 text-right tabular-nums">
                          {fmtUsd(decision.comparisons.cost_of_normal_approximation, 4)}
                        </td>
                        <td className="py-2 text-slate-400">
                          Has the asymmetry, gets the SHAPE wrong: a count law that is mostly zeros
                          is not normal.
                        </td>
                      </tr>
                      <tr className="border-b border-slate-800/70 text-slate-300">
                        <td className="py-2 pr-4">Scarf (1958) min-max</td>
                        <td className="py-2 pr-4 text-right tabular-nums">
                          {fmtQty(decision.comparisons.order_scarf_minmax)}
                        </td>
                        <td className="py-2 pr-4 text-right tabular-nums text-slate-400">—</td>
                        <td className="py-2 text-slate-400">
                          Worst-case optimal over every law with these two moments, so it
                          over-orders on demand that is not adversarial.
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <p className="text-xs text-slate-400 mt-3 leading-relaxed">
                  The two closed-form rules are continuous — the API returns them unrounded, which is
                  why they carry decimals where q* does not. The API returns Scarf's order quantity
                  but not its expected cost at that quantity, so that cell is an em dash rather than
                  a number invented here; Scarf is scored properly in the panel evaluation below,
                  where it turns out to be the toughest baseline of the six.
                </p>
              </div>
            </>
          )}
        </section>

        {/* ══════════════════ 2. THE EVIDENCE ══════════════════════════ */}
        <section className="mb-10">
          <SectionHeading
            eyebrow="2 — The evidence"
            title="Against every baseline, on held-out demand"
          >
            An order quantity on its own is not evidence. The house rule in this repo is that a
            policy ships only by beating a stated baseline, with a paired bootstrap CI that
            excludes zero — the same gate shape the lead-time and regime models are held to.
          </SectionHeading>

          {evalStale && !evalLoading && (
            <div className="bg-slate-800/70 border border-indigo-500/50 rounded-lg px-4 py-3 mb-5 flex flex-col sm:flex-row sm:items-center gap-3">
              <Info size={20} className="text-indigo-300 shrink-0" aria-hidden="true" />
              <p className="text-sm text-slate-300 flex-1 leading-relaxed">
                The evidence below was computed for{' '}
                <span className="font-mono text-slate-200">{evaluation?.protocol.forecast_method}</span>,{' '}
                {evaluation?.protocol.review_period_months}-month review,{' '}
                <span className="font-mono text-slate-200">{evaluation?.costs.shortage_mode}</span>.
                Your levers now say <span className="font-mono text-slate-200">{method}</span>,{' '}
                {reviewPeriod}-month, <span className="font-mono text-slate-200">{shortageMode}</span>.
              </p>
              <button
                type="button"
                onClick={() => runEvaluation(method, reviewPeriod, shortageMode)}
                className="min-h-[44px] bg-indigo-600 hover:bg-indigo-500 border border-indigo-500 px-4 py-2 rounded text-sm font-medium text-white transition-colors focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:ring-offset-2 focus:ring-offset-slate-950 shrink-0"
              >
                Show the evidence at these settings
              </button>
            </div>
          )}

          {evalLoading && (
            <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5">
              <div className="flex items-center gap-3">
                <Loader2 className="w-5 h-5 animate-spin text-indigo-400" aria-hidden="true" />
                <span className="text-sm text-slate-200 font-medium">
                  Loading the panel evaluation… {evalElapsed}s
                </span>
              </div>
              <p className="text-sm text-slate-400 mt-2 leading-relaxed">
                Every balanced series is scored at three rolling origins under six forecast
                methods, then compared with a 5,000-replication paired bootstrap. The full sweep
                takes over four minutes to precompute, so it is not done on request: all 72 settings are
                precomputed by <span className="font-mono">seeds.run_newsvendor</span> and served
                from the committed artifact, which a backend test re-runs and compares leaf by
                leaf.
              </p>
            </div>
          )}

          {evalError && !evalLoading && (
            <div className="bg-red-500/20 border border-red-400 rounded-lg px-4 py-3 text-red-300 flex gap-3 mb-5">
              <AlertTriangle size={20} className="shrink-0 mt-0.5" aria-hidden="true" />
              <div className="flex-1">
                <p className="font-semibold">The evaluation failed</p>
                <p className="text-sm mt-1 leading-relaxed">{evalError}</p>
                <button
                  type="button"
                  onClick={() => runEvaluation(method, reviewPeriod, shortageMode)}
                  className="mt-3 min-h-[44px] bg-slate-800 border border-slate-700 px-3 py-2 rounded text-sm text-slate-300 hover:bg-slate-700 transition-colors focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 focus:ring-offset-slate-950"
                >
                  Retry
                </button>
              </div>
            </div>
          )}

          {evaluation && (
            <>
              {/* Ship gate */}
              <div
                className={`rounded-xl border px-5 py-4 mb-5 flex gap-3 ${
                  evaluation.ship_gate.passed
                    ? 'bg-emerald-500/10 border-emerald-500/60'
                    : 'bg-amber-500/15 border-amber-400/70'
                }`}
              >
                {evaluation.ship_gate.passed ? (
                  <CheckCircle2 size={22} className="text-emerald-300 shrink-0 mt-0.5" aria-hidden="true" />
                ) : (
                  <XCircle size={22} className="text-amber-300 shrink-0 mt-0.5" aria-hidden="true" />
                )}
                <div>
                  <p
                    className={`text-base font-semibold ${
                      evaluation.ship_gate.passed ? 'text-emerald-200' : 'text-amber-200'
                    }`}
                  >
                    Ship gate {evaluation.ship_gate.passed ? 'PASSED' : 'FAILED'} —{' '}
                    <span className="font-mono text-sm">{evaluation.ship_gate.policy}</span>
                  </p>
                  <p className="text-sm text-slate-300 mt-1 leading-relaxed">
                    {evaluation.ship_gate.reason}
                  </p>
                  <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                    Settings:{' '}
                    <span className="font-mono">{evaluation.protocol.forecast_method}</span>,{' '}
                    {evaluation.protocol.review_period_months}-month review period,{' '}
                    <span className="font-mono">{evaluation.costs.shortage_mode}</span>, τ ={' '}
                    {fmtNum(evaluation.costs.critical_ratio, 4)}. The gate fails closed and it does
                    fail — it is not decoration.
                  </p>
                </div>
              </div>

              {/* Protocol */}
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-5">
                <StatTile
                  label="Held-out decisions"
                  value={fmtInt(evaluation.panel.n_decisions)}
                  sub={`${fmtInt(evaluation.panel.n_series_scored)} series × ${evaluation.protocol.n_origins} rolling origins × ${evaluation.protocol.blocks_per_origin} blocks.`}
                />
                <StatTile
                  label="Bootstrap"
                  value={`${fmtInt(evaluation.paired_vs_toughest_baseline.n_boot)} reps`}
                  sub={`Paired by ${evaluation.protocol.replication_unit}, n = ${fmtInt(evaluation.paired_vs_toughest_baseline.n)}.`}
                />
                <StatTile
                  label="Toughest baseline"
                  value={POLICY_LABELS[evaluation.toughest_baseline] ?? evaluation.toughest_baseline}
                  sub={`Beaten by ${fmtNum(evaluation.paired_vs_toughest_baseline.pct_cost_reduction, 1)}% of its cost.`}
                  valueClass="text-base text-white"
                />
                <StatTile
                  label="Series dropped"
                  value={fmtInt(
                    evaluation.panel.n_series_dropped_unbalanced +
                      evaluation.panel.n_series_dropped_pmf_invariant
                  )}
                  sub={`${evaluation.panel.n_series_dropped_unbalanced} by the leaderboard's balance rule, ${evaluation.panel.n_series_dropped_pmf_invariant} for a predictive law that violated its own mean invariant.`}
                />
              </div>

              {/* Baseline table with CI strips */}
              <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5">
                <h3 className="text-base font-semibold text-slate-200">
                  Cost saving against each baseline
                </h3>
                <p className="text-sm text-slate-400 mt-1 mb-4 max-w-4xl leading-relaxed">
                  {evaluation.units.mean_difference} The interval column draws each 95% CI against a
                  shared zero line: a bar sitting entirely to one side of that line is the claim.
                </p>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm min-w-[820px]">
                    <thead>
                      <tr className="text-left text-xs text-slate-400 uppercase tracking-wider border-b border-slate-700">
                        <th className="py-2 pr-4">Policy</th>
                        <th className="py-2 pr-4 text-right">Cost (USD/SKU-period)</th>
                        <th className="py-2 pr-4 text-right">Mean order (units)</th>
                        <th className="py-2 pr-4 text-right">Δ vs newsvendor</th>
                        <th className="py-2 pr-4 w-[180px]">95% CI vs zero</th>
                        <th className="py-2 pr-4 text-right">CI excludes 0</th>
                        <th className="py-2 pr-4 text-right">Reduction</th>
                        <th className="py-2">Win / tie / loss</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr className="border-b border-slate-800/70 text-slate-200 bg-indigo-500/10">
                        <td className="py-2 pr-4 font-semibold">Newsvendor fractile (the policy)</td>
                        <td className="py-2 pr-4 text-right tabular-nums font-semibold">
                          {fmtUsd(
                            evaluation.policies.newsvendor_fractile?.mean_cost_usd_per_sku_period,
                            5
                          )}
                        </td>
                        <td className="py-2 pr-4 text-right tabular-nums">
                          {fmtNum(evaluation.policies.newsvendor_fractile?.mean_order_quantity, 2)}
                        </td>
                        <td className="py-2 pr-4 text-right text-slate-400">—</td>
                        <td className="py-2 pr-4 text-slate-400">—</td>
                        <td className="py-2 pr-4 text-right text-slate-400">—</td>
                        <td className="py-2 pr-4 text-right text-slate-400">—</td>
                        <td className="py-2 text-slate-400">—</td>
                      </tr>
                      {baselineKeys.map((key) => {
                        const c = evaluation.paired_vs_newsvendor[key];
                        const p = evaluation.policies[key];
                        const favourable = c.mean_difference > 0;
                        return (
                          <tr key={key} className="border-b border-slate-800/70 text-slate-300">
                            <td className="py-2 pr-4">{POLICY_LABELS[key] ?? key}</td>
                            <td className="py-2 pr-4 text-right tabular-nums">
                              {fmtUsd(p?.mean_cost_usd_per_sku_period, 5)}
                            </td>
                            <td className="py-2 pr-4 text-right tabular-nums">
                              {fmtNum(p?.mean_order_quantity, 2)}
                            </td>
                            <td
                              className="py-2 pr-4 text-right tabular-nums font-medium"
                              style={{ color: favourable ? COLOR.emerald : COLOR.red }}
                            >
                              {c.mean_difference > 0 ? '+' : ''}
                              {fmtNum(c.mean_difference, 5)}
                            </td>
                            <td className="py-2 pr-4">
                              <CiStrip
                                low={c.ci95_low}
                                high={c.ci95_high}
                                mean={c.mean_difference}
                                domainMin={ciMin}
                                domainMax={ciMax}
                                excludesZero={c.significant}
                                favourable={favourable}
                              />
                              <span className="block text-xs text-slate-400 tabular-nums">
                                [{fmtNum(c.ci95_low, 5)}, {fmtNum(c.ci95_high, 5)}]
                              </span>
                            </td>
                            <td className="py-2 pr-4 text-right">
                              <span
                                className={`inline-flex items-center gap-1 text-xs font-semibold ${
                                  c.significant
                                    ? favourable
                                      ? 'text-emerald-300'
                                      : 'text-red-300'
                                    : 'text-amber-300'
                                }`}
                              >
                                {c.significant ? (
                                  <CheckCircle2 size={14} aria-hidden="true" />
                                ) : (
                                  <AlertTriangle size={14} aria-hidden="true" />
                                )}
                                {c.significant ? (favourable ? 'yes — cheaper' : 'yes — WORSE') : 'no'}
                              </span>
                            </td>
                            <td className="py-2 pr-4 text-right tabular-nums">
                              {fmtNum(c.pct_cost_reduction, 1)}%
                            </td>
                            <td className="py-2">
                              <div className="flex h-2.5 w-[120px] rounded overflow-hidden" aria-hidden="true">
                                <div style={{ width: `${c.win_rate * 100}%`, backgroundColor: COLOR.emerald }} />
                                <div style={{ width: `${c.tie_rate * 100}%`, backgroundColor: COLOR.slate }} />
                                <div style={{ width: `${c.loss_rate * 100}%`, backgroundColor: COLOR.red }} />
                              </div>
                              <span className="block text-xs text-slate-400 tabular-nums mt-1">
                                {fmtRate(c.win_rate)} / {fmtRate(c.tie_rate)} / {fmtRate(c.loss_rate)}
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <ChartKey
                  items={[
                    { color: COLOR.emerald, label: 'CI excludes 0, newsvendor cheaper' },
                    { color: COLOR.red, label: 'CI excludes 0, newsvendor more expensive' },
                    { color: COLOR.amber, label: 'CI covers 0 — no distinguishable difference' },
                    { color: '#e2e8f0', label: 'paired mean difference (USD/SKU-period)' },
                    { color: COLOR.slate, label: 'tied series (win / tie / loss bar)' },
                  ]}
                />
                <p className="text-xs text-slate-400 mt-3 leading-relaxed">
                  Interval axis spans {fmtNum(ciMin, 4)} to {fmtNum(ciMax, 4)} USD per SKU per review
                  period, shared by every row; the pale vertical rule is zero.
                </p>

                {/* The tie column — the sentence that makes the win credible */}
                {evaluation.paired_vs_toughest_baseline.tie_rate > 0.3 && (
                  <div className="mt-4 bg-slate-900/60 border border-slate-700 rounded-lg p-4 flex gap-3">
                    <Info size={18} className="text-slate-300 shrink-0 mt-0.5" aria-hidden="true" />
                    <p className="text-sm text-slate-300 leading-relaxed">
                      <strong className="text-slate-200">Read the tie column, not just the
                      reduction.</strong>{' '}
                      Against{' '}
                      {POLICY_LABELS[evaluation.toughest_baseline] ?? evaluation.toughest_baseline}{' '}
                      the policy wins on {fmtRate(evaluation.paired_vs_toughest_baseline.win_rate)} of
                      series, loses on {fmtRate(evaluation.paired_vs_toughest_baseline.loss_rate)} and{' '}
                      <strong className="text-slate-200">
                        ties on {fmtRate(evaluation.paired_vs_toughest_baseline.tie_rate)}
                      </strong>
                      . On a panel this sparse the fractile and the moment-based rules frequently
                      round to the same integer. The saving is real and significant in the paired
                      mean; it is not a win on most SKUs, and quoting the reduction without this
                      sentence would be the flattering version.
                    </p>
                  </div>
                )}
              </div>
            </>
          )}
        </section>

        {/* ══════════════════ 3. THE ARGUMENT ══════════════════════════ */}
        {evaluation && lb && (
          <section className="mb-10">
            <SectionHeading
              eyebrow="3 — The argument"
              title="The forecast that wins on accuracy loses on cost"
            >
              Give all six forecasting methods the <em>same</em> newsvendor rule and rank them by
              the realised cost of the decision they produce. The ranking is not the accuracy
              ranking. This is the reason this repo scores forecasts with proper scoring rules
              rather than with point error — argued everywhere else in prose, measured here in
              dollars.
            </SectionHeading>

            {maseWinner && costWinner && maseWinner !== costWinner && (
              <div className="bg-gradient-to-r from-amber-500/15 to-slate-800/40 border border-amber-400/70 rounded-xl p-6 mb-5">
                <p className="text-base sm:text-lg text-amber-100 leading-relaxed">
                  <span className="font-mono font-semibold">{maseWinner}</span>
                  {maseWinner === 'zero' ? ' — forecasting nothing, every period — ' : ' '}
                  <strong>wins the point-accuracy leaderboard outright</strong> on this panel
                  (MASE {fmtNum(lb.mase_mean[maseWinner], 4)}, best of {lb.order_by_mase.length})
                  and produces the{' '}
                  <strong>worst decision cost of the {lb.order_by_decision_cost.length}</strong>:{' '}
                  <span className="tabular-nums font-semibold">
                    {fmtUsd(maseWinnerCost, 4)}
                  </span>{' '}
                  against <span className="font-mono font-semibold">{costWinner}</span>&apos;s{' '}
                  <span className="tabular-nums font-semibold">{fmtUsd(costWinnerCost, 4)}</span> per
                  SKU per review period.
                </p>
                {excessOfMaseWinner != null && (
                  <p className="text-sm text-amber-200/90 mt-3 leading-relaxed">
                    That is {fmtNum(excessOfMaseWinner * 100, 0)}% more cost for the method that
                    scored best on accuracy. Optimising MASE on intermittent demand recommends you
                    stock nothing; the cost of stocking nothing is what the column on the right
                    measures.
                  </p>
                )}
              </div>
            )}

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-5">
              <StatTile
                label={`${maseWinner ?? '—'} — rank by MASE`}
                value={`${maseWinner ? maseRank[maseWinner] : '—'} of ${lb.order_by_mase.length}`}
                sub="Lower point error is better. This is the winner."
                accent="border-emerald-500/50"
                valueClass="text-xl text-emerald-300"
              />
              <StatTile
                label={`${maseWinner ?? '—'} — rank by decision cost`}
                value={`${maseWinner ? costRank[maseWinner] : '—'} of ${lb.order_by_decision_cost.length}`}
                sub="Same method, same panel, same newsvendor rule. This is last place."
                accent="border-red-500/50"
                valueClass="text-xl text-red-300"
              />
              <StatTile
                label="Ranking changed"
                value={lb.winner_changed ? 'Yes' : 'No'}
                sub="The API's own verdict on whether the accuracy winner and the decision winner are the same method."
                accent="border-amber-500/50"
                valueClass="text-xl text-amber-300"
              />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 mb-5">
              {[
                {
                  title: 'Ranked by point accuracy',
                  axis: 'MASE (unitless, lower is better)',
                  data: maseChart,
                  dp: 3,
                },
                {
                  title: 'Ranked by decision cost',
                  axis: 'USD per SKU per review period, at $1.00 unit price (lower is better)',
                  data: costChart,
                  dp: 4,
                },
              ].map((chart) => (
                <div key={chart.title} className="bg-slate-800/60 border border-slate-700 rounded-xl p-5">
                  <h3 className="text-base font-semibold text-slate-200 mb-1">{chart.title}</h3>
                  <p className="text-xs text-slate-400 mb-3">Best at the top. Rank is printed on each bar.</p>
                  <div className="overflow-x-auto">
                    <div className="min-w-[320px]">
                      <ResponsiveContainer width="100%" height={250}>
                        <BarChart
                          data={chart.data}
                          layout="vertical"
                          margin={{ top: 4, right: 44, bottom: 34, left: 4 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" stroke={COLOR.grid} horizontal={false} />
                          <XAxis
                            type="number"
                            tick={{ fill: COLOR.axis, fontSize: 12 }}
                            stroke={COLOR.axis}
                            label={{
                              value: chart.axis,
                              position: 'insideBottom',
                              offset: -18,
                              fill: COLOR.axis,
                              fontSize: 12,
                            }}
                          />
                          <YAxis
                            type="category"
                            dataKey="method"
                            width={92}
                            tick={{ fill: COLOR.axis, fontSize: 12 }}
                            stroke={COLOR.axis}
                          />
                          <Tooltip
                            contentStyle={TOOLTIP_STYLE}
                            cursor={{ fill: 'rgba(148,163,184,0.12)' }}
                            formatter={(v: unknown) => [Number(v).toFixed(chart.dp), chart.axis] as [string, string]}
                          />
                          <Bar dataKey="value" name={chart.axis} radius={[0, 3, 3, 0]}>
                            {chart.data.map((d) => (
                              <Cell
                                key={d.method}
                                fill={d.isMaseWinner ? COLOR.amber : COLOR.indigo}
                              />
                            ))}
                            <LabelList
                              dataKey="rank"
                              position="right"
                              fill="#cbd5e1"
                              fontSize={12}
                            />
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                  <ChartKey
                    items={[
                      { color: COLOR.amber, label: `${maseWinner ?? 'accuracy winner'} — the MASE winner` },
                      { color: COLOR.indigo, label: 'every other method' },
                    ]}
                  />
                </div>
              ))}
            </div>

            <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 mb-5">
              <h3 className="text-base font-semibold text-slate-200 mb-3">
                The two rankings, side by side
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-sm min-w-[620px]">
                  <thead>
                    <tr className="text-left text-xs text-slate-400 uppercase tracking-wider border-b border-slate-700">
                      <th className="py-2 pr-4">Method</th>
                      <th className="py-2 pr-4 text-right">MASE</th>
                      <th className="py-2 pr-4 text-right">Rank by MASE</th>
                      <th className="py-2 pr-4 text-right">Decision cost (USD)</th>
                      <th className="py-2 pr-4 text-right">Rank by cost</th>
                      <th className="py-2">Movement</th>
                    </tr>
                  </thead>
                  <tbody>
                    {lb.order_by_decision_cost.map((m) => {
                      const move = maseRank[m] - costRank[m];
                      return (
                        <tr
                          key={m}
                          className={`border-b border-slate-800/70 ${
                            m === maseWinner ? 'bg-amber-500/10 text-amber-100' : 'text-slate-300'
                          }`}
                        >
                          <td className="py-2 pr-4 font-mono">{m}</td>
                          <td className="py-2 pr-4 text-right tabular-nums">
                            {fmtNum(lb.mase_mean[m], 4)}
                          </td>
                          <td className="py-2 pr-4 text-right tabular-nums">{maseRank[m]}</td>
                          <td className="py-2 pr-4 text-right tabular-nums">
                            {fmtUsd(lb.decision_cost_usd_per_sku_period[m], 5)}
                          </td>
                          <td className="py-2 pr-4 text-right tabular-nums">{costRank[m]}</td>
                          <td className="py-2">
                            <span className="inline-flex items-center gap-1.5 text-xs tabular-nums">
                              {maseRank[m]}
                              <ArrowRight size={13} aria-hidden="true" />
                              {costRank[m]}
                              <span className="text-slate-400">
                                {move === 0
                                  ? '(no change)'
                                  : move > 0
                                    ? `(${move} place${move === 1 ? '' : 's'} worse)`
                                    : `(${-move} place${move === -1 ? '' : 's'} better)`}
                              </span>
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="text-xs text-slate-400 mt-3 leading-relaxed">{lb.note}</p>
            </div>

            {assumptions && (
              <div className="bg-slate-900/60 border border-slate-700 rounded-xl p-5">
                <h3 className="text-base font-semibold text-slate-200 mb-2">
                  Why the forecasting track is load-bearing here
                </h3>
                <p className="text-sm text-slate-300 leading-relaxed">
                  {assumptions.derivation.dual_identity}
                </p>
                <p className="text-sm text-slate-400 mt-2 leading-relaxed">
                  In other words the pinball loss already published on the demand leaderboard{' '}
                  <em>is</em> this decision cost up to a constant — the scoring rule and the dollar
                  figure are the same object, which is why a method can be picked on the score and
                  defended on the cost.
                </p>
              </div>
            )}
          </section>
        )}

        {/* ══════════════════ 4. LIMITS ════════════════════════════════ */}
        <section className="mb-10">
          <SectionHeading
            eyebrow="4 — What this does not show"
            title="The limits, stated where the claim is"
          >
            These are the reasons not to believe the numbers above, kept on the page rather than in
            a footnote. Two of them are settings you can reach with the levers at the top, and the
            API will report the failure itself.
          </SectionHeading>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 mb-5">
            <div className="bg-amber-500/10 border border-amber-400/60 rounded-xl p-5">
              <h3 className="text-base font-semibold text-amber-200 mb-2">
                Every dollar is per $1.00 of unit price
              </h3>
              <p className="text-sm text-slate-300 leading-relaxed">
                The Monash panel is real demand with <strong>no prices attached</strong>, and this
                repo does not fabricate data. τ does not depend on price at all — both costs are
                proportional to it, so it cancels — which is precisely what makes the evaluation
                possible on a priceless panel. The evaluation endpoint therefore takes no unit price
                parameter; multiply its figures by your part&apos;s price yourself. The unit-price
                lever above moves the <em>decision</em> panel only.
              </p>
              {assumptions && (
                <p className="text-xs text-slate-400 mt-3 leading-relaxed">
                  {assumptions.derivation.price_invariance}
                </p>
              )}
            </div>

            <div className="bg-amber-500/10 border border-amber-400/60 rounded-xl p-5">
              <h3 className="text-base font-semibold text-amber-200 mb-2">
                <span className="font-mono">line_down</span> fails the ship gate
              </h3>
              <p className="text-sm text-slate-300 leading-relaxed">
                At <span className="font-mono">shortage_mode = line_down</span> the fractile is
                τ = 0.9931. The longest training window in this panel is 45 monthly observations, so
                the finest quantile the data can resolve is 1/45 = 0.022 — a 99.3rd percentile is an
                extrapolation of an assumed tail, not a measurement. The margin over the toughest
                baseline stops excluding zero and the gate refuses it. Switch the shortage mode
                above and reload the evidence to watch it happen.
              </p>
            </div>

            <div className="bg-amber-500/10 border border-amber-400/60 rounded-xl p-5">
              <h3 className="text-base font-semibold text-amber-200 mb-2">
                At a 3-month review period the policy loses
              </h3>
              <p className="text-sm text-slate-300 leading-relaxed">
                Aggregating the monthly law to a quarter is an exact convolution{' '}
                <em>only</em> under the model&apos;s i.i.d.-across-periods assumption, and
                spare-parts demand is serially clustered. At a 3-month review period the policy is
                beaten by simply ordering the point forecast, with a CI that excludes zero on the
                wrong side. At 6 months it wins again. The advantage is a function of the review
                period, and quoting the 1-month number without saying so would be quoting the best
                cell of a sweep.
              </p>
            </div>

            <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5">
              <h3 className="text-base font-semibold text-slate-200 mb-2">
                What the API does not return
              </h3>
              <ul className="text-sm text-slate-300 space-y-2 leading-relaxed list-disc pl-4">
                <li>
                  The expected cost of Scarf&apos;s order quantity for a single decision — only the
                  quantity. That cell is an em dash above rather than a number invented here.
                </li>
                <li>
                  The raw demand history of a panel series. When a preset is selected the page shows
                  the mean and non-zero fraction the API reports for the fitted window, not a chart
                  of a series it never sends.
                </li>
                <li>
                  The full predictive pmf — nine quantiles and two moments, which is what the
                  distribution chart above is drawn from.
                </li>
              </ul>
            </div>
          </div>

          {/* Verbatim caveats from the API itself */}
          {(assumptions || decision || evaluation) && (
            <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5">
              <h3 className="text-base font-semibold text-slate-200 mb-1">
                Caveats, verbatim from the API
              </h3>
              <p className="text-sm text-slate-400 mb-4 leading-relaxed">
                These strings ship in the responses themselves, so the page and the endpoint cannot
                disagree about what the numbers mean.
              </p>
              <ul className="space-y-3">
                {Array.from(
                  new Set([
                    ...(assumptions?.caveats ?? []),
                    ...(decision?.caveats ?? []),
                    ...(evaluation?.caveats ?? []),
                  ])
                ).map((c) => (
                  <li key={c} className="flex gap-3 text-sm text-slate-300 leading-relaxed">
                    <AlertTriangle
                      size={16}
                      className="text-amber-400 shrink-0 mt-0.5"
                      aria-hidden="true"
                    />
                    <span>{c}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>

        {/* ── Provenance ───────────────────────────────────────────────── */}
        {evaluation && (
          <section className="bg-slate-900/60 border border-slate-700 rounded-xl p-5 mb-4">
            <h3 className="text-base font-semibold text-slate-200 mb-3">Provenance</h3>
            <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-3 text-sm">
              <div>
                <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">Panel</dt>
                <dd className="text-slate-300 mt-1 leading-relaxed">{evaluation.protocol.panel}</dd>
              </div>
              <div>
                <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">Split</dt>
                <dd className="text-slate-300 mt-1 leading-relaxed">{evaluation.protocol.split}</dd>
              </div>
              <div>
                <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                  Balance rule
                </dt>
                <dd className="text-slate-300 mt-1 leading-relaxed">{evaluation.protocol.balance_rule}</dd>
              </div>
              <div>
                <dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                  Training windows
                </dt>
                <dd className="text-slate-300 mt-1 leading-relaxed">
                  {evaluation.protocol.train_sizes.join(', ')} months at{' '}
                  {evaluation.protocol.n_origins} rolling origins; horizon{' '}
                  {evaluation.protocol.horizon_months} months.{' '}
                  {evaluation.computation.recomputed ? (
                    <>
                      Evaluated on request, {fmtNum(evaluation.wall_seconds, 1)} s of server
                      wall time.
                    </>
                  ) : (
                    <>
                      Served from <code>{evaluation.computation.source}</code> in{' '}
                      {fmtNum(evaluation.wall_seconds, 2)} s — the committed output of this
                      same evaluator, generated{' '}
                      {evaluation.computation.artifact_generated_at_utc ?? 'unknown'} in{' '}
                      {fmtNum(evaluation.computation.artifact_wall_seconds ?? 0, 1)} s. It is
                      not a summary of the computation; a backend test re-runs the evaluator
                      and asserts every value is identical.
                    </>
                  )}
                </dd>
              </div>
            </dl>
            {costs && (
              <p className="text-sm text-slate-400 mt-4 leading-relaxed">{costs.derivation}</p>
            )}
            {/* The command is one long line. `whitespace-pre` keeps it unwrapped, so the
                <pre> ITSELF must be the scroll container — with `overflow: visible` the
                overflow escaped upward and was swallowed by the `html { overflow-x: hidden }`
                safety net in index.css, leaving the command unreadable and unselectable at
                390px. A wrapper alone was not enough; the scroll has to live on the box whose
                content overflows. */}
            <div className="mt-4">
              <pre className="text-xs text-slate-300 bg-slate-950/70 border border-slate-700 rounded p-3 whitespace-pre overflow-x-auto max-w-full">
                {evaluation.reproduce}
              </pre>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
