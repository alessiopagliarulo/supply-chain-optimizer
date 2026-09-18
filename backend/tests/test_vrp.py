"""Tests for the CVRPTW engine in app/vrp: data model, validator, three solvers, API.

Every instance here is small enough to check by hand or by brute force, and
every solver call is bounded to well under a second.
"""

import itertools
import math

import pytest

from app.vrp import (
    EXACT_MAX_CUSTOMERS,
    STATUS_INFEASIBLE,
    STATUS_NO_SOLUTION,
    STATUS_OPTIMAL,
    Node,
    VrpInstance,
    solve,
    solve_clarke_wright,
    solve_cpsat,
    solve_ortools,
    validate_routes,
)

OPEN = 1000  # a time window wide enough never to bind


def cross(capacity=2, vehicles=2, windows=None):
    """Depot at the origin, two customers up the y axis, two along the x axis.

        2 (0,20)
        1 (0,10)
        0 (0,0)  3 (10,0)  4 (20,0)

    Unit demand. With capacity 2 the optimum is obvious by hand: one vehicle
    per arm, 0-1-2-0 and 0-3-4-0, 40 each, total 80.
    """
    coords = [(0, 0), (0, 10), (0, 20), (10, 0), (20, 0)]
    windows = windows or {}
    nodes = [Node(0, 0, OPEN)] + [Node(1, *windows.get(i, (0, OPEN))) for i in range(1, 5)]
    return VrpInstance.from_coordinates(coords, nodes, num_vehicles=vehicles, vehicle_capacity=capacity)


def clustered(n=10, seed=0, vehicles=6, capacity=30):
    """A deterministic pseudo-random instance with real time windows."""
    import random

    rng = random.Random(seed)
    coords = [(50.0, 50.0)] + [(rng.uniform(0, 100), rng.uniform(0, 100)) for _ in range(n)]
    nodes = [Node(0, 0, 1000)]
    for i in range(1, n + 1):
        reach = math.ceil(math.dist(coords[0], coords[i]))
        ready = rng.randint(reach, 600)
        nodes.append(Node(rng.randint(3, 12), ready, ready + rng.randint(60, 200), 10))
    return VrpInstance.from_coordinates(coords, nodes, num_vehicles=vehicles, vehicle_capacity=capacity)


def brute_force_optimum(instance):
    """Cheapest feasible plan, by enumerating every ordering and every way to cut it into routes."""
    best = None
    customers = list(instance.customers)
    n = len(customers)
    for perm in itertools.permutations(customers):
        for cuts in range(min(n, instance.num_vehicles)):
            for points in itertools.combinations(range(1, n), cuts):
                bounds = (0, *points, n)
                routes = [list(perm[a:b]) for a, b in itertools.pairwise(bounds)]
                report = validate_routes(instance, routes)
                if report.feasible and (best is None or report.total_cost < best):
                    best = report.total_cost
    return best


# ── Data model ───────────────────────────────────────────────────────────────


def test_from_coordinates_builds_rounded_euclidean_matrix_and_defaults_travel_time():
    inst = cross()
    assert inst.distance[0][1] == 10
    assert inst.distance[2][4] == round(math.sqrt(800))  # 28
    assert inst.travel_time is inst.distance
    assert inst.num_customers == 4 and list(inst.customers) == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(nodes=[Node(0, 0, 10), Node(1, 5, 4)], distance=[[0, 1], [1, 0]]), "ready"),
        (dict(nodes=[Node(0, 0, 10), Node(1, 0, 4)], distance=[[0, 1]]), "matrix"),
        (dict(nodes=[Node(0, 0, 10), Node(1, 0, 4)], distance=[[0, -1], [1, 0]]), "non-negative"),
        (dict(nodes=[Node(3, 0, 10), Node(1, 0, 4)], distance=[[0, 1], [1, 0]]), "depot"),
    ],
)
def test_instance_rejects_malformed_input(kwargs, message):
    with pytest.raises(ValueError, match=message):
        VrpInstance(num_vehicles=1, vehicle_capacity=5, **kwargs)


# ── Validator ────────────────────────────────────────────────────────────────


def test_validator_accepts_hand_checked_plan_and_reports_its_schedule():
    report = validate_routes(cross(), [[1, 2], [3, 4]])
    assert report.feasible and report.violations == []
    assert report.total_cost == 80
    first = report.routes[0]
    assert first.load == 2 and first.distance == 40
    assert first.service_starts == [10, 20] and first.return_time == 40


def test_validator_waits_for_a_window_to_open():
    report = validate_routes(cross(windows={1: (50, 60)}), [[1, 2], [3, 4]])
    assert report.feasible
    assert report.routes[0].service_starts == [50, 60]


def test_validator_catches_capacity_violation():
    report = validate_routes(cross(capacity=2), [[1, 2, 3], [4]])
    assert not report.feasible
    assert any("exceeds capacity" in v for v in report.violations)


def test_validator_catches_late_service():
    report = validate_routes(cross(windows={2: (0, 15)}), [[1, 2], [3, 4]])
    assert not report.feasible
    assert any("customer 2" in v and "after its due time" in v for v in report.violations)


def test_validator_catches_late_return_to_depot():
    inst = VrpInstance(
        nodes=[Node(0, 0, 15), Node(1, 0, 15)], distance=[[0, 10], [10, 0]], num_vehicles=1, vehicle_capacity=1
    )
    report = validate_routes(inst, [[1]])
    assert not report.feasible
    assert any("returns to the depot at 20" in v for v in report.violations)


@pytest.mark.parametrize(
    "routes, fragment",
    [
        ([[1, 2], [3]], "customer 4 is not visited"),
        ([[1, 2], [3, 4, 1]], "customer 1 is visited 2 times"),
        ([[1], [2], [3, 4]], "3 routes but only 2 vehicles"),
        ([[1, 2], [3, 4, 0]], "node 0 is not a customer"),
        ([[1, 2], [3, 4, 9]], "node 9 is not a customer"),
        ([[1, 2], [3, 4], []], "empty route"),
    ],
)
def test_validator_catches_structural_violations(routes, fragment):
    report = validate_routes(cross(capacity=3), routes)
    assert not report.feasible
    assert any(fragment in v for v in report.violations), report.violations


# ── CP-SAT exact model ───────────────────────────────────────────────────────


def test_cpsat_proves_the_hand_computed_optimum():
    sol = solve_cpsat(cross(), time_limit_seconds=5)
    assert sol.status == STATUS_OPTIMAL and sol.proven_optimal and sol.feasible
    assert sol.total_cost == 80
    assert sorted(sorted(r) for r in sol.routes) == [[1, 2], [3, 4]]


def test_cpsat_uses_one_vehicle_when_capacity_allows_it():
    # 0-1-2-4-3-0 = 10 + 10 + 28 + 10 + 10 = 68 beats two out-and-back arms (80).
    sol = solve_cpsat(cross(capacity=4), time_limit_seconds=5)
    assert sol.proven_optimal and sol.total_cost == 68 and sol.vehicles_used == 1


def test_cpsat_follows_time_windows_even_when_it_costs_distance():
    # Unconstrained, the best single tour is 0-1-2-4-3-0 = 68. Customer 2 must now be
    # served by t=20, so the tour has to open with the long leg to 2 and pass 1 on the
    # way back (serving 1 at 100 after waiting). By hand, the tours that start at 2:
    #   2,1,4,3: 20 + 10 + 22 + 10 + 10 = 72   <- best
    #   2,1,3,4: 20 + 10 + 14 + 10 + 20 = 74
    #   2,4,3,1: 20 + 28 + 10 + 14 + 10 = 82
    inst = cross(capacity=4, vehicles=1, windows={2: (0, 20), 1: (100, 200)})
    sol = solve_cpsat(inst, time_limit_seconds=5)
    assert sol.proven_optimal and sol.feasible
    assert sol.routes == [[2, 1, 4, 3]] and sol.total_cost == 72
    assert sol.validation.routes[0].service_starts == [20, 100, 122, 132]


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_cpsat_matches_brute_force_on_small_random_instances(seed):
    inst = clustered(n=6, seed=seed, vehicles=3, capacity=25)
    expected = brute_force_optimum(inst)
    sol = solve_cpsat(inst, time_limit_seconds=5)
    assert expected is not None
    assert sol.proven_optimal and sol.total_cost == expected


def test_cpsat_reports_no_solution_on_an_infeasible_instance():
    # Total demand 4, one vehicle of capacity 2: nothing fits.
    sol = solve_cpsat(cross(capacity=2, vehicles=1), time_limit_seconds=5)
    assert sol.status == STATUS_NO_SOLUTION and sol.routes == [] and not sol.feasible


def test_cpsat_rejects_a_customer_heavier_than_any_vehicle():
    inst = VrpInstance(
        nodes=[Node(0, 0, 100), Node(5, 0, 100)], distance=[[0, 1], [1, 0]], num_vehicles=3, vehicle_capacity=4
    )
    assert solve_cpsat(inst).status == STATUS_NO_SOLUTION


# ── Clarke-Wright savings ────────────────────────────────────────────────────


def test_clarke_wright_finds_the_obvious_plan_on_the_cross():
    sol = solve_clarke_wright(cross())
    assert sol.feasible and sol.total_cost == 80
    assert sorted(sorted(r) for r in sol.routes) == [[1, 2], [3, 4]]


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_clarke_wright_is_feasible_and_no_better_than_the_optimum(seed):
    inst = clustered(n=10, seed=seed)
    cw = solve_clarke_wright(inst)
    exact = solve_cpsat(inst, time_limit_seconds=5)
    assert cw.feasible, cw.validation.violations
    assert exact.proven_optimal
    assert cw.total_cost >= exact.total_cost


def test_clarke_wright_scales_to_a_hundred_customers():
    sol = solve_clarke_wright(clustered(n=100, seed=7, vehicles=40, capacity=60))
    assert sol.feasible, sol.validation.violations
    assert sol.wall_seconds < 2


def test_clarke_wright_flags_a_fleet_that_is_too_small():
    sol = solve_clarke_wright(cross(capacity=2, vehicles=1))
    assert sol.status == STATUS_INFEASIBLE
    assert any("only 1 vehicles" in v for v in sol.validation.violations)


# ── OR-Tools routing ─────────────────────────────────────────────────────────


def test_ortools_finds_the_optimum_on_the_cross():
    sol = solve_ortools(cross(), time_limit_seconds=0.3)
    assert sol.feasible and not sol.proven_optimal
    assert sol.total_cost == 80


@pytest.mark.parametrize("seed", [0, 1])
def test_ortools_is_feasible_on_windowed_instances(seed):
    inst = clustered(n=12, seed=seed)
    sol = solve_ortools(inst, time_limit_seconds=0.3)
    exact = solve_cpsat(inst, time_limit_seconds=5)
    assert sol.feasible, sol.validation.violations
    assert sol.total_cost >= exact.total_cost
    # The solver's own objective is the same quantity the validator recomputes.
    assert sol.stats["objective"] == sol.total_cost


def test_ortools_respects_time_windows():
    inst = cross(capacity=4, vehicles=1, windows={2: (0, 20), 1: (100, 200)})
    sol = solve_ortools(inst, time_limit_seconds=0.3)
    assert sol.feasible and sol.routes[0][0] == 2


def test_ortools_reports_no_solution_on_an_infeasible_instance():
    sol = solve_ortools(cross(capacity=2, vehicles=1), time_limit_seconds=0.3)
    assert sol.status == STATUS_NO_SOLUTION and not sol.feasible


# ── Dispatcher ───────────────────────────────────────────────────────────────


def test_auto_uses_the_exact_model_on_small_instances_and_ortools_above():
    assert solve(cross(), "auto").method == "cpsat"
    big = clustered(n=EXACT_MAX_CUSTOMERS + 1, seed=3, vehicles=10)
    assert solve(big, "auto", time_limit_seconds=0.3).method == "ortools"


def test_solve_rejects_unknown_method():
    with pytest.raises(ValueError, match="unknown method"):
        solve(cross(), "simulated_annealing")


def test_no_customers_is_a_trivial_empty_plan():
    inst = VrpInstance(nodes=[Node(0, 0, 10)], distance=[[0]], num_vehicles=1, vehicle_capacity=1)
    for method in ("cpsat", "clarke_wright", "ortools"):
        sol = solve(inst, method)
        assert sol.feasible and sol.routes == [] and sol.total_cost == 0


# ── API ──────────────────────────────────────────────────────────────────────

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


@pytest.mark.parametrize("method", ["cpsat", "clarke_wright", "ortools"])
def test_api_solves_with_each_method(client, method):
    resp = client.post("/api/v1/routing/solve", json={**CROSS_PAYLOAD, "method": method, "time_limit_seconds": 0.3})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["method"] == method and body["feasible"] is True
    assert body["total_cost"] == 80 and body["vehicles_used"] == 2
    assert body["validation"]["violations"] == []
    assert len(body["validation"]["routes"]) == 2


def test_api_accepts_an_explicit_distance_matrix(client):
    payload = {
        "nodes": [{"due": 100}, {"demand": 1, "due": 100}, {"demand": 1, "due": 100}],
        "distance_matrix": [[0, 5, 7], [5, 0, 3], [7, 3, 0]],
        "num_vehicles": 1,
        "vehicle_capacity": 2,
        "method": "cpsat",
    }
    body = client.post("/api/v1/routing/solve", json=payload).json()
    assert body["status"] == "optimal" and body["total_cost"] == 15


def test_api_rejects_missing_distances_and_oversized_exact_requests(client):
    no_coords = {**CROSS_PAYLOAD, "nodes": [{"due": 10}, {"demand": 1, "due": 10}]}
    assert client.post("/api/v1/routing/solve", json=no_coords).status_code == 422

    many = [{"x": 0, "y": 0, "due": OPEN}] + [{"x": i, "y": i, "demand": 1, "due": OPEN} for i in range(1, 30)]
    resp = client.post(
        "/api/v1/routing/solve", json={"nodes": many, "num_vehicles": 30, "vehicle_capacity": 5, "method": "cpsat"}
    )
    assert resp.status_code == 422 and "capped" in resp.text

    too_long = {**CROSS_PAYLOAD, "time_limit_seconds": 60}
    assert client.post("/api/v1/routing/solve", json=too_long).status_code == 422

    bad_window = {**CROSS_PAYLOAD, "nodes": [{"x": 0, "y": 0, "ready": 5, "due": 1}]}
    assert client.post("/api/v1/routing/solve", json=bad_window).status_code == 422


@pytest.mark.parametrize(
    "override",
    [
        {"vehicle_capacity": 10**19},
        {"nodes": [{"x": 0, "y": 0, "due": 10**19}, {"x": 1, "y": 1, "demand": 1, "due": OPEN}]},
        {"nodes": [{"x": 0, "y": 0, "due": OPEN}, {"x": 1, "y": 1, "demand": 10**10, "due": OPEN}]},
        {"nodes": [{"x": 0, "y": 0, "due": OPEN}, {"x": 1, "y": 1, "service_time": 10**10, "due": OPEN}]},
        {"nodes": [{"x": 0, "y": 0, "due": OPEN}, {"x": 1e300, "y": 1, "demand": 1, "due": OPEN}]},
        {
            "nodes": [{"due": OPEN}, {"demand": 1, "due": OPEN}],
            "distance_matrix": [[0, 2**62], [2**62, 0]],
        },
    ],
)
def test_api_rejects_values_beyond_the_solver_integer_range(client, override):
    resp = client.post("/api/v1/routing/solve", json={**CROSS_PAYLOAD, **override, "method": "cpsat"})
    assert resp.status_code == 422, resp.text


def test_api_solves_at_the_value_caps(client):
    from app.api.routing import MAX_VALUE

    payload = {
        "nodes": [{"due": MAX_VALUE}, {"demand": 1, "due": MAX_VALUE}],
        "distance_matrix": [[0, 10**8], [10**8, 0]],
        "num_vehicles": 1,
        "vehicle_capacity": MAX_VALUE,
        "method": "cpsat",
    }
    body = client.post("/api/v1/routing/solve", json=payload).json()
    assert body["status"] == "optimal" and body["total_cost"] == 2 * 10**8
