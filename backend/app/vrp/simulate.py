"""Discrete-event simulation of vehicle routes with random travel and service times.

Simulates a VRP solution by running each vehicle through its route with randomized
travel and service times drawn from configurable distributions. Tracks KPIs useful
for assessing solution robustness and tuning schedule buffers.

CONVENTIONS
-----------
All times are in the same units as the VRP instance (typically minutes for service
times and travel times). Seeds ensure reproducibility; zero variability reproduces
the deterministic plan exactly.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import simpy

from app.vrp.model import VrpInstance, VrpSolution


DistributionFactory = Callable[[random.Random], Callable[[float], float]]


def lognormal_factory(variability: float) -> DistributionFactory:
    """Create a lognormal distribution factory around a nominal time.

    Args:
        variability: coefficient of variation (CV = std / mean). Higher means more variation.

    Returns:
        A function that takes a random.Random and returns a sampler for the distribution.
        The sampler(nominal) returns a sample from lognormal(mu, sigma) where mu, sigma
        are set so the resulting lognormal has the desired CV.
    """

    def factory(rng: random.Random) -> Callable[[float], float]:
        def sampler(nominal: float) -> float:
            if nominal <= 0:
                return nominal
            if variability == 0:
                return nominal
            cv = variability
            sigma = np.sqrt(np.log(1 + cv**2))
            mu = np.log(nominal) - 0.5 * sigma**2
            return float(np.exp(rng.gauss(mu, sigma)))
        return sampler

    return factory


def triangular_factory(variability: float) -> DistributionFactory:
    """Create a triangular distribution factory around a nominal time.

    Args:
        variability: controls the range [nominal * (1 - variability), nominal * (1 + variability)].

    Returns:
        A function that takes a random.Random and returns a sampler for the distribution.
    """

    def factory(rng: random.Random) -> Callable[[float], float]:
        def sampler(nominal: float) -> float:
            if nominal <= 0 or variability == 0:
                return nominal
            low = nominal * (1 - variability)
            high = nominal * (1 + variability)
            return float(rng.triangular(low, nominal, high))
        return sampler

    return factory


@dataclass
class SimulationRun:
    """Results from one replication of the simulation."""

    run_id: int
    vehicle_routes: list[list[int]]
    on_time_per_customer: dict[int, bool] = field(default_factory=dict)
    lateness_per_customer: dict[int, float] = field(default_factory=dict)
    vehicle_completion_times: list[float] = field(default_factory=list)
    vehicle_utilization: list[float] = field(default_factory=list)
    actual_travel_times: dict[tuple[int, int], float] = field(default_factory=dict)
    actual_service_times: dict[int, float] = field(default_factory=dict)


@dataclass
class SimulationResults:
    """Aggregated KPIs across all replications."""

    num_replications: int
    instance_name: str
    on_time_rate: float
    on_time_rate_per_customer: dict[int, float] = field(default_factory=dict)
    mean_lateness: float = 0.0
    p95_lateness: float = 0.0
    mean_route_completion_time: float = 0.0
    max_route_completion_time: float = 0.0
    vehicle_utilization_mean: float = 0.0
    vehicle_utilization_min: float = 0.0
    runs: list[SimulationRun] = field(default_factory=list)


class VehicleSimulation:
    """Simulates one vehicle executing its assigned route."""

    def __init__(
        self,
        env: simpy.Environment,
        vehicle_id: int,
        route: list[int],
        instance: VrpInstance,
        travel_time_sampler: Callable[[float], float],
        service_time_sampler: Callable[[float], float],
    ):
        self.env = env
        self.vehicle_id = vehicle_id
        self.route = route
        self.instance = instance
        self.travel_time_sampler = travel_time_sampler
        self.service_time_sampler = service_time_sampler

        self.on_time_per_customer: dict[int, bool] = {}
        self.lateness_per_customer: dict[int, float] = {}
        self.actual_travel_times: dict[tuple[int, int], float] = {}
        self.actual_service_times: dict[int, float] = {}
        self.load = 0
        self.depart_time: Optional[float] = None
        self.return_time: Optional[float] = None

    def run(self):
        """Execute the vehicle's route."""
        depot = self.instance.nodes[0]
        self.depart_time = max(self.env.now, float(depot.ready))

        if self.depart_time > self.env.now:
            yield self.env.timeout(self.depart_time - self.env.now)

        current = 0

        for customer in self.route:
            nominal_travel = float(self.instance.travel_time[current][customer])
            actual_travel = self.travel_time_sampler(nominal_travel)
            self.actual_travel_times[(current, customer)] = actual_travel

            yield self.env.timeout(actual_travel)

            arrival_time = self.env.now
            node = self.instance.nodes[customer]
            wait_time = max(0, float(node.ready) - arrival_time)

            if wait_time > 0:
                yield self.env.timeout(wait_time)

            nominal_service = float(node.service_time)
            actual_service = self.service_time_sampler(nominal_service)
            self.actual_service_times[customer] = actual_service

            yield self.env.timeout(actual_service)

            service_end = self.env.now
            lateness = max(0, service_end - float(node.due))
            self.on_time_per_customer[customer] = lateness <= 0
            self.lateness_per_customer[customer] = lateness

            current = customer

        nominal_travel_back = float(self.instance.travel_time[current][0])
        actual_travel_back = self.travel_time_sampler(nominal_travel_back)
        self.actual_travel_times[(current, 0)] = actual_travel_back

        yield self.env.timeout(actual_travel_back)

        self.return_time = self.env.now


def simulate_once(
    instance: VrpInstance,
    solution: VrpSolution,
    variability: float = 0.1,
    distribution: str = "lognormal",
    seed: Optional[int] = None,
    run_id: int = 0,
) -> SimulationRun:
    """Run one replication of the simulation.

    Args:
        instance: VRP problem instance.
        solution: VRP solution with routes.
        variability: distribution parameter (CV for lognormal, range for triangular).
        distribution: "lognormal" or "triangular".
        seed: Random seed for reproducibility. If None, uses current state.
        run_id: Identifier for this run.

    Returns:
        SimulationRun with KPIs for this replication.
    """
    if seed is not None:
        rng = random.Random(seed)
    else:
        rng = random.Random()

    if distribution == "lognormal":
        dist_factory = lognormal_factory(variability)
    elif distribution == "triangular":
        dist_factory = triangular_factory(variability)
    else:
        raise ValueError(f"unknown distribution: {distribution}")

    travel_time_sampler = dist_factory(rng)
    service_time_sampler = dist_factory(rng)

    env = simpy.Environment()
    vehicles = []

    for vehicle_id, route in enumerate(solution.routes):
        v = VehicleSimulation(
            env,
            vehicle_id,
            route,
            instance,
            travel_time_sampler,
            service_time_sampler,
        )
        vehicles.append(v)
        env.process(v.run())

    env.run()

    on_time_per_customer = {}
    lateness_per_customer = {}
    completion_times = []

    for vehicle in vehicles:
        on_time_per_customer.update(vehicle.on_time_per_customer)
        lateness_per_customer.update(vehicle.lateness_per_customer)
        if vehicle.return_time is not None:
            completion_times.append(vehicle.return_time)

    result = SimulationRun(
        run_id=run_id,
        vehicle_routes=solution.routes,
        on_time_per_customer=on_time_per_customer,
        lateness_per_customer=lateness_per_customer,
        vehicle_completion_times=completion_times,
        actual_travel_times={},
        actual_service_times={},
    )

    for vehicle in vehicles:
        result.actual_travel_times.update(vehicle.actual_travel_times)
        result.actual_service_times.update(vehicle.actual_service_times)

        if vehicle.route:
            utilization = sum(
                float(instance.nodes[c].service_time) for c in vehicle.route
            ) / (vehicle.return_time - vehicle.depart_time) if vehicle.return_time and vehicle.depart_time else 0
            result.vehicle_utilization.append(utilization)

    return result


def simulate(
    instance: VrpInstance,
    solution: VrpSolution,
    num_replications: int = 100,
    variability: float = 0.1,
    distribution: str = "lognormal",
    seed: Optional[int] = None,
) -> SimulationResults:
    """Run multiple replications and aggregate results.

    Args:
        instance: VRP problem instance.
        solution: VRP solution with routes.
        num_replications: Number of simulation runs.
        variability: Distribution parameter (CV for lognormal, range for triangular).
        distribution: "lognormal" or "triangular".
        seed: Random seed. If provided, runs are seeded as seed, seed+1, seed+2, etc.

    Returns:
        SimulationResults with aggregated KPIs.
    """
    if num_replications < 1:
        raise ValueError("num_replications must be >= 1")

    runs = []

    for i in range(num_replications):
        run_seed = None if seed is None else seed + i
        run = simulate_once(instance, solution, variability, distribution, run_seed, i)
        runs.append(run)

    all_on_time = [r.on_time_per_customer.get(c, False) for r in runs for c in instance.customers]
    on_time_rate = sum(all_on_time) / len(all_on_time) if all_on_time else 1.0

    on_time_rate_per_customer = {}
    for customer in instance.customers:
        customer_on_time = [r.on_time_per_customer.get(customer, False) for r in runs]
        if customer_on_time:
            on_time_rate_per_customer[customer] = sum(customer_on_time) / len(customer_on_time)

    all_lateness = [r.lateness_per_customer.get(c, 0) for r in runs for c in instance.customers]
    mean_lateness = statistics.mean(all_lateness) if all_lateness else 0.0
    p95_lateness = np.percentile(all_lateness, 95) if all_lateness else 0.0

    all_completion_times = [t for r in runs for t in r.vehicle_completion_times]
    mean_completion_time = statistics.mean(all_completion_times) if all_completion_times else 0.0
    max_completion_time = max(all_completion_times) if all_completion_times else 0.0

    all_utilizations = [u for r in runs for u in r.vehicle_utilization]
    utilization_mean = statistics.mean(all_utilizations) if all_utilizations else 0.0
    utilization_min = min(all_utilizations) if all_utilizations else 0.0

    return SimulationResults(
        num_replications=num_replications,
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
