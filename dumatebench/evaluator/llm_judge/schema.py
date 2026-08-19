from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "1.0"
SCORE_LEVELS = (0, 1, 2, 3, 4)

GENERAL_DIMENSIONS: dict[str, str] = {
    "content_relevance": "内容紧扣任务目标与目标读者，不以无关篇幅掩盖缺失。",
    "factual_correctness_faithfulness": "事实、数字、引文和推断有依据；忠实于输入材料并明确不确定性。",
    "requirement_completeness": "覆盖任务中所有实质要求、必需字段和交付约束。",
    "structure_coherence": "结构完整，层级与顺序合理，论证或叙述连贯。",
    "language_style": "表达清楚、准确、自然，语气和文体适合使用场景。",
    "presentation_readability": "Word/Markdown/文本的标题、段落、列表、表格和格式具有一致性与可读性。",
    "edit_fidelity": "编辑任务只改应改内容，保留未授权内容、结构和样式。",
}


class SchemaError(ValueError):
    pass


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SchemaError(f"{field} must be non-empty")
    return text


def _criterion(raw: dict[str, Any], index: int) -> dict[str, Any]:
    criterion_id = _clean_text(raw.get("id") or f"criterion_{index}", "criterion.id")
    dimension = _clean_text(raw.get("dimension"), f"{criterion_id}.dimension")
    if dimension not in GENERAL_DIMENSIONS:
        raise SchemaError(f"{criterion_id}.dimension is unsupported: {dimension}")

    try:
        weight = float(raw.get("weight", 0))
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{criterion_id}.weight must be numeric") from exc
    if weight <= 0:
        raise SchemaError(f"{criterion_id}.weight must be positive")

    levels_raw = raw.get("levels")
    if not isinstance(levels_raw, dict):
        raise SchemaError(f"{criterion_id}.levels must be an object")
    levels: dict[str, str] = {}
    for score in SCORE_LEVELS:
        value = levels_raw.get(str(score), levels_raw.get(score))
        levels[str(score)] = _clean_text(value, f"{criterion_id}.levels.{score}")

    return {
        "id": criterion_id,
        "dimension": dimension,
        "description": _clean_text(raw.get("description"), f"{criterion_id}.description"),
        "weight": weight,
        "evidence_required": bool(raw.get("evidence_required", True)),
        "levels": levels,
    }


def normalize_rubric(raw: dict[str, Any], *, task_id: str, instruction_hash: str) -> dict[str, Any]:
    criteria_raw = raw.get("criteria")
    if not isinstance(criteria_raw, list) or not criteria_raw:
        raise SchemaError("rubric.criteria must be a non-empty list")
    if not 3 <= len(criteria_raw) <= 16:
        raise SchemaError("rubric.criteria must contain 3 to 16 atomic criteria")

    criteria = [_criterion(item, i) for i, item in enumerate(criteria_raw, start=1) if isinstance(item, dict)]
    if len(criteria) != len(criteria_raw):
        raise SchemaError("every rubric criterion must be an object")
    ids = [item["id"] for item in criteria]
    if len(ids) != len(set(ids)):
        raise SchemaError("rubric criterion ids must be unique")

    total = sum(item["weight"] for item in criteria)
    for item in criteria:
        item["weight"] = round(item["weight"] / total, 8)
    drift = 1.0 - sum(item["weight"] for item in criteria)
    criteria[-1]["weight"] = round(criteria[-1]["weight"] + drift, 8)

    dimensions = []
    for item in criteria:
        if item["dimension"] not in dimensions:
            dimensions.append(item["dimension"])
    rubric = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "instruction_hash": instruction_hash,
        "dimensions": dimensions,
        "criteria": criteria,
    }
    rubric["rubric_hash"] = stable_hash(rubric)
    return rubric


def validate_rubric(raw: dict[str, Any], *, instruction_hash: str | None = None) -> dict[str, Any]:
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError(f"unsupported rubric schema_version: {raw.get('schema_version')}")
    task_id = _clean_text(raw.get("task_id"), "rubric.task_id")
    expected_instruction_hash = instruction_hash or _clean_text(raw.get("instruction_hash"), "rubric.instruction_hash")
    if instruction_hash and raw.get("instruction_hash") != instruction_hash:
        raise SchemaError("rubric instruction_hash does not match the current instruction")
    normalized = normalize_rubric(raw, task_id=task_id, instruction_hash=expected_instruction_hash)
    return normalized


@dataclass(frozen=True)
class GateDecision:
    status: str
    hard_failed: bool
    cap: float
    rule_score: float | None
    failed_check_ids: tuple[str, ...]
    reason: str


def apply_rule_gate(rule_result: dict[str, Any] | None) -> GateDecision:
    if not rule_result:
        return GateDecision("not_provided", False, 100.0, None, (), "未提供规则评估结果。")

    checks = rule_result.get("checks")
    if not isinstance(checks, list):
        raise SchemaError("rule result checks must be a list")
    invalid = [
        item for item in checks
        if isinstance(item, dict)
        and (item.get("score_eligible") is False or str(item.get("status", "")) in {
            "unsupported", "evaluator_error", "runner_error", "reference_error"
        })
    ]
    if invalid:
        ids = tuple(str(item.get("id", "unknown")) for item in invalid)
        return GateDecision(
            "evaluator_error",
            True,
            0.0,
            None,
            ids,
            "Checklist contains evaluator errors or unsupported checks.",
        )
    failed = [item for item in checks if isinstance(item, dict) and not bool(item.get("passed"))]
    failed_ids = tuple(str(item.get("id", "unknown")) for item in failed)
    hard_types = {"evaluate_file_exist", "evaluate_file_format_valid"}
    hard_failed = any(str(item.get("type")) in hard_types for item in failed)
    try:
        raw_partial = rule_result.get("partial_pass", 0.0)
        if raw_partial is None:
            return GateDecision("evaluator_error", True, 0.0, None, failed_ids, "Checklist has no eligible score.")
        rule_score = float(raw_partial) * 100.0
    except (TypeError, ValueError) as exc:
        raise SchemaError("rule result partial_pass must be numeric") from exc
    rule_score = max(0.0, min(100.0, rule_score))
    if hard_failed:
        return GateDecision("hard_fail", True, 0.0, rule_score, failed_ids, "交付文件缺失或格式无效。")
    if failed:
        return GateDecision("objective_fail", False, 79.0, rule_score, failed_ids, "存在未通过的客观内容或结构检查。")
    return GateDecision("pass", False, 100.0, rule_score, (), "所有规则检查均通过。")
