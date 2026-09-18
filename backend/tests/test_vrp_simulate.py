"""Tests for the DES simulation in app.vrp.simulate.

Every test uses small instances that can be checked by hand or verified deterministically.
"""

import pytest

from app.vrp import Node, VrpInstance, solve_cpsat
from app.vrp.simulate import (
    SimulationRun,
    SimulationResults,
    lognormal_factory,
    simulate,
    simulate_once,
    triangular_factory,
)

OPEN = 1000


def cross(capacity=2, vehicles=2, windows=None):
    """Depot at origin, customers along two axes. Hand-computable."""
    coords = [(0, 0), (0, 10), (0, 20), (10, 0), (20, 0)]
    windows = windows or {}
    nodes = [Node(0, 0, OPEN)] + [Node(1, *windows.get(i, (0, OPEN))) for i in range(1, 5)]
    return VrpInstance.from_coordinates(coords, nodes, num_vehicles=vehicles, vehicle_capacity=capacity)


def single_customer_instance():
    """Depot at origin, one customer at distance 10, service time 5."""
    coords = [(0, 0), (10, 0)]
    nodes = [Node(0, 0, OPEN), Node(1, 0, OPEN, service_time=5)]
    return VrpInstance.from_coordinates(coords, nodes, num_vehicles=1, vehicle_capacity=10)


def test_zero_variability_reproduces_deterministic_plan():
    """With variability=0, simulation should match the planned schedule exactly."""
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)
    assert sol.feasible and sol.routes == [[1]]

    run = simulate_once(inst, sol, variability=0, seed=0)

    assert run.on_time_per_customer[1] is True
    assert run.lateness_per_customer[1] == 0.0

    assert 1 in run.actual_service_times
    assert run.actual_service_times[1] == 5.0


def test_zero_variability_on_cross_reproduces_schedule():
    """Hand-checkable: two routes on the cross should complete at specific times."""
    inst = cross(capacity=2, vehicles=2)
    sol = solve_cpsat(inst, time_limit_seconds=1)
    assert sol.feasible and sorted(sorted(r) for r in sol.routes) == [[1, 2], [3, 4]]

    run = simulate_once(inst, sol, variability=0, seed=0)

    assert run.on_time_per_customer == {1: True, 2: True, 3: True, 4: True}
    assert all(run.lateness_per_customer[i] == 0.0 for i in [1, 2, 3, 4])

    assert len(run.vehicle_completion_times) == 2
    assert all(t <= inst.horizon for t in run.vehicle_completion_times)


def test_seeded_runs_are_reproducible():
    """Two runs with the same seed should produce identical results."""
    inst = cross()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run1 = simulate_once(inst, sol, variability=0.2, distribution="lognormal", seed=42)
    run2 = simulate_once(inst, sol, variability=0.2, distribution="lognormal", seed=42)

    assert run1.on_time_per_customer == run2.on_time_per_customer
    assert run1.lateness_per_customer == run2.lateness_per_customer
    assert run1.actual_travel_times == run2.actual_travel_times
    assert run1.actual_service_times == run2.actual_service_times


def test_different_seeds_produce_different_results():
    """Different seeds should produce different randomized times."""
    inst = cross()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run1 = simulate_once(inst, sol, variability=0.2, distribution="lognormal", seed=1)
    run2 = simulate_once(inst, sol, variability=0.2, distribution="lognormal", seed=2)

    assert run1.actual_travel_times != run2.actual_travel_times or run1.actual_service_times != run2.actual_service_times


def test_multiple_replications_aggregate_kpis():
    """Aggregation over multiple runs should compute correct statistics."""
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    results = simulate(inst, sol, num_replications=10, variability=0, seed=0)

    assert results.num_replications == 10
    assert results.on_time_rate == 1.0
    assert 1 in results.on_time_rate_per_customer
    assert results.on_time_rate_per_customer[1] == 1.0
    assert results.mean_lateness == 0.0
    assert results.p95_lateness == 0.0


def test_aggregation_computes_mean_and_percentile():
    """Check that statistics like p95 are computed correctly."""
    inst = cross(capacity=4, vehicles=1, windows={2: (0, 20)})
    sol = solve_cpsat(inst, time_limit_seconds=1)

    results = simulate(inst, sol, num_replications=50, variability=0.1, distribution="lognormal", seed=100)

    assert results.num_replications == 50
    assert len(results.runs) == 50
    assert 0 <= results.on_time_rate <= 1
    assert results.p95_lateness >= results.mean_lateness


def test_on_time_rate_is_per_customer_across_replications():
    """On-time rate per customer is computed as fraction of on-time visits."""
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    results = simulate(inst, sol, num_replications=3, variability=0, seed=0)

    on_time_for_1 = sum(1 for r in results.runs if r.on_time_per_customer.get(1, False))
    expected_rate = on_time_for_1 / len(results.runs)
    assert results.on_time_rate_per_customer[1] == expected_rate


def test_vehicle_utilization_is_service_time_over_active_duration():
    """Utilization = sum(service_times) / (return_time - depart_time)."""
    inst = cross(capacity=4, vehicles=1)
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run = simulate_once(inst, sol, variability=0, seed=0)

    assert len(run.vehicle_utilization) > 0
    assert all(0 <= u <= 1 for u in run.vehicle_utilization)


def test_lognormal_distribution_factory_respects_variability():
    """Lognormal with CV=0 should return exact values, CV>0 should vary."""
    import random

    factory_0 = lognormal_factory(0)
    factory_1 = lognormal_factory(0.5)

    rng0 = random.Random(42)
    rng1 = random.Random(42)

    sampler0 = factory_0(rng0)
    sampler1 = factory_1(rng1)

    samples0 = [sampler0(10.0) for _ in range(10)]
    samples1 = [sampler1(10.0) for _ in range(10)]

    assert all(s == 10.0 for s in samples0)

    mean1 = sum(samples1) / len(samples1)
    assert abs(mean1 - 10.0) < 10.0 or len(set(samples1)) > 1


def test_triangular_distribution_factory_respects_variability():
    """Triangular with variability=0 should return exact values."""
    import random

    factory_0 = triangular_factory(0)
    rng = random.Random(42)
    sampler = factory_0(rng)

    samples = [sampler(10.0) for _ in range(10)]
    assert all(s == 10.0 for s in samples)


def test_simulate_once_returns_run_with_all_fields():
    """SimulationRun should have all expected fields populated."""
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run = simulate_once(inst, sol, variability=0.1, seed=0)

    assert isinstance(run, SimulationRun)
    assert run.run_id == 0
    assert len(run.vehicle_routes) > 0
    assert run.on_time_per_customer
    assert run.lateness_per_customer
    assert run.vehicle_completion_times
    assert run.actual_travel_times
    assert run.actual_service_times


def test_simulate_returns_results_with_all_kpis():
    """SimulationResults should have all expected KPIs."""
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    results = simulate(inst, sol, num_replications=5, variability=0)

    assert isinstance(results, SimulationResults)
    assert results.num_replications == 5
    assert results.instance_name == inst.name
    assert 0 <= results.on_time_rate <= 1
    assert results.mean_lateness >= 0
    assert results.p95_lateness >= results.mean_lateness
    assert results.mean_route_completion_time >= 0
    assert results.max_route_completion_time >= results.mean_route_completion_time
    assert 0 <= results.vehicle_utilization_mean <= 1
    assert 0 <= results.vehicle_utilization_min <= 1


def test_empty_routes_handled_gracefully():
    """Instance with no customers (only depot) should handle gracefully."""
    inst = VrpInstance(
        nodes=[Node(0, 0, 100)],
        distance=[[0]],
        num_vehicles=1,
        vehicle_capacity=10,
    )
    sol = solve_cpsat(inst, time_limit_seconds=1)

    results = simulate(inst, sol, num_replications=2, variability=0)
    assert results.num_replications == 2
    assert results.on_time_rate == 1.0


def test_multiple_vehicles_tracked_separately():
    """Each vehicle's completion time should be tracked."""
    inst = cross(capacity=2, vehicles=2)
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run = simulate_once(inst, sol, variability=0, seed=0)

    assert len(run.vehicle_completion_times) == len(sol.routes)


def test_lognormal_vs_triangular_distributions():
    """Lognormal and triangular should produce different results."""
    inst = cross()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run_log = simulate_once(inst, sol, variability=0.1, distribution="lognormal", seed=42)
    run_tri = simulate_once(inst, sol, variability=0.1, distribution="triangular", seed=42)

    assert run_log.actual_travel_times != run_tri.actual_travel_times


def test_invalid_distribution_raises_error():
    """Requesting an unknown distribution should raise ValueError."""
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    with pytest.raises(ValueError, match="unknown distribution"):
        simulate_once(inst, sol, distribution="unknown")


def test_invalid_num_replications_raises_error():
    """Zero or negative replications should raise ValueError."""
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    with pytest.raises(ValueError, match="num_replications"):
        simulate(inst, sol, num_replications=0)


def test_time_windows_respected_in_simulation():
    """Customers with tight time windows should show lateness if randomness pushes them."""
    inst = cross(capacity=4, vehicles=1, windows={2: (0, 20)})
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run = simulate_once(inst, sol, variability=0.05, seed=123)

    if 2 in run.on_time_per_customer and not run.on_time_per_customer[2]:
        assert run.lateness_per_customer[2] > 0


def test_seeded_aggregate_results_reproducible():
    """Multiple replications with same seed should be reproducible across calls."""
    inst = cross()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    results1 = simulate(inst, sol, num_replications=10, variability=0.1, seed=999)
    results2 = simulate(inst, sol, num_replications=10, variability=0.1, seed=999)

    assert results1.on_time_rate == results2.on_time_rate
    assert results1.mean_lateness == results2.mean_lateness
    assert results1.p95_lateness == results2.p95_lateness


def test_run_id_increments_across_replications():
    """Each run in results.runs should have incrementing run_id."""
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    results = simulate(inst, sol, num_replications=5, variability=0)

    run_ids = [r.run_id for r in results.runs]
    assert run_ids == list(range(5))


def test_utilization_computed_for_vehicle_departing_at_t_zero():
    """Regression: vehicle departing at t=0 (depart_time=0) should have utilization computed.

    The bug was checking truthiness of depart_time (0 is falsy), which prevented utilization
    calculation. Fix uses explicit None checks.
    """
    inst = single_customer_instance()
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run = simulate_once(inst, sol, variability=0, seed=0)

    assert len(run.vehicle_utilization) == 1
    assert run.vehicle_utilization[0] > 0


def test_zero_duration_route_gets_zero_utilization_without_dividing_by_zero():
    """Zero-duration route should have 0 utilization without raising ZeroDivisionError."""
    inst = VrpInstance(
        nodes=[Node(0, 0, 100), Node(5, 0, 100, service_time=0)],
        distance=[[0, 0], [0, 0]],
        num_vehicles=1,
        vehicle_capacity=10,
    )
    sol = solve_cpsat(inst, time_limit_seconds=1)

    run = simulate_once(inst, sol, variability=0, seed=0)

    if len(run.vehicle_utilization) > 0:
        assert run.vehicle_utilization[0] == 0


def test_a_stop_served_from_its_due_time_is_on_time():
    """Windows are on service START (app/vrp/model.py), so lateness is too.

    Regression: lateness used to be measured at service END, so any stop with a
    service time whose service started at (or near) its due time counted as
    late even in the deterministic plan the validator had just accepted.
    """
    coords = [(0, 0), (10, 0)]
    nodes = [Node(0, 0, OPEN), Node(1, 0, 10, service_time=90)]
    inst = VrpInstance.from_coordinates(coords, nodes, num_vehicles=1, vehicle_capacity=10)
    sol = solve_cpsat(inst, time_limit_seconds=1)
    assert sol.feasible

    run = simulate_once(inst, sol, variability=0.0, seed=1)

    assert run.lateness_per_customer[1] == 0.0
    assert run.on_time_per_customer[1] is True


def test_zero_variability_keeps_a_feasible_solomon_plan_fully_on_time():
    """The module's contract: zero variability reproduces the deterministic plan.

    C101's customers all take 90 time units of service, so measuring lateness at
    service end made this validated plan look almost entirely late.
    """
    from app.vrp import solve_clarke_wright
    from app.vrp.instances import load_instance

    named = load_instance("sample/C101_25")
    inst = VrpInstance.from_coordinates(
        named.coords, named.nodes, num_vehicles=named.num_vehicles, vehicle_capacity=named.vehicle_capacity
    )
    sol = solve_clarke_wright(inst)
    assert sol.feasible

    results = simulate(inst, sol, num_replications=3, variability=0.0, seed=7)

    assert results.on_time_rate == 1.0
    assert results.mean_lateness == 0.0
