from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "1.0"
SCORE_LEVELS = (0, 1, 2, 3, 4)

GENERAL_DIMENSIONS: dict[str, str] = {
    "instruction_following": "遵循任务中的显式要求、输出约束和目标读者要求。",
    "content_correctness_faithfulness": "事实、数字、引文和结论正确，并忠实于输入或参考材料。",
    "requirement_completeness": "覆盖任务要求的全部内容、页面、字段和交付物。",
    "document_structure": "页面顺序、章节层级、阅读顺序和信息组织完整连贯。",
    "layout_readability": "文字、表格和图形清晰可读，无裁切、重叠、溢出或异常留白。",
    "visual_quality_consistency": "字体、色彩、间距、对齐和视觉层级一致且符合使用场景。",
    "edit_fidelity": "编辑类任务只改变指定内容，并保留源文件中未要求修改的信息与样式。",
    "artifact_integrity": "PDF 可打开、页数和文件结构合理，指定输出存在且无异常附加产物。",
}

DIMENSION_ALIASES = {
    "instruction following": "instruction_following",
    "instruction-following": "instruction_following",
    "following instructions": "instruction_following",
    "content correctness": "content_correctness_faithfulness",
    "content correctness faithfulness": "content_correctness_faithfulness",
    "factual correctness": "content_correctness_faithfulness",
    "faithfulness": "content_correctness_faithfulness",
    "completeness": "requirement_completeness",
    "requirements completeness": "requirement_completeness",
    "document structure": "document_structure",
    "structure": "document_structure",
    "layout readability": "layout_readability",
    "readability": "layout_readability",
    "visual quality": "visual_quality_consistency",
    "visual consistency": "visual_quality_consistency",
    "visual quality consistency": "visual_quality_consistency",
    "edit fidelity": "edit_fidelity",
    "artifact integrity": "artifact_integrity",
}


class SchemaError(ValueError):
    pass


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise SchemaError(f"{field} must be non-empty")
    return result


def normalize_dimension(value: Any) -> str:
    raw = _text(value, "criterion.dimension")
    if raw in GENERAL_DIMENSIONS:
        return raw
    compact = re.sub(r"[\s_-]+", " ", raw).strip().lower()
    alias = DIMENSION_ALIASES.get(compact)
    if alias:
        return alias
    slug = compact.replace(" ", "_")
    if slug in GENERAL_DIMENSIONS:
        return slug
    raise SchemaError(f"unsupported dimension: {raw}")


def rubric_response_format() -> dict[str, Any]:
    dimension_values = list(GENERAL_DIMENSIONS)
    criterion_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "dimension": {"type": "string", "enum": dimension_values},
            "description": {"type": "string"},
            "weight": {"type": "number"},
            "evidence_required": {"type": "boolean"},
            "levels": {
                "type": "object",
                "properties": {str(level): {"type": "string"} for level in SCORE_LEVELS},
                "required": [str(level) for level in SCORE_LEVELS],
                "additionalProperties": False,
            },
        },
        "required": [
            "id",
            "dimension",
            "description",
            "weight",
            "evidence_required",
            "levels",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "pdf_rubric",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "minItems": 3,
                        "maxItems": 16,
                        "items": criterion_schema,
                    }
                },
                "required": ["criteria"],
                "additionalProperties": False,
            },
        },
    }


def judgment_response_format(criteria_ids: list[str], artifact_paths: list[str]) -> dict[str, Any]:
    evidence_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "enum": artifact_paths} if artifact_paths else {"type": "string"},
            "page": {"type": ["integer", "null"]},
            "quote": {"type": "string"},
            "visual_observation": {"type": "string"},
        },
        "required": ["path", "page", "quote", "visual_observation"],
        "additionalProperties": False,
    }
    criterion_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": criteria_ids} if criteria_ids else {"type": "string"},
            "score": {"type": "integer", "enum": list(SCORE_LEVELS)},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "evidence": {
                "type": "array",
                "items": evidence_schema,
            },
            "rationale": {"type": "string"},
        },
        "required": ["id", "score", "confidence", "evidence", "rationale"],
        "additionalProperties": False,
    }
    criteria_schema: dict[str, Any] = {
        "type": "array",
        "minItems": len(criteria_ids) if criteria_ids else 1,
        "maxItems": len(criteria_ids) if criteria_ids else 16,
        "items": criterion_schema,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "pdf_judgment",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "criteria": criteria_schema,
                    "summary": {"type": "string"},
                },
                "required": ["criteria", "summary"],
                "additionalProperties": False,
            },
        },
    }


def normalize_rubric(raw: dict[str, Any], *, task_id: str, instruction_hash: str) -> dict[str, Any]:
    criteria_raw = raw.get("criteria")
    if not isinstance(criteria_raw, list) or not 3 <= len(criteria_raw) <= 16:
        raise SchemaError("rubric.criteria must contain 3 to 16 criteria")

    criteria: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(criteria_raw, start=1):
        if not isinstance(item, dict):
            raise SchemaError(f"criterion {index} must be an object")
        criterion_id = _text(item.get("id") or f"criterion_{index}", "criterion.id")
        if criterion_id in ids:
            raise SchemaError(f"duplicate criterion id: {criterion_id}")
        ids.add(criterion_id)
        dimension = normalize_dimension(item.get("dimension"))
        try:
            weight = float(item.get("weight"))
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"{criterion_id}.weight must be numeric") from exc
        if weight <= 0:
            raise SchemaError(f"{criterion_id}.weight must be positive")
        levels_raw = item.get("levels")
        if not isinstance(levels_raw, dict):
            raise SchemaError(f"{criterion_id}.levels must be an object")
        levels = {str(level): _text(levels_raw.get(str(level)), f"{criterion_id}.levels.{level}") for level in SCORE_LEVELS}
        criteria.append({
            "id": criterion_id,
            "dimension": dimension,
            "description": _text(item.get("description"), f"{criterion_id}.description"),
            "weight": weight,
            "evidence_required": bool(item.get("evidence_required", True)),
            "levels": levels,
        })

    total_weight = sum(item["weight"] for item in criteria)
    for item in criteria:
        item["weight"] = round(item["weight"] / total_weight, 8)
    rounding_delta = round(1.0 - sum(item["weight"] for item in criteria), 8)
    criteria[-1]["weight"] = round(criteria[-1]["weight"] + rounding_delta, 8)

    rubric = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "instruction_hash": instruction_hash,
        "criteria": criteria,
    }
    rubric["rubric_hash"] = stable_hash(rubric)
    return rubric


def validate_rubric(rubric: dict[str, Any], *, instruction_hash: str | None = None) -> None:
    if rubric.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError("unsupported rubric schema_version")
    if instruction_hash is not None and rubric.get("instruction_hash") != instruction_hash:
        raise SchemaError("rubric instruction hash does not match the task instruction")
    expected_hash = rubric.get("rubric_hash")
    unsigned = {key: value for key, value in rubric.items() if key != "rubric_hash"}
    if expected_hash != stable_hash(unsigned):
        raise SchemaError("rubric hash is invalid")
    criteria = rubric.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise SchemaError("rubric.criteria must be non-empty")
    if abs(sum(float(item["weight"]) for item in criteria) - 1.0) > 1e-6:
        raise SchemaError("rubric weights must sum to 1")


@dataclass(frozen=True)
class GateDecision:
    status: str
    hard_failed: bool
    cap: float
    rule_score: float | None
    failed_check_ids: tuple[str, ...]
    reason: str


def apply_rule_gate(rule_result: dict[str, Any] | None, *, artifact_errors: list[str] | None = None) -> GateDecision:
    if artifact_errors:
        return GateDecision("artifact_invalid", True, 0.0, None, (), "; ".join(artifact_errors))
    if not rule_result:
        return GateDecision("not_provided", False, 100.0, None, (), "No deterministic evaluator result was provided.")
    checks = rule_result.get("checks")
    if not isinstance(checks, list):
        raise SchemaError("rule result checks must be a list")
    failed = [item for item in checks if isinstance(item, dict) and not bool(item.get("passed"))]
    failed_ids = tuple(str(item.get("id", "unknown")) for item in failed)
    hard_types = {"evaluate_file_exist", "evaluate_pdf", "evaluate_pdf_valid", "evaluate_output_file_citation_final_OutputFileCitationGrader"}
    hard_failed = any(str(item.get("type", "")) in hard_types for item in failed)
    score_value = rule_result.get("score", rule_result.get("reward"))
    try:
        rule_score = float(score_value) * (100.0 if float(score_value) <= 1.0 else 1.0)
    except (TypeError, ValueError):
        rule_score = None
    cap = 0.0 if hard_failed else (80.0 if failed else 100.0)
    return GateDecision(
        "hard_failed" if hard_failed else ("partial_failure" if failed else "passed"),
        hard_failed,
        cap,
        rule_score,
        failed_ids,
        "Deterministic checks failed." if failed else "Deterministic checks passed.",
    )
