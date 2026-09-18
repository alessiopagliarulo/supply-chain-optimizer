/**
 * 404.
 *
 * Every path that is not one of the three pages lands here - including the pages this
 * app used to have (/login, /dashboard, /resilience, ...). Saying so beats quietly
 * redirecting: a typo, a stale bookmark and a broken link would otherwise all look
 * like success.
 */
import { Link, useLocation } from 'react-router-dom';
import { Compass } from 'lucide-react';

export default function NotFoundPage() {
  const location = useLocation();

  return (
    <div className="h-full overflow-y-auto bg-slate-950 flex items-center justify-center px-6 py-12">
      <div className="flex flex-col items-center text-center gap-4 max-w-md">
        <Compass className="w-10 h-10 text-slate-600" aria-hidden="true" />
        <p className="text-6xl font-semibold text-slate-600 tabular-nums leading-none">404</p>
        <h1 className="text-2xl font-semibold text-white">This page doesn't exist</h1>
        <p className="text-sm text-slate-400 leading-relaxed">
          Nothing is routed at{' '}
          <code className="bg-slate-800 px-1.5 py-0.5 rounded text-slate-300 break-all">{location.pathname}</code>. The
          app has three pages: Route Plan, Simulation and Benchmarks.
        </p>
        <Link
          to="/"
          className="mt-2 inline-flex items-center min-h-[44px] px-4 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-semibold text-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950"
        >
          Back to the start
        </Link>
      </div>
    </div>
  );
}
