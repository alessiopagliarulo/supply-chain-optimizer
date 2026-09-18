#!/usr/bin/env python3
"""
Benchmark the CVRPTW solvers against Solomon instances.

This script runs all three solvers (CP-SAT, Clarke-Wright, OR-Tools) on each
Solomon instance and records: vehicles used, total distance, gap vs best-known,
runtime, feasibility, and solver status.

Results are saved as a machine-readable JSON artifact with provenance info.

Usage:
    python backend/scripts/benchmark_solomon.py [--small]

    --small: run only 25-customer instances for quick validation
"""

import argparse
import json
import platform
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.vrp import solve
from app.vrp.solomon import get_best_known, load_solomon


@dataclass
class BenchmarkResult:
    """One solver's result on one instance."""

    instance: str
    family: str
    num_customers: int
    solver: str
    vehicles_used: int
    total_distance: int
    gap_percent: Optional[float]  # vs best-known
    runtime_seconds: float
    feasible: bool
    solver_status: str
    best_known_distance: Optional[int] = None
    best_known_vehicles: Optional[int] = None


def compute_gap(actual: int, best_known: Optional[int]) -> Optional[float]:
    """Compute gap percentage vs best-known solution."""
    if best_known is None or best_known == 0:
        return None
    return 100 * (actual - best_known) / best_known


def run_benchmark(
    family: str, num_customers: int, time_limit_seconds: float = 30.0
) -> List[BenchmarkResult]:
    """Run all solvers on one Solomon instance."""
    results: List[BenchmarkResult] = []

    try:
        instance = load_solomon(family, num_customers)
    except FileNotFoundError:
        print(f"  ⚠ Instance file not found for {family} {num_customers}")
        return []

    best_known = get_best_known(family, num_customers)
    best_dist = best_known[1] if best_known else None
    best_veh = best_known[0] if best_known else None

    for solver in ["cpsat", "clarke_wright", "ortools"]:
        # CP-SAT only works well on small instances
        if solver == "cpsat" and num_customers > 50:
            print(f"    {solver:20} (skipped - too large)")
            continue

        print(f"    {solver:20}", end=" ", flush=True)

        start_time = time.time()
        try:
            solution = solve(instance, method=solver, time_limit_seconds=time_limit_seconds, seed=42)
        except Exception as e:
            print(f"✗ error: {e}")
            continue
        elapsed = time.time() - start_time

        gap = compute_gap(solution.total_cost, best_dist)
        gap_str = f"{gap:+.1f}%" if gap is not None else "N/A"

        feas_str = "✓" if solution.feasible else "✗"
        print(f"{feas_str} {solution.total_cost:5d}  {gap_str:>7}  {elapsed:6.2f}s")

        result = BenchmarkResult(
            instance=instance.name,
            family=family,
            num_customers=num_customers,
            solver=solver,
            vehicles_used=solution.vehicles_used,
            total_distance=solution.total_cost,
            gap_percent=gap,
            runtime_seconds=elapsed,
            feasible=solution.feasible,
            solver_status=solution.status,
            best_known_distance=best_dist,
            best_known_vehicles=best_veh,
        )
        results.append(result)

    return results


def main():
    parser = argparse.ArgumentParser(description="Benchmark CVRPTW solvers on Solomon instances")
    parser.add_argument("--small", action="store_true", help="Run only 25-customer instances")
    args = parser.parse_args()

    # Determine which sizes to run
    sizes = [25] if args.small else [25, 50, 100]

    all_results: List[BenchmarkResult] = []

    print()
    print("Benchmarking CVRPTW solvers on Solomon instances")
    print("=" * 60)
    print()

    families = ["C1", "C2", "R1", "R2", "RC1", "RC2"]

    for family in families:
        print(f"{family} family:")
        for size in sizes:
            print(f"  {size:3d} customers:")
            results = run_benchmark(family, size, time_limit_seconds=30.0)
            all_results.extend(results)
        print()

    # Save results as JSON
    results_dir = Path(__file__).parent.parent.parent / "docs"
    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = results_dir / "benchmark_results.json"

    # Add provenance
    provenance = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "time_limit_seconds": 30.0,
        "source": "Solomon (Gehring & Homberger 2002) https://www.mech.kuleuven.be/en/cib/op/",
    }

    output = {
        "provenance": provenance,
        "results": [asdict(r) for r in all_results],
    }

    with open(results_file, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Results saved to {results_file}")
    print(f"Total solver runs: {len(all_results)}")


if __name__ == "__main__":
    main()
