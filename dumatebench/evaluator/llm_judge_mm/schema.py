from __future__ import annotations

import hashlib
import json
import math
from typing import Any

SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = {"1.0", "1.1"}
SCORE_LEVELS = (0, 1, 2, 3, 4)
DIMENSIONS = {
    "content_relevance": "内容紧扣任务目标与读者。",
    "factual_correctness_faithfulness": "事实、数字和引文有可定位依据。",
    "requirement_completeness": "覆盖任务的实质要求和交付约束。",
    "structure_coherence": "结构、顺序和叙述连贯。",
    "technical_quality": "媒体或文件的技术质量满足任务要求。",
    "audio_visual_quality": "语音、音乐、环境声、音画同步和剪辑质量可接受。",
}


class SchemaError(ValueError):
    pass


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _text(value: Any, field: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise SchemaError(f"{field} must be non-empty")
    return value


def rubric_response_format() -> dict[str, Any]:
    criterion_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "dimension": {"type": "string", "enum": list(DIMENSIONS)},
            "description": {"type": "string"},
            "weight": {"type": "number"},
            "evidence_required": {"type": "boolean"},
            "levels": {
                "type": "object",
                "properties": {str(level): {"type": "string"} for level in SCORE_LEVELS},
                "required": [str(level) for level in SCORE_LEVELS],
                "additionalProperties": False,
            },
            "modality": {"type": "string", "enum": ["text", "audio", "video", "multimodal", "structured"]},
            "covered_check_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "id",
            "dimension",
            "description",
            "weight",
            "evidence_required",
            "levels",
            "modality",
            "covered_check_ids",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "multimodal_rubric",
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


def _criterion(raw: dict[str, Any], index: int) -> dict[str, Any]:
    cid = _text(raw.get("id") or f"criterion_{index}", "criterion.id")
    dimension = _text(raw.get("dimension"), f"{cid}.dimension")
    if dimension not in DIMENSIONS:
        raise SchemaError(f"{cid}.dimension is unsupported: {dimension}")
    try:
        weight = float(raw.get("weight", 0))
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{cid}.weight must be numeric") from exc
    if weight <= 0:
        raise SchemaError(f"{cid}.weight must be positive")
    levels_raw = raw.get("levels")
    if not isinstance(levels_raw, dict):
        raise SchemaError(f"{cid}.levels must be an object")
    levels = {str(score): _text(levels_raw.get(str(score), levels_raw.get(score)), f"{cid}.levels.{score}") for score in SCORE_LEVELS}
    modality = raw.get("modality", "text")
    if modality not in {"text", "audio", "video", "multimodal", "structured"}:
        raise SchemaError(f"{cid}.modality is unsupported: {modality}")
    return {
        "id": cid, "dimension": dimension, "description": _text(raw.get("description"), f"{cid}.description"),
        "weight": weight,
        "evidence_required": bool(raw.get("evidence_required", True)), "levels": levels,
        "modality": modality,
    }


def normalize_rubric(raw: dict[str, Any], *, task_id: str, instruction_hash: str, min_criteria: int = 3) -> dict[str, Any]:
    values = raw.get("criteria")
    if not isinstance(values, list) or not values:
        raise SchemaError("rubric.criteria must be a non-empty list")
    if not min_criteria <= len(values) <= 16:
        raise SchemaError(f"rubric.criteria must contain {min_criteria} to 16 criteria")
    criteria = [_criterion(item, i) for i, item in enumerate(values, 1) if isinstance(item, dict)]
    if len(criteria) != len(values):
        raise SchemaError("every rubric criterion must be an object")
    if len({item["id"] for item in criteria}) != len(criteria):
        raise SchemaError("rubric criterion ids must be unique")
    total = sum(item["weight"] for item in criteria)
    if total <= 0:
        raise SchemaError("rubric weights must sum to a positive number")
    for item in criteria:
        item["weight"] = round(item["weight"] / total, 8)
    rubric = {
        "schema_version": SCHEMA_VERSION, "task_id": task_id,
        "instruction_hash": instruction_hash,
        "dimensions": list(dict.fromkeys(item["dimension"] for item in criteria)),
        "criteria": criteria,
    }
    rubric["rubric_hash"] = stable_hash(rubric)
    return rubric


def validate_rubric(raw: dict[str, Any], *, instruction_hash: str | None = None) -> dict[str, Any]:
    if raw.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaError(f"unsupported rubric schema_version: {raw.get('schema_version')}")
    if instruction_hash and raw.get("instruction_hash") != instruction_hash:
        raise SchemaError("rubric instruction_hash does not match instruction")
    return normalize_rubric(raw, task_id=_text(raw.get("task_id"), "rubric.task_id"), instruction_hash=_text(raw.get("instruction_hash"), "rubric.instruction_hash"), min_criteria=1)


def judgment_response_format(criteria_ids: list[str]) -> dict[str, Any]:
    criterion_id_schema: dict[str, Any] = {"type": "string"}
    if criteria_ids:
        criterion_id_schema["enum"] = criteria_ids
    evidence_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "timestamp": {"type": ["number", "null"]},
            "quote": {"type": "string"},
            "observation": {"type": "string"},
        },
        "required": ["path", "timestamp", "quote", "observation"],
        "additionalProperties": False,
    }
    criterion_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": criterion_id_schema,
            "status": {"type": "string", "enum": ["assessed", "cannot_assess"]},
            "score": {
                "anyOf": [
                    {"type": "integer", "enum": list(SCORE_LEVELS)},
                    {"type": "null"},
                ],
            },
            "evidence": {
                "type": "array",
                "items": evidence_schema,
            },
            "rationale": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["id", "status", "score", "evidence", "rationale", "confidence"],
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
            "name": "multimodal_judgment",
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


def _normalize_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    confidence = float(value)
    if not math.isfinite(confidence):
        return 0.0
    if 0.0 <= confidence <= 1.0:
        return confidence
    if 1.0 < confidence <= 100.0:
        return confidence / 100.0
    return 0.0


def normalize_judgment(raw: dict[str, Any], rubric: dict[str, Any]) -> dict[str, Any]:
    values = raw.get("criteria")
    if values is None and isinstance(raw.get("criteria_results"), list):
        values = raw.get("criteria_results")
    if not isinstance(values, list):
        raise SchemaError("judgment.criteria must be a list")
    rubric_ids = {item["id"] for item in rubric["criteria"]}
    seen: set[str] = set()
    criteria = []
    for raw_item in values:
        if not isinstance(raw_item, dict):
            raise SchemaError("every judgment criterion must be an object")
        cid = _text(raw_item.get("id", raw_item.get("criterion_id")), "judgment criterion id")
        if cid not in rubric_ids:
            raise SchemaError(f"judgment contains unknown criterion: {cid}")
        if cid in seen:
            raise SchemaError(f"judgment contains duplicate criterion: {cid}")
        seen.add(cid)
        status = raw_item.get("status")
        if status in {"pass", "fail", "partial"}:
            status = "assessed"
        if status not in {"assessed", "cannot_assess"}:
            raise SchemaError(f"{cid}.status must be assessed or cannot_assess")
        score = raw_item.get("score")
        if status == "assessed":
            if isinstance(score, bool) or not isinstance(score, int) or score not in SCORE_LEVELS:
                raise SchemaError(f"{cid}.score must be an integer from 0 to 4")
        elif score is not None:
            raise SchemaError(f"{cid}.score must be null when status is cannot_assess")
        evidence = raw_item.get("evidence", [])
        if not isinstance(evidence, list):
            raise SchemaError(f"{cid}.evidence must be a list")
        confidence = _normalize_confidence(raw_item.get("confidence", 0.0))
        criteria.append({"id": cid, "status": status, "score": score, "evidence": evidence,
                         "rationale": str(raw_item.get("rationale", "")), "confidence": confidence})
    for cid in rubric_ids - seen:
        criteria.append({"id": cid, "status": "cannot_assess", "score": None, "evidence": [],
                         "rationale": "模型未返回该 criterion。", "confidence": 0.0})
    return {"criteria": criteria, "summary": str(raw.get("summary", ""))}


def _force_cannot_assess_ids(judgment: dict[str, Any], ids: set[str], reason: str) -> dict[str, Any]:
    output = dict(judgment)
    criteria = []
    for item in judgment["criteria"]:
        item = dict(item)
        if item["id"] in ids:
            item.update({"status": "cannot_assess", "score": None, "evidence": [],
                         "rationale": reason, "confidence": 0.0})
        criteria.append(item)
    output["criteria"] = criteria
    return output


def _force_assessed_zero_ids(judgment: dict[str, Any], ids: set[str], reason: str) -> dict[str, Any]:
    output = dict(judgment)
    criteria = []
    for item in judgment["criteria"]:
        item = dict(item)
        if item["id"] in ids:
            item.update({"status": "assessed", "score": 0, "evidence": [],
                         "rationale": reason, "confidence": 1.0})
        criteria.append(item)
    output["criteria"] = criteria
    return output


def force_cannot_assess(judgment: dict[str, Any], rubric: dict[str, Any]) -> dict[str, Any]:
    del rubric
    return judgment


def force_media_unavailable(judgment: dict[str, Any], rubric: dict[str, Any], categories: set[str]) -> dict[str, Any]:
    if not categories:
        return judgment
    ids = {item["id"] for item in rubric["criteria"]
           if item["modality"] == "multimodal" or item["modality"] in categories}
    return _force_cannot_assess_ids(judgment, ids, "所需媒体附件不可传输，不能听看或可靠判定。")


def force_output_media_failure(judgment: dict[str, Any], rubric: dict[str, Any], categories: set[str], reason: str) -> dict[str, Any]:
    if not categories:
        return judgment
    ids = {item["id"] for item in rubric["criteria"]
           if item["modality"] == "multimodal" or item["modality"] in categories}
    return _force_assessed_zero_ids(
        judgment,
        ids,
        "候选输出媒体文件不可读取或不可作为有效交付评估，按交付失败计 0 分。原因：" + reason,
    )
