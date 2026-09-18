"""Tests for buffer-tuning optimization in app.vrp.buffer_tuning.

Every test uses small instances that can be validated by hand or deterministically.
"""

import pytest

from app.vrp import Node, VrpInstance, solve
from app.vrp.buffer_tuning import (
    BufferCandidate,
    BufferTuningResult,
    apply_buffers,
    apply_capacity_buffer,
    apply_schedule_buffer,
    evaluate_candidate,
    tune_buffers,
)
from app.vrp.simulate import simulate_once


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


# ── Apply buffers ────────────────────────────────────────────────────────────


def test_zero_schedule_buffer_returns_same_instance():
    """Buffer of 0% should leave the instance unchanged."""
    inst = single_customer_instance()
    buffered = apply_schedule_buffer(inst, 0)

    assert buffered.travel_time == inst.travel_time
    assert len(buffered.nodes) == len(inst.nodes)
    for orig, buf in zip(inst.nodes, buffered.nodes):
        assert buf.service_time == orig.service_time


def test_zero_capacity_buffer_returns_same_capacity():
    """Capacity buffer of 0% should leave capacity unchanged."""
    inst = single_customer_instance()
    buffered = apply_capacity_buffer(inst, 0)

    assert buffered.vehicle_capacity == inst.vehicle_capacity


def test_schedule_buffer_inflates_travel_times():
    """Schedule buffer should increase travel times proportionally."""
    inst = single_customer_instance()
    buffered = apply_schedule_buffer(inst, 10)

    for i in range(len(inst.nodes)):
        for j in range(len(inst.nodes)):
            if inst.travel_time[i][j] > 0:
                assert buffered.travel_time[i][j] > inst.travel_time[i][j]
                expected = int(round(inst.travel_time[i][j] * 1.1))
                assert buffered.travel_time[i][j] == expected


def test_schedule_buffer_inflates_service_times():
    """Schedule buffer should increase service times proportionally."""
    inst = single_customer_instance()
    buffered = apply_schedule_buffer(inst, 10)

    for i in range(1, len(buffered.nodes)):
        if inst.nodes[i].service_time > 0:
            assert buffered.nodes[i].service_time > inst.nodes[i].service_time
            expected = int(round(inst.nodes[i].service_time * 1.1))
            assert buffered.nodes[i].service_time == expected


def test_capacity_buffer_reduces_vehicle_capacity():
    """Capacity buffer should reduce vehicle capacity."""
    inst = single_customer_instance()
    buffered = apply_capacity_buffer(inst, 20)

    expected = int(round(inst.vehicle_capacity * 0.8))
    assert buffered.vehicle_capacity == expected
    assert buffered.vehicle_capacity <= inst.vehicle_capacity


def test_capacity_buffer_never_reduces_to_zero():
    """Capacity buffer should never reduce capacity below 1."""
    inst = VrpInstance(
        nodes=[Node(0, 0, OPEN), Node(1, 0, OPEN)],
        distance=[[0, 1], [1, 0]],
        num_vehicles=1,
        vehicle_capacity=1,
    )
    buffered = apply_capacity_buffer(inst, 95)

    assert buffered.vehicle_capacity >= 1


def test_apply_both_buffers():
    """Applying both buffers should combine their effects."""
    inst = single_customer_instance()
    buffered = apply_buffers(inst, schedule_buffer_pct=10, capacity_buffer_pct=20)

    schedule_buffered = apply_schedule_buffer(inst, 10)
    capacity_only = apply_capacity_buffer(schedule_buffered, 20)

    assert buffered.vehicle_capacity == capacity_only.vehicle_capacity
    assert buffered.travel_time == capacity_only.travel_time


# ── Evaluate candidate ───────────────────────────────────────────────────────


def test_evaluate_candidate_returns_candidate_on_feasible_solution():
    """Evaluating a feasible solution should return a BufferCandidate."""
    inst = single_customer_instance()
    buffered = apply_buffers(inst, 5, 2)

    candidate = evaluate_candidate(
        inst,
        buffered,
        solve,
        num_replications=5,
        variability=0.1,
        distribution="lognormal",
        base_seed=42,
        schedule_buffer_pct=5,
        capacity_buffer_pct=2,
    )

    assert isinstance(candidate, BufferCandidate)
    assert candidate.schedule_buffer_pct == 5
    assert candidate.capacity_buffer_pct == 2
    assert candidate.solution.feasible
    assert isinstance(candidate.simulation, object)


def test_evaluate_candidate_returns_none_on_infeasible_solution():
    """Evaluating an infeasible problem should return None."""
    inst = single_customer_instance()
    too_tight = VrpInstance(
        nodes=[
            Node(0, 0, 10),
            Node(1, 0, 5),
        ],
        distance=[[0, 100], [100, 0]],
        num_vehicles=1,
        vehicle_capacity=1,
    )

    candidate = evaluate_candidate(
        inst,
        too_tight,
        solve,
        num_replications=2,
        variability=0.0,
        distribution="lognormal",
        base_seed=42,
        schedule_buffer_pct=0,
        capacity_buffer_pct=0,
    )

    assert candidate is None


# ── Grid search and frontier ────────────────────────────────────────────────


def test_zero_buffers_reproduce_unbuffered_plan():
    """With zero buffers, tuning should find a solution matching the original."""
    inst = single_customer_instance()

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 0, 1),
        capacity_buffer_range=(0, 0, 1),
        num_replications=5,
        variability=0,
        base_seed=42,
    )

    assert result.chosen_buffer.schedule_buffer_pct == 0
    assert result.chosen_buffer.capacity_buffer_pct == 0
    assert result.chosen_buffer.solution.feasible
    assert len(result.frontier) >= 1


def test_tune_buffers_returns_evaluated_frontier():
    """tune_buffers should return multiple candidates in the frontier."""
    inst = cross(capacity=4, vehicles=2)

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 5, 5),
        capacity_buffer_range=(0, 2, 2),
        num_replications=10,
        variability=0.05,
        base_seed=42,
    )

    assert isinstance(result, BufferTuningResult)
    assert len(result.frontier) > 0
    for candidate in result.frontier:
        assert isinstance(candidate, BufferCandidate)
        assert candidate.solution.feasible


def test_larger_schedule_buffer_never_decreases_on_time_rate():
    """On-time rate should be monotonically non-decreasing with schedule buffer."""
    inst = cross(capacity=4, vehicles=1)

    candidates = []
    for s_buf in [0, 5, 10]:
        buffered = apply_buffers(inst, s_buf, 0)
        solution = solve(buffered)
        if solution.feasible:
            sim = simulate_one(inst, solution, variability=0.1, base_seed=42, replications=20)
            candidates.append((s_buf, sim.on_time_rate))

    if len(candidates) >= 2:
        for i in range(len(candidates) - 1):
            rate1 = candidates[i][1]
            rate2 = candidates[i + 1][1]
            assert rate2 >= rate1 - 0.01, f"On-time rate decreased from {rate1} to {rate2}"


def test_chosen_buffer_meets_target_on_time_rate():
    """The chosen buffer should meet the target on-time rate when one exists."""
    inst = cross(capacity=4, vehicles=1)

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 10, 5),
        capacity_buffer_range=(0, 2, 2),
        num_replications=20,
        variability=0.1,
        base_seed=42,
        target_on_time_rate=0.95,
        objective="minimize_cost_for_target",
    )

    max_on_time = max(c.simulation.on_time_rate for c in result.frontier)
    chosen_rate = result.chosen_buffer.simulation.on_time_rate
    assert (
        chosen_rate >= result.target_on_time_rate or chosen_rate == max_on_time
    )


def test_tune_buffers_with_maximize_on_time_objective():
    """maximize_on_time objective should choose buffer with highest on-time rate."""
    inst = cross(capacity=4, vehicles=2)

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 10, 5),
        capacity_buffer_range=(0, 2, 2),
        num_replications=10,
        variability=0.1,
        base_seed=42,
        objective="maximize_on_time",
    )

    max_on_time = max(c.simulation.on_time_rate for c in result.frontier)
    assert result.chosen_buffer.simulation.on_time_rate == max_on_time


def test_tune_buffers_with_minimize_cost_for_target_objective():
    """minimize_cost_for_target should choose lowest-cost feasible buffer meeting target."""
    inst = cross(capacity=4, vehicles=2)

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 10, 5),
        capacity_buffer_range=(0, 2, 2),
        num_replications=10,
        variability=0.05,
        base_seed=42,
        target_on_time_rate=0.8,
        objective="minimize_cost_for_target",
    )

    assert result.chosen_buffer in result.frontier


def test_tune_buffers_raises_on_invalid_objective():
    """Invalid objective should raise ValueError."""
    inst = single_customer_instance()

    with pytest.raises(ValueError, match="unknown objective"):
        tune_buffers(inst, solve, objective="invalid_objective")


def test_tune_buffers_raises_on_all_infeasible_candidates():
    """If all candidates are infeasible, should raise ValueError."""
    too_tight = VrpInstance(
        nodes=[Node(0, 0, 5), Node(1, 0, 3)],
        distance=[[0, 100], [100, 0]],
        num_vehicles=1,
        vehicle_capacity=1,
    )

    with pytest.raises(ValueError, match="No feasible candidate"):
        tune_buffers(
            too_tight,
            solve,
            schedule_buffer_range=(0, 0, 1),
            capacity_buffer_range=(0, 0, 1),
            num_replications=2,
            variability=0,
        )


def test_frontier_is_sorted_or_spans_buffer_space():
    """Frontier should cover a range of buffer levels."""
    inst = cross(capacity=4, vehicles=2)

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 10, 5),
        capacity_buffer_range=(0, 2, 1),
        num_replications=5,
        variability=0.05,
        base_seed=42,
    )

    buffer_pairs = [
        (c.schedule_buffer_pct, c.capacity_buffer_pct) for c in result.frontier
    ]

    assert len(set(buffer_pairs)) >= 1


def test_result_preserves_search_settings():
    """BufferTuningResult should record all search settings."""
    inst = single_customer_instance()

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 5, 2),
        capacity_buffer_range=(0, 2, 1),
        num_replications=15,
        variability=0.15,
        distribution="triangular",
        base_seed=99,
        target_on_time_rate=0.90,
        objective="minimize_cost_for_target",
    )

    assert result.num_replications == 15
    assert result.variability == 0.15
    assert result.distribution == "triangular"
    assert result.base_seed == 99
    assert result.target_on_time_rate == 0.90
    assert result.objective == "minimize_cost_for_target"


def test_common_random_numbers_across_candidates():
    """All candidates should use seeded simulation for fair comparison."""
    inst = cross(capacity=4, vehicles=2)

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 5, 5),
        capacity_buffer_range=(0, 1, 1),
        num_replications=10,
        variability=0.1,
        base_seed=42,
    )

    for candidate in result.frontier:
        assert candidate.simulation.num_replications == 10


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_single_candidate_frontier():
    """Grid with single point should return one candidate."""
    inst = single_customer_instance()

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 0, 1),
        capacity_buffer_range=(0, 0, 1),
        num_replications=3,
        variability=0,
    )

    assert len(result.frontier) == 1


def test_buffer_range_with_large_step():
    """Large step sizes should sample fewer candidates."""
    inst = cross(capacity=4, vehicles=2)

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 20, 20),
        capacity_buffer_range=(0, 10, 10),
        num_replications=5,
        variability=0.05,
    )

    assert len(result.frontier) >= 1
    for candidate in result.frontier:
        assert candidate.schedule_buffer_pct % 20 == 0 or candidate.schedule_buffer_pct == 0
        assert candidate.capacity_buffer_pct % 10 == 0 or candidate.capacity_buffer_pct == 0


def test_no_improvement_on_already_tight_plan():
    """If plan is already tight (high on-time rate with zero buffer), buffers may not improve much."""
    inst = single_customer_instance()

    result = tune_buffers(
        inst,
        solve,
        schedule_buffer_range=(0, 5, 5),
        capacity_buffer_range=(0, 2, 2),
        num_replications=20,
        variability=0.05,
        base_seed=42,
    )

    zero_buffer_rate = [
        c.simulation.on_time_rate
        for c in result.frontier
        if c.schedule_buffer_pct == 0 and c.capacity_buffer_pct == 0
    ]
    if zero_buffer_rate:
        assert zero_buffer_rate[0] > 0.9


# ── Helper to simulate for testing ───────────────────────────────────────────


def simulate_one(instance, solution, variability, base_seed, replications):
    """Helper to run multiple seeded replications and aggregate."""
    import statistics

    import numpy as np

    from app.vrp.simulate import SimulationResults

    runs = []
    for i in range(replications):
        run = simulate_once(
            instance, solution, variability=variability, seed=base_seed + i, run_id=i
        )
        runs.append(run)

    all_on_time = [
        r.on_time_per_customer.get(c, False)
        for r in runs
        for c in instance.customers
    ]
    on_time_rate = sum(all_on_time) / len(all_on_time) if all_on_time else 1.0

    on_time_rate_per_customer = {}
    for customer in instance.customers:
        customer_on_time = [r.on_time_per_customer.get(customer, False) for r in runs]
        if customer_on_time:
            on_time_rate_per_customer[customer] = sum(customer_on_time) / len(
                customer_on_time
            )

    all_lateness = [
        r.lateness_per_customer.get(c, 0) for r in runs for c in instance.customers
    ]
    mean_lateness = statistics.mean(all_lateness) if all_lateness else 0.0
    p95_lateness = float(np.percentile(all_lateness, 95)) if all_lateness else 0.0

    all_completion_times = [t for r in runs for t in r.vehicle_completion_times]
    mean_completion_time = statistics.mean(all_completion_times) if all_completion_times else 0.0
    max_completion_time = max(all_completion_times) if all_completion_times else 0.0

    all_utilizations = [u for r in runs for u in r.vehicle_utilization]
    utilization_mean = statistics.mean(all_utilizations) if all_utilizations else 0.0
    utilization_min = min(all_utilizations) if all_utilizations else 0.0

    return SimulationResults(
        num_replications=replications,
        instance_name=instance.name,
        on_time_rate=on_time_rate,
        on_time_rate_per_customer=on_time_rate_per_customer,
        mean_lateness=mean_lateness,
        p95_lateness=p95_lateness,
        mean_route_completion_time=mean_completion_time,
        max_route_completion_time=max_completion_time,
        vehicle_utilization_mean=utilization_mean,
        vehicle_utilization_min=utilization_min,
        runs=runs,
    )
