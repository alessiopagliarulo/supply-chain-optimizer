# CVRPTW benchmark on the Solomon instances

The routing engine in `backend/app/vrp/` (CP-SAT exact, Clarke-Wright savings,
OR-Tools routing, one shared validator) is benchmarked on the standard Solomon
(1987) VRPTW instances. The results live in
[`docs/benchmark_results.json`](benchmark_results.json), which the Benchmarks
page reads. That file is written only by the benchmark script; never edit it by
hand. A test (`TestCommittedArtifact`) re-validates every committed solution
against the instance files and fails if any number does not match its routes.

## Regenerate the results

```bash
cd backend
python scripts/benchmark_solomon.py            # all 168 cases x 3 solvers, ~1 hour
```

Defaults: 10 s per solve, CP-SAT seed 42, one case at a time (`--jobs 1`) so
runtimes are not distorted by contention. `--instances C101,R201`, `--sizes 25`,
`--solvers`, `--time-limit`, `--seed`, `--jobs` and `--output` select a subset,
for example:

```bash
python scripts/benchmark_solomon.py --instances C101 --sizes 25 --output /tmp/solomon.json
```

The full run is a generator, not a test. The test suite
(`backend/tests/test_solomon_instances.py`) runs the script end-to-end on one
25-customer case in a couple of seconds.

## Instances

56 instances: C101-C109, C201-C208 (clustered), R101-R112, R201-R211 (random),
RC101-RC108, RC201-RC208 (mixed). Family 1 has short time windows, family 2
long ones. Each comes in three sizes: 100 customers, and the standard 25- and
50-customer versions, which are the first 25 / 50 customers of the 100-customer
file. That is 168 cases in `backend/app/vrp/data/solomon/`
(`c101.txt`, `c101_25.txt`, `c101_50.txt`, ...).

- **Source:** M. M. Solomon, "Algorithms for the Vehicle Routing and Scheduling
  Problems with Time Window Constraints", *Operations Research* 35(2), 1987,
  254-265. Instance page: http://web.cba.neu.edu/~msolomon/problems.htm.
- **Files:** SINTEF TOP's backup of those definitions,
  https://www.sintef.no/globalassets/project/top/vrptw/solomon/solomon-100.zip
  (sha256 `8a0a72cbe6b7f8f9988ace4ebde0378ec34943acaaac47f2c408915e41887747`).
  `python scripts/download_solomon_instances.py --check` re-downloads it and
  confirms all 168 committed files byte-for-byte; without `--check` it rewrites
  them.
- **License:** the instances carry no explicit license. Their author publishes
  them for research benchmarking; they are redistributed unchanged on that
  basis, with the citation above.

Load one in code:

```python
from app.vrp import solve
from app.vrp.solomon import get_best_known, load_solomon, read_solomon

raw = read_solomon("C101", 100)       # file values: coords, windows, fleet
instance = load_solomon("C101", 100)  # the shared VrpInstance, scaled x100
solution = solve(instance, method="ortools", time_limit_seconds=10)
get_best_known("C101", 100)           # {'vehicles': 10, 'distance': 828.94, ...}
```

## Reference values

`backend/app/vrp/data/solomon/best_known.json` holds one published reference
per case, exactly as published, with full citations. Nothing is derived or
estimated; where no value is published the entry is `null`.

| Size | Source | Objective | Distance convention |
| --- | --- | --- | --- |
| 100 | SINTEF TOP best known, https://www.sintef.no/projectweb/top/vrptw/100-customers/ | fewest vehicles, then distance | double precision, 2 decimals |
| 25, 50 | Solomon's tables of proven optima (2005), `c1c2solu.htm`, `r1r2solu.htm`, `rc12solu.htm` on his site (archived copies linked in the JSON) | distance only | each arc truncated to 1 decimal |

Each source also carries `ranks_vehicles_first` (`true` for SINTEF, `false`
for Solomon), the flag the script and the page use to decide whether a distance
gap is comparable.

Solomon's tables list no optimum for R207/50, R208/50 and RC208/50; those
cases have no gap. The one-decimal convention is checked by a test: solving
C101, R101 and RC101 (25 customers) exactly with truncated arcs reproduces the
published optima (191.3, 617.1, 461.1).

## What each result records

One row per solver per case:

- `status` (`optimal` / `feasible` / `infeasible` / `no_solution`),
  `solver_status`, `feasible` (the shared validator's verdict, re-checked by
  the script), `proven_optimal_scaled_model` (see below);
- `vehicles_used`, `routes` (customer ids, depot implicit);
- `distance` - total route length in double precision, from the coordinates;
- `distance_one_decimal` - the same routes with each arc truncated to one
  decimal (the 25/50-customer convention);
- `gap_measured_on` - which distance the gap compares, in the reference's
  convention: `distance` for 100 customers (SINTEF), `distance_one_decimal` for
  25/50 (Solomon). `null` when there is no reference;
- `gap_comparable` - whether the distance gap means anything under the
  reference's own ranking (see "Vehicles first, then distance" below): always
  `true` for 25/50 customers, and for 100 customers `true` only when
  `vehicles_used` equals the reference's vehicles. `null` when the solution is
  not feasible or there is no reference;
- `gap_percent` - vs the reference, on `gap_measured_on`. `null` when the
  solution is not feasible, there is no reference, or `gap_comparable` is
  `false`;
- `vehicle_gap` - vehicles used minus the reference's vehicles (the comparison
  that counts when `gap_comparable` is `false`);
- `runtime_seconds` - wall time of the solver call, model building included.

`provenance` records the command, git commit, platform, Python and OR-Tools
versions, time limit, seed, data URL and sha256, a sha256 over the 168 input
files, and the reference sources. `summary` aggregates per solver and size:
`with_reference` runs with something to compare, `gap_comparable` of those with
a comparable distance gap, `more_vehicles_than_reference` /
`fewer_vehicles_than_reference` over every run with a reference (comparable or
not, at every size), and `mean_gap_percent` over the comparable gaps only.

**Read `mean_gap_percent` with its count.** It is computed over
`gap_comparable` of `runs`. At 25/50 customers that is every feasible run with
a published optimum. At 100 customers it is only the runs that matched the
best-known fleet size, which tend to be the instances the solver handled well,
so the mean is not the solver's performance over all 56 runs; it is `null`
when no run matched (Clarke-Wright). The Benchmarks page shows each mean with
its "N of M runs" count.

The file is `schema_version` 3. Version 2 had no `gap_comparable` and reported a
distance gap for every row, including 100-customer rows with more vehicles than
the reference, where it read as a spurious negative gap (down to -16% on
RC202).

## Reading the numbers

- **Integer scaling.** The shared model is integer-only, so instances are
  scaled by 100: distance rounded to nearest, travel time rounded up, windows
  and service times exact. Rounding travel time up means every schedule the
  validator accepts is also feasible in real arithmetic, so "feasible" holds
  under the published double-precision rules.
- **Vehicles first, then distance.** The SINTEF best known solutions for 100
  customers are ranked hierarchically: fewest vehicles first, then shortest
  distance. The solvers minimise distance only, so they often use more
  vehicles than the best known and, with more trucks, drive a shorter total
  distance. That is not a win under the reference's ranking, so the distance
  gap is only compared at the same vehicle count; otherwise `gap_comparable`
  is `false`, `gap_percent` is `null`, and `vehicle_gap` is the comparison.
  Every negative distance gap in the version-2 artifact came from such a row;
  a test asserts no comparable gap is below zero, since that would be a new
  best known and needs checking, not publishing.
- **25/50-customer gaps are never negative.** A feasible solution cannot beat
  a proven optimum under the optimum's own convention; the artifact test
  asserts this.
- **`proven_optimal_scaled_model` does not mean a 0.0 gap.** It is CP-SAT's
  proof on the solver's own integer model. Travel time rounded up (x100) is
  stricter than either reference convention, so on tight time windows the
  proven solution can be longer than the published optimum and show a positive
  gap (for example R105/50). It is optimal for the scaled model, not for the
  reference's.
- **CP-SAT** is the exact model; with 10 s and one worker it proves optimality
  on many 25/50-customer cases and some 100-customer ones, and otherwise
  reports the best solution found or none.
- **OR-Tools routing** always uses the full time limit (guided local search has
  no stopping criterion). Its search has no random seed, so results vary only
  with how much search fits in 10 s on the machine. On some tight-window
  100-customer instances its first-solution heuristic finds nothing in 10 s;
  that is recorded as `no_solution`, not skipped.
- **Clarke-Wright** is one deterministic pass and ignores the time limit. It
  does not respect the fleet size, so it can return more routes than vehicles;
  the validator then marks the result `infeasible`.
