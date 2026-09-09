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

## 4. Log in with the demo account

1. Open http://localhost:5173
2. Click **"Demo Login"** — no signup required
3. You're in as *Greenville Advanced Manufacturing* (Greenville, SC)

Demo credentials (if using manual login):
- Email: `demo@example.com`
- Password: `demo` *(after registering; Demo Login button is easier)*

The **Demo Login** button mints a private, throwaway demo account per visitor, so two
people clicking through at the same time never see each other's cart. The
`demo@example.com` credentials above sign you into the shared template account that
those private carts are copied from — fine solo, but everyone using it shares one cart.

---

## 5. Full demo flow

### Dashboard
Live risk scores, category breakdown, and KPI cards for 791 real electronic components.

### Scheduler
Select any component to see its 90-day price history and live pricing. (There is no
per-part demand forecast here — the sparkline and "stock-out in ~N weeks" badge that
used to appear were removed along with the tables they read from; see the README's
"What this model can't do" section for why.)

### Map
Interactive US map showing distributor hubs colored by type and risk tier.

### Cart → Checkout (the key demo)
1. Go to **Cart** — pre-loaded with 5 lines / 225 units (ESP32-WROOM-32UE-N4, STM32F103C8T6,
   GD25Q64CSIGR, ESP8266EX, ATMEGA328P-PU — see `backend/seeds/seed_demo_cart.py`)
2. Click **"Optimize & Checkout"**
3. The CP-SAT solver runs in ~12 seconds and returns **4 route strategies**:
   - Cheapest — minimize component + transport cost
   - Fastest — US-only suppliers, minimize delivery days
   - Greenest — minimize CO2 emissions
   - Balanced — weighted multi-objective (recommended)
4. Each card shows cost, ETA (P10/P50/P90 from Monte Carlo), CO2
5. Click any card to see the full route stops, distributor locations, and ETA distribution histogram

### Resilience Dashboard
Simulate supply chain disruptions under three scenarios:
- **Distributor Failure**: pick a distributor, see which BOMs break and rerouting cost
- **Geopolitical Risk**: a 0.5×–5× stress dial on each component's stored risk score
  (it is a what-if multiplier, not a live GPR/ACLED override — the live feeds reach the
  optimizer on `/optimize/*`, not this endpoint)
- **Delivery Target**: slide to 1–90 days and see which suppliers can hit the deadline

---

## Common issues

| Error | Fix |
|-------|-----|
| `SECRET_KEY is insecure` | Check `backend/.env` has `SECRET_KEY=dev-secret-key-2024-supply-chain` |
| `Address already in use (port 8000)` | `pkill -f uvicorn` or use `--port 8001` |
| `Address already in use (port 5173)` | `npm run dev -- --port 5174` |
| Checkout returns 400 "Cart is empty" | Log in with Demo Login first (auth token needed) |
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
    api/           FastAPI routers (auth, cart, optimize, resilience, graph)
    optimization/  CP-SAT sourcing MILP + OR-Tools TSP + cross-dock eval
    graph/         NetworkX bipartite supply graph (Fiedler, centrality, HHI)
    feeds/         Live data: GPR, ACLED, IMF PortWatch, FRED freight
    ml/            Prophet macro (A34SNO) backtest + Chronos comparison, sklearn
                   lead-time models, intermittent-demand method benchmark
  supply_chain.db  SQLite — 791 components, 92 distributors, 8,176 offers
```

## Key talking points

- **Real data** — static 2024 snapshot (791 components, 8,176 offers), originally sourced via Nexar/Octopart, redistributed on HuggingFace under CC-BY-4.0; not synthetic, not a live feed
- **Graph ML** — Fiedler algebraic connectivity measures network fragility
- **Monte Carlo** — 1,000 ETA simulations → P10/P50/P90 confidence bands
- **Multi-objective** — CP-SAT MILP solver, 4 strategies returning 3 distinct sourcing plans on the demo cart (fastest and balanced tie)
- **Live feeds** — geopolitical risk, port congestion, freight indices
