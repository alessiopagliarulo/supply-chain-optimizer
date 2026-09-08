# Resilience Scenario API Reference

## Base URL

```
http://localhost:8000/api/v1
```

## Authentication

All endpoints are public (no authentication required).

## Endpoints

### POST /resilience/distributor-failure

Simulate the failure of a specific distributor. Returns cost, delivery time, risk score, and fulfillment rate deltas.

**Request:**
```json
{
  "distributor_id": 5,
  "bom_component_ids": [1, 2, 3, 4, 5]
}
```

**Parameters:**
- `distributor_id` (int, required): ID of the distributor to simulate failing.
- `bom_component_ids` (array of int, required): Component IDs in the BOM. Max 200.

**Response (200 OK):**
```json
{
  "baseline_cost_usd": 250.50,
  "scenario_cost_usd": 287.75,
  "cost_delta_pct": 14.9,
  "baseline_eta_days": 21,
  "scenario_eta_days": 24,
  "eta_delta_days": 3,
  "baseline_risk_score": 0.32,
  "scenario_risk_score": 0.58,
  "risk_delta": 0.26,
  "baseline_fulfillment_p10": 0.85,
  "baseline_fulfillment_p50": 0.95,
  "baseline_fulfillment_p90": 0.99,
  "scenario_fulfillment_p10": 0.68,
  "scenario_fulfillment_p50": 0.88,
  "scenario_fulfillment_p90": 0.97,
  "affected_bom_ids": [1, 3],
  "affected_suppliers": ["Arrow Electronics", "RS Components"]
}
```

**Error Responses:**
- `400 Bad Request`: Invalid distributor_id, BOM too large (>200), or empty BOM.
- `503 Service Unavailable`: Graph not loaded (system starting up).

**Cache:** Results cached for 1 hour. Repeated calls with same params return <50ms.

---

### POST /resilience/geopolitical-risk

Simulate a geopolitical risk event (e.g., war, sanctions, trade tensions). **This endpoint reads no live feed.** `risk_multiplier` is a stress dial applied to each component's STORED `risk_score` column, and is passed into the Monte Carlo as a scenario stress factor. The sentence here previously said "live feeds (GPR, ACLED) are multiplied by the risk factor"; that was false, and the retraction now stands in `backend/app/api/resilience.py` and on the OpenAPI spec too. The live GPR signal does reach the CP-SAT objective — but in `optimization/sourcing.py`, on `/optimize/*`, not here.

**Request:**
```json
{
  "risk_multiplier": 2.5,
  "bom_component_ids": [1, 2, 3, 4, 5]
}
```

**Parameters:**
- `risk_multiplier` (float, required): Multiplier on the stored `risk_score` column (NOT on any live feed value). Range: 0.5–5.0.
  - 0.5: Low risk scenario (e.g., de-escalation)
  - 1.0: Baseline (current state)
  - 2.0: Moderate risk (e.g., trade war intensifies)
  - 5.0: Severe crisis (e.g., major conflict)
- `bom_component_ids` (array of int, required): Component IDs in the BOM. Max 200.

**Response (200 OK):**
```json
{
  "baseline_cost_usd": 250.50,
  "scenario_cost_usd": 270.40,
  "cost_delta_pct": 7.9,
  "baseline_eta_days": 21,
  "scenario_eta_days": 22,
  "eta_delta_days": 1,
  "baseline_risk_score": 0.32,
  "scenario_risk_score": 0.51,
  "risk_delta": 0.19,
  "baseline_fulfillment_p10": 0.85,
  "baseline_fulfillment_p50": 0.95,
  "baseline_fulfillment_p90": 0.99,
  "scenario_fulfillment_p10": 0.78,
  "scenario_fulfillment_p50": 0.91,
  "scenario_fulfillment_p90": 0.97,
  "affected_bom_ids": [1, 4],
  "affected_suppliers": ["Heilind Asia", "Tech Data"]
}
```

**Cache:** Results cached for 1 hour.

---

### POST /resilience/delivery-target

Simulate a delivery acceleration request. Constrains suppliers to those who can meet the target lead time.

**Request:**
```json
{
  "target_delivery_days": 14,
  "bom_component_ids": [1, 2, 3, 4, 5]
}
```

**Parameters:**
- `target_delivery_days` (int, required): Desired delivery timeframe in days. Range: 1–90.
- `bom_component_ids` (array of int, required): Component IDs in the BOM. Max 200.

**Response (200 OK):**
```json
{
  "baseline_cost_usd": 250.50,
  "scenario_cost_usd": 312.80,
  "cost_delta_pct": 24.9,
  "baseline_eta_days": 21,
  "scenario_eta_days": 14,
  "eta_delta_days": -7,
  "baseline_risk_score": 0.32,
  "scenario_risk_score": 0.38,
  "risk_delta": 0.06,
  "baseline_fulfillment_p10": 0.85,
  "baseline_fulfillment_p50": 0.95,
  "baseline_fulfillment_p90": 0.99,
  "scenario_fulfillment_p10": 0.82,
  "scenario_fulfillment_p50": 0.93,
  "scenario_fulfillment_p90": 0.98,
  "affected_bom_ids": [2, 5],
  "affected_suppliers": [],
  "suppliers_capable": [
    { "name": "Arrow Electronics", "lead_time_days": 10, "cost_per_component_avg": 5.20 },
    { "name": "Tech Data", "lead_time_days": 12, "cost_per_component_avg": 6.10 }
  ],
  "suppliers_cannot_meet": [
    { "name": "Heilind Asia", "min_lead_time_days": 28, "reason": "lead_time_too_long" },
    { "name": "RS Components", "min_lead_time_days": 35, "reason": "lead_time_too_long" }
  ]
}
```

**Cache:** Results cached for 1 hour.

---

## Common Error Codes

| Code | Message | Cause | Resolution |
|------|---------|-------|-----------|
| 400 | Invalid distributor_id | Distributor does not exist or is deactivated | Check /api/v1/distributors for valid IDs |
| 400 | BOM too large | bom_component_ids array exceeds 200 items | Reduce BOM size or split into multiple requests |
| 400 | Empty BOM | bom_component_ids array is empty | Include at least one component ID |
| 400 | Invalid risk_multiplier | Multiplier outside 0.5–5.0 range | Use a multiplier within the range |
| 400 | Invalid target_delivery_days | Days outside 1–90 range | Use a target between 1 and 90 days |
| 503 | Graph not loaded | Graph ML engine is starting up or failed to initialize | Wait 10-30 seconds and retry |

---

## Response Schema

All scenario responses share the following structure:

```typescript
interface ScenarioResponse {
  // Baseline metrics (current state)
  baseline_cost_usd: number;           // Total cost without scenario
  baseline_eta_days: number;           // Estimated delivery time
  baseline_risk_score: number;         // Network risk metric (0–1)
  baseline_fulfillment_p10: number;    // Worst-case fulfillment rate
  baseline_fulfillment_p50: number;    // Median fulfillment rate
  baseline_fulfillment_p90: number;    // Best-case fulfillment rate

  // Scenario metrics (under failure/risk/constraint)
  scenario_cost_usd: number;
  scenario_eta_days: number;
  scenario_risk_score: number;
  scenario_fulfillment_p10: number;
  scenario_fulfillment_p50: number;
  scenario_fulfillment_p90: number;

  // Deltas (scenario - baseline)
  cost_delta_pct: number;              // Percentage change in cost
  eta_delta_days: number;              // Days added/removed from delivery
  risk_delta: number;                  // Absolute change in risk score

  // Impact details
  affected_bom_ids: number[];          // Component IDs with limited alternatives
  affected_suppliers: string[];        // Supplier names that become critical
}

interface DeliveryTargetResponse extends ScenarioResponse {
  suppliers_capable: Array<{
    name: string;
    lead_time_days: number;
    cost_per_component_avg: number;
  }>;
  suppliers_cannot_meet: Array<{
    name: string;
    min_lead_time_days: number;
    reason: "stock_unavailable" | "lead_time_too_long" | "moq_too_high";
  }>;
}
```

---

## Caching

All endpoints cache results with a 1-hour time-to-live (TTL). 

- **Cache key:** SHA256 hash of (endpoint_type, sorted_params)
- **Hit latency:** <50ms (cache lookup + deserialization)
- **Miss latency:** 1–5 seconds (simulation + database write)
- **Cleanup:** Expired entries deleted every 10 minutes

To force a cache miss, modify a parameter slightly (e.g., add a component ID or change distributor by 1).

---

## Rate Limiting

No explicit rate limit, but each scenario simulation is computationally expensive (1,000 Monte Carlo iterations, graph traversal, optimization). Clients should avoid hammering the endpoint; caching handles repeated calls efficiently.

---

## Tracing (Optional)

If OpenTelemetry/Jaeger is running, slow spans (>500ms) are exported:

```
span.name: "distributor_failure_scenario" (or geopolitical_risk_scenario, delivery_target_scenario)
span.attributes:
  - distributor_id (int)
  - risk_multiplier (float)
  - target_delivery_days (int)
  - bom_size (int)
  - cache_hit (bool)
  - result_source ("cache" or "computed")
```

View traces at `http://localhost:16686` (if Jaeger UI is running).

---

## Examples

### Example 1: Distributor Failure Scenario

```bash
curl -X POST http://localhost:8000/api/v1/resilience/distributor-failure \
  -H "Content-Type: application/json" \
  -d '{
    "distributor_id": 5,
    "bom_component_ids": [1, 2, 3]
  }'
```

### Example 2: Geopolitical Risk Scenario

```bash
curl -X POST http://localhost:8000/api/v1/resilience/geopolitical-risk \
  -H "Content-Type: application/json" \
  -d '{
    "risk_multiplier": 2.0,
    "bom_component_ids": [1, 2, 3]
  }'
```

### Example 3: Delivery Target Scenario

```bash
curl -X POST http://localhost:8000/api/v1/resilience/delivery-target \
  -H "Content-Type: application/json" \
  -d '{
    "target_delivery_days": 14,
    "bom_component_ids": [1, 2, 3]
  }'
```

---

## Support

For issues or feature requests, contact the maintainer or check the GitHub repo.
