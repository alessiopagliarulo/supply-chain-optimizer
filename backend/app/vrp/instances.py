"""
The named CVRPTW instances the web app can load: built-in samples plus any
Solomon benchmark files present in the checkout.

Two directories, one text format (Solomon's):

* ``data/samples/`` - shipped with the app so the Route Plan page always has
  something real to solve. See ``data/samples/README.md`` for provenance.
* ``data/solomon/`` - the benchmark instance files, when they are present.
  Listed if the directory exists; absent is not an error.

An instance's id is ``<directory>/<file stem>`` (e.g. ``sample/C101_25``), so
the same file name in both directories cannot collide.

Coordinates stay in the file's planar units. Distances are built by
:meth:`VrpInstance.from_coordinates` with ``scale=1`` (rounded Euclidean), the
same convention the time windows are written in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from app.vrp.model import Node

DATA_DIR = Path(__file__).resolve().parent / "data"

#: (id prefix, directory, human label). Order is listing order.
SOURCES: Tuple[Tuple[str, Path, str], ...] = (
    ("sample", DATA_DIR / "samples", "Built-in sample"),
    ("solomon", DATA_DIR / "solomon", "Solomon benchmark"),
)

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class NamedInstance:
    """One instance as the app serves it: coordinates, nodes and fleet."""

    id: str
    name: str
    source: str
    num_vehicles: int
    vehicle_capacity: int
    coords: List[Tuple[float, float]]
    nodes: List[Node]

    @property
    def num_customers(self) -> int:
        return len(self.nodes) - 1


def _ints_after(tokens: List[str], keyword: str) -> Optional[int]:
    for i, tok in enumerate(tokens[:-1]):
        if tok.upper() == keyword:
            try:
                return int(tokens[i + 1])
            except ValueError:
                return None
    return None


def parse_solomon_text(text: str) -> Tuple[str, int, int, List[Tuple[float, float]], List[Node]]:
    """Parse a Solomon-format instance.

    Returns ``(name, num_vehicles, vehicle_capacity, coords, nodes)``, depot first.

    The fleet is read from the standard header (a ``NUMBER CAPACITY`` line
    followed by the two values), or from ``VEHICLES n`` / ``CAPACITY n`` lines.
    Every line of exactly seven numbers is a node row:
    ``id x y demand ready due service``.
    """
    lines = [line.strip() for line in text.splitlines()]
    name = next((line for line in lines if line), "")
    num_vehicles: Optional[int] = None
    capacity: Optional[int] = None
    coords: List[Tuple[float, float]] = []
    nodes: List[Node] = []

    for i, line in enumerate(lines):
        tokens = line.split()
        upper = [t.upper() for t in tokens]
        if upper[:2] == ["NUMBER", "CAPACITY"] and i + 1 < len(lines):
            values = lines[i + 1].split()
            if len(values) == 2:
                num_vehicles, capacity = int(values[0]), int(values[1])
            continue
        if "VEHICLES" in upper and num_vehicles is None:
            num_vehicles = _ints_after(tokens, "VEHICLES")
        if "CAPACITY" in upper and capacity is None and "NUMBER" not in upper:
            capacity = _ints_after(tokens, "CAPACITY")
        if len(tokens) == 7:
            try:
                values7 = [float(t) for t in tokens]
            except ValueError:
                continue
            _, x, y, demand, ready, due, service = values7
            coords.append((x, y))
            nodes.append(Node(demand=int(demand), ready=int(ready), due=int(due), service_time=int(service)))

    if num_vehicles is None or capacity is None:
        raise ValueError("no vehicle number / capacity header found")
    if not nodes:
        raise ValueError("no node rows found")
    return name, num_vehicles, capacity, coords, nodes


def _load_file(prefix: str, label: str, path: Path) -> NamedInstance:
    name, num_vehicles, capacity, coords, nodes = parse_solomon_text(path.read_text(encoding="utf-8"))
    return NamedInstance(
        id=f"{prefix}/{path.stem}",
        name=path.stem.replace("_", ".") if prefix == "sample" else (name or path.stem),
        source=label,
        num_vehicles=num_vehicles,
        vehicle_capacity=capacity,
        coords=coords,
        nodes=nodes,
    )


def list_instances() -> List[NamedInstance]:
    """Every loadable instance, samples first. Unparseable files are left out."""
    out: List[NamedInstance] = []
    for prefix, directory, label in SOURCES:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.txt")):
            try:
                out.append(_load_file(prefix, label, path))
            except (OSError, ValueError):
                continue
    return out


def load_instance(instance_id: str) -> NamedInstance:
    """The instance with ``instance_id``. Raises ``KeyError`` if there is none."""
    prefix, _, stem = instance_id.partition("/")
    for source_prefix, directory, label in SOURCES:
        if source_prefix != prefix or not _ID_RE.match(stem):
            continue
        path = directory / f"{stem}.txt"
        if path.is_file():
            try:
                return _load_file(prefix, label, path)
            except ValueError as exc:
                raise KeyError(instance_id) from exc
    raise KeyError(instance_id)
