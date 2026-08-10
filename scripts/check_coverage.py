"""Enforce independent line and branch coverage thresholds."""

import argparse
import json
from pathlib import Path


def _percentage(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("--line", type=float, default=85.0)
    parser.add_argument("--branch", type=float, default=80.0)
    args = parser.parse_args()

    document = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    totals = document["totals"]
    line = _percentage(int(totals["covered_lines"]), int(totals["num_statements"]))
    branch = _percentage(int(totals["covered_branches"]), int(totals["num_branches"]))
    print(f"line={line:.2f}% branch={branch:.2f}%")
    return 0 if line >= args.line and branch >= args.branch else 1


if __name__ == "__main__":
    raise SystemExit(main())
