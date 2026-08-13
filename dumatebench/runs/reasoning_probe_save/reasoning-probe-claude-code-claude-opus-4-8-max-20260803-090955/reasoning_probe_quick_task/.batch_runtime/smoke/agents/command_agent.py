#!/usr/bin/env python3
import argparse
import json
import os
import socket
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path


DEFAULT_TASK = Path(__file__).resolve().parents[1] / "datasets" / "dev" / "odyssey_2_12_smoke"
MAX_OBS_CHARS = 6000
DEFAULT_TRUSTED_BASE_URLS = {
    "https://api.openai.com/v1",
    "https://cn.huayanapi.com:27502/v1",
}
RETRIABLE_LLM_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300


def env_int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def run(cmd, cwd=None, check=True, capture=True):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=capture,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def run_with_timeout(cmd, cwd=None, timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS):
    start = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, 15)
            stdout, stderr = proc.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        returncode = 124
    elapsed = round(time.time() - start, 3)
    return subprocess.CompletedProcess(
        cmd,
        returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    ), elapsed, timed_out


def truncate(text, limit=MAX_OBS_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def load_task_prompt(task_dir):
    return (task_dir / "instruction.md").read_text()


def normalize_base_url(url):
    return url.rstrip("/")


def load_trusted_base_urls(extra_values):
    trusted = {normalize_base_url(url) for url in DEFAULT_TRUSTED_BASE_URLS}
    env_value = os.environ.get("DUMATE_TRUSTED_BASE_URLS", "")
    for raw in env_value.split(","):
        raw = raw.strip()
        if raw:
            trusted.add(normalize_base_url(raw))
    for raw in extra_values:
        if raw:
            trusted.add(normalize_base_url(raw))
    return trusted


def validate_trusted_base_url(base_url, trusted):
    normalized = normalize_base_url(base_url)
    if normalized not in trusted:
        trusted_list = ", ".join(sorted(trusted))
        raise SystemExit(
            f"OPENAI_BASE_URL is not trusted by this DuMateBench runner: {normalized}\n"
            f"Trusted base URLs: {trusted_list}\n"
            "Add it with --trusted-base-url or DUMATE_TRUSTED_BASE_URLS."
        )
    parsed = urlparse(normalized)
    if parsed.scheme != "https":
        raise SystemExit(f"Trusted LLM base_url must use https: {normalized}")
    return normalized


def _usage_numbers(usage):
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = float(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = float(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = float(usage.get("total_tokens") or input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _compact_number(value):
    return int(value) if float(value).is_integer() else round(value, 4)


def chat_completion(messages, model, base_url, api_key):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    max_attempts = max(1, env_int("DUMATE_LLM_API_RETRIES", 4))
    initial_delay = max(0.0, env_float("DUMATE_LLM_API_RETRY_INITIAL_DELAY", 2.0))
    max_delay = max(initial_delay, env_float("DUMATE_LLM_API_RETRY_MAX_DELAY", 30.0))
    url = f"{base_url.rstrip('/')}/chat/completions"
    last_error = None
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read().decode("utf-8")
            payload = json.loads(body)
            content = payload["choices"][0]["message"]["content"]
            return content, payload.get("usage", {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"LLM API HTTP {exc.code}: {body}")
            should_retry = exc.code in RETRIABLE_LLM_HTTP_STATUS
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = RuntimeError(f"LLM API request failed: {exc}")
            should_retry = True

        if not should_retry or attempt >= max_attempts:
            raise last_error
        delay = min(max_delay, initial_delay * (2 ** (attempt - 1)))
        print(
            f"[llm] transient API failure on attempt {attempt}/{max_attempts}; "
            f"retrying in {delay:.1f}s: {last_error}",
            file=sys.stderr,
        )
        time.sleep(delay)
    raise last_error or RuntimeError("LLM API request failed without an error detail.")


def parse_action(content):
    try:
        action = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned non-JSON content: {content}") from exc
    if action.get("finish"):
        return {"finish": True, "reason": action.get("reason", "")}
    command = action.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"JSON response must contain a non-empty command or finish=true: {content}")
    return {"finish": False, "command": command.strip(), "reason": action.get("reason", "")}


def compose(task_dir):
    return ["docker", "compose", "-f", str(task_dir / "environment" / "docker-compose.yaml")]


def exec_in_task(task_dir, command):
    AGENT_PATH = "/opt/dumate/wrappers:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    timeout = max(1, env_int("DUMATE_AGENT_COMMAND_TIMEOUT_SECONDS", DEFAULT_COMMAND_TIMEOUT_SECONDS))

    cmd = compose(task_dir) + [
        "exec", "-T", "--user", "agent", "task",
        "env",
        "HOME=/home/agent",
        f"PATH={AGENT_PATH}",
        "DUMATE_TOOL_FAULT_CONFIG=/opt/dumate/tool_faults.yaml",
        "DUMATE_TOOL_FAULT_LOG=/logs/tool_faults.jsonl",
        "bash", "--noprofile", "--norc", "-c", command,
    ]
    result, elapsed, timed_out = run_with_timeout(cmd, cwd=task_dir, timeout=timeout)
    output = ""
    if timed_out:
        output += f"Command timed out after {timeout} seconds and was terminated.\n"
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += ("\nSTDERR:\n" + result.stderr)
    return {
        "returncode": result.returncode,
        "elapsed_sec": elapsed,
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        "output": truncate(output),
    }


def exec_local_as_agent(command):
    AGENT_PATH = "/opt/dumate/wrappers:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    timeout = max(1, env_int("DUMATE_AGENT_COMMAND_TIMEOUT_SECONDS", DEFAULT_COMMAND_TIMEOUT_SECONDS))

    cmd = [
        "sudo", "-H", "-u", "agent",
        "env",
        "HOME=/home/agent",
        f"PATH={AGENT_PATH}",
        "DUMATE_TOOL_FAULT_CONFIG=/opt/dumate/tool_faults.yaml",
        "DUMATE_TOOL_FAULT_LOG=/logs/tool_faults.jsonl",
        "bash", "--noprofile", "--norc", "-c", command,
    ]
    result, elapsed, timed_out = run_with_timeout(cmd, timeout=timeout)
    output = ""
    if timed_out:
        output += f"Command timed out after {timeout} seconds and was terminated.\n"
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += ("\nSTDERR:\n" + result.stderr)
    return {
        "returncode": result.returncode,
        "elapsed_sec": elapsed,
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        "output": truncate(output),
    }


def write_log(log_path, record):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Run a DuMateBench task with an OdysseyBench-style command agent.")
    parser.add_argument("--task-dir", type=Path, default=DEFAULT_TASK)
    parser.add_argument("--model", default=os.environ.get("DUMATE_MODEL", "gpt-4o"))
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--keep-containers", action="store_true")
    parser.add_argument("--trusted-base-url", action="append", default=[])
    parser.add_argument("--in-container", action="store_true")
    parser.add_argument(
        "--run-evaluator",
        action="store_true",
        help="Run the task evaluator from this process. Host-side runners should leave this off.",
    )
    args = parser.parse_args()

    task_dir = args.task_dir.resolve()
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = normalize_base_url(os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required. Set it in the shell environment; do not write it into repo files.")
    trusted_base_urls = load_trusted_base_urls(args.trusted_base_url)
    base_url = validate_trusted_base_url(base_url, trusted_base_urls)

    (task_dir / "run_outputs").mkdir(exist_ok=True)
    (task_dir / "run_logs").mkdir(exist_ok=True)
    for child in (task_dir / "run_outputs").iterdir():
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)

    if not args.in_container:
        for child in (task_dir / "run_logs").iterdir():
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
    else:
        for name in [
            "agent_llm.jsonl",
            "agent_llm.log",
            "agent_status.json",
            "llm_endpoint.jsonl",
            "tool_faults.jsonl",
        ]:
            path = task_dir / "run_logs" / name
            if path.exists():
                path.unlink()

    command_log = task_dir / "run_logs" / "agent_llm.jsonl"
    text_log = task_dir / "run_logs" / "agent_llm.log"
    status_log = task_dir / "run_logs" / "agent_status.json"
    cost_log = task_dir / "run_logs" / "agent_cost.json"
    write_log(task_dir / "run_logs" / "llm_endpoint.jsonl", {
        "event": "trusted_llm_base_url",
        "base_url": base_url,
        "network_fault_scope": "in_container_llm_calls_use_trusted_base_url; task_commands_run_as_agent_user",
    })

    system_prompt = (
        "You are a command-line agent running a DuMateBench task inside Docker. "
        "Respond only as JSON. Use this schema for each step: "
        '{"command": "bash command to run", "reason": "short reason"}. '
        "When the task is complete, respond with "
        '{"finish": true, "reason": "short reason"}. '
        "Run commands directly; do not ask the user for help. "
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
        "Use calendar_write to create final .ics calendar artifacts. Example: "
        "calendar_write --input-text /path/to/text.txt --output /outputs/calendar/Alice.ics. "
        "Use mail_send when a task asks you to send email. Example: "
        "mail_send --sender Alice --to Bob --subject 'Subject text' --body 'Email body text'. "
        "mail_send writes .eml files under /outputs/emails/<username>/. "
        "If a command fails because of transient network or wrapped-tool errors, observe the error "
        "and recover by retrying, waiting, reinstalling missing tools, or switching command-line strategies. "
        "Before finishing, verify that every required final artifact exists at the requested path. "
        "Only after verification should you respond with finish=true."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": load_task_prompt(task_dir)},
    ]
    status = {
        "agent_finished": False,
        "finish_reason": None,
        "max_steps_reached": False,
        "steps": 0,
        "evaluator_returncode": None,
    }
    cost = {
        "agent_backend": "react",
        "agent_model": args.model,
        "api_calls": 0,
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "total_tokens": 0.0,
        "elapsed_seconds": 0.0,
        "total_cost_usd": None,
    }
    agent_started = time.time()
    should_run_evaluator = args.run_evaluator or not args.in_container

    try:
        if not args.in_container:
            run(compose(task_dir) + ["down", "--remove-orphans"], cwd=task_dir, check=False)
            run(compose(task_dir) + ["build"], cwd=task_dir, capture=False)
            run(compose(task_dir) + ["up", "-d", "task"], cwd=task_dir, capture=False)

        for step in range(1, args.max_steps + 1):
            status["steps"] = step
            content, usage = chat_completion(messages, args.model, base_url, api_key)
            usage_totals = _usage_numbers(usage)
            cost["api_calls"] += 1
            cost["input_tokens"] += usage_totals["input_tokens"]
            cost["output_tokens"] += usage_totals["output_tokens"]
            cost["total_tokens"] += usage_totals["total_tokens"]
            action = parse_action(content)
            write_log(command_log, {"step": step, "model_response": action, "usage": usage})
            with text_log.open("a") as f:
                f.write(f"\n[step {step}] model: {json.dumps(action, ensure_ascii=False)}\n")

            if action["finish"]:
                status["agent_finished"] = True
                status["finish_reason"] = action.get("reason", "")
                write_log(command_log, {"step": step, "event": "agent_finish", "reason": status["finish_reason"]})
                break

            if args.in_container:
                observation = exec_local_as_agent(action["command"])
            else:
                observation = exec_in_task(task_dir, action["command"])
            write_log(command_log, {"step": step, "observation": observation})
            with text_log.open("a") as f:
                f.write(f"[step {step}] returncode={observation['returncode']} elapsed={observation['elapsed_sec']}s\n")
                f.write(observation["output"] + "\n")

            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "Observation:\n" + json.dumps(observation, ensure_ascii=False)})
        else:
            status["max_steps_reached"] = True
            status["finish_reason"] = f"Agent reached max steps ({args.max_steps}) without finish=true."
            write_log(command_log, {"event": "max_steps_reached", "max_steps": args.max_steps})
            with text_log.open("a") as f:
                f.write(f"\n[runner] {status['finish_reason']}\n")

        if should_run_evaluator:
            result = run(
                [sys.executable, str(task_dir / "evaluator" / "evaluator.py"), "--task-dir", str(task_dir)],
                check=False,
            )
            status["evaluator_returncode"] = result.returncode
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
    finally:
        cost["elapsed_seconds"] = round(time.time() - agent_started, 3)
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            cost[key] = _compact_number(cost[key])
        status["agent_cost_path"] = str(cost_log)
        status["total_tokens"] = cost["total_tokens"]
        status["total_cost_usd"] = cost["total_cost_usd"]
        cost_log.write_text(json.dumps(cost, indent=2, ensure_ascii=False))
        status_log.write_text(json.dumps(status, indent=2, ensure_ascii=False))
        if not args.in_container:
            logs = run(compose(task_dir) + ["logs", "--no-color"], cwd=task_dir, check=False).stdout
            (task_dir / "run_logs" / "compose.log").write_text(logs)
            if not args.keep_containers:
                run(compose(task_dir) + ["down", "--remove-orphans"], cwd=task_dir, check=False)


if __name__ == "__main__":
    main()
