"""Agent adapter protocol: run one task with an external agent executable.

The wire protocol is defined in ``dumatebench/agents/agent_contract.md`` and is
unchanged here. This module wraps the same stdin/stdout JSON loop implemented
in ``dumatebench/agents/adapter_runner.py`` so it can be called in-process by
the batch runner instead of only as a standalone script.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_OBS_CHARS = 6000
AGENT_PATH = "/opt/dumate/wrappers:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MAIN_SERVICE = "main"
LEGACY_SERVICE = "task"

# Generated fresh into environment/ on every `dumate run` (overwritten each
# time, never checked in) since template.py no longer emits a task-authored
# docker-compose.yaml -- see template.py's module docstring. Service name and
# build context intentionally mirror Harbor's own base compose overlay
# (`MAIN_SERVICE_NAME = "main"`, context hardcoded to `environment/`) so the
# same Dockerfile works unmodified under both `dumate run` and `harbor run`.
_COMPOSE_FILENAME = ".dumate-compose.yaml"
_COMPOSE_TEMPLATE = """services:
  {service}:
    build:
      context: .
      dockerfile: Dockerfile
    init: true
    command: ["sleep", "infinity"]
    volumes:
      - ../run_outputs:/outputs
      - ../run_logs:/logs
"""

DEFAULT_SYSTEM_PROMPT = (
    "You are a command-line agent running a DuMateBench task inside Docker. "
    "The task instruction gives the required final artifact path or filename. "
    "Write final deliverables to the requested path, normally under /outputs. "
    "Use sudo if you need to install packages. "
    "The container may have real network-stack faults and tool-wrapper API faults. "
    "Use benchmark-provided tools by their command names as exposed in PATH; do not call "
    "their absolute system paths or rewrite PATH to bypass wrappers. "
    "If a command fails because of transient network or wrapped-tool errors, observe the error "
    "and recover by retrying, waiting, reinstalling missing tools, or switching command-line strategies. "
    "Before finishing, verify that every required final artifact exists at the requested path. "
    "Only after verification should you respond with finish=true."
)


class AdapterError(RuntimeError):
    """Raised when the agent adapter violates the stdin/stdout protocol."""


@dataclass
class StepRecord:
    action: dict[str, Any]
    observation: dict[str, Any] | None = None


@dataclass
class AdapterRunResult:
    finished: bool
    finish_reason: str | None
    max_steps_reached: bool
    steps_taken: int
    history: list[StepRecord] = field(default_factory=list)

    def as_status_dict(self) -> dict[str, Any]:
        return {
            "agent_finished": self.finished,
            "finish_reason": self.finish_reason,
            "max_steps_reached": self.max_steps_reached,
            "steps": self.steps_taken,
        }


def compose_service(task_dir: Path) -> str:
    """Return the service used by the task's Docker Compose definition."""
    if (task_dir / "environment" / "docker-compose.yaml").is_file():
        return LEGACY_SERVICE
    return MAIN_SERVICE


def compose_cmd(task_dir: Path) -> list[str]:
    """Return the ``docker compose`` invocation for a task's ``environment/``.

    Writes (or overwrites) a minimal compose file into ``environment/``
    defining the ``main`` service on every call, since ``template.py`` no
    longer generates a task-authored ``docker-compose.yaml`` -- the build
    context for that service is ``environment/`` itself, matching Harbor's
    own hardcoded ``main``-service context (see ``template.py``'s module
    docstring).
    """
    environment_dir = task_dir / "environment"
    authored_compose = environment_dir / "docker-compose.yaml"
    if authored_compose.is_file():
        return ["docker", "compose", "-f", str(authored_compose)]
    compose_path = environment_dir / _COMPOSE_FILENAME
    compose_path.write_text(_COMPOSE_TEMPLATE.format(service=MAIN_SERVICE), encoding="utf-8")
    return ["docker", "compose", "-f", str(compose_path)]


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def _truncate(text: str, limit: int = MAX_OBS_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def exec_in_task(task_dir: Path, command: str) -> dict[str, Any]:
    cmd = compose_cmd(task_dir) + [
        "exec",
        "-T",
        "--user",
        "agent",
        compose_service(task_dir),
        "env",
        "HOME=/home/agent",
        f"PATH={AGENT_PATH}",
        "DUMATE_TOOL_FAULT_CONFIG=/opt/dumate/tool_faults.yaml",
        "DUMATE_TOOL_FAULT_LOG=/logs/tool_faults.jsonl",
        "bash",
        "--noprofile",
        "--norc",
        "-c",
        command,
    ]
    start = time.time()
    result = _run(cmd, cwd=task_dir, check=False)
    elapsed = round(time.time() - start, 3)
    output = result.stdout or ""
    if result.stderr:
        output += "\nSTDERR:\n" + result.stderr
    return {"returncode": result.returncode, "elapsed_sec": elapsed, "output": _truncate(output)}


def parse_action(content: str) -> dict[str, Any]:
    try:
        action = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Adapter returned non-JSON output: {content}") from exc
    if action.get("finish"):
        return {"finish": True, "reason": action.get("reason", "")}
    command = action.get("command")
    if not isinstance(command, str) or not command.strip():
        raise AdapterError(f"Adapter output must contain a non-empty command or finish=true: {content}")
    return {"finish": False, "command": command.strip(), "reason": action.get("reason", "")}


def call_adapter(agent_cmd: str, state: dict[str, Any], cwd: Path, timeout: int) -> dict[str, Any]:
    result = subprocess.run(
        shlex.split(agent_cmd),
        input=json.dumps(state, ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AdapterError(
            f"Agent adapter failed ({result.returncode}): {agent_cmd}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return parse_action(result.stdout.strip())


def build_state(
    instruction: str,
    step: int,
    max_steps: int,
    history: list[StepRecord],
    system_prompt: str,
) -> dict[str, Any]:
    history_payload = [
        {"action": record.action, "observation": record.observation}
        for record in history
    ]
    return {
        "schema_version": "0.1",
        "step": step,
        "max_steps": max_steps,
        "instruction": instruction,
        "system_prompt": system_prompt,
        "history": history_payload,
        "last_observation": history[-1].observation if history else None,
    }


def run_adapter_loop(
    task_dir: Path,
    agent_cmd: str,
    max_steps: int,
    adapter_timeout: int,
    step_log_cb: Any = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> AdapterRunResult:
    """Drive one task container against an agent adapter until finish or max_steps.

    Assumes the task container is already built and started (``docker compose up``);
    callers own image build/up/down lifecycle so this function can be reused across
    single-task and batch execution paths.
    """
    instruction = (task_dir / "instruction.md").read_text()
    history: list[StepRecord] = []
    finished = False
    finish_reason: str | None = None
    max_steps_reached = False
    step = 0

    for step in range(1, max_steps + 1):
        state = build_state(instruction, step, max_steps, history, system_prompt)
        action = call_adapter(agent_cmd, state, Path.cwd(), adapter_timeout)
        if step_log_cb:
            step_log_cb({"step": step, "action": action})

        if action["finish"]:
            finished = True
            finish_reason = action.get("reason", "")
            break

        observation = exec_in_task(task_dir, action["command"])
        record = StepRecord(action=action, observation=observation)
        history.append(record)
        if step_log_cb:
            step_log_cb({"step": step, "observation": observation})
    else:
        max_steps_reached = True
        finish_reason = f"Agent reached max steps ({max_steps}) without finish=true."

    return AdapterRunResult(
        finished=finished,
        finish_reason=finish_reason,
        max_steps_reached=max_steps_reached,
        steps_taken=step,
        history=history,
    )


def reset_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
