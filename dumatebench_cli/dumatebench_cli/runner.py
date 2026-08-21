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

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dumatebench_cli.adapter import compose_cmd, compose_service, reset_dir, run_adapter_loop
from dumatebench_cli.task_metadata import TaskMetadataError, load_task_metadata, shared_evaluate_path

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


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture, env=env)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def _shared_evaluate_path() -> Path | None:
    """Find the shared evaluator shipped with a source checkout.

    Task evaluators are intentionally kept outside the task container and import
    this file at evaluation time. The CLI is normally installed editable from
    the repository, so the path next to this module is the stable default. The
    current working directory fallback also supports running from a copied CLI
    checkout.
    """
    candidates = [
        shared_evaluate_path(),
        Path.cwd() / "dumatebench" / "evaluator" / "evaluate.py",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _evaluator_env() -> dict[str, str]:
    """Return the evaluator environment without changing agent/container env."""
    env = os.environ.copy()
    if not env.get("DUMATE_EVALUATE_PY"):
        shared_path = _shared_evaluate_path()
        if shared_path is not None:
            env["DUMATE_EVALUATE_PY"] = str(shared_path)
    return env


def _reward_error(path: Path, expected_task_id: str) -> str | None:
    """Return a reason when reward.json violates the evaluator contract."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "reward.json is missing or invalid JSON"
    if not isinstance(value, dict):
        return "reward.json must contain an object"
    if value.get("task_id") != expected_task_id:
        return (
            f"reward.json task_id {value.get('task_id')!r} does not match "
            f"task.yaml task_id {expected_task_id!r}"
        )
    for key in ("complete_pass", "partial_pass"):
        score = value.get(key)
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= float(score) <= 1:
            return f"reward.json has invalid {key}"
    return None


def _valid_reward(path: Path, expected_task_id: str | None = None) -> bool:
    """Check that the evaluator produced the minimum reward contract."""
    if expected_task_id is None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(value, dict):
            return False
        for key in ("complete_pass", "partial_pass"):
            score = value.get(key)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= float(score) <= 1:
                return False
        return True
    return _reward_error(path, expected_task_id) is None


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


def _execution_id(run_id: str | None, prefix: str) -> str:
    base = run_id or prefix
    return f"{base}-{uuid.uuid4().hex[:10]}"


def compose_project_name(run_id: str, task_dir: Path) -> str:
    """Build a unique, stable Compose project name for one task execution."""
    digest = hashlib.sha1(f"{run_id}\0{task_dir.resolve()}".encode("utf-8")).hexdigest()[:12]
    safe_run = "".join(
        c.lower() if c.isascii() and (c.isalnum() or c in "-_") else "-"
        for c in run_id
    )
    safe_run = safe_run.strip("-_")[:28] or "run"
    return f"dumatebench-{safe_run}-{digest}"


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
    run_id: str | None = None,
) -> TaskResult:
    task_dir = task_dir.resolve()
    task_id = task_dir.name
    try:
        _task_yaml, task_id = load_task_metadata(task_dir)
    except TaskMetadataError as exc:
        return TaskResult(
            task_id=task_id,
            task_dir=str(task_dir),
            status="error",
            steps_taken=None,
            evaluator_returncode=None,
            elapsed_seconds=0.0,
            reward_path=None,
            error=str(exc),
        )
    execution_id = _execution_id(run_id, "single")
    project_name = compose_project_name(execution_id, task_dir)
    run_outputs = task_dir / "run_outputs"
    run_logs = task_dir / "run_logs"
    status_log = run_logs / "agent_status.json"
    adapter_log = run_logs / "agent_adapter.jsonl"

    reset_dir(run_outputs)
    reset_dir(run_logs)

    start = time.time()
    status: dict[str, Any] = {"adapter_command": agent_cmd, "compose_project_name": project_name}
    evaluator_returncode: int | None = None
    error: str | None = None
    reward_path = run_outputs / "reward.json"
    (run_logs / "compose_project_name.txt").write_text(project_name + "\n", encoding="utf-8")

    try:
        _run(compose_cmd(task_dir, project_name) + ["down", "--remove-orphans"], cwd=task_dir, check=False)
        if not no_build:
            _run(compose_cmd(task_dir, project_name) + ["build"], cwd=task_dir, capture=False)
        _run(
            compose_cmd(task_dir, project_name) + ["up", "-d", compose_service(task_dir)],
            cwd=task_dir,
            capture=False,
        )

        result = run_adapter_loop(
            task_dir=task_dir,
            agent_cmd=agent_cmd,
            max_steps=max_steps,
            adapter_timeout=adapter_timeout,
            step_log_cb=lambda record: write_jsonl(adapter_log, record),
            project_name=project_name,
        )
        status.update(result.as_status_dict())

        evaluator_proc = _run(
            [sys.executable, str(task_dir / "evaluator" / "evaluator.py"), "--task-dir", str(task_dir)],
            check=False,
            env=_evaluator_env(),
        )
        evaluator_returncode = evaluator_proc.returncode
        status["evaluator_returncode"] = evaluator_returncode
        if evaluator_proc.stderr:
            status["evaluator_stderr"] = evaluator_proc.stderr[-4000:]
        reward_error = _reward_error(reward_path, task_id)
        if reward_error:
            detail = (
                "Evaluator did not produce a valid reward.json "
                f"(returncode={evaluator_returncode}): {reward_error}."
            )
            stderr = evaluator_proc.stderr.strip()
            if stderr:
                detail += f"\n{stderr[-2000:]}"
            error = detail
            status["error"] = error
    except Exception as exc:  # noqa: BLE001 - surfaced in summary, not swallowed silently
        error = str(exc)
        status["error"] = error
    finally:
        status_log.write_text(json.dumps(status, indent=2, ensure_ascii=False))
        logs = _run(compose_cmd(task_dir, project_name) + ["logs", "--no-color"], cwd=task_dir, check=False).stdout
        (run_logs / "compose.log").write_text(logs or "")
        if not keep_containers:
            _run(compose_cmd(task_dir, project_name) + ["down", "--remove-orphans"], cwd=task_dir, check=False)

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
    run_id: str | None = None,
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
    execution_id = _execution_id(run_id, "batch")

    def _execute(task_dir: Path) -> TaskResult:
        return run_single_task(
            task_dir=task_dir,
            agent_cmd=agent_cmd,
            max_steps=max_steps,
            adapter_timeout=adapter_timeout,
            no_build=no_build,
            keep_containers=keep_containers,
            run_id=execution_id,
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
