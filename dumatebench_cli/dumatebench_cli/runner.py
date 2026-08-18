"""Batch runner: discover DuMateBench task directories, run an agent adapter
against each, run the task's own evaluator, and write a JSONL summary.

This mirrors the docker/evaluator lifecycle in
``dumatebench/scripts/run_task_batch.py`` but keeps only what a contestant
needs: task discovery, docker compose build/up/down, adapter execution via
``adapter.py``, evaluator invocation, and summary output. Internal-only
concerns (LLM judge, memory-scaling retries, web-reference prep) are not
reproduced here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dumatebench_cli.adapter import compose_cmd, compose_service, reset_dir, run_adapter_loop

TASK_MARKER_FILES = ("task.yaml", "instruction.md")


@dataclass
class TaskResult:
    task_id: str
    task_dir: str
    status: str
    steps_taken: int | None
    evaluator_returncode: int | None
    elapsed_seconds: float
    reward_path: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_dir": self.task_dir,
            "status": self.status,
            "steps_taken": self.steps_taken,
            "evaluator_returncode": self.evaluator_returncode,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "reward_path": self.reward_path,
            "error": self.error,
        }


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def is_task_dir(path: Path) -> bool:
    return path.is_dir() and all((path / marker).exists() for marker in TASK_MARKER_FILES)


def discover_tasks(tasks_root: Path, task_glob: str = "*", recursive: bool = True) -> list[Path]:
    if is_task_dir(tasks_root):
        return [tasks_root]

    candidates = tasks_root.rglob(task_glob) if recursive else tasks_root.glob(task_glob)
    found = sorted({p for p in candidates if is_task_dir(p)})
    return found


def default_run_id(agent_label: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_label = "".join(c if c.isalnum() or c in "-_." else "-" for c in agent_label)
    return f"{safe_label}-{timestamp}"


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_single_task(
    task_dir: Path,
    agent_cmd: str,
    max_steps: int,
    adapter_timeout: int,
    no_build: bool,
    keep_containers: bool,
) -> TaskResult:
    task_dir = task_dir.resolve()
    task_id = task_dir.name
    run_outputs = task_dir / "run_outputs"
    run_logs = task_dir / "run_logs"
    status_log = run_logs / "agent_status.json"
    adapter_log = run_logs / "agent_adapter.jsonl"

    reset_dir(run_outputs)
    reset_dir(run_logs)

    start = time.time()
    status: dict[str, Any] = {"adapter_command": agent_cmd}
    evaluator_returncode: int | None = None
    error: str | None = None

    try:
        _run(compose_cmd(task_dir) + ["down", "--remove-orphans"], cwd=task_dir, check=False)
        if not no_build:
            _run(compose_cmd(task_dir) + ["build"], cwd=task_dir, capture=False)
        _run(compose_cmd(task_dir) + ["up", "-d", compose_service(task_dir)], cwd=task_dir, capture=False)

        result = run_adapter_loop(
            task_dir=task_dir,
            agent_cmd=agent_cmd,
            max_steps=max_steps,
            adapter_timeout=adapter_timeout,
            step_log_cb=lambda record: write_jsonl(adapter_log, record),
        )
        status.update(result.as_status_dict())

        evaluator_proc = _run(
            [sys.executable, str(task_dir / "evaluator" / "evaluator.py"), "--task-dir", str(task_dir)],
            check=False,
        )
        evaluator_returncode = evaluator_proc.returncode
        status["evaluator_returncode"] = evaluator_returncode
    except Exception as exc:  # noqa: BLE001 - surfaced in summary, not swallowed silently
        error = str(exc)
        status["error"] = error
    finally:
        status_log.write_text(json.dumps(status, indent=2, ensure_ascii=False))
        logs = _run(compose_cmd(task_dir) + ["logs", "--no-color"], cwd=task_dir, check=False).stdout
        (run_logs / "compose.log").write_text(logs or "")
        if not keep_containers:
            _run(compose_cmd(task_dir) + ["down", "--remove-orphans"], cwd=task_dir, check=False)

    reward_path = run_outputs / "reward.json"
    return TaskResult(
        task_id=task_id,
        task_dir=str(task_dir),
        status="error" if error else "completed",
        steps_taken=status.get("steps"),
        evaluator_returncode=evaluator_returncode,
        elapsed_seconds=time.time() - start,
        reward_path=str(reward_path) if reward_path.exists() else None,
        error=error,
    )


def run_batch(
    tasks_root: Path,
    agent_cmd: str,
    task_glob: str = "*",
    recursive: bool = True,
    limit: int = 0,
    max_steps: int = 20,
    adapter_timeout: int = 180,
    no_build: bool = False,
    keep_containers: bool = False,
    concurrency: int = 1,
    summary_path: Path | None = None,
    stop_on_failure: bool = False,
) -> list[TaskResult]:
    tasks = discover_tasks(tasks_root, task_glob=task_glob, recursive=recursive)
    if limit > 0:
        tasks = tasks[:limit]
    if not tasks:
        raise RuntimeError(f"No task directories found under {tasks_root} (glob={task_glob!r}).")

    if summary_path is None:
        summary_path = tasks_root / "batch_summary.jsonl"
    if summary_path.exists():
        summary_path.unlink()

    results: list[TaskResult] = []

    def _execute(task_dir: Path) -> TaskResult:
        return run_single_task(
            task_dir=task_dir,
            agent_cmd=agent_cmd,
            max_steps=max_steps,
            adapter_timeout=adapter_timeout,
            no_build=no_build,
            keep_containers=keep_containers,
        )

    if concurrency <= 1:
        for task_dir in tasks:
            result = _execute(task_dir)
            results.append(result)
            write_jsonl(summary_path, result.as_dict())
            if stop_on_failure and result.status == "error":
                break
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_execute, task_dir): task_dir for task_dir in tasks}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                write_jsonl(summary_path, result.as_dict())

    return results
