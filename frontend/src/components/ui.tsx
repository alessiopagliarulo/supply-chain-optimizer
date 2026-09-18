import { useState, type ReactNode } from 'react';
import { AlertTriangle, Loader2 } from 'lucide-react';
import WakeNotice from './WakeNotice';
import { useElapsedSeconds } from '../services/warmup';

/** The page frame: a scrollable column with a heading and a one-line purpose. */
export function Page({ title, intro, children }: { title: string; intro: ReactNode; children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto bg-slate-950">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8 flex flex-col gap-6">
        <header className="flex flex-col gap-2">
          <h1 className="text-2xl font-semibold text-white">{title}</h1>
          <p className="text-sm text-slate-400 leading-relaxed max-w-3xl">{intro}</p>
        </header>
        {children}
      </div>
    </div>
  );
}

export function Card({ title, children, className = '' }: { title?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`bg-slate-900 border border-slate-800 rounded-xl p-4 sm:p-5 flex flex-col gap-4 min-w-0 ${className}`}>
      {title && <h2 className="text-base font-semibold text-slate-100">{title}</h2>}
      {children}
    </section>
  );
}

/** A labelled form control. `hint` is the one line under it. */
export function Field({ label, hint, htmlFor, children }: { label: string; hint?: ReactNode; htmlFor: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5 min-w-0">
      <label htmlFor={htmlFor} className="text-sm font-medium text-slate-300">
        {label}
      </label>
      {children}
      {hint && <p className="text-xs text-slate-400 leading-relaxed">{hint}</p>}
    </div>
  );
}

export const inputClass =
  'w-full min-h-[44px] bg-slate-950 border border-slate-700 rounded-lg px-3 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500';

export const buttonClass =
  'inline-flex items-center justify-center gap-2 min-h-[44px] px-4 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-400 disabled:cursor-not-allowed text-white text-sm font-semibold transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900';

export const secondaryButtonClass =
  'inline-flex items-center justify-center gap-2 min-h-[44px] px-4 rounded-lg border border-slate-700 hover:bg-slate-800 text-slate-200 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900';

/** One headline figure. `tone` colours the value, never the label. */
export function Stat({ label, value, detail, tone = 'neutral' }: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: 'neutral' | 'good' | 'bad';
}) {
  const color = tone === 'good' ? 'text-emerald-400' : tone === 'bad' ? 'text-red-400' : 'text-white';
  return (
    <div className="bg-slate-950 border border-slate-800 rounded-lg px-4 py-3 flex flex-col gap-1 min-w-0">
      <span className="text-xs font-medium text-slate-400">{label}</span>
      <span className={`text-xl font-semibold tabular-nums ${color}`}>{value}</span>
      {detail && <span className="text-xs text-slate-400 leading-relaxed">{detail}</span>}
    </div>
  );
}

export function ErrorBox({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div role="alert" className="flex gap-3 bg-red-950/40 border border-red-900 rounded-lg p-3 text-sm">
      <AlertTriangle className="w-5 h-5 text-red-400 shrink-0" aria-hidden="true" />
      <div className="flex flex-col gap-1 min-w-0">
        <span className="font-semibold text-red-200">{title}</span>
        <div className="text-red-200/90 leading-relaxed break-words">{children}</div>
      </div>
    </div>
  );
}

/**
 * "Working on it", with the cold-start explanation once a request has run for three
 * seconds - the API sleeps on Render's free tier and can take minutes to wake.
 */
export function Busy({ what }: { what: string }) {
  const [startedAt] = useState(() => performance.now());
  const elapsed = useElapsedSeconds(startedAt);
  return (
    <div className="flex flex-col gap-3" aria-live="polite">
      <div className="flex items-center gap-2 text-sm text-slate-300">
        <Loader2 className="w-4 h-4 animate-spin text-blue-400" aria-hidden="true" />
        {what}
      </div>
      {elapsed >= 3 && <WakeNotice requestSec={elapsed} />}
    </div>
  );
}
