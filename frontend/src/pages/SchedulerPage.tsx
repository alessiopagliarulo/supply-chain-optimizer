import { useEffect, useState, useCallback } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, ReferenceArea } from 'recharts';
import { RefreshCw, Zap, ShieldCheck, ShieldAlert, CheckCircle2, AlertTriangle, Settings } from 'lucide-react';
import { componentsAPI, livePricesAPI, demandAPI, isTimeoutError } from '../services/api';
import type { LivePriceResponse, DemandBenchmarkResponse, LiveSourceReport } from '../services/api';
import { useElapsedSeconds } from '../services/warmup';
import WakeNotice from '../components/WakeNotice';
import { useCartStore } from '../store/cartStore';
import {
  catalogueRiskTier, formatRiskIndex, formatRiskFactor,
  CATALOGUE_RISK_LABELS, RISK_INDEX_SCALE, RISK_INDEX_NOTE,
  type CatalogueRiskTier,
} from '../lib/risk';

interface ComponentItem {
  id: number;
  mpn: string;
  manufacturer: string;
  manufacturer_country: string | null;
  category: string;
  description: string | null;
  risk_score: number;
  risk_factors: string[] | null;
  min_price: number | null;
  max_price: number | null;
  num_offers: number;
}

interface Offer {
  id: number;
  distributor_id: number;
  distributor_name: string;
  distributor_city: string | null;
  distributor_state: string | null;
  distributor_country: string | null;
  is_domestic: boolean;
  price: number;
  stock: number;
  sku: string | null;
  currency: string | null;
}

interface ComponentDetail {
  id: number;
  mpn: string;
  manufacturer: string;
  manufacturer_country: string | null;
  category: string;
  description: string | null;
  datasheets: string[] | null;
  risk_score: number;
  risk_factors: string[] | null;
  offers: Offer[];
}

// Component unit prices are genuinely sub-cent for passives (the catalog's
// cheapest real offer is $0.0031, and 46% of offers carry more than two
// decimals), so money is rendered with 2–4 decimals: always at least cents,
// extra digits only when the real price actually has them. That keeps
// "$16.16" from rendering as "$16.1600" while never rounding a part to $0.00.
//
// Both helpers take the offer's OWN currency and render it honestly — the
// static catalog is USD-only today, but live-pricing offers from European
// distributors (Schukat, Farnell) come back in EUR/GBP, and hardcoding "$" in
// front of a non-USD figure silently mislabels the unit of account. Currency
// defaults to USD only when the field is genuinely absent (e.g. an aggregate
// like a component's min/max price that isn't tied to one offer).
const fmtMoney = (n: number, currency: string | null | undefined, maximumFractionDigits: number) => {
  const code = (currency || 'USD').trim().toUpperCase();
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: code,
      minimumFractionDigits: 2,
      maximumFractionDigits,
    }).format(n);
  } catch {
    // Not a valid ISO 4217 code — still label the unit rather than silently
    // rendering a bare "$" in front of it.
    return `${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits })} ${code}`;
  }
};

const fmtUnitPrice = (n: number, currency?: string | null) => fmtMoney(n, currency, 4);

// Whole-dollar money (line/order totals) always shows exactly two decimals.
const fmtUsd = (n: number, currency?: string | null) => fmtMoney(n, currency, 2);

// ...except that two decimals turned a real amount into a confident "$0.00":
// at the default quantity of 1, the catalog's cheapest genuine offer ($0.0031)
// rendered "Estimated Total $0.00" directly beneath its own correct unit price.
// A total never renders a nonzero amount as zero; below a cent it borrows the
// unit-price precision instead.
const fmtTotal = (n: number, currency?: string | null) =>
  n > 0 && n < 0.005 ? fmtUnitPrice(n, currency) : fmtUsd(n, currency);

// A "% vs best price" or "Best Price" comparison across two offers is only
// meaningful when both are denominated in the same currency — comparing a raw
// EUR figure to a raw USD figure without conversion is exactly the kind of
// silent unit error this repo has shipped before.
const sameCurrency = (a: string | null | undefined, b: string | null | undefined) =>
  (a || 'USD').trim().toUpperCase() === (b || 'USD').trim().toUpperCase();

const plural = (n: number, singular: string, pluralForm = `${singular}s`) =>
  `${n.toLocaleString()} ${n === 1 ? singular : pluralForm}`;

// Tailwind classes per catalogue risk tier. The TIER itself comes from
// lib/risk.ts so this page and the Dashboard can never disagree — they used to:
// this file banded at 0.3/0.6 and Dashboard at 0.4/0.7, which put the same 13
// ESP8266 parts (risk_score 0.60) in red here and amber there. Both cut sets
// sat inside the score's empty interval (0.25, 0.60), so neither was testable.
const RISK_TIER_TEXT: Record<CatalogueRiskTier, string> = {
  unflagged:      'text-slate-300',
  flagged:        'text-yellow-400',
  origin_flagged: 'text-red-400',
};

const RISK_TIER_BADGE: Record<CatalogueRiskTier, string> = {
  unflagged:      'bg-slate-500/20 text-slate-300 border-slate-500/30',
  flagged:        'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  origin_flagged: 'bg-red-500/20 text-red-400 border-red-500/30',
};

// ── Live-pricing per-source status ──────────────────────────────────────────
// The "Live Pricing" panel claims four sources (Nexar, DigiKey, OEMsecrets,
// TrustedParts); in this deployment two of them never actually answer (Nexar's
// part-limit quota, TrustedParts unconfigured). The backend's per-source
// SourceReport already says exactly why — rendering it turns "the feature is
// half-broken" into "the feature gracefully degrades and says so."
const SOURCE_LABELS: Record<string, string> = {
  nexar: 'Nexar',
  digikey: 'DigiKey',
  oemsecrets: 'OEMsecrets',
  trustedparts: 'TrustedParts',
  mouser: 'Mouser',
};

function sourceStatusStyle(status: string) {
  switch (status) {
    case 'ok':
      return 'bg-green-500/10 text-green-400 border-green-500/30';
    case 'error':
      return 'bg-red-500/10 text-red-400 border-red-500/30';
    default:
      return 'bg-slate-700/40 text-slate-400 border-slate-600/40';
  }
}

function sourceStatusText(s: { status: string; offer_count: number; error: string | null }) {
  if (s.status === 'ok') return plural(s.offer_count, 'offer');
  if (s.status === 'not_configured') return 'not configured';
  if (s.status === 'skipped') return 'skipped';
  // Never echo the upstream string here. It is a vendor billing/quota message
  // ("Nexar: You have exceeded your part limit of 10...") and rendering it as a
  // status badge reads as our own product failing at the user. The raw detail is
  // still available: the call site puts it in `title` on hover.
  return 'unavailable';
}

// ── Demand model panel ──────────────────────────────────────────────────────
// Fed by demandAPI.benchmark() — the real intermittent-demand method
// benchmark that replaced the retired per-part forecasts API surface. It is a
// fleet-wide benchmark of demand *methods* on the Monash car-parts panel, not
// a per-part forecast for these electronic components (no public per-SKU
// demand series exists for them). See backend/app/api/demand.py.
function DemandModelPanel() {
  const [state, setState] = useState<'loading' | 'unavailable' | 'error' | 'loaded'>('loading');
  const [data, setData] = useState<DemandBenchmarkResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    demandAPI
      .benchmark()
      .then((res) => {
        setData(res.data);
        setState('loaded');
      })
      .catch((err: unknown) => {
        const axiosErr = err as { response?: { status?: number; data?: { detail?: string } } };
        if (axiosErr?.response?.status === 503) {
          setErrorMsg(axiosErr.response?.data?.detail || 'Demand benchmark not available in this deployment.');
          setState('unavailable');
        } else {
          setErrorMsg('Failed to load demand benchmark.');
          setState('error');
        }
      });
  }, []);

  if (state === 'loading') {
    return (
      <div className="border-b border-slate-700 bg-slate-800/60 px-5 py-3 text-xs text-slate-400">
        Loading demand model benchmark…
      </div>
    );
  }

  if (state === 'unavailable' || state === 'error') {
    return (
      <div className="border-b border-slate-700 bg-slate-800/60 px-5 py-3">
        <div className="flex items-center gap-2 text-xs text-amber-400">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          <span>
            {state === 'unavailable'
              ? 'Demand model benchmark not available in this deployment.'
              : 'Demand model benchmark failed to load.'}
            {errorMsg ? ` ${errorMsg}` : ''}
          </span>
        </div>
      </div>
    );
  }

  if (!data) return null;

  const crpsMcb = data.mcb.find((m) => m.metric === 'crps') ?? null;
  const bestCrpsRank = data.methods.length > 0 ? Math.min(...data.methods.map((m) => m.rank_crps)) : null;
  // Proper-scoring metrics lead; MASE (the metric this panel exists to caveat) trails.
  const rankChartData = data.methods.map((m) => ({
    name: m.name,
    'Rank (CRPS)': m.rank_crps,
    'Rank (SPL)': m.rank_spl,
    'Rank (RMSSE)': m.rank_rmsse,
    'Rank (MASE)': m.rank_mase,
  }));

  const metricLabel = (m: string) => (m === 'mase' || m === 'rmsse' || m === 'crps' || m === 'spl' ? m.toUpperCase() : m);
  const testLabel = (t: string) =>
    t === 'clark_west' ? 'Clark–West' : t === 'diebold_mariano' ? 'Diebold–Mariano (HLN)' : t;
  const fmtP = (p: number) => (p < 0.001 ? '< 0.001' : p.toFixed(4));

  return (
    <div className="border-b border-slate-700 bg-slate-800/40">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-5 py-3 text-left hover:bg-slate-700/30 transition-colors"
      >
        <div className="min-w-0">
          <div className="text-sm font-semibold text-white">Demand model</div>
          <div className="text-xs text-slate-400 line-clamp-2 mt-0.5">{data.headline}</div>
          {/* Visible collapsed, not just in the expanded disclosure below — this sits
              directly above an "All (N)" list of the electronic components in this
              catalog, so the caveat that the benchmark below is on a DIFFERENT panel
              (Monash car parts) must not require a click to see. */}
          <div className="text-xs text-amber-300 mt-1">
            Benchmarked on Monash car-parts sales data — not these electronic components.
          </div>
        </div>
        <span className="text-xs text-slate-400 shrink-0 ml-3">{expanded ? 'Hide details −' : 'Show details +'}</span>
      </button>

      {expanded && (
        <div className="px-5 pb-5 space-y-4">
          {/* Framing */}
          <p className="text-xs text-slate-400 bg-slate-900/40 border border-slate-700/60 rounded-lg p-3">
            This is a benchmark of intermittent-demand <em>methods</em>, scored on a real spare-parts
            sales panel (car parts, not electronics) — it is not a per-part demand forecast for the
            components in this catalog. No public per-SKU demand series exists for these electronic
            components, so none is claimed here.
          </p>

          {/* Point vs proper-scoring winner callout */}
          <div
            className={`flex items-start gap-2 text-xs rounded-lg p-3 border ${
              data.ranking_changed
                ? 'bg-amber-500/5 border-amber-500/20 text-amber-300'
                : 'bg-emerald-500/5 border-emerald-500/20 text-emerald-300'
            }`}
          >
            {data.ranking_changed ? (
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            ) : (
              <CheckCircle2 className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            )}
            <span>
              {data.ranking_changed ? (
                <>
                  The point-accuracy (MASE) winner is <strong>{data.point_winner}</strong>, but the
                  proper-scoring (CRPS) winner is <strong>{data.distributional_winner}</strong> — picking
                  by MASE alone would choose the wrong method for modeling the full demand distribution.
                </>
              ) : (
                <>
                  The ranking is unchanged under proper scoring — MASE and CRPS agree that{' '}
                  <strong>{data.point_winner}</strong> is best.
                </>
              )}
            </span>
          </div>

          {/* Leaderboard — proper-scoring columns (CRPS, SPL) lead; MASE, the metric
              this whole panel exists to caveat, trails rather than heads the table. */}
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-400 uppercase tracking-wider border-b border-slate-700">
                  <th className="text-left py-1.5 pr-3">Method</th>
                  <th className="text-right py-1.5 px-2">CRPS mean</th>
                  <th className="text-right py-1.5 px-2">Rank (CRPS)</th>
                  <th className="text-right py-1.5 px-2">SPL mean</th>
                  <th className="text-right py-1.5 px-2">MASE mean</th>
                  <th className="text-right py-1.5 px-2">MASE median</th>
                  <th className="text-right py-1.5 pl-2">Rank (MASE)</th>
                </tr>
              </thead>
              <tbody>
                {data.methods.map((m) => (
                  <tr key={m.name} className="border-b border-slate-800">
                    <td className="py-1.5 pr-3">
                      <span
                        className="text-white cursor-help border-b border-dotted border-slate-400"
                        title={m.assumption}
                      >
                        {m.name}
                      </span>
                      {m.name === data.distributional_winner && (
                        <span className="ml-1.5 text-[11px] px-1 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
                          CRPS best
                        </span>
                      )}
                      {m.name === data.point_winner && (
                        <span className="ml-1.5 text-[11px] px-1 py-0.5 rounded bg-blue-500/20 text-blue-400 border border-blue-500/30">
                          MASE best
                        </span>
                      )}
                    </td>
                    <td className="text-right py-1.5 px-2 text-slate-300">{m.crps_mean.toFixed(3)}</td>
                    <td className="text-right py-1.5 px-2 text-slate-300">{m.rank_crps.toFixed(2)}</td>
                    <td className="text-right py-1.5 px-2 text-slate-300">{m.spl_mean.toFixed(3)}</td>
                    <td className="text-right py-1.5 px-2 text-slate-400">{m.mase_mean.toFixed(3)}</td>
                    <td className="text-right py-1.5 px-2 text-slate-400">{m.mase_median.toFixed(3)}</td>
                    <td className="text-right py-1.5 pl-2 text-slate-400">{m.rank_mase.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mean rank comparison, all 4 scored metrics — real returned numbers, no
              synthetic data. The shaded band is the Nemenyi critical difference around
              the best CRPS rank: any bar falling inside it is not statistically
              distinguishable from the winner. */}
          <div className="bg-slate-900/40 border border-slate-700/60 rounded-lg p-3">
            <div className="text-xs text-slate-400 mb-2">Mean Friedman rank by metric (lower = better)</div>
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={rankChartData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis dataKey="name" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} label={{ value: 'mean rank', angle: -90, position: 'insideLeft', fill: '#94a3b8', fontSize: 10 }} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#0f172a',
                    border: '1px solid #475569',
                    borderRadius: '8px',
                    padding: '12px',
                    fontSize: '12px',
                  }}
                />
                <Legend wrapperStyle={{ fontSize: '11px' }} />
                {crpsMcb && bestCrpsRank != null && (
                  <ReferenceArea
                    y1={bestCrpsRank}
                    y2={bestCrpsRank + crpsMcb.critical_difference}
                    ifOverflow="extendDomain"
                    fill="#34d399"
                    fillOpacity={0.12}
                    stroke="#34d399"
                    strokeOpacity={0.5}
                    strokeDasharray="4 4"
                    label={{ value: 'CD band (CRPS)', position: 'insideTopRight', fill: '#34d399', fontSize: 10 }}
                  />
                )}
                <Bar dataKey="Rank (CRPS)" fill="#34d399" />
                <Bar dataKey="Rank (SPL)" fill="#f59e0b" />
                <Bar dataKey="Rank (RMSSE)" fill="#a78bfa" />
                <Bar dataKey="Rank (MASE)" fill="#60a5fa" />
              </BarChart>
            </ResponsiveContainer>
            {crpsMcb && (
              <p className="text-[11px] text-slate-400 mt-2">
                Shaded band: methods whose CRPS rank falls inside it are statistically indistinguishable
                from the best CRPS method (Nemenyi critical difference = {crpsMcb.critical_difference.toFixed(3)},
                α = {crpsMcb.alpha}, n = {crpsMcb.n_series.toLocaleString()} series).
              </p>
            )}
          </div>

          {/* MCB — all 4 metrics. CD is identical across metrics because it depends
              only on the number of methods and series compared (Nemenyi), not on which
              metric is being ranked — this is a real property of the test, not a bug. */}
          <div className="bg-slate-900/40 border border-slate-700/60 rounded-lg p-3">
            <div className="text-slate-300 font-medium text-xs mb-1">
              Friedman rank test + Nemenyi critical differences, all 4 metrics
            </div>
            <p className="text-[11px] text-slate-400 mb-2">
              Two methods whose mean ranks differ by less than the critical difference (CD) are
              statistically indistinguishable at the stated α — a smaller Friedman p means the method
              ranks are very unlikely to be a coincidence of sampling.
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-slate-400 uppercase tracking-wider border-b border-slate-700">
                    <th className="text-left py-1.5 pr-3">Metric</th>
                    <th className="text-right py-1.5 px-2">Friedman χ²</th>
                    <th className="text-right py-1.5 px-2">p</th>
                    <th className="text-right py-1.5 px-2">CD (α)</th>
                    <th className="text-left py-1.5 pl-2">Statistically tied</th>
                  </tr>
                </thead>
                <tbody>
                  {data.mcb.map((m) => (
                    <tr key={m.metric} className="border-b border-slate-800">
                      <td className="py-1.5 pr-3 text-white">{metricLabel(m.metric)}</td>
                      <td className="text-right py-1.5 px-2 text-slate-300">{m.friedman_chi2.toFixed(1)}</td>
                      <td className="text-right py-1.5 px-2 text-slate-300">{fmtP(m.friedman_p)}</td>
                      <td className="text-right py-1.5 px-2 text-slate-400">
                        {m.critical_difference.toFixed(3)} ({m.alpha})
                      </td>
                      <td className="py-1.5 pl-2 text-slate-300">
                        {m.cliques.length > 0 ? (
                          m.cliques.map((c) => c.join(' ≈ ')).join('; ')
                        ) : (
                          <span className="text-slate-400">none — every pair significantly differs</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Significance tests — Clark–West on the genuinely nested comparisons,
              HLN-corrected Diebold–Mariano on the rest. Some Clark–West rows are
              self-flagged DEGENERATE by the backend: the test statistic there is
              mechanically forced regardless of which method actually wins, so it
              carries no evidence — that self-flagging is the statistically senior
              move, and hiding it would understate the analysis, not simplify it. */}
          {data.significance.length > 0 && (
            <div className="bg-slate-900/40 border border-slate-700/60 rounded-lg p-3">
              <div className="text-slate-300 font-medium text-xs mb-1">Pairwise significance tests</div>
              <p className="text-[11px] text-slate-400 mb-2">
                Hover a row for the full explanation. A <span className="text-amber-400 font-medium">DEGENERATE</span> flag
                means the comparison is mathematically forced (e.g. against the zero forecast) and should
                be read as uninformative, not as evidence either method is better.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-400 uppercase tracking-wider border-b border-slate-700">
                      <th className="text-left py-1.5 pr-3">Comparison</th>
                      <th className="text-left py-1.5 px-2">Test</th>
                      <th className="text-right py-1.5 px-2">Statistic</th>
                      <th className="text-right py-1.5 px-2">p</th>
                      <th className="text-left py-1.5 pl-2">Flag</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.significance.map((row, i) => {
                      const degenerate = row.note.trim().toUpperCase().startsWith('DEGENERATE');
                      return (
                        <tr key={`${row.test}-${row.a}-${row.b}-${i}`} className="border-b border-slate-800" title={row.note}>
                          <td className="py-1.5 pr-3 text-white cursor-help border-b border-dotted border-slate-400">
                            {row.a} vs {row.b}
                          </td>
                          <td className="py-1.5 px-2 text-slate-300">{testLabel(row.test)}</td>
                          <td className="text-right py-1.5 px-2 text-slate-300">{row.statistic.toFixed(2)}</td>
                          <td className="text-right py-1.5 px-2 text-slate-300">{fmtP(row.p_value)}</td>
                          <td className="py-1.5 pl-2">
                            {degenerate ? (
                              <span className="text-[11px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30 cursor-help">
                                DEGENERATE
                              </span>
                            ) : (
                              <span className="text-slate-400">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Dataset provenance */}
          <div className="text-xs text-slate-400 bg-slate-900/40 border border-slate-700/60 rounded-lg p-3 space-y-1">
            <div>
              <span className="text-slate-300 font-medium">{data.dataset.name}</span> — {data.dataset.source} (
              {data.dataset.license})
            </div>
            <div>
              {data.dataset.n_series.toLocaleString()} series × {data.dataset.series_length} periods,{' '}
              {(data.dataset.nonzero_fraction * 100).toFixed(1)}% non-zero
            </div>
            <div className="font-mono text-[11px] text-slate-400 mt-1.5 bg-slate-950/60 rounded px-2 py-1 overflow-x-auto">
              {data.reproduce_command}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SchedulerPage() {
  const { addItem } = useCartStore();
  const [components, setComponents] = useState<ComponentItem[]>([]);
  const [categories, setCategories] = useState<{ name: string; count: number }[]>([]);
  const [selectedCat, setSelectedCat] = useState('All');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<ComponentDetail | null>(null);
  const [qty, setQty] = useState(1);
  const [selectedOfferId, setSelectedOfferId] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const [addedMsg, setAddedMsg] = useState('');
  // Errors get their own state: a success message may fade, a failure must not.
  const [addError, setAddError] = useState('');
  const [loading, setLoading] = useState(true);
  // Set only when the catalogue request actually FAILED — never inferred from an
  // empty list. Without it the page rendered its ordinary success layout over `[]`
  // after any fetch error: "No components found", "0 of 0 components" and "Browse 0
  // electronic components with real distributor pricing" — three confident zeros
  // for a request that never came back, and no way to retry.
  const [catalogueError, setCatalogueError] = useState<string | null>(null);
  // Real elapsed time on the in-flight catalogue request, so a free-tier cold start
  // can say so (same WakeNotice the login screen and the auth splash use) instead of
  // showing a static "Loading components..." for up to two minutes.
  const [loadStartedAt, setLoadStartedAt] = useState<number | null>(null);
  const loadSec = useElapsedSeconds(loadStartedAt);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [failedComponent, setFailedComponent] = useState<ComponentItem | null>(null);
  const [domesticOnly, setDomesticOnly] = useState(false);

  // ── Live pricing (Nexar / DigiKey / OEMsecrets / TrustedParts) ─────────────
  // Pulled on demand — the static offers above are a frozen 2024 HuggingFace
  // snapshot; this hits real distributor APIs right now.
  const [liveState, setLiveState] = useState<'idle' | 'loading' | 'loaded' | 'not_found' | 'unconfigured' | 'error'>('idle');
  const [liveData, setLiveData] = useState<LivePriceResponse | null>(null);
  const [liveErrorMsg, setLiveErrorMsg] = useState<string>('');
  // Per-source outcome (ok / error / not_configured), captured on BOTH the success
  // path and the 404/503/502 error paths — the backend's structured `detail.sources`
  // carries this even when the overall call "failed", and dropping it there hid the
  // fact that e.g. DigiKey answered fine while Nexar hit a rate limit.
  const [liveSources, setLiveSources] = useState<LiveSourceReport[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState('');

  const loadCatalogue = useCallback(async () => {
    setLoading(true);
    setCatalogueError(null);
    setLoadStartedAt(performance.now());
    try {
      const [cRes, catRes] = await Promise.all([
        componentsAPI.list(),
        componentsAPI.categories(),
      ]);
      setComponents(cRes.data);
      setCategories(catRes.data);
    } catch (err: unknown) {
      console.error('Initial load failed:', err);
      const status = (err as { response?: { status?: number } })?.response?.status;
      setComponents([]);
      setCategories([]);
      setCatalogueError(
        isTimeoutError(err)
          ? 'The backend did not respond in time. It runs on a free tier and can take up to ~2 minutes to wake from sleep.'
          : `The catalogue request failed${status ? ` (HTTP ${status})` : ''}.`,
      );
    } finally {
      setLoading(false);
      setLoadStartedAt(null);
    }
  }, []);

  useEffect(() => { void loadCatalogue(); }, [loadCatalogue]);

  const selectComponent = useCallback(async (comp: ComponentItem) => {
    setSelectedOfferId(null);
    setQty(1);
    setAddedMsg('');
    setAddError('');
    setDetailLoading(true);
    setDetailError(null);
    // Reset live-price state — it's per-component and must not leak across selections.
    setLiveState('idle');
    setLiveData(null);
    setLiveErrorMsg('');
    setLiveSources([]);
    setSyncMsg('');
    try {
      const res = await componentsAPI.get(comp.id);
      setSelected(res.data);
      setFailedComponent(null);
    } catch (err: unknown) {
      // Without this the request could reject, leave "Loading offers..." on screen
      // forever and surface as an unhandled promise rejection.
      const axiosErr = err as { response?: { status?: number; data?: { detail?: unknown } } };
      const detail = axiosErr?.response?.data?.detail;
      setSelected(null);
      setFailedComponent(comp);
      setDetailError(
        typeof detail === 'string' && detail.trim()
          ? detail
          : `Could not load offers for ${comp.mpn}${axiosErr?.response?.status ? ` (HTTP ${axiosErr.response.status})` : ''}.`,
      );
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const fetchLivePrices = useCallback(async () => {
    if (!selected) return;
    setLiveState('loading');
    setLiveErrorMsg('');
    try {
      const res = await livePricesAPI.get(selected.mpn);
      setLiveData(res.data);
      setLiveSources(res.data.sources ?? []);
      setLiveState('loaded');
    } catch (err: unknown) {
      const axiosErr = err as { response?: { status?: number; data?: { detail?: unknown } } };
      const status = axiosErr?.response?.status;
      const rawDetail = axiosErr?.response?.data?.detail;
      // The backend's 404/503/502 responses carry a STRUCTURED detail object —
      // { message, sources, all_sources_failed? } — not a plain string. Treating
      // `detail` as a string here (the previous behavior) silently discarded the
      // per-source report on every non-2xx response, which is exactly when it's
      // most useful (it explains *why* the call failed).
      const detailObj =
        rawDetail && typeof rawDetail === 'object' ? (rawDetail as { message?: string; sources?: LiveSourceReport[] }) : null;
      const detailMsg = typeof rawDetail === 'string' ? rawDetail : detailObj?.message;
      setLiveSources(detailObj?.sources ?? []);
      if (status === 404) {
        setLiveState('not_found');
        setLiveErrorMsg(detailMsg || `No live offers found for ${selected.mpn} today.`);
      } else if (status === 503) {
        setLiveState('unconfigured');
        setLiveErrorMsg(detailMsg || 'No live pricing sources configured.');
      } else if (status === 502) {
        setLiveState('error');
        setLiveErrorMsg(detailMsg || 'All configured live pricing sources failed for this part — try again.');
      } else {
        setLiveState('error');
        setLiveErrorMsg(detailMsg || 'Live price lookup failed — try again.');
      }
    }
  }, [selected]);

  const syncToDatabase = useCallback(async () => {
    if (!selected) return;
    setSyncing(true);
    setSyncMsg('');
    try {
      const res = await livePricesAPI.sync(selected.mpn);
      const { db_offers_updated = 0, db_offers_created = 0 } = res.data;
      if (db_offers_updated || db_offers_created) {
        setSyncMsg(`Saved: ${plural(db_offers_updated, 'offer')} updated, ${db_offers_created} created.`);
        // Re-pull the component so the static offers list reflects the new prices.
        const refreshed = await componentsAPI.get(selected.id);
        setSelected(refreshed.data);
      } else {
        setSyncMsg(res.data.message || 'No matching distributors in the catalog to update.');
      }
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } } };
      setSyncMsg(axiosErr?.response?.data?.detail || 'Sync failed — try again.');
    } finally {
      setSyncing(false);
    }
  }, [selected]);

  const selectedOffer = selected?.offers.find((o) => o.id === selectedOfferId) ?? null;

  // The server rejects these with a 422 (see POST /api/v1/cart). Knowing why the
  // button is dead is the difference between "out of stock" and "the site is
  // broken", so the same rule is evaluated here and stated on the button.
  const outOfStock = !!selectedOffer && selectedOffer.stock === 0;
  const overStock = !!selectedOffer && selectedOffer.stock > 0 && qty > selectedOffer.stock;
  // The cart has no currency column and books every line as USD, so the API
  // refuses a non-USD offer (422). Surfacing that as a disabled button with the
  // reason on it beats letting them fill in a quantity and hit a wall.
  const nonUsdOffer =
    !!selectedOffer && (selectedOffer.currency || 'USD').trim().toUpperCase() !== 'USD';
  const canAdd =
    !!selectedOffer && !outOfStock && !overStock && !nonUsdOffer && qty >= 1;

  const handleAddToCart = async () => {
    if (!selected || !selectedOffer || !canAdd) return;
    setAdding(true);
    setAddError('');
    try {
      await addItem({
        component_id: selected.id,
        distributor_id: selectedOffer.distributor_id,
        quantity: qty,
        unit_price: selectedOffer.price,
      });
      setAddedMsg('Added to cart!');
      setTimeout(() => setAddedMsg(''), 2500);
    } catch (err: unknown) {
      // Stays on screen until the next attempt — no timer, no silent 422.
      setAddError(err instanceof Error && err.message ? err.message : 'Failed to add to cart');
      setAddedMsg('');
    } finally {
      setAdding(false);
    }
  };

  // Filter + search
  const visible = components.filter((c) => {
    const catOk = selectedCat === 'All' || c.category === selectedCat;
    const searchOk =
      !search ||
      c.mpn.toLowerCase().includes(search.toLowerCase()) ||
      c.manufacturer.toLowerCase().includes(search.toLowerCase()) ||
      (c.description || '').toLowerCase().includes(search.toLowerCase());
    return catOk && searchOk;
  });

  // Filter offers by domestic preference
  const filteredOffers = selected
    ? selected.offers.filter((o) => !domesticOnly || o.is_domestic)
    : [];

  // The API orders offers by RAW price with no currency term, so for a part whose
  // offers span currencies, index 0 is the smallest NUMBER — not the cheapest
  // offer. Component 473 (INA2126U) shipped exactly that: an EUR 5.188 listing
  // wore the green "Best Price" badge directly above a USD 5.464 one, and EUR
  // 5.188 is ~USD 5.45-5.60 at any recent rate, i.e. at or ABOVE the offer under
  // it. There are no FX rates in this repo and inventing one would be synthetic
  // data, so the honest move is to decline the ranking and say why rather than
  // publish a confident wrong order. Catalogue as served: 8,152 USD / 17 EUR /
  // 4 GBP / 3 SGD. The `sameCurrency` guard above already covers the two "%"
  // lines; these are the three claims it never reached.
  const offerCurrencies = Array.from(
    new Set(filteredOffers.map((o) => (o.currency || 'USD').trim().toUpperCase())),
  );
  const offersRankable = offerCurrencies.length <= 1;

  // Still the lowest LISTED figure, and still correct to show with its own
  // currency symbol — it just is not "the best price" unless it is rankable.
  const lowestListedOffer = filteredOffers.length > 0 ? filteredOffers[0] : null;
  const cheapestOffer = offersRankable ? lowestListedOffer : null;

  return (
    <div className="flex flex-col h-full bg-slate-900 text-slate-100">
      <DemandModelPanel />
      {/* Column below `lg`: a fixed w-80 sidebar left ~70px for the detail panel at
          phone widths, which forced content off-screen. Stacked list-then-detail on
          narrow viewports, side-by-side from `lg` up. */}
      <div className="flex flex-col lg:flex-row flex-1 min-h-0">
      {/* Left panel: component list */}
      <div className="w-full lg:w-80 shrink-0 border-b lg:border-b-0 lg:border-r border-slate-700 flex flex-col max-h-[50vh] lg:max-h-none">
        <div className="p-3 border-b border-slate-700 space-y-2">
          <input
            type="text"
            placeholder="Search components, MPN, manufacturer..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full bg-slate-700 border border-slate-600 rounded px-3 py-1.5 text-sm text-white placeholder-slate-400 focus:outline-none focus:border-blue-500"
          />
          {/* Chips were 20px tall packed edge to edge (below the 44px touch-target
              floor with under 8px spacing). min-h-[44px] + gap-2 fixes both; the
              taller max-h keeps roughly the same number of rows visible before the
              existing internal scroll takes over. */}
          <div className="flex flex-wrap gap-2 max-h-40 overflow-y-auto">
            <button
              onClick={() => setSelectedCat('All')}
              className={`inline-flex items-center min-h-[44px] text-xs px-3 py-1.5 rounded transition-colors ${
                selectedCat === 'All' ? 'bg-blue-600 text-white' : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
              }`}
            >
              {catalogueError ? 'All' : `All (${components.length})`}
            </button>
            {categories.map((cat) => (
              <button
                key={cat.name}
                onClick={() => setSelectedCat(cat.name)}
                className={`inline-flex items-center min-h-[44px] text-xs px-3 py-1.5 rounded transition-colors ${
                  selectedCat === cat.name ? 'bg-blue-600 text-white' : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                }`}
              >
                {cat.name} ({cat.count})
              </button>
            ))}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="p-4 space-y-3">
              <div className="flex items-center justify-center gap-2 text-slate-400 text-sm">
                <div className="w-4 h-4 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
                Loading components{loadSec > 0 ? ` — ${loadSec}s` : '…'}
              </div>
              {loadSec >= 3 && <WakeNotice requestSec={loadSec} />}
            </div>
          )}

          {/* The request failed: say so and offer a retry. Anything else here would
              be a catalogue of zero parts that reads like a real answer. */}
          {!loading && catalogueError && (
            <div className="p-4" data-testid="catalogue-error">
              <div className="bg-red-900/20 border border-red-700/50 rounded-lg p-4 text-center">
                <div className="text-sm font-semibold text-red-300">Couldn&apos;t load the catalogue</div>
                <div className="text-xs text-red-200/80 mt-1.5">{catalogueError}</div>
                <button
                  onClick={() => { void loadCatalogue(); }}
                  className="mt-3 inline-flex items-center gap-1.5 bg-red-600/80 hover:bg-red-500 text-white text-xs font-medium px-3 py-1.5 rounded transition-colors"
                >
                  Retry
                </button>
              </div>
            </div>
          )}
          {visible.map((comp) => (
            <button
              key={comp.id}
              onClick={() => { void selectComponent(comp); }}
              className={`w-full text-left px-3 py-2.5 border-b border-slate-700/50 hover:bg-slate-700/40 transition-colors ${
                selected?.id === comp.id ? 'bg-slate-700/60 border-l-2 border-l-blue-500' : ''
              }`}
            >
              <div className="flex items-start justify-between gap-1">
                <div>
                  <div className="text-sm text-white font-medium leading-tight">{comp.mpn}</div>
                  <div className="text-xs text-slate-400">{comp.manufacturer}</div>
                </div>
                <div className="text-right shrink-0">
                  {comp.min_price != null && (
                    <div className="text-xs text-green-400 font-medium">{fmtUnitPrice(comp.min_price)}</div>
                  )}
                  <div className="text-xs text-slate-400">{plural(comp.num_offers, 'offer')}</div>
                </div>
              </div>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-xs text-slate-400 truncate">{comp.category}</span>
                {/* Badged on the FLAG, not on a numeric cut: `risk_score > 0.5`
                    was a cut inside the score's empty interval, so it silently
                    meant "chinese_origin fired" while reading as a magnitude. */}
                {comp.risk_factors && comp.risk_factors.length > 0 && (
                  <span
                    className={`text-xs px-1.5 py-0.5 rounded border ${
                      RISK_TIER_BADGE[catalogueRiskTier(comp.risk_factors, comp.risk_score)]
                    }`}
                  >
                    {comp.risk_factors.map(formatRiskFactor).join(', ')}
                  </span>
                )}
              </div>
            </button>
          ))}
          {!loading && !catalogueError && visible.length === 0 && (
            <div className="p-4 text-center text-slate-400 text-sm">No components found</div>
          )}
        </div>
        <div className="px-3 py-2 text-xs text-slate-400 border-t border-slate-700">
          {catalogueError
            ? 'Catalogue unavailable'
            : `${visible.length} of ${components.length} components`}
        </div>
      </div>

      {/* Right panel: detail */}
      <div className="flex-1 min-w-0 overflow-y-auto p-5">
        {!selected && detailLoading && (
          <div className="h-full flex items-center justify-center">
            <div className="w-7 h-7 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
          </div>
        )}

        {/* Component detail failed to load — an explicit, recoverable state rather
            than a stuck "Loading offers..." with a rejected promise behind it. */}
        {!selected && !detailLoading && detailError && (
          <div className="h-full flex items-center justify-center" data-testid="component-detail-error">
            <div className="max-w-md text-center bg-red-900/20 border border-red-700/50 rounded-lg p-5">
              <div className="text-sm font-semibold text-red-300">Couldn&apos;t load this component</div>
              <div className="text-xs text-red-200/80 mt-1.5">{detailError}</div>
              {failedComponent && (
                <button
                  onClick={() => { void selectComponent(failedComponent); }}
                  className="mt-4 inline-flex items-center gap-1.5 bg-red-600/80 hover:bg-red-500 text-white text-xs font-medium px-3 py-1.5 rounded transition-colors"
                >
                  Retry
                </button>
              )}
            </div>
          </div>
        )}

        {!selected && !detailLoading && !detailError && (
          <div className="h-full flex items-center justify-center text-slate-400">
            <div className="text-center">
              <Settings className="w-10 h-10 text-slate-400 mx-auto mb-3" aria-hidden="true" />
              <div className="text-lg font-medium text-slate-400">
                {catalogueError ? 'Catalogue unavailable' : 'Select a component'}
              </div>
              {/* Never "Browse 0 electronic components": the count is only a fact
                  when the request that produced it succeeded. */}
              <div className="text-sm mt-1">
                {catalogueError
                  ? 'The component list could not be loaded — retry from the panel on the left.'
                  : `Browse ${components.length} electronic components with real distributor pricing`}
              </div>
            </div>
          </div>
        )}

        {selected && (
          <div className="flex flex-col lg:flex-row gap-5">
            {/* Left column: info + offers */}
            <div className="flex-1 min-w-0 space-y-5">
              {/* Header */}
              <div>
                <div className="flex items-center gap-3">
                  <h1 className="text-xl font-bold text-white">{selected.mpn}</h1>
                  {selected.manufacturer_country && (
                    <span className="text-xs bg-slate-700 text-slate-300 px-2 py-0.5 rounded">
                      {selected.manufacturer_country}
                    </span>
                  )}
                </div>
                <p className="text-slate-400 text-sm mt-0.5">
                  {selected.manufacturer} &middot; {selected.category}
                </p>
                {selected.description && (
                  <p className="text-slate-400 text-sm mt-1">{selected.description}</p>
                )}
              </div>

              {/* Risk + metadata */}
              <div className="grid grid-cols-3 gap-3">
                <div className="bg-slate-800 rounded-lg p-3 border border-slate-700">
                  <div className="text-xs text-slate-400 mb-1">Risk Index</div>
                  <div
                    className={`text-lg font-bold ${
                      RISK_TIER_TEXT[catalogueRiskTier(selected.risk_factors, selected.risk_score)]
                    }`}
                  >
                    {formatRiskIndex(selected.risk_score)}{' '}
                    <span className="text-xs font-normal text-slate-400">{RISK_INDEX_SCALE}</span>
                  </div>
                </div>
                <div className="bg-slate-800 rounded-lg p-3 border border-slate-700">
                  <div className="text-xs text-slate-400 mb-1">Distributors</div>
                  <div className="text-lg font-bold text-white">{selected.offers.length}</div>
                </div>
                <div className="bg-slate-800 rounded-lg p-3 border border-slate-700">
                  <div className="text-xs text-slate-400 mb-1">Price Range</div>
                  <div className="text-sm font-bold text-white">
                    {selected.offers.length === 0
                      ? 'N/A'
                      : new Set(
                            selected.offers.map((o) => (o.currency || 'USD').trim().toUpperCase()),
                          ).size > 1
                        ? 'Mixed currencies'
                        : `${fmtUnitPrice(selected.offers[0].price, selected.offers[0].currency)} – ${fmtUnitPrice(
                            selected.offers[selected.offers.length - 1].price,
                            selected.offers[selected.offers.length - 1].currency,
                          )}`}
                  </div>
                </div>
              </div>

              {/* Risk factors — the flags behind the index. Always rendered,
                  including the empty case: a part scoring 0.20 with no flag
                  recorded is exactly the thing a reader needs to see, and it
                  describes 387 of the 791 catalogue parts. */}
              <div className="space-y-1.5">
                <div className="flex gap-2 flex-wrap items-center">
                  {selected.risk_factors && selected.risk_factors.length > 0 ? (
                    selected.risk_factors.map((rf) => (
                      <span key={rf} className="text-xs bg-red-900/30 border border-red-700/50 text-red-300 px-2 py-1 rounded">
                        {formatRiskFactor(rf)}
                      </span>
                    ))
                  ) : (
                    <span className="text-xs bg-slate-700/40 border border-slate-600/50 text-slate-300 px-2 py-1 rounded">
                      {CATALOGUE_RISK_LABELS.unflagged} recorded
                    </span>
                  )}
                </div>
                <p className="text-xs text-slate-500 leading-snug">{RISK_INDEX_NOTE}</p>
              </div>

              {/* Distributor offers table */}
              <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-sm font-semibold text-white">
                    Distributor Offers ({filteredOffers.length})
                  </h3>
                  <label className="flex items-center gap-2 text-xs text-slate-400 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={domesticOnly}
                      onChange={(e) => setDomesticOnly(e.target.checked)}
                      className="rounded bg-slate-700 border-slate-600"
                    />
                    US Domestic Only
                  </label>
                </div>
                {detailLoading ? (
                  <div className="text-slate-400 text-sm text-center py-4">Loading offers...</div>
                ) : detailError ? (
                  <div className="text-center py-4">
                    <div className="text-red-300 text-sm">{detailError}</div>
                    {failedComponent && (
                      <button
                        onClick={() => { void selectComponent(failedComponent); }}
                        className="mt-2 text-xs text-red-200 underline hover:text-white"
                      >
                        Retry
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="space-y-2 max-h-[420px] overflow-y-auto">
                    {filteredOffers.map((offer, i) => (
                      <button
                        key={offer.id}
                        onClick={() => { setSelectedOfferId(offer.id); setAddError(''); setAddedMsg(''); }}
                        className={`w-full text-left rounded-lg p-3 border transition-colors ${
                          selectedOfferId === offer.id
                            ? 'bg-blue-900/40 border-blue-500/60'
                            : 'bg-slate-700/40 border-slate-600/40 hover:bg-slate-700/60'
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            {i === 0 && offersRankable && (
                              <span className="text-xs bg-green-600 text-white px-1.5 py-0.5 rounded font-medium">
                                Best Price
                              </span>
                            )}
                            <span className="text-sm font-medium text-white">{offer.distributor_name}</span>
                            {offer.is_domestic && (
                              <span className="text-xs text-blue-400">US</span>
                            )}
                            {!offer.is_domestic && (
                              <span className="text-xs text-slate-400">{offer.distributor_country}</span>
                            )}
                          </div>
                          <span className="text-sm font-bold text-green-400">
                            {fmtUnitPrice(offer.price, offer.currency)}
                          </span>
                        </div>
                        <div className="grid grid-cols-3 gap-2 mt-2 text-xs text-slate-400">
                          <div>
                            Stock:{' '}
                            {offer.stock === 0
                              ? <span className="text-amber-300 font-medium">0 · out of stock</span>
                              : <span className="text-white">{offer.stock.toLocaleString()}</span>}
                          </div>
                          <div>SKU: <span className="text-white">{offer.sku || '—'}</span></div>
                          <div>
                            {offer.distributor_city && offer.distributor_state
                              ? `${offer.distributor_city}, ${offer.distributor_state}`
                              : offer.distributor_country || '—'}
                          </div>
                        </div>
                        {/* A "% vs best price" comparison across currencies without conversion would be
                            exactly the kind of silent unit error this repo has shipped before, so it's
                            gated on the two offers actually sharing a currency. */}
                        {cheapestOffer && offer.price > cheapestOffer.price && sameCurrency(offer.currency, cheapestOffer.currency) && (
                          <div className="text-xs text-red-400 mt-1">
                            +{((offer.price - cheapestOffer.price) / cheapestOffer.price * 100).toFixed(1)}% vs best price
                          </div>
                        )}
                      </button>
                    ))}
                    {filteredOffers.length === 0 && (
                      <div className="text-slate-400 text-sm text-center py-4">
                        No {domesticOnly ? 'domestic ' : ''}distributors found
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* Live pricing — real-time multi-distributor lookup, on demand */}
              <div className="mt-5">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <h3 className="text-sm font-semibold text-white flex items-center gap-1.5">
                      <Zap className="w-3.5 h-3.5 text-amber-400" />
                      Live Pricing
                    </h3>
                    <p className="text-slate-400 text-xs mt-0.5">
                      Offers above are a frozen 2024 snapshot (CC-BY-4.0). This pulls today&rsquo;s real prices
                      from up to four sources — availability varies per part and per source below.
                    </p>
                  </div>
                  <button
                    onClick={fetchLivePrices}
                    disabled={liveState === 'loading'}
                    className="shrink-0 inline-flex items-center gap-1.5 bg-amber-500/10 hover:bg-amber-500/20 disabled:opacity-50 border border-amber-500/30 text-amber-400 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
                  >
                    <RefreshCw className={`w-3.5 h-3.5 ${liveState === 'loading' ? 'animate-spin' : ''}`} />
                    {liveState === 'loading' ? 'Fetching live prices…' : liveState === 'loaded' ? 'Refresh live price' : 'Get live price'}
                  </button>
                </div>

                {/* Per-source outcome — shown whenever we have it, regardless of whether
                    the overall call "succeeded": graceful degradation only reads as a
                    feature if the degradation is visible. Hover a chip for the full error. */}
                {liveSources.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mb-2">
                    {liveSources.map((s) => (
                      <span
                        key={s.name}
                        title={s.error ?? undefined}
                        className={`text-[11px] px-1.5 py-0.5 rounded border ${sourceStatusStyle(s.status)} ${s.error ? 'cursor-help' : ''}`}
                      >
                        {SOURCE_LABELS[s.name] ?? s.name}: {sourceStatusText(s)}
                      </span>
                    ))}
                  </div>
                )}

                {liveState === 'idle' && (
                  <div className="text-slate-400 text-xs bg-slate-800/40 border border-slate-700/60 border-dashed rounded-lg p-3 text-center">
                    Not fetched yet — click &ldquo;Get live price&rdquo; to query real distributor APIs for {selected.mpn}.
                  </div>
                )}

                {(liveState === 'not_found' || liveState === 'unconfigured' || liveState === 'error') && (
                  <div className="text-xs bg-slate-800/60 border border-slate-700 rounded-lg p-3 text-slate-400">
                    {liveState === 'unconfigured' ? (
                      <>No live pricing sources configured on the backend. {liveErrorMsg}</>
                    ) : liveState === 'not_found' ? (
                      <>{liveErrorMsg} This part may not be carried by the currently-configured distributors.</>
                    ) : (
                      <span className="text-red-400">{liveErrorMsg}</span>
                    )}
                  </div>
                )}

                {liveState === 'loaded' && liveData && (
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <span className="inline-flex items-center gap-1.5 bg-amber-500/10 border border-amber-500/30 text-amber-400 text-[11px] px-2 py-0.5 rounded-full">
                        <span className="w-1.5 h-1.5 rounded-full bg-amber-400 motion-safe:animate-pulse" />
                        Live &middot; fetched just now
                      </span>
                      <span className="text-[11px] text-slate-400">
                        {plural(liveData.total_offers, 'offer')} from {liveData.sources_used.join(', ') || '—'}
                      </span>
                    </div>
                    {liveData.offers.length > 0 && (
                      <div className="flex items-center justify-between mb-2 gap-2">
                        <button
                          onClick={syncToDatabase}
                          disabled={syncing}
                          className="text-[11px] inline-flex items-center gap-1.5 text-slate-400 hover:text-white disabled:opacity-50 transition-colors"
                          title="Write these live prices into the component's catalog offers"
                        >
                          {syncing ? 'Saving to catalog…' : 'Save live prices to catalog →'}
                        </button>
                        {syncMsg && <span className="text-[11px] text-slate-400">{syncMsg}</span>}
                      </div>
                    )}
                    {liveData.offers.length === 0 ? (
                      <div className="text-slate-400 text-xs text-center py-3">No live offers returned for this part.</div>
                    ) : (
                      <div className="space-y-2 max-h-[320px] overflow-y-auto">
                        {liveData.offers.map((offer, i) => (
                          <div key={`${offer.distributor}-${offer.sku}-${i}`} className="rounded-lg p-3 border border-amber-700/30 bg-amber-950/10">
                            <div className="flex items-center justify-between">
                              <div className="flex items-center gap-2 min-w-0">
                                <span className="text-sm font-medium text-white truncate">{offer.distributor}</span>
                                <span className="text-[11px] uppercase tracking-wide text-slate-400 bg-slate-800 px-1.5 py-0.5 rounded shrink-0">{offer.source}</span>
                                {offer.is_authorized ? (
                                  <span title="Authorized distributor" className="shrink-0"><ShieldCheck className="w-3.5 h-3.5 text-green-400" /></span>
                                ) : (
                                  <span title="Unauthorized / gray-market channel" className="shrink-0"><ShieldAlert className="w-3.5 h-3.5 text-yellow-500" /></span>
                                )}
                              </div>
                              <span className="text-sm font-bold text-amber-400 shrink-0">
                                {offer.price > 0 ? fmtUnitPrice(offer.price, offer.currency) : '—'}
                              </span>
                            </div>
                            <div className="grid grid-cols-2 gap-x-3 gap-y-1 mt-2 text-xs text-slate-400">
                              <div>Stock: <span className="text-white">{offer.stock.toLocaleString()}</span></div>
                              <div>MOQ: <span className="text-white">{offer.moq.toLocaleString()}</span></div>
                              <div>SKU: <span className="text-white">{offer.sku || '—'}</span></div>
                              <div>Lead time: <span className="text-white">{offer.lead_time_weeks != null ? `${offer.lead_time_weeks}w` : '—'}</span></div>
                              {offer.lifecycle_status && (
                                <div className="col-span-2">Lifecycle: <span className="text-white">{offer.lifecycle_status}</span></div>
                              )}
                            </div>
                            {offer.price_breaks && offer.price_breaks.length > 0 && (
                              <div className="flex flex-wrap gap-1.5 mt-2">
                                {offer.price_breaks.slice(0, 5).map((pb, j) => {
                                  const qty = pb.quantity ?? pb.qty ?? pb.break_quantity;
                                  const price = pb.price ?? pb.unit_price;
                                  if (qty == null || price == null) return null;
                                  return (
                                    <span key={j} className="text-[11px] text-slate-400 bg-slate-800/70 px-1.5 py-0.5 rounded">
                                      {String(qty)}+ @ {fmtUnitPrice(Number(price), offer.currency)}
                                    </span>
                                  );
                                })}
                              </div>
                            )}
                            {offer.datasheet_url && (
                              <a href={offer.datasheet_url} target="_blank" rel="noopener noreferrer" className="text-[11px] text-blue-400 hover:underline mt-2 inline-block">
                                Datasheet
                              </a>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* Right column: Order panel */}
            <div className="w-full lg:w-80 lg:shrink-0 space-y-4">
              {/* Price display */}
              <div className="bg-slate-800 rounded-lg p-4 border border-slate-700 text-center">
                {selectedOffer ? (
                  <>
                    <div className="text-xs text-slate-400 mb-1">{selectedOffer.distributor_name}</div>
                    <div className="text-3xl font-bold text-white">
                      {fmtUnitPrice(selectedOffer.price, selectedOffer.currency)}
                    </div>
                    <div className="text-slate-400 text-xs mt-0.5">per unit</div>
                    {cheapestOffer && selectedOffer.price > cheapestOffer.price && sameCurrency(selectedOffer.currency, cheapestOffer.currency) && (
                      <div className="text-xs text-red-400 mt-1">
                        {((selectedOffer.price - cheapestOffer.price) / cheapestOffer.price * 100).toFixed(1)}% above best price ({fmtUnitPrice(cheapestOffer.price, cheapestOffer.currency)} at {cheapestOffer.distributor_name})
                      </div>
                    )}
                    {cheapestOffer && selectedOffer.id === cheapestOffer.id && (
                      <div className="text-xs text-green-400 mt-1">Best available price</div>
                    )}
                    {!offersRankable && (
                      <div className="text-xs text-amber-400 mt-1">
                        Offers span {offerCurrencies.join(', ')} — not converted, so they are not
                        rank-ordered.
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    <div className="text-xs text-slate-400 mb-1">
                      {offersRankable ? 'Best Available Price' : 'Lowest Listed Price'}
                    </div>
                    <div className="text-3xl font-bold text-white">
                      {lowestListedOffer
                        ? fmtUnitPrice(lowestListedOffer.price, lowestListedOffer.currency)
                        : '--'}
                    </div>
                    <div className="text-slate-400 text-xs mt-0.5">
                      {lowestListedOffer ? `at ${lowestListedOffer.distributor_name}` : 'per unit'}
                    </div>
                    {!offersRankable && lowestListedOffer && (
                      <div className="text-xs text-amber-400 mt-1">
                        Offers span {offerCurrencies.join(', ')} — not converted, so this is the
                        lowest figure listed, not necessarily the cheapest.
                      </div>
                    )}
                    <div className="text-xs text-slate-400 mt-1">Select a distributor to order</div>
                  </>
                )}
              </div>

              {/* Order form */}
              {selectedOffer && (
                <div className="bg-slate-800 rounded-lg p-4 border border-slate-700 space-y-3">
                  <div>
                    <label className="text-xs text-slate-400 block mb-1">Quantity (units)</label>
                    <input
                      type="number"
                      min={1}
                      step={1}
                      value={qty}
                      onChange={(e) => setQty(parseInt(e.target.value) || 1)}
                      className="w-full bg-slate-700 border border-slate-600 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                    />
                  </div>

                  {outOfStock && (
                    <div className="text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded px-2.5 py-2 leading-relaxed">
                      <span className="font-semibold">Out of stock</span> at {selectedOffer.distributor_name}.
                      Pick another distributor above — the server will reject this line.
                    </div>
                  )}

                  {overStock && (
                    <div className="text-xs text-red-400">
                      Exceeds available stock ({plural(selectedOffer.stock, 'unit')})
                    </div>
                  )}

                  <div className="flex items-center justify-between text-sm">
                    <span className="text-slate-400">Estimated Total</span>
                    <span className="text-white font-bold text-lg">
                      {fmtTotal(selectedOffer.price * qty, selectedOffer.currency)}
                    </span>
                  </div>

                  <button
                    onClick={handleAddToCart}
                    disabled={adding || !canAdd}
                    title={
                      outOfStock
                        ? `${selectedOffer.distributor_name} has no stock for this part`
                        : nonUsdOffer
                          ? `This offer is priced in ${(selectedOffer.currency || 'USD').trim().toUpperCase()}; the cart books every line in USD and no conversion is available`
                          : undefined
                    }
                    className="w-full bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-400 disabled:cursor-not-allowed text-white py-2.5 rounded-lg text-sm font-semibold transition-colors"
                    data-testid="add-to-cart"
                  >
                    {adding
                      ? 'Adding...'
                      : outOfStock
                        ? 'Out of Stock'
                        : overStock
                          ? 'Reduce Quantity'
                          : nonUsdOffer
                            ? `Priced in ${(selectedOffer.currency || 'USD').trim().toUpperCase()} — USD only`
                            : 'Add to Cart'}
                  </button>

                  {addError && (
                    <div
                      className="text-xs text-red-200 bg-red-900/30 border border-red-700/50 rounded px-2.5 py-2 leading-relaxed"
                      role="alert"
                      data-testid="add-to-cart-error"
                    >
                      {addError}
                    </div>
                  )}

                  {addedMsg && (
                    <div className="text-sm text-center text-green-400" role="status">
                      {addedMsg}
                    </div>
                  )}
                </div>
              )}

              {/* Component info card */}
              {selected.datasheets && selected.datasheets.length > 0 && (
                <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
                  <h4 className="text-xs text-slate-400 mb-2">Datasheets</h4>
                  {selected.datasheets.map((url, i) => (
                    <a
                      key={i}
                      href={url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-blue-400 hover:underline block truncate"
                    >
                      Datasheet {i + 1}
                    </a>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      </div>
    </div>
  );
}
