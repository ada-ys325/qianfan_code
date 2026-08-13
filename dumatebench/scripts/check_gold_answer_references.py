#!/usr/bin/env python3
"""Report locked tasks that have a gold-answer reference file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DEFAULT_LOCKED_DIR = Path("/data/sx/locked_llm_judge_inputs")
GOLD_REFERENCE = Path("evaluator/gold_answer_reference.json")


def find_tasks(locked_dir: Path) -> tuple[list[Path], list[Path]]:
    """Return task directories split into generated and missing groups."""
    task_dirs = [
        path
        for path in sorted(locked_dir.iterdir(), key=lambda item: item.name)
        if path.is_dir() and (path / "instruction.md").is_file()
    ]
    generated = [path for path in task_dirs if (path / GOLD_REFERENCE).is_file()]
    missing = [path for path in task_dirs if not (path / GOLD_REFERENCE).is_file()]
    return generated, missing


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--locked-dir",
        type=Path,
        default=DEFAULT_LOCKED_DIR,
        help=f"locked task root (default: {DEFAULT_LOCKED_DIR})",
    )
    parser.add_argument(
        "--status",
        choices=("all", "generated", "missing"),
        default="all",
        help="which task groups to print (default: all)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of text",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    locked_dir = args.locked_dir.expanduser()
    if not locked_dir.is_dir():
        print(f"locked task directory does not exist: {locked_dir}", file=sys.stderr)
        return 2

    generated, missing = find_tasks(locked_dir)
    if args.json:
        print(
            json.dumps(
                {
                    "locked_dir": str(locked_dir.resolve()),
                    "reference_file": GOLD_REFERENCE.as_posix(),
                    "total": len(generated) + len(missing),
                    "generated_count": len(generated),
                    "missing_count": len(missing),
                    "generated": [path.name for path in generated],
                    "missing": [path.name for path in missing],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.status in ("all", "generated"):
        print(f"Generated ({len(generated)}):")
        for task_dir in generated:
            print(f"  {task_dir.name}")

    if args.status == "all":
        print()

    if args.status in ("all", "missing"):
        print(f"Missing ({len(missing)}):")
        for task_dir in missing:
            print(f"  {task_dir.name}")

    print(
        f"\nSummary: total={len(generated) + len(missing)}, "
        f"generated={len(generated)}, missing={len(missing)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
