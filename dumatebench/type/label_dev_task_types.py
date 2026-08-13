#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

TYPE_KEYS = [
    "read_docx",
    "read_pptx",
    "read_excel",
    "read_pdf",
    "edit_docx",
    "edit_pptx",
    "edit_excel",
    "edit_pdf",
    "file_organization",
    "code_writing",
    "web_retrieval",
    "text_generation",
    "image_generation_or_editing",
    "video_generation_or_editing",
    "audio_generation_or_editing",
    "email_processing",
    "calendar_processing",
]

FEATURE_SCHEMA = {
    "read_docx": 0,
    "read_pptx": 0,
    "read_excel": 0,
    "read_pdf": 0,
    "edit_docx": 0,
    "edit_pptx": 0,
    "edit_excel": 0,
    "edit_pdf": 0,
    "file_organization": 0,
    "code_writing": 0,
    "web_retrieval": 0,
    "text_generation": 0,
    "image_generation_or_editing": 0,
    "video_generation_or_editing": 0,
    "audio_generation_or_editing": 0,
    "email_processing": 0,
    "calendar_processing": 0,
    "history_turns": 0,
    "file_count": 0,
    "file_size_mb": 0.0,
    "user_config_chars": 0,
    "cross_app_count": 0,
    "checklist_length": 0,
    "missing_tools_or_dependencies": 0,
    "resource_limited": 0,
    "network_or_api_errors": 0,
    "workspace_noise_file_count": 0,
}

PRIMARY_FEATURE_FILE = "task_type_features.json"
SINGULAR_FEATURE_FILE = "task_type_feature.json"


def default_tasks_dir() -> Path:
    datasets_dev = Path("dumatebench/datasets/dev")
    if datasets_dev.is_dir():
        return datasets_dev
    return Path("dumatebench/dataset/dev")


def script_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_path(path: Path) -> Path:
    if path.exists() or path.is_absolute():
        return path
    repo_relative = script_repo_root() / path
    if repo_relative.exists():
        return repo_relative
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Label all DuMateBench dev task type features and summarize their distribution."
    )
    parser.add_argument(
        "--tasks-dir",
        default=default_tasks_dir(),
        type=Path,
        help="Dev dataset root. The script recursively finds task dirs containing instruction.md.",
    )
    parser.add_argument(
        "--summary-dir",
        type=Path,
        default=None,
        help="Directory for task_type_feature_summary.json/csv. Defaults to <tasks-dir>.",
    )
    parser.add_argument("--model", default="claude-opus-4-8")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("CUSTOM_BASE_URL")
        or "https://cn.huayanapi.com:27502/v1",
    )
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N tasks. 0 means all.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between successful model calls.")
    parser.add_argument("--no-singular-alias", action="store_true", help="Do not write task_type_feature.json.")
    parser.add_argument(
        "--start-at",
        default="",
        help="Start labeling at this task, inclusive. Match by relative path, task directory name, or absolute path.",
    )
    parser.add_argument(
        "--start-after",
        default="",
        help="Start labeling after this task, exclusive. Match by relative path, task directory name, or absolute path.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help=f"Skip tasks that already have {PRIMARY_FEATURE_FILE}. Useful for resuming interrupted batches.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Record per-task labeling errors and continue with the remaining tasks.",
    )
    parser.add_argument(
        "--error-log",
        type=Path,
        default=None,
        help="JSONL file for failed tasks. Defaults to <summary-dir>/task_type_feature_errors.jsonl.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Do not call the model. Only summarize existing task_type_features.json files.",
    )
    parser.add_argument("--log-config", action="store_true", help="Print masked model/base-url/key config once.")
    return parser.parse_args()


def mask_api_key(key: str) -> str:
    if len(key) <= 10:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def task_id_parts(task_dir: Path, existing: dict[str, Any]) -> tuple[str, str]:
    uid = str(existing.get("uid") or "").strip()
    session_id = str(existing.get("sessionId") or existing.get("session_id") or "").strip()
    if uid and session_id:
        return uid, session_id
    if "_ses_" in task_dir.name:
        uid_part, session_part = task_dir.name.split("_ses_", 1)
        return uid or uid_part, session_id or f"ses_{session_part}"
    parent_uid = task_dir.parent.name if re.fullmatch(r"[0-9a-f]{32}", task_dir.parent.name) else ""
    return uid or parent_uid or task_dir.name, session_id


def count_checks(task_dir: Path) -> int:
    checks_path = task_dir / "evaluator" / "checks.yaml"
    if not checks_path.is_file():
        return 0
    text = checks_path.read_text(encoding="utf-8", errors="ignore")
    return len(re.findall(r"^\s*-\s+type\s*:", text, re.M))


def workspace_seed_stats(task_dir: Path) -> tuple[int, float]:
    seed_dir = task_dir / "workspace_seed"
    if not seed_dir.is_dir():
        return 0, 0.0
    file_count = 0
    total_size = 0
    for path in seed_dir.rglob("*"):
        if not path.is_file():
            continue
        file_count += 1
        try:
            total_size += path.stat().st_size
        except OSError:
            pass
    return file_count, round(total_size / (1024 * 1024), 3)


def build_messages(instruction: str) -> list[dict[str, str]]:
    schema_text = json.dumps({key: 0 for key in TYPE_KEYS}, ensure_ascii=False, indent=2)
    return [
        {
            "role": "system",
            "content": (
                "你是 DuMateBench 任务类型标注员。你只根据 instruction.md 的任务要求判断任务类型。"
                "必须严格使用给定类别，所有类别输出 0 或 1。只输出 JSON，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请根据下面的 instruction.md 判断任务类型。\n\n"
                "类别定义：\n"
                "- read_docx/read_pptx/read_excel/read_pdf：任务需要读取、理解或引用对应格式的已有输入文件。\n"
                "- edit_docx/edit_pptx/edit_excel/edit_pdf：任务要求创建、修改、转换或输出对应格式文件。\n"
                "- file_organization：任务核心包含文件整理、重命名、归档、目录结构调整或批量搬运。\n"
                "- code_writing：任务要求编写、修改、调试、生成脚本、程序、网页、配置或自动化代码。\n"
                "- web_retrieval：任务需要联网检索、调用外部网站/API、获取实时/在线数据或爬取网页。\n"
                "- text_generation：任务要求撰写、总结、改写、翻译、生成文案/报告/说明等自然语言文本。\n"
                "- image_generation_or_editing/video_generation_or_editing/audio_generation_or_editing：任务要求生成或编辑对应媒体。\n"
                "- email_processing：任务涉及邮件撰写、回复、整理或发送。\n"
                "- calendar_processing：任务涉及日历、日程、会议邀请、ics 文件或排期。\n\n"
                "注意：可以多标签；如果任务写代码去获取网页/API数据，则 code_writing 和 web_retrieval 都应为 1。"
                "如果只是最终输出普通 .txt/.md 文本，通常 text_generation=1；"
                "如果只是代码文件注释或 JSON 配置，不必标 text_generation。\n\n"
                "输出 JSON 格式必须为：\n"
                "{\n"
                "  \"features\": "
                f"{schema_text},\n"
                "  \"rationale\": \"简短中文理由\"\n"
                "}\n\n"
                "instruction.md 内容如下：\n"
                f"{instruction.strip()}\n"
            ),
        },
    ]


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("LLM response is not a JSON object")
    return obj


def call_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    retries: int,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read().decode("utf-8")
            obj = json.loads(raw)
            return parse_json_object(obj["choices"][0]["message"]["content"])
        except Exception as exc:  # noqa: BLE001 - CLI retry diagnostics are clearer this way.
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"LLM request failed after {retries} attempts: {last_error}")


def normalize_features(obj: dict[str, Any]) -> dict[str, int]:
    raw = obj.get("features") if isinstance(obj.get("features"), dict) else obj
    normalized: dict[str, int] = {}
    for key in TYPE_KEYS:
        value = raw.get(key, 0) if isinstance(raw, dict) else 0
        if isinstance(value, str):
            value = value.strip().lower() in {"1", "true", "yes", "y", "是"}
        normalized[key] = 1 if bool(value) else 0
    return normalized


def output_feature_record(task_dir: Path, existing: dict[str, Any], type_features: dict[str, int]) -> dict[str, Any]:
    uid, session_id = task_id_parts(task_dir, existing)
    file_count, file_size_mb = workspace_seed_stats(task_dir)
    record: dict[str, Any] = {"uid": uid, "sessionId": session_id}
    for key, default in FEATURE_SCHEMA.items():
        if key in TYPE_KEYS:
            record[key] = int(type_features[key])
        elif key == "checklist_length":
            record[key] = existing.get(key, count_checks(task_dir))
        elif key == "file_count":
            record[key] = existing.get(key, file_count)
        elif key == "file_size_mb":
            record[key] = existing.get(key, file_size_mb)
        else:
            record[key] = existing.get(key, default)
    return record


def discover_task_dirs(tasks_dir: Path) -> list[Path]:
    if not tasks_dir.is_dir():
        raise SystemExit(f"tasks dir not found: {tasks_dir}")
    task_dirs = [path.parent for path in tasks_dir.rglob("instruction.md") if path.is_file()]
    return sorted(set(task_dirs), key=lambda path: str(path.relative_to(tasks_dir)))


def task_matches_selector(task_dir: Path, tasks_dir: Path, selector: str) -> bool:
    selector = selector.strip().rstrip("/")
    if not selector:
        return False
    rel_path = str(task_dir.relative_to(tasks_dir))
    return selector in {rel_path, task_dir.name, str(task_dir), str(task_dir.resolve())}


def slice_task_dirs(task_dirs: list[Path], tasks_dir: Path, start_at: str, start_after: str) -> list[Path]:
    if start_at and start_after:
        raise SystemExit("use only one of --start-at or --start-after")
    selector = start_at or start_after
    if not selector:
        return task_dirs
    for index, task_dir in enumerate(task_dirs):
        if task_matches_selector(task_dir, tasks_dir, selector):
            start_index = index if start_at else index + 1
            return task_dirs[start_index:]
    raise SystemExit(f"start task not found: {selector}")


def combo_from_record(record: dict[str, Any]) -> list[str]:
    return [key for key in TYPE_KEYS if int(record.get(key, 0) or 0) == 1]


def build_summary_rows(task_dirs: list[Path], summary_dir: Path, tasks_dir: Path, model: str, base_url: str) -> None:
    rows: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for task_dir in task_dirs:
        record = load_json(task_dir / PRIMARY_FEATURE_FILE)
        if not record:
            print(f"[skip] missing {task_dir / PRIMARY_FEATURE_FILE}", file=sys.stderr)
            continue
        combo = combo_from_record(record)
        rel_task_id = str(task_dir.relative_to(tasks_dir))
        sessions.append(
            {
                "task_id": rel_task_id,
                "uid": record.get("uid", ""),
                "sessionId": record.get("sessionId", ""),
                "task_type_combo": combo,
                "feature_file": str(task_dir / PRIMARY_FEATURE_FILE),
                "instruction_file": str(task_dir / "instruction.md"),
                "features": {key: record.get(key, 0) for key in FEATURE_SCHEMA},
            }
        )
        rows.append(
            {
                "task_id": rel_task_id,
                "uid": record.get("uid", ""),
                "sessionId": record.get("sessionId", ""),
                "task_type_combo": "+".join(combo),
                **{key: record.get(key, 0) for key in FEATURE_SCHEMA},
            }
        )

    type_counts = {key: sum(int(row.get(key, 0) or 0) for row in rows) for key in TYPE_KEYS}
    combo_counts: dict[str, int] = {}
    for row in rows:
        combo = str(row["task_type_combo"] or "none")
        combo_counts[combo] = combo_counts.get(combo, 0) + 1

    summary_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "tasks_dir": str(tasks_dir),
        "model": model,
        "base_url": base_url.rstrip("/"),
        "count": len(rows),
        "type_counts": type_counts,
        "combo_counts": dict(sorted(combo_counts.items(), key=lambda item: (-item[1], item[0]))),
        "sessions": sessions,
    }
    dump_json(summary_dir / "task_type_feature_summary.json", summary)

    csv_path = summary_dir / "task_type_feature_summary.csv"
    fieldnames = ["task_id", "uid", "sessionId", "task_type_combo", *FEATURE_SCHEMA.keys()]
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(
        json.dumps(
            {
                "count": len(rows),
                "summary_json": str(summary_dir / "task_type_feature_summary.json"),
                "summary_csv": str(csv_path),
                "type_counts": type_counts,
                "combo_counts": summary["combo_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def append_error_log(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    tasks_dir = resolve_path(args.tasks_dir)
    summary_dir = resolve_path(args.summary_dir) if args.summary_dir else tasks_dir
    error_log = resolve_path(args.error_log) if args.error_log else summary_dir / "task_type_feature_errors.jsonl"
    task_dirs = discover_task_dirs(tasks_dir)
    task_dirs = slice_task_dirs(task_dirs, tasks_dir, args.start_at, args.start_after)
    if args.limit > 0:
        task_dirs = task_dirs[: args.limit]

    if args.summary_only:
        build_summary_rows(task_dirs, summary_dir, tasks_dir, args.model, args.base_url)
        return 0

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise SystemExit(f"missing API key env var: {args.api_key_env}")
    if args.log_config:
        print(
            json.dumps(
                {
                    "task_type_labeling_config": {
                        "base_url": args.base_url.rstrip("/"),
                        "model": args.model,
                        "api_key_env": args.api_key_env,
                        "api_key_mask": mask_api_key(api_key),
                        "tasks_dir": str(tasks_dir),
                        "skip_existing": args.skip_existing,
                        "continue_on_error": args.continue_on_error,
                        "error_log": str(error_log),
                        "start_at": args.start_at,
                        "start_after": args.start_after,
                    }
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )

    failed = 0
    skipped = 0
    for index, task_dir in enumerate(task_dirs, start=1):
        rel_task_id = str(task_dir.relative_to(tasks_dir))
        feature_path = task_dir / PRIMARY_FEATURE_FILE
        if args.skip_existing and load_json(feature_path):
            skipped += 1
            print(f"[{index}/{len(task_dirs)}] skip existing {rel_task_id}", flush=True)
            continue
        try:
            instruction = (task_dir / "instruction.md").read_text(encoding="utf-8", errors="ignore")
            existing = load_json(feature_path)
            llm_obj = call_openai_compatible(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                messages=build_messages(instruction),
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                retries=args.retries,
            )
            record = output_feature_record(task_dir, existing, normalize_features(llm_obj))
            dump_json(feature_path, record)
            if not args.no_singular_alias:
                dump_json(task_dir / SINGULAR_FEATURE_FILE, record)
            combo = combo_from_record(record)
            print(f"[{index}/{len(task_dirs)}] labeled {rel_task_id}: {','.join(combo) or 'none'}", flush=True)
        except Exception as exc:  # noqa: BLE001 - batch mode should preserve the failing task id.
            failed += 1
            error_record = {
                "task_id": rel_task_id,
                "task_dir": str(task_dir),
                "error": repr(exc),
                "error_type": type(exc).__name__,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            append_error_log(error_log, error_record)
            print(f"[{index}/{len(task_dirs)}] error {rel_task_id}: {exc}", file=sys.stderr, flush=True)
            if not args.continue_on_error:
                raise
        if args.sleep > 0:
            time.sleep(args.sleep)

    build_summary_rows(task_dirs, summary_dir, tasks_dir, args.model, args.base_url)
    if failed or skipped:
        print(
            json.dumps(
                {"skipped_existing": skipped, "failed": failed, "error_log": str(error_log) if failed else ""},
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
