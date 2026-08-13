from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_checks(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read evaluator/checks.yaml") from exc
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    checks = value.get("checks", []) if isinstance(value, dict) else value
    return [item for item in checks if isinstance(item, dict)]


def _targets(value: Any) -> list[str]:
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            if key in {"file", "path", "target", "keyword", "keywords"}:
                result.extend(_targets(child))
            else:
                result.extend(_targets(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_targets(child))
        return result
    return [str(value)] if value is not None else []


def summarize_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for check in checks:
        items.append({
            "id": str(check.get("id", "")),
            "description": str(check.get("description", "")),
            "type": str(check.get("type", "")),
            "targets": _targets(check.get("args", {})),
        })
    return {"count": len(items), "checks": items}


def checks_prompt_text(checks: list[dict[str, Any]]) -> str:
    return json.dumps(summarize_checks(checks), ensure_ascii=False, indent=2)


def _norm(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())


def _mechanical_kind(check: dict[str, Any]) -> str | None:
    typ = check.get("type", "").lower()
    if "exist" in typ:
        return "exist"
    if "format" in typ or "valid" in typ:
        return "format"
    if "contain" in typ or "keyword" in typ:
        return "keyword"
    return None


def filter_duplicate_criteria(criteria: list[dict[str, Any]], checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove only criteria clearly covered by this session's mechanical checks."""
    mechanical = [(check, _mechanical_kind(check)) for check in checks]
    output = []
    for criterion in criteria:
        description = _norm(str(criterion.get("description", "")))
        explicit = set(criterion.get("covered_check_ids", []))
        duplicate = bool(explicit and any(str(check.get("id")) in explicit for check, _ in mechanical))
        for check, kind in mechanical:
            if not kind:
                continue
            if kind == "exist" and any(token in description for token in ("文件存在", "必须存在", "交付文件", "是否存在")):
                duplicate = True
            elif kind == "format" and any(token in description for token in ("格式", "可读取", "有效文件")):
                duplicate = True
        if not duplicate:
            output.append({key: value for key, value in criterion.items() if key != "covered_check_ids"})
    return output
