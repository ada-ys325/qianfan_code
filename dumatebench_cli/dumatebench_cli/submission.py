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
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

RUN_OUTPUT_FILES = ("reward.json",)
RUN_LOG_FILES = ("agent_status.json", "agent_adapter.jsonl", "compose.log")


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
    real gatekeeping happens in the leaderboard repo's validate_submission.py.
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
    """Run the same basic completeness checks the leaderboard repo's bot will run.

    Returns a list of error strings; empty list means the bundle is well-formed
    enough to submit (does not verify trajectory authenticity — that is the
    human-review step described in the plan, not something checkable offline).
    """
    errors: list[str] = []
    bundle_dir = bundle_dir.resolve()

    metadata_path = bundle_dir / "metadata.yaml"
    if not metadata_path.exists():
        errors.append("Missing metadata.yaml")
    else:
        metadata = yaml.safe_load(metadata_path.read_text()) or {}
        for field_name in ("agent_display_name", "agent_org_display_name"):
            if not metadata.get(field_name):
                errors.append(f"metadata.yaml missing required field: {field_name}")
        models = metadata.get("models") or []
        if not models or not models[0].get("model_name"):
            errors.append("metadata.yaml missing required field: models[0].model_name")

    summary_path = bundle_dir / "batch_summary.jsonl"
    if not summary_path.exists():
        errors.append("Missing batch_summary.jsonl")
        return errors

    records = _read_jsonl(summary_path)
    task_ids = {r.get("task_id") for r in records if r.get("task_id")}
    task_dirs = {p.name for p in bundle_dir.iterdir() if p.is_dir()}
    missing_dirs = task_ids - task_dirs
    if missing_dirs:
        errors.append(f"batch_summary.jsonl references tasks with no submission directory: {sorted(missing_dirs)}")

    for task_id in task_ids & task_dirs:
        task_dir = bundle_dir / task_id
        if not (task_dir / "reward.json").exists():
            errors.append(f"{task_id}: missing reward.json")
        adapter_log = task_dir / "agent_adapter.jsonl"
        if not adapter_log.exists():
            errors.append(f"{task_id}: missing agent_adapter.jsonl (trajectory evidence)")
        elif not adapter_log.read_text().strip():
            errors.append(f"{task_id}: agent_adapter.jsonl is empty")

    return errors
