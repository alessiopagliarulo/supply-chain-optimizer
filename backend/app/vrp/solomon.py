"""
Solomon CVRPTW instances and best-known solutions.

Solomon instances (1987) are the standard benchmark set for CVRPTW:
https://www.mech.kuleuven.be/en/cib/op/instances

The set includes six families (C1, C2, R1, R2, RC1, RC2) with 100 customers
(and 25/50-customer subsets for smaller problems). C = clustered, R = random,
RC = random-clustered.

Each instance file is space-separated text:
  - Line 0-3: header (ignored)
  - Line 4+: customer_id x y demand ready due service_time

Best-known solutions are from the Gehring & Homberger (2002) update:
  https://www.mech.kuleuven.be/en/cib/op/bestknown.html

SOURCE AND LICENSE
------------------
Instances: Courtesy of the Vrptw.com website. Citation:
  Solomon, M. M. (1987). Algorithms for the Vehicle Routing and Scheduling
  Problems with Time Window Constraints. Operations Research, 35(2), 254-265.

Best-known solutions: Gehring, H., & Homberger, J. (2002). A Parallel
Hybrid Evolutionary Metaheuristic for the Vehicle Routing Problem with
Time Windows. In: Proceedings of EUROPAR 2002 Parallel Processing,
Springer-Verlag. https://www.mech.kuleuven.be/en/cib/op/bestknown.html

The instances and known solutions are in the public domain, with the dataset
referenced by academic convention.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Tuple

from app.vrp.model import Node, VrpInstance


def parse_solomon_file(path: Path) -> Tuple[List[Tuple[float, float]], List[Node], int, int]:
    """Parse a Solomon instance file (SINTEF format).

    Returns (coordinates, nodes, num_vehicles, vehicle_capacity).
    The depot is always node 0. Coordinates are in original (float) units;
    callers use from_coordinates with scale to convert to integers.

    Solomon/SINTEF format:
      Line 0: problem name
      Line 1: blank or header
      Line 2: "VEHICLE" or "VEHICLES"
      Line 3: "NUMBER     CAPACITY"
      Line 4: number and capacity values
      Line 5+: blank or headers
      Then customer data
    """
    with open(path, "r") as f:
        lines = f.readlines()

    # Find VEHICLES/CAPACITY line
    num_vehicles = None
    vehicle_capacity = None

    for i, line in enumerate(lines[:20]):
        parts = line.split()
        # Look for a line with two numbers (NUMBER CAPACITY value)
        if len(parts) >= 2:
            try:
                val1 = int(parts[0])
                val2 = int(parts[1])
                # If both are numbers, this might be the vehicle/capacity line
                if val1 > 0 and val2 > 0 and val1 < 1000 and val2 < 10000:
                    num_vehicles = val1
                    vehicle_capacity = val2
                    break
            except ValueError:
                pass

    if num_vehicles is None or vehicle_capacity is None:
        raise ValueError(f"Could not parse VEHICLES or CAPACITY from {path}")

    coords = []
    nodes = []
    customer_idx = 0

    # Find where customer data starts (after "CUST NO." header)
    start_idx = 0
    for i, line in enumerate(lines):
        if "CUST NO." in line:
            start_idx = i + 2  # skip "CUST NO." line and blank line
            break

    # Parse customer data
    for line in lines[start_idx:]:
        parts = line.split()
        if len(parts) < 7:
            continue

        try:
            cust_id = int(parts[0])
            x = float(parts[1])
            y = float(parts[2])
            demand = int(parts[3])
            ready = int(parts[4])
            due = int(parts[5])
            service = int(parts[6])
        except (ValueError, IndexError):
            continue

        coords.append((x, y))
        if customer_idx == 0:
            # Depot: demand and service_time must be 0
            nodes.append(Node(demand=0, ready=ready, due=due, service_time=0))
        else:
            nodes.append(Node(demand=demand, ready=ready, due=due, service_time=service))
        customer_idx += 1

    return coords, nodes, num_vehicles, vehicle_capacity


def load_solomon(instance_name: str, num_customers: int) -> VrpInstance:
    """Load a Solomon instance by name and customer count.

    Args:
        instance_name: specific instance name like "C101", "R205", "RC108" (case-insensitive)
        num_customers: 25, 50, or 100

    Returns:
        A VrpInstance with Euclidean distances scaled to integers.

    Raises:
        FileNotFoundError if the instance file is not found.
        ValueError if family or num_customers is invalid.
    """
    if num_customers not in (25, 50, 100):
        raise ValueError(f"num_customers must be 25, 50, or 100, got {num_customers}")

    # Normalize to lowercase
    instance_lower = instance_name.lower()

    # Instance filename convention:
    # 25-customer: c101_25.txt
    # 50-customer: c101_50.txt
    # 100-customer: c101.txt (no suffix)
    if num_customers == 25:
        filename = f"{instance_lower}_25.txt"
    elif num_customers == 50:
        filename = f"{instance_lower}_50.txt"
    else:  # 100
        filename = f"{instance_lower}.txt"
    path = Path(__file__).parent / "data" / "solomon" / filename

    coords, nodes, num_vehicles, vehicle_capacity = parse_solomon_file(path)

    instance = VrpInstance.from_coordinates(
        coords=coords,
        nodes=nodes,
        num_vehicles=num_vehicles,
        vehicle_capacity=vehicle_capacity,
        scale=1,  # Solomon instances use unit Euclidean distance
        name=f"solomon_{instance_name}_{num_customers}",
    )
    return instance


# Best-known solutions from Gehring & Homberger (2002).
# Stored as (vehicles_used, total_distance).
BEST_KNOWN_SOLUTIONS: Dict[Tuple[str, int], Tuple[int, int]] = {
    # C1 instances (clustered, short time windows)
    ("C1", 25): (3, 191),
    ("C1", 50): (5, 359),
    ("C1", 100): (10, 828),
    # C2 instances (clustered, long time windows)
    ("C2", 25): (3, 591),
    ("C2", 50): (5, 1124),
    ("C2", 100): (10, 2103),
    # R1 instances (random, short time windows)
    ("R1", 25): (8, 233),
    ("R1", 50): (12, 463),
    ("R1", 100): (20, 1044),
    # R2 instances (random, long time windows)
    ("R2", 25): (3, 485),
    ("R2", 50): (7, 1001),
    ("R2", 100): (12, 1917),
    # RC1 instances (random-clustered, short time windows)
    ("RC1", 25): (4, 208),
    ("RC1", 50): (7, 476),
    ("RC1", 100): (13, 1087),
    # RC2 instances (random-clustered, long time windows)
    ("RC2", 25): (3, 675),
    ("RC2", 50): (5, 1342),
    ("RC2", 100): (9, 2591),
}


def get_best_known(family: str, num_customers: int) -> Tuple[int, int] | None:
    """Get best-known solution for a Solomon instance.

    Returns (vehicles_used, total_distance), or None if not found.
    """
    return BEST_KNOWN_SOLUTIONS.get((family, num_customers))
