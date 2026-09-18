# Archived screenshots

> **These images are retired. Do not reuse them anywhere.**

Each file here was published in `README.md` at some point and no longer describes the
deployed product. They are kept only so the history of what was published stays auditable.

| File | Published as | Retired | Why |
|---|---|---|---|
| `sc-checkout-2026-08-18-SUPERSEDED.png` | `README.md` "VRP Optimization (4 strategies)" | 2026-09-02 | Showed the optimizer **failing**: a banner reading "STRATEGIES NOT DIFFERENTIATED — all 4 strategies converge on the same plan", every metric tagged `= TIED`, `0.0 kg` CO2 on all four cards, and the long-renamed "Semiconductor shortage stress" label. On the live build (`85b2890`) the same cart returns 3 distinct plans across 4 strategies — $374.02 / 6.9 d / 89.6 kg vs $747.44 / 4.5 d / 1.49 kg. Replaced by `docs/screenshots/optimize-four-strategies.png`. |
| `sc-resilience-2026-08-18-SUPERSEDED.png` | `README.md` "Resilience Dashboard" | 2026-09-02 | Showed the distributor-failure scenario returning **nulls**: `$0.00` spend at risk, `0.0 d` ETA delta, `0.000` risk delta and "0 components affected" on an $18.40 order. The live scenario on the demo cart returns +28.5% cost, fulfilment 100% → 80%, risk 0.220 → 0.420 and $9.01 CVaR-95 spend at risk. Replaced by `docs/screenshots/resilience-distributor-failure.png`. |
| `resilience-distributor-failure-2026-09-18-RETIRED.png` | `README.md` "Screenshots" (`/resilience`) | 2026-09-18 | The Resilience page was removed when the web app shrank to Route Plan, Simulation and Benchmarks (issue #16). The capture was also already stale in most figures; see the README revision before this date. |
| `sc-dashboard-2026-09-18-RETIRED.png` | not referenced at retirement | 2026-09-18 | The Dashboard page was removed with the sourcing-era pages (issue #16). |
