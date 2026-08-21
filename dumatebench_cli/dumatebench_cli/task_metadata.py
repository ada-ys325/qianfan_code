"""Shared task metadata and source-checkout contract helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class TaskMetadataError(ValueError):
    """Raised when a task's task.yaml cannot provide a safe task identity."""


def load_task_yaml(task_dir: Path) -> dict[str, Any]:
    """Read task.yaml and require a top-level mapping."""
    path = task_dir / "task.yaml"
    if not path.is_file():
        raise TaskMetadataError(f"{task_dir}: missing task.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TaskMetadataError(f"{task_dir}: task.yaml is invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise TaskMetadataError(f"{task_dir}: task.yaml must contain a mapping")
    return data


def task_id_from_yaml(data: dict[str, Any], task_dir: Path) -> str:
    """Return the canonical, path-safe task ID from task.yaml."""
    value = data.get("task_id")
    if not isinstance(value, str) or not value or not TASK_ID_PATTERN.fullmatch(value):
        raise TaskMetadataError(
            f"{task_dir}: task.yaml.task_id must be a non-empty string matching "
            r"^[A-Za-z0-9._-]+$"
        )
    return value


def load_task_metadata(task_dir: Path) -> tuple[dict[str, Any], str]:
    data = load_task_yaml(task_dir)
    return data, task_id_from_yaml(data, task_dir)


def shared_evaluate_path() -> Path:
    """Return the shared evaluator path expected in a source checkout."""
    return Path(__file__).resolve().parents[2] / "dumatebench" / "evaluator" / "evaluate.py"
