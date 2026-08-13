from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

class ChecksError(ValueError):
    pass

def load_checks(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read evaluator/checks.yaml") from exc
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = raw.get("checks", raw) if isinstance(raw, dict) else raw
    return [x for x in values if isinstance(x, dict)] if isinstance(values, list) else []

def summarize_checks(path: Path) -> dict[str, Any]:
    checks = load_checks(path)
    targets: list[str] = []
    mechanical: list[str] = []
    seen = set()
    for check in checks:
        text = json.dumps(check, ensure_ascii=False, sort_keys=True)
        sources = [check, check.get("args", {})]
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in ("target", "path", "file", "files"):
                value = source.get(key)
                values = value if isinstance(value, list) else [value]
                for val in values:
                    if isinstance(val, str) and val and val not in seen:
                        seen.add(val); targets.append(val)
        if re.search(r"exist|format|valid|size|dimension|shape|file|存在|格式|尺寸", text, re.I):
            mechanical.append(text[:500])
    return {"count": len(checks), "target_files": targets, "mechanical_checks": list(dict.fromkeys(mechanical))}
