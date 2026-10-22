"""Shared reward.json contract used by runners and submission validation."""

from __future__ import annotations

import json
from pathlib import Path


def reward_error(path: Path, expected_task_id: str | None = None) -> str | None:
    """Return a human-readable error when ``path`` violates the reward contract.

    Evaluator exit codes are intentionally not checked here. A return code of
    ``1`` is the normal signal for a completed evaluation whose task did not
    fully pass; the reward fields carry the actual score and completion state.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "reward.json is missing or invalid JSON"
    if not isinstance(value, dict):
        return "reward.json must contain an object"
    if expected_task_id is not None and value.get("task_id") != expected_task_id:
        return (
            f"reward.json task_id {value.get('task_id')!r} does not match "
            f"expected task_id {expected_task_id!r}"
        )
    for key in ("complete_pass", "partial_pass"):
        score = value.get(key)
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= float(score) <= 1:
            return f"reward.json has invalid {key}"
    return None
