"""Package a completed run into a leaderboard-submission artifact.

Two independent submission formats live here, covering two different ways a
player can produce results:

- ``pack_submission``/``validate_submission``: the full, file-based bundle
  built from a ``dumate run`` batch's own on-disk artifacts
  (``run_outputs/reward.json``, ``run_logs/agent_status.json``,
  ``run_logs/agent_adapter.jsonl``, ``run_logs/compose.log``) plus a
  ``batch_summary.<run-id>.jsonl``. This format is self-contained and carries
  its own (untrusted) reward copies as local evidence.
- ``pack_submission_from_harbor_job``/``validate_submission_manifest``: a
  small pointer manifest for players who ran their agent through the real
  ``harbor run`` CLI instead. It records only the Harbor job's id and which
  dumatebench task_ids it covers -- never a score -- because the trusted
  GitHub workflow fetches that same Harbor job independently and recomputes
  the official metrics from it.

Neither path recomputes or trusts any scoring output produced by the player;
the repository CI is the only source of truth for an accepted submission's
score.
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

from dumatebench_cli.task_metadata import TaskMetadataError, load_task_metadata
from dumatebench_cli.reward import reward_error

RUN_OUTPUT_FILES = ("reward.json",)
RUN_LOG_FILES = ("agent_status.json", "agent_adapter.jsonl", "compose.log")
MANIFEST_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}$")
HARBOR_TASK_NAME_PREFIX = "dumate/"
HARBOR_MANIFEST_SCHEMA_VERSION = 2


class SubmissionError(RuntimeError):
    """Raised when a batch's on-disk artifacts are incomplete for packaging."""


@dataclass
class PackResult:
    out_dir: Path
    task_count: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class ManifestResult:
    manifest_path: Path
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
    normalized_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            warnings.append(f"Skipping malformed summary record: {record}")
            continue
        task_dir_value = record.get("task_dir")
        if not task_dir_value:
            warnings.append(f"Skipping summary record without task_dir: {record}")
            continue
        task_dir = Path(task_dir_value).resolve()
        try:
            _task_yaml, canonical_task_id = load_task_metadata(task_dir)
        except TaskMetadataError as exc:
            warnings.append(f"Skipping {task_dir}: {exc}")
            continue
        recorded_task_id = record.get("task_id")
        if recorded_task_id != canonical_task_id:
            warnings.append(
                f"{task_dir}: replacing summary task_id {recorded_task_id!r} "
                f"with task.yaml task_id {canonical_task_id!r}"
            )
        normalized_record = dict(record)
        normalized_record["task_id"] = canonical_task_id
        normalized_record["task_dir"] = str(task_dir)
        normalized_records.append(normalized_record)
        warnings.extend(_copy_task_artifacts(task_dir, out_dir / canonical_task_id, canonical_task_id))

    (out_dir / "batch_summary.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in normalized_records),
        encoding="utf-8",
    )

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

    return PackResult(out_dir=out_dir, task_count=len(normalized_records), warnings=warnings)


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
            reward_issue = reward_error(reward_path, expected_task_id=task_id)
            if reward_issue:
                errors.append(f"{task_id}: {reward_issue}")
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
    if value.get("schema_version") != HARBOR_MANIFEST_SCHEMA_VERSION:
        errors.append(
            "Submission manifest schema_version must be "
            f"{HARBOR_MANIFEST_SCHEMA_VERSION}"
        )
    job_id = value.get("harbor_job_id")
    if not isinstance(job_id, str) or not MANIFEST_JOB_ID.fullmatch(job_id):
        errors.append("Submission manifest harbor_job_id must be a non-empty Harbor job ID or URL")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("Submission manifest metadata must be an object")
    else:
        forbidden = {"score", "accuracy", "metrics", "final_score"} & set(metadata)
        if forbidden:
            errors.append(f"Submission manifest metadata must not claim results: {sorted(forbidden)}")
        for field_name in ("agent_display_name", "agent_org_display_name"):
            if not isinstance(metadata.get(field_name), str) or not metadata[field_name].strip():
                errors.append(f"Submission manifest metadata.{field_name} must be a non-empty string")
        models = metadata.get("models")
        if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
            errors.append("Submission manifest metadata.models must contain exactly one model")
        else:
            for field_name in ("model_name", "model_provider"):
                if not isinstance(models[0].get(field_name), str) or not models[0][field_name].strip():
                    errors.append(
                        f"Submission manifest metadata.models[0].{field_name} "
                        "must be a non-empty string"
                    )
    verification = value.get("verification")
    if verification is not None and not isinstance(verification, dict):
        errors.append("Submission manifest verification must be an object")
    return errors


def _trial_dirs(job_dir: Path) -> list[Path]:
    return sorted(
        p for p in job_dir.iterdir()
        if p.is_dir() and (p / "result.json").is_file()
    )


def _dumate_task_id(task_name: Any) -> str | None:
    if not isinstance(task_name, str) or not task_name.startswith(HARBOR_TASK_NAME_PREFIX):
        return None
    return task_name[len(HARBOR_TASK_NAME_PREFIX):]


def pack_submission_from_harbor_job(
    job_dir: Path,
    out_path: Path,
    agent_name: str,
    agent_org: str,
    model_name: str,
    model_provider: str,
    agent_repo: str | None = None,
    agent_docs: str | None = None,
) -> ManifestResult:
    """Build a Harbor-job-pointer submission manifest from a real ``harbor run`` job directory.

    Unlike ``pack_submission``, this never copies or re-derives reward/score
    values from the job's trials -- it only records the job's identity
    (``harbor_job_id``) and which dumatebench task_ids it covers, exactly the
    manifest shape ``validate_submission_manifest`` checks. The trusted
    GitHub workflow is expected to fetch the same Harbor job independently
    and recompute scores from it, so this function deliberately does not
    read any ``verifier_result``/reward field out of the trials.
    """
    job_dir = job_dir.resolve()
    if not job_dir.is_dir():
        raise SubmissionError(f"Harbor job directory does not exist: {job_dir}")

    result_path = job_dir / "result.json"
    if not result_path.is_file():
        raise SubmissionError(f"Not a Harbor job directory (missing result.json): {job_dir}")
    try:
        job_result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubmissionError(f"{result_path}: not valid JSON: {exc}") from exc

    job_id = job_result.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise SubmissionError(f"{result_path}: missing job id")

    warnings: list[str] = []
    task_ids: list[str] = []
    for trial_dir in _trial_dirs(job_dir):
        try:
            trial_result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"{trial_dir.name}: result.json not valid JSON: {exc}")
            continue
        task_id = _dumate_task_id(trial_result.get("task_name"))
        if task_id is None:
            warnings.append(
                f"{trial_dir.name}: task_name {trial_result.get('task_name')!r} "
                f"is not a dumatebench task (expected {HARBOR_TASK_NAME_PREFIX!r} prefix), skipping"
            )
            continue
        if trial_result.get("exception_info") is not None:
            warnings.append(f"{task_id}: trial {trial_dir.name} raised an exception, included anyway")
        task_ids.append(task_id)

    if not task_ids:
        raise SubmissionError(f"No dumatebench trials found under Harbor job directory: {job_dir}")

    if out_path.exists():
        raise SubmissionError(f"Output file already exists, refusing to overwrite: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": HARBOR_MANIFEST_SCHEMA_VERSION,
        "harbor_job_id": job_id,
        "metadata": {
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
            "task_ids": sorted(set(task_ids)),
        },
    }
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    manifest_errors = validate_submission_manifest(out_path)
    if manifest_errors:
        raise SubmissionError(
            f"Generated manifest failed self-validation (this is a bug): {manifest_errors}"
        )

    return ManifestResult(manifest_path=out_path, task_count=len(set(task_ids)), warnings=warnings)
