from __future__ import annotations

import hashlib
import json
from typing import Any


SCHEMA_VERSION = "1.0"
SCORE_LEVELS = (0, 1, 2, 3, 4)

CODE_DIMENSIONS: dict[str, str] = {
    "functional_correctness": "代码是否实现任务要求、核心行为和预期输出。",
    "bug_risk_defect": "代码是否存在明显缺陷、运行时错误、逻辑漏洞、状态不一致、异常路径失败或隐藏 bug 风险。",
    "reference_fidelity": "代码是否忠实使用给定 reference、接口说明、数据 schema 或正确答案。",
    "repo_integration": "代码是否正确集成到现有项目结构、API、依赖和调用链中。",
    "regression_safety": "是否避免破坏已有功能、公开接口、文件格式、兼容行为。",
    "edge_case_robustness": "是否处理边界条件、异常输入、空值、错误状态和 contracts。",
    "algorithmic_efficiency": "时间/空间复杂度、批量数据规模和实现效率是否合理。",
    "maintainability_readability": "代码是否清晰、局部、可维护，符合项目风格，避免过度复杂。",
    "security_safety": "是否避免注入、路径穿越、敏感信息泄露、危险执行和不安全依赖。",
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
    if dimension not in CODE_DIMENSIONS:
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
    return normalize_rubric(raw, task_id=task_id, instruction_hash=expected_instruction_hash)

