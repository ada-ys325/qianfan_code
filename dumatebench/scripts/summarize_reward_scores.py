#!/usr/bin/env python3
"""Recompute equal-weight checklist scores from reward.json files."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_REWARD_FILE = "run_outputs/reward.json"


@dataclass
class RewardScore:
    task_id: str
    run_id: str
    task_dir: str
    reward_file: str
    complete_pass: int
    partial_pass: float
    passed_items: int
    total_items: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "task_dir": self.task_dir,
            "reward_file": self.reward_file,
            "complete_pass": self.complete_pass,
            "partial_pass": self.partial_pass,
            "passed_items": self.passed_items,
            "total_items": self.total_items,
        }


@dataclass
class ScoreCollection:
    scores: list[RewardScore]
    invalid_file_count: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute complete_pass and partial_pass using only checks[].passed "
            "from reward.json. Every item within a task has equal weight."
        )
    )
    parser.add_argument("runs_dir", help="Runs root containing reward.json files.")
    parser.add_argument(
        "--reward-file",
        default=DEFAULT_REWARD_FILE,
        help="Reward path relative to each task directory.",
    )
    parser.add_argument(
        "--dir-glob",
        default=None,
        help="Only scan direct child directories matching this glob.",
    )
    parser.add_argument(
        "--format",
        choices=("tsv", "csv", "jsonl"),
        default="tsv",
        help="Per-task output format.",
    )
    parser.add_argument("--output", default="-", help="Output path, or '-' for stdout.")
    parser.add_argument("--no-summary", action="store_true", help="Suppress aggregate statistics.")
    parser.add_argument(
        "--summary-format",
        choices=("text", "json"),
        default="text",
        help="Aggregate statistics format written to stderr.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Only print aggregate statistics; suppress per-task rows.",
    )
    return parser.parse_args(argv)


def _is_ignored_path(path: Path) -> bool:
    ignored = {".batch_runtime", ".git", "__pycache__", "task_view"}
    return any(part in ignored or part.startswith(".") for part in path.parts)


def iter_scan_roots(runs_dir: Path, dir_glob: str | None = None) -> list[Path]:
    if not dir_glob:
        return [runs_dir]
    roots = []
    for path in sorted(runs_dir.glob(dir_glob)):
        if not path.is_dir():
            continue
        rel = path.relative_to(runs_dir)
        if len(rel.parts) == 1 and not _is_ignored_path(rel):
            roots.append(path)
    return roots


def discover_reward_files(
    runs_dir: Path,
    reward_file: str = DEFAULT_REWARD_FILE,
) -> list[Path]:
    reward_path = Path(reward_file)
    if reward_path.is_absolute():
        raise ValueError("--reward-file must be relative to each task directory")

    files = []
    for path in sorted(runs_dir.glob(f"**/{reward_path.name}")):
        if not path.is_file():
            continue
        rel = path.relative_to(runs_dir)
        if _is_ignored_path(rel):
            continue
        if tuple(path.parts[-len(reward_path.parts) :]) == reward_path.parts:
            files.append(path)
    return files


def task_dir_from_reward_file(reward_path: Path, reward_file: str) -> Path:
    task_dir = reward_path
    for _ in Path(reward_file).parts:
        task_dir = task_dir.parent
    return task_dir


def equal_weight_scores(checks: Any) -> tuple[int, float, int, int]:
    """Return complete, partial, passed count, and total count for one task."""
    if not isinstance(checks, list) or not checks:
        return 0, 0.0, 0, 0
    passed_items = sum(
        isinstance(item, dict) and bool(item.get("passed")) for item in checks
    )
    total_items = len(checks)
    complete_pass = int(passed_items == total_items)
    partial_pass = round(passed_items / total_items, 6)
    return complete_pass, partial_pass, passed_items, total_items


def read_score(
    reward_path: Path,
    scan_root: Path,
    reward_file: str,
) -> tuple[RewardScore, bool]:
    valid = True
    try:
        data = json.loads(reward_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
        valid = False
    if not isinstance(data, dict):
        data = {}
        valid = False

    task_dir = task_dir_from_reward_file(reward_path, reward_file)
    rel_task_dir = task_dir.relative_to(scan_root)
    run_id = rel_task_dir.parts[0] if len(rel_task_dir.parts) > 1 else scan_root.name
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        valid = False
    complete, partial, passed, total = equal_weight_scores(checks)
    return (
        RewardScore(
            task_id=str(data.get("task_id") or task_dir.name),
            run_id=run_id,
            task_dir=str(task_dir),
            reward_file=str(reward_path),
            complete_pass=complete,
            partial_pass=partial,
            passed_items=passed,
            total_items=total,
        ),
        valid,
    )


def collect_scores(
    runs_dir: Path,
    reward_file: str = DEFAULT_REWARD_FILE,
    *,
    dir_glob: str | None = None,
) -> ScoreCollection:
    scores = []
    invalid_file_count = 0
    for scan_root in iter_scan_roots(runs_dir, dir_glob):
        for reward_path in discover_reward_files(scan_root, reward_file):
            score, valid = read_score(reward_path, scan_root, reward_file)
            scores.append(score)
            if not valid:
                invalid_file_count += 1
    return ScoreCollection(scores=scores, invalid_file_count=invalid_file_count)


def _average(values: Iterable[float | int]) -> float | None:
    numbers = list(values)
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 6)


def aggregate_scores(collection: ScoreCollection) -> dict[str, Any]:
    groups: dict[str, list[RewardScore]] = {}
    for score in collection.scores:
        groups.setdefault(score.run_id, []).append(score)

    def stats(rows: list[RewardScore]) -> dict[str, Any]:
        return {
            "task_count": len(rows),
            "avg_complete_pass": _average(row.complete_pass for row in rows),
            "avg_partial_pass": _average(row.partial_pass for row in rows),
        }

    result = stats(collection.scores)
    result["invalid_file_count"] = collection.invalid_file_count
    result["groups"] = {name: stats(rows) for name, rows in sorted(groups.items())}
    return result


def write_scores(scores: Iterable[RewardScore], output_format: str, output_path: str) -> None:
    rows = [score.as_dict() for score in scores]
    handle = sys.stdout if output_path == "-" else open(output_path, "w", encoding="utf-8", newline="")
    try:
        if output_format == "jsonl":
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            return
        delimiter = "\t" if output_format == "tsv" else ","
        fieldnames = list(RewardScore("", "", "", "", 0, 0.0, 0, 0).as_dict())
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if handle is not sys.stdout:
            handle.close()


def write_aggregate(stats: dict[str, Any], summary_format: str) -> None:
    if summary_format == "json":
        sys.stderr.write(json.dumps(stats, ensure_ascii=False) + "\n")
        return
    lines = [
        f"task_count: {stats['task_count']}",
        f"invalid_file_count: {stats['invalid_file_count']}",
        f"avg_complete_pass: {stats['avg_complete_pass']}",
        f"avg_partial_pass: {stats['avg_partial_pass']}",
    ]
    for name, group in stats["groups"].items():
        lines.extend(
            [
                f"group[{name}].task_count: {group['task_count']}",
                f"group[{name}].avg_complete_pass: {group['avg_complete_pass']}",
                f"group[{name}].avg_partial_pass: {group['avg_partial_pass']}",
            ]
        )
    sys.stderr.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    if not runs_dir.is_dir():
        raise SystemExit(f"runs dir not found: {runs_dir}")
    collection = collect_scores(runs_dir, args.reward_file, dir_glob=args.dir_glob)
    if not args.summary_only:
        write_scores(collection.scores, args.format, args.output)
    if args.summary_only or not args.no_summary:
        write_aggregate(aggregate_scores(collection), args.summary_format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
