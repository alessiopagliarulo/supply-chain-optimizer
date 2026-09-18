# Quick Start — Electronics Supply Chain Optimizer

## ⭐ To SEE the app

Open **https://supply-chain-ui-bhwz.onrender.com** — that is the deployed build of
`main`, and it is the only URL that shows what actually ships. The nav bar's top-right
corner prints `build <hash>`; compare it against
[`/version`](https://supply-chain-api-qy8x.onrender.com/version) on the API (note: no
`/api/v1` prefix — it is a root route) to confirm which version you are looking at. Deployment is automatic on
push to `main` via Render.

Don't judge the app from a localhost server — a local build can pass while the deployed
one is broken, and that is how stale pages get demoed.

---

Everything below is for **local development only** (running the code on your
machine to work on it), not for viewing the app.

Get a fully functional demo running in **under 5 minutes**.
No Docker. No PostgreSQL. No Redis. SQLite + local Python + Node.

---

## Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| Python | 3.11+ | `python3 --version` |
| Node.js | 18+ | `node --version` |
| npm | 9+ | `npm --version` |

---

## 1. Clone / open the project

```bash
cd "path/to/Logisitics Project"
```

---

## 2. Start the backend (FastAPI + SQLite)

The database (`supply_chain.db`) is already seeded with **791 real components, 92 distributors, and 8,176 price offers** — a static 2024 snapshot, originally sourced via the Nexar/Octopart API, redistributed on HuggingFace under CC-BY-4.0 (not a live feed).

```bash
cd backend
python3 -m venv venv                      # first time only
source venv/bin/activate                   # macOS/Linux
# venv\Scripts\activate                   # Windows

pip install -r requirements.txt            # first time only

python -m uvicorn app.main:app --reload --port 8000
```

Backend ready at: http://localhost:8000  
API docs (Swagger UI): http://localhost:8000/docs

---

## 3. Start the frontend (React + Vite)

Open a **new terminal** in the project root:

```bash
cd frontend
npm install                                # first time only
npm run dev
```

Frontend ready at: http://localhost:5173

---

## 4. Open the app

Open http://localhost:5173. There is no login; the landing page links to the three pages.

---

## 5. Demo flow

### Route Plan
Pick a built-in Solomon sample (C101 or R101, 25 customers) or upload your own customer
CSV (the page lists the columns; "Download this instance as CSV" gives you a template).
Choose a solver and press **Solve** to see every route on an x/y plot, with total
distance, vehicles used, feasibility and solver runtime.

### Simulation
Press **Simulate this plan** (or solve a sample on the page itself). Set variability,
distribution, replications and seed, then **Run simulation** for on-time rate, lateness,
completion against depot close and utilization. **Tune buffers** searches schedule and
capacity buffers and charts the evaluated frontier.

### Benchmarks
Shows the committed Solomon benchmark results (`docs/benchmark_results.json`) as a
sortable table and a gap-vs-runtime chart per solver, with provenance. Until that file is
committed, the page says the benchmarks are not generated yet.

---

## Common issues

| Error | Fix |
|-------|-----|
| `SECRET_KEY is insecure` | Check `backend/.env` has `SECRET_KEY=dev-secret-key-2024-supply-chain` |
| `Address already in use (port 8000)` | `pkill -f uvicorn` or use `--port 8001` |
| `Address already in use (port 5173)` | `npm run dev -- --port 5174` |
| sklearn version warnings on startup | Harmless — ETA model falls back to route-derived calculation |

---

## Environment variables (all optional for local demo)

`backend/.env` is **not** in the repository — `.env` is gitignored, so a fresh clone has
none, and `config.py` declares `SECRET_KEY` with no default and will refuse to start
without it. Create it before step 2 with the three lines below; that is the whole of the
required configuration, and every external API key is optional on top of it:

```env
# backend/.env — create this file
DATABASE_URL=sqlite:///./supply_chain.db
SECRET_KEY=dev-secret-key-2024-supply-chain
DEBUG=true

# Optional: enables live geopolitical / freight feed data
FRED_API_KEY=       # free at fred.stlouisfed.org
MAPBOX_API_KEY=     # free tier at mapbox.com (for interactive map)
ACLED_EMAIL=        # free at acleddata.com
ACLED_KEY=
```

The app runs fully without any external keys — live feeds gracefully degrade to cached/static data.

---

## Architecture summary

```
frontend/          React 18 + TypeScript + Tailwind + Recharts + Zustand
backend/
  app/
    api/           FastAPI routers (auth, cart, resilience, graph, ...)
    optimization/  OR-Tools pickup TSP + cross-dock eval, freight costs, newsvendor
    graph/         NetworkX bipartite supply graph (Fiedler, centrality, HHI)
    feeds/         Live data: GPR, ACLED, IMF PortWatch, FRED freight
    ml/            Prophet macro (A34SNO) backtest + Chronos comparison, sklearn
                   lead-time models, intermittent-demand method benchmark
  supply_chain.db  SQLite — 791 components, 92 distributors, 8,176 offers
```

## Key talking points

- **Real data** — static 2024 snapshot (791 components, 8,176 offers), originally sourced via Nexar/Octopart, redistributed on HuggingFace under CC-BY-4.0; not synthetic, not a live feed
- **Graph ML** — Fiedler algebraic connectivity measures network fragility
- **Monte Carlo** — 1,000 disruption scenarios → P10/P50/P90 fulfilment bands
- **Live feeds** — geopolitical risk, port congestion, freight indices
