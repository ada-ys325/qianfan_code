#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


MAX_OBS_CHARS = 6000
AGENT_PATH = "/opt/dumate/wrappers:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SYSTEM_PROMPT = (
    "You are a command-line agent running a DuMateBench task inside Docker. "
    "The task instruction gives the required final artifact path or filename. "
    "Write final deliverables to the requested path, normally under /outputs. "
    "The workspace may contain user.md, soul.md, and session_chat_history.json; "
    "these files contain user preferences, profile/configuration, and current-session interaction history "
    "that may help you understand the user's needs and complete the task. "
    "Use sudo if you need to install packages. "
    "The container may have real network-stack faults and tool-wrapper API faults. "
    "Use benchmark-provided tools by their command names as exposed in PATH; do not call "
    "their absolute system paths or rewrite PATH to bypass wrappers. "
    "Use tesseract or ocr_extract for OCR when OCR is needed. "
    "Use calendar_write to create final .ics calendar artifacts. "
    "Use mail_send when a task asks you to send email. "
    "If a command fails because of transient network or wrapped-tool errors, observe the error "
    "and recover by retrying, waiting, reinstalling missing tools, or switching command-line strategies. "
    "Before finishing, verify that every required final artifact exists at the requested path. "
    "Only after verification should you respond with finish=true."
)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def truncate(text: str, limit: int = MAX_OBS_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def compose(task_dir: Path) -> list[str]:
    return ["docker", "compose", "-f", str(task_dir / "environment" / "docker-compose.yaml")]


def reset_dir(path: Path) -> None:
    path.mkdir(exist_ok=True)
    for child in path.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def write_log(log_path: Path, record: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def exec_in_task(task_dir: Path, command: str) -> dict[str, Any]:
    cmd = compose(task_dir) + [
        "exec",
        "-T",
        "--user",
        "agent",
        "task",
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
    result = run(cmd, cwd=task_dir, check=False)
    elapsed = round(time.time() - start, 3)
    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += ("\nSTDERR:\n" + result.stderr)
    return {"returncode": result.returncode, "elapsed_sec": elapsed, "output": truncate(output)}


def parse_action(content: str) -> dict[str, Any]:
    try:
        action = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Adapter returned non-JSON output: {content}") from exc
    if action.get("finish"):
        return {"finish": True, "reason": action.get("reason", "")}
    command = action.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"Adapter output must contain a non-empty command or finish=true: {content}")
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
        raise RuntimeError(
            f"Agent adapter failed ({result.returncode}): {agent_cmd}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return parse_action(result.stdout.strip())


def build_state(task_dir: Path, step: int, max_steps: int, history: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "step": step,
        "max_steps": max_steps,
        "instruction": (task_dir / "instruction.md").read_text(),
        "system_prompt": SYSTEM_PROMPT,
        "history": history,
        "last_observation": history[-1]["observation"] if history else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a DuMateBench task with an external agent adapter.")
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--agent-cmd", required=True, help="Command that reads adapter JSON from stdin and writes action JSON to stdout.")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--adapter-timeout", type=int, default=180)
    parser.add_argument("--keep-containers", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    task_dir = args.task_dir.resolve()
    run_outputs = task_dir / "run_outputs"
    run_logs = task_dir / "run_logs"
    status_log = run_logs / "agent_status.json"
    adapter_log = run_logs / "agent_adapter.jsonl"
    history: list[dict[str, Any]] = []
    status: dict[str, Any] = {
        "agent_finished": False,
        "finish_reason": None,
        "max_steps_reached": False,
        "steps": 0,
        "evaluator_returncode": None,
        "adapter_command": args.agent_cmd,
    }

    reset_dir(run_outputs)
    reset_dir(run_logs)

    try:
        run(compose(task_dir) + ["down", "--remove-orphans"], cwd=task_dir, check=False)
        if not args.no_build:
            run(compose(task_dir) + ["build"], cwd=task_dir, capture=False)
        run(compose(task_dir) + ["up", "-d", "task"], cwd=task_dir, capture=False)

        for step in range(1, args.max_steps + 1):
            status["steps"] = step
            state = build_state(task_dir, step, args.max_steps, history)
            action = call_adapter(args.agent_cmd, state, Path.cwd(), args.adapter_timeout)
            write_log(adapter_log, {"step": step, "action": action})

            if action["finish"]:
                status["agent_finished"] = True
                status["finish_reason"] = action.get("reason", "")
                break

            observation = exec_in_task(task_dir, action["command"])
            history.append({"action": action, "observation": observation})
            write_log(adapter_log, {"step": step, "observation": observation})
        else:
            status["max_steps_reached"] = True
            status["finish_reason"] = f"Agent reached max steps ({args.max_steps}) without finish=true."

        result = run(
            [sys.executable, str(task_dir / "evaluator" / "evaluator.py"), "--task-dir", str(task_dir)],
            check=False,
        )
        status["evaluator_returncode"] = result.returncode
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode
    finally:
        status_log.write_text(json.dumps(status, indent=2, ensure_ascii=False))
        logs = run(compose(task_dir) + ["logs", "--no-color"], cwd=task_dir, check=False).stdout
        (run_logs / "compose.log").write_text(logs)
        if not args.keep_containers:
            run(compose(task_dir) + ["down", "--remove-orphans"], cwd=task_dir, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
