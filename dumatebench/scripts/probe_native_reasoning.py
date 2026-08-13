#!/usr/bin/env python3
"""Probe reasoning-effort evidence for DuMateBench native backends.

The script has two jobs:

1. Build or execute a matrix of run_task_batch.py commands for native backends.
2. Inspect completed run_logs directories and report what reasoning settings are
   explicitly observable from the logs.

It intentionally separates explicit evidence from inference. With the matching
native_agent.py proxy changes, model_proxy.jsonl records prompt-safe request body
summaries that include reasoning/thinking fields without logging prompt content.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUN_TASK_BATCH = ROOT / "scripts" / "run_task_batch.py"
DEFAULT_TASKS_DIR = ROOT / "datasets/dev"
DEFAULT_TEMPLATE_TASK = DEFAULT_TASKS_DIR / "odyssey_2_12_smoke"
DEFAULT_MODELS = ["gpt-5.5", "claude-opus-4-8", "glm-5.2", "deepseek-v4-pro"]
DEFAULT_BACKENDS = ["codex", "claude-code"]
DEFAULT_REASONING_EFFORTS = ["default", "low", "medium", "high", "xhigh", "max"]
QUICK_TASK_ID = "reasoning_probe_quick_task"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run and/or analyze native backend reasoning-effort probes."
    )
    parser.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR))
    parser.add_argument("--template-task", default=str(DEFAULT_TEMPLATE_TASK))
    parser.add_argument("--task-glob", default="*")
    parser.add_argument("--limit", type=int, default=1, help="Tasks per backend/model run.")
    parser.add_argument("--runs-root", default=str(ROOT / "runs" / "reasoning_probe"))
    parser.add_argument("--run-id-prefix", default="reasoning-probe")
    parser.add_argument("--agent-base-url", default=os.environ.get("DUMATE_AGENT_BASE_URL", ""))
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--backends", nargs="+", choices=DEFAULT_BACKENDS, default=DEFAULT_BACKENDS)
    parser.add_argument(
        "--reasoning-efforts",
        nargs="+",
        default=DEFAULT_REASONING_EFFORTS,
        help="Reasoning efforts to test. Use 'default' to send no explicit override.",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--agent-timeout", type=int, default=0)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument(
        "--quick-smoke",
        action="store_true",
        help="Use a generated one-task smoke suite that finishes after one tiny output file.",
    )
    parser.add_argument("--quick-timeout", type=int, default=60)
    parser.add_argument(
        "--keep-quick-task",
        action="store_true",
        help="Keep the generated quick task directory instead of deleting it at exit.",
    )
    parser.add_argument("--execute", action="store_true", help="Actually run the probe matrix.")
    parser.add_argument(
        "--analyze-runs-root",
        action="append",
        default=[],
        help="Analyze an existing runs root or run-id directory. Repeatable.",
    )
    parser.add_argument("--summary-json", default="", help="Write JSON summary here.")
    parser.add_argument("--summary-md", default="", help="Write Markdown summary here.")
    parser.add_argument(
        "--extra-run-arg",
        action="append",
        default=[],
        help="Extra argument passed to run_task_batch.py. Repeat for multiple args.",
    )
    return parser.parse_args(argv)


def write_quick_task(task_root: Path, template_task: Path, timeout: int) -> None:
    task_dir = task_root / QUICK_TASK_ID
    task_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template_task / "environment", task_dir / "environment", dirs_exist_ok=True)
    evaluator_dir = task_dir / "evaluator"
    evaluator_dir.mkdir(exist_ok=True)
    (task_dir / "workspace_seed").mkdir(exist_ok=True)
    (task_dir / "instruction.md").write_text(
        "Create the file /outputs/probe.txt containing exactly the text reasoning-probe-ok, "
        "then stop. Do not do extra exploration.\n",
        encoding="utf-8",
    )
    (evaluator_dir / "evaluator.py").write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import json

output = Path('/outputs/probe.txt')
ok = output.is_file() and output.read_text(encoding='utf-8', errors='ignore').strip() == 'reasoning-probe-ok'
reward = {
    'complete_pass': int(ok),
    'partial_pass': 1.0 if ok else 0.0,
    'checks': [{'name': 'probe_file', 'passed': bool(ok), 'detail': str(output)}],
}
Path('/outputs/reward.json').write_text(json.dumps(reward, indent=2) + '\\n', encoding='utf-8')
raise SystemExit(0 if ok else 1)
""",
        encoding="utf-8",
    )
    (evaluator_dir / "checks.yaml").write_text("checks: []\n", encoding="utf-8")
    (task_dir / "task.yaml").write_text(
        f"""schema_version: "0.1"
task_id: "{QUICK_TASK_ID}"
task_name: "Reasoning probe quick smoke"
split: "dev"
difficulty: "L0"
tags:
  - reasoning-probe

agent:
  timeout_sec: {max(1, timeout)}
  user: "agent"
  workdir: "/workspace"
  direct_shell: true
  runner: "dumatebench/agents/command_agent.py"
  runtime: "in_container"

environment:
  backend: "docker-compose"
  dockerfile: "environment/Dockerfile"
  compose_file: "environment/docker-compose.yaml"
  cpus: 1
  memory_mb: 4096
  storage_mb: 4000
  allow_internet: true
  allow_package_install: true
  env:
    DUMATE_TASK_SEED: "20260803"

evaluation:
  entrypoint: "evaluator/evaluator.py"
  checks_file: "evaluator/checks.yaml"
  hidden_tests: false
  metrics:
    - complete_pass
    - partial_pass
""",
        encoding="utf-8",
    )


def sanitize_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "value"


def build_run_id(prefix: str, backend: str, model: str, reasoning_effort: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    effort = sanitize_component(reasoning_effort or "default")
    return f"{sanitize_component(prefix)}-{sanitize_component(backend)}-{sanitize_component(model)}-{effort}-{timestamp}"


def build_batch_command(
    args: argparse.Namespace,
    backend: str,
    model: str,
    reasoning_effort: str,
    run_id: str,
) -> list[str]:
    cmd = [
        sys.executable,
        str(RUN_TASK_BATCH),
        "--tasks-dir",
        args.tasks_dir,
        "--template-task",
        args.template_task,
        "--task-glob",
        args.task_glob,
        "--limit",
        str(args.limit),
        "--workers",
        str(args.workers),
        "--runs-root",
        args.runs_root,
        "--run-id",
        run_id,
        "--agent-backend",
        backend,
        "--agent-model",
        model,
        "--agent-timeout",
        str(args.agent_timeout),
        "--skip-llm-judge",
    ]
    if reasoning_effort and reasoning_effort != "default":
        cmd.extend(["--agent-reasoning-effort", reasoning_effort])
    if args.agent_base_url:
        cmd.extend(["--agent-base-url", args.agent_base_url])
    if args.no_build:
        cmd.append("--no-build")
    cmd.extend(args.extra_run_arg)
    return cmd


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def option_value(command: list[str], flag: str) -> str | None:
    for index, item in enumerate(command):
        if item == flag and index + 1 < len(command):
            return command[index + 1]
    return None


def extract_config_effort(command: list[str]) -> list[str]:
    efforts: list[str] = []
    for index, item in enumerate(command):
        value = None
        if item in {"-c", "--config"} and index + 1 < len(command):
            value = command[index + 1]
        elif item.startswith("-c") and item != "-c":
            value = item[2:]
        elif item.startswith("--config="):
            value = item.split("=", 1)[1]
        if value and "model_reasoning_effort" in value:
            efforts.append(value)
    return efforts


def recursive_find_keys(value: Any, names: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names:
                found.append(item)
            found.extend(recursive_find_keys(item, names))
    elif isinstance(value, list):
        for item in value:
            found.extend(recursive_find_keys(item, names))
    return found


def collect_request_reasoning_evidence(proxy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for row in proxy_rows:
        for field in ("request_body_summary", "adapter_upstream_request_body_summary"):
            summary = row.get(field)
            if not isinstance(summary, dict):
                continue
            picked = {
                key: summary[key]
                for key in ("reasoning", "thinking", "output_config")
                if key in summary
            }
            if picked:
                evidence.append({
                    "path": row.get("path"),
                    "source": field,
                    "model": summary.get("model"),
                    **picked,
                })
    return evidence


def load_codex_catalog_defaults() -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["codex", "debug", "models", "--bundled"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"gpt-5.5": "medium"}
    text = proc.stdout.strip()
    if not text:
        return {"gpt-5.5": "medium"}
    json_start = text.find("{")
    if json_start > 0:
        text = text[json_start:]
    try:
        catalog = json.loads(text)
    except json.JSONDecodeError:
        return {"gpt-5.5": "medium"}
    defaults: dict[str, str] = {}
    for model in catalog.get("models", []):
        if not isinstance(model, dict):
            continue
        slug = model.get("slug")
        effort = model.get("default_reasoning_level") or model.get("defaultReasoningEffort")
        if isinstance(slug, str) and isinstance(effort, str):
            defaults[slug] = effort
    defaults.setdefault("gpt-5.5", "medium")
    return defaults


def analyze_run_logs(run_logs: Path, codex_defaults: dict[str, str]) -> dict[str, Any]:
    command = read_json(run_logs / "batch_agent_command.json")
    if not isinstance(command, list):
        command = []
    status = read_json(run_logs / "agent_status.json")
    if not isinstance(status, dict):
        status = {}

    backend = option_value(command, "--backend") or status.get("agent_backend")
    model = option_value(command, "--model") or status.get("agent_model")
    base_url = option_value(command, "--base-url")
    command_config_efforts = extract_config_effort(command)
    command_effort = option_value(command, "--effort")
    agent_reasoning_effort = (
        option_value(command, "--reasoning-effort")
        or option_value(command, "--agent-reasoning-effort")
        or status.get("agent_reasoning_effort")
    )

    native_rows = read_jsonl(run_logs / "native_agent.jsonl")
    proxy_rows = read_jsonl(run_logs / "model_proxy.jsonl")
    reasoning_tokens = recursive_find_keys(native_rows, {"reasoning_output_tokens", "reasoning_tokens"})
    request_reasoning_evidence = collect_request_reasoning_evidence(proxy_rows)
    reasoning_item_count = sum(
        1
        for row in native_rows
        if isinstance(row.get("item"), dict) and row["item"].get("type") == "reasoning"
    )

    explicit_sources: list[str] = []
    if command_config_efforts:
        explicit_sources.extend(f"command config: {item}" for item in command_config_efforts)
    if command_effort:
        explicit_sources.append(f"claude command --effort {command_effort}")
    if agent_reasoning_effort and agent_reasoning_effort != "default":
        explicit_sources.append(f"native agent reasoning effort {agent_reasoning_effort}")
    if request_reasoning_evidence:
        explicit_sources.append("proxy log contains request body thinking/output_config/effort")

    inferred_strength = "unobserved"
    inference_basis = "No explicit reasoning/thinking field was found."
    if explicit_sources:
        inferred_strength = "explicit"
        inference_basis = "; ".join(explicit_sources)
    elif backend == "codex" and isinstance(model, str) and model in codex_defaults:
        inferred_strength = codex_defaults[model]
        inference_basis = "Codex model catalog default; no explicit override found."
    elif backend == "codex":
        inferred_strength = "gateway/default"
        inference_basis = "Codex has no catalog default for this model and no explicit override was found."
    elif backend == "claude-code":
        inferred_strength = "claude-code/gateway default"
        inference_basis = (
            "Claude Code was launched without --effort or thinking env overrides; "
            "no request-level reasoning/thinking field was found in the proxy summaries."
        )

    return {
        "run_logs": str(run_logs),
        "backend": backend,
        "model": model,
        "base_url": base_url,
        "agent_version": status.get("agent_version"),
        "agent_reasoning_effort": agent_reasoning_effort or "default",
        "agent_returncode": status.get("agent_returncode"),
        "timed_out": status.get("timed_out"),
        "inferred_reasoning_strength": inferred_strength,
        "inference_basis": inference_basis,
        "explicit_evidence": explicit_sources,
        "request_reasoning_evidence": request_reasoning_evidence,
        "reasoning_token_values": reasoning_tokens,
        "reasoning_item_count": reasoning_item_count,
        "proxy_request_count": len(proxy_rows),
        "proxy_status_counts": status_counts(proxy_rows),
    }


def status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def find_run_logs(root: Path) -> list[Path]:
    if root.name == "run_logs" and root.is_dir():
        return [root]
    return sorted(path for path in root.rglob("run_logs") if path.is_dir())


def render_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Native Reasoning Probe Summary",
        "",
        "| backend | model | configured effort | inferred reasoning | request evidence | explicit evidence | reasoning tokens | proxy requests | run_logs |",
        "|---|---|---|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        tokens = row.get("reasoning_token_values") or []
        explicit = "; ".join(row.get("explicit_evidence") or []) or "none"
        request_evidence = json.dumps(row.get("request_reasoning_evidence") or [], ensure_ascii=False)
        lines.append(
            "| {backend} | {model} | {configured} | {reasoning} | `{request_evidence}` | {explicit} | {tokens} | {requests} | `{logs}` |".format(
                backend=row.get("backend") or "",
                model=row.get("model") or "",
                configured=row.get("agent_reasoning_effort") or "default",
                reasoning=row.get("inferred_reasoning_strength") or "",
                request_evidence=request_evidence.replace("|", "\\|"),
                explicit=explicit.replace("|", "\\|"),
                tokens=sum(int(x) for x in tokens if isinstance(x, int)),
                requests=row.get("proxy_request_count") or 0,
                logs=row.get("run_logs") or "",
            )
        )
    lines.append("")
    lines.append("Notes:")
    lines.append("- `explicit evidence = none` means the logs do not prove a concrete effort level was sent.")
    lines.append("- `request evidence` comes from the new proxy request-body summary fields and is the closest evidence for actual API parameters.")
    lines.append("- For Codex known models, the script uses the local `codex debug models --bundled` catalog default when no explicit override is present.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    quick_tmp: tempfile.TemporaryDirectory[str] | None = None
    if args.quick_smoke:
        if args.keep_quick_task:
            quick_root = Path(args.runs_root) / "_quick_task"
            print(f"Quick task directory: {quick_root}")
        else:
            quick_tmp = tempfile.TemporaryDirectory(prefix="dumate-reasoning-probe-")
            quick_root = Path(quick_tmp.name)
        write_quick_task(quick_root, Path(args.template_task), args.quick_timeout)
        args.tasks_dir = str(quick_root)
        args.task_glob = QUICK_TASK_ID
        args.limit = 1
        if args.agent_timeout == 0:
            args.agent_timeout = args.quick_timeout
    run_ids: list[str] = []
    commands: list[list[str]] = []

    for backend in args.backends:
        for model in args.models:
            for reasoning_effort in args.reasoning_efforts:
                run_id = build_run_id(args.run_id_prefix, backend, model, reasoning_effort)
                run_ids.append(run_id)
                commands.append(build_batch_command(args, backend, model, reasoning_effort, run_id))

    if args.execute:
        for cmd in commands:
            print(f"Running: {json.dumps(cmd, ensure_ascii=False)}", flush=True)
            proc = subprocess.run(cmd, check=False)
            if proc.returncode != 0:
                print(f"Command exited with {proc.returncode}", file=sys.stderr)
    elif not args.analyze_runs_root:
        print("Planned commands. Re-run with --execute to launch Docker/model calls.")
        for cmd in commands:
            print(json.dumps(cmd, ensure_ascii=False))

    analyze_roots = [Path(path) for path in args.analyze_runs_root]
    if args.execute:
        analyze_roots.append(Path(args.runs_root))

    rows: list[dict[str, Any]] = []
    if analyze_roots:
        codex_defaults = load_codex_catalog_defaults()
        for root in analyze_roots:
            for run_logs in find_run_logs(root):
                rows.append(analyze_run_logs(run_logs, codex_defaults))

    if rows:
        print(render_markdown(rows))
        if args.summary_json:
            Path(args.summary_json).write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.summary_md:
            Path(args.summary_md).write_text(render_markdown(rows), encoding="utf-8")

    if quick_tmp is not None:
        quick_tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
