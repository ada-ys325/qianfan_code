#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
from typing import Any


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except Exception:
            pass
    if value in {"true", "false"}:
        return value == "true"
    if value == "null":
        return None
    if value.startswith(('"', "'")):
        try:
            return json.loads(value)
        except Exception:
            return value.strip('"').strip("'")
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def load_checks(path: Path) -> list[dict[str, Any]]:
    checks = []
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped == "checks:":
            continue
        if stripped.startswith("- id:"):
            if current:
                checks.append(current)
            current = {"id": stripped.split(":", 1)[1].strip()}
        elif current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key.strip()] = parse_scalar(value)
    if current:
        checks.append(current)
    return checks


def load_evaluate_module(task_dir: Path):
    candidates = []
    if os.environ.get("DUMATE_EVALUATE_PY"):
        candidates.append(Path(os.environ["DUMATE_EVALUATE_PY"]))
    for parent in [task_dir, *task_dir.parents]:
        candidates.append(parent / "evaluate.py")
        candidates.append(parent / "data_annotation" / "evaluate.py")
    candidates.append(Path.cwd() / "data_annotation" / "evaluate.py")
    candidates.append(Path.cwd() / "evaluate.py")

    module_path = None
    for candidate in candidates:
        if candidate.is_file():
            module_path = candidate.resolve()
            break
    if module_path is None:
        raise RuntimeError("cannot find shared evaluate.py; set DUMATE_EVALUATE_PY")

    spec = importlib.util.spec_from_file_location("dumatebench_evaluate", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load evaluate module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluate_check(task_dir: Path, check: dict[str, Any]) -> tuple[bool, str]:
    typ = str(check.get("type", ""))
    args = check.get("args") or {}
    module = load_evaluate_module(task_dir)
    func = getattr(module, typ, None)
    if func is None:
        return False, f"unknown evaluate function: {typ}"
    try:
        return bool(func(task_dir, args)), json.dumps(args, ensure_ascii=False)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def evaluate(task_dir: Path) -> dict[str, Any]:
    checks = load_checks(task_dir / "evaluator" / "checks.yaml")
    total = 0.0
    earned = 0.0
    results = []
    for check in checks:
        weight = float(check.get("weight", 0))
        total += weight
        passed, detail = evaluate_check(task_dir, check)
        if passed:
            earned += weight
        results.append({
            "id": check.get("id"),
            "type": check.get("type"),
            "weight": weight,
            "passed": passed,
            "detail": detail,
            "description": check.get("description", ""),
        })
    return {
        "task_id": "91e93b82116f4c4ebe6643fb4482737e_ses_10c5617b4ffef1kW1BoO5UZckY",
        "complete_pass": 1 if results and all(r["passed"] for r in results) else 0,
        "partial_pass": round(earned / total, 4) if total else 0.0,
        "checks": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", default=".")
    args = parser.parse_args()
    task_dir = Path(args.task_dir).resolve()
    result = evaluate(task_dir)
    out = task_dir / "run_outputs" / "reward.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["complete_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
