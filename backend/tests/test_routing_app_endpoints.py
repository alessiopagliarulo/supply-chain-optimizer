"""The routing endpoints the web app's three pages call.

Route Plan  -> GET /routing/instances, GET /routing/instances/{id}, POST /routing/solve
Simulation  -> POST /routing/simulate, POST /routing/tune-buffers
Benchmarks  -> GET /routing/benchmarks
"""

import json

import pytest

from app.api import routing as routing_api
from app.vrp import instances as instances_mod
from app.vrp.instances import load_instance, parse_solomon_text

OPEN = 1000

CROSS_PAYLOAD = {
    "nodes": [
        {"x": 0, "y": 0, "due": OPEN},
        {"x": 0, "y": 10, "demand": 1, "due": OPEN},
        {"x": 0, "y": 20, "demand": 1, "due": OPEN},
        {"x": 10, "y": 0, "demand": 1, "due": OPEN},
        {"x": 20, "y": 0, "demand": 1, "due": OPEN},
    ],
    "num_vehicles": 2,
    "vehicle_capacity": 2,
}
CROSS_ROUTES = [[1, 2], [3, 4]]


# ── Named instances ──────────────────────────────────────────────────────────


def test_the_built_in_samples_are_listed_with_the_request_caps(client):
    body = client.get("/api/v1/routing/instances").json()
    by_id = {i["id"]: i for i in body["instances"]}
    for sample in ("sample/C101_25", "sample/R101_25"):
        assert by_id[sample]["num_customers"] == 25
        assert by_id[sample]["num_vehicles"] == 25
        assert by_id[sample]["vehicle_capacity"] == 200
        assert by_id[sample]["source"] == "Built-in sample"
    assert body["limits"] == {
        "max_customers": routing_api.MAX_CUSTOMERS,
        "max_exact_customers": routing_api.MAX_EXACT_CUSTOMERS,
        "max_tuning_customers": routing_api.MAX_EXACT_CUSTOMERS,
        "max_time_limit_seconds": routing_api.MAX_TIME_LIMIT_SECONDS,
        "max_replications": routing_api.MAX_REPLICATIONS,
    }


def test_a_sample_is_served_depot_first_exactly_as_the_file_says(client):
    body = client.get("/api/v1/routing/instances/sample/C101_25").json()
    assert body["name"] == "C101.25"
    assert len(body["nodes"]) == 26
    # Solomon C101 depot and first customer, as written in data/samples/C101_25.txt.
    assert body["nodes"][0] == {"x": 40.0, "y": 50.0, "demand": 0, "ready": 0, "due": 1236, "service_time": 0}
    assert body["nodes"][1] == {"x": 45.0, "y": 68.0, "demand": 10, "ready": 912, "due": 967, "service_time": 90}


def test_a_served_sample_solves_to_a_feasible_plan(client):
    inst = client.get("/api/v1/routing/instances/sample/R101_25").json()
    payload = {
        "nodes": inst["nodes"],
        "num_vehicles": inst["num_vehicles"],
        "vehicle_capacity": inst["vehicle_capacity"],
        "method": "clarke_wright",
    }
    body = client.post("/api/v1/routing/solve", json=payload).json()
    assert body["feasible"] is True
    assert sorted(c for r in body["routes"] for c in r) == list(range(1, 26))


@pytest.mark.parametrize("path", ["sample/NOPE", "nope/C101_25", "sample/C101_25.txt"])
def test_an_unknown_instance_is_a_404(client, path):
    assert client.get(f"/api/v1/routing/instances/{path}").status_code == 404


def test_load_instance_refuses_ids_that_leave_its_directories():
    for bad in ("sample/../samples/C101_25", "sample/", "C101_25", "sample/a/b"):
        with pytest.raises(KeyError):
            load_instance(bad)


def test_the_parser_reads_the_alternate_vehicles_capacity_header():
    text = "X1\nVEHICLES 3\nCAPACITY 40\nCUST NO. XCOORD. YCOORD. DEMAND READY DUE SERVICE\n" \
        "0 0 0 0 0 100 0\n1 3 4 5 0 50 2\n"
    name, vehicles, capacity, coords, nodes = parse_solomon_text(text)
    assert (name, vehicles, capacity) == ("X1", 3, 40)
    assert coords == [(0.0, 0.0), (3.0, 4.0)]
    assert nodes[1].demand == 5 and nodes[1].due == 50 and nodes[1].service_time == 2


def test_the_parser_rejects_a_file_without_a_fleet():
    with pytest.raises(ValueError, match="capacity"):
        parse_solomon_text("X\n0 0 0 0 0 100 0\n")


def test_benchmark_instance_files_are_listed_when_present(client, tmp_path, monkeypatch):
    (tmp_path / "Z101.txt").write_text(
        "Z101\n\nVEHICLE\nNUMBER     CAPACITY\n  2         10\n\n0 0 0 0 0 100 0\n1 1 1 1 0 100 0\n"
    )
    (tmp_path / "broken.txt").write_text("not an instance\n")
    monkeypatch.setattr(
        instances_mod,
        "SOURCES",
        (instances_mod.SOURCES[0], ("solomon", tmp_path, "Solomon benchmark")),
    )
    ids = [i["id"] for i in client.get("/api/v1/routing/instances").json()["instances"]]
    assert "solomon/Z101" in ids
    assert "solomon/broken" not in ids
    detail = client.get("/api/v1/routing/instances/solomon/Z101").json()
    assert detail["num_customers"] == 1 and detail["source"] == "Solomon benchmark"


# ── Simulation ───────────────────────────────────────────────────────────────


def test_simulating_a_plan_with_no_variability_reproduces_it(client):
    payload = {**CROSS_PAYLOAD, "routes": CROSS_ROUTES, "num_replications": 5, "variability": 0}
    resp = client.post("/api/v1/routing/simulate", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["on_time_rate"] == 1.0
    assert body["mean_lateness"] == 0.0 and body["p95_lateness"] == 0.0
    assert body["depot_close"] == OPEN
    # Both routes drive 40 and serve instantly; the depot opens at 0.
    assert body["max_route_completion_time"] == 40.0
    assert body["plan_feasible"] is True and body["plan_violations"] == []
    assert body["num_replications"] == 5


def test_simulation_reports_a_submitted_plan_the_validator_rejects(client):
    payload = {**CROSS_PAYLOAD, "routes": [[1, 2, 3, 4]], "num_replications": 2, "variability": 0}
    body = client.post("/api/v1/routing/simulate", json=payload).json()
    assert body["plan_feasible"] is False
    assert any("exceeds capacity" in v for v in body["plan_violations"])


def test_simulation_is_seeded(client):
    payload = {**CROSS_PAYLOAD, "routes": CROSS_ROUTES, "num_replications": 20, "variability": 0.3, "seed": 11}
    first = client.post("/api/v1/routing/simulate", json=payload).json()
    second = client.post("/api/v1/routing/simulate", json=payload).json()
    assert first == second


@pytest.mark.parametrize(
    "override",
    [
        {"routes": [[1, 2], [3, 9]]},
        {"routes": [[0, 1]]},
        {"num_replications": routing_api.MAX_REPLICATIONS + 1},
        {"num_replications": 0},
        {"variability": -0.1},
        {"distribution": "uniform"},
    ],
)
def test_simulation_rejects_bad_requests(client, override):
    payload = {**CROSS_PAYLOAD, "routes": CROSS_ROUTES, **override}
    assert client.post("/api/v1/routing/simulate", json=payload).status_code == 422


# ── Buffer tuning solver choice ──────────────────────────────────────────────


def test_buffer_tuning_uses_the_requested_solver(client):
    payload = {
        **CROSS_PAYLOAD,
        "schedule_buffer_max": 10,
        "schedule_buffer_step": 5,
        "capacity_buffer_max": 0,
        "capacity_buffer_step": 1,
        "num_replications": 5,
        "method": "clarke_wright",
    }
    resp = client.post("/api/v1/routing/tune-buffers", json=payload)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["frontier"]) == 3

    bad = {**payload, "method": "annealing"}
    assert client.post("/api/v1/routing/tune-buffers", json=bad).status_code == 422
    too_long = {**payload, "time_limit_seconds": routing_api.MAX_TIME_LIMIT_SECONDS + 1}
    assert client.post("/api/v1/routing/tune-buffers", json=too_long).status_code == 422


# ── Benchmarks ───────────────────────────────────────────────────────────────


def test_benchmarks_say_so_when_the_artifact_is_absent(client, tmp_path, monkeypatch):
    monkeypatch.setattr(routing_api, "BENCHMARK_ARTIFACT_PATH", tmp_path / "benchmark_results.json")
    body = client.get("/api/v1/routing/benchmarks").json()
    assert body == {"available": False, "artifact": "docs/benchmark_results.json"}


def test_benchmarks_serve_the_artifact_as_written(client, tmp_path, monkeypatch):
    artifact = {
        "provenance": {"time_limit_seconds": 30.0, "source": "test"},
        "results": [{"instance": "x", "solver": "ortools", "total_distance": 1}],
    }
    path = tmp_path / "benchmark_results.json"
    path.write_text(json.dumps(artifact))
    monkeypatch.setattr(routing_api, "BENCHMARK_ARTIFACT_PATH", path)
    body = client.get("/api/v1/routing/benchmarks").json()
    assert body == {"available": True, "artifact": "docs/benchmark_results.json", **artifact}


@pytest.mark.parametrize("content", ["{not json", "[]", '{"results": "none"}'])
def test_a_malformed_benchmark_artifact_is_an_error_not_an_empty_table(client, tmp_path, monkeypatch, content):
    path = tmp_path / "benchmark_results.json"
    path.write_text(content)
    monkeypatch.setattr(routing_api, "BENCHMARK_ARTIFACT_PATH", path)
    assert client.get("/api/v1/routing/benchmarks").status_code == 500
