#!/usr/bin/env python3
"""Summarize final checklist and LLM-judge scores for run directories."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_REWARD_FILE = "run_outputs/reward_with_llm_judge.json"
# A task directory keeps these next to its run outputs, which is how a task can
# still be listed when it never produced a reward file.
TASK_MARKER_FILES = ("task.yaml", "instruction.md")
ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dumatebench.evaluator.scoring import final_score as merge_final_score  # noqa: E402


@dataclass
class RewardSummary:
    task_id: str
    task_dir: str
    reward_file: str
    base_complete_pass: Any
    base_partial_pass: Any
    llm_judge_score: Any
    final_score: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_dir": self.task_dir,
            "reward_file": self.reward_file,
            "base_complete_pass": self.base_complete_pass,
            "base_partial_pass": self.base_partial_pass,
            "llm_judge_score": self.llm_judge_score,
            "final_score": self.final_score,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find runs/<task>/run_outputs/reward_with_llm_judge.json files and "
            "summarize true checklist partial pass and LLM-judge scores."
        )
    )
    parser.add_argument("tasks_dir", help="Runs root containing task run directories.")
    parser.add_argument(
        "--dir-glob",
        default=None,
        help=(
            "Only scan direct child directories of tasks_dir matching this glob "
            "(for example, 'group_*')."
        ),
    )
    parser.add_argument("--reward-file", default=DEFAULT_REWARD_FILE, help="Reward file path relative to each task directory.")
    parser.add_argument("--format", choices=("tsv", "csv", "jsonl"), default="tsv", help="Output format.")
    parser.add_argument("--output", default="-", help="Output file path, or '-' for stdout.")
    parser.add_argument("--include-missing", action="store_true", help="Also list discovered task directories without the reward file.")
    parser.add_argument("--no-summary", action="store_true", help="Do not print aggregate statistics to stderr.")
    parser.add_argument("--summary-format", choices=("text", "json"), default="text", help="Aggregate statistics format.")
    parser.add_argument("--summary-only", action="store_true", help="Only print aggregate statistics; suppress per-task rows.")
    return parser.parse_args(argv)


def _is_ignored_path(path: Path) -> bool:
    ignored = {".batch_runtime", ".git", "__pycache__"}
    return any(part in ignored or part.startswith(".") for part in path.parts)


def discover_reward_files(tasks_dir: Path, reward_file: str = DEFAULT_REWARD_FILE) -> list[Path]:
    """Find reward files at any depth under tasks_dir.

    Harbor writes runs as ``<job>/<task>/run_outputs/...``, and batch runs add
    their own grouping directories, so scanning only direct children misses
    most real layouts.
    """
    reward_path = Path(reward_file)
    if reward_path.is_absolute():
        raise ValueError("--reward-file must be relative to each task directory")
    if ".." in reward_path.parts:
        raise ValueError("--reward-file must not escape the task directory")

    depth = len(reward_path.parts)
    rewards: list[Path] = []
    for path in sorted(tasks_dir.rglob(reward_path.name)):
        if not path.is_file():
            continue
        rel = path.relative_to(tasks_dir)
        if len(rel.parts) < depth or rel.parts[-depth:] != reward_path.parts:
            continue
        if _is_ignored_path(rel):
            continue
        rewards.append(path)
    return rewards


def discover_task_dirs(tasks_dir: Path) -> list[Path]:
    """Find task directories at any depth, identified by their marker files."""
    tasks: list[Path] = []
    for path in sorted(tasks_dir.rglob("*")):
        if not path.is_dir():
            continue
        rel = path.relative_to(tasks_dir)
        if _is_ignored_path(rel):
            continue
        if any((path / marker).is_file() for marker in TASK_MARKER_FILES):
            tasks.append(path)
    return sorted(tasks)


def task_dir_from_reward_file(reward_path: Path, reward_file: str) -> Path:
    parts = Path(reward_file).parts
    task_dir = reward_path
    for _ in parts:
        task_dir = task_dir.parent
    return task_dir


def read_summary(reward_path: Path, reward_file: str) -> RewardSummary:
    task_dir = task_dir_from_reward_file(reward_path, reward_file)
    try:
        data = json.loads(reward_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        data = {"error": f"invalid json: {exc}"}
    if not isinstance(data, dict):
        data = {"error": "reward file is not a JSON object"}

    llm_judge_score = _number(data.get("llm_judge_score"))
    if llm_judge_score is None and isinstance(data.get("llm_judge"), dict):
        llm_judge_score = _number(data["llm_judge"].get("score"))
    complete_pass = _number(data.get("base_complete_pass"))
    checks_partial_pass = true_checklist_partial_pass(data)

    # Only a checks list proves what the evaluator actually scored. Without it
    # the recorded aggregates are the best evidence available, so they are kept
    # instead of being silently rewritten to zero or recomputed from a partial
    # picture.
    if checks_partial_pass is None:
        partial_pass = _number(data.get("base_partial_pass"))
        final_score = _number(data.get("final_score"))
    else:
        partial_pass = checks_partial_pass
        final_score = true_llm_final_score(
            checks_partial_pass, llm_judge_score, complete_pass or 0.0
        )
        if final_score is None:
            final_score = _number(data.get("final_score"))

    return RewardSummary(
        task_id=str(data.get("task_id") or task_dir.name),
        task_dir=str(task_dir),
        reward_file=str(reward_path),
        base_complete_pass=data.get("base_complete_pass"),
        base_partial_pass=partial_pass,
        llm_judge_score=llm_judge_score,
        final_score=final_score,
    )


def iter_scan_roots(tasks_dir: Path, dir_glob: str | None = None) -> list[Path]:
    if not dir_glob:
        return [tasks_dir]
    roots = []
    for path in sorted(tasks_dir.glob(dir_glob)):
        if not path.is_dir():
            continue
        try:
            rel = path.relative_to(tasks_dir)
        except ValueError:
            rel = path
        if len(rel.parts) != 1 or _is_ignored_path(rel):
            continue
        roots.append(path)
    return roots


def collect_summaries(
    tasks_dir: Path,
    reward_file: str = DEFAULT_REWARD_FILE,
    *,
    include_missing: bool = False,
    dir_glob: str | None = None,
) -> list[RewardSummary]:
    scan_roots = iter_scan_roots(tasks_dir, dir_glob)
    reward_paths = [path for root in scan_roots for path in discover_reward_files(root, reward_file)]
    summaries = [read_summary(path, reward_file) for path in reward_paths]
    if not include_missing:
        return summaries

    seen_task_dirs = {Path(summary.task_dir).resolve() for summary in summaries}
    for task_dir in [path for root in scan_roots for path in discover_task_dirs(root)]:
        if task_dir.resolve() in seen_task_dirs:
            continue
        summaries.append(
            RewardSummary(
                task_id=task_dir.name,
                task_dir=str(task_dir),
                reward_file=str(task_dir / reward_file),
                base_complete_pass=None,
                base_partial_pass=None,
                llm_judge_score=None,
                final_score=None,
            )
        )
    return sorted(summaries, key=lambda item: item.task_dir)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def true_checklist_partial_pass(data: dict[str, Any]) -> float | None:
    """Weighted checklist pass rate, or None when the file records no checks."""
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        return None
    passed = 0
    for item in checks:
        if isinstance(item, dict) and bool(item.get("passed")):
            passed += 1
    return round(passed / len(checks), 6)


def true_llm_final_score(
    true_partial_pass: float,
    llm_judge_score: float | None,
    complete_pass: float = 0.0,
) -> float | None:
    if llm_judge_score is None:
        return None
    return merge_final_score(complete_pass, true_partial_pass, llm_judge_score, precision=6)


def _average(values: Iterable[Any]) -> float | None:
    numbers = [_number(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 6)


def aggregate_summaries(summaries: Iterable[RewardSummary]) -> dict[str, Any]:
    rows = list(summaries)
    reward_rows = [row for row in rows if Path(row.reward_file).is_file()]
    return {
        "task_count": len(rows),
        "reward_count": len(reward_rows),
        "missing_reward_count": len(rows) - len(reward_rows),
        "avg_base_complete_pass": _average(row.base_complete_pass for row in reward_rows),
        "avg_base_partial_pass": _average(row.base_partial_pass for row in reward_rows),
        "avg_llm_judge_score": _average(row.llm_judge_score for row in reward_rows),
        "avg_final_score": _average(row.final_score for row in reward_rows),
    }


def write_aggregate(stats: dict[str, Any], summary_format: str) -> None:
    if summary_format == "json":
        sys.stderr.write(json.dumps(stats, ensure_ascii=False) + "\n")
        return
    sys.stderr.write(
        "\n".join(
            [
                f"task_count: {stats['task_count']}",
                f"reward_count: {stats['reward_count']}",
                f"missing_reward_count: {stats['missing_reward_count']}",
                f"avg_base_complete_pass: {stats['avg_base_complete_pass']}",
                f"avg_base_partial_pass: {stats['avg_base_partial_pass']}",
                f"avg_llm_judge_score: {stats['avg_llm_judge_score']}",
                f"avg_final_score: {stats['avg_final_score']}",
            ]
        )
        + "\n"
    )


def write_summaries(summaries: Iterable[RewardSummary], output_format: str, output_path: str) -> None:
    rows = [summary.as_dict() for summary in summaries]
    handle = sys.stdout if output_path == "-" else open(output_path, "w", encoding="utf-8", newline="")
    try:
        if output_format == "jsonl":
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            return

        delimiter = "\t" if output_format == "tsv" else ","
        writer = csv.DictWriter(handle, fieldnames=list(RewardSummary("", "", "", None, None, None, None).as_dict()), delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if handle is not sys.stdout:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks_dir = Path(args.tasks_dir).expanduser().resolve()
    if not tasks_dir.is_dir():
        raise SystemExit(f"tasks dir not found: {tasks_dir}")
    summaries = collect_summaries(tasks_dir, args.reward_file, include_missing=args.include_missing, dir_glob=args.dir_glob)
    if not args.summary_only:
        write_summaries(summaries, args.format, args.output)
    if args.summary_only or not args.no_summary:
        write_aggregate(aggregate_summaries(summaries), args.summary_format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
