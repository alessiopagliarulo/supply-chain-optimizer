#!/usr/bin/env python3
"""
Benchmark the CVRPTW solvers on the Solomon instances and write the results artifact.

Runs every solver (CP-SAT exact, Clarke-Wright savings, OR-Tools routing) on
every Solomon instance (56 instances x 25/50/100 customers = 168 cases) with
a fixed per-solve time limit and seed, re-checks every solution with the
shared validator, and writes ``docs/benchmark_results.json``. That file is the
data source for the Benchmarks page; it is produced only by this script and
is never edited by hand.

    cd backend
    python scripts/benchmark_solomon.py                      # full run, ~1 hour
    python scripts/benchmark_solomon.py --instances C101,R201 --sizes 25 \\
        --output /tmp/solomon.json                           # a quick subset

This is a generator, not a test: the test suite only exercises it on a tiny
subset (tests/test_solomon_instances.py).

WHAT IS RECORDED per solver run: status, feasibility (shared validator),
vehicles used, total distance in double precision (from the coordinates, not
the scaled integers), runtime, and the gap to the published reference value,
measured in that reference's convention (``gap_measured_on`` names the field):

* 100 customers - vs the SINTEF best known (fewest vehicles, then distance,
  double precision): ``gap_percent`` compares ``distance``.
* 25 / 50 customers - vs Solomon's proven optima, which use distances
  truncated to one decimal per arc: ``gap_percent`` compares
  ``distance_one_decimal`` (the same routes measured that way), so nothing
  feasible can read below 0.0.

``proven_optimal_scaled_model`` is CP-SAT's proof on the solver's own integer
model (distance x100 rounded, travel time x100 rounded up). That model is
stricter than either reference convention, so a proven solution can still show
a positive gap: it is optimal for the scaled model, not for the reference's.

The solvers minimise distance only, so on 100 customers a solution may use
more vehicles than the hierarchical best known and still be shorter; the
negative gap is then real and ``vehicle_gap`` shows the extra vehicles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import ortools  # noqa: E402

from app.vrp import solve_clarke_wright, solve_cpsat, solve_ortools, validate_routes  # noqa: E402
from app.vrp.model import VrpSolution  # noqa: E402
from app.vrp.solomon import (  # noqa: E402
    DATA_DIR,
    INSTANCE_NAMES,
    SCALE,
    SIZES,
    best_known_sources,
    family_of,
    get_best_known,
    instance_path,
    read_solomon,
    route_distance,
)

DEFAULT_OUTPUT = BACKEND.parent / "docs" / "benchmark_results.json"
SOLVERS = ("cpsat", "clarke_wright", "ortools")
DATA_URL = "https://www.sintef.no/globalassets/project/top/vrptw/solomon/solomon-100.zip"
DATA_SHA256 = "8a0a72cbe6b7f8f9988ace4ebde0378ec34943acaaac47f2c408915e41887747"


def run_solver(solver: str, instance, time_limit: float, seed: int) -> VrpSolution:
    if solver == "cpsat":
        return solve_cpsat(instance, time_limit_seconds=time_limit, random_seed=seed)
    if solver == "clarke_wright":
        return solve_clarke_wright(instance)
    if solver == "ortools":
        return solve_ortools(instance, time_limit_seconds=time_limit)
    raise ValueError(f"unknown solver {solver!r}")


def gap(value: float, reference: float) -> float:
    return round(100.0 * (value - reference) / reference, 3)


def run_case(args: tuple) -> List[dict]:
    """All solvers on one (instance, size). Top-level so a process pool can run it."""
    name, size, solvers, time_limit, seed = args
    raw = read_solomon(name, size)
    instance = raw.to_vrp()
    reference = get_best_known(name, size)
    records = []
    for solver in solvers:
        t0 = time.perf_counter()
        solution = run_solver(solver, instance, time_limit, seed)
        runtime = time.perf_counter() - t0
        # Re-check with the shared validator here, independent of the solver's own path.
        report = validate_routes(instance, solution.routes)
        if report.feasible != solution.feasible:
            raise RuntimeError(f"{name}/{size}/{solver}: validator disagrees with the solver's own validation")
        has_routes = bool(solution.routes)
        distance = route_distance(raw.coords, solution.routes) if has_routes else None
        distance_1dp = route_distance(raw.coords, solution.routes, truncate_one_decimal=True) if has_routes else None
        record = {
            "instance": name,
            "family": family_of(name),
            "num_customers": size,
            "solver": solver,
            "status": solution.status,
            "solver_status": solution.stats.get("solver_status", solution.stats.get("routing_status")),
            "feasible": report.feasible,
            "proven_optimal_scaled_model": solution.proven_optimal,
            "vehicles_used": solution.vehicles_used,
            "distance": round(distance, 2) if distance is not None else None,
            "distance_one_decimal": round(distance_1dp, 1) if distance_1dp is not None else None,
            "gap_measured_on": None,
            "gap_percent": None,
            "vehicle_gap": None,
            "runtime_seconds": round(runtime, 3),
            "reference": reference,
            "violations": report.violations[:3],
            "routes": solution.routes,
        }
        if reference is not None:
            record["gap_measured_on"] = (
                "distance_one_decimal" if reference["source"] == "solomon_optimal" else "distance"
            )
        if reference is not None and report.feasible and has_routes:
            # Gaps use the reported (rounded) distances so a reader can reproduce them from the file.
            record["gap_percent"] = gap(record[record["gap_measured_on"]], reference["distance"])
            record["vehicle_gap"] = solution.vehicles_used - reference["vehicles"]
        records.append(record)
        shown = f"{record['distance']:9.2f}" if distance is not None else "        -"
        shown_gap = f"{record['gap_percent']:+7.2f}%" if record["gap_percent"] is not None else "       -"
        print(
            f"{name:>6}/{size:<3} {solver:<13} {solution.status:<11} veh {solution.vehicles_used:>2} "
            f"dist {shown} gap {shown_gap} {runtime:6.2f}s",
            flush=True,
        )
    return records


def summarize(results: List[dict]) -> List[dict]:
    """Per solver and size: how many feasible, mean gap where a reference exists, mean runtime."""
    rows = []
    for size in sorted({r["num_customers"] for r in results}):
        for solver in SOLVERS:
            runs = [r for r in results if r["num_customers"] == size and r["solver"] == solver]
            if not runs:
                continue
            gaps = [r["gap_percent"] for r in runs if r["gap_percent"] is not None]
            rows.append(
                {
                    "num_customers": size,
                    "solver": solver,
                    "runs": len(runs),
                    "feasible": sum(r["feasible"] for r in runs),
                    "proven_optimal_scaled_model": sum(r["proven_optimal_scaled_model"] for r in runs),
                    "with_reference": len(gaps),
                    "mean_gap_percent": round(sum(gaps) / len(gaps), 3) if gaps else None,
                    "mean_runtime_seconds": round(sum(r["runtime_seconds"] for r in runs) / len(runs), 3),
                }
            )
    return rows


def data_digest(cases: List[tuple]) -> str:
    """sha256 over the instance files used, in run order, so a reader can confirm the inputs."""
    h = hashlib.sha256()
    for name, size in cases:
        path = instance_path(name, size)
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def git_commit() -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BACKEND, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def provenance(
    cases: List[tuple], solvers: List[str], time_limit: float, seed: int, jobs: int, argv: List[str]
) -> dict:
    return {
        "generated_by": "backend/scripts/benchmark_solomon.py",
        "command": " ".join(["python", "scripts/benchmark_solomon.py", *argv]),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": git_commit(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "ortools_version": ortools.__version__,
        "time_limit_seconds": time_limit,
        "random_seed": seed,
        "parallel_jobs": jobs,
        "solvers": {
            "cpsat": f"CP-SAT exact model, 1 search worker, random_seed={seed}, stops at the time limit",
            "clarke_wright": "Clarke-Wright savings, one deterministic pass; ignores the time limit",
            "ortools": "OR-Tools routing, PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH until the time limit; "
            "the search has no random seed, so results vary only with how much search fits in the time limit",
        },
        "runtime_note": "runtime_seconds is wall time of the whole solver call, model building included",
        "integer_scale": SCALE,
        "distance_note": (
            "Solvers work on integers: distance = Euclidean x 100 rounded, travel time = Euclidean x 100 rounded up "
            "(so every validated schedule is feasible in real arithmetic). Reported distance is recomputed from the "
            "coordinates in double precision; distance_one_decimal truncates each arc to one decimal."
        ),
        "gap_note": (
            "100 customers: gap_percent = (distance - reference) / reference vs the SINTEF best known. "
            "25/50 customers: gap_percent = (distance_one_decimal - reference) / reference vs Solomon's proven optima. "
            "gap_measured_on names the field compared. null when there is no feasible solution or no "
            "published reference."
        ),
        "optimality_note": (
            "proven_optimal_scaled_model is CP-SAT's optimality proof on the integer model above, not on the "
            "reference's convention. Travel time rounded up makes that model stricter, so a proven solution can "
            "still have a positive gap_percent."
        ),
        "data": {
            "instances": "Solomon (1987) VRPTW instances, SINTEF TOP backup",
            "url": DATA_URL,
            "sha256": DATA_SHA256,
            "files": str(DATA_DIR.relative_to(BACKEND.parent)),
            "files_sha256": data_digest(cases),
            "subsets": "25/50-customer versions are the first 25/50 customers of each 100-customer file",
        },
        "best_known": best_known_sources(),
        "instances": sorted({name for name, _ in cases}, key=INSTANCE_NAMES.index),
        "sizes": sorted({size for _, size in cases}),
    }


def write_json(path: Path, payload: dict) -> None:
    """Indented JSON, but one line per result so the file stays readable and diffable."""
    results = payload["results"]
    head = {k: v for k, v in payload.items() if k != "results"}
    text = json.dumps(head, indent=2)[:-2]  # drop the closing "\n}"
    lines = [json.dumps(r, separators=(", ", ": ")) for r in results]
    text += ',\n  "results": [\n    ' + ",\n    ".join(lines) + "\n  ]\n}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def parse_list(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the CVRPTW solvers on the Solomon instances.")
    parser.add_argument("--instances", default="all", help="comma-separated names (e.g. C101,R201) or 'all'")
    parser.add_argument("--sizes", default="25,50,100", help="comma-separated customer counts from 25,50,100")
    parser.add_argument("--solvers", default=",".join(SOLVERS), help=f"comma-separated from {','.join(SOLVERS)}")
    parser.add_argument("--time-limit", type=float, default=10.0, help="per-solve time limit in seconds")
    parser.add_argument("--seed", type=int, default=42, help="CP-SAT random seed")
    parser.add_argument("--jobs", type=int, default=1, help="cases solved in parallel (1 = most faithful runtimes)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="where to write the JSON artifact")
    args = parser.parse_args(argv)
    argv = list(sys.argv[1:] if argv is None else argv)

    names = INSTANCE_NAMES if args.instances == "all" else [n.upper() for n in parse_list(args.instances)]
    sizes = [int(s) for s in parse_list(args.sizes)]
    solvers = parse_list(args.solvers)
    for n in names:
        if n not in INSTANCE_NAMES:
            parser.error(f"unknown instance {n}")
    for s in sizes:
        if s not in SIZES:
            parser.error(f"size must be one of {SIZES}")
    for s in solvers:
        if s not in SOLVERS:
            parser.error(f"unknown solver {s}")
    if args.time_limit <= 0:
        parser.error("time limit must be positive")

    cases = [(name, size) for name in names for size in sizes]
    work = [(name, size, solvers, args.time_limit, args.seed) for name, size in cases]
    print(
        f"{len(cases)} cases x {len(solvers)} solvers, {args.time_limit:g}s limit, seed {args.seed}, {args.jobs} job(s)"
    )
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            per_case = list(pool.map(run_case, work))
    else:
        per_case = [run_case(w) for w in work]
    results: List[dict] = [r for records in per_case for r in records]

    payload: Dict[str, object] = {
        "schema_version": 2,
        "provenance": provenance(cases, solvers, args.time_limit, args.seed, args.jobs, argv),
        "summary": summarize(results),
        "results": results,
    }
    write_json(args.output, payload)
    print(f"wrote {len(results)} results to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
