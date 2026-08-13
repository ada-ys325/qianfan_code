#!/usr/bin/env python3
"""Add missing references to existing locked LLM judge reference files."""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEFAULT_RUNS_DIR = REPO_ROOT / "cc_runs"
DEFAULT_LOCKED_DIR = ROOT / "locked_llm_judge_inputs"
DEFAULT_MODEL = "gemini-3.1-pro-preview"
REFERENCE_ROOTS = ("workspace_seed", "web_reference")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_task_id(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    match = re.search(r"^task_id:\s*(.+?)\s*$", text, flags=re.M)
    return match.group(1).strip().strip("'\"") if match else ""


def normalize_reference_path(value: str) -> str:
    path = value.strip().replace("\\", "/").lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith(("history_agent_files/", "uploads/", "uploads_raw/")):
        path = f"workspace_seed/{path}"
    return path


def is_allowed_reference_path(path: str) -> bool:
    normalized = normalize_reference_path(path)
    return normalized.startswith(("workspace_seed/", "web_reference/", ".llm_judge_selected_references/")) or normalized in {
        "evaluator/gold_answer_reference.json",
    }


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def index_task_views(runs_dir: Path) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for task_yaml in sorted(runs_dir.rglob("task.yaml")):
        if task_yaml.parent.name != "task_view":
            continue
        task_id = load_task_id(task_yaml)
        if not task_id:
            continue
        result.setdefault(task_id.lower(), []).append(task_yaml.parent)
    return result


def collect_candidates(task_views: list[Path]) -> list[dict[str, Any]]:
    sizes: dict[str, int] = {}
    occurrences: dict[str, int] = {}
    for task_view in task_views:
        found_in_view: set[str] = set()
        for root_name in REFERENCE_ROOTS:
            root = task_view / root_name
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(task_view).as_posix()
                found_in_view.add(relative)
                try:
                    sizes[relative] = max(sizes.get(relative, 0), path.stat().st_size)
                except OSError:
                    sizes.setdefault(relative, 0)
        for relative in found_in_view:
            occurrences[relative] = occurrences.get(relative, 0) + 1
    return [
        {"path": path, "size_bytes": sizes[path], "run_count": occurrences[path]}
        for path in sorted(sizes)
    ]


def candidate_paths(candidates: list[dict[str, Any]]) -> set[str]:
    return {str(item["path"]) for item in candidates if isinstance(item.get("path"), str)}


def local_reference_paths(task_dir: Path) -> set[str]:
    result: set[str] = set()
    for relative in ("evaluator/gold_answer_reference.json",):
        if (task_dir / relative).is_file():
            result.add(relative)
    return result


def resolve_reference_path(path: str, allowed: set[str]) -> tuple[str, str]:
    normalized = normalize_reference_path(path)
    if normalized in allowed:
        return normalized, "ok"

    name = Path(normalized).name
    if not name:
        return normalized, "missing"
    matches = [item for item in sorted(allowed) if Path(item).name == name]
    if normalized.startswith("workspace_seed/"):
        preferred = [item for item in matches if item.startswith("workspace_seed/")]
        if len(preferred) == 1:
            return preferred[0], "repaired"
    if normalized.startswith("web_reference/"):
        preferred = [item for item in matches if item.startswith("web_reference/")]
        if len(preferred) == 1:
            return preferred[0], "repaired"
    if len(matches) == 1:
        return matches[0], "repaired"
    return normalized, "ambiguous" if matches else "missing"


def load_reference_document(path: Path) -> tuple[str, dict[str, Any] | None, list[Any], list[str]]:
    data = read_json(path)
    if data is not None and isinstance(data.get("references"), list):
        raw_entries = list(data["references"])
        paths: list[str] = []
        for item in raw_entries:
            if isinstance(item, str):
                paths.append(normalize_reference_path(item))
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                paths.append(normalize_reference_path(item["path"]))
        return "json", data, raw_entries, dedupe(paths)

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    paths = [normalize_reference_path(line) for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return "text", None, lines, dedupe(paths)


def repair_reference_document(
    path: Path,
    document_type: str,
    json_doc: dict[str, Any] | None,
    raw_entries: list[Any],
    allowed: set[str],
    *,
    dry_run: bool,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    repaired: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []

    def repair_value(value: str) -> tuple[str, str]:
        normalized = normalize_reference_path(value)
        if not is_allowed_reference_path(normalized):
            removed.append({"path": normalized, "reason": "not_reference_input"})
            return normalized, "removed"
        fixed, status = resolve_reference_path(normalized, allowed)
        if status == "repaired":
            repaired.append({"from": normalized, "to": fixed})
        elif status in {"missing", "ambiguous"}:
            unresolved.append({"path": normalized, "status": status})
        return fixed, status

    if document_type == "json":
        assert json_doc is not None
        new_entries: list[Any] = []
        paths: list[str] = []
        for item in raw_entries:
            if isinstance(item, str):
                fixed, status = repair_value(item)
                if status == "removed":
                    continue
                new_entries.append(fixed)
                paths.append(fixed)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                fixed, status = repair_value(item["path"])
                if status == "removed":
                    continue
                new_item = dict(item)
                new_item["path"] = fixed
                new_entries.append(new_item)
                paths.append(fixed)
            else:
                new_entries.append(item)
        if (repaired or removed) and not dry_run:
            json_doc["references"] = new_entries
            path.write_text(json.dumps(json_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return dedupe(paths), repaired, unresolved, removed

    new_lines: list[str] = []
    paths = []
    for line in raw_entries:
        if not isinstance(line, str) or not line.strip() or line.lstrip().startswith("#"):
            new_lines.append(line)
            continue
        fixed, status = repair_value(line)
        if status == "removed":
            continue
        new_lines.append(fixed)
        paths.append(fixed)
    if (repaired or removed) and not dry_run:
        path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    return dedupe(paths), repaired, unresolved, removed


def load_criteria_context(task_dir: Path) -> list[dict[str, Any]]:
    evaluator_dir = task_dir / "evaluator"
    artifacts_doc = read_json(evaluator_dir / "llm_judge_artifacts.json") or {}
    artifacts = artifacts_doc.get("artifacts")
    result: list[dict[str, Any]] = []
    criteria_paths: list[tuple[str, Path]] = []

    for artifact in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(artifact, dict):
            continue
        criteria_file = artifact.get("criteria_file")
        if isinstance(criteria_file, str):
            criteria_paths.append((str(artifact.get("output_file") or artifact.get("id") or ""), task_dir / criteria_file))

    if not criteria_paths:
        criteria_paths = [("", path) for path in sorted((evaluator_dir / "criteria").glob("*.json"))]

    for output_file, criteria_path in criteria_paths:
        data = read_json(criteria_path) or {}
        compact: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        criteria = data.get("criteria")
        for item in criteria if isinstance(criteria, list) else []:
            if not isinstance(item, dict):
                continue
            criterion_id = str(item.get("id") or "")
            description = str(item.get("description") or "")
            key = (criterion_id, description)
            if key in seen:
                continue
            seen.add(key)
            compact.append(
                {
                    "id": criterion_id,
                    "dimension": item.get("dimension"),
                    "description": description,
                    "weight": item.get("weight"),
                }
            )
        result.append(
            {
                "output_file": output_file,
                "criteria_file": criteria_path.relative_to(task_dir).as_posix(),
                "criteria": compact,
            }
        )
    return result


def build_prompt(
    task_id: str,
    instruction: str,
    criteria_context: list[dict[str, Any]],
    existing_references: list[str],
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    system = (
        "You select reference files needed by an LLM judge. Return one JSON object only. "
        "You may only add files from the supplied candidate inventory. Never invent or rewrite paths."
    )
    user = f"""请补全 task 的固定 reference 文件清单。

判断原则：
1. 结合 instruction.md 和每个 output artifact 的 criteria，选择评估正确性、忠实度、编辑保真度或内容完整性所必需的输入、原文件、素材、数据、模板或事实依据。
2. instruction.md 明确提到需要沿用、修改、转换、分析或对照的文件，通常应选入。
3. 当 criteria 要求事实/内容忠实度时，应选入相关 web_reference；当仅凭 output 自身即可评分时，不要无关扩张。
4. 不要选择 README、manifest、缓存、环境文件或生成脚本，除非 instruction/criteria 明确要求以其作为评估依据。
5. existing_references 必须保留。只返回其中缺少且确实必要的文件。
6. path 必须逐字复制 candidate_inventory 中的 path。

返回格式：
{{
  "missing_references": [
    {{"path": "workspace_seed/...", "reason": "为什么它对现有 criteria 的评估必要"}}
  ]
}}

task_id:
{task_id}

instruction.md:
{instruction}

artifacts_and_criteria:
{json.dumps(criteria_context, ensure_ascii=False, separators=(",", ":"))}

existing_references:
{json.dumps(existing_references, ensure_ascii=False)}

candidate_inventory:
{json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


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


def parse_llm_json_content(content: Any) -> dict[str, Any]:
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
    request_data = json.dumps(
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
            request = urllib.request.Request(url, data=request_data, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            parsed = json.loads(body)
            return parse_llm_json_content(parsed["choices"][0]["message"]["content"])
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


def selected_missing_paths(payload: dict[str, Any], allowed: set[str], existing: set[str]) -> list[str]:
    values = payload.get("missing_references")
    if not isinstance(values, list):
        values = payload.get("references")
    result: list[str] = []
    for item in values if isinstance(values, list) else []:
        raw = item.get("path") if isinstance(item, dict) else item
        if not isinstance(raw, str):
            continue
        path = normalize_reference_path(raw)
        if path in allowed and path not in existing:
            result.append(path)
    return dedupe(result)


def unique_alias(path: str, used: set[str]) -> str:
    alias = Path(path).name or "reference"
    if alias not in used:
        used.add(alias)
        return alias
    stem = Path(alias).stem or "reference"
    suffix = Path(alias).suffix
    index = 2
    while f"{stem}_{index}{suffix}" in used:
        index += 1
    alias = f"{stem}_{index}{suffix}"
    used.add(alias)
    return alias


def append_references(
    path: Path,
    document_type: str,
    json_doc: dict[str, Any] | None,
    raw_entries: list[Any],
    additions: list[str],
) -> None:
    if document_type == "text":
        text = path.read_text(encoding="utf-8", errors="ignore")
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join(additions) + ("\n" if additions else "")
        path.write_text(text, encoding="utf-8")
        return

    assert json_doc is not None
    string_style = bool(raw_entries) and all(isinstance(item, str) for item in raw_entries)
    if string_style:
        raw_entries.extend(additions)
    else:
        used_aliases = {
            str(item.get("as"))
            for item in raw_entries
            if isinstance(item, dict) and isinstance(item.get("as"), str)
        }
        raw_entries.extend({"path": item, "as": unique_alias(item, used_aliases)} for item in additions)
    json_doc["references"] = raw_entries
    path.write_text(json.dumps(json_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_task(
    task_dir: Path,
    task_views: list[Path],
    args: argparse.Namespace,
    api_key: str,
) -> dict[str, Any]:
    task_id = task_dir.name
    instruction_path = task_dir / "instruction.md"
    reference_path = task_dir / "evaluator" / "llm_judge_references.json"
    if not instruction_path.is_file():
        raise FileNotFoundError(f"missing {instruction_path}")
    if not reference_path.is_file():
        raise FileNotFoundError(f"missing {reference_path}")

    document_type, json_doc, raw_entries, existing = load_reference_document(reference_path)
    candidates = collect_candidates([task_dir, *task_views])
    allowed = candidate_paths(candidates) | local_reference_paths(task_dir)
    existing, repaired, unresolved, removed = repair_reference_document(
        reference_path,
        document_type,
        json_doc,
        raw_entries,
        allowed,
        dry_run=args.dry_run,
    )
    if (repaired or removed) and not args.dry_run:
        document_type, json_doc, raw_entries, existing = load_reference_document(reference_path)
    criteria_context = load_criteria_context(task_dir)

    if args.dry_run or args.repair_only:
        return {
            "task_id": task_id,
            "status": "dry_run" if args.dry_run else ("repaired" if repaired else "checked"),
            "existing": len(existing),
            "candidates": len(candidates),
            "added": [],
            "repaired": repaired,
            "unresolved": unresolved,
            "removed": removed,
        }

    payload = call_openai_compatible_json(
        build_prompt(
            task_id,
            instruction_path.read_text(encoding="utf-8", errors="ignore"),
            criteria_context,
            existing,
            candidates,
        ),
        model=args.model,
        base_url=args.base_url,
        api_key=api_key,
        timeout=args.timeout,
        retries=args.retries,
    )
    additions = selected_missing_paths(payload, allowed, set(existing))
    if additions:
        append_references(reference_path, document_type, json_doc, raw_entries, additions)
    return {
        "task_id": task_id,
        "status": "updated" if additions or repaired else "unchanged",
        "existing": len(existing),
        "candidates": len(candidates),
        "added": additions,
        "repaired": repaired,
        "unresolved": unresolved,
        "removed": removed,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--locked-dir", type=Path, default=DEFAULT_LOCKED_DIR)
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Discover evaluator/llm_judge_references.json recursively under --locked-dir.",
    )
    parser.add_argument("--task-id", action="append", default=[], help="Limit to one task_id. Repeatable.")
    parser.add_argument("--model", default=os.environ.get("DUMATE_LLM_JUDGE_LOCK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Validate and report task inventories without calling the LLM or writing files.")
    parser.add_argument("--repair-only", action="store_true", help="Only validate and repair existing reference paths; do not call the LLM.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not args.runs_dir.is_dir():
        print(f"runs directory does not exist: {args.runs_dir}", file=sys.stderr)
        return 2
    if not args.locked_dir.is_dir():
        print(f"locked directory does not exist: {args.locked_dir}", file=sys.stderr)
        return 2

    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not args.repair_only and not api_key:
        print(f"{args.api_key_env} is required unless --dry-run is used", file=sys.stderr)
        return 2

    task_views = index_task_views(args.runs_dir)
    requested = {item.lower() for item in args.task_id}
    reference_files = (
        sorted(args.locked_dir.rglob("evaluator/llm_judge_references.json"))
        if args.recursive
        else sorted(args.locked_dir.glob("*/evaluator/llm_judge_references.json"))
    )
    task_dirs = [
        path.parent.parent
        for path in reference_files
        if not requested or path.parent.parent.name.lower() in requested
    ]
    if not task_dirs:
        print(f"no matching existing llm_judge_references.json files under {args.locked_dir}", file=sys.stderr)
        return 1

    failures: list[tuple[str, BaseException]] = []
    results: list[dict[str, Any]] = []
    workers = max(1, args.workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_task, task_dir, task_views.get(task_dir.name.lower(), []), args, api_key): task_dir
            for task_dir in task_dirs
        }
        for future in concurrent.futures.as_completed(futures):
            task_dir = futures[future]
            try:
                result = future.result()
                results.append(result)
                added = result["added"]
                repaired = result.get("repaired", [])
                unresolved = result.get("unresolved", [])
                removed = result.get("removed", [])
                print(
                    f"{result['status']} {result['task_id']}: "
                    f"{len(added)} added, {len(repaired)} repaired, "
                    f"{len(removed)} removed, {len(unresolved)} unresolved, {result['existing']} existing, "
                    f"{result['candidates']} candidates"
                )
                for path in added:
                    print(f"  + {path}")
                for item in repaired:
                    print(f"  ~ {item['from']} -> {item['to']}")
                for item in unresolved:
                    print(f"  ! {item['status']}: {item['path']}")
                for item in removed:
                    print(f"  - {item['reason']}: {item['path']}")
            except Exception as exc:  # noqa: BLE001 - continue processing independent tasks.
                failures.append((task_dir.name, exc))
                print(f"failed {task_dir.name}: {type(exc).__name__}: {exc}", file=sys.stderr)

    added_total = sum(len(item["added"]) for item in results)
    repaired_total = sum(len(item.get("repaired", [])) for item in results)
    removed_total = sum(len(item.get("removed", [])) for item in results)
    unresolved_total = sum(len(item.get("unresolved", [])) for item in results)
    print(
        f"completed {len(results)} tasks; added {added_total} references; "
        f"repaired {repaired_total}; removed {removed_total}; "
        f"unresolved {unresolved_total}; failed {len(failures)}"
    )
    if removed_total:
        print("removed invalid references:")
        for result in sorted(results, key=lambda item: str(item.get("task_id", ""))):
            for item in result.get("removed", []):
                print(f"  {result['task_id']}: {item['reason']}: {item['path']}")
    if unresolved_total:
        print("unresolved references:")
        for result in sorted(results, key=lambda item: str(item.get("task_id", ""))):
            for item in result.get("unresolved", []):
                print(f"  {result['task_id']}: {item['status']}: {item['path']}")
    return 1 if failures or unresolved_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
