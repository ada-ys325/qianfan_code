#!/usr/bin/env python3
"""Rerun LLM judge for prior failed-zero and code-artifact task outputs."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent

FAILED_ZERO_MARKERS = (
    "llm judge failed",
    "llm judge json request failed",
    "llm request failed",
    "unified llm judge failed",
    "textual llm judge failed",
    "pdf llm judge failed",
    "image llm judge failed",
    "multimodal llm judge failed",
    "excel llm judge failed",
    "provider",
    "timeout",
    "timed out",
    "bad gateway",
    "gateway",
    "service unavailable",
    "internal server error",
    "empty content",
    "invalid json",
    "jsondecodeerror",
    "expecting value",
)

CODE_SUFFIXES = {
    ".py",
    ".ipynb",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".bat",
    ".ps1",
    ".sql",
    ".r",
    ".jl",
    ".lua",
    ".pl",
    ".pm",
    ".dart",
    ".vue",
    ".svelte",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-root",
        required=True,
        help="Root containing already-run task directories, e.g. /data/sjh/final_dataset/runs/<run-id>.",
    )
    parser.add_argument("--task-glob", default="*", help="Glob for task directories under --runs-root.")
    parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--limit", type=int, default=0, help="Maximum selected tasks to rerun; 0 means all.")
    parser.add_argument("--max-attempts", type=int, default=5, help="Maximum judge-only attempts per selected task.")
    parser.add_argument("--sleep-seconds", type=float, default=2.0, help="Delay between attempts for the same task.")
    parser.add_argument("--evaluator-python", default=os.environ.get("DUMATE_EVALUATOR_PYTHON", sys.executable))
    parser.add_argument("--llm-judge-model", default=os.environ.get("DUMATE_LLM_JUDGE_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o")
    parser.add_argument("--llm-judge-base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    parser.add_argument("--reference-dir", default="workspace_seed")
    parser.add_argument("--web-reference-dir", default="web_reference")
    parser.add_argument("--final-reward-file", default="run_outputs/reward_with_llm_judge.json")
    parser.add_argument("--llm-judge-output-file", default="run_outputs/llm_judge_score.json")
    parser.add_argument("--summary-file", default="", help="JSONL summary path. Defaults to <runs-root>/rerun_failed_llm_judge_only_summary.jsonl.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def unit_score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score > 1.0:
        score /= 100.0
    return max(0.0, min(1.0, score))


def discover_task_dirs(runs_root: Path, task_glob: str, recursive: bool) -> list[Path]:
    pattern = f"**/{task_glob}" if recursive else task_glob
    tasks: list[Path] = []
    for path in sorted(runs_root.glob(pattern)):
        if not path.is_dir():
            continue
        run_outputs = path / "run_outputs"
        if run_outputs.is_dir() and (
            (run_outputs / "llm_judge_score.json").is_file()
            or (run_outputs / "reward_with_llm_judge.json").is_file()
            or (run_outputs / "reward.json").is_file()
        ):
            tasks.append(path)
    return tasks


def strings_from(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        items: list[str] = []
        for child in value.values():
            items.extend(strings_from(child))
        return items
    if isinstance(value, list):
        items = []
        for child in value:
            items.extend(strings_from(child))
        return items
    return []


def output_files_from_reports(task_dir: Path) -> list[str]:
    paths: list[str] = []
    judge = read_json(task_dir / "run_outputs" / "llm_judge_score.json") or {}
    final = read_json(task_dir / "run_outputs" / "reward_with_llm_judge.json") or {}
    for source in (judge, final.get("llm_judge") if isinstance(final.get("llm_judge"), dict) else {}):
        if not isinstance(source, dict):
            continue
        output = source.get("output_file")
        if isinstance(output, str):
            paths.append(output)
        outputs = source.get("output_files")
        if isinstance(outputs, list):
            paths.extend(item for item in outputs if isinstance(item, str))
        reports = source.get("artifact_reports")
        if isinstance(reports, list):
            for item in reports:
                if isinstance(item, dict) and isinstance(item.get("output_file"), str):
                    paths.append(item["output_file"])
    ordered: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path and path not in seen:
            ordered.append(path)
            seen.add(path)
    return ordered


def is_code_artifact_task(task_dir: Path) -> bool:
    return any(Path(path).suffix.lower() in CODE_SUFFIXES for path in output_files_from_reports(task_dir))


def failed_zero_due_to_judge_error(task_dir: Path) -> bool:
    judge = read_json(task_dir / "run_outputs" / "llm_judge_score.json") or {}
    final = read_json(task_dir / "run_outputs" / "reward_with_llm_judge.json") or {}
    scores = [
        unit_score(judge.get("judge_score")),
        unit_score(judge.get("final_score")) if judge.get("judge_score") is None else None,
        unit_score(final.get("llm_judge_score")),
    ]
    has_zero = any(score == 0.0 for score in scores if score is not None)
    if not has_zero:
        return False
    status_text = " ".join(
        strings_from(
            {
                "judge_status": judge.get("status"),
                "judge_reason": judge.get("reason"),
                "judge_error": judge.get("error"),
                "judge_report": judge.get("judge_report"),
                "final_llm_judge": final.get("llm_judge"),
            }
        )
    ).lower()
    return any(marker in status_text for marker in FAILED_ZERO_MARKERS)


def needs_rerun(task_dir: Path) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if failed_zero_due_to_judge_error(task_dir):
        reasons.append("failed_zero")
    if is_code_artifact_task(task_dir):
        reasons.append("code_artifact")
    return bool(reasons), reasons


def build_judge_only_command(task_dir: Path, args: argparse.Namespace) -> list[str]:
    cmd = [
        *shlex.split(args.evaluator_python),
        str(ROOT / "scripts" / "run_llm_judge_only.py"),
        "--tasks-dir",
        str(task_dir.parent),
        "--task-glob",
        task_dir.name,
        "--no-recursive",
        "--no-dedupe-by-name",
        "--force",
        "--llm-judge-model",
        args.llm_judge_model,
        "--reference-dir",
        args.reference_dir,
        "--web-reference-dir",
        args.web_reference_dir,
        "--final-reward-file",
        args.final_reward_file,
        "--llm-judge-output-file",
        args.llm_judge_output_file,
    ]
    if args.llm_judge_base_url:
        cmd.extend(["--llm-judge-base-url", args.llm_judge_base_url])
    return cmd


def rerun_task(task_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    selected, reasons = needs_rerun(task_dir)
    result: dict[str, Any] = {
        "task_dir": str(task_dir),
        "selected": selected,
        "selection_reasons": reasons,
        "attempts": [],
    }
    if not selected:
        result["status"] = "not_selected"
        return result
    if args.dry_run:
        result["status"] = "would_rerun"
        result["command"] = build_judge_only_command(task_dir, args)
        return result

    for attempt in range(1, max(1, args.max_attempts) + 1):
        cmd = build_judge_only_command(task_dir, args)
        started = time.time()
        completed = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        still_failed_zero = failed_zero_due_to_judge_error(task_dir)
        post_reasons = []
        if completed.returncode != 0:
            post_reasons.append("judge_only_command_failed")
        if still_failed_zero:
            post_reasons.append("failed_zero")
        still_failed = bool(post_reasons)
        attempt_record = {
            "attempt": attempt,
            "returncode": completed.returncode,
            "elapsed_seconds": round(time.time() - started, 3),
            "still_needs_rerun": still_failed,
            "post_reasons": post_reasons,
            "output_tail": completed.stdout[-4000:],
        }
        result["attempts"].append(attempt_record)
        if not still_failed:
            result["status"] = "ok"
            return result
        if attempt < args.max_attempts:
            time.sleep(max(0.0, args.sleep_seconds))
    result["status"] = "still_failed"
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runs_root = Path(args.runs_root).expanduser().resolve()
    summary_path = (
        Path(args.summary_file).expanduser().resolve()
        if args.summary_file
        else runs_root / "rerun_failed_llm_judge_only_summary.jsonl"
    )
    tasks = discover_task_dirs(runs_root, args.task_glob, args.recursive)
    selected = [task for task in tasks if needs_rerun(task)[0]]
    if args.limit > 0:
        selected = selected[: args.limit]

    print(f"discovered_tasks: {len(tasks)}")
    print(f"selected_tasks: {len(selected)}")
    print(f"summary: {summary_path}")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with summary_path.open("w", encoding="utf-8") as summary:
        for index, task_dir in enumerate(selected, start=1):
            _, reasons = needs_rerun(task_dir)
            print(f"[rerun] {index}/{len(selected)} {task_dir.name} reasons={','.join(reasons)}", flush=True)
            result = rerun_task(task_dir, args)
            if result.get("status") == "still_failed":
                failures += 1
            print(f"[{result.get('status')}] {task_dir.name}", flush=True)
            summary.write(json.dumps(result, ensure_ascii=False) + "\n")
            summary.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
