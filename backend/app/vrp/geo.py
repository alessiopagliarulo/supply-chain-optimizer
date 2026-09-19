"""
Distances between real places: great-circle distance times a road factor.

The routing engine works on integer matrices (``app/vrp/model.py``). For real
places we do not call a road-routing service; we take the great-circle
(haversine) distance between two latitude/longitude points and multiply it by
:data:`ROAD_FACTOR` to approximate the extra length of the road network.

WHY 1.3
-------
Roads are never straight, so the driven distance between two points is longer
than the straight line. The ratio of the two is called the *circuity* or
*detour* factor. Published measurements put it at roughly 1.2 to 1.4 for road
travel: Ballou, Rahardja and Sakai, "Selected country circuity factors for road
travel distance estimation", Transportation Research Part A 36(9), 2002, report
country factors in that band, and Boscoe, Henry and Zdeb, "A nationwide
comparison of driving distance versus straight-line distance to hospitals",
The Professional Geographer 64(2), 2012, measured about 1.4 across the United
States (higher for short trips). 1.3 is the middle of that band. It is an
average, so any single leg can be shorter or longer by road; a later task can
swap in a real road-distance service behind :func:`road_km` without touching
the solvers.

The Earth radius is the IUGG mean radius, 6371.0088 km.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

#: Mean Earth radius in kilometres (IUGG).
EARTH_RADIUS_KM = 6371.0088

#: Driven distance / straight-line distance. See the module docstring.
ROAD_FACTOR = 1.3

ROAD_FACTOR_RATIONALE = (
    "Roads are never straight, so every straight-line (great-circle) distance is "
    "multiplied by 1.3 to approximate the driven distance. Published road circuity "
    "studies measure about 1.2 to 1.4 (Ballou, Rahardja and Sakai 2002; Boscoe, Henry "
    "and Zdeb 2012); 1.3 is the middle of that band. It is an average: a single leg "
    "can be shorter or longer by road."
)

LatLng = Tuple[float, float]


def great_circle_km(a: LatLng, b: LatLng) -> float:
    """Haversine distance in kilometres between two (latitude, longitude) points."""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def road_km(a: LatLng, b: LatLng, road_factor: float = ROAD_FACTOR) -> float:
    """Estimated driving distance in kilometres: great-circle distance times the road factor."""
    return great_circle_km(a, b) * road_factor


def road_distance_matrix_m(points: Sequence[LatLng], road_factor: float = ROAD_FACTOR) -> List[List[int]]:
    """Integer metres between every pair of points, for the solvers' integer model."""
    return [[int(round(road_km(a, b, road_factor) * 1000)) for b in points] for a in points]


def travel_time_matrix_min(distance_m: Sequence[Sequence[int]], speed_kmh: float) -> List[List[int]]:
    """Whole minutes to drive each entry of a metre matrix at a constant average speed."""
    if speed_kmh <= 0:
        raise ValueError("speed_kmh must be positive")
    metres_per_minute = speed_kmh * 1000 / 60
    return [[int(math.ceil(d / metres_per_minute)) for d in row] for row in distance_m]
