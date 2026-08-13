from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA_VERSION = "1.0"
DIMENSIONS = {
    "instruction_content_fidelity": "指令与内容忠实度",
    "semantic_factual_correctness": "语义与事实正确性",
    "composition_visual_hierarchy": "构图与视觉层级",
    "text_readability": "文字可读性",
    "style_aesthetic_consistency": "风格与美学一致性",
    "reference_fidelity": "参考图忠实度",
    "technical_completeness": "技术完整性",
}
DIMENSION_ALIASES = {
    value: key for key, value in DIMENSIONS.items()
}
DIMENSION_ALIASES.update({
    "instruction content fidelity": "instruction_content_fidelity",
    "semantic factual correctness": "semantic_factual_correctness",
    "composition visual hierarchy": "composition_visual_hierarchy",
    "text readability": "text_readability",
    "style aesthetic consistency": "style_aesthetic_consistency",
    "reference fidelity": "reference_fidelity",
    "technical completeness": "technical_completeness",
})
SCORE_LEVELS = (0, 1, 2, 3, 4)

class SchemaError(ValueError):
    pass

def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{name} must be a non-empty string")
    return value.strip()


def normalize_dimension(value: Any) -> str:
    dimension = _text(value, "criterion.dimension")
    if dimension in DIMENSIONS:
        return dimension
    compact = " ".join(dimension.replace("-", " ").replace("_", " ").split()).lower()
    alias = DIMENSION_ALIASES.get(dimension) or DIMENSION_ALIASES.get(compact)
    if alias:
        return alias
    slug = compact.replace(" ", "_")
    if slug in DIMENSIONS:
        return slug
    raise SchemaError(f"unsupported dimension: {dimension}")


def rubric_response_format() -> dict[str, Any]:
    criterion_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "dimension": {"type": "string", "enum": list(DIMENSIONS)},
            "description": {"type": "string"},
            "evidence_hint": {"type": "string"},
            "weight": {"type": "number"},
            "evidence_required": {"type": "boolean"},
            "levels": {
                "type": "object",
                "properties": {str(level): {"type": "string"} for level in SCORE_LEVELS},
                "required": [str(level) for level in SCORE_LEVELS],
                "additionalProperties": False,
            },
        },
        "required": ["id", "dimension", "description", "evidence_hint", "weight", "evidence_required", "levels"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "image_rubric",
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

def normalize_rubric(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SchemaError("rubric must be an object")
    criteria = raw.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise SchemaError("rubric.criteria must be a non-empty array")
    normalized = []
    total = 0.0
    seen = set()
    for i, item in enumerate(criteria):
        if not isinstance(item, dict):
            raise SchemaError(f"criteria[{i}] must be an object")
        cid = _text(item.get("id", f"criterion_{i+1}"), f"criteria[{i}].id")
        if cid in seen:
            raise SchemaError(f"duplicate criterion id: {cid}")
        seen.add(cid)
        dimension = normalize_dimension(item.get("dimension"))
        try:
            weight = float(item.get("weight"))
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"{cid}.weight must be numeric") from exc
        if weight <= 0:
            raise SchemaError(f"{cid}.weight must be positive")
        levels = item.get("levels")
        if not isinstance(levels, dict) or set(levels) != {"0", "1", "2", "3", "4"}:
            raise SchemaError(f"{cid}.levels must contain string keys 0..4")
        normalized.append({
            "id": cid,
            "dimension": dimension,
            "description": _text(item.get("description"), f"{cid}.description"),
            "evidence_hint": _text(item.get("evidence_hint"), f"{cid}.evidence_hint"),
            "weight": weight,
            "evidence_required": bool(item.get("evidence_required", True)),
            "levels": {str(k): _text(v, f"{cid}.levels.{k}") for k, v in levels.items()},
        })
        total += weight
    total = sum(item["weight"] for item in normalized)
    if total <= 0:
        raise SchemaError("rubric weights must sum to a positive value")
    for item in normalized:
        item["weight"] = round(item["weight"] / total, 8)
    correction = round(1.0 - sum(x["weight"] for x in normalized), 8)
    normalized[-1]["weight"] = round(normalized[-1]["weight"] + correction, 8)
    return {"schema_version": SCHEMA_VERSION, "criteria": normalized}

def validate_rubric(rubric: Any) -> dict[str, Any]:
    normalized = normalize_rubric(rubric)
    if abs(sum(x["weight"] for x in normalized["criteria"]) - 1.0) > 1e-6:
        raise SchemaError("rubric weights must sum to 1")
    return normalized


def judge_response_format(criteria_ids: list[str] | None = None) -> dict[str, Any]:
    criterion_id_schema: dict[str, Any] = {"type": "string"}
    if criteria_ids:
        criterion_id_schema["enum"] = criteria_ids
    criterion_result_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": criterion_id_schema,
            "status": {"type": "string", "enum": ["pass", "fail", "cannot_assess"]},
            "score": {
                "anyOf": [
                    {"type": "integer", "enum": list(SCORE_LEVELS)},
                    {"type": "null"},
                ],
            },
            "evidence": {"type": "string"},
            "rationale": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["id", "status", "score", "evidence", "rationale", "confidence"],
        "additionalProperties": False,
    }
    criteria_results_schema: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 16,
        "items": criterion_result_schema,
    }
    if criteria_ids:
        criteria_results_schema["minItems"] = len(criteria_ids)
        criteria_results_schema["maxItems"] = len(criteria_ids)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "image_judgment",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "criteria_results": criteria_results_schema,
                    "gate": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "enum": ["ok", "blocked"]},
                            "reasons": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["status", "reasons"],
                        "additionalProperties": False,
                    },
                    "summary": {"type": "string"},
                },
                "required": ["criteria_results", "gate", "summary"],
                "additionalProperties": False,
            },
        },
    }


def validate_judge_result(raw: Any, rubric: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SchemaError("judge result must be an object")
    items = raw.get("criteria_results")
    if not isinstance(items, list):
        raise SchemaError("criteria_results must be an array")
    expected = {x["id"] for x in rubric["criteria"]}
    actual = {x.get("id", x.get("criterion_id")) for x in items if isinstance(x, dict)}
    if actual != expected or len(items) != len(expected):
        raise SchemaError("criteria_results must contain each rubric criterion exactly once")
    total = 0.0
    result = []
    for item in items:
        cid = item.get("id", item.get("criterion_id"))
        status = item.get("status")
        if status not in {"pass", "fail", "cannot_assess"}:
            raise SchemaError(f"{cid}.status is invalid")
        score = item.get("score")
        if status == "cannot_assess":
            if score is not None:
                raise SchemaError(f"{cid}.score must be null for cannot_assess")
        elif not isinstance(score, int) or score not in range(5):
            raise SchemaError(f"{cid}.score must be an integer from 0 to 4")
        evidence = item.get("evidence")
        if not isinstance(evidence, str):
            raise SchemaError(f"{cid}.evidence must be a string")
        rationale = item.get("rationale", "")
        if not isinstance(rationale, str):
            raise SchemaError(f"{cid}.rationale must be a string")
        confidence_raw = item.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"{cid}.confidence must be a number from 0 to 1") from exc
        confidence = max(0.0, min(1.0, confidence))
        weight = next(x["weight"] for x in rubric["criteria"] if x["id"] == cid)
        if score is not None:
            total += weight * score
        result.append({"id": cid, "criterion_id": cid, "status": status, "score": score, "evidence": evidence, "rationale": rationale, "confidence": confidence})
    gate = raw.get("gate") or {"status": "ok", "reasons": []}
    if not isinstance(gate, dict) or gate.get("status") not in {"ok", "blocked"} or not isinstance(gate.get("reasons", []), list):
        raise SchemaError("gate must contain status ok/blocked and reasons array")
    return {
        "schema_version": SCHEMA_VERSION,
        "criteria_results": result,
        "weighted_score": round(total, 4),
        "gate": gate,
        "summary": str(raw.get("summary", "")),
    }
