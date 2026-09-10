import { useEffect, useRef, useState } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate, Outlet, useLocation, Link } from 'react-router-dom';
import { useAuthStore } from './store/authStore';
import { useCartStore } from './store/cartStore';
import NavBar from './components/NavBar';
import ErrorBoundary from './components/ErrorBoundary';
import WakeNotice from './components/WakeNotice';
import { startWarmup, useElapsedSeconds } from './services/warmup';
import { Login } from './pages/Login';
import LandingPage from './pages/LandingPage';
import Register from './pages/Register';
import { Dashboard } from './pages/Dashboard';
import MapPage from './pages/MapPage';
import SchedulerPage from './pages/SchedulerPage';
import CartPage from './pages/CartPage';
import CheckoutPage from './pages/CheckoutPage';
import BenchmarkPage from './pages/BenchmarkPage';
import ResiliencePage from './pages/ResiliencePage';
import ModelCardPage from './pages/ModelCardPage';
import FrontierPage from './pages/FrontierPage';
import NewsvendorPage from './pages/NewsvendorPage';
import NotFoundPage from './pages/NotFoundPage';
import './index.css';

/**
 * Shown while the stored session cookie is being validated against /auth/me.
 *
 * This is the OTHER cold-start entry point, and the worse one: a returning visitor
 * deep-linking to /benchmark hits it before any page renders, and /auth/me carries the
 * 150s cold-start timeout. Left bare it is a spinner with no explanation for up to two
 * minutes. After three seconds it borrows the same WakeNotice the login screen uses —
 * one mechanism, two places, no second story about what is happening.
 */
function AuthSplash() {
  const [startedAt] = useState(() => performance.now());
  const elapsed = useElapsedSeconds(startedAt);

  return (
    <div className="h-screen w-screen bg-slate-900 flex items-center justify-center p-4">
      <div className="flex flex-col items-center gap-3 w-full max-w-md">
        <div className="w-8 h-8 border-2 border-slate-700 border-t-blue-500 rounded-full animate-spin" />
        <span className="text-slate-500 text-sm">Restoring session…</span>
        {elapsed >= 3 && <WakeNotice requestSec={elapsed} />}
      </div>
    </div>
  );
}

/**
 * A stored token that GET /auth/me could not confirm — the free-tier backend was
 * asleep or unreachable, NOT a 401.
 *
 * What used to happen instead: the store kept `isAuthenticated: true` with `user`
 * null, so the app rendered as normal with no Logout control anywhere, a blank
 * "Welcome back, " on the dashboard, and a map that drew markers and zero routes.
 * Bouncing to /login would be the other lie — the session was never rejected — so
 * this says exactly what is known and offers the two real options.
 */
function SessionUnverified({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="h-screen w-screen bg-slate-900 flex items-center justify-center p-4">
      <div className="w-full max-w-md bg-slate-800/60 border border-slate-700 rounded-xl p-5">
        <h1 className="text-lg font-semibold text-slate-100">Couldn&apos;t reach the backend</h1>
        <p className="text-sm text-slate-400 mt-2">
          Your sign-in is still stored, but the server did not answer when we asked it to
          confirm the session, so nothing below can be shown honestly. The API runs on
          Render&apos;s free tier: it sleeps after ~15 minutes idle and a cold start takes
          up to ~2 minutes.
        </p>
        <div className="flex items-center gap-3 mt-4">
          <button
            onClick={onRetry}
            className="bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium px-3 py-2 rounded transition-colors focus:outline-none focus:ring-2 focus:ring-blue-400"
          >
            Try again
          </button>
          <Link
            to="/login"
            className="text-sm text-slate-300 hover:text-white underline underline-offset-2"
          >
            Sign in again
          </Link>
        </div>
      </div>
    </div>
  );
}

function ProtectedLayout() {
  const { isAuthenticated, authResolved, sessionUnverified, retrySession } = useAuthStore();
  const { fetchCart } = useCartStore();
  const location = useLocation();

  useEffect(() => {
    if (isAuthenticated) fetchCart();
  }, [isAuthenticated]);

  // Until initializeAuth() has resolved we know NOTHING about the session. Rendering
  // <Navigate to="/login"> here is what dumped logged-in users at the login screen on
  // every refresh and deep link — the redirect fired before the cookie was ever read.
  if (!authResolved) return <AuthSplash />;
  // "We could not check" is a third state, distinct from signed-in and signed-out.
  if (sessionUnverified) return <SessionUnverified onRetry={() => { void retrySession(); }} />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;

  return (
    <div className="flex flex-col h-screen">
      <NavBar />
      <div className="flex-1 overflow-hidden">
        {/* Per-page boundary: a crash in one page keeps the nav usable, and
            navigating elsewhere resets it via the pathname resetKey. */}
        <ErrorBoundary scope="This page" resetKey={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </div>
    </div>
  );
}

/** Login/Register shouldn't be reachable once a session is already restored. */
function PublicOnly({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, authResolved } = useAuthStore();
  if (!authResolved) return <AuthSplash />;
  if (isAuthenticated) return <Navigate to="/dashboard" replace />;
  return <>{children}</>;
}

/**
 * Resolves the session — but never on the public landing page.
 *
 * This CANNOT be a plain effect in App(): App() renders <Router>, so it has no router
 * context, and a `window.location` check there reads the pathname exactly once, at
 * mount. That is the trap. A visitor who lands on "/" and then clicks through to the
 * app navigates client-side, App() never re-mounts, the effect never re-runs, and
 * `authResolved` stays false for the life of the tab — so PublicOnly and ProtectedLayout
 * both render <AuthSplash /> forever. The CTA would spin and never resolve.
 *
 * Living inside <Router> means useLocation() re-evaluates on every navigation, so the
 * session resolves the moment the visitor leaves "/" — and not one request before.
 */
function AuthBootstrap() {
  const { initializeAuth, authResolved } = useAuthStore();
  const location = useLocation();
  const started = useRef(false);

  useEffect(() => {
    // "/" stays offline: no auth probe, no warmup, nothing.
    if (location.pathname === '/') return;
    if (started.current || authResolved) return;
    started.current = true;

    // The visitor has committed to the app, so start waking the API now rather than
    // after the auth call fails — main.tsx skipped this if they entered via "/".
    startWarmup();
    initializeAuth();
  }, [location.pathname, authResolved, initializeAuth]);

  return null;
}

function App() {
  return (
    // Root boundary: the last line of defence. Anything the per-page boundary
    // cannot catch (nav bar, router, layout) lands here instead of a white screen.
    <ErrorBoundary scope="The app">
      <Router>
        <AuthBootstrap />
        <Routes>
          {/* Public, no-login landing page. Deliberately NOT wrapped in PublicOnly
              (which renders AuthSplash while auth resolves) and NOT inside
              ProtectedLayout — it must never depend on auth state or NavBar. */}
          <Route path="/" element={<LandingPage />} />
          <Route path="/login" element={<PublicOnly><Login /></PublicOnly>} />
          <Route path="/register" element={<PublicOnly><Register /></PublicOnly>} />
          <Route element={<ProtectedLayout />}>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/map" element={<MapPage />} />
            {/* Canonical paths now match the nav labels and the page content.
                The old paths stay mounted so existing links keep working. */}
            <Route path="/components" element={<SchedulerPage />} />
            <Route path="/scheduler" element={<SchedulerPage />} />
            <Route path="/cart" element={<CartPage />} />
            <Route path="/optimize" element={<CheckoutPage />} />
            <Route path="/checkout" element={<CheckoutPage />} />
            <Route path="/benchmark" element={<BenchmarkPage />} />
            <Route path="/resilience" element={<ResiliencePage />} />
            <Route path="/frontier" element={<FrontierPage />} />
            <Route path="/newsvendor" element={<NewsvendorPage />} />
            <Route path="/model-card" element={<ModelCardPage />} />
            {/* A real 404 rather than the old silent <Navigate to="/dashboard">.
                Redirecting an unknown URL to the dashboard makes a typo, a stale
                bookmark and a genuinely broken link all look identical — and all
                look like success. It lives INSIDE the protected layout so it keeps
                the nav bar; a logged-out visitor still lands on /login first, the
                same as every other route here. */}
            <Route path="*" element={<NotFoundPage />} />
          </Route>
          {/* /digital-twin removed: the page called a legacy "simplified" endpoint,
              did no re-optimization, and rendered fields the API never returned.
              Resilience covers the same ground with real Monte Carlo + CVaR. */}
          <Route path="/digital-twin" element={<Navigate to="/resilience" replace />} />
        </Routes>
      </Router>
    </ErrorBoundary>
  );
}

export default App;
