/**
 * Public, no-login landing page at `/`.
 *
 * Renders with ZERO network requests: every number on this page is imported from
 * `generated/landingData.ts`, a build-time artifact read out of committed JSON/DB
 * files (see scripts/build-landing-data.mjs and its backend contract test). This
 * component must never import services/api, store/authStore, or anything that
 * transitively touches the network — that is the entire point of the page existing
 * outside ProtectedLayout and PublicOnly.
 */
import { motion } from 'framer-motion';
import { Link } from 'react-router-dom';
import {
  ArrowRight,
  Boxes,
  CheckCircle2,
  Database,
  Gauge,
  ShieldCheck,
  TrendingDown,
  Warehouse,
} from 'lucide-react';
import {
  landingStats,
  landingProofPoints,
  catalogue,
  type LandingStat,
} from '../generated/landingData';

/** Small, tasteful rounding for display only — the underlying value is untouched. */
function formatStatValue(stat: LandingStat): string {
  const { value, unit } = stat;
  // Values already carry deliberate precision (e.g. 4.266×, 2.24 ms); round to a
  // reasonable display precision without ever re-deriving the number.
  const decimals = Number.isInteger(value) ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  return `${value.toFixed(decimals)}${unit}`;
}

const STAT_ICONS: Record<string, typeof TrendingDown> = {
  'cost-edge': TrendingDown,
  'cvar-leverage': ShieldCheck,
  'forecast-mape': Gauge,
  'wape-reduction': Gauge,
  shortfall: ShieldCheck,
  latency: Gauge,
};

function StatCard({ stat, index }: { stat: LandingStat; index: number }) {
  const Icon = STAT_ICONS[stat.id] ?? Gauge;
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '-40px' }}
      transition={{ duration: 0.4, delay: index * 0.05 }}
      className="bg-slate-800/70 border border-slate-700 rounded-xl p-6 backdrop-blur-sm flex flex-col gap-3"
    >
      <div className="flex items-center gap-2 text-blue-400">
        <Icon size={18} aria-hidden="true" />
        <span className="text-3xl font-bold text-white tracking-tight tabular-nums">
          {formatStatValue(stat)}
        </span>
      </div>
      <p className="text-slate-200 text-sm font-medium leading-snug">{stat.label}</p>
      <p className="text-slate-400 text-xs leading-relaxed">{stat.detail}</p>
      <p className="text-slate-500 text-[11px] font-mono mt-auto pt-2 border-t border-slate-700/60 break-all">
        {stat.source}
      </p>
    </motion.div>
  );
}

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 text-slate-200">
      {/* ── Minimal header — deliberately NOT NavBar, which reads auth state ── */}
      <header className="border-b border-slate-800">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2 text-white font-semibold">
            <Warehouse size={20} className="text-blue-400" aria-hidden="true" />
            Electronics Supply Chain Optimizer
          </div>
          <Link
            to="/login"
            className="text-sm font-medium text-slate-200 hover:text-white bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg px-3 py-1.5 transition-colors"
          >
            Sign in
          </Link>
        </div>
      </header>

      <main>
        {/* ── Hero ─────────────────────────────────────────────────────── */}
        <section className="max-w-6xl mx-auto px-6 pt-16 pb-12 text-center">
          <motion.div
            initial={{ opacity: 0, y: -12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5 }}
          >
            <span className="inline-flex items-center gap-1.5 bg-blue-500/10 border border-blue-500/30 text-blue-300 text-xs font-medium px-3 py-1 rounded-full mb-6">
              <Database size={12} aria-hidden="true" />
              {catalogue.partsPriced.toLocaleString()} priced parts &middot;{' '}
              {catalogue.distributorsQuoting} distributors &middot;{' '}
              {catalogue.offers.toLocaleString()} offers
            </span>
            <h1 className="text-4xl sm:text-5xl font-bold text-white tracking-tight leading-tight max-w-3xl mx-auto">
              A sourcing optimizer for real electronic-component supply chains
            </h1>
            <p className="text-slate-400 text-lg mt-5 max-w-2xl mx-auto leading-relaxed">
              CP-SAT mixed-integer solving, Monte Carlo risk analysis, and a Prophet demand
              forecaster run over a real catalogue of {catalogue.parts} electronic components,{' '}
              {catalogue.partsPriced} of which are priced by {catalogue.distributorsQuoting}{' '}
              distributors &mdash; a static 2024 Nexar/Octopart snapshot, not synthetic data and
              not a live feed.
            </p>
            <div className="flex flex-col items-center gap-2 mt-8">
              <Link
                to="/login"
                className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-500 text-white font-semibold px-6 py-3 rounded-lg transition-colors focus:outline-none focus:ring-2 focus:ring-blue-400 focus:ring-offset-2 focus:ring-offset-slate-950"
              >
                Try the live app &mdash; demo credentials included
                <ArrowRight size={18} aria-hidden="true" />
              </Link>
              <p className="text-slate-500 text-xs">
                Free-tier backend; first load can take up to a minute to wake.
              </p>
            </div>
          </motion.div>
        </section>

        {/* ── Headline stats ───────────────────────────────────────────── */}
        <section className="max-w-6xl mx-auto px-6 py-10">
          <h2 className="text-center text-sm font-semibold uppercase tracking-wider text-slate-500 mb-6">
            Every number below traces to a committed artifact
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {landingStats.map((stat, i) => (
              <StatCard key={stat.id} stat={stat} index={i} />
            ))}
          </div>
        </section>

        {/* ── Proof points ─────────────────────────────────────────────── */}
        <section className="max-w-6xl mx-auto px-6 py-10">
          <h2 className="text-center text-2xl font-bold text-white mb-2">
            Verified, not asserted
          </h2>
          <p className="text-center text-slate-400 text-sm mb-8 max-w-xl mx-auto">
            The credibility of an optimizer is in what got checked, not what got claimed.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {landingProofPoints.map((point, i) => (
              <motion.div
                key={point.label}
                initial={{ opacity: 0, y: 16 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true, margin: '-40px' }}
                transition={{ duration: 0.4, delay: i * 0.05 }}
                className="bg-slate-800/50 border border-slate-700 rounded-xl p-5 flex gap-4"
              >
                <CheckCircle2 size={20} className="text-emerald-400 flex-shrink-0 mt-0.5" aria-hidden="true" />
                <div className="flex flex-col gap-1">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="text-xl font-bold text-white tabular-nums">{point.value}</span>
                    <span className="text-slate-200 text-sm font-medium">{point.label}</span>
                  </div>
                  <p className="text-slate-400 text-xs leading-relaxed">{point.detail}</p>
                  <p className="text-slate-500 text-[11px] font-mono mt-1 break-all">{point.source}</p>
                </div>
              </motion.div>
            ))}
          </div>
        </section>

        {/* ── Catalogue callout ─────────────────────────────────────────── */}
        <section className="max-w-6xl mx-auto px-6 py-10">
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: '-40px' }}
            transition={{ duration: 0.4 }}
            className="bg-slate-800/70 border border-slate-700 rounded-xl p-6 backdrop-blur-sm flex flex-col sm:flex-row items-center gap-4 sm:gap-8 justify-center text-center sm:text-left"
          >
            <Boxes size={28} className="text-indigo-400 flex-shrink-0" aria-hidden="true" />
            <p className="text-slate-300 text-sm leading-relaxed">
              <span className="text-white font-semibold">
                {catalogue.parts} parts &middot; {catalogue.distributors} distributors &middot;{' '}
                {catalogue.offers.toLocaleString()} offers.
              </span>{' '}
              Read from <code className="font-mono text-slate-400">backend/supply_chain.db</code> &mdash;
              the same database the live API serves. Where real pricing or supplier data doesn&apos;t
              exist, the app says so instead of filling the gap.
            </p>
          </motion.div>
        </section>

        {/* ── CTA ──────────────────────────────────────────────────────── */}
        <section className="max-w-6xl mx-auto px-6 pt-6 pb-20 text-center">
          <h2 className="text-2xl font-bold text-white mb-3">See it running on real data</h2>
          <p className="text-slate-400 text-sm max-w-xl mx-auto mb-6">
            Sign in with the published demo account to browse the catalogue, build a BOM, and run
            the CP-SAT sourcing optimizer yourself.
          </p>
          <Link
            to="/login"
            className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-500 text-white font-semibold px-6 py-3 rounded-lg transition-colors focus:outline-none focus:ring-2 focus:ring-blue-400 focus:ring-offset-2 focus:ring-offset-slate-950"
          >
            Open the app
            <ArrowRight size={18} aria-hidden="true" />
          </Link>
          <p className="text-slate-500 text-xs mt-3">
            Demo login: <code className="font-mono">demo@example.com</code> / <code className="font-mono">demo</code>
            {' '}&mdash; free-tier backend, first load may take up to a minute.
          </p>
        </section>
      </main>

      <footer className="border-t border-slate-800">
        <div className="max-w-6xl mx-auto px-6 py-6 text-center text-slate-600 text-xs">
          Built with FastAPI, OR-Tools CP-SAT, Prophet, and React &mdash; every figure on this page
          traces to a committed artifact, not a slide.
        </div>
      </footer>
    </div>
  );
}
