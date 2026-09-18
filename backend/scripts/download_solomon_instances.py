#!/usr/bin/env python3
"""
Download Solomon CVRPTW benchmark instances.

This script downloads the standard Solomon CVRPTW instance set from the
Gehring & Homberger repository. Six families (C1, C2, R1, R2, RC1, RC2)
with 100 customers each, plus 25- and 50-customer subsets.

Usage:
    python backend/scripts/download_solomon_instances.py
"""

import urllib.request
from pathlib import Path
import sys

# Solomon instances are hosted at the Vrptw.com repository maintained by
# Gehring & Homberger. We download from the standard academic source.
BASE_URL = "https://www.mech.kuleuven.be/en/cib/op/data/text/"

FAMILIES = ["C1", "C2", "R1", "R2", "RC1", "RC2"]
SIZES = [25, 50, 100]

INSTANCES = [
    # C family: 25 customers in C101.txt, 50 in C1_50.txt, 100 in C1_100.txt
    *[(f, size) for f in FAMILIES for size in SIZES],
]


def download_instance(family: str, size: int, output_dir: Path) -> bool:
    """Download a single instance file."""
    # File naming convention: C101.txt for C1/25, C1_50.txt for C1/50, C1_100.txt for C1/100
    if size == 25:
        filename = f"{family}01.txt"
    else:
        filename = f"{family}_{size}.txt"

    url = f"{BASE_URL}{filename}"
    output_file = output_dir / filename

    if output_file.exists():
        print(f"✓ {filename} already exists")
        return True

    print(f"Downloading {filename}...", end=" ", flush=True)
    try:
        urllib.request.urlretrieve(url, output_file)
        print("✓")
        return True
    except urllib.error.URLError as e:
        print(f"✗ failed: {e}")
        return False


def main():
    data_dir = Path(__file__).parent.parent / "app" / "vrp" / "data" / "solomon"
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading Solomon instances to {data_dir}")
    print()

    failed = []
    for family in FAMILIES:
        print(f"{family} family:")
        for size in SIZES:
            if not download_instance(family, size, data_dir):
                failed.append((family, size))
        print()

    if failed:
        print(f"Failed to download {len(failed)} instance(s):")
        for family, size in failed:
            print(f"  - {family} ({size} customers)")
        sys.exit(1)
    else:
        print("✓ All instances downloaded successfully")


if __name__ == "__main__":
    main()
