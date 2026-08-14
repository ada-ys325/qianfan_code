"""Package a completed ``dumate run`` batch into a leaderboard-submission bundle.

The bundle format mirrors what a batch already produces on disk
(``run_outputs/reward.json``, ``run_logs/agent_status.json``,
``run_logs/agent_adapter.jsonl``, ``run_logs/compose.log``) plus a
``batch_summary.<run-id>.jsonl``. This module only rearranges those existing
files under a fixed directory layout and adds ``metadata.yaml``/``config.json``
— it does not recompute or touch any scoring output, so a submission is by
construction identical to what the player already ran locally.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

RUN_OUTPUT_FILES = ("reward.json",)
RUN_LOG_FILES = ("agent_status.json", "agent_adapter.jsonl", "compose.log")
MANIFEST_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}$")


class SubmissionError(RuntimeError):
    """Raised when a batch's on-disk artifacts are incomplete for packaging."""


@dataclass
class PackResult:
    out_dir: Path
    task_count: int
    warnings: list[str] = field(default_factory=list)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _copy_task_artifacts(task_dir: Path, dest: Path, task_id: str) -> list[str]:
    """Copy one task's reward/log files into dest, returning missing-file warnings."""
    warnings: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)

    for name in RUN_OUTPUT_FILES:
        src = task_dir / "run_outputs" / name
        if src.exists():
            shutil.copy2(src, dest / name)
        else:
            warnings.append(f"{task_id}: missing run_outputs/{name}")

    for name in RUN_LOG_FILES:
        src = task_dir / "run_logs" / name
        if src.exists():
            shutil.copy2(src, dest / name)
        else:
            warnings.append(f"{task_id}: missing run_logs/{name}")

    return warnings


def pack_submission(
    summary_path: Path,
    out_dir: Path,
    agent_name: str,
    agent_org: str,
    model_name: str,
    model_provider: str,
    agent_repo: str | None = None,
    agent_docs: str | None = None,
    dumate_run_args: dict[str, Any] | None = None,
) -> PackResult:
    """Collect one batch's reward/log artifacts into a submission bundle at out_dir.

    ``summary_path`` is a ``batch_summary.<run-id>.jsonl`` produced by ``dumate run``
    (or a single task's equivalent single-line summary). Raises SubmissionError if
    the summary itself can't be read or is empty — missing per-task artifacts are
    reported as warnings on the returned PackResult rather than aborting, since a
    partially-successful batch (e.g. one task errored) is still packable and the
    repository CI performs the final intake check.
    """
    summary_path = summary_path.resolve()
    if not summary_path.exists():
        raise SubmissionError(f"Summary file does not exist: {summary_path}")

    records = _read_jsonl(summary_path)
    if not records:
        raise SubmissionError(f"Summary file is empty: {summary_path}")

    if out_dir.exists():
        raise SubmissionError(f"Output directory already exists, refusing to overwrite: {out_dir}")
    out_dir.mkdir(parents=True)

    warnings: list[str] = []
    for record in records:
        task_id = record.get("task_id")
        task_dir = record.get("task_dir")
        if not task_id or not task_dir:
            warnings.append(f"Skipping malformed summary record: {record}")
            continue
        warnings.extend(_copy_task_artifacts(Path(task_dir), out_dir / task_id, task_id))

    shutil.copy2(summary_path, out_dir / "batch_summary.jsonl")

    metadata = {
        "agent_display_name": agent_name,
        "agent_org_display_name": agent_org,
        "agent_repo": agent_repo,
        "agent_docs": agent_docs,
        "models": [
            {
                "model_name": model_name,
                "model_provider": model_provider,
            }
        ],
        "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / "metadata.yaml").write_text(
        yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    )

    config = dumate_run_args or {}
    (out_dir / "config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False))

    return PackResult(out_dir=out_dir, task_count=len(records), warnings=warnings)


def validate_submission(bundle_dir: Path) -> list[str]:
    """Validate the untrusted, file-based submission intake format.

    This check proves that a submission is complete and internally consistent.
    It deliberately does not trust the copied reward values as an official
    score. The trusted GitHub workflow fetches the original Harbor trials and
    recomputes the official DuMateBench metrics from that external source.
    """
    errors: list[str] = []
    bundle_dir = bundle_dir.resolve()

    if not bundle_dir.is_dir():
        return [f"Submission directory does not exist: {bundle_dir}"]

    task_id_pattern = re.compile(r"^[A-Za-z0-9._-]+$")

    for path in bundle_dir.rglob("*"):
        if path.is_symlink():
            errors.append(f"Symlinks are not allowed: {path.relative_to(bundle_dir)}")

    allowed_root_files = {"metadata.yaml", "batch_summary.jsonl", "config.json"}
    for path in bundle_dir.iterdir():
        if path.is_file() and path.name not in allowed_root_files:
            errors.append(f"Unexpected file at submission root: {path.name}")

    metadata_path = bundle_dir / "metadata.yaml"
    if not metadata_path.exists():
        errors.append("Missing metadata.yaml")
    else:
        try:
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            metadata = {}
            errors.append(f"metadata.yaml is not valid YAML: {exc}")
        if not isinstance(metadata, dict):
            metadata = {}
            errors.append("metadata.yaml must contain a mapping")
        for field_name in ("agent_display_name", "agent_org_display_name"):
            if not metadata.get(field_name):
                errors.append(f"metadata.yaml missing required field: {field_name}")
        models = metadata.get("models") or []
        if not isinstance(models, list) or not models or not isinstance(models[0], dict):
            errors.append("metadata.yaml missing required field: models[0].model_name")
        elif not models[0].get("model_name") or not models[0].get("model_provider"):
            errors.append("metadata.yaml missing required field: models[0].model_name")

    summary_path = bundle_dir / "batch_summary.jsonl"
    if not summary_path.exists():
        errors.append("Missing batch_summary.jsonl")
        return errors

    try:
        records = _read_jsonl(summary_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [*errors, f"batch_summary.jsonl is not valid JSONL: {exc}"]
    if not records:
        errors.append("batch_summary.jsonl is empty")
        return errors

    task_ids: list[str] = []
    for line_number, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            errors.append(f"batch_summary.jsonl line {line_number}: record must be an object")
            continue
        task_id = record.get("task_id")
        if not isinstance(task_id, str) or not task_id_pattern.fullmatch(task_id):
            errors.append(f"batch_summary.jsonl line {line_number}: invalid task_id")
            continue
        task_ids.append(task_id)
        if record.get("status") not in {"completed", "ok"}:
            errors.append(f"{task_id}: run status must be completed, got {record.get('status')!r}")
        if record.get("evaluator_returncode") not in {None, 0}:
            errors.append(f"{task_id}: evaluator_returncode must be 0")

    if len(task_ids) != len(set(task_ids)):
        errors.append("batch_summary.jsonl contains duplicate task_id values")

    task_id_set = set(task_ids)
    task_dirs = {
        p.name for p in bundle_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    }
    missing_dirs = task_id_set - task_dirs
    if missing_dirs:
        errors.append(
            "batch_summary.jsonl references tasks with no submission directory: "
            f"{sorted(missing_dirs)}"
        )
    extra_dirs = task_dirs - task_id_set
    if extra_dirs:
        errors.append(f"Submission contains task directories not listed in summary: {sorted(extra_dirs)}")

    for task_id in task_id_set & task_dirs:
        task_dir = bundle_dir / task_id
        reward_path = task_dir / "reward.json"
        if not reward_path.exists():
            errors.append(f"{task_id}: missing reward.json")
        else:
            try:
                reward = json.loads(reward_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{task_id}: reward.json is not valid JSON: {exc}")
            else:
                if not isinstance(reward, dict):
                    errors.append(f"{task_id}: reward.json must contain an object")
                elif reward.get("task_id") != task_id:
                    errors.append(f"{task_id}: reward.json task_id does not match directory")
                else:
                    for score_field in ("complete_pass", "partial_pass"):
                        value = reward.get(score_field)
                        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                            errors.append(f"{task_id}: reward.json has invalid {score_field}")
        adapter_log = task_dir / "agent_adapter.jsonl"
        if not adapter_log.exists():
            errors.append(f"{task_id}: missing agent_adapter.jsonl (trajectory evidence)")
        else:
            try:
                if not adapter_log.read_text(encoding="utf-8").strip():
                    errors.append(f"{task_id}: agent_adapter.jsonl is empty")
            except OSError as exc:
                errors.append(f"{task_id}: cannot read agent_adapter.jsonl: {exc}")

    config_path = bundle_dir / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"config.json is not valid JSON: {exc}")
        else:
            if not isinstance(config, dict):
                errors.append("config.json must contain an object")
            else:
                forbidden = {"score", "accuracy", "metrics"} & set(config)
                if forbidden:
                    errors.append(
                        "config.json must not contain claimed results: "
                        f"{sorted(forbidden)}"
                    )
                for field_name in ("max_steps", "concurrency"):
                    if field_name in config and (
                        not isinstance(config[field_name], int) or config[field_name] <= 0
                    ):
                        errors.append(f"config.json {field_name} must be a positive integer")

    return errors


def validate_submission_manifest(manifest_path: Path) -> list[str]:
    """Validate the small, Harbor-backed manifest used by formal PR intake.

    The manifest identifies the external run whose trials CI will fetch. It
    contains no score field because scores must be recomputed from Harbor.
    """
    errors: list[str] = []
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        return [f"Submission manifest does not exist: {manifest_path}"]
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"Submission manifest is not valid JSON: {exc}"]
    if not isinstance(value, dict):
        return ["Submission manifest must contain an object"]

    allowed = {"schema_version", "harbor_job_id", "metadata", "verification"}
    forbidden = {"score", "accuracy", "metrics", "final_score"} & set(value)
    if forbidden:
        errors.append(f"Submission manifest must not claim results: {sorted(forbidden)}")
    unknown = set(value) - allowed
    if unknown:
        errors.append(f"Submission manifest has unknown fields: {sorted(unknown)}")
    if value.get("schema_version") != 1:
        errors.append("Submission manifest schema_version must be 1")
    job_id = value.get("harbor_job_id")
    if not isinstance(job_id, str) or not MANIFEST_JOB_ID.fullmatch(job_id):
        errors.append("Submission manifest harbor_job_id must be a non-empty Harbor job ID or URL")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        errors.append("Submission manifest metadata must be an object")
    else:
        forbidden = {"score", "accuracy", "metrics", "final_score"} & set(metadata)
        if forbidden:
            errors.append(f"Submission manifest metadata must not claim results: {sorted(forbidden)}")
    verification = value.get("verification")
    if verification is not None and not isinstance(verification, dict):
        errors.append("Submission manifest verification must be an object")
    return errors
