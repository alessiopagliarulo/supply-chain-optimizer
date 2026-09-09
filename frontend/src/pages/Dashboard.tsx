import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ScatterChart, Scatter, XAxis, YAxis, ZAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, PieChart, Pie, Cell,
  BarChart, Bar, ReferenceLine, LabelList,
} from 'recharts';
import { Map, Boxes, ShoppingCart, Rocket, type LucideIcon } from 'lucide-react';
import { useAuthStore } from '../store/authStore';
import { useCartStore } from '../store/cartStore';
import { componentsAPI, distributorsAPI, feedsAPI } from '../services/api';
import {
  CATALOGUE_RISK_COLORS, CATALOGUE_RISK_LABELS, catalogueRiskTier,
  formatRiskIndex, formatRiskFactor, RISK_INDEX_SCALE, RISK_INDEX_NOTE,
} from '../lib/risk';

// ── Types ─────────────────────────────────────────────────────────────────────
interface ComponentItem {
  id: number;
  mpn: string;
  manufacturer: string;
  category: string;
  description: string | null;
  risk_score: number;
  // The API serves this as a JSON list of flag names (`["chinese_origin"]`) or
  // null — it was typed as an object here, which is why the flags were never
  // rendered even though they are strictly more informative than the score.
  risk_factors: string[] | null;
  min_price: number | null;
  max_price: number | null;
  num_offers: number;
}

interface DistributorItem {
  id: number;
  name: string;
  city: string | null;
  state: string | null;
  country: string;
  is_domestic: boolean;
  total_offers: number;
  total_stock: number;
}

interface NavCard {
  title: string;
  desc: string;
  icon: LucideIcon;
  path: string;
  border: string;
  badge: string;
}

// ── Animated KPI Card ─────────────────────────────────────────────────────────
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
      <span className="text-slate-400 text-xs font-medium uppercase tracking-wider">{title}</span>
      <span className="text-3xl font-bold text-white tabular-nums">{value}</span>
      <span className="text-slate-400 text-xs">{sub}</span>
    </motion.div>
  );
}

// ── Explicit degraded/error state for a data section ─────────────────────────
// Used wherever a chart or list would otherwise render an empty/zero state
// that's indistinguishable from "backend returned real data, there's just
// nothing here." Never let a failed fetch look like a confident zero.
function DataUnavailable({ height = 'h-40' }: { height?: string }) {
  return (
    <div className={`${height} flex flex-col items-center justify-center gap-1 text-center px-4`}>
      <span className="text-red-400 text-xs font-medium">Data unavailable</span>
      <span className="text-slate-400 text-xs">Backend unreachable — not shown as zero</span>
    </div>
  );
}

// ── Category label shortening ────────────────────────────────────────────────
// Distributor taxonomies use long names ("Analog to Digital Converters (ADCs)",
// "DSPs - Digital Signal Processors"). A blind character cut clipped them
// mid-word ("Analog to Digital Con…"), so instead we prefer the abbreviation
// the name already carries, then fall back to a word-boundary cut. The FULL
// name always travels alongside the short one and is what tooltips (and the
// legend's title attribute) show, so nothing is actually lost.
const CATEGORY_LABEL_MAX = 22;

const truncateAtWord = (s: string, max: number): string => {
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const lastSpace = cut.lastIndexOf(' ');
  const head = lastSpace > max * 0.5 ? cut.slice(0, lastSpace) : cut;
  return `${head.replace(/[\s,;/&+-]+$/, '')}…`;
};

const shortenCategory = (s: string, max: number = CATEGORY_LABEL_MAX): string => {
  const name = (s || '').trim();
  if (!name) return 'Uncategorized';
  if (name.length <= max) return name;
  // "Analog to Digital Converters (ADCs)" → "ADCs"; "Integrated Circuits (ICs)" → "ICs"
  const paren = name.match(/\(([^)]{2,6})\)\s*$/);
  if (paren && paren[1].trim().length <= max) return paren[1].trim();
  // "DSPs - Digital Signal Processors" → "DSPs"
  const head = name.split(/\s+[-–—]\s+/)[0];
  if (head.length < name.length && head.length <= max) return head;
  return truncateAtWord(name, max);
};

// Two long names can abbreviate to the same label ("Amplifiers - Current Sense"
// and "Amplifiers - Op Amps, Buffer, Instrumentation" both → "Amplifiers"), which
// would put two identically-labelled slices on one chart. When that happens, fall
// back to a word-boundary cut of the full name for the colliding pair.
const shortenCategories = (
  names: string[],
  max: number = CATEGORY_LABEL_MAX,
): Record<string, string> => {
  const short: Record<string, string> = {};
  const seen: Record<string, number> = {};
  names.forEach((n) => {
    const label = shortenCategory(n, max);
    short[n] = label;
    seen[label] = (seen[label] || 0) + 1;
  });
  names.forEach((n) => {
    if (seen[short[n]] > 1) short[n] = truncateAtWord(n, max);
  });
  return short;
};

// Live Feeds poll cadence — copy that references this ("every N minutes")
// is derived from this constant so it can never drift from the real interval.
const FEED_POLL_INTERVAL_MS = 60_000;
const FEED_POLL_LABEL = FEED_POLL_INTERVAL_MS % 60_000 === 0
  ? `${FEED_POLL_INTERVAL_MS / 60_000} minute${FEED_POLL_INTERVAL_MS / 60_000 === 1 ? '' : 's'}`
  : `${FEED_POLL_INTERVAL_MS / 1000} seconds`;

// ── Custom Scatter Tooltip ────────────────────────────────────────────────────
function RiskTooltip({ active, payload }: { active?: boolean; payload?: any[] }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  const tier = catalogueRiskTier(d.factors, d.y);
  return (
    <div className="bg-slate-900 border border-slate-600 rounded-lg p-3 text-xs shadow-xl max-w-[200px]">
      <p className="text-white font-semibold mb-1 truncate">{d.name}</p>
      <p className="text-slate-400">Category: <span className="text-slate-200">{d.category}</span></p>
      {/* An index on its stated scale — never a percentage. The flags below it
          are the falsifiable part, so they get shown alongside the number. */}
      <p className="text-slate-400">
        Risk index: <span className="text-slate-200">{formatRiskIndex(d.y)} {RISK_INDEX_SCALE}</span>
      </p>
      <p className="text-slate-400">
        Flags: <span style={{ color: CATALOGUE_RISK_COLORS[tier] }}>
          {d.factors?.length ? d.factors.map(formatRiskFactor).join(', ') : CATALOGUE_RISK_LABELS.unflagged}
        </span>
      </p>
      <p className="text-slate-400">Offers: <span className="text-blue-400">{d.offers}</span></p>
      {d.price != null && <p className="text-slate-400">Min Price: <span className="text-green-400">${d.price.toFixed(4)}</span></p>}
    </div>
  );
}

// ── Main Dashboard ─────────────────────────────────────────────────────────────
export const Dashboard = () => {
  const navigate = useNavigate();
  const { user } = useAuthStore();
  const { items: cartItems } = useCartStore();

  const [components, setComponents] = useState<ComponentItem[]>([]);
  const [distributors, setDistributors] = useState<DistributorItem[]>([]);
  const [loading, setLoading] = useState(true);
  // True only after a fetch attempt actually failed — never inferred from
  // empty data, so a slow-but-working backend never gets flagged as down.
  const [dataError, setDataError] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [cRes, dRes] = await Promise.all([componentsAPI.list(), distributorsAPI.list()]);
      setComponents(cRes.data);
      setDistributors(dRes.data);
      setDataError(false);
    } catch {
      // Backend unreachable/errored — surface it explicitly rather than
      // leaving components/distributors at [] and letting every KPI below
      // render a confident-looking zero.
      setDataError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // ── Live Feeds status ────────────────────────────────────────────────────────
  const [feedStatus, setFeedStatus] = useState<Array<{
    name: string;
    fetched_at: string | null;
    // 'inactive' = credential not configured, feed never ran (detail says which
    // env var is missing). Distinct from 'unavailable' = tried and failed.
    status: 'live' | 'stale' | 'inactive' | 'unavailable';
    value_summary: string | null;
    detail?: string | null;
    // Date of the newest OBSERVATION in the payload, where the payload dates
    // itself. A feed can download fine and still be stale on this field — that
    // is exactly the GPR case, and `detail` explains it in the tooltip.
    observation_date?: string | null;
  }>>([]);
  const [feedError, setFeedError] = useState(false);

  useEffect(() => {
    const fetchFeeds = async () => {
      try {
        const res = await feedsAPI.getStatus();
        setFeedStatus(res.data);
        setFeedError(false);
      } catch {
        setFeedError(true);
      }
    };
    fetchFeeds();
    const interval = setInterval(fetchFeeds, FEED_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, []);

  const formatFeedTime = (isoString: string | null): string => {
    if (!isoString) return '\u2014';
    const date = new Date(isoString);
    const diffMs = Date.now() - date.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 60) return `${diffMin}m ago`;
    return new Intl.DateTimeFormat('en-US', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
    }).format(date) + ' UTC';
  };

  // ── Derived data ────────────────────────────────────────────────────────────
  // Counted by FLAG, not by a numeric cut on risk_score. Every threshold that
  // used to live here landed in the empty interval between 0.25 and 0.60, so it
  // could never be wrong and never be right — see lib/risk.ts for the support.
  const originFlagged = components.filter(
    (c) => catalogueRiskTier(c.risk_factors, c.risk_score) === 'origin_flagged',
  ).length;
  // The 0.20 cohort: a nonzero score with no flag behind it. Counted live so
  // the caption can never drift from the catalogue actually being served.
  const placeholderScored = components.filter(
    (c) => !c.risk_factors?.length && c.risk_score > 0,
  ).length;
  const avgRisk = components.length
    ? (components.reduce((s, c) => s + c.risk_score, 0) / components.length)
    : 0;
  const domesticDists = distributors.filter((d) => d.is_domestic).length;

  // Risk matrix data for scatter chart — risk_score vs num_offers.
  // This plots the FULL catalogue on purpose. It used to plot `slice(0, 200)`,
  // which is array order and not a sample: none of the `placeholderScored` cohort
  // named in the caption below fell inside the first 200 rows, so the chart and
  // the sentence under it described two different populations.
  const maxOffers = Math.max(1, ...components.map((cc) => cc.num_offers));
  const riskMatrix = components.map((c) => ({
    x: c.num_offers / maxOffers,
    y: c.risk_score,
    z: c.min_price ? Math.log(c.min_price + 1) * 30 + 20 : 30,
    name: c.mpn,
    category: c.category,
    price: c.min_price,
    offers: c.num_offers,
    factors: c.risk_factors,
  }));

  // Category distribution — grouped by the full category name (not a
  // first-two-words truncation, which collapsed distinct categories like
  // "DSPs - Digital Signal Processors" into "DSPs -" and undercounted the
  // real category total the API reports). Labels are only truncated for
  // display via truncateLabel().
  const catCounts = components.reduce<Record<string, number>>((acc, c) => {
    acc[c.category] = (acc[c.category] || 0) + 1;
    return acc;
  }, {});
  const topCategories = Object.entries(catCounts)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 10);
  const categoryLabels = shortenCategories(topCategories.map(([name]) => name));
  const categoryData = topCategories.map(([name, value]) => ({
    name: categoryLabels[name],
    full: name,
    value,
  }));

  const CATEGORY_COLORS = ['#6366f1', '#f59e0b', '#10b981', '#3b82f6', '#ec4899', '#8b5cf6', '#ef4444', '#06b6d4', '#f97316', '#14b8a6'];

  // Category risk radar — same full-name grouping as catCounts above.
  const topCatRisk = Object.entries(
    components.reduce<Record<string, { risk: number; count: number }>>((acc, c) => {
      const cat = c.category;
      if (!acc[cat]) acc[cat] = { risk: 0, count: 0 };
      acc[cat].risk += c.risk_score;
      acc[cat].count += 1;
      return acc;
    }, {})
  )
    .sort(([, a], [, b]) => b.count - a.count)
    .slice(0, 8);
  // Bar rows have a whole line each, so they take a slightly longer cap than the
  // radar's circle labels did — and the full name is in the tooltip and in the
  // raw-data table underneath either way.
  const catRiskLabels = shortenCategories(topCatRisk.map(([cat]) => cat));
  // Mean of the raw index, on the index's own 0–1 scale. It used to be
  // multiplied by 100 and plotted against a 0–100 axis, which read as a
  // percentage of something. It is not a percentage of anything.
  //
  // Sorted DESCENDING by the value: ranking is the whole point of this panel, and
  // three of these categories sit on exactly the same mean (0.200), which a radar
  // renders as three indistinguishable spokes.
  const catRisk = topCatRisk
    .map(([cat, d]) => ({
      category: catRiskLabels[cat],
      full: cat,
      n: d.count,
      'Risk index': parseFloat((d.risk / d.count).toFixed(3)),
    }))
    .sort((a, b) => b['Risk index'] - a['Risk index']);
  // Axis top is derived from the data (rounded up to a tenth) rather than
  // pinned to 1.0, so the bars stay readable without ever clipping.
  const catRiskMax = Math.max(
    0.1,
    Math.ceil(Math.max(0, ...catRisk.map((c) => c['Risk index'])) * 10) / 10,
  );

  // Distributor country distribution
  const countryBins = distributors.reduce<Record<string, number>>((acc, d) => {
    acc[d.country] = (acc[d.country] || 0) + 1;
    return acc;
  }, {});
  const countryData = Object.entries(countryBins)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 6)
    .map(([label, count], i) => ({ label, count, fill: CATEGORY_COLORS[i % CATEGORY_COLORS.length] }));

  // NAV badges/desc read live counts from state, never a hardcoded snapshot —
  // and fall back to an honest "Unavailable" instead of a confident "0" when
  // the underlying fetch failed (dataError).
  const NAV: NavCard[] = [
    { title: 'Distributor Map', desc: dataError ? 'Distributors worldwide' : `${distributors.length} distributors worldwide`, icon: Map, path: '/map', border: 'hover:border-blue-500 hover:bg-blue-500/5', badge: dataError ? 'Unavailable' : `${distributors.length} distributors` },
    { title: 'Component Browser', desc: dataError ? 'Real pricing from live distributors' : `Real pricing from ${distributors.length} distributors`, icon: Boxes, path: '/scheduler', border: 'hover:border-green-500 hover:bg-green-500/5', badge: dataError ? 'Unavailable' : `${components.length} components` },
    { title: 'Bill of Materials', desc: 'Build orders across distributors', icon: ShoppingCart, path: '/cart', border: 'hover:border-purple-500 hover:bg-purple-500/5', badge: cartItems.length > 0 ? `${cartItems.length} items` : 'Empty' },
    { title: 'Route Optimization', desc: 'CP-SAT sourcing MILP + OR-Tools TSP tour', icon: Rocket, path: '/checkout', border: 'hover:border-orange-500 hover:bg-orange-500/5', badge: 'MILP + TSP' },
  ];

  return (
    <div className="min-h-full bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 overflow-y-auto h-full">
      <div className="max-w-7xl mx-auto px-6 py-8">

        {/* ── Header ──────────────────────────────────────────────────────── */}
        <motion.div
          initial={{ opacity: 0, y: -12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
          className="flex items-start justify-between mb-8"
        >
          <div>
            <h1 className="text-3xl font-bold text-white tracking-tight">
              Supply Chain Intelligence
            </h1>
            <p className="text-slate-400 mt-1 text-sm">
              Welcome back, <span className="text-white font-medium">{user?.factory_name}</span>
              {user && (
                <span className="text-slate-400 ml-2">
                  {Math.abs(user.latitude).toFixed(2)}°{user.latitude >= 0 ? 'N' : 'S'}{' '}
                  {Math.abs(user.longitude).toFixed(2)}°{user.longitude >= 0 ? 'E' : 'W'}
                </span>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {loading ? (
              <span className="inline-flex items-center gap-1.5 bg-slate-700/40 border border-slate-600/40 text-slate-400 text-xs px-3 py-1.5 rounded-full">
                <span className="w-1.5 h-1.5 rounded-full bg-slate-400 animate-pulse" />
                Loading…
              </span>
            ) : dataError ? (
              <span className="inline-flex items-center gap-1.5 bg-red-500/10 border border-red-500/30 text-red-400 text-xs px-3 py-1.5 rounded-full">
                <span className="w-1.5 h-1.5 rounded-full bg-red-400" />
                Data unavailable — backend unreachable
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 bg-green-500/10 border border-green-500/30 text-green-400 text-xs px-3 py-1.5 rounded-full">
                <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
                Real Data
              </span>
            )}
          </div>
        </motion.div>

        {/* ── KPI Strip ────────────────────────────────────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          <KpiCard delay={0}    title="Components"      value={loading ? '…' : dataError ? '—' : components.length}    sub={dataError ? 'fetch failed' : 'from HuggingFace dataset'}      accent={dataError ? 'border-red-500/30' : 'border-slate-700'} />
          <KpiCard delay={0.05} title="Distributors"    value={loading ? '…' : dataError ? '—' : distributors.length}  sub={dataError ? 'fetch failed' : `${domesticDists} domestic, ${distributors.length - domesticDists} int'l`} accent={dataError ? 'border-red-500/30' : 'border-blue-500/30'} />
          {/* An index, on its stated scale. The accent no longer changes with
              the value: colouring a tile by a numeric cut on this score is the
              same unfalsifiable threshold the bands used to encode. */}
          <KpiCard delay={0.1}  title="Avg Risk Index"  value={loading ? '…' : dataError ? '—' : `${formatRiskIndex(avgRisk)} ${RISK_INDEX_SCALE}`} sub={dataError ? 'fetch failed' : 'catalogue flag sum, not a probability'} accent={dataError ? 'border-red-500/30' : 'border-slate-700'} />
          <KpiCard delay={0.15} title="China-Origin Flagged" value={loading ? '…' : dataError ? '—' : originFlagged} sub={dataError ? 'fetch failed' : `of ${components.length} parts · chinese_origin flag`} accent="border-red-500/30" />
        </div>

        {/* ── Row 2: Risk Matrix + Category Distribution ─────────────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-4 mb-6">

          {/* Risk Matrix Scatter */}
          <motion.div
            initial={{ opacity: 0, scale: 0.97 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: 0.2, duration: 0.5 }}
            className="col-span-1 lg:col-span-3 bg-slate-800/60 border border-slate-700 rounded-xl p-5 backdrop-blur-sm"
          >
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-white font-semibold text-sm">Component Risk Matrix</h3>
                <p className="text-slate-400 text-xs mt-0.5">Offer availability vs catalogue risk index · bubble size = price</p>
              </div>
            </div>
            {loading ? (
              <div className="h-52 flex items-center justify-center text-slate-400 text-sm">Loading…</div>
            ) : dataError ? (
              <DataUnavailable height="h-52" />
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <ScatterChart margin={{ top: 8, right: 16, bottom: 16, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis
                    type="number" dataKey="x" name="Offer Availability"
                    domain={[0, 1]} tickFormatter={(v) => `${(v * 100).toFixed(0)}%`}
                    tick={{ fill: '#64748b', fontSize: 12 }} label={{ value: 'Offer Availability', position: 'insideBottom', offset: -8, fill: '#475569', fontSize: 12 }}
                  />
                  {/* Ticks are the index itself. Multiplying by 100 and
                      appending "%" turned a flag sum into a rate. */}
                  <YAxis
                    type="number" dataKey="y" name="Risk index"
                    domain={[0, 1]} ticks={[0, 0.25, 0.5, 0.75, 1]} tickFormatter={(v) => formatRiskIndex(v)}
                    tick={{ fill: '#64748b', fontSize: 12 }} label={{ value: 'Risk index (0–1)', angle: -90, position: 'insideLeft', offset: 12, fill: '#475569', fontSize: 12 }}
                  />
                  <ZAxis type="number" dataKey="z" range={[20, 200]} />
                  <Tooltip content={<RiskTooltip />} cursor={{ stroke: '#334155', strokeDasharray: '4 4' }} />
                  <ReferenceLine x={0.5} stroke="#334155" strokeDasharray="4 4" />
                  {/* No horizontal reference line: any y-cut here would sit in
                      the score's empty interval (0.25, 0.60) and imply a
                      boundary the data cannot support. */}
                  <Scatter
                    name="Components"
                    data={riskMatrix}
                    fill="#6366f1"
                    fillOpacity={0.6}
                  />
                </ScatterChart>
              </ResponsiveContainer>
            )}
            {!loading && !dataError && (
              <div className="mt-2 text-xs text-slate-400 space-y-1">
                <div className="grid grid-cols-2 gap-2">
                  <span className="text-left pl-8">◄ Fewer offers</span>
                  <span className="text-right pr-4">More offers ►</span>
                </div>
                {/* The one line that says what this quantity is. Kept short and
                    placed where the index is most prominent. */}
                <p className="text-slate-500 leading-snug">
                  {RISK_INDEX_NOTE}{' '}
                  {placeholderScored > 0 && (
                    <>
                      {placeholderScored} of {components.length} parts carry a nonzero index with
                      no flag recorded behind it.
                    </>
                  )}
                </p>
              </div>
            )}
          </motion.div>

          {/* Category Donut + Distributor Countries */}
          <motion.div
            initial={{ opacity: 0, scale: 0.97 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: 0.25, duration: 0.5 }}
            className="col-span-1 lg:col-span-2 flex flex-col gap-4"
          >
            {/* Donut */}
            <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 flex-1 backdrop-blur-sm">
              <h3 className="text-white font-semibold text-sm mb-1">Top Categories</h3>
              <p className="text-slate-400 text-xs mb-3">
                {dataError ? 'Unavailable' : `${components.length} components across ${Object.keys(catCounts).length} categories`}
              </p>
              {loading ? (
                <div className="h-32 flex items-center justify-center text-slate-400 text-sm">Loading…</div>
              ) : dataError ? (
                <DataUnavailable height="h-32" />
              ) : (
                <div className="flex items-center gap-3">
                  <ResponsiveContainer width="60%" height={120}>
                    <PieChart>
                      <Pie data={categoryData} cx="50%" cy="50%" innerRadius={32} outerRadius={52} dataKey="value" strokeWidth={0}>
                        {categoryData.map((_, i) => (
                          <Cell key={i} fill={CATEGORY_COLORS[i % CATEGORY_COLORS.length]} />
                        ))}
                      </Pie>
                      <Tooltip
                        contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 12 }}
                        formatter={(v: any, n: any, p: any) => [v, p?.payload?.full ?? n]}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="flex flex-col gap-1 flex-1">
                    {categoryData.slice(0, 6).map((d, i) => (
                      <div key={d.name} className="flex items-center justify-between text-xs">
                        <span className="flex items-center gap-1.5 text-slate-400 min-w-0">
                          <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: CATEGORY_COLORS[i % CATEGORY_COLORS.length] }} />
                          <span className="truncate" title={d.full}>{d.name}</span>
                        </span>
                        <span className="text-slate-300 tabular-nums ml-1">{d.value}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Distributor countries */}
            <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 backdrop-blur-sm">
              <h3 className="text-white font-semibold text-sm mb-1">Distributor Countries</h3>
              <p className="text-slate-400 text-xs mb-3">{dataError ? 'Unavailable' : `${distributors.length} distributors`}</p>
              {loading ? (
                <div className="h-24 flex items-center justify-center text-slate-400 text-sm">Loading…</div>
              ) : dataError ? (
                <DataUnavailable height="h-16" />
              ) : (
                <ResponsiveContainer width="100%" height={100}>
                  <BarChart data={countryData} margin={{ top: 0, right: 0, left: -16, bottom: 0 }}>
                    <XAxis dataKey="label" tick={{ fill: '#64748b', fontSize: 12 }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fill: '#64748b', fontSize: 12 }} axisLine={false} tickLine={false} />
                    <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 12 }} />
                    <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                      {countryData.map((b, i) => <Cell key={i} fill={b.fill} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </motion.div>
        </div>

        {/* ── Row 3: Risk index by category ───────── */}
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-4 mb-6">
          <motion.div
            initial={{ opacity: 0, scale: 0.97 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: 0.3, duration: 0.5 }}
            className="col-span-1 lg:col-span-3 bg-slate-800/60 border border-slate-700 rounded-xl p-5 backdrop-blur-sm"
          >
            {/*
              This was a radar chart, and a radar chart was the wrong form for it.
              The design database grades radar B and names the exact failure mode
              here: "values need precise comparison (use grouped bar)". Concretely,
              on the live catalogue three of these eight categories sit on exactly
              the same mean (0.200), which a radar draws as three spokes a reader
              cannot tell apart; the radial axis rendered two ticks, both labelled
              "0", so no spoke could be read off a scale at all; and two category
              names were ellipsised into the circle. A horizontal bar chart is the
              database's "Compare Categories" form — grade AAA, "value labels on
              each bar by default", "always sort descending by value" — so every
              number is legible without an axis lookup, and the raw table below is
              the fallback for the names the axis still has to shorten.
            */}
            <h3 className="text-white font-semibold text-sm mb-1">Risk Index by Category</h3>
            <p className="text-slate-400 text-xs mb-3">
              Mean catalogue risk index for the {catRisk.length} largest categories, on the
              index&rsquo;s own 0–1 scale. Sorted by index; every bar carries its value.
            </p>
            {loading ? (
              <div className="h-48 flex items-center justify-center text-slate-400 text-sm">Loading…</div>
            ) : dataError ? (
              <DataUnavailable height="h-48" />
            ) : (
              <>
                <ResponsiveContainer width="100%" height={catRisk.length * 26 + 44}>
                  <BarChart
                    data={catRisk}
                    layout="vertical"
                    margin={{ top: 4, right: 48, bottom: 4, left: 4 }}
                    barCategoryGap="24%"
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" horizontal={false} />
                    <XAxis
                      type="number"
                      domain={[0, catRiskMax]}
                      stroke="#94a3b8"
                      tick={{ fill: '#94a3b8', fontSize: 12 }}
                      tickFormatter={(v: number) => formatRiskIndex(v)}
                    />
                    <YAxis
                      type="category"
                      dataKey="category"
                      width={128}
                      stroke="#94a3b8"
                      tickLine={false}
                      tick={{ fill: '#94a3b8', fontSize: 12 }}
                    />
                    {/* `Flash` averages exactly 0.000 on the live catalogue. Recharts drops a
                        zero-width rectangle entirely, and with it that row's value label —
                        so the category kept its axis tick and lost its number, which is the
                        decorative failure this whole change exists to remove. A 2px stub
                        keeps the row rendered; the label beside it still reads 0.000. */}
                    <Bar dataKey="Risk index" fill="#ef4444" radius={[0, 3, 3, 0]} barSize={14} minPointSize={2} isAnimationActive={false}>
                      <LabelList
                        dataKey="Risk index"
                        position="right"
                        formatter={(v: any) => formatRiskIndex(Number(v), 3)}
                        style={{ fill: '#cbd5e1', fontSize: 12 }}
                      />
                    </Bar>
                    <Tooltip
                      cursor={{ fill: '#1e293b66' }}
                      contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 12 }}
                      labelFormatter={(label: any, p: any) => p?.[0]?.payload?.full ?? label}
                      formatter={(v: any, name: any, p: any) => [
                        `${formatRiskIndex(Number(v), 3)} ${RISK_INDEX_SCALE} · ${p?.payload?.n ?? 0} parts`,
                        name,
                      ]}
                    />
                  </BarChart>
                </ResponsiveContainer>

                {/* The axis has to shorten two of these names to fit. The full name,
                    the exact mean and the number of parts it averages are all here,
                    so nothing on the chart is only available as a hover. */}
                <details className="mt-3 group">
                  <summary className="min-h-[44px] flex items-center cursor-pointer text-xs text-slate-400 hover:text-slate-200 select-none">
                    Show the {catRisk.length} values as a table
                  </summary>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs mt-1">
                      <thead>
                        <tr className="text-slate-400 border-b border-slate-700">
                          <th className="text-left py-1.5 pr-3 font-medium">Category</th>
                          <th className="text-right py-1.5 pr-3 font-medium">Parts</th>
                          <th className="text-right py-1.5 font-medium">Mean risk index {RISK_INDEX_SCALE}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {catRisk.map((c) => (
                          <tr key={c.full} className="border-b border-slate-800 last:border-0">
                            <td className="py-1.5 pr-3 text-slate-300">{c.full}</td>
                            <td className="py-1.5 pr-3 text-right text-slate-400 tabular-nums">{c.n}</td>
                            <td className="py-1.5 text-right text-slate-200 tabular-nums">
                              {formatRiskIndex(c['Risk index'], 3)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </>
            )}
          </motion.div>

          {/* Top risky components */}
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.35, duration: 0.5 }}
            className="col-span-1 lg:col-span-2 bg-slate-800/60 border border-slate-700 rounded-xl p-5 backdrop-blur-sm"
          >
            <h3 className="text-white font-semibold text-sm mb-1">Most-Flagged Components</h3>
            <p className="text-slate-400 text-xs mb-3">Top 5 by catalogue risk index</p>
            {loading ? (
              <div className="h-40 flex items-center justify-center text-slate-400 text-sm">Loading…</div>
            ) : dataError ? (
              <DataUnavailable height="h-40" />
            ) : (
              <div className="space-y-2">
                {[...components]
                  .sort((a, b) => b.risk_score - a.risk_score)
                  .slice(0, 5)
                  .map((c, i) => {
                    const tier = catalogueRiskTier(c.risk_factors, c.risk_score);
                    return (
                      <motion.div
                        key={c.id}
                        initial={{ opacity: 0, x: 12 }}
                        animate={{ opacity: 1, x: 0 }}
                        transition={{ delay: 0.4 + i * 0.06 }}
                        className="flex items-center justify-between bg-slate-900/50 rounded-lg px-3 py-2"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-white text-xs font-medium truncate">{c.mpn}</p>
                          {/* The flags are what the row is really ranked on, and
                              they are falsifiable in a way the number is not —
                              so they sit next to the manufacturer, not hidden. */}
                          <p className="text-slate-400 text-xs truncate">
                            {c.manufacturer}
                            {' · '}
                            <span style={{ color: CATALOGUE_RISK_COLORS[tier] }}>
                              {c.risk_factors?.length
                                ? c.risk_factors.map(formatRiskFactor).join(', ')
                                : CATALOGUE_RISK_LABELS.unflagged}
                            </span>
                          </p>
                        </div>
                        <div className="flex items-center gap-3 text-xs shrink-0 ml-2">
                          <span className="text-slate-400">{c.num_offers} offers</span>
                          <span className="font-semibold text-slate-200">
                            {formatRiskIndex(c.risk_score)}
                          </span>
                        </div>
                      </motion.div>
                    );
                  })}
              </div>
            )}
          </motion.div>
        </div>

        {/* ── Live Feeds Status ──────────────────────────────────────────────── */}
        <div className="grid grid-cols-1 gap-4 mb-6">
          <motion.div
            initial={{ opacity: 0, scale: 0.97 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: 0.5, duration: 0.5 }}
            className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 backdrop-blur-sm"
            aria-live="polite"
          >
            <div className="mb-3">
              <h3 className="text-white font-semibold text-sm">Live Feeds</h3>
              <p className="text-slate-400 text-xs mt-0.5">External signals refresh every 15 min · status checked every {FEED_POLL_LABEL}</p>
            </div>
            {feedError ? (
              <p className="text-slate-400 text-xs py-2">Feed status unavailable. Refresh to retry.</p>
            ) : (
              <div className="space-y-0">
                {(feedStatus.length > 0 ? feedStatus : [
                  { name: 'GPR Index', fetched_at: null, status: 'unavailable' as const, value_summary: null },
                  { name: 'ACLED Conflict', fetched_at: null, status: 'unavailable' as const, value_summary: null },
                  { name: 'IMF PortWatch', fetched_at: null, status: 'unavailable' as const, value_summary: null },
                  { name: 'FRED Freight', fetched_at: null, status: 'unavailable' as const, value_summary: null },
                ]).map((feed) => (
                  <div
                    key={feed.name}
                    className="flex items-center justify-between py-2 hover:bg-slate-900/50 rounded px-2 -mx-2"
                    title={
                      feed.detail
                        ? feed.detail
                        : feed.fetched_at
                          ? `Last fetched: ${feed.fetched_at}`
                          : undefined
                    }
                  >
                    <span className="text-xs font-semibold text-slate-200">{feed.name}</span>
                    <div className="flex items-center gap-3">
                      <span className="text-xs text-slate-400 tabular-nums">
                        {formatFeedTime(feed.fetched_at)}
                      </span>
                      {feed.status === 'live' && (
                        <span className="inline-flex items-center gap-1.5 bg-green-500/10 border border-green-500/30 text-green-400 text-xs px-2 py-0.5 rounded-full">
                          <span className="w-1.5 h-1.5 rounded-full bg-green-400 motion-safe:animate-pulse" />
                          Live
                        </span>
                      )}
                      {feed.status === 'stale' && (
                        <span className="inline-flex items-center gap-1.5 bg-amber-500/10 border border-amber-500/30 text-amber-400 text-xs px-2 py-0.5 rounded-full">
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                          Stale
                        </span>
                      )}
                      {feed.status === 'inactive' && (
                        <span className="inline-flex items-center gap-1.5 bg-slate-700/40 border border-slate-600/40 text-slate-400 text-xs px-2 py-0.5 rounded-full">
                          <span className="w-1.5 h-1.5 rounded-full border border-slate-500 bg-transparent" />
                          Inactive (no key)
                        </span>
                      )}
                      {feed.status === 'unavailable' && (
                        <span className="inline-flex items-center gap-1.5 bg-slate-700/40 border border-slate-600/40 text-slate-400 text-xs px-2 py-0.5 rounded-full">
                          <span className="w-1.5 h-1.5 rounded-full border border-slate-500 bg-transparent" />
                          Unavailable
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </motion.div>
        </div>

        {/* ── Row 4: Navigation Cards ───────────────────────────────────────── */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
          {NAV.map((item, i) => {
            const Icon = item.icon;
            return (
            <motion.button
              key={item.path}
              onClick={() => navigate(item.path)}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.4 + i * 0.05, duration: 0.4 }}
              whileHover={{ scale: 1.02, y: -2 }}
              whileTap={{ scale: 0.98 }}
              className={`text-left bg-slate-800/60 rounded-xl p-4 border border-slate-700 cursor-pointer transition-colors duration-200 backdrop-blur-sm ${item.border}`}
            >
              <div className="flex items-center justify-between mb-2">
                <Icon size={16} className="shrink-0" aria-hidden="true" />
                <span className="text-xs bg-slate-700/80 text-slate-400 px-2 py-0.5 rounded-full">{item.badge}</span>
              </div>
              <h3 className="text-white font-semibold text-sm mb-1">{item.title}</h3>
              <p className="text-slate-400 text-xs leading-relaxed">{item.desc}</p>
            </motion.button>
            );
          })}
        </div>

        {/* ── Footer: Capability Pills ─────────────────────────────────────── */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.6 }}
          className="flex flex-wrap gap-2"
        >
          {[
            'CP-SAT fixed-charge sourcing MILP + OR-Tools TSP pickup tour',
            'Cost / time / CO₂ weighting ranks the solved plans',
            dataError ? 'Real electronic components' : `${components.length} real electronic components`,
            'Monte Carlo ETA (n=1000)',
            dataError ? 'Real distributors worldwide' : `${distributors.length} real distributors worldwide`,
            'Digital twin what-if scenarios',
            'Real pricing — static 2024 snapshot (CC-BY-4.0)',
          ].map((cap) => (
            <span key={cap} className="text-xs bg-slate-800/60 border border-slate-700 text-slate-400 px-3 py-1 rounded-full">
              {cap}
            </span>
          ))}
        </motion.div>
      </div>
    </div>
  );
};
