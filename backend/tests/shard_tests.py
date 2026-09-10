#!/usr/bin/env python3
"""Deterministically split backend/tests/ into N shards, BY FILE.

WHY BY FILE AND NOT BY TEST. The suite is run with `--dist loadfile` because two
files build a module-scoped fixture that does real work once and shares it across
every test in the file -- the `retrain_lead_time()` in
tests/test_lead_time_schema_contract.py and the panel evaluation in
tests/test_newsvendor.py. Splitting a file across shards would rebuild those once
per shard, which is slower AND changes what is being tested. So a file is atomic.

WHY THE MODEL-CI FILES TRAVEL TOGETHER. tests/test_model_ci_gates.py's
`test_the_model_ci_gate_census_is_complete` cross-checks its static AST census
against COLLECTED_MODEL_CI_NODEIDS, which conftest.py fills from *this process's*
collection. Its strongest assertion -- that the total number of collected gates
equals EXPECTED_MODEL_CI_GATES -- is guarded by
`if set(collected) == set(MODEL_CI_GATE_CENSUS)`, so it silently degrades to a
no-op in any shard that does not collect ALL FOUR gate files. Keeping them in one
shard keeps that check firing exactly as it does today. Sharding must not quietly
weaken a gate; this is the line that stops it.

WHY A COMMITTED DURATIONS FILE AND NOT pytest-split. pytest-split splits by TEST
and its chunking does not respect file boundaries, so it can put half of
test_lead_time_schema_contract.py in one shard and half in another -- two real
retrains instead of one. A file-level greedy longest-processing-time pack is a
dozen lines, has no new dependency, and cannot split a file by construction.

COMPLETENESS IS BY CONSTRUCTION, AND ALSO ASSERTED. Every `tests/test_*.py` on
disk is placed in exactly one shard: the partition is built from the glob, not
from a hand-maintained list, so a NEW test file is always picked up (it just gets
the default weight until durations.json is refreshed). `--verify` asserts it.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

TESTS = pathlib.Path(__file__).resolve().parent
DURATIONS = TESTS / "durations.json"

#: These four are the `pytestmark = pytest.mark.model_ci` files. They are packed
#: as ONE atom so the census gate above keeps its full strength. Kept in sync by
#: --verify, which fails if the set on disk differs.
MODEL_CI_FILES = frozenset({
    "test_lead_time_endpoint_contract.py",
    "test_lead_time_schema_contract.py",
    "test_model_ci_gates.py",
    "test_serve_coverage.py",
})

#: Seconds assumed for a file with no recorded duration. Deliberately above the
#: median so a brand-new (and therefore untimed) file is not all dumped into the
#: same bin as every other unknown.
DEFAULT_WEIGHT = 8.0


def _model_ci_files_on_disk() -> set[str]:
    out = set()
    for path in sorted(TESTS.glob("test_*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("pytestmark") and "model_ci" in line:
                out.add(path.name)
                break
    return out


def partition(n: int) -> list[list[str]]:
    files = sorted(p.name for p in TESTS.glob("test_*.py"))
    if not files:
        sys.exit("no tests/test_*.py found -- refusing to produce empty shards")
    try:
        weights = json.loads(DURATIONS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        weights = {}

    atoms: list[tuple[float, list[str]]] = []
    bundle = [f for f in files if f in MODEL_CI_FILES]
    for f in files:
        if f not in MODEL_CI_FILES:
            atoms.append((float(weights.get(f, DEFAULT_WEIGHT)), [f]))
    if bundle:
        atoms.append((sum(float(weights.get(f, DEFAULT_WEIGHT)) for f in bundle), bundle))

    # Longest-processing-time first; ties broken by name so the packing is
    # byte-for-byte reproducible on every runner.
    atoms.sort(key=lambda a: (-a[0], a[1][0]))
    bins: list[list[float | list[str]]] = [[0.0, []] for _ in range(n)]
    for weight, names in atoms:
        i = min(range(n), key=lambda j: (bins[j][0], j))
        bins[i][0] += weight
        bins[i][1].extend(names)
    return [sorted(b[1]) for b in bins]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, required=True)
    ap.add_argument("--index", type=int, help="1-based shard to print")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    parts = partition(args.shards)

    if args.verify:
        on_disk = {p.name for p in TESTS.glob("test_*.py")}
        placed = [f for part in parts for f in part]
        dupes = sorted({f for f in placed if placed.count(f) > 1})
        if dupes:
            sys.exit(f"these files were placed in more than one shard: {dupes}")
        missing = sorted(on_disk - set(placed))
        extra = sorted(set(placed) - on_disk)
        if missing or extra:
            sys.exit(f"partition is not a partition: missing={missing} extra={extra}")
        empty = [i for i, p in enumerate(parts, 1) if not p]
        if empty:
            sys.exit(f"shards {empty} are empty -- lower --shards")
        marked = _model_ci_files_on_disk()
        if marked != set(MODEL_CI_FILES):
            sys.exit(
                "the set of model_ci-marked files changed on disk "
                f"({sorted(marked)}) but MODEL_CI_FILES says {sorted(MODEL_CI_FILES)}. "
                "Update MODEL_CI_FILES, or the census gate stops being fully checked."
            )
        for part in parts:
            hit = MODEL_CI_FILES & set(part)
            if hit and hit != set(MODEL_CI_FILES):
                sys.exit(f"model_ci files were split across shards: {sorted(hit)}")
        print(f"OK: {len(on_disk)} test files -> {args.shards} shards, "
              f"sizes {[len(p) for p in parts]}, model_ci bundle intact")
        return

    if not args.index or not 1 <= args.index <= args.shards:
        sys.exit("--index must be between 1 and --shards")
    print(" ".join(f"tests/{f}" for f in parts[args.index - 1]))


if __name__ == "__main__":
    main()
