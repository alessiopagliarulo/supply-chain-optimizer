"""Tests for Solomon instance loading and benchmarking."""

import json
import pytest

from app.vrp.solomon import (
    BEST_KNOWN_SOLUTIONS,
    get_best_known,
    load_solomon,
    parse_solomon_file,
)
from pathlib import Path


class TestSolomonParsing:
    """Test parsing of Solomon instance files."""

    def test_parse_c101(self):
        """Test parsing the C101 test instance."""
        path = Path(__file__).parent.parent / "app" / "vrp" / "data" / "solomon" / "C101.txt"
        if not path.exists():
            pytest.skip("C101.txt not found")

        coords, nodes, num_vehicles, capacity = parse_solomon_file(path)

        # C101 is a 25-customer instance
        assert len(coords) == 26  # depot + 25 customers
        assert len(nodes) == 26
        assert num_vehicles == 10
        assert capacity == 40

        # Depot
        assert nodes[0].demand == 0
        assert nodes[0].service_time == 0

        # First customer (from our test file)
        assert nodes[1].demand == 10

    def test_load_solomon_c101(self):
        """Test loading C101 as a VrpInstance."""
        path = Path(__file__).parent.parent / "app" / "vrp" / "data" / "solomon" / "C101.txt"
        if not path.exists():
            pytest.skip("C101.txt not found (download instances first)")

        instance = load_solomon("C1", 25)

        assert instance.num_customers == 25
        assert instance.num_vehicles == 10
        assert instance.vehicle_capacity == 40
        assert instance.name == "solomon_C1_25"

        # Check that distance matrix is integer and symmetric
        assert len(instance.distance) == 26
        for row in instance.distance:
            assert len(row) == 26
            for val in row:
                assert isinstance(val, int)

        # Depot to itself should be 0
        assert instance.distance[0][0] == 0

    def test_best_known_solutions(self):
        """Test that best-known solutions are defined for all families."""
        expected_families = [
            ("C1", 25), ("C1", 50), ("C1", 100),
            ("C2", 25), ("C2", 50), ("C2", 100),
            ("R1", 25), ("R1", 50), ("R1", 100),
            ("R2", 25), ("R2", 50), ("R2", 100),
            ("RC1", 25), ("RC1", 50), ("RC1", 100),
            ("RC2", 25), ("RC2", 50), ("RC2", 100),
        ]

        for family, size in expected_families:
            key = (family, size)
            assert key in BEST_KNOWN_SOLUTIONS, f"Best-known missing for {family} {size}"
            vehicles, distance = BEST_KNOWN_SOLUTIONS[key]
            assert vehicles > 0
            assert distance > 0

    def test_get_best_known(self):
        """Test retrieving best-known solutions."""
        # Known value
        result = get_best_known("C1", 100)
        assert result is not None
        vehicles, distance = result
        assert vehicles == 10
        assert distance == 828

        # Unknown value
        result = get_best_known("C1", 999)
        assert result is None

    def test_load_solomon_invalid_family(self):
        """Test that invalid family raises ValueError."""
        with pytest.raises(ValueError, match="unknown Solomon family"):
            load_solomon("X1", 25)

    def test_load_solomon_invalid_size(self):
        """Test that invalid size raises ValueError."""
        with pytest.raises(ValueError, match="num_customers must be"):
            load_solomon("C1", 35)

    def test_load_solomon_missing_file(self):
        """Test that missing instance file raises FileNotFoundError."""
        # These large instances won't exist unless downloaded
        with pytest.raises(FileNotFoundError):
            load_solomon("C1", 100)

    def test_load_solomon_c101_has_correct_properties(self):
        """Test that loaded C101 instance has correct properties."""
        path = Path(__file__).parent.parent / "app" / "vrp" / "data" / "solomon" / "C101.txt"
        if not path.exists():
            pytest.skip("C101.txt not found")

        instance = load_solomon("C1", 25)

        # Verify instance properties
        assert instance.num_customers == 25
        assert instance.num_vehicles == 10
        assert instance.vehicle_capacity == 40
        assert instance.name == "solomon_C1_25"

        # Verify distance matrix is valid
        assert len(instance.distance) == 26
        for row in instance.distance:
            assert len(row) == 26
            for val in row:
                assert isinstance(val, int) and val >= 0

        # Verify travel_time equals distance when not specified
        assert instance.travel_time == instance.distance

    def test_benchmark_results_format(self):
        """Test that benchmark results JSON has correct format."""
        results_file = (
            Path(__file__).parent.parent.parent / "docs" / "benchmark_results.json"
        )
        if not results_file.exists():
            pytest.skip("benchmark_results.json not found (run benchmark first)")

        with open(results_file) as f:
            data = json.load(f)

        # Check provenance
        assert "provenance" in data
        assert "timestamp" in data["provenance"]
        assert "platform" in data["provenance"]
        assert "python_version" in data["provenance"]
        assert "time_limit_seconds" in data["provenance"]
        assert "source" in data["provenance"]

        # Check results
        assert "results" in data
        assert isinstance(data["results"], list)

        for result in data["results"]:
            # Verify required fields
            assert "instance" in result
            assert "family" in result
            assert "num_customers" in result
            assert "solver" in result
            assert "vehicles_used" in result
            assert "total_distance" in result
            assert "gap_percent" in result or result["gap_percent"] is None
            assert "runtime_seconds" in result
            assert "feasible" in result
            assert "solver_status" in result
            assert "best_known_distance" in result
            assert "best_known_vehicles" in result
