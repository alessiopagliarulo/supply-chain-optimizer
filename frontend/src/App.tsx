import { Suspense, lazy, useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Outlet, Navigate, useLocation } from 'react-router-dom';
import NavBar from './components/NavBar';
import ErrorBoundary from './components/ErrorBoundary';
import LandingPage from './pages/LandingPage';
import NotFoundPage from './pages/NotFoundPage';
import { startWarmup } from './services/warmup';

// Each page (and the map and charting libraries they use) loads on first visit, so the
// landing page ships only what it renders.
const MapPage = lazy(() => import('./pages/MapPage'));
const RoutePlanPage = lazy(() => import('./pages/RoutePlanPage'));
const DigitalTwinPage = lazy(() => import('./pages/DigitalTwinPage'));
const BenchmarksPage = lazy(() => import('./pages/BenchmarksPage'));
import './index.css';

function AppLayout() {
  const location = useLocation();
  // A visitor who entered on "/" (which stays offline) starts waking the API the moment
  // they open a page. A no-op when main.tsx already fired it for a deep link.
  useEffect(() => {
    startWarmup();
  }, []);
  return (
    <div className="flex flex-col h-screen">
      <NavBar />
      <div className="flex-1 overflow-hidden">
        {/* Per-page boundary: a crash in one page keeps the nav usable, and
            navigating elsewhere resets it via the pathname resetKey. */}
        <ErrorBoundary scope="This page" resetKey={location.pathname}>
          <Suspense fallback={<div className="h-full bg-slate-950" aria-busy="true" />}>
            <Outlet />
          </Suspense>
        </ErrorBoundary>
      </div>
    </div>
  );
}

/**
 * Exactly four pages plus the landing page: Map, Route Plan, Digital Twin, Benchmarks.
 * No login: every page is public. `/simulation` (the Digital Twin's former name) moves
 * to its new address; any other path - including the removed sourcing-era pages - is
 * a 404.
 */
function App() {
  return (
    // Root boundary: the last line of defence for anything the per-page boundary
    // cannot catch (nav bar, router, layout).
    <ErrorBoundary scope="The app">
      <Router>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route element={<AppLayout />}>
            <Route path="/map" element={<MapPage />} />
            <Route path="/route-plan" element={<RoutePlanPage />} />
            <Route path="/digital-twin" element={<DigitalTwinPage />} />
            <Route path="/simulation" element={<Navigate to="/digital-twin" replace />} />
            <Route path="/benchmarks" element={<BenchmarksPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Routes>
      </Router>
    </ErrorBoundary>
  );
}

export default App;
