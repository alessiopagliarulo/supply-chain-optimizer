import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { startWarmup } from './services/warmup'

// Fired BEFORE the first render, so a deep link to any page starts waking the API
// immediately. Fire-and-forget: nothing awaits it and every failure inside is swallowed,
// so it cannot delay or break the paint. Once per page load, not once per route.
//
// Exception: the public landing page at "/" must render with ZERO network requests, so
// it is reachable with no backend running at all. Read window.location directly rather
// than a router hook: this runs before <App> (and its <Router>) mounts. Leaving "/" for
// a page warms the API from that page's own first request.
if (window.location.pathname !== '/') {
  startWarmup();
}

const rootElement = document.getElementById('root');
if (rootElement) {
  createRoot(rootElement).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
