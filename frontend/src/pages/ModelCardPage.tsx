import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, ResponsiveContainer, LabelList, Cell,
} from 'recharts';
import {
  Cpu, Database, GitBranch, Activity, ArrowLeft, CheckCircle2, XCircle, HelpCircle, ChevronRight,
} from 'lucide-react';
import {
  mlAPI,
  stressObservationMonth,
  type ModelInfoResponse,
  type ModelComparisonResponse,
  type StressResponse,
  type ModelMetrics,
} from '../services/api';

// ── Formatting helpers ──────────────────────────────────────────────────────
// Never round up, never silently drop a null into a fake "0" — a missing
// number is shown as "—", not invented.
const fmt = (v: number | null | undefined, digits = 3): string =>
  v === null || v === undefined ? '—' : v.toFixed(digits);
const pct = (v: number | null | undefined, digits = 1): string =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`;
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

// Backend prose occasionally carries markdown backticks, ASCII `->` arrows, and
// a lowercased "brier" (a proper noun — Glenn Brier). The source strings can't
// be edited from this file, so render them cleaned instead of verbatim.
const cleanProse = (s: string | null | undefined): string => {
  if (!s) return '';
  return s
    .replace(/`([^`]*)`/g, '$1')
    .replace(/->/g, '→')
    .replace(/\bbrier\b/g, 'Brier');
};

// A backend fragment concatenated mid-sentence by this page can start lowercase
// (it was written to continue a sentence, not begin one). Capitalise it here.
const capFirst = (s: string): string => (s.length > 0 ? s.charAt(0).toUpperCase() + s.slice(1) : s);

// ── Derived prose ───────────────────────────────────────────────────────────
// Nothing on this page may assert a comparison the payload does not support.
// Every word below ("higher", "ties", "win") is computed from the numbers that
// are rendered beside it, so the prose cannot drift away from the artifact the
// way a hardcoded sentence silently does the day the model is retrained.

// Compare at the SAME precision the reader sees, so the word can never
// disagree with the digits printed next to it.
const compareWord = (a: number | null | undefined, b: number | null | undefined, digits = 3): string | null => {
  const x = num(a);
  const y = num(b);
  if (x === null || y === null) return null;
  if (x.toFixed(digits) === y.toFixed(digits)) return 'the same as';
  return y > x ? 'higher than' : 'lower than';
};

// How big is fold-to-fold noise relative to the central estimate?
const spreadWord = (std: number | null | undefined, centre: number | null | undefined): string | null => {
  const s = num(std);
  const c = num(centre);
  if (s === null || c === null || c === 0) return null;
  const ratio = s / Math.abs(c);
  return ratio >= 1 ? 'larger than' : ratio >= 0.5 ? 'comparable to' : 'smaller than';
};

// A 0.5-percentage-point band counts as a tie: on a validation set this small
// a sub-half-point accuracy gap is one or two flipped months, not skill.
const ACCURACY_TIE_EPSILON = 0.005;
const accuracyIsTie = (
  delta: number | null | undefined,
  val: number | null | undefined,
  baseline: number | null | undefined,
): boolean => {
  const d = num(delta) ?? (num(val) !== null && num(baseline) !== null ? (val as number) - (baseline as number) : null);
  return d !== null && Math.abs(d) <= ACCURACY_TIE_EPSILON;
};

// Every Brier figure on this page is printed at BRIER_DIGITS places, because the
// backend's own `ship_gate_reason` quotes them at 4 ("Brier 0.3926 beats both
// persistence 0.5388 …") and the tiles used to print 3 ("0.393") directly above
// it. Two different roundings of one number, adjacent, read as two numbers. One
// constant so they cannot drift apart again.
const BRIER_DIGITS = 4;

// Brier is a LOSS: lower is better, so the skill statement has to invert. Like
// every other sentence on this page it is computed from the two numbers printed
// beside it, and says "the same as" whenever they tie at rendered precision.
const brierSkill = (
  model: number | null | undefined,
  baseline: number | null | undefined,
  digits = BRIER_DIGITS,
): string | null => {
  const m = num(model);
  const b = num(baseline);
  if (m === null || b === null || b === 0) return null;
  if (m.toFixed(digits) === b.toFixed(digits)) return 'the same as persistence';
  const rel = ((b - m) / b) * 100;
  return `${Math.abs(rel).toFixed(1)}% ${rel > 0 ? 'lower' : 'higher'} than persistence`;
};

const accuracyVerdict = (
  delta: number | null | undefined,
  val: number | null | undefined,
  baseline: number | null | undefined,
): string | null => {
  const d = num(delta) ?? (num(val) !== null && num(baseline) !== null ? (val as number) - (baseline as number) : null);
  if (d === null) return null;
  if (Math.abs(d) <= ACCURACY_TIE_EPSILON) return 'ties persistence (within ±0.5 pp)';
  const pp = `${Math.abs(d * 100).toFixed(1)} pp`;
  return d > 0 ? `beats persistence by ${pp}` : `trails persistence by ${pp}`;
};

// `emphasis` marks the ONE number a section is judged on, so the tile a reader
// lands on first is the metric that actually gates shipping — not whichever
// metric happened to be written first.
function InfoTile({
  label, value, sub, emphasis = false,
}: { label: string; value: string; sub?: string; emphasis?: boolean }) {
  return (
    <div className={`rounded-xl p-4 flex flex-col gap-1 border ${
      emphasis
        ? 'bg-emerald-500/5 border-emerald-500/40'
        : 'bg-slate-800/70 border-slate-700'
    }`}>
      <span className={`text-xs font-medium uppercase tracking-wider ${
        emphasis ? 'text-emerald-300/80' : 'text-slate-400'
      }`}>{label}</span>
      <span
        className={`font-bold tabular-nums truncate ${emphasis ? 'text-2xl text-emerald-300' : 'text-xl text-white'}`}
        title={value}
      >{value}</span>
      {sub && <span className={`text-sm ${emphasis ? 'text-emerald-200/70' : 'text-slate-400'}`}>{sub}</span>}
    </div>
  );
}

// A stat tile that carries an honesty framing note beneath the number instead
// of a color judgment — this page's whole point is presenting numbers as they
// are, not dressing a modest R² up as good or a real win down as marginal.
function HeroStat({
  label, value, note, accent,
}: { label: string; value: string; note: string; accent: 'neutral' | 'positive' }) {
  return (
    <div className="bg-slate-800/70 border border-slate-700 rounded-xl p-5 flex-1 min-w-[220px]">
      <div className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">{label}</div>
      <div className={`text-3xl font-bold tabular-nums ${accent === 'positive' ? 'text-emerald-400' : 'text-sky-300'}`}>
        {value}
      </div>
      <p className="text-sm text-slate-400 mt-2 leading-relaxed">{note}</p>
    </div>
  );
}

function GatePill({ passed }: { passed: boolean | null }) {
  if (passed === null) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-slate-700/40 border border-slate-600/40 text-slate-400">
        <HelpCircle className="w-3 h-3" /> gate unknown
      </span>
    );
  }
  return passed ? (
    <span className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-400">
      <CheckCircle2 className="w-3 h-3" /> ship gate passed
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-slate-700/40 border border-slate-600/40 text-slate-400">
      <XCircle className="w-3 h-3" /> ship gate failed
    </span>
  );
}

// Served model = blue (the entity a reader should track); every baseline
// stays a single neutral slate so "beats the field" reads at a glance without
// a legend key per baseline name (a table underneath carries the names).
const SERVED_COLOR = '#60a5fa';
const BASELINE_COLOR = '#64748b';

function R2Chart({ models, baselines }: { models: ModelMetrics[]; baselines: ModelMetrics[] }) {
  const rows = [...models, ...baselines]
    .filter((m) => m.cv_r2_mean !== null && m.cv_r2_mean !== undefined)
    .map((m) => ({
      name: m.name,
      r2: m.cv_r2_mean as number,
      served: m.is_served,
      kind: m.kind,
    }))
    .sort((a, b) => b.r2 - a.r2);

  if (rows.length === 0) {
    return <div className="py-8 text-center text-slate-400 text-sm">No CV metrics available.</div>;
  }

  return (
    <ResponsiveContainer width="100%" height={Math.max(220, rows.length * 44 + 40)}>
      <BarChart data={rows} layout="vertical" margin={{ top: 8, right: 56, left: 8, bottom: 8 }} barCategoryGap="22%">
        <CartesianGrid strokeDasharray="3 3" stroke="#334155" horizontal={false} />
        <XAxis
          type="number"
          stroke="#94a3b8"
          tick={{ fontSize: 11 }}
          tickFormatter={(v) => v.toFixed(2)}
          // Padding the domain keeps the longest negative bar (and the value label at its
          // end) clear of the category axis — they used to render as one token,
          // e.g. "always_210d-2.286".
          domain={[(dataMin: number) => dataMin - 0.4, (dataMax: number) => dataMax + 0.12]}
        />
        <YAxis type="category" dataKey="name" stroke="#94a3b8" width={160} tick={{ fontSize: 11 }} />
        <Bar dataKey="r2" radius={[0, 4, 4, 0]} barSize={20} isAnimationActive={false}>
          {rows.map((r, i) => (
            <Cell key={i} fill={r.served ? SERVED_COLOR : BASELINE_COLOR} />
          ))}
          <LabelList
            dataKey="r2"
            position="right"
            formatter={(v) => Number(v).toFixed(3)}
            style={{ fill: '#cbd5e1', fontSize: 11 }}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function BrierChart({ stress }: { stress: StressResponse }) {
  const rows = [
    { name: 'Model', value: stress.brier, primary: true },
    { name: 'Persistence', value: stress.baseline_brier, primary: false },
    { name: 'Climatology', value: stress.climatology_brier, primary: false },
  ].filter((r) => r.value !== null && r.value !== undefined) as Array<{ name: string; value: number; primary: boolean }>;

  if (rows.length === 0) {
    return <div className="py-8 text-center text-slate-400 text-sm">No Brier scores available.</div>;
  }

  return (
    <ResponsiveContainer width="100%" height={rows.length * 44 + 56}>
      <BarChart data={rows} layout="vertical" margin={{ top: 8, right: 56, left: 8, bottom: 8 }} barCategoryGap="22%">
        <CartesianGrid strokeDasharray="3 3" stroke="#334155" horizontal={false} />
        <XAxis type="number" stroke="#94a3b8" tick={{ fontSize: 11 }} tickFormatter={(v) => v.toFixed(2)} />
        <YAxis type="category" dataKey="name" stroke="#94a3b8" width={90} tick={{ fontSize: 11 }} />
        <Bar dataKey="value" radius={[0, 4, 4, 0]} barSize={20} isAnimationActive={false}>
          {rows.map((r, i) => (
            <Cell key={i} fill={r.primary ? SERVED_COLOR : BASELINE_COLOR} />
          ))}
          <LabelList dataKey="value" position="right" formatter={(v) => Number(v).toFixed(BRIER_DIGITS)} style={{ fill: '#cbd5e1', fontSize: 11 }} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ── The leakage audit ───────────────────────────────────────────────────────
// `GET /ml/model-comparison` has always carried `leakage_audit` — the three-number
// collapse computed on every retrain — and this page never rendered it. What the
// reader saw instead was a sentence inside `caveat` quoting a RETIRED vintage
// (810 rows, random_forest), so the page published one model's numbers under
// another model's name. It is read straight off the artifact here; nothing is
// transcribed, and when the block is absent nothing is shown rather than
// something remembered.
//
// Typed locally on purpose: `ModelComparisonResponse` in services/api.ts is owned
// by another workstream this release. Lift this into that interface when it is quiet.
type LeakageAudit = {
  model?: string;
  n_rows?: number;
  n_families?: number;
  n_manufacturers?: number;
  n_splits?: number;
  random?: number | null;
  family?: number | null;
  manufacturer?: number | null;
  headline?: string;
};

const readLeakageAudit = (cmp: ModelComparisonResponse | null): LeakageAudit | null => {
  const raw = (cmp as unknown as { leakage_audit?: unknown } | null)?.leakage_audit;
  if (!raw || typeof raw !== 'object') return null;
  const audit = raw as LeakageAudit;
  // A block with no scored regime is not an audit; showing its labels alone
  // would imply a measurement that did not happen.
  const scored = [audit.random, audit.family, audit.manufacturer].filter((v) => num(v) !== null);
  return scored.length > 0 ? audit : null;
};

// Each rung of the progression, with the split protocol named beside the score —
// an R² is meaningless without the protocol that produced it.
function LeakageRung({ label, protocolNote, value, protocol }: {
  label: string;
  protocolNote: string;
  value: number | null | undefined;
  protocol: 'optimistic' | 'reported' | 'deployment';
}) {
  const v = num(value);
  // Tone tracks how honest the SPLIT PROTOCOL is, never the sign of the score.
  // Colouring by sign made the leaked random-split number this panel exists to
  // discredit (+0.8341 on the 2026-09-03 artifact) the greenest thing on the page,
  // and made the manufacturer-held-out number deployment actually faces (−0.4422)
  // read as a failure.
  const tone =
    v === null ? 'text-slate-400'
      : protocol === 'optimistic' ? 'text-amber-300'
      : protocol === 'deployment' ? 'text-emerald-300'
      : 'text-sky-300';
  return (
    <div className="flex-1 min-w-[150px] bg-slate-900/60 border border-slate-700/60 rounded-lg px-3 py-2">
      <div className="text-xs uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`text-xl font-mono font-semibold ${tone}`}>
        {v === null ? '—' : `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(4)}`}
      </div>
      <div className="text-sm text-slate-400 leading-snug mt-0.5">{protocolNote}</div>
    </div>
  );
}

export default function ModelCardPage() {
  const navigate = useNavigate();
  const [info, setInfo] = useState<ModelInfoResponse | null>(null);
  const [comparison, setComparison] = useState<ModelComparisonResponse | null>(null);
  const [stress, setStress] = useState<StressResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([mlAPI.modelInfo(), mlAPI.modelComparison(), mlAPI.stress()]).then((results) => {
      if (cancelled) return;
      const [infoRes, cmpRes, stressRes] = results;
      if (infoRes.status === 'fulfilled') setInfo(infoRes.value.data);
      if (cmpRes.status === 'fulfilled') setComparison(cmpRes.value.data);
      if (stressRes.status === 'fulfilled') setStress(stressRes.value.data);
      if (infoRes.status === 'rejected' && cmpRes.status === 'rejected') {
        // This used to read "Run seeds.train_ml_models to serve this page." — an
        // instruction to whoever deploys the API, shown to whoever is reading the
        // page. Reworded for the reader without softening what is missing.
        setError(
          'No trained model is loaded on this server right now, so this page has ' +
          'nothing to report. Rather than show placeholder or remembered numbers, ' +
          'it shows none: the metrics return as soon as a trained model is published ' +
          'to this deployment.',
        );
      }
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, []);

  if (loading) {
    return (
      <div className="min-h-full bg-slate-900 text-slate-100 overflow-y-auto h-full flex items-center justify-center">
        <div className="w-8 h-8 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
      </div>
    );
  }

  const served = comparison?.served_metrics ?? null;
  const paired = comparison?.paired_vs_toughest_baseline ?? {};

  // Paired result, derived — a positive RMSE reduction is a win, a negative one
  // is a loss, and the sign/label/accent all follow the number rather than an
  // assumption baked in on the day this page was written.
  const rmseReduction = num(paired.mean_rmse_reduction_days);
  const rmseStdError = num(paired.std_error);
  const pairedIsWin = rmseReduction !== null && rmseReduction > 0;
  const pairedLabelVerb = rmseReduction === null ? 'result' : pairedIsWin ? 'win' : rmseReduction < 0 ? 'loss' : 'tie';
  // Printed at the precision the API reports, so it reads identically to the
  // same figure quoted inside the caveat below.
  const pairedValue = rmseReduction === null
    ? '—'
    : `${pairedIsWin ? '−' : rmseReduction < 0 ? '+' : ''}${String(Math.abs(rmseReduction))}` +
      `${rmseStdError !== null ? ` ± ${String(rmseStdError)}` : ''} d`;

  // R² spread language, derived from the served artifact's own CV numbers.
  const meanVsMedian = compareWord(served?.cv_r2_median, served?.cv_r2_mean);
  const r2Spread = spreadWord(served?.cv_r2_std, served?.cv_r2_mean);

  // Feature schema is emitted with typed prefixes (n= numeric, c= one-hot).
  // Only describe the mix when every column actually carries a prefix.
  const featureCols = comparison?.feature_columns ?? [];
  const oneHotCount = featureCols.filter((f) => f.startsWith('c=')).length;
  const numericCount = featureCols.filter((f) => f.startsWith('n=')).length;
  const featureMix = featureCols.length > 0 && oneHotCount + numericCount === featureCols.length
    ? `${featureCols.length} columns — ${oneHotCount} one-hot, ${numericCount} numeric`
    : `${featureCols.length} columns`;

  const gateIsBrier = stress?.ship_gate_policy === 'brier';
  const leakage = readLeakageAudit(comparison);

  // Feature exclusions are grouped by their (byte-identical) reason so four
  // features sharing one 200-character sentence render that sentence once,
  // with the feature names listed under it, instead of stacking the same
  // paragraph four times.
  type ExclusionGroup = { reason: string; features: { feature: string; kind?: string }[] };
  const exclusionGroups: ExclusionGroup[] = [];
  (comparison?.feature_exclusions ?? []).forEach((ex) => {
    const reason = String(ex.reason ?? '');
    const feature = String(ex.feature ?? 'feature');
    const kind = ex.kind != null ? String(ex.kind) : undefined;
    const existing = exclusionGroups.find((g) => g.reason === reason);
    if (existing) existing.features.push({ feature, kind });
    else exclusionGroups.push({ reason, features: [{ feature, kind }] });
  });

  return (
    <div className="min-h-full bg-slate-900 text-slate-100 overflow-y-auto h-full">
      <main className="max-w-6xl mx-auto px-6 py-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-[28px] font-bold text-white flex items-center gap-2">
              <Cpu className="w-5 h-5 text-sky-400" /> ML Model Card
            </h1>
            <p className="text-sm text-slate-400 mt-0.5">
              Served model, provenance, and grouped-CV performance vs baselines — reported as measured.
            </p>
          </div>
          <button onClick={() => navigate('/dashboard')} className="flex items-center gap-1.5 min-h-[44px] text-xs text-slate-400 hover:text-white transition-colors">
            <ArrowLeft className="w-3.5 h-3.5" /> Back to Dashboard
          </button>
        </div>

        {error && (
          <div className="bg-red-900/30 border border-red-700/50 rounded-lg p-4 text-sm text-red-300 mb-6">{error}</div>
        )}

        {/* ── Provenance strip ────────────────────────────────────────────── */}
        {info && (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-8">
            <InfoTile label="Estimator" value={info.model_name ?? '—'} />
            <InfoTile
              label="Source"
              value={info.model_source === 'mlflow_registry' ? 'MLflow' : info.model_source === 'local_joblib' ? 'Local joblib' : 'None'}
              sub={info.alias ?? undefined}
            />
            <InfoTile label="Version" value={info.model_version ?? '—'} />
            <InfoTile label="Training rows" value={info.n_training_samples != null ? String(info.n_training_samples) : '—'} />
            <InfoTile label="Features" value={info.n_features != null ? String(info.n_features) : '—'} />
          </div>
        )}
        {/* The full serve-time provenance string is the honest answer to "which
            model served this, and why not the registry?" — but it is a paragraph
            of machine detail (and, before the serve layer relativized them, of
            server paths). It belongs one click away, not above the fold. */}
        {info?.detail && (
          <details className="-mt-6 mb-8 group">
            <summary className="min-h-[44px] flex items-center text-xs text-slate-400 hover:text-slate-300 cursor-pointer inline-flex items-center gap-1.5 list-none marker:content-['']">
              <Database className="w-3.5 h-3.5 shrink-0 text-slate-400" />
              <span className="underline decoration-dotted underline-offset-2">Provenance details</span>
              <ChevronRight className="w-3 h-3 transition-transform group-open:rotate-90" />
            </summary>
            <div className="mt-2 bg-slate-900/60 border border-slate-700/60 rounded-lg p-3 space-y-2">
              <p className="text-sm text-slate-400 leading-relaxed">{cleanProse(info.detail)}</p>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 text-sm">
                {([
                  ['Artifact', info.model_uri],
                  ['Artifact modified', info.artifact_mtime],
                  ['Resolved at', info.resolved_at],
                  ['Selection metric', info.selection_metric],
                ] as [string, string | null][])
                  .filter(([, v]) => v)
                  .map(([k, v]) => (
                    <div key={k} className="flex gap-2 min-w-0">
                      <dt className="text-slate-400 shrink-0">{k}</dt>
                      <dd className="text-slate-300 truncate" title={v ?? undefined}>{v}</dd>
                    </div>
                  ))}
              </dl>
            </div>
          </details>
        )}

        {/* ── Lead-time bake-off ──────────────────────────────────────────── */}
        {comparison && (
          <section className="mb-10">
            <h2 className="text-sm font-semibold text-white flex items-center gap-2 mb-1">
              <GitBranch className="w-4 h-4 text-sky-400" /> Factory Lead-Time Model — Bake-off
            </h2>
            <p className="text-sm text-slate-400 mb-4 leading-relaxed">{cleanProse(comparison.evaluation)}</p>

            {comparison.beats_all_baselines !== null && (
              <div className="mb-3">
                <span className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full ${
                  comparison.beats_all_baselines
                    ? 'bg-emerald-500/10 border border-emerald-500/30 text-emerald-400'
                    : 'bg-slate-700/40 border border-slate-600/40 text-slate-400'
                }`}>
                  {comparison.beats_all_baselines ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                  {comparison.beats_all_baselines
                    ? `Beats all ${comparison.baselines.length} naive baselines on ${comparison.selection_metric}`
                    : `Does not beat every baseline on ${comparison.selection_metric}`}
                </span>
              </div>
            )}

            <div className="flex flex-wrap gap-3 mb-5">
              <HeroStat
                label="CV R² (grouped by part family, median)"
                value={fmt(served?.cv_r2_median)}
                accent="neutral"
                note={
                  served
                    ? [
                        meanVsMedian
                          ? `The mean is ${meanVsMedian} the median: ${fmt(served.cv_r2_mean)} ± ${fmt(served.cv_r2_std)}` +
                            `${served.cv_splits != null ? ` over ${served.cv_splits} grouped splits` : ''}.`
                          : `Mean ${fmt(served.cv_r2_mean)} ± ${fmt(served.cv_r2_std)}` +
                            `${served.cv_splits != null ? ` over ${served.cv_splits} grouped splits` : ''}.`,
                        r2Spread
                          ? `Fold-to-fold spread is ${r2Spread} the mean itself, so neither figure alone is a summary.`
                          : '',
                        'Splits are grouped by part family for the reason given in the caveat below; read this next to the paired comparison, not on its own.',
                      ].filter(Boolean).join(' ')
                    : 'No served model metrics available.'
                }
              />
              <HeroStat
                label={`Paired ${pairedLabelVerb} vs "${comparison.toughest_baseline ?? 'toughest baseline'}"`}
                value={pairedValue}
                accent={pairedIsWin ? 'positive' : 'neutral'}
                note={
                  num(paired.folds_model_won) !== null
                    ? `Won ${paired.folds_model_won}/${paired.n_folds ?? '—'} identical grouped CV folds` +
                      `${num(paired.paired_t_p_value) !== null ? ` (p=${paired.paired_t_p_value})` : ''}. ` +
                      `${paired.caveat ? capFirst(cleanProse(String(paired.caveat))) : 'A paired test on identical folds is the like-for-like read; the CV R² beside it is not.'}`
                    : 'No paired comparison recorded.'
                }
              />
            </div>

            <div className="bg-slate-800/70 border border-slate-700 rounded-xl p-5 mb-4">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">CV R² mean — served model vs every baseline</h3>
                <span className="text-xs text-slate-400">selection metric: {comparison.selection_metric}</span>
              </div>
              <R2Chart models={comparison.models} baselines={comparison.baselines} />
            </div>

            {comparison.feature_columns.length > 0 && (
              <details className="mb-4">
                <summary className="cursor-pointer min-h-[44px] flex items-center text-xs font-semibold text-slate-300 uppercase tracking-wider hover:text-white transition-colors">
                  Feature schema v{comparison.feature_schema_version} ({featureMix})
                </summary>
                <div className="flex flex-wrap gap-1.5 mt-3">
                  {comparison.feature_columns.map((f) => (
                    <span key={f} className="text-xs font-mono bg-slate-800 border border-slate-700 text-slate-300 px-2 py-1 rounded">
                      {f}
                    </span>
                  ))}
                </div>
              </details>
            )}

            {comparison.feature_exclusions.length > 0 && (
              <div className="mb-4">
                <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">
                  Declared candidates excluded ({comparison.feature_exclusions.length})
                </h3>
                <div className="space-y-2.5">
                  {exclusionGroups.map((g, i) => (
                    <div key={i} className="text-sm text-slate-400">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mb-0.5">
                        {g.features.map((f, j) => (
                          <span key={j} className="inline-flex items-center gap-1">
                            <span className="font-mono text-slate-300">{f.feature}</span>
                            {f.kind != null && <span className="text-slate-400">({f.kind})</span>}
                            {j < g.features.length - 1 && <span className="text-slate-400">·</span>}
                          </span>
                        ))}
                      </div>
                      <span className="leading-relaxed">— {capFirst(cleanProse(g.reason))}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {leakage && (
              <div className="bg-slate-800/70 border border-amber-500/30 rounded-xl p-5 mb-4">
                <div className="flex items-center justify-between mb-1 gap-3">
                  <h3 className="text-xs font-semibold text-amber-300 uppercase tracking-wider">
                    Leakage audit — the same estimator, three split protocols
                  </h3>
                  {leakage.n_splits != null && (
                    <span className="text-xs text-slate-400 shrink-0">
                      mean over {leakage.n_splits} repeated group-shuffle splits
                    </span>
                  )}
                </div>
                <p className="text-sm text-slate-400 mb-3 leading-relaxed">
                  Computed on every retrain and read here straight from the served artifact
                  {leakage.model ? ` (${leakage.model}` : ''}
                  {leakage.model && leakage.n_rows != null ? `, ${leakage.n_rows.toLocaleString()} rows` : ''}
                  {leakage.model && leakage.n_manufacturers != null ? `, ${leakage.n_manufacturers} manufacturers` : ''}
                  {leakage.model ? ')' : ''}. Only the grouping changes between the three.
                </p>
                <div className="flex flex-wrap gap-2">
                  <LeakageRung
                    label="Random split"
                    protocolNote="rows shuffled — siblings of one part family straddle the split, so this scores recognition"
                    value={leakage.random}
                    protocol="optimistic"
                  />
                  <LeakageRung
                    label="Grouped by part family"
                    protocolNote="the protocol every metric on this page uses"
                    value={leakage.family}
                    protocol="reported"
                  />
                  <LeakageRung
                    label="Whole manufacturers held out"
                    protocolNote="the question deployment actually asks: a part from a vendor never quoted"
                    value={leakage.manufacturer}
                    protocol="deployment"
                  />
                </div>
                {leakage.headline && (
                  <p className="text-sm text-slate-400 leading-relaxed mt-3">{cleanProse(leakage.headline)}</p>
                )}
              </div>
            )}

            <div className="bg-slate-900/60 border border-slate-700/60 rounded-lg p-3">
              <p className="text-base text-slate-300 leading-relaxed">{capFirst(cleanProse(comparison.caveat))}</p>
            </div>
            {!comparison.metrics_describe_served_model && (
              <p className="text-sm text-amber-400 mt-2">
                These metrics do NOT describe the currently deployed model — see caveat above.
              </p>
            )}
          </section>
        )}

        {/* ── Macro regime model ──────────────────────────────────────────── */}
        {stress && (
          <section>
            <h2 className="text-sm font-semibold text-white flex items-center gap-2 mb-1">
              <Activity className="w-4 h-4 text-sky-400" /> Macro Stress Regime Model
            </h2>
            <p className="text-sm text-slate-400 mb-4 leading-relaxed">
              Forecasts the NY Fed GSCPI regime one month ahead. The optimizer consumes the probability directly,
              not a label
              {stress.ship_gate_policy
                ? `, and the ship gate is scored on ${cleanProse(stress.ship_gate_policy)}${gateIsBrier ? ' — a proper scoring rule — not accuracy' : ''}`
                : ''}.
            </p>

            <div className="flex flex-wrap items-center gap-3 mb-4">
              <GatePill passed={stress.ship_gate_passed} />
              {stress.ship_gate_policy && <span className="text-xs text-slate-400">{cleanProse(stress.ship_gate_policy)}</span>}
            </div>

            {/* The headline tile is the metric the model is SHIPPED on and wins
                on — Brier. Accuracy used to hold this slot while being an exact
                tie with persistence, i.e. the most prominent number on the page
                was the one carrying no skill. It stays visible below, captioned
                for what it is, because hiding it would be the other failure. */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
              <InfoTile
                emphasis
                label="Brier score — the ship gate"
                value={fmt(stress.brier, BRIER_DIGITS)}
                sub={(() => {
                  const parts = [
                    stress.baseline_brier != null ? `persistence ${fmt(stress.baseline_brier, BRIER_DIGITS)}` : null,
                    stress.climatology_brier != null ? `climatology ${fmt(stress.climatology_brier, BRIER_DIGITS)}` : null,
                  ].filter(Boolean).join(' · ');
                  const skill = brierSkill(stress.brier, stress.baseline_brier);
                  const tail = skill ? `${parts ? ' — ' : ''}${skill}` : '';
                  return `${parts}${tail}` || 'lower is better';
                })()}
              />
              {/* NOT an InfoTile. The observation month is set at the SAME size
                  and weight as the percentage, on the same line, because it is
                  part of the claim rather than a footnote to it: this number
                  scores one row of a MONTHLY frame and describes that month,
                  not today. Shipping it as smaller print underneath is the
                  exact mistake this page has made before. */}
              <div
                data-testid="stress-figure"
                className="rounded-xl p-4 flex flex-col gap-1 border bg-slate-800/70 border-slate-700"
              >
                <span className="text-xs font-medium uppercase tracking-wider text-slate-400">
                  Stress probability
                </span>
                <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span data-testid="stress-probability" className="text-xl font-bold tabular-nums text-white">
                    {stress.available ? pct(stress.stress_probability) : '—'}
                  </span>
                  <span data-testid="stress-vintage" className="text-xl font-bold tabular-nums text-amber-300">
                    {stressObservationMonth(stress) ?? 'vintage unknown'}
                  </span>
                </span>
                <span className="text-sm text-slate-400">
                  {stress.stress_level}
                  {stress.observation_age_days != null
                    ? ` · ${stress.observation_age_days} days old`
                    : ''}
                </span>
              </div>
              <InfoTile label="Calibration slope" value={fmt(stress.calibration_slope, 3)} sub="1.0 = perfectly calibrated" />
              <InfoTile label="Shortage recall" value={pct(stress.shortage_recall, 1)} />
            </div>

            {/* Accuracy, demoted but not deleted. */}
            <div className="bg-slate-900/60 border border-slate-700/60 rounded-lg p-3 mb-5">
              <div className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-1">
                Also measured — accuracy (reported, not the gate)
              </div>
              <p className="text-base text-slate-300 leading-relaxed">
                <span className="tabular-nums text-slate-200">{pct(stress.val_accuracy, 1)}</span>
                {stress.baseline_accuracy != null && (
                  <> vs persistence baseline <span className="tabular-nums text-slate-200">{pct(stress.baseline_accuracy, 1)}</span></>
                )}
                {(() => {
                  const verdict = accuracyVerdict(
                    stress.accuracy_delta_vs_baseline, stress.val_accuracy, stress.baseline_accuracy,
                  );
                  const tie = accuracyIsTie(
                    stress.accuracy_delta_vs_baseline, stress.val_accuracy, stress.baseline_accuracy,
                  );
                  if (!verdict) return null;
                  return <> — the model {verdict}{tie ? ', i.e. no accuracy skill over the naive baseline' : ''}.</>;
                })()}
                {' '}A label metric cannot see the probability the optimizer actually prices, and persistence
                can only ever emit 0 or 1, so accuracy is published for completeness rather than used to decide
                whether this model ships.
              </p>
            </div>

            <div className="bg-slate-800/70 border border-slate-700 rounded-xl p-5 mb-4">
              <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-3">
                Brier score — model vs baselines (lower is better{gateIsBrier ? '; this is the metric that gates shipping' : ''})
              </h3>
              <BrierChart stress={stress} />
            </div>

            <div className="bg-slate-900/60 border border-slate-700/60 rounded-lg p-3">
              <p className="text-base text-slate-300 leading-relaxed">{capFirst(cleanProse(stress.interpretation))}</p>
              {stress.ship_gate_reason && (
                <p className="text-sm text-slate-400 leading-relaxed mt-1">{capFirst(cleanProse(stress.ship_gate_reason))}</p>
              )}
            </div>
          </section>
        )}

        {!comparison && !stress && !error && (
          <div className="text-center py-16 text-slate-400 text-sm">No ML data available.</div>
        )}
      </main>
    </div>
  );
}
