#!/usr/bin/env python3
"""Run a native coding-agent CLI inside a DuMateBench task container."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import pwd
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


AGENT_PATH = "/opt/dumate/wrappers:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_TIMEOUT_SECONDS = 900
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

SYSTEM_PROMPT = """You are the native agent being evaluated in a DuMateBench task container.
Work autonomously until the task is complete. Do not ask the user questions.

Environment contract:
- Your working directory is /workspace.
- Read the files in /workspace that are relevant to the task. The workspace may include user.md,
  soul.md, and session_chat_history.json with useful user context.
- Write every final deliverable to the exact path requested by the task, normally under /outputs.
- Before finishing, verify every required output exists and is a valid artifact.
- Use benchmark tools by their command names from PATH, including tesseract, ocr_extract,
  calendar_write, and mail_send. Never bypass their wrappers with absolute paths or a rewritten PATH.
- The environment can have missing dependencies, noisy files, transient network failures, and
  injected tool failures. Diagnose failures and retry or use another legitimate strategy.
- You may use sudo to install dependencies. Do not modify /opt/dumate/task, fault configuration,
  benchmark logs, the evaluator, or the network-fault daemon.
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Claude Code or Codex as a native DuMateBench agent.")
    parser.add_argument("--backend", choices=("claude-code", "codex"), required=True)
    parser.add_argument("--task-dir", type=Path, default=Path("/opt/dumate/task"))
    parser.add_argument("--model", default=os.environ.get("DUMATE_AGENT_MODEL", ""))
    parser.add_argument("--base-url", default=os.environ.get("DUMATE_AGENT_BASE_URL", ""))
    parser.add_argument("--timeout", type=int, default=0, help="Total agent timeout. Zero reads task.yaml.")
    parser.add_argument(
        "--max-turns",
        type=int,
        default=0,
        help="Optional Claude Code agentic turn limit. Zero lets the CLI run until it exits or times out.",
    )
    parser.add_argument(
        "--direct-model-network",
        action="store_true",
        help="Send model traffic directly as the agent user instead of through the root-side local proxy.",
    )
    return parser.parse_args(argv)


def task_timeout(task_dir: Path, override: int) -> int:
    if override > 0:
        return override
    task_yaml = task_dir / "task.yaml"
    if task_yaml.is_file():
        match = re.search(r"^\s*timeout_sec\s*:\s*(\d+)\s*$", task_yaml.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            return max(1, int(match.group(1)))
    return DEFAULT_TIMEOUT_SECONDS


def build_prompt(task_dir: Path) -> str:
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    return f"{SYSTEM_PROMPT}\nTask instruction:\n\n{instruction.strip()}\n"


def resolve_base_url(backend: str, explicit: str, env: dict[str, str]) -> str:
    if explicit:
        base_url = explicit.rstrip("/")
    elif backend == "codex":
        base_url = env.get("OPENAI_BASE_URL", "").rstrip("/")
    else:
        base_url = env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if backend == "claude-code" and base_url.endswith("/v1"):
        return base_url[:-3]
    return base_url


def resolve_api_key(backend: str, env: dict[str, str]) -> str:
    if env.get("DUMATE_AGENT_API_KEY"):
        return env["DUMATE_AGENT_API_KEY"]
    if backend == "codex":
        return env.get("OPENAI_API_KEY", "")
    return env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY", "")


def claude_auth_environment(env: dict[str, str]) -> dict[str, str]:
    if env.get("DUMATE_AGENT_API_KEY"):
        return {"ANTHROPIC_AUTH_TOKEN": env["DUMATE_AGENT_API_KEY"]}
    if env.get("ANTHROPIC_AUTH_TOKEN"):
        return {"ANTHROPIC_AUTH_TOKEN": env["ANTHROPIC_AUTH_TOKEN"]}
    if env.get("ANTHROPIC_API_KEY"):
        return {"ANTHROPIC_API_KEY": env["ANTHROPIC_API_KEY"]}
    return {}


def is_anthropic_model(model: str) -> bool:
    normalized = model.lower()
    return (
        "claude" in normalized
        or normalized.startswith("anthropic/")
        or normalized.startswith("glm")
        or normalized.startswith("ds/")
        or "deepseek" in normalized
    )


def _content_text(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return str(parts) if parts is not None else ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if text is None:
                text = part.get("input_text")
            if text is None:
                text = part.get("output_text")
            if text is not None:
                chunks.append(str(text))
    return "\n".join(chunk for chunk in chunks if chunk)


def responses_input_to_anthropic_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": [{"type": "text", "text": value}]}]
    if not isinstance(value, list):
        return [{"role": "user", "content": [{"type": "text", "text": str(value)}]}]

    messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for item in value:
        if not isinstance(item, dict):
            flush_tool_results()
            messages.append({"role": "user", "content": [{"type": "text", "text": str(item)}]})
            continue
        item_type = item.get("type")
        role = item.get("role")
        if item_type == "message" or role in {"user", "assistant", "system", "developer"}:
            flush_tool_results()
            text = _content_text(item.get("content"))
            if not text:
                continue
            mapped_role = "assistant" if role == "assistant" else "user"
            text_block = {"type": "text", "text": text}
            if messages and messages[-1]["role"] == mapped_role:
                messages[-1]["content"].append(text_block)
            else:
                messages.append({"role": mapped_role, "content": [text_block]})
        elif item_type in {"function_call", "tool_call"}:
            # A tool result must immediately follow the assistant turn that
            # requested it. Flush results from a completed tool-call group
            # before starting the next assistant tool-call group.
            flush_tool_results()
            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"
            name = item.get("name") or item.get("function", {}).get("name") or "tool"
            arguments = item.get("arguments") or item.get("function", {}).get("arguments") or "{}"
            try:
                tool_input = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                tool_input = {"arguments": arguments}
            tool_use = {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
            if messages and messages[-1]["role"] == "assistant":
                messages[-1]["content"].append(tool_use)
            else:
                messages.append({"role": "assistant", "content": [tool_use]})
        elif item_type in {"function_call_output", "tool_result"}:
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": item.get("call_id") or item.get("id") or item.get("tool_call_id") or "unknown",
                "content": _content_text(item.get("output", item.get("content", ""))),
            })

    flush_tool_results()
    if not messages:
        messages.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    return messages


def responses_tools_to_anthropic_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            name = tool.get("name") or tool.get("function", {}).get("name")
            description = tool.get("description") or tool.get("function", {}).get("description") or ""
            schema = tool.get("parameters") or tool.get("function", {}).get("parameters") or {"type": "object"}
        else:
            name = tool.get("name") or tool.get("type")
            description = tool.get("description", "")
            schema = tool.get("input_schema") or tool.get("parameters") or {"type": "object"}
        if name:
            converted.append({"name": name, "description": description, "input_schema": schema})
    return converted


def anthropic_to_response_payload(message: dict[str, Any], model: str, response_id: str | None = None) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    text_chunks: list[str] = []
    for part in message.get("content", []):
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text_chunks.append(str(part.get("text", "")))
        elif part.get("type") == "tool_use":
            output.append({
                "id": part.get("id") or f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "status": "completed",
                "call_id": part.get("id") or f"call_{uuid.uuid4().hex}",
                "name": part.get("name", "tool"),
                "arguments": json.dumps(part.get("input", {}), ensure_ascii=False),
            })
    if text_chunks:
        text = "\n".join(chunk for chunk in text_chunks if chunk)
        output.insert(0, {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    usage = message.get("usage", {}) if isinstance(message.get("usage"), dict) else {}
    input_tokens = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    output_tokens = usage.get("output_tokens", 0)
    return {
        "id": response_id or f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


class ModelProxy:
    """Forward model HTTP traffic from an agent-user CLI via this root process."""

    def __init__(self, upstream_base_url: str, log_path: Path, api_key: str = "", enable_responses_adapter: bool = True):
        parsed = urlsplit(upstream_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"Invalid model base URL: {upstream_base_url!r}")
        self.parsed = parsed
        self.log_path = log_path
        self.api_key = api_key
        self.enable_responses_adapter = enable_responses_adapter
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:  # noqa: N802
                self._forward()

            def do_POST(self) -> None:  # noqa: N802
                self._forward()

            def do_DELETE(self) -> None:  # noqa: N802
                self._forward()

            def log_message(self, format_string: str, *args: Any) -> None:
                del format_string, args

            def _forward(self) -> None:
                started = time.time()
                status = 502
                adapter = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length) if length else None
                    if owner._should_adapt_responses(self.path, body):
                        adapter = "responses_to_messages"
                        status = owner._handle_responses_adapter(self, body or b"")
                        return
                    headers = {
                        key: value
                        for key, value in self.headers.items()
                        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
                    }
                    headers["Host"] = owner.parsed.netloc
                    port = owner.parsed.port
                    if owner.parsed.scheme == "https":
                        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                            owner.parsed.hostname,
                            port or 443,
                            timeout=600,
                            context=ssl.create_default_context(),
                        )
                    else:
                        connection = http.client.HTTPConnection(owner.parsed.hostname, port or 80, timeout=600)
                    connection.request(self.command, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    status = response.status
                    self.send_response(response.status, response.reason)
                    for key, value in response.getheaders():
                        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length":
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    connection.close()
                except Exception as exc:  # pragma: no cover - exercised only on broken upstreams
                    try:
                        self.send_error(502, str(exc))
                    except OSError:
                        pass
                finally:
                    owner._log({
                        "method": self.command,
                        "path": self.path,
                        "status": status,
                        "elapsed_sec": round(time.time() - started, 3),
                        **({"adapter": adapter} if adapter else {}),
                    })

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="dumate-model-proxy", daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        base_path = self.parsed.path.rstrip("/")
        return f"http://127.0.0.1:{port}{base_path}"

    def _upstream_path(self, path: str) -> str:
        base_path = self.parsed.path.rstrip("/")
        suffix = path[len(base_path):] if base_path and path.startswith(base_path + "/") else path
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        return f"{base_path}{suffix}"

    def _should_adapt_responses(self, path: str, body: bytes | None) -> bool:
        if not self.enable_responses_adapter or not body:
            return False
        if not path.split("?", 1)[0].endswith("/responses"):
            return False
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and is_anthropic_model(str(payload.get("model", "")))

    def _anthropic_headers(self, body: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "anthropic-version": "2023-06-01",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request_anthropic_messages(self, payload: dict[str, Any]) -> tuple[int, bytes, str]:
        upstream_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        port = self.parsed.port
        if self.parsed.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                self.parsed.hostname,
                port or 443,
                timeout=600,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(self.parsed.hostname, port or 80, timeout=600)
        connection.request(
            "POST",
            self._upstream_path("/messages"),
            body=upstream_body,
            headers=self._anthropic_headers(upstream_body),
        )
        response = connection.getresponse()
        body = response.read()
        status = response.status
        reason = response.reason
        connection.close()
        return status, body, reason

    def _responses_to_messages_payload(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        model = str(request_payload.get("model", ""))
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": int(request_payload.get("max_output_tokens") or request_payload.get("max_tokens") or 4096),
            "messages": responses_input_to_anthropic_messages(request_payload.get("input", "")),
        }
        instructions = request_payload.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            payload["system"] = instructions
        tools = responses_tools_to_anthropic_tools(request_payload.get("tools"))
        if tools:
            payload["tools"] = tools
            if request_payload.get("tool_choice"):
                payload["tool_choice"] = {"type": "auto"}
        return payload

    def _handle_responses_adapter(self, handler: BaseHTTPRequestHandler, body: bytes) -> int:
        request_payload = json.loads(body)
        model = str(request_payload.get("model", ""))
        anthropic_payload = self._responses_to_messages_payload(request_payload)
        upstream_status, upstream_body, upstream_reason = self._request_anthropic_messages(anthropic_payload)
        if upstream_status >= 400:
            handler.send_response(upstream_status, upstream_reason)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(upstream_body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(upstream_body)
            return upstream_status
        message = json.loads(upstream_body)
        response_payload = anthropic_to_response_payload(message, model)
        if request_payload.get("stream") is True:
            self._send_response_stream(handler, response_payload)
        else:
            response_body = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
            handler.send_response(200, "OK")
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(response_body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(response_body)
        return 200

    def _send_sse_event(self, handler: BaseHTTPRequestHandler, event_name: str, payload: dict[str, Any]) -> None:
        data = f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        handler.wfile.write(data)
        handler.wfile.flush()

    def _send_response_stream(self, handler: BaseHTTPRequestHandler, response_payload: dict[str, Any]) -> None:
        handler.send_response(200, "OK")
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        self._send_sse_event(handler, "response.created", {"type": "response.created", "response": response_payload})
        for output_index, item in enumerate(response_payload.get("output", [])):
            self._send_sse_event(handler, "response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            })
            if item.get("type") == "message":
                for content_index, part in enumerate(item.get("content", [])):
                    text = str(part.get("text", ""))
                    self._send_sse_event(handler, "response.content_part.added", {
                        "type": "response.content_part.added",
                        "item_id": item.get("id"),
                        "output_index": output_index,
                        "content_index": content_index,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    })
                    if text:
                        self._send_sse_event(handler, "response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "item_id": item.get("id"),
                            "output_index": output_index,
                            "content_index": content_index,
                            "delta": text,
                        })
                    self._send_sse_event(handler, "response.output_text.done", {
                        "type": "response.output_text.done",
                        "item_id": item.get("id"),
                        "output_index": output_index,
                        "content_index": content_index,
                        "text": text,
                    })
                    self._send_sse_event(handler, "response.content_part.done", {
                        "type": "response.content_part.done",
                        "item_id": item.get("id"),
                        "output_index": output_index,
                        "content_index": content_index,
                        "part": part,
                    })
            elif item.get("type") == "function_call":
                arguments = str(item.get("arguments", ""))
                if arguments:
                    self._send_sse_event(handler, "response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item.get("id"),
                        "output_index": output_index,
                        "delta": arguments,
                    })
                self._send_sse_event(handler, "response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "item_id": item.get("id"),
                    "output_index": output_index,
                    "arguments": arguments,
                })
            self._send_sse_event(handler, "response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": item,
            })
        self._send_sse_event(handler, "response.completed", {"type": "response.completed", "response": response_payload})
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _log(self, record: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record["ts"] = time.time()
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_codex_config(config_home: Path, model: str, base_url: str) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    escaped_model = json.dumps(model)
    escaped_url = json.dumps(base_url)
    config = (
        f"model = {escaped_model}\n"
        'model_provider = "dumate"\n'
        "check_for_updates_on_startup = false\n"
        "\n[model_providers.dumate]\n"
        'name = "DuMateBench model gateway"\n'
        f"base_url = {escaped_url}\n"
        'env_key = "DUMATE_NATIVE_API_KEY"\n'
        'wire_api = "responses"\n'
        "request_max_retries = 4\n"
        "stream_max_retries = 5\n"
        "stream_idle_timeout_ms = 300000\n"
    )
    (config_home / "config.toml").write_text(config, encoding="utf-8")


def build_command(backend: str, model: str, prompt: str, max_turns: int) -> tuple[list[str], str | None]:
    if backend == "codex":
        return (
            [
                "codex",
                "exec",
                "--json",
                "--ephemeral",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                model,
                "-",
            ],
            prompt,
        )
    command = [
        "claude",
        "-p",
        "--bare",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        prompt,
    ]
    if max_turns > 0:
        command[1:1] = ["--max-turns", str(max_turns)]
    return command, None


def agent_identity() -> tuple[int, int]:
    record = pwd.getpwnam("agent")
    return record.pw_uid, record.pw_gid


def demote(uid: int, gid: int):
    def apply() -> None:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    return apply


def run_agent(
    command: list[str],
    stdin_text: str | None,
    child_env: dict[str, str],
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[int, bool, float]:
    uid, gid = agent_identity()
    started = time.time()
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        proc = subprocess.Popen(
            command,
            cwd="/workspace",
            env=child_env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            preexec_fn=demote(uid, gid),
        )
        try:
            proc.communicate(input=stdin_text.encode("utf-8") if stdin_text is not None else None, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    return int(proc.returncode), timed_out, round(time.time() - started, 3)


def command_version(executable: str) -> str:
    result = subprocess.run([executable, "--version"], text=True, capture_output=True, timeout=30, check=False)
    return (result.stdout or result.stderr).strip()


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _usage_numbers(usage: dict[str, Any]) -> dict[str, float]:
    input_tokens = _number(usage.get("input_tokens")) + _number(usage.get("prompt_tokens"))
    output_tokens = _number(usage.get("output_tokens")) + _number(usage.get("completion_tokens"))
    cache_tokens = _number(usage.get("cache_creation_input_tokens")) + _number(usage.get("cache_read_input_tokens"))
    if isinstance(usage.get("cache_creation"), dict):
        cache_tokens += sum(_number(value) for value in usage["cache_creation"].values())
    total_tokens = _number(usage.get("total_tokens"))
    if total_tokens == 0.0:
        total_tokens = input_tokens + output_tokens + cache_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "total_tokens": total_tokens,
    }


def _merge_usage(total: dict[str, float], usage: dict[str, Any]) -> None:
    numbers = _usage_numbers(usage)
    for key, value in numbers.items():
        total[key] = total.get(key, 0.0) + value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def summarize_native_cost(
    stdout_path: Path,
    proxy_log_path: Path,
    *,
    backend: str,
    model: str,
    elapsed_seconds: float,
) -> dict[str, Any]:
    records = _read_jsonl(stdout_path)
    usage_totals: dict[str, float] = {}
    cost_usd = 0.0
    usage_sources = 0
    for record in records:
        usage = record.get("usage")
        if not isinstance(usage, dict) and isinstance(record.get("message"), dict):
            usage = record["message"].get("usage")
        if isinstance(usage, dict) and (record.get("type") == "result" or not usage_totals):
            _merge_usage(usage_totals, usage)
            usage_sources += 1
        cost_usd += _number(record.get("total_cost_usd"))
        cost_usd += _number(record.get("cost_usd"))

    proxy_records = _read_jsonl(proxy_log_path)
    status_counts: dict[str, int] = {}
    for record in proxy_records:
        status = str(record.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1

    def compact(value: float) -> int | float:
        return int(value) if float(value).is_integer() else round(value, 4)

    return {
        "agent_backend": backend,
        "agent_model": model,
        "elapsed_seconds": elapsed_seconds,
        "api_calls": len(proxy_records),
        "api_status_counts": status_counts,
        "usage_sources": usage_sources,
        "input_tokens": compact(usage_totals.get("input_tokens", 0.0)),
        "output_tokens": compact(usage_totals.get("output_tokens", 0.0)),
        "cache_tokens": compact(usage_totals.get("cache_tokens", 0.0)),
        "total_tokens": compact(usage_totals.get("total_tokens", 0.0)),
        "total_cost_usd": round(cost_usd, 8) if cost_usd else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    task_dir = args.task_dir.resolve()
    logs_dir = Path("/logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    status_path = logs_dir / "agent_status.json"
    cost_path = logs_dir / "agent_cost.json"
    stdout_path = logs_dir / "native_agent.jsonl"
    stderr_path = logs_dir / "native_agent.stderr.log"
    proxy_log_path = logs_dir / "model_proxy.jsonl"
    status: dict[str, Any] = {
        "agent_backend": args.backend,
        "agent_model": args.model,
        "agent_finished": False,
        "agent_returncode": None,
        "timed_out": False,
        "elapsed_seconds": 0.0,
        "timeout_seconds": task_timeout(task_dir, args.timeout),
        "model_proxy_enabled": not args.direct_model_network,
    }
    proxy: ModelProxy | None = None

    try:
        executable = "codex" if args.backend == "codex" else "claude"
        if shutil.which(executable) is None:
            raise RuntimeError(f"{executable} is not installed in this task image")
        if not args.model:
            raise RuntimeError("Agent model is required; pass --agent-model or set DUMATE_AGENT_MODEL")

        env = dict(os.environ)
        base_url = resolve_base_url(args.backend, args.base_url, env)
        api_key = resolve_api_key(args.backend, env)
        if not base_url:
            raise RuntimeError("Agent base URL is required; pass --agent-base-url or set DUMATE_AGENT_BASE_URL")
        if not api_key:
            raise RuntimeError(
                "Agent API key is required; set DUMATE_AGENT_API_KEY or the backend-specific API key variable"
            )

        effective_base_url = base_url
        if not args.direct_model_network:
            proxy = ModelProxy(base_url, proxy_log_path, api_key=api_key)
            effective_base_url = proxy.start()

        child_env = {
            "HOME": "/home/agent",
            "USER": "agent",
            "LOGNAME": "agent",
            "PATH": AGENT_PATH,
            "LANG": env.get("LANG", "C.UTF-8"),
            "LC_ALL": env.get("LC_ALL", "C.UTF-8"),
            "DUMATE_TOOL_FAULT_CONFIG": "/opt/dumate/tool_faults.yaml",
            "DUMATE_TOOL_FAULT_LOG": "/logs/tool_faults.jsonl",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_UPDATES": "1",
        }
        if args.backend == "codex":
            codex_home = Path("/home/agent/.codex-dumate")
            write_codex_config(codex_home, args.model, effective_base_url)
            uid, gid = agent_identity()
            os.chown(codex_home, uid, gid)
            os.chown(codex_home / "config.toml", uid, gid)
            child_env.update({
                "CODEX_HOME": str(codex_home),
                "DUMATE_NATIVE_API_KEY": api_key,
            })
        else:
            child_env.update({
                "ANTHROPIC_BASE_URL": effective_base_url,
                "ANTHROPIC_MODEL": args.model,
                "CLAUDE_CONFIG_DIR": "/home/agent/.claude-dumate",
            })
            child_env.update(claude_auth_environment(env))

        prompt = build_prompt(task_dir)
        command, stdin_text = build_command(args.backend, args.model, prompt, args.max_turns)
        status["agent_version"] = command_version(executable)
        returncode, timed_out, elapsed = run_agent(
            command,
            stdin_text,
            child_env,
            status["timeout_seconds"],
            stdout_path,
            stderr_path,
        )
        status.update({
            "agent_returncode": returncode,
            "timed_out": timed_out,
            "elapsed_seconds": elapsed,
            "agent_finished": returncode == 0 and not timed_out,
        })
        if timed_out:
            status["finish_reason"] = f"Native agent exceeded {status['timeout_seconds']} seconds"
        elif returncode != 0:
            status["finish_reason"] = f"Native agent exited with return code {returncode}"
        else:
            status["finish_reason"] = "Native agent exited successfully"
        return returncode
    except Exception as exc:
        status["finish_reason"] = str(exc)
        print(f"native agent runner failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if proxy is not None:
            proxy.stop()
        cost = summarize_native_cost(
            stdout_path,
            proxy_log_path,
            backend=args.backend,
            model=args.model,
            elapsed_seconds=float(status.get("elapsed_seconds", 0.0)),
        )
        status["agent_cost_path"] = str(cost_path)
        status["total_tokens"] = cost["total_tokens"]
        status["total_cost_usd"] = cost["total_cost_usd"]
        cost_path.write_text(json.dumps(cost, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
