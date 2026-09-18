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

from pathlib import Path
from typing import Dict, List, Tuple

from app.vrp.model import Node, VrpInstance


def parse_solomon_file(path: Path) -> Tuple[List[Tuple[float, float]], List[Node], int, int]:
    """Parse a Solomon instance file.

    Returns (coordinates, nodes, num_vehicles, vehicle_capacity).
    The depot is always node 0. Coordinates are in original (float) units;
    callers use from_coordinates with scale to convert to integers.

    Solomon format:
      Line 0: problem name
      Line 1-3: header lines (may contain VEHICLES and CAPACITY)
      Line 4+: customer data (customer_id x y demand ready due service)
    """
    with open(path) as f:
        lines = f.read().strip().split("\n")

    # Find VEHICLES and CAPACITY in header lines
    num_vehicles = None
    vehicle_capacity = None

    for line in lines[:4]:
        if "VEHICLES" in line.upper():
            parts = line.split()
            for i, p in enumerate(parts):
                if p.upper() == "VEHICLES" and i + 1 < len(parts):
                    try:
                        num_vehicles = int(parts[i + 1])
                    except ValueError:
                        pass
        if "CAPACITY" in line.upper():
            parts = line.split()
            for i, p in enumerate(parts):
                if p.upper() == "CAPACITY" and i + 1 < len(parts):
                    try:
                        vehicle_capacity = int(parts[i + 1])
                    except ValueError:
                        pass

    if num_vehicles is None or vehicle_capacity is None:
        raise ValueError(f"Could not parse VEHICLES or CAPACITY from {path}")

    coords = []
    nodes = []

    for i, line in enumerate(lines[4:]):
        fields = line.split()
        if len(fields) < 7:
            continue

        try:
            x = float(fields[1])
            y = float(fields[2])
            demand = int(fields[3])
            ready = int(fields[4])
            due = int(fields[5])
            service = int(fields[6])
        except (ValueError, IndexError):
            continue

        coords.append((x, y))
        if i == 0:
            # Depot: demand and service_time must be 0
            nodes.append(Node(demand=0, ready=ready, due=due, service_time=0))
        else:
            nodes.append(Node(demand=demand, ready=ready, due=due, service_time=service))

    return coords, nodes, num_vehicles, vehicle_capacity


def load_solomon(family: str, num_customers: int) -> VrpInstance:
    """Load a Solomon instance by family and customer count.

    Args:
        family: one of "C1", "C2", "R1", "R2", "RC1", "RC2"
        num_customers: 25, 50, or 100

    Returns:
        A VrpInstance with Euclidean distances scaled to integers.

    Raises:
        FileNotFoundError if the instance file is not found.
        ValueError if family or num_customers is invalid.
    """
    if family not in ("C1", "C2", "R1", "R2", "RC1", "RC2"):
        raise ValueError(f"unknown Solomon family {family!r}")
    if num_customers not in (25, 50, 100):
        raise ValueError(f"num_customers must be 25, 50, or 100, got {num_customers}")

    # Instance filename: C101.txt for C1/25, C1_50.txt for C1/50, C1_100.txt for C1/100
    # For 25-customer instances, the convention is C101, C201, R101, etc. (not C1.txt)
    if num_customers == 25:
        # 25-customer instances: C1 -> C101, C2 -> C201, R1 -> R101, etc.
        instance_id = family + "01"
    else:
        # 50 and 100-customer instances: C1_50, C1_100, etc.
        instance_id = f"{family}_{num_customers}"

    filename = f"{instance_id}.txt"
    path = Path(__file__).parent / "data" / "solomon" / filename

    coords, nodes, num_vehicles, vehicle_capacity = parse_solomon_file(path)

    instance = VrpInstance.from_coordinates(
        coords=coords,
        nodes=nodes,
        num_vehicles=num_vehicles,
        vehicle_capacity=vehicle_capacity,
        scale=1,  # Solomon instances use unit Euclidean distance
        name=f"solomon_{family}_{num_customers}",
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
