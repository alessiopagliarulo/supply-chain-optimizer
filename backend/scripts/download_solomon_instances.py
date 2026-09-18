#!/usr/bin/env python3
"""
Regenerate the Solomon instance files in ``backend/app/vrp/data/solomon/``.

Downloads SINTEF TOP's backup of Solomon's 56 100-customer instance files,
checks its sha256, writes each 100-customer file unchanged (``c101.txt``) and
derives the standard 25- and 50-customer versions (``c101_25.txt``,
``c101_50.txt``): the same header and depot with only the first 25 / 50
customer rows, which is how Solomon defines them.

The files are committed, so this is only needed to audit or rebuild them:

    python backend/scripts/download_solomon_instances.py          # rewrite the files
    python backend/scripts/download_solomon_instances.py --check  # verify, write nothing
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

URL = "https://www.sintef.no/globalassets/project/top/vrptw/solomon/solomon-100.zip"
SHA256 = "8a0a72cbe6b7f8f9988ace4ebde0378ec34943acaaac47f2c408915e41887747"
# The SINTEF site answers the default urllib User-Agent with 403.
USER_AGENT = "Mozilla/5.0 (compatible; supply-chain-optimizer solomon fetch)"

DATA_DIR = Path(__file__).resolve().parent.parent / "app" / "vrp" / "data" / "solomon"
# Lines before the depot row: name, blank, VEHICLE, NUMBER CAPACITY, values, blank,
# CUSTOMER, CUST NO. header, blank. The depot row follows, then one row per customer.
HEADER_LINES = 9
SUBSET_SIZES = (25, 50)


def fetch_zip() -> bytes:
    request = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"sha256 mismatch for {URL}: got {digest}, expected {SHA256}")
    return data


def subset(text: bytes, num_customers: int) -> bytes:
    """The first ``num_customers`` customers of a 100-customer file, LF line endings."""
    lines = text.decode("ascii").splitlines()
    return ("\n".join(lines[: HEADER_LINES + 1 + num_customers]) + "\n").encode("ascii")


def build_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(fetch_zip())) as archive:
        for member in sorted(archive.namelist()):
            if not member.lower().endswith(".txt"):
                continue
            stem = Path(member).stem.lower()
            raw = archive.read(member)
            files[f"{stem}.txt"] = raw
            for size in SUBSET_SIZES:
                files[f"{stem}_{size}.txt"] = subset(raw, size)
    if len(files) != 56 * 3:
        raise SystemExit(f"expected 168 instance files, built {len(files)}")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="compare with the committed files; write nothing")
    args = parser.parse_args()

    files = build_files()
    if args.check:
        stale = [
            name
            for name, data in files.items()
            if not (DATA_DIR / name).is_file() or (DATA_DIR / name).read_bytes() != data
        ]
        for name in stale:
            print(f"differs: {name}")
        print(f"{len(files) - len(stale)}/{len(files)} files match {URL}")
        return 1 if stale else 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (DATA_DIR / name).write_bytes(data)
    print(f"wrote {len(files)} files to {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
