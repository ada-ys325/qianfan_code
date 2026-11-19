#!/usr/bin/env python3
"""Run DuMateBench's unified judge from a Harbor verifier container."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dumatebench.evaluator.llm_judge.unified import run_llm_judge_score
from dumatebench.evaluator.scoring import equal_weight_partial_pass, final_score


def _outputs(task_dir: Path, reward: dict) -> list[str]:
    candidates: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value not in candidates:
            candidates.append(value)

    checks = reward.get("checks", [])
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        detail = check.get("detail", "")
        if isinstance(detail, str):
            try:
                value = json.loads(detail)
            except json.JSONDecodeError:
                value = {}
            if isinstance(value, dict):
                add(value.get("file"))
                add(value.get("path"))
                args = value.get("args")
                if isinstance(args, dict):
                    add(args.get("file"))
                    add(args.get("output_file"))
    checks_file = task_dir / "evaluator" / "checks.yaml"
    if checks_file.is_file():
        try:
            import yaml

            checks_data = yaml.safe_load(checks_file.read_text(encoding="utf-8")) or {}
            yaml_checks = checks_data.get("checks", []) if isinstance(checks_data, dict) else []
            for check in yaml_checks if isinstance(yaml_checks, list) else []:
                if not isinstance(check, dict):
                    continue
                add(check.get("file"))
                add(check.get("path"))
                args = check.get("args")
                if isinstance(args, dict):
                    add(args.get("file"))
                    add(args.get("output_file"))
        except Exception:
            # Keep simple YAML path forms usable even in minimal custom task images.
            for match in re.finditer(
                r"(?m)^\s*(?:file|path)\s*:\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s#]+))",
                checks_file.read_text(errors="ignore"),
            ):
                add(next(group for group in match.groups() if group is not None))
    result: list[str] = []
    for value in candidates:
        if value.startswith("run_outputs/") and value not in result and (task_dir / value).is_file():
            result.append(value)
    return result


def _checklist_score(reward: dict) -> float:
    return equal_weight_partial_pass(
        reward.get("checks"),
        fallback=reward.get("partial_pass", 0.0),
    )


def _artifact_specs(task_dir: Path, reward: dict) -> list[dict]:
    """Use the task's declared artifacts and fixed rubrics when available."""
    manifest = task_dir / "evaluator" / "llm_judge_artifacts.json"
    if not manifest.is_file():
        return [{"output_file": path} for path in _outputs(task_dir, reward)]
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        items = next((raw[key] for key in ("artifacts", "outputs", "output_files") if key in raw), None)
        defaults = {key: value for key, value in raw.items() if key not in {"artifacts", "outputs", "output_files"}}
    else:
        items, defaults = raw, {}
    if not isinstance(items, list):
        raise ValueError(f"Invalid LLM judge artifacts manifest: {manifest}")
    specs: list[dict] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            spec = {**defaults, "output_file": item}
        elif isinstance(item, dict):
            spec = {**defaults, **item}
            spec["output_file"] = item.get("output_file") or item.get("path") or item.get("file")
        else:
            raise ValueError(f"Invalid artifact #{index} in {manifest}")
        path = spec.get("output_file")
        if not isinstance(path, str) or not path.startswith("run_outputs/") or ".." in Path(path).parts:
            raise ValueError(f"Invalid output_file for artifact #{index} in {manifest}: {path!r}")
        specs.append(spec)
    return specs


def _fixed_rubric_args(task_dir: Path, spec: dict) -> dict:
    """Use an available fixed rubric, or let the judge generate one."""
    extra = spec.get("judge_args") or {}
    if not isinstance(extra, dict):
        raise ValueError("judge_args must be an object")
    selected = {key: value for key, value in spec.items() if key in {
        "artifact_type", "type", "doc_type", "criteria_file", "rubric_file",
        "criteria", "task_rubrics", "rubric", "reference_file", "references_file",
        "reference_manifest", "references_manifest", "reference_dir", "web_reference_dir",
    }}
    selected.update(extra)
    if selected.get("type") and not selected.get("artifact_type"):
        selected["artifact_type"] = selected["type"]
    if any(isinstance(selected.get(key), (dict, list)) for key in ("criteria", "task_rubrics", "rubric")):
        return selected
    available_fixed_file = False
    for key in (
        "criteria_file", "rubric_file", "llm_judge_criteria_file",
        "llm_judge_rubric_file", "rubric",
    ):
        value = selected.get(key)
        if value:
            rubric_path = Path(value)
            if not rubric_path.is_absolute():
                rubric_path = task_dir / rubric_path
            if rubric_path.is_file():
                available_fixed_file = True
            else:
                selected.pop(key)
    if available_fixed_file:
        return selected
    for relative in ("evaluator/llm_judge_criteria.json", "evaluator/llm_judge_rubric.json"):
        if (task_dir / relative).is_file():
            selected["criteria_file"] = relative
            return selected
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--model", default=os.environ.get("DUMATE_LLM_JUDGE_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    args = parser.parse_args()
    task_dir = args.task_dir.resolve()
    reward_path = task_dir / "run_outputs" / "reward.json"
    reward = json.loads(reward_path.read_text(encoding="utf-8"))
    checklist_score = _checklist_score(reward)
    scores: list[float] = []
    reports: list[dict] = []
    for index, spec in enumerate(_artifact_specs(task_dir, reward), start=1):
        output_file = spec["output_file"]
        judge_options = _fixed_rubric_args(task_dir, spec)
        if not any(judge_options.get(key) for key in (
            "criteria", "task_rubrics", "rubric", "criteria_file", "rubric_file",
            "llm_judge_criteria_file", "llm_judge_rubric_file",
        )):
            print(f"No fixed LLM judge rubric for {output_file}; generating one with the judge model.", file=sys.stderr)
        report = run_llm_judge_score(task_dir, {
            "output_file": output_file,
            "rule_result": reward,
            "model": args.model,
            "base_url": args.base_url,
            "judge_output_file": f"run_outputs/llm_judge_scores/{index}.json",
            "reference_dir": "/workspace_seed",
            "web_reference_dir": "/opt/dumate/task/web_reference",
            **judge_options,
        })
        reports.append(report)
        if report.get("status") == "ok" and report.get("judge_score") is not None:
            scores.append(float(report["judge_score"]))
    if not scores:
        return 0
    judge = round(sum(scores) / len(scores), 4)
    merged = dict(reward)
    merged.update({
        "base_partial_pass": checklist_score,
        "checklist_score": checklist_score,
        "llm_judge_score": judge,
        "final_score": final_score(checklist_score, judge),
        "llm_judge": {"status": "ok", "reports": reports},
    })
    (task_dir / "run_outputs" / "reward_with_llm_judge.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
