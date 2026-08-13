#!/usr/bin/env python3
"""Rebuild a run's batch_summary.jsonl from per-task agent_status.json files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


TASK_ID_RE = re.compile(r"^\s*task_id\s*:\s*(.*?)\s*$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild <run-dir>/batch_summary.jsonl from each task's "
            "run_logs/agent_status.json."
        )
    )
    parser.add_argument("run_dir", type=Path, help="A single run directory under a runs/ directory.")
    parser.add_argument(
        "--dataset-root",
        type=Path,
        help="Dataset root used for task_dir. Defaults to the parent of the runs/ directory.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSONL path. Defaults to <run-dir>/batch_summary.jsonl.",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing output file.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Stop instead of skipping a malformed agent_status.json.",
    )
    return parser.parse_args(argv)


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value is not an object")
    return value


def _unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, str) else value
        except json.JSONDecodeError:
            return value.strip('"')
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value.split(" #", 1)[0].strip()


def task_id_from_run_dir(run_dir: Path) -> str:
    candidates = (
        run_dir / "task_view" / "task.yaml",
        run_dir / ".batch_runtime" / "smoke" / "task_context" / "task.yaml",
    )
    for task_yaml in candidates:
        if not task_yaml.is_file():
            continue
        for line in task_yaml.read_text(encoding="utf-8").splitlines():
            match = TASK_ID_RE.match(line)
            if match:
                task_id = _unquote_yaml_scalar(match.group(1))
                if task_id:
                    return task_id

    # Run directory components are sanitized to lowercase, so this fallback may
    # not preserve the original task-id case. Normal runs retain task.yaml.
    return run_dir.name.rsplit("__", 1)[-1]


def task_dir_from_run_dir(run_dir: Path, dataset_root: Path, task_id: str) -> Path:
    parts = run_dir.name.split("__")
    return dataset_root.joinpath(*parts[:-1], task_id)


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def infer_evaluator_returncode(status: dict[str, Any], reward_path: Path) -> int | None:
    evaluator_rc = _optional_int(status.get("evaluator_returncode"))
    if evaluator_rc is not None:
        return evaluator_rc
    # The checklist evaluator writes reward.json. Its presence is the best
    # available recovery signal when native-agent status omitted evaluator_rc.
    return 0 if reward_path.is_file() else None


def infer_status(status: dict[str, Any], agent_rc: int | None, evaluator_rc: int | None) -> str:
    if status.get("agent_finished") is False and bool(status.get("max_steps_reached")):
        return "agent_incomplete"
    if status.get("timed_out") is True or agent_rc == 124:
        return "agent_timeout"
    if agent_rc == 137:
        return "agent_sigkill"
    if agent_rc not in {None, 0} or evaluator_rc not in {None, 0}:
        return "failed"
    if agent_rc == 0 and evaluator_rc == 0:
        return "ok"
    return "failed"


def build_row(task_run_dir: Path, dataset_root: Path, run_id: str) -> dict[str, Any]:
    status = _read_json_object(task_run_dir / "run_logs" / "agent_status.json")
    task_id = task_id_from_run_dir(task_run_dir)
    task_dir = task_dir_from_run_dir(task_run_dir, dataset_root, task_id)
    reward_path = task_run_dir / "run_outputs" / "reward.json"
    final_reward_path = task_run_dir / "task_view" / "run_outputs" / "reward_with_llm_judge.json"
    agent_rc = _optional_int(status.get("agent_returncode"))
    evaluator_rc = infer_evaluator_returncode(status, reward_path)
    return {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "run_id": run_id,
        "run_dir": str(task_run_dir),
        "status": infer_status(status, agent_rc, evaluator_rc),
        "agent_returncode": agent_rc,
        "evaluator_returncode": evaluator_rc,
        "elapsed_seconds": _optional_float(status.get("elapsed_seconds")),
        "reward_path": str(reward_path) if reward_path.is_file() else None,
        "final_reward_path": str(final_reward_path) if final_reward_path.is_file() else None,
        "error": None,
    }


def build_missing_status_row(task_run_dir: Path, dataset_root: Path, run_id: str) -> dict[str, Any]:
    """Build the best-effort row for a run directory whose agent status is absent."""
    task_id = task_id_from_run_dir(task_run_dir)
    task_dir = task_dir_from_run_dir(task_run_dir, dataset_root, task_id)
    reward_path = task_run_dir / "run_outputs" / "reward.json"
    final_reward_path = task_run_dir / "task_view" / "run_outputs" / "reward_with_llm_judge.json"
    return {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "run_id": run_id,
        "run_dir": str(task_run_dir),
        "status": "agent_sigkill",
        "agent_returncode": -9,
        "evaluator_returncode": None,
        "elapsed_seconds": 0.0,
        "reward_path": str(reward_path) if reward_path.is_file() else None,
        "final_reward_path": str(final_reward_path) if final_reward_path.is_file() else None,
        "error": "run_logs/agent_status.json is missing; agent return code assumed to be -9",
    }


def discover_status_files(run_dir: Path) -> list[Path]:
    return sorted(
        path / "run_logs" / "agent_status.json"
        for path in run_dir.iterdir()
        if path.is_dir() and (path / "run_logs" / "agent_status.json").is_file()
    )


def discover_task_run_dirs(run_dir: Path) -> list[Path]:
    return sorted(path for path in run_dir.iterdir() if path.is_dir())


def write_jsonl_atomic(output: Path, rows: list[dict[str, Any]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(temporary_name, output)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = args.run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        print(f"error: run directory does not exist: {run_dir}", file=sys.stderr)
        return 2

    dataset_root = (
        args.dataset_root.expanduser().resolve()
        if args.dataset_root
        else run_dir.parent.parent.resolve()
    )
    output = args.output.expanduser().resolve() if args.output else run_dir / "batch_summary.jsonl"
    if output.exists() and not args.force:
        print(f"error: output already exists (pass --force to replace it): {output}", file=sys.stderr)
        return 2

    task_run_dirs = discover_task_run_dirs(run_dir)
    if not task_run_dirs:
        print(f"error: no task directories found under {run_dir}", file=sys.stderr)
        return 1

    rows: list[dict[str, Any]] = []
    skipped = 0
    for task_run_dir in task_run_dirs:
        status_file = task_run_dir / "run_logs" / "agent_status.json"
        if not status_file.is_file():
            rows.append(build_missing_status_row(task_run_dir, dataset_root, run_dir.name))
            continue
        try:
            rows.append(build_row(task_run_dir, dataset_root, run_dir.name))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            if args.strict:
                raise
            skipped += 1
            print(f"warning: skipping {status_file}: {exc}", file=sys.stderr)

    if not rows:
        print("error: no valid agent status files found", file=sys.stderr)
        return 1
    write_jsonl_atomic(output, rows)
    print(f"wrote {len(rows)} rows to {output} (skipped {skipped}, missing_status={sum(1 for row in rows if row['error'] and 'agent_status.json' in row['error'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
