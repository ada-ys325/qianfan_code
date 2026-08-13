#!/usr/bin/env python3
"""Summarize batch rewards under a runs directory after filtering batch rows."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_EXCLUDED_AGENT_RETURNCODES = (137,)


@dataclass
class BatchRewardSummary:
    task_id: str
    task_dir: str
    run_id: Any
    run_dir: str
    status: Any
    agent_returncode: Any
    evaluator_returncode: Any
    elapsed_seconds: Any
    reward_file: str
    base_complete_pass: Any
    base_partial_pass: Any
    llm_judge_score: Any
    final_score: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_dir": self.task_dir,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "status": self.status,
            "agent_returncode": self.agent_returncode,
            "evaluator_returncode": self.evaluator_returncode,
            "elapsed_seconds": self.elapsed_seconds,
            "reward_file": self.reward_file,
            "base_complete_pass": self.base_complete_pass,
            "base_partial_pass": self.base_partial_pass,
            "llm_judge_score": self.llm_judge_score,
            "final_score": self.final_score,
        }


@dataclass
class BatchCollection:
    summaries: list[BatchRewardSummary]
    batch_row_count: int
    filtered_row_count: int
    invalid_line_count: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read a run_task_batch.py JSONL summary, filter out rows whose "
            "agent_returncode matches --exclude-agent-returncode, then summarize "
            "the remaining rows' reward files under --runs."
        )
    )
    parser.add_argument("batch_summary", help="Path to batch_summary.jsonl, e.g. A.jsonl.")
    parser.add_argument(
        "--runs",
        required=True,
        help=(
            "Runs root to scan for remaining tasks' rewards. This can be either "
            "the run_id directory or a parent containing run_id subdirectories."
        ),
    )
    parser.add_argument(
        "--exclude-agent-returncode",
        type=int,
        action="append",
        default=None,
        help="Agent return code to exclude. May be repeated. Defaults to 137.",
    )
    parser.add_argument(
        "--reward-file",
        default="run_outputs/reward_with_llm_judge.json",
        help=(
            "Reward file path relative to each task run directory. Defaults to "
            "run_outputs/reward_with_llm_judge.json."
        ),
    )
    parser.add_argument("--format", choices=("tsv", "csv", "jsonl"), default="tsv", help="Output format.")
    parser.add_argument("--output", default="-", help="Output file path, or '-' for stdout.")
    parser.add_argument("--no-summary", action="store_true", help="Do not print aggregate statistics to stderr.")
    parser.add_argument("--summary-format", choices=("text", "json"), default="text", help="Aggregate statistics format.")
    parser.add_argument("--summary-only", action="store_true", help="Only print aggregate statistics; suppress per-task rows.")
    return parser.parse_args(argv)


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    invalid_count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            invalid_count += 1
            continue
        if isinstance(item, dict):
            rows.append(item)
        else:
            invalid_count += 1
    return rows, invalid_count


def _agent_returncode(row: dict[str, Any]) -> int | None:
    value = row.get("agent_returncode")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _candidate_reward_paths(row: dict[str, Any], runs_dir: Path, reward_file: str) -> list[Path]:
    candidates: list[Path] = []
    run_dir = row.get("run_dir")
    if run_dir:
        run_leaf = Path(str(run_dir)).name
        candidates.append(runs_dir / run_leaf / reward_file)
        run_id = row.get("run_id")
        if run_id:
            candidates.append(runs_dir / str(run_id) / run_leaf / reward_file)

    task_id = row.get("task_id")
    if task_id:
        candidates.append(runs_dir / str(task_id) / reward_file)
        run_id = row.get("run_id")
        if run_id:
            candidates.append(runs_dir / str(run_id) / str(task_id) / reward_file)

    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in candidates:
        resolved = path.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def _index_reward_files(runs_dir: Path, reward_file: str) -> dict[str, Path]:
    reward_name = Path(reward_file).name
    index: dict[str, Path] = {}
    for path in sorted(runs_dir.glob(f"**/{reward_name}")):
        if not path.is_file():
            continue
        try:
            if path.relative_to(path.parents[len(Path(reward_file).parts) - 1]).as_posix() != reward_file:
                continue
        except (IndexError, ValueError):
            pass
        run_dir = path
        for _ in Path(reward_file).parts:
            run_dir = run_dir.parent
        index.setdefault(run_dir.name, path)
        reward = _read_reward(path)
        task_id = reward.get("task_id")
        if task_id:
            index.setdefault(str(task_id), path)
    return index


def _reward_path(row: dict[str, Any], runs_dir: Path, reward_file: str, reward_index: dict[str, Path]) -> Path:
    for path in _candidate_reward_paths(row, runs_dir, reward_file):
        if path.is_file():
            return path

    run_dir = row.get("run_dir")
    if run_dir:
        path = reward_index.get(Path(str(run_dir)).name)
        if path:
            return path

    task_id = row.get("task_id")
    if task_id:
        path = reward_index.get(str(task_id))
        if path:
            return path

    candidates = _candidate_reward_paths(row, runs_dir, reward_file)
    return candidates[0] if candidates else runs_dir / str(row.get("task_id") or "") / reward_file


def _read_reward(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_summary(row: dict[str, Any], runs_dir: Path, reward_file: str, reward_index: dict[str, Path]) -> BatchRewardSummary:
    reward_path = _reward_path(row, runs_dir, reward_file, reward_index)
    reward = _read_reward(reward_path)
    task_id = str(row.get("task_id") or reward.get("task_id") or Path(str(row.get("task_dir") or "")).name)
    return BatchRewardSummary(
        task_id=task_id,
        task_dir=str(row.get("task_dir") or ""),
        run_id=row.get("run_id"),
        run_dir=str(row.get("run_dir") or ""),
        status=row.get("status"),
        agent_returncode=row.get("agent_returncode"),
        evaluator_returncode=row.get("evaluator_returncode"),
        elapsed_seconds=row.get("elapsed_seconds"),
        reward_file=str(reward_path),
        base_complete_pass=reward.get("base_complete_pass"),
        base_partial_pass=reward.get("base_partial_pass"),
        llm_judge_score=reward.get("llm_judge_score"),
        final_score=reward.get("final_score"),
    )


def collect_summaries(
    batch_summary: Path,
    runs_dir: Path,
    *,
    excluded_agent_returncodes: set[int] | None = None,
    reward_file: str = "run_outputs/reward_with_llm_judge.json",
) -> BatchCollection:
    excluded = set(DEFAULT_EXCLUDED_AGENT_RETURNCODES if excluded_agent_returncodes is None else excluded_agent_returncodes)
    rows, invalid_count = _read_jsonl(batch_summary)
    kept_rows = [row for row in rows if _agent_returncode(row) not in excluded]
    reward_index = _index_reward_files(runs_dir, reward_file)
    return BatchCollection(
        summaries=[read_summary(row, runs_dir, reward_file, reward_index) for row in kept_rows],
        batch_row_count=len(rows),
        filtered_row_count=len(rows) - len(kept_rows),
        invalid_line_count=invalid_count,
    )


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _average(values: Iterable[Any]) -> float | None:
    numbers = [_number(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 6)


def aggregate_collection(collection: BatchCollection) -> dict[str, Any]:
    rows = collection.summaries
    reward_rows = [row for row in rows if Path(row.reward_file).is_file()]
    return {
        "batch_row_count": collection.batch_row_count,
        "filtered_agent_returncode_count": collection.filtered_row_count,
        "invalid_line_count": collection.invalid_line_count,
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
                f"batch_row_count: {stats['batch_row_count']}",
                f"filtered_agent_returncode_count: {stats['filtered_agent_returncode_count']}",
                f"invalid_line_count: {stats['invalid_line_count']}",
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


def write_summaries(summaries: Iterable[BatchRewardSummary], output_format: str, output_path: str) -> None:
    rows = [summary.as_dict() for summary in summaries]
    handle = sys.stdout if output_path == "-" else open(output_path, "w", encoding="utf-8", newline="")
    try:
        if output_format == "jsonl":
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            return

        delimiter = "\t" if output_format == "tsv" else ","
        writer = csv.DictWriter(
            handle,
            fieldnames=list(BatchRewardSummary("", "", None, "", None, None, None, None, "", None, None, None, None).as_dict()),
            delimiter=delimiter,
        )
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if handle is not sys.stdout:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    batch_summary = Path(args.batch_summary).expanduser().resolve()
    if not batch_summary.is_file():
        raise SystemExit(f"batch summary not found: {batch_summary}")
    runs_dir = Path(args.runs).expanduser().resolve()
    if not runs_dir.is_dir():
        raise SystemExit(f"runs dir not found: {runs_dir}")
    excluded = set(args.exclude_agent_returncode or DEFAULT_EXCLUDED_AGENT_RETURNCODES)
    collection = collect_summaries(
        batch_summary,
        runs_dir,
        excluded_agent_returncodes=excluded,
        reward_file=args.reward_file,
    )
    if not args.summary_only:
        write_summaries(collection.summaries, args.format, args.output)
    if args.summary_only or not args.no_summary:
        write_aggregate(aggregate_collection(collection), args.summary_format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
