import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { startWarmup } from './services/warmup'

// Fired BEFORE the first render, and before the router has decided which route this is,
// so a deep link to /benchmark or /frontier warms the API exactly like a landing on any
// authenticated route. It is fire-and-forget: nothing below awaits it, and every failure
// inside is swallowed, so it cannot delay or break the paint. Once per page load — not
// once per route.
//
// Exception: the public landing page at "/" must render with ZERO network requests — it
// is meant to be reachable with no backend running at all. Read window.location directly
// rather than a router hook: this runs before <App> (and its <Router>) even mounts.
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
