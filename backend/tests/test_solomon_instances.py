"""Tests for the Solomon instance loader, reference values, benchmark runner and its committed artifact.

The full benchmark (scripts/benchmark_solomon.py, ~1 hour) is a generator, not
a test. Here it runs end-to-end on one small case, and the committed artifact
it produced is re-checked against the shared validator and the instance files.
"""

import json
import math
import sys
from pathlib import Path

import pytest

from app.vrp import Node, VrpInstance, solve_cpsat, validate_routes
from app.vrp.solomon import (
    DATA_DIR,
    FAMILIES,
    INSTANCE_NAMES,
    SCALE,
    SIZES,
    best_known_sources,
    get_best_known,
    load_solomon,
    parse_solomon_file,
    read_solomon,
    route_distance,
)

BACKEND = Path(__file__).resolve().parent.parent
ARTIFACT = BACKEND.parent / "docs" / "benchmark_results.json"
sys.path.insert(0, str(BACKEND / "scripts"))

import benchmark_solomon  # noqa: E402


class TestInstanceFiles:
    def test_all_168_files_present(self):
        assert len(INSTANCE_NAMES) == 56
        assert [len(v) for v in FAMILIES.values()] == [9, 8, 12, 11, 8, 8]
        for name in INSTANCE_NAMES:
            for size in SIZES:
                assert read_solomon(name, size).num_customers == size

    def test_parse_c101_25_matches_the_published_file(self):
        inst = parse_solomon_file(DATA_DIR / "c101_25.txt")
        assert inst.name == "C101"
        assert inst.num_customers == 25
        assert (inst.num_vehicles, inst.vehicle_capacity) == (25, 200)
        # Depot row: 0  40 50  0  0 1236  0
        assert inst.coords[0] == (40.0, 50.0)
        assert (inst.demand[0], inst.ready[0], inst.due[0], inst.service_time[0]) == (0, 0, 1236, 0)
        # First customer: 1  45 68  10  912 967  90
        assert inst.coords[1] == (45.0, 68.0)
        assert (inst.demand[1], inst.ready[1], inst.due[1], inst.service_time[1]) == (10, 912, 967, 90)
        # Last customer: 25  25 52  40  169 224  90
        assert inst.coords[25] == (25.0, 52.0)
        assert (inst.demand[25], inst.ready[25], inst.due[25], inst.service_time[25]) == (40, 169, 224, 90)

    @pytest.mark.parametrize("name", ["C101", "R211", "RC208"])
    def test_subsets_are_the_first_customers_of_the_100_customer_file(self, name):
        full = read_solomon(name, 100)
        for size in (25, 50):
            sub = read_solomon(name, size)
            assert (sub.num_vehicles, sub.vehicle_capacity) == (full.num_vehicles, full.vehicle_capacity)
            assert sub.coords == full.coords[: size + 1]
            assert sub.demand == full.demand[: size + 1]
            assert sub.ready == full.ready[: size + 1]
            assert sub.due == full.due[: size + 1]
            assert sub.service_time == full.service_time[: size + 1]

    def test_invalid_arguments(self):
        with pytest.raises(ValueError, match="unknown Solomon instance"):
            load_solomon("X101", 25)
        with pytest.raises(ValueError, match="num_customers must be"):
            load_solomon("C101", 35)


class TestScaledModel:
    def test_distances_and_times_are_scaled_by_100(self):
        raw = read_solomon("C101", 25)
        inst = load_solomon("C101", 25)
        assert isinstance(inst, VrpInstance)
        assert inst.name == "solomon_C101_25"
        d01 = math.dist((40, 50), (45, 68))  # 18.6815...
        assert inst.distance[0][1] == 1868  # rounded to nearest
        assert inst.travel_time[0][1] == 1869  # rounded up: never optimistic about time
        assert inst.nodes[1] == Node(demand=10, ready=912 * SCALE, due=967 * SCALE, service_time=90 * SCALE)
        assert inst.nodes[0] == Node(demand=0, ready=0, due=1236 * SCALE, service_time=0)
        assert all(
            inst.travel_time[i][j] >= math.dist(raw.coords[i], raw.coords[j]) * SCALE
            for i in range(26)
            for j in range(26)
        )
        assert abs(inst.distance[0][1] - d01 * SCALE) <= 0.5

    def test_exact_distances_do_not_round_up(self):
        # Customers 1 (45,68) and 2 (45,70) are exactly 2.0 apart.
        inst = load_solomon("C101", 25)
        assert inst.distance[1][2] == inst.travel_time[1][2] == 200

    def test_route_distance(self):
        coords = [(0, 0), (3, 4), (6, 0)]
        assert route_distance(coords, [[1, 2]]) == pytest.approx(16.0)
        assert route_distance(coords, [[1], [2]]) == pytest.approx(22.0)
        coords = [(0, 0), (1, 1)]  # sqrt(2) = 1.414 -> 1.4 per arc when truncated
        assert route_distance(coords, [[1]], truncate_one_decimal=True) == pytest.approx(2.8)


class TestBestKnown:
    def test_published_values_are_stored_exactly(self):
        assert get_best_known("C101", 100) == {
            "vehicles": 10,
            "distance": 828.94,
            "reference": "RT",
            "source": "sintef_best_known",
        }
        assert get_best_known("R111", 100)["distance"] == 1096.73
        assert get_best_known("rc208", 100)["distance"] == 828.14
        assert get_best_known("C101", 25) == {
            "vehicles": 3,
            "distance": 191.3,
            "reference": "KDMSS",
            "source": "solomon_optimal",
        }
        assert get_best_known("R101", 50) == {
            "vehicles": 12,
            "distance": 1044.0,
            "reference": "KDMSS",
            "source": "solomon_optimal",
        }

    def test_unpublished_subset_optima_are_null(self):
        for name in ("R207", "R208", "RC208"):
            assert get_best_known(name, 50) is None

    def test_every_case_has_a_cited_source(self):
        info = best_known_sources()
        for name in INSTANCE_NAMES:
            assert get_best_known(name, 100)["source"] == "sintef_best_known"
            for size in SIZES:
                ref = get_best_known(name, size)
                if ref is None:
                    continue
                assert ref["source"] in info["sources"]
                for key in ref["reference"].split("+"):
                    assert key in info["references"], f"{name}/{size}: no citation for {key}"

    @pytest.mark.parametrize("name,optimum", [("C101", 191.3), ("R101", 617.1), ("RC101", 461.1)])
    def test_subset_optima_use_one_decimal_truncation(self, name, optimum):
        # Solving exactly with each arc truncated to one decimal reproduces the published optimum,
        # which is why the 25/50-customer gaps are measured on distance_one_decimal.
        raw = read_solomon(name, 25)
        tenths = [[int(math.floor(math.dist(a, b) * 10 + 1e-9)) for b in raw.coords] for a in raw.coords]
        nodes = [
            Node(
                demand=raw.demand[i] if i else 0,
                ready=raw.ready[i] * 10,
                due=raw.due[i] * 10,
                service_time=raw.service_time[i] * 10 if i else 0,
            )
            for i in range(26)
        ]
        inst = VrpInstance(
            nodes=nodes, distance=tenths, num_vehicles=raw.num_vehicles, vehicle_capacity=raw.vehicle_capacity
        )
        solution = solve_cpsat(inst, time_limit_seconds=30)
        assert solution.proven_optimal
        assert solution.total_cost / 10 == pytest.approx(optimum)
        assert get_best_known(name, 25)["distance"] == optimum


class TestBenchmarkRunner:
    def test_end_to_end_on_one_small_case(self, tmp_path):
        out = tmp_path / "results.json"
        assert (
            benchmark_solomon.main(["--instances", "C101", "--sizes", "25", "--time-limit", "1", "--output", str(out)])
            == 0
        )
        data = json.loads(out.read_text())
        prov = data["provenance"]
        assert prov["time_limit_seconds"] == 1.0
        assert prov["random_seed"] == 42
        assert prov["data"]["sha256"] == "8a0a72cbe6b7f8f9988ace4ebde0378ec34943acaaac47f2c408915e41887747"
        assert set(prov["best_known"]["sources"]) == {"sintef_best_known", "solomon_optimal"}
        results = data["results"]
        assert [r["solver"] for r in results] == ["cpsat", "clarke_wright", "ortools"]
        for r in results:
            assert r["instance"] == "C101" and r["num_customers"] == 25
            assert r["feasible"] is True
            assert r["reference"]["distance"] == 191.3
            assert r["gap_percent"] >= 0.0
            assert r["runtime_seconds"] < 5
        cpsat = results[0]
        assert cpsat["status"] == "optimal"
        assert cpsat["proven_optimal_scaled_model"] is True
        assert cpsat["gap_measured_on"] == "distance_one_decimal"
        # CP-SAT proves the real-distance optimum; measured with one-decimal arcs it is the published optimum.
        assert cpsat["distance_one_decimal"] == pytest.approx(191.3)
        assert data["summary"][0]["runs"] == 1

    def test_rejects_unknown_instance(self):
        with pytest.raises(SystemExit):
            benchmark_solomon.main(["--instances", "Z999", "--output", "/dev/null"])

    @pytest.mark.parametrize("limit", ["0", "-1"])
    def test_rejects_non_positive_time_limit(self, limit, tmp_path):
        out = tmp_path / "out.json"
        with pytest.raises(SystemExit) as exc:
            benchmark_solomon.main(["--instances", "C101", "--sizes", "25", "--time-limit", limit, "--output", str(out)])
        assert exc.value.code == 2
        assert not out.exists()


class TestCommittedArtifact:
    """docs/benchmark_results.json must be exactly what the script produces from the committed inputs."""

    @pytest.fixture(scope="class")
    def data(self):
        return json.loads(ARTIFACT.read_text())

    def test_covers_every_case_and_solver(self, data):
        prov = data["provenance"]
        assert prov["generated_by"] == "backend/scripts/benchmark_solomon.py"
        assert prov["time_limit_seconds"] == 10.0
        assert prov["random_seed"] == 42
        assert prov["data"]["url"] == "https://www.sintef.no/globalassets/project/top/vrptw/solomon/solomon-100.zip"
        cases = [(name, size) for name in INSTANCE_NAMES for size in SIZES]
        assert prov["data"]["files_sha256"] == benchmark_solomon.data_digest(cases)
        keys = sorted((r["instance"], r["num_customers"], r["solver"]) for r in data["results"])
        assert keys == sorted((n, s, solver) for n, s in cases for solver in benchmark_solomon.SOLVERS)

    def test_every_result_matches_its_routes(self, data):
        cache = {}
        for r in data["results"]:
            key = (r["instance"], r["num_customers"])
            if key not in cache:
                raw = read_solomon(*key)
                cache[key] = (raw, raw.to_vrp())
            raw, inst = cache[key]
            label = f"{key}/{r['solver']}"
            assert r["reference"] == get_best_known(*key), label
            assert r["vehicles_used"] == len(r["routes"]), label
            if not r["routes"]:
                assert r["feasible"] is False and r["distance"] is None and r["gap_percent"] is None, label
                continue
            assert validate_routes(inst, r["routes"]).feasible == r["feasible"], label
            assert r["distance"] == round(route_distance(raw.coords, r["routes"]), 2), label
            one_dp = round(route_distance(raw.coords, r["routes"], truncate_one_decimal=True), 1)
            assert r["distance_one_decimal"] == one_dp, label
            ref = r["reference"]
            expected_field = None if ref is None else (
                "distance_one_decimal" if ref["source"] == "solomon_optimal" else "distance"
            )
            assert r["gap_measured_on"] == expected_field, label
            if ref is None or not r["feasible"]:
                assert r["gap_percent"] is None, label
                continue
            measured = r[expected_field]
            assert r["gap_percent"] == pytest.approx(100 * (measured - ref["distance"]) / ref["distance"], abs=1e-3), (
                label
            )
            if ref["source"] == "solomon_optimal":
                # Nothing feasible can beat a proven optimum under its own distance convention.
                assert r["gap_percent"] >= 0.0, label
