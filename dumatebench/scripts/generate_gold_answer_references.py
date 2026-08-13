#!/usr/bin/env python3
"""Discover, compute, and verify deterministic gold-answer references for locked tasks."""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEFAULT_RUNS_DIR = REPO_ROOT / "cc_runs"
DEFAULT_LOCKED_DIR = ROOT / "locked_llm_judge_inputs"
DEFAULT_SELECTOR_MODEL = "gemini-3.1-pro-preview"
DEFAULT_GENERATOR_MODEL = "gpt-5.5"
DEFAULT_VERIFIER_MODELS = ("claude-opus-4-8", "deepseek-v4-pro")
DEFAULT_GOLD_FILE = "evaluator/gold_answer_reference.json"
CODEX_CLI_FALLBACKS = (Path("/Applications/ChatGPT.app/Contents/Resources/codex"),)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_name(value: str, fallback: str = "item") -> str:
    name = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("._")
    return name[:160] or fallback


def load_task_id(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    match = re.search(r"^task_id:\s*(.+?)\s*$", text, flags=re.M)
    return match.group(1).strip().strip("'\"") if match else ""


def index_task_views(runs_dir: Path) -> dict[str, list[tuple[str, Path]]]:
    result: dict[str, list[tuple[str, Path]]] = {}
    for task_yaml in sorted(runs_dir.rglob("task.yaml")):
        task_view = task_yaml.parent
        if task_view.name != "task_view":
            continue
        task_id = load_task_id(task_yaml)
        if not task_id:
            continue
        try:
            backend = task_view.relative_to(runs_dir).parts[0]
        except (ValueError, IndexError):
            backend = task_view.parents[1].name
        result.setdefault(task_id.lower(), []).append((backend, task_view))
    return result


def file_inventory(root: Path, *, prefix: str = "", max_files: int = 5000) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        result.append({"path": f"{prefix}{relative}", "size_bytes": size})
        if len(result) >= max_files:
            result.append({"path": "...inventory_truncated...", "size_bytes": 0})
            break
    return result


def load_artifacts_and_criteria(task_dir: Path) -> list[dict[str, Any]]:
    evaluator_dir = task_dir / "evaluator"
    artifact_doc = read_json(evaluator_dir / "llm_judge_artifacts.json") or {}
    artifacts = artifact_doc.get("artifacts")
    result: list[dict[str, Any]] = []
    for artifact in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(artifact, dict):
            continue
        criteria_file = str(artifact.get("criteria_file") or "")
        criteria_doc = read_json(task_dir / criteria_file) if criteria_file else None
        criteria: list[dict[str, Any]] = []
        values = criteria_doc.get("criteria") if criteria_doc else []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            criteria.append(
                {
                    "id": item.get("id"),
                    "dimension": item.get("dimension"),
                    "description": item.get("description"),
                    "weight": item.get("weight"),
                }
            )
        result.append(
            {
                "id": artifact.get("id"),
                "output_file": artifact.get("output_file"),
                "artifact_type": artifact.get("artifact_type"),
                "criteria": criteria,
            }
        )
    return result


def build_task_context(task_dir: Path, task_views: list[tuple[str, Path]]) -> dict[str, Any]:
    instruction_path = task_dir / "instruction.md"
    instruction = instruction_path.read_text(encoding="utf-8", errors="ignore")
    artifacts = load_artifacts_and_criteria(task_dir)
    target_paths = {
        str(item.get("output_file") or "").removeprefix("run_outputs/")
        for item in artifacts
        if item.get("output_file")
    }
    canonical_view = task_views[0][1]
    inputs = file_inventory(canonical_view / "workspace_seed", prefix="workspace_seed/")
    inputs.extend(file_inventory(canonical_view / "web_reference", prefix="web_reference/"))
    runs: list[dict[str, Any]] = []
    for backend, task_view in task_views:
        target_files: list[dict[str, Any]] = []
        for relative in sorted(target_paths):
            path = task_view / "run_outputs" / relative
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            target_files.append({"path": f"run_outputs/{relative}", "size_bytes": size})
        runs.append(
            {
                "backend": backend,
                "task_view": str(task_view.resolve()),
                "workspace_seed": str((task_view / "workspace_seed").resolve()),
                "web_reference": str((task_view / "web_reference").resolve()),
                "run_outputs": str((task_view / "run_outputs").resolve()),
                "target_files": target_files,
            }
        )
    return {
        "task_id": task_dir.name,
        "task_dir": str(task_dir.resolve()),
        "instruction": instruction,
        "artifacts_and_criteria": artifacts,
        "input_inventory": inputs,
        "runs": runs,
    }


def strip_markdown_json_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_first_json_value(text: str) -> str | None:
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return None
    start = min(starts)
    stack: list[str] = []
    in_string = False
    escape = False
    pairs = {"{": "}", "[": "]"}
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def parse_json_object(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or part.get("content") or "") if isinstance(part, dict) else str(part)
            for part in content
        )
    text = strip_markdown_json_fence(str(content or "").strip())
    if not text:
        raise ValueError("LLM response content is empty")
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        extracted = extract_first_json_value(text)
        if extracted is None:
            raise
        value = json.loads(extracted)
    for _ in range(2):
        if isinstance(value, str):
            value = json.loads(strip_markdown_json_fence(value.strip()))
        elif isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            value = value[0]
        else:
            break
    if not isinstance(value, dict):
        raise ValueError(f"LLM response JSON is not an object: {type(value).__name__}")
    return value


def call_openai_compatible_json(
    messages: list[dict[str, str]],
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            parsed = json.loads(body)
            return parse_json_object(parsed["choices"][0]["message"]["content"])
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            http.client.RemoteDisconnected,
            TimeoutError,
            OSError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM request failed after {retries + 1} attempts: {last_error}")


def selection_messages(context: dict[str, Any]) -> list[dict[str, str]]:
    payload = {
        "task_id": context["task_id"],
        "instruction": context["instruction"],
        "artifacts_and_criteria": context["artifacts_and_criteria"],
        "input_inventory": context["input_inventory"],
        "run_target_manifests": [
            {"backend": item["backend"], "target_files": item["target_files"]} for item in context["runs"]
        ],
    }
    system = (
        "You are a strict benchmark annotation designer. Decide whether a task needs a deterministic gold answer. "
        "Return one JSON object only. Prefer false when the answer is subjective, visual, creative, time-dependent, "
        "or cannot be independently recomputed from static task inputs."
    )
    user = f"""判断该任务是否需要补充 gold answer。

只有同时满足以下条件才返回 needs_gold_answer=true：
1. instruction 要求对静态输入执行数据处理、计算、查询、转换、统计、排序、匹配、解析或可验证的代码逻辑；
2. 存在明确正确答案、关键中间数值、预期记录集合、确定性行为或可给容差的结果；
3. gold answer 会实质帮助 LLM judge 判断 correctness，而不只是重复 criteria；
4. 答案能从 workspace_seed/web_reference 静态文件独立复算。

必须返回 false 的情况：实时价格/天气等随执行时间变化；开放式预测；主观写作或设计；纯排版/视觉审美；答案主要取决于 agent 的创作选择；缺少必要静态数据，无法可靠复算。

返回格式：
{{
  "needs_gold_answer": true,
  "confidence": 0.0,
  "reason": "...",
  "gold_questions": [
    {{
      "id": "stable_snake_case_id",
      "question": "需要计算或确定的精确问题",
      "answer_type": "number|table|records|mapping|text|behavior",
      "source_inputs": ["workspace_seed/..."],
      "related_outputs": ["run_outputs/..."],
      "verification_plan": "如何独立复算，包括容差",
      "time_sensitive": false
    }}
  ]
}}

任务上下文：
{json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_selection(raw: dict[str, Any], threshold: float) -> dict[str, Any]:
    questions: list[dict[str, Any]] = []
    values = raw.get("gold_questions")
    for index, item in enumerate(values if isinstance(values, list) else [], start=1):
        if not isinstance(item, dict) or bool(item.get("time_sensitive")):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        questions.append(
            {
                "id": safe_name(str(item.get("id") or f"gold_question_{index}"), f"gold_question_{index}"),
                "question": question,
                "answer_type": str(item.get("answer_type") or "text"),
                "source_inputs": [str(value) for value in item.get("source_inputs", []) if isinstance(value, str)],
                "related_outputs": [str(value) for value in item.get("related_outputs", []) if isinstance(value, str)],
                "verification_plan": str(item.get("verification_plan") or ""),
                "time_sensitive": False,
            }
        )
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    needed = bool(raw.get("needs_gold_answer")) and confidence >= threshold and bool(questions)
    return {
        "needs_gold_answer": needed,
        "model_decision": bool(raw.get("needs_gold_answer")),
        "confidence": confidence,
        "reason": str(raw.get("reason") or ""),
        "gold_questions": questions,
    }


def command_parts(command: str, *, fallbacks: tuple[Path, ...] = ()) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError("agent command is empty")
    if shutil.which(parts[0]) or Path(parts[0]).is_file():
        return parts
    for fallback in fallbacks:
        if fallback.is_file():
            return [str(fallback), *parts[1:]]
    raise FileNotFoundError(f"agent command not found: {parts[0]}")


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def effective_claude_permission_mode(requested: str) -> str:
    requested = requested.strip()
    if running_as_root() and requested in {"bypassPermissions", "dangerously-skip-permissions"}:
        # Claude Code refuses bypassPermissions for root. The explicit
        # --allowedTools list remains active when permission mode is omitted.
        return ""
    return requested


def agent_command(args: argparse.Namespace, model: str, work_dir: Path, prompt: str) -> list[str]:
    if args.agent_backend == "claude":
        cmd = [
            *command_parts(args.agent_command),
            "-p",
            prompt,
            "--model",
            model,
        ]
        permission_mode = effective_claude_permission_mode(args.claude_permission_mode)
        if permission_mode:
            cmd.extend(["--permission-mode", permission_mode])
        if args.claude_allowed_tools:
            cmd.extend(["--allowedTools", args.claude_allowed_tools])
        return cmd

    cmd = [*command_parts(args.agent_command, fallbacks=CODEX_CLI_FALLBACKS), "exec"]
    cmd.extend(
        [
            "--skip-git-repo-check",
            *( ["--ephemeral"] if args.codex_ephemeral else [] ),
            "-m",
            model,
            "-C",
            str(work_dir.resolve()),
            "-s",
            "workspace-write",
            prompt,
        ]
    )
    return cmd


def run_agent(
    args: argparse.Namespace,
    *,
    model: str,
    work_dir: Path,
    prompt: str,
    output_file: str,
) -> dict[str, Any]:
    cmd = agent_command(args, model, work_dir, prompt)
    env = os.environ.copy()
    agent_api_key = (
        env.get(args.agent_api_key_env)
        or env.get("DUMATE_AGENT_API_KEY")
        or env.get(args.api_key_env)
        or env.get("OPENAI_API_KEY", "")
    )
    if args.agent_backend == "claude":
        agent_base_url = (args.agent_base_url or env.get("ANTHROPIC_BASE_URL", "")).rstrip("/")
        if agent_base_url.endswith("/v1"):
            agent_base_url = agent_base_url[:-3]
        if agent_base_url:
            env["ANTHROPIC_BASE_URL"] = agent_base_url
        if agent_api_key:
            env["ANTHROPIC_AUTH_TOKEN"] = agent_api_key
    elif args.agent_base_url:
        env["OPENAI_BASE_URL"] = args.agent_base_url.rstrip("/")
        if agent_api_key:
            env["OPENAI_API_KEY"] = agent_api_key
    path = work_dir / output_file
    last_error = "unknown agent failure"
    for attempt in range(args.agent_retries + 1):
        path.unlink(missing_ok=True)
        try:
            completed = subprocess.run(
                cmd,
                cwd=work_dir,
                env=env,
                text=True,
                capture_output=True,
                timeout=args.agent_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            last_error = f"agent {model} timed out after {args.agent_timeout}s: {exc}"
        else:
            if completed.returncode == 0:
                result = read_json(path)
                if result is not None:
                    return result
                detail = (completed.stdout or completed.stderr or "").strip()[-2000:]
                last_error = f"agent {model} did not write valid {output_file}: {detail}"
            else:
                detail = (completed.stderr or completed.stdout or "").strip()[-3000:]
                last_error = f"agent {model} exited {completed.returncode}: {detail}"
        if attempt < args.agent_retries:
            delay = min(
                args.agent_retry_max_delay,
                args.agent_retry_initial_delay * (2**attempt),
            )
            print(
                f"retrying agent {model} after process failure "
                f"({attempt + 1}/{args.agent_retries + 1}) in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise RuntimeError(f"{last_error}; failed after {args.agent_retries + 1} process attempts")


def generator_prompt(context_file: Path, questions: list[dict[str, Any]], output_file: str) -> str:
    return f"""你是 DuMateBench gold answer 计算 agent。请自主读取文件、执行代码并独立复算答案，不能只凭语言猜测。

上下文文件：{context_file}
待回答问题：{json.dumps(questions, ensure_ascii=False, indent=2)}

要求：
- 读取 context 中 canonical workspace_seed/web_reference 的实际文件；必要时使用 Python、表格/数据库解析工具进行计算。
- 综合比较不同 backend 的 run_outputs，但这些产物只是有噪声的候选证据，不能按多数直接当真。
- 对每个答案记录完整的复算方法、命令或公式、容差以及证据路径。
- 动态数据、无法复算的结论或不充分的数据必须写入 limitations，不得伪造答案。
- evidence 路径使用 workspace_seed/...、web_reference/... 或 runs/<backend>/run_outputs/... 逻辑路径，不写本机绝对路径。
- 只把最终 JSON 写到 {output_file}，不要修改 context 中的 task 或 runs。

输出 schema：
{{
  "schema_version": "1.0",
  "task_id": "...",
  "model": "...",
  "gold_answers": [
    {{
      "id": "...",
      "question": "...",
      "answer_type": "number|table|records|mapping|text|behavior",
      "answer": null,
      "derivation": "可审计的计算过程",
      "verification": {{"method": "...", "commands": ["..."], "tolerance": null}},
      "evidence": [{{"path": "workspace_seed/...", "role": "input|cross_check"}}],
      "related_outputs": ["run_outputs/..."]
    }}
  ],
  "run_comparison": {{"runs": [], "agreement_analysis": "..."}},
  "limitations": [],
  "self_confidence": 0.0
}}
"""


def verifier_prompt(
    context_file: Path,
    candidate_file: Path,
    output_file: str,
    generator_model: str,
) -> str:
    return f"""你是独立 gold answer verifier。生成模型是 {generator_model}，你必须自己读取输入并复算，不能仅评价文字是否合理。

任务上下文：{context_file}
待验证 candidate：{candidate_file}

要求：
- 对 candidate 中每一个 gold answer 独立执行计算或检查。
- 检查不同 runs 的产物是否支持或反驳答案，并区分 run 的共同错误与真实一致性。
- 只有所有重要答案在给定容差内一致、证据充分、且答案不是时间敏感/主观结论时，overall_verdict 才能为 agree。
- 把最终 JSON 写到 {output_file}，不要修改其他文件。

输出 schema：
{{
  "schema_version": "1.0",
  "verifier_model": "...",
  "overall_verdict": "agree|minor_correction|disagree|unverifiable",
  "confidence": 0.0,
  "claim_verifications": [
    {{"id": "...", "verdict": "agree|disagree|unverifiable", "independent_answer": null, "method": "...", "difference": "..."}}
  ],
  "run_evidence_analysis": "...",
  "issues": []
}}
"""


def validate_candidate(raw: dict[str, Any], question_ids: set[str]) -> dict[str, Any]:
    answers = raw.get("gold_answers")
    valid_answers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in answers if isinstance(answers, list) else []:
        if not isinstance(item, dict):
            continue
        answer_id = str(item.get("id") or "")
        if answer_id not in question_ids or answer_id in seen or "answer" not in item:
            continue
        seen.add(answer_id)
        valid_answers.append(item)
    try:
        confidence = max(0.0, min(1.0, float(raw.get("self_confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "schema_version": "1.0",
        "task_id": str(raw.get("task_id") or ""),
        "model": str(raw.get("model") or ""),
        "gold_answers": valid_answers,
        "run_comparison": raw.get("run_comparison") if isinstance(raw.get("run_comparison"), dict) else {},
        "limitations": raw.get("limitations") if isinstance(raw.get("limitations"), list) else [],
        "self_confidence": confidence,
        "complete": seen == question_ids,
    }


def validate_verification(raw: dict[str, Any], answer_ids: set[str], model: str) -> dict[str, Any]:
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    claims = raw.get("claim_verifications")
    claim_results = [item for item in claims if isinstance(item, dict)] if isinstance(claims, list) else []
    agreed_ids = {
        str(item.get("id"))
        for item in claim_results
        if str(item.get("verdict") or "").lower() == "agree"
    }
    verdict = str(raw.get("overall_verdict") or "unverifiable").lower()
    return {
        "verifier_model": model,
        "overall_verdict": verdict,
        "confidence": confidence,
        "claim_verifications": claim_results,
        "run_evidence_analysis": str(raw.get("run_evidence_analysis") or ""),
        "issues": raw.get("issues") if isinstance(raw.get("issues"), list) else [],
        "fully_agrees": verdict == "agree" and agreed_ids == answer_ids,
    }


def adjudication_messages(
    selection: dict[str, Any],
    candidate: dict[str, Any],
    verifications: list[dict[str, Any]],
) -> list[dict[str, str]]:
    payload = {
        "selection": selection,
        "candidate": candidate,
        "verifications": verifications,
    }
    system = (
        "You are a conservative gold-answer consensus adjudicator. Return one JSON object only. "
        "Do not correct or invent answers; only accept answers independently verified by multiple distinct models."
    )
    user = f"""判断 candidate 是否足以作为 benchmark gold answer。

只有当以下条件全部满足才 accepted=true：答案确定且非时间敏感；推导可审计；所有 answer id 均被至少两个不同 verifier 独立复算为 agree；不同 runs 的一致性分析没有暴露共同错误；无关键 limitation。

返回：
{{
  "accepted": false,
  "confidence": 0.0,
  "reason": "...",
  "agreement_summary": "...",
  "approved_answer_ids": []
}}

材料：
{json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_adjudication(raw: dict[str, Any], answer_ids: set[str]) -> dict[str, Any]:
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    approved = {str(item) for item in raw.get("approved_answer_ids", []) if isinstance(item, str)}
    return {
        "accepted": bool(raw.get("accepted")) and approved == answer_ids,
        "confidence": confidence,
        "reason": str(raw.get("reason") or ""),
        "agreement_summary": str(raw.get("agreement_summary") or ""),
        "approved_answer_ids": sorted(approved),
    }


def append_gold_to_reference_manifest(task_dir: Path) -> None:
    manifest_path = task_dir / "evaluator" / "llm_judge_references.json"
    gold_path = DEFAULT_GOLD_FILE
    try:
        raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        text = manifest_path.read_text(encoding="utf-8", errors="ignore")
        existing = {
            line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        }
        if gold_path not in existing:
            if text and not text.endswith("\n"):
                text += "\n"
            manifest_path.write_text(text + gold_path + "\n", encoding="utf-8")
        return

    if isinstance(raw, dict) and isinstance(raw.get("references"), list):
        references = raw["references"]
        existing = {
            item if isinstance(item, str) else item.get("path")
            for item in references
            if isinstance(item, (str, dict))
        }
        if gold_path not in existing:
            if references and all(isinstance(item, str) for item in references):
                references.append(gold_path)
            else:
                used = {
                    str(item.get("as"))
                    for item in references
                    if isinstance(item, dict) and isinstance(item.get("as"), str)
                }
                alias = "gold_answer_reference.json"
                index = 2
                while alias in used:
                    alias = f"gold_answer_reference_{index}.json"
                    index += 1
                references.append({"path": gold_path, "as": alias})
            write_json(manifest_path, raw)
            return
    raise ValueError(f"unsupported reference manifest format: {manifest_path}")


def materialize_gold(
    task_dir: Path,
    selection: dict[str, Any],
    candidate: dict[str, Any],
    verifications: list[dict[str, Any]],
    adjudication: dict[str, Any],
    confidence: float,
    args: argparse.Namespace,
    context: dict[str, Any],
) -> Path:
    run_models = sorted({str(item["backend"]) for item in context["runs"]})
    document = {
        "schema_version": "1.0",
        "task_id": task_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gold_answers": candidate["gold_answers"],
        "confidence": {
            "score": confidence,
            "level": "high" if confidence >= 0.9 else "sufficient",
            "selection_confidence": selection["confidence"],
            "generator_self_confidence": candidate["self_confidence"],
            "verifier_confidences": {
                item["verifier_model"]: item["confidence"] for item in verifications
            },
            "adjudicator_confidence": adjudication["confidence"],
            "agreement_summary": adjudication["agreement_summary"],
        },
        "provenance": {
            "selector_model": args.selector_model,
            "generator_model": args.generator_model,
            "verifier_models": [item["verifier_model"] for item in verifications],
            "adjudicator_model": args.adjudicator_model,
            "compared_run_backends": run_models,
        },
        "run_comparison": candidate["run_comparison"],
        "limitations": candidate["limitations"],
        "adjudication": {
            "reason": adjudication["reason"],
            "approved_answer_ids": adjudication["approved_answer_ids"],
        },
    }
    output_path = task_dir / DEFAULT_GOLD_FILE
    write_json(output_path, document)
    append_gold_to_reference_manifest(task_dir)
    return output_path


def process_task(
    args: argparse.Namespace,
    task_dir: Path,
    task_views: list[tuple[str, Path]],
    api_key: str,
) -> dict[str, Any]:
    gold_path = task_dir / DEFAULT_GOLD_FILE
    if gold_path.is_file() and not args.overwrite:
        if not args.dry_run:
            append_gold_to_reference_manifest(task_dir)
        return {"task_id": task_dir.name, "status": "skipped_existing", "gold_file": str(gold_path)}
    if not task_views:
        raise FileNotFoundError(f"no runs found for {task_dir.name}")
    context = build_task_context(task_dir, task_views)
    if args.dry_run:
        return {
            "task_id": task_dir.name,
            "status": "dry_run",
            "run_count": len(task_views),
            "input_count": len(context["input_inventory"]),
            "artifact_count": len(context["artifacts_and_criteria"]),
        }

    raw_selection = call_openai_compatible_json(
        selection_messages(context),
        model=args.selector_model,
        base_url=args.base_url,
        api_key=api_key,
        timeout=args.llm_timeout,
        retries=args.retries,
    )
    selection = validate_selection(raw_selection, args.selection_threshold)
    base_result: dict[str, Any] = {
        "task_id": task_dir.name,
        "selection": selection,
        "run_count": len(task_views),
    }
    if not selection["needs_gold_answer"]:
        return {**base_result, "status": "not_needed"}
    if args.screen_only:
        return {**base_result, "status": "selected"}

    work_parent = args.work_dir.resolve() if args.work_dir else None
    if work_parent:
        work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"gold_{safe_name(task_dir.name)}_", dir=work_parent) as temp_name:
        work_dir = Path(temp_name)
        context_file = work_dir / "task_context.json"
        write_json(context_file, context)
        candidate_file = "gold_candidate.json"
        raw_candidate = run_agent(
            args,
            model=args.generator_model,
            work_dir=work_dir,
            prompt=generator_prompt(context_file, selection["gold_questions"], candidate_file),
            output_file=candidate_file,
        )
        question_ids = {item["id"] for item in selection["gold_questions"]}
        candidate = validate_candidate(raw_candidate, question_ids)
        if not candidate["complete"] or candidate["self_confidence"] < args.confidence_threshold:
            return {**base_result, "status": "candidate_insufficient", "candidate": candidate}

        verifications: list[dict[str, Any]] = []
        answer_ids = {str(item["id"]) for item in candidate["gold_answers"]}
        for index, model in enumerate(args.verifier_model, start=1):
            verify_dir = work_dir / f"verify_{index}_{safe_name(model)}"
            verify_dir.mkdir(parents=True, exist_ok=True)
            verify_context = verify_dir / "task_context.json"
            verify_candidate = verify_dir / "gold_candidate.json"
            shutil.copy2(context_file, verify_context)
            shutil.copy2(work_dir / candidate_file, verify_candidate)
            output_file = "verification.json"
            raw_verification = run_agent(
                args,
                model=model,
                work_dir=verify_dir,
                prompt=verifier_prompt(verify_context, verify_candidate, output_file, args.generator_model),
                output_file=output_file,
            )
            verifications.append(validate_verification(raw_verification, answer_ids, model))

        agreeing = [
            item
            for item in verifications
            if item["fully_agrees"] and item["confidence"] >= args.confidence_threshold
        ]
        if len(agreeing) < args.min_verifiers:
            return {
                **base_result,
                "status": "verification_failed",
                "candidate": candidate,
                "verifications": verifications,
            }

        raw_adjudication = call_openai_compatible_json(
            adjudication_messages(selection, candidate, verifications),
            model=args.adjudicator_model,
            base_url=args.base_url,
            api_key=api_key,
            timeout=args.llm_timeout,
            retries=args.retries,
        )
        adjudication = validate_adjudication(raw_adjudication, answer_ids)
        confidence = min(
            selection["confidence"],
            candidate["self_confidence"],
            *(item["confidence"] for item in agreeing),
            adjudication["confidence"],
        )
        if not adjudication["accepted"] or confidence < args.confidence_threshold:
            return {
                **base_result,
                "status": "adjudication_rejected",
                "candidate": candidate,
                "verifications": verifications,
                "adjudication": adjudication,
                "confidence": confidence,
            }
        output_path = materialize_gold(
            task_dir,
            selection,
            candidate,
            verifications,
            adjudication,
            confidence,
            args,
            context,
        )
        return {
            **base_result,
            "status": "wrote",
            "gold_file": str(output_path),
            "confidence": confidence,
            "answer_count": len(candidate["gold_answers"]),
            "verifier_models": [item["verifier_model"] for item in agreeing],
        }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--locked-dir", type=Path, default=DEFAULT_LOCKED_DIR)
    parser.add_argument("--task-id", action="append", default=[], help="Limit to one task_id. Repeatable.")
    parser.add_argument(
        "--retry-failures-from",
        type=Path,
        default=None,
        help="Only process task IDs listed in the failures array of a previous gold_answer_summary.json.",
    )
    parser.add_argument("--selector-model", default=DEFAULT_SELECTOR_MODEL)
    parser.add_argument("--generator-model", default=DEFAULT_GENERATOR_MODEL)
    parser.add_argument("--verifier-model", action="append", default=None, help="Repeat for independent verifier models.")
    parser.add_argument("--adjudicator-model", default=DEFAULT_SELECTOR_MODEL)
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--agent-backend", choices=("codex", "claude"), default="codex")
    parser.add_argument("--agent-command", default="codex")
    parser.add_argument(
        "--agent-base-url",
        default=os.environ.get("DUMATE_AGENT_BASE_URL", ""),
        help="Native agent endpoint. Claude accepts either the host root or a URL ending in /v1.",
    )
    parser.add_argument("--agent-api-key-env", default="DUMATE_AGENT_API_KEY")
    parser.add_argument(
        "--codex-full-auto",
        action="store_true",
        help="Deprecated compatibility flag; workspace-write sandbox mode is always used.",
    )
    parser.add_argument("--codex-ephemeral", action="store_true")
    parser.add_argument("--claude-permission-mode", default="bypassPermissions")
    parser.add_argument("--claude-allowed-tools", default="Bash,Read,Write,Edit,Glob,Grep,LS")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--llm-timeout", type=int, default=180)
    parser.add_argument("--agent-timeout", type=int, default=1800)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--agent-retries", type=int, default=5, help="Retries after an entire agent CLI process fails.")
    parser.add_argument("--agent-retry-initial-delay", type=float, default=5.0)
    parser.add_argument("--agent-retry-max-delay", type=float, default=60.0)
    parser.add_argument("--selection-threshold", type=float, default=0.8)
    parser.add_argument("--confidence-threshold", type=float, default=0.85)
    parser.add_argument("--min-verifiers", type=int, default=2)
    parser.add_argument("--work-dir", type=Path, default=None, help="Parent for temporary agent workspaces.")
    parser.add_argument("--summary-file", type=Path, default=None)
    parser.add_argument("--screen-only", action="store_true", help="Run task selection only; do not run agents or write gold files.")
    parser.add_argument("--dry-run", action="store_true", help="Only validate local task/run discovery; do not call models or write files.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate existing gold answer references.")
    args = parser.parse_args(argv)
    if args.verifier_model is None:
        args.verifier_model = list(DEFAULT_VERIFIER_MODELS)
    if args.agent_backend == "claude" and args.agent_command == "codex":
        args.agent_command = "claude"
    if args.generator_model in args.verifier_model:
        parser.error("--verifier-model must differ from --generator-model")
    if len(set(args.verifier_model)) != len(args.verifier_model):
        parser.error("--verifier-model values must be distinct")
    if args.min_verifiers < 1 or args.min_verifiers > len(args.verifier_model):
        parser.error("--min-verifiers must be between 1 and the number of verifier models")
    if args.agent_retries < 0:
        parser.error("--agent-retries must be at least 0")
    if args.agent_retry_initial_delay < 0 or args.agent_retry_max_delay < 0:
        parser.error("agent retry delays must be non-negative")
    for name in ("selection_threshold", "confidence_threshold"):
        if not 0 <= getattr(args, name) <= 1:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    return args


def print_result(result: dict[str, Any]) -> None:
    status = result["status"]
    task_id = result["task_id"]
    if status == "wrote":
        print(
            f"wrote {task_id}: {result['answer_count']} answers, "
            f"confidence={result['confidence']:.3f}, {result['gold_file']}"
        )
    elif status == "not_needed":
        selection = result["selection"]
        print(f"not needed {task_id}: confidence={selection['confidence']:.3f}; {selection['reason']}")
    elif status == "selected":
        selection = result["selection"]
        print(f"selected {task_id}: {len(selection['gold_questions'])} questions; {selection['reason']}")
    else:
        print(f"{status} {task_id}")


def load_failed_task_ids(path: Path) -> set[str]:
    data = read_json(path)
    if data is None or not isinstance(data.get("failures"), list):
        raise ValueError(f"summary does not contain a failures array: {path}")
    return {
        str(item.get("task_id")).lower()
        for item in data["failures"]
        if isinstance(item, dict) and item.get("task_id")
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not args.runs_dir.is_dir() or not args.locked_dir.is_dir():
        print("--runs-dir and --locked-dir must be existing directories", file=sys.stderr)
        return 2
    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not api_key:
        print(f"{args.api_key_env} is required unless --dry-run is used", file=sys.stderr)
        return 2
    if not args.dry_run and not args.screen_only:
        try:
            command_parts(
                args.agent_command,
                fallbacks=CODEX_CLI_FALLBACKS if args.agent_backend == "codex" else (),
            )
        except (ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

    views_by_id = index_task_views(args.runs_dir)
    requested = {value.lower() for value in args.task_id}
    retry_failures = load_failed_task_ids(args.retry_failures_from) if args.retry_failures_from else set()
    if args.retry_failures_from and not retry_failures:
        print(f"no failed task IDs found in {args.retry_failures_from}")
        return 0
    task_dirs = [
        path
        for path in sorted(args.locked_dir.iterdir())
        if path.is_dir()
        and (path / "instruction.md").is_file()
        and (not requested or path.name.lower() in requested)
        and (not args.retry_failures_from or path.name.lower() in retry_failures)
    ]
    if not task_dirs:
        print(f"no matching locked tasks under {args.locked_dir}", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                process_task,
                args,
                task_dir,
                views_by_id.get(task_dir.name.lower(), []),
                api_key,
            ): task_dir
            for task_dir in task_dirs
        }
        for future in concurrent.futures.as_completed(futures):
            task_dir = futures[future]
            try:
                result = future.result()
                results.append(result)
                print_result(result)
            except Exception as exc:  # noqa: BLE001 - keep independent task processing alive.
                failure = {"task_id": task_dir.name, "error": f"{type(exc).__name__}: {exc}"}
                failures.append(failure)
                print(f"failed {failure['task_id']}: {failure['error']}", file=sys.stderr)

    results.sort(key=lambda item: item["task_id"])
    failures.sort(key=lambda item: item["task_id"])
    summary = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs_dir": str(args.runs_dir),
        "locked_dir": str(args.locked_dir),
        "results": results,
        "failures": failures,
    }
    if not args.dry_run:
        default_summary_name = "gold_answer_retry_summary.json" if args.retry_failures_from else "gold_answer_summary.json"
        summary_file = args.summary_file or (args.locked_dir / default_summary_name)
        write_json(summary_file, summary)
        print(f"summary: {summary_file}")
    print(f"completed {len(results)} tasks; wrote {sum(item['status'] == 'wrote' for item in results)}; failed {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
