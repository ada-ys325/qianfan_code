#!/usr/bin/env python3
"""Batch-run DuMateBench tasks with a reusable smoke-task runtime config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEFAULT_TEMPLATE_TASK = ROOT / "datasets/dev/template_task"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dumatebench.evaluator.scoring import equal_weight_partial_pass, final_score as merge_final_score  # noqa: E402


@dataclass
class TaskResult:
    task_id: str
    task_dir: str
    run_id: str | None
    run_dir: str | None
    status: str
    agent_returncode: int | None
    evaluator_returncode: int | None
    elapsed_seconds: float
    reward_path: str | None
    final_reward_path: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_dir": self.task_dir,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "status": self.status,
            "agent_returncode": self.agent_returncode,
            "evaluator_returncode": self.evaluator_returncode,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "reward_path": self.reward_path,
            "final_reward_path": self.final_reward_path,
            "error": self.error,
        }


@dataclass
class TaskExecution:
    result: TaskResult
    memory_mb: int | None
    attempt: int
    attempts: int


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run every task under a directory while reusing the docker and fault-injection "
            "configuration from a template smoke task."
        )
    )
    parser.add_argument("--tasks-dir", default=str(ROOT / "datasets/dev"), help="Directory containing task subdirectories.")
    parser.add_argument("--template-task", default=str(DEFAULT_TEMPLATE_TASK), help="Task whose docker/fault config is reused.")
    parser.add_argument("--task-glob", default="*", help="Glob for task directory names under --tasks-dir.")
    parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True, help="Recursively discover task directories under --tasks-dir.")
    parser.add_argument(
        "--dedupe-by-name",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Collapse duplicate task leaf directory names and keep one path. Disabled by default "
            "so sibling groups can contain tasks with the same session/task directory name."
        ),
    )
    parser.add_argument("--reuse-template-setup", action="store_true", help="Use the template task's setup.sh verbatim instead of generating a generic setup.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of tasks to run; 0 means all.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip tasks whose run directory already exists under <runs-root>/<run-id>.")
    parser.add_argument("--stop-on-failure", action="store_true", help="Stop after the first task failure.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Maximum number of different tasks to execute concurrently. Defaults to 1.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print the tasks and generated runtime paths.")
    parser.add_argument("--runtime-name", default="smoke", help="Subdirectory under .batch_runtime for generated config.")
    parser.add_argument("--run-id", default="", help="Experiment/run identifier. Defaults to <backend>-<model>-<timestamp>.")
    parser.add_argument("--runs-root", default="", help="Directory for isolated run artifacts. Defaults to <package-root>/runs.")
    parser.add_argument("--summary-file", default="", help="JSONL summary path. Defaults to <runs-root>/<run-id>/batch_summary.jsonl.")
    parser.add_argument(
        "--native-memory-summary-file",
        default="",
        help="JSONL path recording successful native-task memory usage. Defaults to <runs-root>/<run-id>/native_memory_summary.jsonl.",
    )
    parser.add_argument(
        "--rerun-agent-137-summary",
        default="",
        help="Previous batch_summary.jsonl whose rows with agent_returncode=137 should be rerun.",
    )
    parser.add_argument(
        "--rerun-agent-1-summary",
        default="",
        help="Previous batch_summary.jsonl whose rows with agent_returncode=1 should be rerun.",
    )
    parser.add_argument(
        "--rerun-agent-timeout-summary",
        default="",
        help="Previous batch_summary.jsonl whose rows with agent_returncode not in {0, 1} should be rerun.",
    )
    parser.add_argument("--evaluator-python", default=os.environ.get("DUMATE_EVALUATOR_PYTHON", sys.executable))
    parser.add_argument("--trusted-base-url", default=os.environ.get("DUMATE_TRUSTED_BASE_URLS", "https://cn.huayanapi.com:27502/v1"))
    parser.add_argument("--image-prefix", default="dumatebench-batch", help="Docker image name prefix.")
    parser.add_argument("--max-steps", type=int, default=20, help="Maximum ReAct command-agent steps for each task.")
    parser.add_argument(
        "--agent-backend",
        choices=("react", "claude-code", "codex"),
        default=os.environ.get("DUMATE_AGENT_BACKEND", "react"),
        help="Agent implementation to evaluate. 'react' preserves the existing command_agent.py runner.",
    )
    parser.add_argument(
        "--agent-model",
        default=os.environ.get("DUMATE_AGENT_MODEL") or os.environ.get("DUMATE_MODEL", "gpt-4o"),
        help="Model ID used by the selected agent backend.",
    )
    parser.add_argument(
        "--agent-base-url",
        default=os.environ.get("DUMATE_AGENT_BASE_URL", ""),
        help=(
            "Native-agent model gateway. Codex requires OpenAI Responses; Claude Code requires "
            "Anthropic Messages. Defaults to the backend-specific environment variable."
        ),
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=0,
        help="Native-agent wall-clock timeout in seconds; zero uses agent.timeout_sec from task.yaml.",
    )
    parser.add_argument(
        "--agent-max-turns",
        type=int,
        default=0,
        help="Optional Claude Code turn limit for native agents; zero lets the CLI run until it exits or times out.",
    )
    parser.add_argument(
        "--agent-reasoning-effort",
        default=os.environ.get("DUMATE_AGENT_REASONING_EFFORT", ""),
        help=(
            "Optional native-agent reasoning effort. Empty/default sends no override; "
            "other values are passed through to native_agent.py."
        ),
    )
    parser.add_argument(
        "--claude-code-version",
        default=os.environ.get("DUMATE_CLAUDE_CODE_VERSION", "stable"),
        help="Claude Code version or release channel installed in native images.",
    )
    parser.add_argument(
        "--codex-version",
        default=os.environ.get("DUMATE_CODEX_VERSION", "latest"),
        help="Codex npm fallback version installed when the standalone installer is unavailable.",
    )
    parser.add_argument(
        "--native-direct-model-network",
        action="store_true",
        help="Apply task network faults to native CLI model traffic too (disabled by default).",
    )
    parser.add_argument(
        "--native-memory-mb",
        type=int,
        default=_env_int("DUMATE_NATIVE_MEMORY_MB", 8192),
        help="Docker memory limit for codex/claude-code native agent attempts, in MB.",
    )
    parser.add_argument(
        "--native-retry-memory-mb",
        type=int,
        default=_env_int("DUMATE_NATIVE_RETRY_MEMORY_MB", 16384),
        help="Docker memory limit for codex/claude-code retry attempts after agent return code 137, in MB.",
    )
    parser.add_argument("--final-reward-file", default="run_outputs/reward_with_llm_judge.json")
    parser.add_argument("--skip-llm-judge", action="store_true", help="Do not actively run the unified LLM judge; only merge existing judge reports.")
    parser.add_argument("--llm-judge-model", default=os.environ.get("DUMATE_LLM_JUDGE_MODEL") or os.environ.get("OPENAI_MODEL") or os.environ.get("DUMATE_MODEL") or "gpt-4o")
    parser.add_argument("--llm-judge-base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    parser.add_argument("--reference-dir", default="workspace_seed", help="Reference/input directory passed to unified judge; use '-' to disable.")
    parser.add_argument("--web-reference-dir", default="web_reference", help="Additional web gold-reference directory merged into judge references; use '-' to disable.")
    parser.add_argument("--llm-judge-criteria-file", default="", help="Optional per-task JSON criteria/rubric file passed to unified judge.")
    parser.add_argument(
        "--llm-judge-artifacts-file",
        default="",
        help=(
            "Per-task JSON manifest listing exactly which artifacts the LLM judge should score. "
            "Defaults to evaluator/llm_judge_artifacts.json when that file exists; otherwise targets are inferred."
        ),
    )
    parser.add_argument("--no-build", action="store_true", help="Skip docker compose build and use an existing image.")
    parser.add_argument(
        "agent_args",
        nargs=argparse.REMAINDER,
        help="Extra selected-runner arguments after a literal --. Usually unnecessary for native backends.",
    )
    args = parser.parse_args(argv)
    if args.agent_args and args.agent_args[0] == "--":
        args.agent_args = args.agent_args[1:]
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def _is_ignored_discovery_path(path: Path) -> bool:
    ignored_parts = {
        ".batch_runtime",
        ".git",
        "__pycache__",
        "environment",
        "evaluator",
        "run_logs",
        "run_outputs",
        "runs",
        "workspace_seed",
    }
    return any(part in ignored_parts or part.startswith(".") for part in path.parts)


def discover_tasks(tasks_dir: Path, task_glob: str, limit: int, *, recursive: bool = True) -> list[Path]:
    candidates = tasks_dir.rglob(task_glob) if recursive else tasks_dir.glob(task_glob)
    tasks = []
    for path in sorted(candidates):
        if not path.is_dir():
            continue
        try:
            rel = path.relative_to(tasks_dir)
        except ValueError:
            rel = path
        if _is_ignored_discovery_path(rel):
            continue
        if (path / "instruction.md").is_file():
            tasks.append(path)
    return tasks[:limit] if limit > 0 else tasks


def dedupe_tasks_by_name(tasks: list[Path]) -> list[Path]:
    chosen: dict[str, Path] = {}
    for task in tasks:
        existing = chosen.get(task.name)
        if existing is None:
            chosen[task.name] = task
            continue
        existing_depth = len(existing.parts)
        task_depth = len(task.parts)
        if task_depth > existing_depth or (task_depth == existing_depth and str(task) < str(existing)):
            chosen[task.name] = task
    return sorted(chosen.values())


def _ignore_runtime_noise(dir_name: str, names: list[str]) -> set[str]:
    del dir_name
    return {
        name
        for name in names
        if name in {".DS_Store", "__pycache__", ".pytest_cache", "run_outputs", "run_logs"}
        or name.endswith(".pyc")
        or name.startswith("._")
    }


def _relative_to_package_root(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def _sanitize_image_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower()
    return cleaned[:96] or "task"


def default_run_id(args: argparse.Namespace) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    model = _sanitize_image_component(args.agent_model)
    return f"{_sanitize_image_component(args.agent_backend)}-{model}-{timestamp}-{os.getpid()}"


def default_runs_root() -> Path:
    return ROOT / "runs"


def task_run_name(task_dir: Path, tasks_dir: Path) -> str:
    try:
        rel = task_dir.resolve().relative_to(tasks_dir.resolve()).as_posix()
    except ValueError:
        rel = task_dir.name
    return _sanitize_image_component(rel.replace("/", "__"))


def task_display_name(task_dir: Path, tasks_dir: Path) -> str:
    try:
        return task_dir.resolve().relative_to(tasks_dir.resolve()).as_posix()
    except ValueError:
        return task_dir.name


def compose_project_name_for_run(run_dir: Path) -> str:
    """Return a Docker Compose project name unique to this task run directory."""
    digest = hashlib.sha1(str(run_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    prefix = _sanitize_image_component(run_dir.name)[:32].strip("._-") or "task"
    return f"dumatebench-{prefix}-{digest}"


def _read_task_memory_mb(task_dir: Path) -> int | None:
    resources = _read_environment_resources(task_dir / "task.yaml")
    value = resources.get("memory_mb")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _native_backend_uses_memory_retry(agent_backend: str) -> bool:
    return agent_backend in {"codex", "claude-code"}


def _memory_attempts_for_task(task_dir: Path, args: argparse.Namespace) -> list[int | None]:
    if not _native_backend_uses_memory_retry(args.agent_backend):
        return [None]
    task_memory_mb = _read_task_memory_mb(task_dir) or 0
    initial_memory_mb = max(task_memory_mb, int(args.native_memory_mb))
    retry_memory_mb = max(task_memory_mb, int(args.native_retry_memory_mb))
    return [initial_memory_mb, retry_memory_mb, retry_memory_mb]


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_agent_returncode_rerun_keys(
    summary_file: Path,
    should_rerun: Any,
) -> tuple[set[Path], set[str]]:
    task_paths: set[Path] = set()
    task_ids: set[str] = set()
    if not summary_file.is_file():
        raise SystemExit(f"rerun summary not found: {summary_file}")
    for line_number, line in enumerate(summary_file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            print(f"[warn] ignoring malformed summary row {summary_file}:{line_number}", file=sys.stderr, flush=True)
            continue
        try:
            agent_returncode = int(row.get("agent_returncode"))
        except (TypeError, ValueError):
            agent_returncode = None
        if not should_rerun(agent_returncode):
            continue
        task_dir = row.get("task_dir")
        if isinstance(task_dir, str) and task_dir:
            task_paths.add(Path(task_dir).expanduser().resolve())
        for key in ("task_id", "task_name", "display_name"):
            value = row.get(key)
            if isinstance(value, str) and value:
                task_ids.add(value)
    return task_paths, task_ids


def _load_agent_137_rerun_keys(summary_file: Path) -> tuple[set[Path], set[str]]:
    return _load_agent_returncode_rerun_keys(summary_file, lambda agent_returncode: agent_returncode == 137)


def _filter_tasks_for_agent_returncode_rerun(
    tasks: list[Path],
    summary_file: Path,
    tasks_dir: Path,
    should_rerun: Any,
    option_name: str,
) -> list[Path]:
    task_paths, task_ids = _load_agent_returncode_rerun_keys(summary_file, should_rerun)
    filtered = [
        task
        for task in tasks
        if task.resolve() in task_paths
        or task.name in task_ids
        or task_display_name(task, tasks_dir) in task_ids
    ]
    print(f"{option_name}: {summary_file} matched {len(filtered)} / {len(tasks)} discovered tasks")
    return filtered


def _filter_tasks_for_agent_137_rerun(tasks: list[Path], summary_file: Path, tasks_dir: Path) -> list[Path]:
    return _filter_tasks_for_agent_returncode_rerun(
        tasks,
        summary_file,
        tasks_dir,
        lambda agent_returncode: agent_returncode == 137,
        "rerun_agent_137_summary",
    )


def default_task_yaml_text(task_dir: Path) -> str:
    return (
        f"task_id: {json.dumps(task_dir.name, ensure_ascii=False)}\n"
        "agent:\n"
        "  timeout_sec: 900\n"
    )


def ensure_task_yaml(source: Path, target: Path, task_dir: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    if source.is_file():
        shutil.copy2(source, target)
        return
    target.write_text(default_task_yaml_text(task_dir), encoding="utf-8")


def _clear_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in list(path.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _link_or_copy(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    try:
        target.symlink_to(source.resolve(), target_is_directory=source.is_dir())
    except OSError:
        if source.is_dir():
            shutil.copytree(source, target, ignore=_ignore_runtime_noise)
        else:
            shutil.copy2(source, target)


def prepare_task_view(task_dir: Path, run_dir: Path) -> Path:
    view_dir = run_dir / "task_view"
    _clear_directory(view_dir)
    for name in ("instruction.md", "evaluator", "workspace_seed"):
        source = task_dir / name
        if source.exists():
            _link_or_copy(source, view_dir / name)
    ensure_task_yaml(task_dir / "task.yaml", view_dir / "task.yaml", task_dir)
    for name in ("web_reference", ".llm_judge_selected_references"):
        source = task_dir / name
        if source.exists():
            _link_or_copy(source, view_dir / name)
    _link_or_copy(run_dir / "run_outputs", view_dir / "run_outputs")
    _link_or_copy(run_dir / "run_logs", view_dir / "run_logs")
    return view_dir


def _write_generic_setup(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

mkdir -p /workspace /outputs /logs
rm -rf /workspace/*
cp -a /workspace_seed/. /workspace/

chown -R agent:agent /workspace /outputs /logs
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _read_environment_resources(task_yaml: Path) -> dict[str, str]:
    if not task_yaml.is_file():
        return {}
    text = task_yaml.read_text(encoding="utf-8", errors="ignore")
    resources: dict[str, str] = {}
    patterns = {
        "cpus": r"(?m)^\s*cpus\s*:\s*([0-9.]+)\s*$",
        "memory_mb": r"(?m)^\s*memory_mb\s*:\s*(\d+)\s*$",
        "storage_mb": r"(?m)^\s*storage_mb\s*:\s*(\d+)\s*$",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            resources[key] = match.group(1)
    return resources


def prepare_runtime(
    task_dir: Path,
    template_task: Path,
    runtime_name: str,
    image_prefix: str,
    *,
    agent_backend: str = "react",
    runtime_parent: Path | None = None,
    output_dir: Path | None = None,
    logs_dir: Path | None = None,
    reuse_template_setup: bool = False,
    memory_mb_override: int | None = None,
) -> Path:
    runtime_dir = (runtime_parent or task_dir) / ".batch_runtime" / runtime_name
    output_dir = output_dir or (task_dir / "run_outputs")
    logs_dir = logs_dir or (task_dir / "run_logs")
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)

    template_env = template_task / "environment"
    runtime_env = runtime_dir / "environment"
    shutil.copytree(template_env, runtime_env, ignore=_ignore_runtime_noise)
    if not reuse_template_setup:
        _write_generic_setup(runtime_env / "setup.sh")
    runtime_agents = runtime_dir / "agents"
    runtime_agents.mkdir()
    for agent_file in ("command_agent.py", "native_agent.py"):
        shutil.copy2(ROOT / "agents" / agent_file, runtime_agents / agent_file)
    runtime_task = runtime_dir / "task_context"
    shutil.copytree(task_dir / "workspace_seed", runtime_task / "workspace_seed", ignore=_ignore_runtime_noise)
    shutil.copy2(task_dir / "instruction.md", runtime_task / "instruction.md")
    ensure_task_yaml(task_dir / "task.yaml", runtime_task / "task.yaml", task_dir)
    shutil.copytree(task_dir / "evaluator", runtime_task / "evaluator", ignore=_ignore_runtime_noise)
    for file_name in ("network_faults.yaml", "tool_faults.yaml"):
        source = template_task / file_name
        if source.is_file():
            shutil.copy2(source, runtime_dir / file_name)

    dockerfile = runtime_env / "Dockerfile"
    docker_text = dockerfile.read_text(encoding="utf-8")
    build_context = runtime_dir
    template_rel = _relative_to_package_root(template_task)
    task_rel = "task_context"
    runtime_rel = "."
    docker_text = docker_text.replace(f"{template_rel}/workspace_seed/", f"{task_rel}/workspace_seed/")
    docker_text = docker_text.replace(f"{template_rel}/instruction.md", f"{task_rel}/instruction.md")
    docker_text = docker_text.replace(f"{template_rel}/task.yaml", f"{task_rel}/task.yaml")
    docker_text = docker_text.replace(f"{template_rel}/evaluator/", f"{task_rel}/evaluator/")
    docker_text = docker_text.replace(f"{template_rel}/network_faults.yaml", f"{runtime_rel}/network_faults.yaml")
    docker_text = docker_text.replace(f"{template_rel}/tool_faults.yaml", f"{runtime_rel}/tool_faults.yaml")
    docker_text = docker_text.replace(f"{template_rel}/environment/", f"{runtime_rel}/environment/")
    dockerfile.write_text(docker_text, encoding="utf-8")

    backend_component = "" if agent_backend == "react" else f"-{_sanitize_image_component(agent_backend)}"
    image_name = f"{image_prefix}{backend_component}-{_sanitize_image_component(task_dir.name)}:latest"
    resources = _read_environment_resources(task_dir / "task.yaml")
    resource_lines = ""
    if resources.get("cpus"):
        resource_lines += f'    cpus: "{resources["cpus"]}"\n'
    memory_mb = memory_mb_override if memory_mb_override is not None else resources.get("memory_mb")
    if memory_mb:
        resource_lines += f"    mem_limit: {memory_mb}m\n"
    if resources.get("storage_mb"):
        resource_lines += f'    labels:\n      dumatebench.storage_mb: "{resources["storage_mb"]}"\n'
    compose_text = f"""services:
  task:
    build:
      context: {build_context.as_posix()}
      dockerfile: environment/Dockerfile
      args:
        DUMATE_BASE_IMAGE: ${{DUMATE_BASE_IMAGE:-python:3.12-slim}}
        DUMATE_APT_DEBIAN_MIRROR: ${{DUMATE_APT_DEBIAN_MIRROR:-}}
        DUMATE_APT_SECURITY_MIRROR: ${{DUMATE_APT_SECURITY_MIRROR:-}}
        DUMATE_AGENT_BACKEND: ${{DUMATE_AGENT_BACKEND:-react}}
        DUMATE_CLAUDE_CODE_VERSION: ${{DUMATE_CLAUDE_CODE_VERSION:-stable}}
        DUMATE_CODEX_VERSION: ${{DUMATE_CODEX_VERSION:-latest}}
    image: {image_name}
{resource_lines.rstrip()}
    working_dir: /workspace
    environment:
      DUMATE_TASK_SEED: "20260706"
      DUMATE_NETWORK_FAULT_CONFIG: "/opt/dumate/network_faults.yaml"
      DUMATE_TOOL_FAULT_CONFIG: "/opt/dumate/tool_faults.yaml"
    cap_add:
      - NET_ADMIN
    volumes:
      - {output_dir.resolve().as_posix()}:/outputs
      - {logs_dir.resolve().as_posix()}:/logs
"""
    (runtime_dir / "docker-compose.yaml").write_text(compose_text, encoding="utf-8")
    return runtime_dir


def run_command(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    proc = subprocess.run(cmd, cwd=cwd, env=env, check=False)
    return int(proc.returncode)


def native_agent_base_url(args: argparse.Namespace, env: dict[str, str]) -> str:
    if args.agent_base_url:
        return args.agent_base_url.rstrip("/")
    if args.agent_backend == "codex":
        return env.get("OPENAI_BASE_URL", "").rstrip("/")
    return env.get("ANTHROPIC_BASE_URL", "").rstrip("/")


def build_agent_command(compose_file: Path, args: argparse.Namespace, env: dict[str, str]) -> list[str]:
    common = ["docker", "compose", "-f", str(compose_file), "run", "--rm"]
    if args.agent_backend == "react":
        return [
            *common,
            "-e",
            "OPENAI_API_KEY",
            "-e",
            "OPENAI_BASE_URL",
            "-e",
            f"DUMATE_MODEL={args.agent_model}",
            "-e",
            f"DUMATE_TRUSTED_BASE_URLS={args.trusted_base_url}",
            "task",
            "/opt/dumate/command_agent.py",
            "--in-container",
            "--task-dir",
            "/opt/dumate/task",
            "--trusted-base-url",
            args.trusted_base_url,
            "--max-steps",
            str(args.max_steps),
            *args.agent_args,
        ]

    base_url = native_agent_base_url(args, env)
    if not base_url:
        expected = "OPENAI_BASE_URL" if args.agent_backend == "codex" else "ANTHROPIC_BASE_URL"
        raise RuntimeError(
            f"{args.agent_backend} requires --agent-base-url, DUMATE_AGENT_BASE_URL, or {expected}"
        )
    command = [
        *common,
        "-e",
        "DUMATE_AGENT_API_KEY",
        "-e",
        "OPENAI_API_KEY",
        "-e",
        "ANTHROPIC_API_KEY",
        "-e",
        "ANTHROPIC_AUTH_TOKEN",
        "-e",
        f"DUMATE_AGENT_MODEL={args.agent_model}",
        "-e",
        f"DUMATE_AGENT_BASE_URL={base_url}",
        "-e",
        f"DUMATE_AGENT_REASONING_EFFORT={args.agent_reasoning_effort}",
        "task",
        "/opt/dumate/native_agent.py",
        "--backend",
        args.agent_backend,
        "--task-dir",
        "/opt/dumate/task",
        "--model",
        args.agent_model,
        "--base-url",
        base_url,
        "--timeout",
        str(args.agent_timeout),
    ]
    if args.agent_reasoning_effort and args.agent_reasoning_effort != "default":
        command.extend(["--reasoning-effort", args.agent_reasoning_effort])
    if args.agent_max_turns > 0:
        command.extend(["--max-turns", str(args.agent_max_turns)])
    if args.native_direct_model_network:
        command.append("--direct-model-network")
    command.extend(args.agent_args)
    return command


def _unit_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 1.0:
        score /= 100.0
    return max(0.0, min(1.0, score))


def _optional_unit_score(value: Any) -> float | None:
    return None if value is None else _unit_score(value)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _infer_output_file_from_checks(task_dir: Path, reward: dict[str, Any]) -> str | None:
    outputs = _infer_output_files_from_checks(task_dir, reward)
    return outputs[0] if outputs else None


def _append_output_candidate(candidates: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    value = value.strip()
    if value:
        candidates.append(value)


def _infer_output_files_from_checks(task_dir: Path, reward: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for check in reward.get("checks", []):
        if not isinstance(check, dict):
            continue
        detail = check.get("detail")
        if isinstance(detail, str):
            parsed = _extract_json_object(detail)
            if parsed and isinstance(parsed.get("file"), str):
                _append_output_candidate(candidates, parsed["file"])
            if parsed and isinstance(parsed.get("expected_files"), list):
                for item in parsed["expected_files"]:
                    _append_output_candidate(candidates, item)

    checks_path = task_dir / "evaluator" / "checks.yaml"
    if checks_path.is_file():
        text = checks_path.read_text(encoding="utf-8", errors="ignore")
        candidates.extend(re.findall(r'["\']?file["\']?\s*:\s*["\']([^"\']+)["\']', text))
        for match in re.findall(r'["\']?expected_files["\']?\s*:\s*\[([^\]]+)\]', text, flags=re.S):
            candidates.extend(re.findall(r'["\']([^"\']+)["\']', match))

    instruction_path = task_dir / "instruction.md"
    if instruction_path.is_file():
        instruction = instruction_path.read_text(encoding="utf-8", errors="ignore")
        candidates.extend(re.findall(r'run_outputs/[^\s`"\'，。；,;）)\]]+', instruction))

    ordered: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if not path.startswith("run_outputs/"):
            continue
        path = path.rstrip("，。；,;")
        if path not in seen:
            ordered.append(path)
            seen.add(path)
    return ordered


def _artifact_type(path: str | None) -> str | None:
    if not path:
        return None
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"ppt", "pptx"}:
        return "ppt"
    if suffix in {"doc", "docx", "md", "txt", "json", "html", "htm"}:
        return "textual"
    if suffix in {"xls", "xlsx", "xlsm", "xltx", "xltm"}:
        return "excel"
    if suffix in {
        "py", "js", "jsx", "ts", "tsx", "java", "go", "rs", "cpp", "cc", "cxx", "c",
        "h", "hpp", "cs", "rb", "php", "swift", "kt", "kts", "scala", "sh", "bash",
        "zsh", "ps1", "sql", "r", "lua", "pl", "pm", "dart", "ex", "exs", "erl",
        "hrl", "clj", "cljs", "fs", "fsx", "jl", "nim", "zig", "vue", "svelte",
        "astro", "xml", "yml", "yaml", "properties", "gradle",
    }:
        return "code"
    return suffix or None


def _supports_unified_llm_judge(output_file: str, artifact_type: str | None = None) -> bool:
    from dumatebench.evaluator.llm_judge.unified import (
        CODE_TYPES,
        EXCEL_TYPES,
        IMAGE_TYPES,
        MULTIMODAL_TYPES,
        PDF_TYPES,
        PPT_TYPES,
        TEXTUAL_TYPES,
        infer_artifact_type,
    )

    artifact_type = (artifact_type or infer_artifact_type(output_file)).lower().lstrip(".")
    return artifact_type in (
        PPT_TYPES | TEXTUAL_TYPES | EXCEL_TYPES | PDF_TYPES | IMAGE_TYPES | MULTIMODAL_TYPES | CODE_TYPES
    )


SKIPPED_JUDGE_STATUSES = {"skipped_unsupported", "skipped_unavailable"}


def _skipped_unsupported_artifact_report(output_file: str, artifact_exists: bool, artifact_type: str | None = None) -> dict[str, Any]:
    artifact_type = artifact_type or _artifact_type(output_file)
    return {
        "output_file": output_file,
        "artifact_type": artifact_type,
        "artifact_exists": artifact_exists,
        "status": "skipped_unsupported",
        "reason": f"Skipped unsupported LLM judge artifact type: {artifact_type}",
        "judge_score": None,
        "judge_report_file": None,
    }


def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _load_existing_judge_score(task_dir: Path) -> tuple[float | None, str, str, dict[str, Any] | None]:
    run_outputs = task_dir / "run_outputs"

    unified = _load_json_file(run_outputs / "llm_judge_score.json")
    if unified is not None:
        status = str(unified.get("status") or ("ok" if "error" not in unified else "failed"))
        reason = str(unified.get("reason") or unified.get("error") or "Loaded existing unified LLM judge report.")
        return (
            _optional_unit_score(unified.get("judge_score", unified.get("final_score"))),
            status,
            reason,
            unified,
        )

    ppt = _load_json_file(run_outputs / "ppt_llm_judge.json")
    if ppt is not None:
        score = ppt.get("judge_score")
        if score is None and isinstance(ppt.get("result"), dict):
            score = ppt["result"].get("score")
        status = str(ppt.get("status") or "ok")
        return _optional_unit_score(score), status, str(ppt.get("reason") or "Loaded existing PPT LLM judge report."), ppt

    excel = _load_json_file(run_outputs / "excel_llm_judge" / "judge_result.json")
    if excel is not None:
        score = excel.get("judge_score")
        if score is None and isinstance(excel.get("result"), dict):
            score = excel["result"].get("overall_score")
        return _unit_score(score), "ok", "Loaded existing Excel LLM judge report.", excel

    return 0.0, "not_run", "LLM judge report was not produced.", None


def _safe_report_name(output_file: str, index: int) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "_", output_file).strip("_")
    return f"{index:02d}_{stem[:140] or 'artifact'}.json"


def _judge_report_file_for_output(output_file: str, index: int, total: int) -> str:
    if total == 1:
        return "run_outputs/llm_judge_score.json"
    return f"run_outputs/llm_judge_scores/{_safe_report_name(output_file, index)}"


def _read_json_manifest(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_llm_judge_artifact_specs(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        values = raw.get("artifacts") or raw.get("outputs") or raw.get("output_files")
        defaults = {
            key: value
            for key, value in raw.items()
            if key not in {"artifacts", "outputs", "output_files"} and value not in (None, "")
        }
    else:
        values = raw
        defaults = {}
    if not isinstance(values, list):
        raise ValueError("LLM judge artifacts manifest must be a list or contain artifacts/output_files list")
    specs: list[dict[str, Any]] = []
    for index, item in enumerate(values, start=1):
        if isinstance(item, str):
            output_file = item
            spec: dict[str, Any] = {**defaults, "id": f"artifact_{index}", "output_file": output_file}
        elif isinstance(item, dict):
            output_file = str(item.get("output_file") or item.get("path") or item.get("file") or "").strip()
            if not output_file:
                raise ValueError(f"LLM judge artifact spec #{index} is missing output_file/path/file")
            spec = {**defaults, **item}
            spec["output_file"] = output_file
            spec.setdefault("id", Path(output_file).stem or f"artifact_{index}")
        else:
            raise ValueError(f"LLM judge artifact spec #{index} must be a string or object")
        specs.append(spec)
    return specs


def _llm_judge_artifact_specs(task_dir: Path, args: argparse.Namespace, reward: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_arg = getattr(args, "llm_judge_artifacts_file", "")
    if not manifest_arg:
        default_manifest = task_dir / "evaluator" / "llm_judge_artifacts.json"
        if default_manifest.is_file():
            manifest_arg = "evaluator/llm_judge_artifacts.json"
    if manifest_arg:
        manifest_path = Path(manifest_arg)
        if not manifest_path.is_absolute():
            manifest_path = task_dir / manifest_path
        return _normalize_llm_judge_artifact_specs(_read_json_manifest(manifest_path))
    return [{"id": Path(output_file).stem or f"artifact_{index}", "output_file": output_file}
            for index, output_file in enumerate(_infer_output_files_from_checks(task_dir, reward), start=1)]


def _apply_artifact_spec_overrides(judge_args: dict[str, Any], spec: dict[str, Any]) -> None:
    for source_key, target_key in (
        ("artifact_type", "artifact_type"),
        ("type", "artifact_type"),
        ("doc_type", "doc_type"),
        ("criteria_file", "criteria_file"),
        ("rubric_file", "rubric_file"),
        ("criteria", "criteria"),
        ("task_rubrics", "task_rubrics"),
        ("rubric", "rubric"),
        ("reference_file", "reference_file"),
        ("references_file", "references_file"),
        ("reference_manifest", "reference_manifest"),
        ("references_manifest", "references_manifest"),
        ("reference_dir", "reference_dir"),
        ("web_reference_dir", "web_reference_dir"),
        ("judge_output_file", "judge_output_file"),
    ):
        value = spec.get(source_key)
        if value not in (None, ""):
            judge_args[target_key] = value
    extra_args = spec.get("judge_args")
    if isinstance(extra_args, dict):
        judge_args.update(extra_args)


def _write_aggregate_judge_report(task_dir: Path, reward: dict[str, Any], artifact_reports: list[dict[str, Any]]) -> None:
    judged_reports = [item for item in artifact_reports if item.get("status") not in SKIPPED_JUDGE_STATUSES]
    judge_scores = [
        _unit_score(item.get("judge_score", 0.0))
        for item in judged_reports
        if item.get("judge_score") is not None
    ]
    checklist_score = _unit_score(reward.get("partial_pass", 0.0))
    judge_score = round(sum(judge_scores) / len(judge_scores), 4) if judge_scores else 0.0
    failed = [item for item in judged_reports if item.get("status") == "failed"]
    missing = [item for item in judged_reports if item.get("status") == "missing_artifact"]
    if failed:
        status = "failed"
    elif missing:
        status = "partial" if len(missing) < len(judged_reports) else "missing_artifact"
    elif judged_reports:
        status = "ok"
    else:
        skipped_statuses = {str(item.get("status")) for item in artifact_reports}
        status = "skipped_unavailable" if "skipped_unavailable" in skipped_statuses else "skipped_unsupported"
    reason = (
        "Averaged LLM judge scores across supported inferred target artifacts."
        if judged_reports
        else "No inferred target artifacts produced an available LLM judge score."
    )
    report = {
        "schema_version": "1.0",
        "status": status,
        "reason": reason,
        "checklist_score": checklist_score,
        "judge_score": judge_score,
        "final_score": (
            None
            if status in {"failed", "skipped_unavailable"}
            else merge_final_score(reward.get("complete_pass", 0), checklist_score, judge_score)
        ),
        "artifact_reports": artifact_reports,
        "rule_result": reward,
    }
    report_path = task_dir / "run_outputs" / "llm_judge_score.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_checklist_reward(task_dir: Path) -> dict[str, Any]:
    reward_path = task_dir / "run_outputs" / "reward.json"
    if reward_path.is_file():
        try:
            reward = json.loads(reward_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            reward = {}
    else:
        reward = {
            "task_id": task_dir.name,
            "complete_pass": 0,
            "partial_pass": 0.0,
            "checks": [],
            "error": "reward.json was not produced by the checklist evaluator.",
        }
    if not isinstance(reward, dict):
        reward = {}
    reward["partial_pass"] = equal_weight_partial_pass(
        reward.get("checks"), fallback=reward.get("partial_pass", 0.0)
    )
    return reward


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def has_complete_existing_outputs(task_dir: Path, final_reward_file: str) -> bool:
    run_outputs = task_dir / "run_outputs"
    reward = _read_json_object(run_outputs / "reward.json")
    judge = _read_json_object(run_outputs / "llm_judge_score.json")
    final_reward = _read_json_object(task_dir / final_reward_file)
    if reward is None or judge is None or final_reward is None:
        return False
    if "complete_pass" not in reward and "partial_pass" not in reward:
        return False
    if not _is_number(judge.get("judge_score")):
        return False
    if judge.get("status", "ok") not in {"ok"} | SKIPPED_JUDGE_STATUSES:
        return False
    if not _is_number(final_reward.get("llm_judge_score")):
        return False
    if not _is_number(final_reward.get("final_score")):
        return False
    if final_reward.get("llm_judge", {}).get("status", "ok") not in {"ok"} | SKIPPED_JUDGE_STATUSES:
        return False
    return True


def has_existing_run_dir(run_dir: Path) -> bool:
    return run_dir.is_dir()


def run_unified_llm_judge_if_possible(task_dir: Path, args: argparse.Namespace, reward: dict[str, Any]) -> None:
    if args.skip_llm_judge:
        return

    artifact_specs = _llm_judge_artifact_specs(task_dir, args, reward)
    if not artifact_specs:
        return
    output_files = [str(spec["output_file"]) for spec in artifact_specs]

    report_path = task_dir / "run_outputs" / "llm_judge_score.json"
    if report_path.is_file():
        return

    artifact_reports: list[dict[str, Any]] = []
    from dumatebench.evaluator.llm_judge.unified import run_llm_judge_score

    for index, spec in enumerate(artifact_specs, start=1):
        output_file = str(spec["output_file"])
        artifact_path = task_dir / output_file
        artifact_type_override = spec.get("artifact_type") or spec.get("type")
        if not _supports_unified_llm_judge(output_file, str(artifact_type_override) if artifact_type_override else None):
            artifact_reports.append(_skipped_unsupported_artifact_report(output_file, artifact_path.is_file(), str(artifact_type_override) if artifact_type_override else None))
            continue

        artifact_report_file = str(spec.get("judge_output_file") or _judge_report_file_for_output(output_file, index, len(output_files)))
        if not artifact_path.is_file():
            artifact_reports.append(
                {
                    "artifact_id": spec.get("id"),
                    "output_file": output_file,
                    "artifact_type": str(artifact_type_override) if artifact_type_override else _artifact_type(output_file),
                    "artifact_exists": False,
                    "status": "missing_artifact",
                    "reason": f"Expected artifact is missing: {output_file}",
                    "judge_score": 0.0,
                    "judge_report_file": artifact_report_file,
                }
            )
            continue

        judge_args: dict[str, Any] = {
            "output_file": output_file,
            "rule_result": reward,
            "model": args.llm_judge_model,
            "judge_output_file": artifact_report_file,
            "reference_dir": args.reference_dir,
            "web_reference_dir": args.web_reference_dir,
        }
        if args.llm_judge_criteria_file:
            judge_args["criteria_file"] = args.llm_judge_criteria_file
        _apply_artifact_spec_overrides(judge_args, spec)
        if args.llm_judge_base_url:
            judge_args["base_url"] = args.llm_judge_base_url

        try:
            report = run_llm_judge_score(task_dir, judge_args)
            if not isinstance(report, dict):
                report = _load_json_file(task_dir / artifact_report_file) or {}
            artifact_reports.append(
                {
                    "artifact_id": spec.get("id"),
                    "output_file": output_file,
                    "artifact_type": str(artifact_type_override) if artifact_type_override else _artifact_type(output_file),
                    "artifact_exists": True,
                    "status": str(report.get("status", "ok")),
                    "reason": str(report.get("reason", "Unified LLM judge completed.")),
                    "judge_score": _optional_unit_score(report.get("judge_score", report.get("final_score", 0.0))),
                    "judge_report_file": artifact_report_file,
                    "report": report,
                }
            )
        except Exception as exc:
            failure = {
                "schema_version": "1.0",
                "status": "failed",
                "reason": f"Unified LLM judge failed: {type(exc).__name__}: {exc}",
                "artifact_type": str(artifact_type_override) if artifact_type_override else _artifact_type(output_file),
                "artifact_id": spec.get("id"),
                "output_file": output_file,
                "checklist_score": _unit_score(reward.get("partial_pass", 0.0)),
                "judge_score": 0.0,
                "final_score": round(_unit_score(reward.get("partial_pass", 0.0)) / 2.0, 4),
                "pass": False,
                "rule_result": reward,
            }
            failure_path = task_dir / artifact_report_file
            failure_path.parent.mkdir(parents=True, exist_ok=True)
            failure_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            artifact_reports.append(
                {
                    "output_file": output_file,
                    "artifact_type": _artifact_type(output_file),
                    "artifact_exists": True,
                    "status": "failed",
                    "reason": failure["reason"],
                    "judge_score": 0.0,
                    "judge_report_file": artifact_report_file,
                    "report": failure,
                }
            )

    _write_aggregate_judge_report(task_dir, reward, artifact_reports)


def write_final_reward(task_dir: Path, output_file: str) -> str | None:
    reward = read_checklist_reward(task_dir)

    inferred_outputs = _infer_output_files_from_checks(task_dir, reward)
    inferred_output = inferred_outputs[0] if inferred_outputs else None
    checklist_score = _unit_score(reward.get("partial_pass", 0.0))
    artifact_states = [
        {
            "output_file": item,
            "artifact_type": _artifact_type(item),
            "artifact_exists": (task_dir / item).is_file(),
        }
        for item in inferred_outputs
    ]
    artifact_exists = bool(artifact_states and all(item["artifact_exists"] for item in artifact_states))
    judge_score = 0.0
    judge_status = "not_run"
    reason = "LLM judge report was not produced."
    judge_report = None
    if not inferred_outputs:
        judge_status = "no_output_path_inferred"
        reason = "Could not infer the expected artifact path from checklist results."
    elif not artifact_exists:
        missing = [item["output_file"] for item in artifact_states if not item["artifact_exists"]]
        judge_score, judge_status, reason, judge_report = _load_existing_judge_score(task_dir)
        if judge_status == "not_run":
            judge_status = "missing_artifact"
            reason = "Expected artifact is missing: " + ", ".join(missing)
    else:
        judge_score, judge_status, reason, judge_report = _load_existing_judge_score(task_dir)

    final_score = (
        None
        if judge_status in {"failed", "skipped_unavailable"} or judge_score is None
        else merge_final_score(reward.get("complete_pass", 0), checklist_score, judge_score)
    )
    combined = dict(reward)
    combined["base_complete_pass"] = reward.get("complete_pass", 0)
    combined["base_partial_pass"] = checklist_score
    combined["llm_judge"] = {
        "score": judge_score,
        "status": judge_status,
        "reason": reason,
        "artifact_type": _artifact_type(inferred_output),
        "output_file": inferred_output,
        "output_files": inferred_outputs,
        "artifact_exists": artifact_exists,
        "artifacts": artifact_states,
    }
    if judge_report is not None:
        combined["llm_judge"]["report"] = judge_report
    combined["llm_judge_score"] = judge_score
    combined["final_score"] = final_score
    combined["complete_pass_with_llm_judge"] = int(final_score is not None and final_score >= 0.7)
    combined["partial_pass_with_llm_judge"] = final_score

    final_path = task_dir / output_file
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(final_path)


def run_task(task_dir: Path, runtime_dir: Path, args: argparse.Namespace, run_id: str, run_dir: Path) -> TaskResult:
    started = time.time()
    compose_file = runtime_dir / "docker-compose.yaml"
    run_outputs = run_dir / "run_outputs"
    run_logs = run_dir / "run_logs"
    _clear_directory(run_outputs)
    _clear_directory(run_logs)
    task_view = prepare_task_view(task_dir, run_dir)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["DUMATE_EVALUATE_PY"] = env.get("DUMATE_EVALUATE_PY", str(ROOT / "evaluator/evaluate.py"))
    env["DUMATE_AGENT_BACKEND"] = args.agent_backend
    env["DUMATE_CLAUDE_CODE_VERSION"] = args.claude_code_version
    env["DUMATE_CODEX_VERSION"] = args.codex_version
    env["COMPOSE_PROJECT_NAME"] = compose_project_name_for_run(run_dir)
    (run_logs / "compose_project_name.txt").write_text(env["COMPOSE_PROJECT_NAME"] + "\n", encoding="utf-8")

    agent_rc: int | None = None
    evaluator_rc: int | None = None
    try:
        run_command(["docker", "compose", "-f", str(compose_file), "down", "--remove-orphans"], cwd=run_dir, env=env)
        if not args.no_build:
            build_log = run_logs / "docker_build.log"
            with build_log.open("w", encoding="utf-8") as handle:
                build = subprocess.run(
                    ["docker", "compose", "-f", str(compose_file), "build"],
                    cwd=run_dir,
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            if build.returncode != 0:
                final_reward_path = write_final_reward(task_view, args.final_reward_file)
                return TaskResult(
                    task_dir.name,
                    str(task_dir),
                    run_id,
                    str(run_dir),
                    "build_failed",
                    None,
                    None,
                    time.time() - started,
                    None,
                    final_reward_path,
                )

        agent_cmd = build_agent_command(compose_file, args, env)
        (run_logs / "batch_agent_command.json").write_text(json.dumps(agent_cmd, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        agent_rc = run_command(agent_cmd, cwd=run_dir, env=env)
        evaluator_cmd = [*shlex.split(args.evaluator_python), "evaluator/evaluator.py", "--task-dir", str(task_view)]
        evaluator_rc = run_command(evaluator_cmd, cwd=task_view, env=env)
        run_unified_llm_judge_if_possible(task_view, args, read_checklist_reward(task_view))
        final_reward_path = write_final_reward(task_view, args.final_reward_file)
        agent_status_path = run_logs / "agent_status.json"
        try:
            agent_status = json.loads(agent_status_path.read_text(encoding="utf-8")) if agent_status_path.is_file() else {}
        except json.JSONDecodeError:
            agent_status = {}
        agent_incomplete = agent_status.get("agent_finished") is False and bool(agent_status.get("max_steps_reached"))
        if agent_rc == 0 and evaluator_rc == 0 and not agent_incomplete:
            status = "ok"
        elif agent_incomplete:
            status = "agent_incomplete"
        else:
            status = "failed"
        return TaskResult(
            task_dir.name,
            str(task_dir),
            run_id,
            str(run_dir),
            status,
            agent_rc,
            evaluator_rc,
            time.time() - started,
            str(run_outputs / "reward.json") if (run_outputs / "reward.json").is_file() else None,
            final_reward_path,
        )
    except Exception as exc:
        run_unified_llm_judge_if_possible(task_view, args, read_checklist_reward(task_view))
        final_reward_path = write_final_reward(task_view, args.final_reward_file)
        return TaskResult(task_dir.name, str(task_dir), run_id, str(run_dir), "error", agent_rc, evaluator_rc, time.time() - started, None, final_reward_path, str(exc))
    finally:
        compose_log = run_logs / "compose.log"
        with compose_log.open("w", encoding="utf-8") as handle:
            subprocess.run(["docker", "compose", "-f", str(compose_file), "logs", "--no-color"], cwd=run_dir, env=env, stdout=handle, stderr=subprocess.STDOUT, check=False)
        run_command(["docker", "compose", "-f", str(compose_file), "down", "--remove-orphans"], cwd=run_dir, env=env)


def run_task_with_memory_retries(
    task_dir: Path,
    template_task: Path,
    args: argparse.Namespace,
    run_id: str,
    run_dir: Path,
    display_name: str,
) -> TaskExecution:
    memory_attempts = _memory_attempts_for_task(task_dir, args)
    last_result: TaskResult | None = None
    for index, memory_mb in enumerate(memory_attempts, start=1):
        runtime_dir = prepare_runtime(
            task_dir,
            template_task,
            args.runtime_name,
            args.image_prefix,
            agent_backend=args.agent_backend,
            runtime_parent=run_dir,
            output_dir=run_dir / "run_outputs",
            logs_dir=run_dir / "run_logs",
            reuse_template_setup=args.reuse_template_setup,
            memory_mb_override=memory_mb,
        )
        memory_label = f" memory={memory_mb}MB" if memory_mb is not None else ""
        retry_label = f" attempt={index}/{len(memory_attempts)}" if len(memory_attempts) > 1 else ""
        print(f"[run] {display_name}{memory_label}{retry_label}")
        result = run_task(task_dir, runtime_dir, args, run_id, run_dir)
        print(f"[{result.status}] {display_name} agent={result.agent_returncode} evaluator={result.evaluator_returncode}{memory_label}")
        if result.agent_returncode != 137 or index == len(memory_attempts):
            return TaskExecution(result=result, memory_mb=memory_mb, attempt=index, attempts=index)
        print(f"[retry-137] {display_name}: agent returned 137; retrying with next memory limit", flush=True)
        last_result = result

    assert last_result is not None
    return TaskExecution(result=last_result, memory_mb=memory_attempts[-1], attempt=len(memory_attempts), attempts=len(memory_attempts))


def run_task_with_agent_1_retries(
    task_dir: Path,
    template_task: Path,
    args: argparse.Namespace,
    run_id: str,
    run_dir: Path,
    display_name: str,
) -> TaskExecution:
    max_attempts = 3
    execution: TaskExecution | None = None
    for attempt in range(1, max_attempts + 1):
        execution = run_task_with_memory_retries(
            task_dir,
            template_task,
            args,
            run_id,
            run_dir,
            display_name,
        )
        if execution.result.agent_returncode != 1 or attempt == max_attempts:
            return execution
        print(
            f"[retry-1] {display_name}: agent returned 1; retrying task "
            f"({attempt}/{max_attempts - 1} retries used)",
            flush=True,
        )

    assert execution is not None
    return execution


def args_with_agent_timeout(args: argparse.Namespace, timeout_seconds: int) -> argparse.Namespace:
    values = vars(args).copy()
    values["agent_timeout"] = timeout_seconds
    return argparse.Namespace(**values)


def run_task_with_timeout_retries(
    task_dir: Path,
    template_task: Path,
    args: argparse.Namespace,
    run_id: str,
    run_dir: Path,
    display_name: str,
) -> TaskExecution:
    execution = run_task_with_agent_1_retries(
        task_dir,
        template_task,
        args,
        run_id,
        run_dir,
        display_name,
    )
    agent_returncode = execution.result.agent_returncode
    if agent_returncode in {0, 1, None}:
        return execution

    relaxed_timeout = max(int(args.agent_timeout or 0), 1800)
    timeout_args = args_with_agent_timeout(args, relaxed_timeout)
    print(
        f"[retry-timeout] {display_name}: agent returned {agent_returncode}; "
        f"retrying once with agent timeout {relaxed_timeout}s",
        flush=True,
    )
    return run_task_with_agent_1_retries(
        task_dir,
        template_task,
        timeout_args,
        run_id,
        run_dir,
        display_name,
    )


def write_successful_memory_record(
    memory_summary_file: Path,
    execution: TaskExecution,
    args: argparse.Namespace,
) -> None:
    result = execution.result
    if result.status != "ok":
        return
    _append_jsonl(
        memory_summary_file,
        {
            "task_id": result.task_id,
            "task_dir": result.task_dir,
            "run_id": result.run_id,
            "run_dir": result.run_dir,
            "status": result.status,
            "agent_backend": args.agent_backend,
            "agent_model": args.agent_model,
            "memory_mb": execution.memory_mb,
            "attempt": execution.attempt,
            "attempts": execution.attempts,
            "agent_returncode": result.agent_returncode,
            "evaluator_returncode": result.evaluator_returncode,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
        },
    )


def execute_batch_task(
    task_dir: Path,
    *,
    tasks_dir: Path,
    template_task: Path,
    batch_run_dir: Path,
    run_id: str,
    args: argparse.Namespace,
) -> tuple[TaskResult, TaskExecution | None]:
    """Execute one isolated task without writing shared batch-level files."""
    display_name = task_display_name(task_dir, tasks_dir)
    run_dir = batch_run_dir / task_run_name(task_dir, tasks_dir)
    task_view = run_dir / "task_view"
    if args.skip_existing and has_existing_run_dir(run_dir):
        return (
            TaskResult(
                display_name,
                str(task_dir),
                run_id,
                str(run_dir),
                "skipped_existing",
                None,
                None,
                0.0,
                str(run_dir / "run_outputs/reward.json")
                if (run_dir / "run_outputs/reward.json").is_file()
                else None,
                str(task_view / args.final_reward_file)
                if (task_view / args.final_reward_file).is_file()
                else None,
            ),
            None,
        )

    started = time.time()
    try:
        if args.dry_run:
            runtime_dir = run_dir / ".batch_runtime" / args.runtime_name
            result = TaskResult(
                display_name,
                str(task_dir),
                run_id,
                str(run_dir),
                "dry_run",
                None,
                None,
                0.0,
                None,
            )
            print(f"[dry-run] {display_name}: run={run_dir} runtime={runtime_dir}", flush=True)
            return result, None

        (run_dir / "run_outputs").mkdir(parents=True, exist_ok=True)
        (run_dir / "run_logs").mkdir(parents=True, exist_ok=True)
        execution = run_task_with_timeout_retries(
            task_dir,
            template_task,
            args,
            run_id,
            run_dir,
            display_name,
        )
        return execution.result, execution
    except Exception as exc:  # noqa: BLE001 - batch mode records task-level failures and continues.
        result = TaskResult(
            display_name,
            str(task_dir),
            run_id,
            str(run_dir),
            "error",
            None,
            None,
            time.time() - started,
            None,
            None,
            f"{type(exc).__name__}: {exc}",
        )
        print(f"[error] {display_name}: {result.error}", file=sys.stderr, flush=True)
        return result, None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks_dir = Path(args.tasks_dir).expanduser().resolve()
    template_task = Path(args.template_task).expanduser().resolve()
    run_id = args.run_id or default_run_id(args)
    runs_root = Path(args.runs_root).expanduser().resolve() if args.runs_root else default_runs_root()
    batch_run_dir = runs_root / run_id
    summary_file = Path(args.summary_file).expanduser().resolve() if args.summary_file else batch_run_dir / "batch_summary.jsonl"
    memory_summary_file = (
        Path(args.native_memory_summary_file).expanduser().resolve()
        if args.native_memory_summary_file
        else batch_run_dir / "native_memory_summary.jsonl"
    )
    tasks = [
        task
        for task in discover_tasks(tasks_dir, args.task_glob, 0, recursive=args.recursive)
        if task.resolve() != template_task
    ]
    rerun_specs: list[tuple[Path, Any, str]] = []
    relaxed_timeout_task_paths: set[Path] = set()
    if args.rerun_agent_137_summary:
        rerun_specs.append((
            Path(args.rerun_agent_137_summary).expanduser().resolve(),
            lambda agent_returncode: agent_returncode == 137,
            "rerun_agent_137_summary",
        ))
    if args.rerun_agent_1_summary:
        rerun_specs.append((
            Path(args.rerun_agent_1_summary).expanduser().resolve(),
            lambda agent_returncode: agent_returncode == 1,
            "rerun_agent_1_summary",
        ))
    if args.rerun_agent_timeout_summary:
        rerun_specs.append((
            Path(args.rerun_agent_timeout_summary).expanduser().resolve(),
            lambda agent_returncode: agent_returncode not in {0, 1, None},
            "rerun_agent_timeout_summary",
        ))
    if rerun_specs:
        matched_task_paths: set[Path] = set()
        for rerun_summary, should_rerun, option_name in rerun_specs:
            matched = {
                task.resolve()
                for task in _filter_tasks_for_agent_returncode_rerun(
                    tasks,
                    rerun_summary,
                    tasks_dir,
                    should_rerun,
                    option_name,
                )
            }
            matched_task_paths.update(matched)
            if option_name == "rerun_agent_timeout_summary":
                relaxed_timeout_task_paths.update(matched)
        tasks = [task for task in tasks if task.resolve() in matched_task_paths]
    if args.dedupe_by_name:
        tasks = dedupe_tasks_by_name(tasks)
    if args.limit > 0:
        tasks = tasks[: args.limit]

    if not template_task.is_dir():
        raise SystemExit(f"template task not found: {template_task}")
    if not tasks:
        raise SystemExit(f"no task directories found under {tasks_dir} matching {args.task_glob!r}")

    print(f"template: {template_task}")
    print(f"tasks: {len(tasks)}")
    print(f"run_id: {run_id}")
    print(f"run_dir: {batch_run_dir}")
    print(f"workers: {args.workers}")
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        summary_file.write_text("", encoding="utf-8")
        if _native_backend_uses_memory_retry(args.agent_backend):
            memory_summary_file.parent.mkdir(parents=True, exist_ok=True)
            memory_summary_file.write_text("", encoding="utf-8")

    failures = 0

    def execution_args_for_task(task_dir: Path) -> argparse.Namespace:
        if task_dir.resolve() not in relaxed_timeout_task_paths:
            return args
        return args_with_agent_timeout(args, max(int(args.agent_timeout or 0), 1800))

    def record_outcome(outcome: tuple[TaskResult, TaskExecution | None]) -> bool:
        nonlocal failures
        result, execution = outcome
        if (
            not args.dry_run
            and execution is not None
            and _native_backend_uses_memory_retry(args.agent_backend)
        ):
            write_successful_memory_record(memory_summary_file, execution, args)
        if not args.dry_run:
            _append_jsonl(summary_file, result.as_dict())
        failed = result.status not in {"ok", "dry_run", "skipped_existing"}
        if failed:
            failures += 1
        return failed

    def submit_task(
        executor: ThreadPoolExecutor,
        task_dir: Path,
    ) -> Future[tuple[TaskResult, TaskExecution | None]]:
        return executor.submit(
            execute_batch_task,
            task_dir,
            tasks_dir=tasks_dir,
            template_task=template_task,
            batch_run_dir=batch_run_dir,
            run_id=run_id,
            args=execution_args_for_task(task_dir),
        )

    if args.workers == 1:
        for task_dir in tasks:
            failed = record_outcome(
                execute_batch_task(
                    task_dir,
                    tasks_dir=tasks_dir,
                    template_task=template_task,
                    batch_run_dir=batch_run_dir,
                    run_id=run_id,
                    args=execution_args_for_task(task_dir),
                )
            )
            if failed and args.stop_on_failure:
                break
    else:
        task_iterator = iter(tasks)
        pending: dict[Future[tuple[TaskResult, TaskExecution | None]], Path] = {}
        stop_submitting = False
        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="dumate-task") as executor:
            for _ in range(min(args.workers, len(tasks))):
                task_dir = next(task_iterator)
                pending[submit_task(executor, task_dir)] = task_dir

            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    pending.pop(future)
                    failed = record_outcome(future.result())
                    if failed and args.stop_on_failure:
                        stop_submitting = True

                if not stop_submitting:
                    for _ in range(len(completed)):
                        try:
                            task_dir = next(task_iterator)
                        except StopIteration:
                            break
                        pending[submit_task(executor, task_dir)] = task_dir

    print(f"summary: {summary_file}")
    if not args.dry_run and _native_backend_uses_memory_retry(args.agent_backend):
        print(f"native_memory_summary: {memory_summary_file}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
