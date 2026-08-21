#!/usr/bin/env python3
"""Run only unified LLM-as-Judge for existing DuMateBench task outputs."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dumatebench.scripts.run_task_batch import (  # noqa: E402
    SKIPPED_JUDGE_STATUSES,
    _artifact_type,
    _infer_output_file_from_checks,
    _infer_output_files_from_checks,
    _apply_artifact_spec_overrides,
    _judge_report_file_for_output,
    _llm_judge_artifact_specs,
    _load_json_file,
    _optional_unit_score,
    _skipped_unsupported_artifact_report,
    _supports_unified_llm_judge,
    _unit_score,
    _write_aggregate_judge_report,
    dedupe_tasks_by_name,
    discover_tasks,
    read_checklist_reward,
    task_run_name,
    write_final_reward,
)
from dumatebench.evaluator.scoring import (  # noqa: E402
    equal_weight_partial_pass,
    final_score as merge_final_score,
)


@dataclass
class JudgeOnlyResult:
    task_id: str
    task_dir: str
    status: str
    elapsed_seconds: float
    output_file: str | None
    output_files: list[str]
    artifact_exists: bool
    judge_report_path: str | None
    final_reward_path: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_dir": self.task_dir,
            "status": self.status,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "output_file": self.output_file,
            "output_files": self.output_files,
            "artifact_exists": self.artifact_exists,
            "judge_report_path": self.judge_report_path,
            "final_reward_path": self.final_reward_path,
            "error": self.error,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks-dir",
        default="",
        help=(
            "Path under which source tasks are discovered recursively. Defaults to "
            "<package-root>/datasets/dev."
        ),
    )
    parser.add_argument(
        "--runs-root",
        default="",
        help="Root directory containing run outputs. Use with --run-id to judge matching task folders in that run.",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Run identifier under --runs-root. Use with --runs-root to select an existing run.",
    )
    parser.add_argument("--task-glob", default="*", help="Glob for task directory names under --tasks-dir.")
    parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--dedupe-by-name",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Collapse duplicate task leaf directory names and keep one path. Disabled by default.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of tasks to judge; 0 means all.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of tasks to judge concurrently. Defaults to 1.",
    )
    parser.add_argument("--skip-existing", action="store_true", help="Skip tasks with run_outputs/llm_judge_score.json.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing LLM judge and final reward reports.")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--summary-file", default="", help="JSONL summary path. Defaults to <tasks-dir>/llm_judge_only_summary.jsonl.")
    parser.add_argument(
        "--resume-summary",
        action="store_true",
        help=(
            "Resume/update --summary-file in place: rerun rows whose status is failed, "
            "skip rows already present with non-failed status, and run discovered tasks "
            "that are missing from the summary."
        ),
    )
    parser.add_argument(
        "--rerun-agent-not-137-summary",
        default="",
        help=(
            "Previous batch_summary.jsonl whose rows with agent_returncode != 137 should "
            "be judged again. Tasks are matched by task_dir, run_dir basename, task_id, "
            "task_name, or display_name."
        ),
    )
    parser.add_argument(
        "--rerun-failed-summary",
        default="",
        help=(
            "Previous llm_judge_only_summary.jsonl whose rows with status == failed should "
            "be judged again. Tasks are matched by task_dir, final_reward_path, task_id, "
            "task_name, or display_name."
        ),
    )
    parser.add_argument("--final-reward-file", default="run_outputs/reward_with_llm_judge.json")
    parser.add_argument("--llm-judge-output-file", default="run_outputs/llm_judge_score.json")
    parser.add_argument("--llm-judge-model", default=os.environ.get("DUMATE_LLM_JUDGE_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o")
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
    parser.add_argument("--media-mode", default=os.environ.get("DU_MATE_MEDIA_MODE", "data_url"))
    parser.add_argument("--video-mode", default=os.environ.get("DU_MATE_VIDEO_MODE", "frames"))
    parser.add_argument("--ffmpeg-path", default=os.environ.get("DU_MATE_FFMPEG_PATH", ""))
    parser.add_argument("--ffprobe-path", default=os.environ.get("DU_MATE_FFPROBE_PATH", ""))
    parser.add_argument("--dry-run", action="store_true", help="Print what would be judged without calling the LLM.")
    return parser.parse_args(argv)


def _row_agent_returncode(row: dict[str, Any]) -> int | None:
    try:
        return int(row.get("agent_returncode"))
    except (TypeError, ValueError):
        return None


def _task_display_name(task_dir: Path, tasks_dir: Path) -> str:
    try:
        return task_dir.resolve().relative_to(tasks_dir.resolve()).as_posix()
    except ValueError:
        return task_dir.name


def _load_agent_not_137_rerun_keys(summary_file: Path) -> tuple[set[Path], set[str], set[str], int, int]:
    task_paths: set[Path] = set()
    run_dir_names: set[str] = set()
    task_ids: set[str] = set()
    row_count = 0
    invalid_count = 0
    if not summary_file.is_file():
        raise SystemExit(f"rerun summary not found: {summary_file}")
    for line_number, line in enumerate(summary_file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid_count += 1
            print(f"[warn] ignoring malformed summary row {summary_file}:{line_number}", file=sys.stderr, flush=True)
            continue
        if _row_agent_returncode(row) == 137:
            continue
        row_count += 1
        task_dir = row.get("task_dir")
        if isinstance(task_dir, str) and task_dir:
            task_paths.add(Path(task_dir).expanduser().resolve())
        run_dir = row.get("run_dir")
        if isinstance(run_dir, str) and run_dir:
            resolved_run_dir = Path(run_dir).expanduser().resolve()
            task_paths.add(resolved_run_dir)
            task_paths.add(resolved_run_dir / "task_view")
            run_dir_names.add(Path(run_dir.rstrip("/")).name)
        final_reward_path = row.get("final_reward_path")
        if isinstance(final_reward_path, str) and final_reward_path:
            resolved_final = Path(final_reward_path).expanduser().resolve()
            task_paths.add(resolved_final.parents[1] if len(resolved_final.parents) > 1 else resolved_final.parent)
        for key in ("task_id", "task_name", "display_name"):
            value = row.get(key)
            if isinstance(value, str) and value:
                task_ids.add(value)
    return task_paths, run_dir_names, task_ids, row_count, invalid_count


def _add_rerun_row_keys(
    row: dict[str, Any],
    *,
    task_paths: set[Path],
    run_dir_names: set[str],
    task_ids: set[str],
) -> None:
    task_dir = row.get("task_dir")
    if isinstance(task_dir, str) and task_dir:
        resolved_task_dir = Path(task_dir).expanduser().resolve()
        task_paths.add(resolved_task_dir)
        if resolved_task_dir.name == "task_view":
            task_paths.add(resolved_task_dir.parent)
            run_dir_names.add(resolved_task_dir.parent.name)
    run_dir = row.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        resolved_run_dir = Path(run_dir).expanduser().resolve()
        task_paths.add(resolved_run_dir)
        task_paths.add(resolved_run_dir / "task_view")
        run_dir_names.add(Path(run_dir.rstrip("/")).name)
    final_reward_path = row.get("final_reward_path")
    if isinstance(final_reward_path, str) and final_reward_path:
        resolved_final = Path(final_reward_path).expanduser().resolve()
        task_paths.add(resolved_final.parents[1] if len(resolved_final.parents) > 1 else resolved_final.parent)
    for key in ("task_id", "task_name", "display_name"):
        value = row.get(key)
        if isinstance(value, str) and value:
            task_ids.add(value)


def _summary_row_match_keys(row: dict[str, Any]) -> set[str]:
    task_paths: set[Path] = set()
    run_dir_names: set[str] = set()
    task_ids: set[str] = set()
    _add_rerun_row_keys(row, task_paths=task_paths, run_dir_names=run_dir_names, task_ids=task_ids)
    keys = {f"path:{path}" for path in task_paths}
    keys.update(f"run:{name}" for name in run_dir_names)
    keys.update(f"id:{task_id}" for task_id in task_ids)
    return keys


def _task_pair_match_keys(task_dir: Path, source_task_dir: Path, tasks_dir: Path) -> set[str]:
    keys = {
        f"path:{task_dir.resolve()}",
        f"path:{source_task_dir.resolve()}",
        f"id:{task_dir.name}",
        f"id:{source_task_dir.name}",
        f"id:{_task_display_name(source_task_dir, tasks_dir)}",
        f"run:{task_dir.parent.name}" if task_dir.name == "task_view" else f"run:{task_dir.name}",
        f"run:{task_run_name(source_task_dir, tasks_dir)}",
    }
    if task_dir.name == "task_view":
        keys.add(f"path:{task_dir.parent.resolve()}")
    return keys


def _load_summary_rows(summary_path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    invalid_count = 0
    if not summary_path.is_file():
        return rows, invalid_count
    for line_number, line in enumerate(summary_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid_count += 1
            print(f"[warn] ignoring malformed summary row {summary_path}:{line_number}", file=sys.stderr, flush=True)
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            invalid_count += 1
            print(f"[warn] ignoring non-object summary row {summary_path}:{line_number}", file=sys.stderr, flush=True)
    return rows, invalid_count


def _write_summary_rows(summary_path: Path, rows: list[dict[str, Any]]) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = summary_path.with_name(f".{summary_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as summary:
        for row in rows:
            summary.write(json.dumps(row, ensure_ascii=False) + "\n")
    temp_path.replace(summary_path)


def _filter_tasks_for_agent_not_137_rerun(tasks: list[Path], summary_file: Path, tasks_dir: Path) -> list[Path]:
    task_paths, run_dir_names, task_ids, row_count, invalid_count = _load_agent_not_137_rerun_keys(summary_file)
    filtered = [
        task
        for task in tasks
        if task.resolve() in task_paths
        or task.parent.resolve() in task_paths
        or task.parent.name in run_dir_names
        or task_run_name(task, tasks_dir) in run_dir_names
        or task.name in task_ids
        or _task_display_name(task, tasks_dir) in task_ids
    ]
    print(
        "rerun_agent_not_137_summary: "
        f"{summary_file} kept {row_count} rows, ignored {invalid_count} malformed rows, "
        f"matched {len(filtered)} / {len(tasks)} discovered tasks"
    )
    return filtered


def _load_failed_rerun_keys(summary_file: Path) -> tuple[set[Path], set[str], set[str], int, int]:
    task_paths: set[Path] = set()
    run_dir_names: set[str] = set()
    task_ids: set[str] = set()
    row_count = 0
    invalid_count = 0
    if not summary_file.is_file():
        raise SystemExit(f"rerun summary not found: {summary_file}")
    for line_number, line in enumerate(summary_file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid_count += 1
            print(f"[warn] ignoring malformed summary row {summary_file}:{line_number}", file=sys.stderr, flush=True)
            continue
        if str(row.get("status", "")).lower() != "failed":
            continue
        row_count += 1
        _add_rerun_row_keys(row, task_paths=task_paths, run_dir_names=run_dir_names, task_ids=task_ids)
    return task_paths, run_dir_names, task_ids, row_count, invalid_count


def _filter_tasks_for_failed_rerun(tasks: list[Path], summary_file: Path, tasks_dir: Path) -> list[Path]:
    task_paths, run_dir_names, task_ids, row_count, invalid_count = _load_failed_rerun_keys(summary_file)
    filtered = [
        task
        for task in tasks
        if task.resolve() in task_paths
        or task.parent.resolve() in task_paths
        or task.parent.name in run_dir_names
        or task_run_name(task, tasks_dir) in run_dir_names
        or task.name in task_ids
        or _task_display_name(task, tasks_dir) in task_ids
    ]
    print(
        "rerun_failed_summary: "
        f"{summary_file} kept {row_count} failed rows, ignored {invalid_count} malformed rows, "
        f"matched {len(filtered)} / {len(tasks)} discovered tasks"
    )
    return filtered


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _ensure_task_view_run_outputs_link(task_dir: Path) -> None:
    if task_dir.name != "task_view":
        return
    run_outputs = task_dir.parent / "run_outputs"
    if not run_outputs.is_dir():
        return
    view_run_outputs = task_dir / "run_outputs"
    if _same_path(view_run_outputs, run_outputs):
        return
    if view_run_outputs.is_symlink() or view_run_outputs.is_file():
        view_run_outputs.unlink()
    elif view_run_outputs.is_dir():
        if any(view_run_outputs.iterdir()):
            raise RuntimeError(
                "task_view/run_outputs is a non-empty directory, but expected it to point to "
                f"{run_outputs}. Please move or remove the stale task_view/run_outputs first."
            )
        view_run_outputs.rmdir()
    view_run_outputs.symlink_to(run_outputs.resolve(), target_is_directory=True)


def _remove_existing_reports(task_dir: Path, final_reward_file: str, judge_output_file: str) -> None:
    paths = [
        task_dir / judge_output_file,
        task_dir / final_reward_file,
        task_dir / "run_outputs" / "ppt_llm_judge.json",
        task_dir / "run_outputs" / "excel_llm_judge" / "judge_result.json",
        task_dir / "run_outputs" / "pdf_llm_judge" / "judge_result.json",
        task_dir / "run_outputs" / "image_llm_judge" / "judge_result.json",
        task_dir / "run_outputs" / "multimodal_llm_judge" / "judge_result.json",
    ]
    for path in paths:
        if path.is_file():
            path.unlink()
    scores_dir = task_dir / "run_outputs" / "llm_judge_scores"
    if scores_dir.is_dir():
        shutil.rmtree(scores_dir)


def _recompute_equal_weight_reward(task_dir: Path, *, write: bool) -> dict[str, Any]:
    """Recompute checklist aggregates with one equal vote per check item."""
    reward = read_checklist_reward(task_dir)
    checks = reward.get("checks")
    has_item_results = isinstance(checks, list) and bool(checks) and all(
        isinstance(item, dict) and "passed" in item for item in checks
    )
    reward["partial_pass"] = equal_weight_partial_pass(
        checks,
        fallback=reward.get("partial_pass", 0.0),
    )
    if has_item_results:
        reward["complete_pass"] = int(all(bool(item["passed"]) for item in checks))
    else:
        reward["complete_pass"] = int(bool(reward.get("complete_pass", 0)))
    if write:
        reward_path = task_dir / "run_outputs" / "reward.json"
        reward_path.parent.mkdir(parents=True, exist_ok=True)
        reward_path.write_text(json.dumps(reward, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return reward


def _write_no_output_judge_report(task_dir: Path, args: argparse.Namespace, reward: dict[str, Any]) -> str:
    checklist_score = _unit_score(reward.get("partial_pass", 0.0))
    report = {
        "schema_version": "1.0",
        "status": "no_output_path_inferred",
        "reason": "Could not infer an artifact path for the LLM judge.",
        "checklist_score": checklist_score,
        "judge_score": 0.0,
        "final_score": merge_final_score(reward.get("complete_pass", 0), checklist_score, 0.0),
        "artifact_reports": [],
        "rule_result": reward,
    }
    report_path = task_dir / args.llm_judge_output_file
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(report_path)


def _refresh_existing_judge_report(task_dir: Path, args: argparse.Namespace, reward: dict[str, Any]) -> None:
    report_path = task_dir / args.llm_judge_output_file
    report = _load_json_file(report_path)
    if not isinstance(report, dict):
        return
    checklist_score = _unit_score(reward.get("partial_pass", 0.0))
    judge_score = _optional_unit_score(report.get("judge_score", report.get("final_score", 0.0)))
    status = str(report.get("status", "ok"))
    report["checklist_score"] = checklist_score
    report["rule_result"] = reward
    report["final_score"] = (
        None
        if status in {"failed", "skipped_unavailable"} or judge_score is None
        else merge_final_score(reward.get("complete_pass", 0), checklist_score, judge_score)
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_failure_report(
    task_dir: Path,
    args: argparse.Namespace,
    reward: dict[str, Any],
    output_file: str | None,
    exc: Exception,
    *,
    judge_output_file: str | None = None,
) -> str:
    report_path = task_dir / (judge_output_file or args.llm_judge_output_file)
    checklist_score = _unit_score(reward.get("partial_pass", 0.0))
    failure = {
        "schema_version": "1.0",
        "status": "failed",
        "reason": f"Unified LLM judge failed: {type(exc).__name__}: {exc}",
        "output_file": output_file,
        "checklist_score": checklist_score,
        "judge_score": 0.0,
        "final_score": merge_final_score(reward.get("complete_pass", 0), checklist_score, 0.0),
        "pass": False,
        "rule_result": reward,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(report_path)


def _run_task_llm_judge_in_dir(task_dir: Path, args: argparse.Namespace) -> JudgeOnlyResult:
    started = time.time()
    _ensure_task_view_run_outputs_link(task_dir)
    reward = _recompute_equal_weight_reward(task_dir, write=not args.dry_run)
    artifact_specs = _llm_judge_artifact_specs(task_dir, args, reward)
    output_files = [str(spec["output_file"]) for spec in artifact_specs]
    output_file = output_files[0] if output_files else _infer_output_file_from_checks(task_dir, reward)
    artifact_exists = bool(output_files and all((task_dir / item).is_file() for item in output_files))
    judge_report_path = task_dir / args.llm_judge_output_file

    if args.dry_run:
        status = "would_run" if artifact_exists else "would_write_zero_final_score"
        return JudgeOnlyResult(
            task_id=task_dir.name,
            task_dir=str(task_dir),
            status=status,
            elapsed_seconds=time.time() - started,
            output_file=output_file,
            output_files=output_files,
            artifact_exists=artifact_exists,
            judge_report_path=str(judge_report_path) if artifact_exists else None,
            final_reward_path=str(task_dir / args.final_reward_file),
        )

    if args.force:
        _remove_existing_reports(task_dir, args.final_reward_file, args.llm_judge_output_file)

    if args.skip_existing and judge_report_path.is_file():
        _refresh_existing_judge_report(task_dir, args, reward)
        final_path = write_final_reward(task_dir, args.final_reward_file)
        return JudgeOnlyResult(
            task_id=task_dir.name,
            task_dir=str(task_dir),
            status="skipped_existing",
            elapsed_seconds=time.time() - started,
            output_file=output_file,
            output_files=output_files,
            artifact_exists=artifact_exists,
            judge_report_path=str(judge_report_path),
            final_reward_path=final_path,
        )

    status = "ok"
    error = None
    if artifact_specs:
        artifact_reports: list[dict[str, Any]] = []
        from dumatebench.evaluator.llm_judge.unified import run_llm_judge_score

        for index, spec in enumerate(artifact_specs, start=1):
            artifact = str(spec["output_file"])
            artifact_path = task_dir / artifact
            artifact_type_override = spec.get("artifact_type") or spec.get("type")
            if not _supports_unified_llm_judge(artifact, str(artifact_type_override) if artifact_type_override else None):
                artifact_reports.append(_skipped_unsupported_artifact_report(artifact, artifact_path.is_file(), str(artifact_type_override) if artifact_type_override else None))
                continue

            artifact_report_file = str(spec.get("judge_output_file") or _judge_report_file_for_output(artifact, index, len(output_files)))
            if not artifact_path.is_file():
                artifact_reports.append(
                    {
                        "artifact_id": spec.get("id"),
                        "output_file": artifact,
                        "artifact_type": str(artifact_type_override) if artifact_type_override else _artifact_type(artifact),
                        "artifact_exists": False,
                        "status": "missing_artifact",
                        "reason": f"Expected artifact is missing: {artifact}",
                        "judge_score": 0.0,
                        "judge_report_file": artifact_report_file,
                    }
                )
                continue
            judge_args: dict[str, Any] = {
                "output_file": artifact,
                "rule_result": reward,
                "model": args.llm_judge_model,
                "judge_output_file": artifact_report_file,
                "reference_dir": args.reference_dir,
                "web_reference_dir": args.web_reference_dir,
                "media_mode": args.media_mode,
                "video_mode": args.video_mode,
            }
            if args.llm_judge_criteria_file:
                judge_args["criteria_file"] = args.llm_judge_criteria_file
            _apply_artifact_spec_overrides(judge_args, spec)
            if args.llm_judge_base_url:
                judge_args["base_url"] = args.llm_judge_base_url
            if args.ffmpeg_path:
                judge_args["ffmpeg_path"] = args.ffmpeg_path
            if args.ffprobe_path:
                judge_args["ffprobe_path"] = args.ffprobe_path
            try:
                report = run_llm_judge_score(task_dir, judge_args)
                if not isinstance(report, dict):
                    report = _load_json_file(task_dir / artifact_report_file) or {}
                artifact_reports.append(
                    {
                        "artifact_id": spec.get("id"),
                        "output_file": artifact,
                        "artifact_type": str(artifact_type_override) if artifact_type_override else _artifact_type(artifact),
                        "artifact_exists": True,
                        "status": str(report.get("status", "ok")),
                        "reason": str(report.get("reason", "Unified LLM judge completed.")),
                        "judge_score": _optional_unit_score(report.get("judge_score", report.get("final_score", 0.0))),
                        "judge_report_file": artifact_report_file,
                        "report": report,
                    }
                )
            except Exception as exc:  # keep batch diagnostics flowing
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
                _write_failure_report(task_dir, args, reward, artifact, exc, judge_output_file=artifact_report_file)
                artifact_reports.append(
                    {
                        "artifact_id": spec.get("id"),
                        "output_file": artifact,
                        "artifact_type": str(artifact_type_override) if artifact_type_override else _artifact_type(artifact),
                        "artifact_exists": True,
                        "status": "failed",
                        "reason": f"Unified LLM judge failed: {error}",
                        "judge_score": 0.0,
                        "judge_report_file": artifact_report_file,
                        "report": _load_json_file(task_dir / artifact_report_file) or {},
                    }
                )
        if artifact_reports:
            _write_aggregate_judge_report(task_dir, reward, artifact_reports)
        judged_reports = [item for item in artifact_reports if item.get("status") not in SKIPPED_JUDGE_STATUSES]
        if any(item["status"] == "missing_artifact" for item in judged_reports) and status == "ok":
            status = "missing_artifact"
        elif any(item["status"] == "failed" for item in judged_reports):
            status = "failed"
        elif artifact_reports and not judged_reports:
            statuses = {str(item.get("status")) for item in artifact_reports}
            status = "skipped_unavailable" if "skipped_unavailable" in statuses else "skipped_unsupported"
    elif output_file:
        status = "missing_artifact"
    else:
        status = "no_output_path_inferred"
        _write_no_output_judge_report(task_dir, args, reward)

    final_path = write_final_reward(task_dir, args.final_reward_file)
    return JudgeOnlyResult(
        task_id=task_dir.name,
        task_dir=str(task_dir),
        status=status,
        elapsed_seconds=time.time() - started,
        output_file=output_file,
        output_files=output_files,
        artifact_exists=artifact_exists,
        judge_report_path=str(judge_report_path) if judge_report_path.is_file() else None,
        final_reward_path=final_path,
        error=error,
    )


def run_task_llm_judge(
    task_dir: Path,
    args: argparse.Namespace,
    *,
    source_task_dir: Path | None = None,
) -> JudgeOnlyResult:
    """Judge a run task while sourcing task metadata from the original task directory."""
    if source_task_dir is None or source_task_dir.resolve() == task_dir.resolve():
        return _run_task_llm_judge_in_dir(task_dir, args)

    run_outputs = task_dir.parent / "run_outputs" if task_dir.name == "task_view" else task_dir / "run_outputs"
    run_outputs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".llm_judge_view-", dir=str(task_dir.parent)) as temp_dir:
        view_dir = Path(temp_dir)
        for source_item in source_task_dir.iterdir():
            if source_item.name in {"run_outputs", "run_logs"}:
                continue
            (view_dir / source_item.name).symlink_to(source_item.resolve(), target_is_directory=source_item.is_dir())
        (view_dir / "run_outputs").symlink_to(run_outputs.resolve(), target_is_directory=True)
        result = _run_task_llm_judge_in_dir(view_dir, args)
    result.task_id = task_dir.name
    result.task_dir = str(task_dir)
    if result.judge_report_path:
        result.judge_report_path = str(task_dir / args.llm_judge_output_file)
    if result.final_reward_path:
        result.final_reward_path = str(task_dir / args.final_reward_file)
    return result


def _run_task_pairs(
    task_pairs: list[tuple[Path, Path]],
    args: argparse.Namespace,
    *,
    on_result: Any,
) -> int:
    failures = 0
    if args.workers == 1:
        for index, (task_dir, source_task_dir) in enumerate(task_pairs, start=1):
            print(f"[judge] {index}/{len(task_pairs)} {task_dir.name}")
            result = run_task_llm_judge(task_dir, args, source_task_dir=source_task_dir)
            if result.status == "failed":
                failures += 1
            print(f"[{result.status}] {task_dir.name} output={result.output_file} artifact={result.artifact_exists}")
            on_result(result)
            if result.status == "failed" and args.stop_on_failure:
                break
        return failures

    task_iter = iter(enumerate(task_pairs, start=1))
    futures: dict[concurrent.futures.Future[JudgeOnlyResult], tuple[int, Path]] = {}

    def submit_next(executor: concurrent.futures.ThreadPoolExecutor) -> bool:
        try:
            index, (task_dir, source_task_dir) = next(task_iter)
        except StopIteration:
            return False
        print(f"[judge] {index}/{len(task_pairs)} {task_dir.name}")
        future = executor.submit(run_task_llm_judge, task_dir, args, source_task_dir=source_task_dir)
        futures[future] = (index, task_dir)
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        for _ in range(min(args.workers, len(task_pairs))):
            submit_next(executor)
        while futures:
            done, _ = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                _, task_dir = futures.pop(future)
                result = future.result()
                if result.status == "failed":
                    failures += 1
                print(f"[{result.status}] {task_dir.name} output={result.output_file} artifact={result.artifact_exists}")
                on_result(result)
                if not (result.status == "failed" and args.stop_on_failure):
                    submit_next(executor)
            if args.stop_on_failure and failures:
                break
    return failures


def _run_resume_summary(
    task_pairs: list[tuple[Path, Path]],
    args: argparse.Namespace,
    *,
    tasks_dir: Path,
    summary_path: Path,
) -> int:
    existing_rows, invalid_count = _load_summary_rows(summary_path)
    key_to_row: dict[str, int] = {}
    for index, row in enumerate(existing_rows):
        for key in _summary_row_match_keys(row):
            key_to_row.setdefault(key, index)

    output_rows = list(existing_rows)
    pending: list[tuple[int | None, Path, Path]] = []
    used_existing_rows: set[int] = set()
    skipped = 0
    missing = 0
    rerun_failed = 0

    for task_dir, source_task_dir in task_pairs:
        row_index = None
        for key in _task_pair_match_keys(task_dir, source_task_dir, tasks_dir):
            candidate = key_to_row.get(key)
            if candidate is not None and candidate not in used_existing_rows:
                row_index = candidate
                break
        if row_index is None:
            pending.append((None, task_dir, source_task_dir))
            missing += 1
            continue

        used_existing_rows.add(row_index)
        status = str(output_rows[row_index].get("status", "")).lower()
        if status == "failed":
            pending.append((row_index, task_dir, source_task_dir))
            rerun_failed += 1
        else:
            skipped += 1
            print(f"[skip_summary_{status or 'unknown'}] {task_dir.name}")

    print(
        "resume_summary: "
        f"{summary_path} loaded {len(existing_rows)} rows, ignored {invalid_count} malformed rows, "
        f"rerun_failed={rerun_failed}, missing={missing}, skipped={skipped}"
    )

    pending_pairs = [(task_dir, source_task_dir) for _, task_dir, source_task_dir in pending]
    pending_by_task_dir = {
        str(task_dir): row_index
        for row_index, task_dir, _ in pending
    }

    def on_result(result: JudgeOnlyResult) -> None:
        row_index = pending_by_task_dir.get(result.task_dir)
        if row_index is None:
            output_rows.append(result.as_dict())
        else:
            output_rows[row_index] = result.as_dict()
        _write_summary_rows(summary_path, output_rows)

    failures = _run_task_pairs(pending_pairs, args, on_result=on_result)
    if not pending_pairs:
        _write_summary_rows(summary_path, output_rows)
    return failures


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if bool(args.runs_root) != bool(args.run_id):
        raise SystemExit("--runs-root and --run-id must be specified together")
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    tasks_dir = Path(args.tasks_dir).expanduser().resolve() if args.tasks_dir else (ROOT / "datasets/dev").resolve()
    batch_run_dir = (Path(args.runs_root).expanduser() / args.run_id).resolve() if args.runs_root else None
    summary_root = batch_run_dir or tasks_dir
    summary_path = Path(args.summary_file).expanduser().resolve() if args.summary_file else summary_root / "llm_judge_only_summary.jsonl"
    source_tasks = discover_tasks(tasks_dir, args.task_glob, args.limit, recursive=args.recursive)
    if args.dedupe_by_name:
        source_tasks = dedupe_tasks_by_name(source_tasks)
    if args.rerun_agent_not_137_summary:
        rerun_summary = Path(args.rerun_agent_not_137_summary).expanduser().resolve()
        source_tasks = _filter_tasks_for_agent_not_137_rerun(source_tasks, rerun_summary, tasks_dir)
    if args.rerun_failed_summary:
        failed_summary = Path(args.rerun_failed_summary).expanduser().resolve()
        source_tasks = _filter_tasks_for_failed_rerun(source_tasks, failed_summary, tasks_dir)
    task_pairs = (
        [(batch_run_dir / task_run_name(task, tasks_dir) / "task_view", task) for task in source_tasks]
        if batch_run_dir is not None
        else [(task, task) for task in source_tasks]
    )

    print(f"tasks: {len(task_pairs)}")
    print(f"reference_dir: {args.reference_dir}")
    print(f"model: {args.llm_judge_model}")
    print(f"workers: {args.workers}")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    if args.resume_summary:
        failures = _run_resume_summary(task_pairs, args, tasks_dir=tasks_dir, summary_path=summary_path)
    else:
        with summary_path.open("w", encoding="utf-8") as summary:
            failures = _run_task_pairs(
                task_pairs,
                args,
                on_result=lambda result: (
                    summary.write(json.dumps(result.as_dict(), ensure_ascii=False) + "\n"),
                    summary.flush(),
                ),
            )

    print(f"summary: {summary_path}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
