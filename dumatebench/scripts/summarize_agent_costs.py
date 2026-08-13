#!/usr/bin/env python3
"""Summarize elapsed time, token usage, and API cost for batch runs."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_COST_FILE = "run_logs/agent_cost.json"
DEFAULT_NATIVE_LOG_FILE = "run_logs/native_agent.jsonl"
ROOT = Path(__file__).resolve().parents[1]

# USD per one million tokens. Keep this in sync with
# dumatebench_sjh/scripts/calculate_hermes_cost.py.
PRICING: dict[str, dict[str, float]] = {
    "gpt-5.5": {"input": 5.0, "output": 30.0, "cache_read": 0.5, "cache_write": 0.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "glm-5.2": {"input": 1.4, "output": 4.4, "cache_read": 0.26, "cache_write": 0.0},
    "deepseek-v4-pro": {"input": 1.74, "output": 3.48, "cache_read": 0.145, "cache_write": 0.0},
}


def _calculate_model_cost(model: Any, summary: "AgentCostSummary") -> float | None:
    aliases = {"opus-4.8": "claude-opus-4-8", "claude-opus-4.8": "claude-opus-4-8"}
    model_name = aliases.get(str(model), str(model))
    prices = PRICING.get(model_name)
    if prices is None:
        return None
    # Legacy agent_cost.json files only have a combined cache_tokens field;
    # without the read/write split, the Hermes pricing formula cannot be
    # applied faithfully.  The native JSONL path supplies both fields.
    if any(value is None for value in (summary.input_tokens, summary.output_tokens,
                                       summary.cache_read_tokens, summary.cache_write_tokens)):
        return None

    def tokens(value: Any) -> int:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return 0
        return int(value)

    return sum(
        tokens(value) * prices[price_key] / 1_000_000
        for value, price_key in (
            (summary.input_tokens, "input"),
            (summary.output_tokens, "output"),
            (summary.cache_read_tokens, "cache_read"),
            (summary.cache_write_tokens, "cache_write"),
        )
    )


@dataclass
class AgentCostSummary:
    task_id: str
    run_id: str
    task_dir: str
    cost_file: str
    agent_backend: Any
    agent_model: Any
    elapsed_seconds: Any
    api_calls: Any
    usage_sources: Any
    input_tokens: Any
    output_tokens: Any
    cache_tokens: Any
    total_tokens: Any
    total_cost_usd: Any
    # These fields were added after the original summary schema.  Keep them
    # last (and optional) so callers constructing rows positionally remain
    # compatible with older versions.
    cache_read_tokens: Any = None
    cache_write_tokens: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "task_dir": self.task_dir,
            "cost_file": self.cost_file,
            "agent_backend": self.agent_backend,
            "agent_model": self.agent_model,
            "elapsed_seconds": self.elapsed_seconds,
            "api_calls": self.api_calls,
            "usage_sources": self.usage_sources,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_tokens": self.cache_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


@dataclass
class CostCollection:
    summaries: list[AgentCostSummary]
    invalid_file_count: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find run_logs/agent_cost.json files under a runs directory and "
            "summarize elapsed time, token usage, and total_cost_usd."
        )
    )
    parser.add_argument("runs_dir", help="Runs root to scan, e.g. /data/sx/runs/claude-code-glm-5.2.")
    parser.add_argument(
        "--cost-file",
        default=DEFAULT_COST_FILE,
        help="Cost file path relative to each task run directory. Defaults to run_logs/agent_cost.json.",
    )
    parser.add_argument(
        "--native-log-file",
        default=DEFAULT_NATIVE_LOG_FILE,
        help="Native agent JSONL path relative to each task directory. Its final usage record is authoritative for tokens.",
    )
    parser.add_argument(
        "--dir-glob",
        default=None,
        help="Only scan direct child directories of runs_dir matching this glob, for example 'group_*'.",
    )
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recursively discover cost files under each scan root. Defaults to true.",
    )
    parser.add_argument(
        "--group-by",
        choices=("none", "backend-model"),
        default="backend-model",
        help="Aggregate summary grouping. Defaults to backend-model.",
    )
    parser.add_argument("--format", choices=("tsv", "csv", "jsonl"), default="tsv", help="Output format.")
    parser.add_argument("--output", default="-", help="Output file path, or '-' for stdout.")
    parser.add_argument("--no-summary", action="store_true", help="Do not print aggregate statistics to stderr.")
    parser.add_argument("--summary-format", choices=("text", "json"), default="text", help="Aggregate statistics format.")
    parser.add_argument("--summary-only", action="store_true", help="Only print aggregate statistics; suppress per-task rows.")
    return parser.parse_args(argv)


def _is_ignored_path(path: Path) -> bool:
    ignored = {".batch_runtime", ".git", "__pycache__"}
    return any(part in ignored or part.startswith(".") for part in path.parts)


def iter_scan_roots(runs_dir: Path, dir_glob: str | None = None) -> list[Path]:
    if not dir_glob:
        return [runs_dir]
    roots: list[Path] = []
    for path in sorted(runs_dir.glob(dir_glob)):
        if not path.is_dir():
            continue
        try:
            rel = path.relative_to(runs_dir)
        except ValueError:
            rel = path
        if len(rel.parts) != 1 or _is_ignored_path(rel):
            continue
        roots.append(path)
    return roots


def discover_cost_files(runs_dir: Path, cost_file: str = DEFAULT_COST_FILE, *, recursive: bool = True) -> list[Path]:
    cost_path = Path(cost_file)
    if cost_path.is_absolute():
        raise ValueError("--cost-file must be relative to each task run directory")

    if recursive:
        candidates = sorted(runs_dir.glob(f"**/{cost_path.name}"))
    else:
        candidates = sorted((path / cost_path for path in runs_dir.iterdir() if path.is_dir()))

    costs: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(runs_dir)
        except ValueError:
            rel = path
        if _is_ignored_path(rel):
            continue
        if recursive and tuple(path.parts[-len(cost_path.parts) :]) != cost_path.parts:
            continue
        costs.append(path)
    return costs


def task_dir_from_cost_file(cost_path: Path, cost_file: str) -> Path:
    task_dir = cost_path
    for _ in Path(cost_file).parts:
        task_dir = task_dir.parent
    return task_dir


def _read_cost(path: Path) -> tuple[dict[str, Any], bool]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    return data, True


def _read_final_usage(path: Path) -> dict[str, Any] | None:
    """Return usage from the last valid native-agent JSONL record."""
    if not path.is_file():
        return None
    final: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        usage = record.get("usage")
        if not isinstance(usage, dict) and isinstance(record.get("message"), dict):
            usage = record["message"].get("usage")
        if isinstance(usage, dict):
            final = usage
    return final


def _native_token_values(usage: dict[str, Any]) -> dict[str, int]:
    """Compute token totals from the explicit usage fields in a native log."""
    def integer(name: str) -> int:
        value = usage.get(name, 0)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    input_tokens = integer("input_tokens")
    output_tokens = integer("output_tokens")
    cache_read = integer("cache_read_input_tokens")
    cache_write = integer("cache_creation_input_tokens")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_read + cache_write,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": input_tokens + output_tokens + cache_read + cache_write,
    }


def read_summary(
    cost_path: Path,
    runs_dir: Path,
    cost_file: str,
    native_log_file: str = DEFAULT_NATIVE_LOG_FILE,
) -> tuple[AgentCostSummary, bool]:
    data, valid = _read_cost(cost_path)
    task_dir = task_dir_from_cost_file(cost_path, cost_file)
    try:
        rel_task_dir = task_dir.relative_to(runs_dir)
    except ValueError:
        rel_task_dir = task_dir
    task_id = str(data.get("task_id") or task_dir.name)
    run_id = str(data.get("run_id") or (rel_task_dir.parts[0] if len(rel_task_dir.parts) > 1 else runs_dir.name))

    native_usage = _read_final_usage(task_dir / native_log_file)
    token_values = _native_token_values(native_usage) if native_usage is not None else {}

    summary = AgentCostSummary(
            task_id=task_id,
            run_id=run_id,
            task_dir=str(task_dir),
            cost_file=str(cost_path),
            agent_backend=data.get("agent_backend"),
            agent_model=data.get("agent_model"),
            elapsed_seconds=data.get("elapsed_seconds"),
            api_calls=data.get("api_calls"),
            usage_sources=data.get("usage_sources"),
            input_tokens=token_values.get("input_tokens", data.get("input_tokens")),
            output_tokens=token_values.get("output_tokens", data.get("output_tokens")),
            cache_tokens=token_values.get("cache_tokens", data.get("cache_tokens")),
            cache_read_tokens=token_values.get("cache_read_tokens", data.get("cache_read_tokens", data.get("cache_read_input_tokens"))),
            cache_write_tokens=token_values.get("cache_write_tokens", data.get("cache_write_tokens", data.get("cache_creation_input_tokens"))),
            total_tokens=token_values.get("total_tokens", data.get("total_tokens")),
            total_cost_usd=data.get("total_cost_usd"),
        )
    recalculated_cost = _calculate_model_cost(summary.agent_model, summary)
    if recalculated_cost is not None:
        summary.total_cost_usd = round(recalculated_cost, 8)
    return summary, valid


def collect_summaries(
    runs_dir: Path,
    cost_file: str = DEFAULT_COST_FILE,
    *,
    dir_glob: str | None = None,
    recursive: bool = True,
    native_log_file: str = DEFAULT_NATIVE_LOG_FILE,
) -> CostCollection:
    summaries: list[AgentCostSummary] = []
    invalid_count = 0
    for root in iter_scan_roots(runs_dir, dir_glob):
        for cost_path in discover_cost_files(root, cost_file, recursive=recursive):
            summary, valid = read_summary(cost_path, root, cost_file, native_log_file)
            summaries.append(summary)
            if not valid:
                invalid_count += 1
    return CostCollection(summaries=summaries, invalid_file_count=invalid_count)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum(values: Iterable[Any], *, digits: int | None = None) -> float | int:
    numbers = [_number(value) for value in values]
    total = sum(value for value in numbers if value is not None)
    if digits is not None:
        return round(total, digits)
    return int(total)


def _average(values: Iterable[Any]) -> float | None:
    numbers = [_number(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 6)


def aggregate_rows(rows: list[AgentCostSummary], invalid_file_count: int) -> dict[str, Any]:
    return {
        "run_count": len(rows),
        "invalid_file_count": invalid_file_count,
        "total_elapsed_seconds": _sum((row.elapsed_seconds for row in rows), digits=3),
        "avg_elapsed_seconds": _average(row.elapsed_seconds for row in rows),
        "total_api_calls": _sum(row.api_calls for row in rows),
        "total_input_tokens": _sum(row.input_tokens for row in rows),
        "total_output_tokens": _sum(row.output_tokens for row in rows),
        "total_cache_tokens": _sum(row.cache_tokens for row in rows),
        "total_cache_read_tokens": _sum(row.cache_read_tokens for row in rows),
        "total_cache_write_tokens": _sum(row.cache_write_tokens for row in rows),
        "total_tokens": _sum(row.total_tokens for row in rows),
        "avg_input_tokens": _average(row.input_tokens for row in rows),
        "avg_output_tokens": _average(row.output_tokens for row in rows),
        "avg_cache_read_tokens": _average(row.cache_read_tokens for row in rows),
        "avg_cache_write_tokens": _average(row.cache_write_tokens for row in rows),
        "avg_total_tokens": _average(row.total_tokens for row in rows),
        "total_cost_usd": _sum((row.total_cost_usd for row in rows), digits=6),
        "avg_cost_usd": _average(row.total_cost_usd for row in rows),
    }


def aggregate_collection(collection: CostCollection, group_by: str = "backend-model") -> dict[str, Any]:
    rows = collection.summaries
    stats = aggregate_rows(rows, collection.invalid_file_count)
    if group_by == "none":
        return stats

    groups: dict[str, list[AgentCostSummary]] = {}
    for row in rows:
        key = f"{row.agent_backend or ''}/{row.agent_model or ''}"
        groups.setdefault(key, []).append(row)
    stats["groups"] = {key: aggregate_rows(value, 0) for key, value in sorted(groups.items())}
    return stats


def write_aggregate(stats: dict[str, Any], summary_format: str) -> None:
    if summary_format == "json":
        sys.stderr.write(json.dumps(stats, ensure_ascii=False) + "\n")
        return

    keys = [
        "run_count",
        "invalid_file_count",
        "total_elapsed_seconds",
        "avg_elapsed_seconds",
        "total_api_calls",
        "total_input_tokens",
        "total_output_tokens",
        "total_cache_tokens",
        "total_cache_read_tokens",
        "total_cache_write_tokens",
        "total_tokens",
        "avg_input_tokens",
        "avg_output_tokens",
        "avg_cache_read_tokens",
        "avg_cache_write_tokens",
        "avg_total_tokens",
        "total_cost_usd",
        "avg_cost_usd",
    ]
    lines = [f"{key}: {stats[key]}" for key in keys]
    groups = stats.get("groups")
    if isinstance(groups, dict) and groups:
        lines.append("groups:")
        for key, group_stats in groups.items():
            lines.append(
                "  "
                + key
                + ": "
                + ", ".join(
                    [
                        f"run_count={group_stats['run_count']}",
                        f"total_elapsed_seconds={group_stats['total_elapsed_seconds']}",
                        f"avg_input_tokens={group_stats['avg_input_tokens']}",
                        f"avg_output_tokens={group_stats['avg_output_tokens']}",
                        f"avg_cache_read_tokens={group_stats['avg_cache_read_tokens']}",
                        f"avg_cache_write_tokens={group_stats['avg_cache_write_tokens']}",
                        f"total_tokens={group_stats['total_tokens']}",
                        f"avg_total_tokens={group_stats['avg_total_tokens']}",
                        f"total_cost_usd={group_stats['total_cost_usd']}",
                    ]
                )
            )
    sys.stderr.write("\n".join(lines) + "\n")


def write_summaries(summaries: Iterable[AgentCostSummary], output_format: str, output_path: str) -> None:
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
            fieldnames=list(
                AgentCostSummary(
                    "", "", "", "", None, None, None, None, None, None, None, None, None, None
                ).as_dict()
            ),
            delimiter=delimiter,
        )
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if handle is not sys.stdout:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    if not runs_dir.is_dir():
        raise SystemExit(f"runs dir not found: {runs_dir}")
    collection = collect_summaries(
        runs_dir,
        args.cost_file,
        dir_glob=args.dir_glob,
        recursive=args.recursive,
        native_log_file=args.native_log_file,
    )
    if not args.summary_only:
        write_summaries(collection.summaries, args.format, args.output)
    if args.summary_only or not args.no_summary:
        write_aggregate(aggregate_collection(collection, args.group_by), args.summary_format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
