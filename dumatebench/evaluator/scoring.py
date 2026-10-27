"""Shared score calculations for checklist and LLM-judge evaluation."""

from __future__ import annotations

from typing import Any


def equal_weight_partial_pass(
    checks: Any,
    *,
    fallback: Any = 0.0,
    precision: int = 4,
) -> float:
    """Return the unweighted fraction of passed checklist items.

    Legacy reports may not include per-item ``passed`` values. In that case the
    supplied aggregate is retained so old reports remain readable.
    """
    if isinstance(checks, list) and checks and all(
        isinstance(item, dict) and "passed" in item for item in checks
    ):
        passed = sum(bool(item["passed"]) for item in checks)
        return round(passed / len(checks), precision)
    try:
        value = float(fallback)
    except (TypeError, ValueError):
        value = 0.0
    return round(max(0.0, min(1.0, value)), precision)


def final_score(
    checklist_score: Any,
    llm_judge_score: Any,
    *,
    precision: int = 4,
) -> float:
    """Combine the checklist score (30%) and LLM-judge score (70%)."""

    def unit(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 0.0
        if score > 1.0:
            score /= 100.0
        return max(0.0, min(1.0, score))

    result = 0.3 * unit(checklist_score) + 0.7 * unit(llm_judge_score)
    return round(result, precision)
