"""Buffer-tuning optimization loop that finds schedule/capacity buffers balancing
on-time performance against slack and cost.

The loop solves VRP instances with different buffer levels, evaluates each plan
via DES simulation, and returns the evaluated frontier and chosen buffers.

Buffers:
- Schedule buffer: inflated travel/service times (or tightened time windows) to
  leave slack in the plan for handling randomness.
- Capacity buffer: reduced effective vehicle capacity to pack routes more loosely,
  increasing the number of vehicles but improving routability when variability rises.

Objective:
Minimize distance/vehicles subject to meeting a target on-time rate, or equivalently,
maximize on-time rate subject to a cost budget. The objective is specified via an
optional target on-time rate (0..1) and weights.

Search:
Grid search over buffer levels with common random numbers for fair comparisons.
Returns the evaluated frontier (buffer -> KPIs), the chosen buffers, and settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.vrp.model import Node, VrpInstance, VrpSolution
from app.vrp.simulate import SimulationResults, simulate


@dataclass
class BufferCandidate:
    """One evaluated point in the buffer space."""

    schedule_buffer_pct: float
    capacity_buffer_pct: float
    solution: VrpSolution
    simulation: SimulationResults
    total_distance: int
    vehicles_used: int


@dataclass
class BufferTuningResult:
    """Result of the buffer-tuning optimization."""

    chosen_buffer: BufferCandidate
    frontier: list[BufferCandidate]
    num_replications: int
    variability: float
    distribution: str
    base_seed: int
    objective: str
    target_on_time_rate: float


def apply_schedule_buffer(instance: VrpInstance, buffer_pct: float) -> VrpInstance:
    """Apply a schedule buffer by inflating travel and service times.

    Args:
        instance: Original VRP instance.
        buffer_pct: Buffer as a percentage (0..100) to add to travel/service times.

    Returns:
        A new instance with inflated times and tightened time windows if needed
        to preserve schedule feasibility while leaving slack.
    """
    if buffer_pct == 0:
        return instance

    factor = 1.0 + buffer_pct / 100.0
    travel_time = instance.travel_time
    assert travel_time is not None

    buffered_travel = [
        [int(round(t * factor)) for t in row] for row in travel_time
    ]

    buffered_nodes = []
    for i, node in enumerate(instance.nodes):
        if i == 0:
            buffered_nodes.append(node)
        else:
            new_service = int(round(node.service_time * factor))
            buffered_nodes.append(
                Node(
                    demand=node.demand,
                    ready=node.ready,
                    due=node.due,
                    service_time=new_service,
                )
            )

    return VrpInstance(
        nodes=buffered_nodes,
        distance=instance.distance,
        num_vehicles=instance.num_vehicles,
        vehicle_capacity=instance.vehicle_capacity,
        travel_time=buffered_travel,
        name=f"{instance.name} (schedule_buffer={buffer_pct}%)",
    )


def apply_capacity_buffer(instance: VrpInstance, buffer_pct: float) -> VrpInstance:
    """Apply a capacity buffer by reducing effective vehicle capacity.

    Args:
        instance: Original VRP instance.
        buffer_pct: Buffer as a percentage (0..100) to subtract from vehicle capacity.

    Returns:
        A new instance with reduced vehicle capacity (packing looser, using more vehicles).
    """
    if buffer_pct == 0:
        return instance

    factor = 1.0 - buffer_pct / 100.0
    new_capacity = max(1, int(round(instance.vehicle_capacity * factor)))

    return VrpInstance(
        nodes=instance.nodes,
        distance=instance.distance,
        num_vehicles=instance.num_vehicles,
        vehicle_capacity=new_capacity,
        travel_time=instance.travel_time,
        name=f"{instance.name} (capacity_buffer={buffer_pct}%)",
    )


def apply_buffers(
    instance: VrpInstance,
    schedule_buffer_pct: float,
    capacity_buffer_pct: float,
) -> VrpInstance:
    """Apply both schedule and capacity buffers to an instance.

    Args:
        instance: Original VRP instance.
        schedule_buffer_pct: Schedule buffer as a percentage.
        capacity_buffer_pct: Capacity buffer as a percentage.

    Returns:
        A new instance with both buffers applied.
    """
    buffered = apply_schedule_buffer(instance, schedule_buffer_pct)
    buffered = apply_capacity_buffer(buffered, capacity_buffer_pct)
    return buffered


def evaluate_candidate(
    instance: VrpInstance,
    buffered_instance: VrpInstance,
    solve_func,
    num_replications: int,
    variability: float,
    distribution: str,
    base_seed: int,
    schedule_buffer_pct: float,
    capacity_buffer_pct: float,
) -> Optional[BufferCandidate]:
    """Solve and simulate one candidate buffer level.

    Args:
        instance: Original instance (used for simulation validation).
        buffered_instance: Instance with buffers applied.
        solve_func: Function to solve VRP (e.g., app.vrp.solve).
        num_replications: Number of simulation replications.
        variability: Lognormal CV or triangular range.
        distribution: "lognormal" or "triangular".
        base_seed: Base random seed.
        schedule_buffer_pct: Schedule buffer percentage (for labeling).
        capacity_buffer_pct: Capacity buffer percentage (for labeling).

    Returns:
        BufferCandidate with solution and simulation results, or None if infeasible.
    """
    try:
        solution = solve_func(buffered_instance)
    except ValueError:
        return None

    if not solution.feasible:
        return None

    sim_results = simulate(
        instance,
        solution,
        num_replications=num_replications,
        variability=variability,
        distribution=distribution,
        seed=base_seed,
    )

    return BufferCandidate(
        schedule_buffer_pct=schedule_buffer_pct,
        capacity_buffer_pct=capacity_buffer_pct,
        solution=solution,
        simulation=sim_results,
        total_distance=solution.total_cost,
        vehicles_used=solution.vehicles_used,
    )


def tune_buffers(
    instance: VrpInstance,
    solve_func,
    schedule_buffer_range: tuple[float, float, float] = (0, 10, 2),
    capacity_buffer_range: tuple[float, float, float] = (0, 5, 1),
    num_replications: int = 50,
    variability: float = 0.1,
    distribution: str = "lognormal",
    base_seed: int = 42,
    target_on_time_rate: float = 0.95,
    objective: str = "maximize_on_time",
) -> BufferTuningResult:
    """Tune schedule/capacity buffers against DES simulation results.

    Uses grid search over buffer levels. Evaluates each candidate via simulation
    with common random numbers for fair comparison.

    Args:
        instance: VRP instance.
        solve_func: Function to solve VRP instances (e.g., app.vrp.solve).
        schedule_buffer_range: (min, max, step) for schedule buffer percentages.
        capacity_buffer_range: (min, max, step) for capacity buffer percentages.
        num_replications: Number of simulation replications per candidate.
        variability: Lognormal CV or triangular range for distribution.
        distribution: "lognormal" or "triangular".
        base_seed: Random seed for reproducibility.
        target_on_time_rate: Target on-time rate for the objective (0..1).
        objective: One of "maximize_on_time" or "minimize_cost_for_target".
            - "maximize_on_time": maximize on-time rate subject to cost.
            - "minimize_cost_for_target": minimize distance/vehicles subject to
              meeting target on-time rate.

    Returns:
        BufferTuningResult with frontier, chosen buffers, and settings.

    Raises:
        ValueError: If solve_func returns infeasible solutions for all candidates.
    """
    if objective not in ("maximize_on_time", "minimize_cost_for_target"):
        raise ValueError(f"unknown objective {objective!r}")

    schedule_min, schedule_max, schedule_step = schedule_buffer_range
    capacity_min, capacity_max, capacity_step = capacity_buffer_range

    frontier = []

    schedule_levels = [
        schedule_min + i * schedule_step
        for i in range(int((schedule_max - schedule_min) / schedule_step) + 1)
    ]
    capacity_levels = [
        capacity_min + i * capacity_step
        for i in range(int((capacity_max - capacity_min) / capacity_step) + 1)
    ]

    for s_buf in schedule_levels:
        for c_buf in capacity_levels:
            buffered = apply_buffers(instance, s_buf, c_buf)
            candidate = evaluate_candidate(
                instance,
                buffered,
                solve_func,
                num_replications,
                variability,
                distribution,
                base_seed,
                s_buf,
                c_buf,
            )
            if candidate is not None:
                frontier.append(candidate)

    if not frontier:
        raise ValueError("No feasible candidate found in buffer grid search")

    if objective == "maximize_on_time":
        chosen = max(frontier, key=lambda c: c.simulation.on_time_rate)
    else:
        candidates_meeting_target = [
            c for c in frontier if c.simulation.on_time_rate >= target_on_time_rate
        ]
        if candidates_meeting_target:
            chosen = min(candidates_meeting_target, key=lambda c: c.total_distance)
        else:
            chosen = max(frontier, key=lambda c: c.simulation.on_time_rate)

    return BufferTuningResult(
        chosen_buffer=chosen,
        frontier=frontier,
        num_replications=num_replications,
        variability=variability,
        distribution=distribution,
        base_seed=base_seed,
        objective=objective,
        target_on_time_rate=target_on_time_rate,
    )
