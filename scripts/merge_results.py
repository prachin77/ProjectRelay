"""Merge one exported run into the canonical ``logs/results.csv``.

WHY THIS SCRIPT EXISTS
----------------------
The real Groq quality run and the stand-in runs are executed separately: the
real run must live in its own log directory so it never mixes traces with the
simulated runs. The brief requires **one** ``results.csv`` containing one row
per request, so the two raw files are merged here.

CONTRACT
--------
* The target file must already exist with the exact required header.
* The source file must have the same header.
* ``request_id`` is the identity of a row. A request that already exists is a
  hard error, never silently overwritten -- the real and stand-in runs use
  different prefixes (``q-...`` vs ``s-...``), so a collision means a mistake.
* Rows are appended in source order. Nothing is recomputed or edited.

Usage
-----
    python scripts/merge_results.py <source.csv> <target.csv>

Run this **last**, after every experiment, then re-run
``python -m the_relay.main metrics --export`` so the root ``results.csv`` is the
merged file.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from the_relay.obs.results_csv import RESULTS_COLUMNS, read_results  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-3], file=sys.stderr)
        print("usage: merge_results.py <source.csv> <target.csv>", file=sys.stderr)
        return 2

    source = Path(argv[1])
    target = Path(argv[2])
    if not source.exists():
        print(f"source does not exist: {source}", file=sys.stderr)
        return 2
    if not target.exists():
        print(f"target does not exist: {target}", file=sys.stderr)
        return 2

    for path in (source, target):
        with path.open(encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle))
        if tuple(header) != RESULTS_COLUMNS:
            print(f"{path}: header does not match the required columns", file=sys.stderr)
            return 2

    existing = read_results(target)
    incoming = read_results(source)
    seen = {row["request_id"] for row in existing}
    duplicates = [row["request_id"] for row in incoming if row["request_id"] in seen]
    if duplicates:
        print(
            "refusing to merge: these request_ids already exist: "
            + ", ".join(duplicates[:10]),
            file=sys.stderr,
        )
        return 1

    with target.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULTS_COLUMNS, extrasaction="raise")
        for row in incoming:
            writer.writerow(row)

    print(
        f"merged {len(incoming)} rows from {source.name} into {target} "
        f"({len(existing) + len(incoming)} total)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
