# CVRPTW Benchmarking against Solomon Instances

## Overview

The `supply-chain-optimizer` benchmarks its CVRPTW solvers (CP-SAT exact, Clarke-Wright heuristic, OR-Tools routing) against the standard Solomon benchmark instance set.

Solomon instances (1987) are the canonical CVRPTW benchmarks:
- Six families: C1, C2, R1, R2, RC1, RC2
- 100 customers per family, plus 25 and 50-customer subsets
- Clustered (C), random (R), and random-clustered (RC) distributions
- Short and long time window variants

**Source & License:**
- Instances from https://www.mech.kuleuven.be/en/cib/op/instances
- Best-known solutions from Gehring & Homberger (2002) update
- Public domain / academic benchmark set

## Downloading Instances

### Automated Download

```bash
python backend/scripts/download_solomon_instances.py
```

This downloads all 18 instances (6 families × 3 sizes) to `backend/app/vrp/data/solomon/`.

### Manual Download

Visit https://www.mech.kuleuven.be/en/cib/op/data/text/ and download:
- C101.txt, C1_50.txt, C1_100.txt
- C201.txt, C2_50.txt, C2_100.txt
- R101.txt, R1_50.txt, R1_100.txt
- R201.txt, R2_50.txt, R2_100.txt
- RC101.txt, RC1_50.txt, RC1_100.txt
- RC201.txt, RC2_50.txt, RC2_100.txt

Place all files in `backend/app/vrp/data/solomon/`.

## Running Benchmarks

### Quick Test (25-customer instances only)

```bash
python backend/scripts/benchmark_solomon.py --small
```

Runs ~1 minute per family (time limits: 30s per solver).

### Full Benchmark (all sizes)

```bash
python backend/scripts/benchmark_solomon.py
```

Runs ~5 minutes per family. CP-SAT skips instances > 50 customers (computational complexity).

### Results

Benchmark results are saved to `docs/benchmark_results.json` with:
- **vehicles_used**: number of routes in solution
- **total_distance**: sum of distances
- **gap_percent**: optimality gap vs best-known
- **runtime_seconds**: wall time
- **feasible**: whether solution meets all constraints
- **solver_status**: optimal/feasible/infeasible/no_solution

**Provenance** section records:
- Timestamp and platform (for reproducibility)
- Python and library versions
- Time limits and source reference

## API Usage

### Load a Solomon Instance

```python
from app.vrp.solomon import load_solomon

instance = load_solomon("C1", 100)  # C1 family, 100 customers
print(instance.num_customers)  # 100
print(instance.num_vehicles)   # 10 (C1 has small vehicle capacity)
```

### Solve an Instance

```python
from app.vrp import solve

solution = solve(instance, method="ortools", time_limit_seconds=30)
print(solution.total_cost)     # total distance
print(solution.vehicles_used)  # number of routes
print(solution.feasible)       # whether solution is valid
```

### Get Best-Known Solutions

```python
from app.vrp.solomon import get_best_known

vehicles, distance = get_best_known("C1", 100)
print(f"Best known: {vehicles} vehicles, {distance} distance")
```

## Testing

Unit tests for Solomon loading and benchmarking:

```bash
pytest backend/tests/test_solomon_instances.py -v
```

Tests verify:
- Solomon file parsing (C101 test instance)
- Instance loader creates valid `VrpInstance` objects
- Best-known solutions are defined for all families
- Benchmark results JSON has correct schema

## Instance Format

Solomon instance files are space-separated text:

```
INSTANCE_NAME
VEHICLES 10
CAPACITY 40
CUST NO.   XCOORD.   YCOORD.   DEMAND   READY   DUE   SERVICE
     0       40        50         0       0      1236      0
     1       45        68        10      912     967      90
     ...
```

- Node 0 is always the depot
- Coordinates are planar (Euclidean distance)
- All times and quantities are integers
- Demand and service time are 0 for the depot

## Implementation Notes

- **Distance Scaling**: Euclidean distances are scaled with factor 1 (unit distance)
- **Travel Time**: equals distance when not specified (Solomon convention)
- **Validation**: all solutions go through shared validator regardless of solver
- **Time Limits**: CP-SAT and OR-Tools respect `time_limit_seconds`; Clarke-Wright ignores it
- **Random Seed**: fixed at 42 for reproducibility across runs (both CP-SAT and OR-Tools use randomized search strategies)
- **Fleet Constraint**: solvers attempt to use ≤ `num_vehicles` routes; infeasibility if they use more
