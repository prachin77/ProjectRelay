"""Build a submission zip that can never contain the API key.

WHY THIS EXISTS
---------------
The brief says: "Do not include your key in your submission." The key lives in
`.env`, which git already ignores. But a zip made by right-clicking the folder
would include it, and one careless upload ends the submission. This script builds
the zip from an allow-by-default walk with explicit exclusions, and then *reads
every file back* looking for a Groq key pattern. If it finds one, it refuses to
finish.

WHAT IS EXCLUDED, AND WHY
-------------------------
`.env*`            the key, and any local overrides
`.venv/`           installed packages; reproducible from requirements.txt
`__pycache__/`     byte-code, machine specific
`indexes/`         rebuilt by `python -m the_relay.main ingest`
`dist/`            this script's own output
`scratch/`, `*.db`, `*.sqlite`   local experiments, not evidence

WHAT IS KEPT
------------
All source, tests, config, the three PDFs, `results.csv` (root and `logs/`), and
the `logs/*.jsonl` traces that back Table 1. Evidence reviewers may want is
evidence we ship.

USAGE
-----
    .\\.venv\\Scripts\\python.exe scripts/package_submission.py
    .\\.venv\\Scripts\\python.exe scripts/package_submission.py --output dist/x.zip
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories never shipped. Names are matched at any depth.
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "indexes",
    "scratch",
    "dist",
}

# Exact file names never shipped.
EXCLUDE_FILES = {".env"}

# Suffixes that are machine state, not evidence.
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".rdb", ".aof"}

# A Groq key looks like gsk_ followed by a long token. Matching bytes keeps this
# cheap even on the PDFs.
KEY_PATTERN = re.compile(rb"gsk_[A-Za-z0-9]{10,}")

# Files every submission must contain, checked at the end.
REQUIRED = [
    "README.md",
    "results.csv",
    "logs/results.csv",
    "reports/REPORT.md",
    "the_relay/main.py",
    "requirements.txt",
]


def should_skip(rel: Path) -> bool:
    """True when this path must not enter the zip."""
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    # .env.example is documentation and must ship; every other .env* is local.
    if rel.name.startswith(".env") and rel.name != ".env.example":
        return True
    return rel.suffix in EXCLUDE_SUFFIXES


def build(output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)

    shipped: list[tuple[str, int]] = []
    withheld: list[str] = []
    leaks: list[str] = []

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(ROOT)
            if should_skip(rel):
                if rel.name.startswith(".env") or rel.name in EXCLUDE_FILES:
                    withheld.append(rel.as_posix())
                continue

            data = path.read_bytes()
            if KEY_PATTERN.search(data):
                leaks.append(rel.as_posix())
                continue

            zf.writestr(rel.as_posix(), data)
            shipped.append((rel.as_posix(), len(data)))

    names = {name for name, _ in shipped}
    missing = [name for name in REQUIRED if name not in names]
    total_mb = sum(size for _, size in shipped) / 1_048_576

    print(f"wrote {output}")
    print(f"files: {len(shipped)}  size: {total_mb:.1f} MB")
    print(f"withheld secrets/local files: {withheld or 'none found'}")

    if leaks:
        output.unlink(missing_ok=True)
        print("REFUSING TO SHIP: key pattern found in:")
        for name in leaks:
            print(f"  {name}")
        return 1

    if missing:
        print("WARNING: required files missing from the zip:")
        for name in missing:
            print(f"  {name}")
        return 1

    print("checked: no Groq key pattern in any shipped file")
    print("checked: required files present")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "the-relay-submission.zip",
        help="where to write the zip (default: dist/the-relay-submission.zip)",
    )
    args = parser.parse_args()
    return build(args.output)


if __name__ == "__main__":
    sys.exit(main())
