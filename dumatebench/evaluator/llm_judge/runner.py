from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Protocol

from .artifacts import artifact_inventory, collect_artifacts
from .prompts import judge_messages, rubric_messages
from .schema import GateDecision, SchemaError, apply_rule_gate, normalize_rubric, stable_hash, validate_rubric


class JsonClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SchemaError(f"expected JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _evidence(raw: Any, sources: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        artifact_path = str(item.get("artifact_path", "")).strip()
        quote = str(item.get("quote", "")).strip()[:500]
        result.append({
            "artifact_path": artifact_path,
            "location": str(item.get("location", "")).strip(),
            "quote": quote,
            "verified": bool(artifact_path in sources and quote and quote in sources[artifact_path]),
        })
    return result


def normalize_judgment(
    raw: dict[str, Any],
    rubric: dict[str, Any],
    evidence_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    items_raw = raw.get("criteria")
    if not isinstance(items_raw, list):
        raise SchemaError("judgment.criteria must be a list")
    by_id = {
        str(item.get("id")): item
        for item in items_raw
        if isinstance(item, dict) and item.get("id") is not None
    }
    unknown = set(by_id) - {item["id"] for item in rubric["criteria"]}
    if unknown:
        raise SchemaError(f"judgment contains unknown criterion ids: {sorted(unknown)}")

    results = []
    for criterion in rubric["criteria"]:
        item = by_id.get(criterion["id"], {})
        status = str(item.get("status", "cannot_assess"))
        if status not in {"pass", "partial", "fail", "cannot_assess"}:
            status = "cannot_assess"
        score_raw = item.get("score")
        if status == "cannot_assess" or score_raw is None:
            score = None
            status = "cannot_assess"
        else:
            try:
                score = int(score_raw)
            except (TypeError, ValueError) as exc:
                raise SchemaError(f"invalid score for {criterion['id']}") from exc
            if score not in {0, 1, 2, 3, 4}:
                raise SchemaError(f"score for {criterion['id']} must be 0..4")
            status = "pass" if score == 4 else "partial" if score in {2, 3} else "fail"
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        evidence = _evidence(item.get("evidence"), evidence_sources or {})
        results.append({
            "id": criterion["id"],
            "status": status,
            "score": score,
            "evidence": evidence,
            "rationale": str(item.get("rationale", "")).strip(),
            "confidence": max(0.0, min(1.0, confidence)),
        })
    return {"criteria": results, "summary": str(raw.get("summary", "")).strip()}


def aggregate_judgments(runs: list[dict[str, Any]], rubric: dict[str, Any]) -> dict[str, Any]:
    if not runs:
        raise SchemaError("at least one judgment run is required")
    aggregated = []
    assessed_weight = 0.0
    earned_weight = 0.0
    for criterion in rubric["criteria"]:
        results = [run["criteria"][[x["id"] for x in run["criteria"]].index(criterion["id"])] for run in runs]
        assessed = [item for item in results if item["score"] is not None]
        if assessed:
            score = float(statistics.median(item["score"] for item in assessed))
            chosen = min(assessed, key=lambda item: abs(float(item["score"]) - score))
            status = chosen["status"]
            confidence = sum(item["confidence"] for item in assessed) / len(assessed)
            assessed_weight += criterion["weight"]
            earned_weight += criterion["weight"] * (score / 4.0)
        else:
            score = None
            status = "cannot_assess"
            confidence = 0.0
            chosen = results[0]
        aggregated.append({
            **criterion,
            "status": status,
            "score": score,
            "confidence": round(confidence, 4),
            "evidence": chosen["evidence"],
            "rationale": chosen["rationale"],
            "run_scores": [item["score"] for item in results],
        })

    conservative_score = round(earned_weight * 100.0, 2)
    provisional_score = round(earned_weight / assessed_weight * 100.0, 2) if assessed_weight else 0.0
    return {
        "criteria": aggregated,
        "judge_score_conservative": conservative_score,
        "judge_score_assessed_only": provisional_score,
        "assessment_coverage": round(assessed_weight, 4),
        "needs_human_review": assessed_weight < 0.9999 or any(item["confidence"] < 0.5 for item in aggregated),
        "run_summaries": [run.get("summary", "") for run in runs],
    }


def hybrid_score(aggregate: dict[str, Any], gate: GateDecision, *, rule_weight: float = 0.4) -> dict[str, Any]:
    if not 0.0 <= rule_weight <= 1.0:
        raise SchemaError("rule_weight must be between 0 and 1")
    judge_score = float(aggregate["judge_score_conservative"])
    if gate.rule_score is None:
        raw_score = judge_score
        applied_rule_weight = 0.0
    else:
        raw_score = gate.rule_score * rule_weight + judge_score * (1.0 - rule_weight)
        applied_rule_weight = rule_weight
    cap = float(gate.cap)
    final_score = 0.0 if gate.hard_failed else min(raw_score, cap)
    return {
        "final_score": round(final_score, 2),
        "uncapped_score": round(raw_score, 2),
        "score_cap": cap,
        "rule_weight": applied_rule_weight,
        "judge_weight": 1.0 - applied_rule_weight,
        "rule_gate": {
            "status": gate.status,
            "reason": gate.reason,
            "rule_score": gate.rule_score,
            "failed_check_ids": list(gate.failed_check_ids),
        },
    }


class JudgeRunner:
    def __init__(self, client: JsonClient) -> None:
        self.client = client

    def generate_rubric(
        self,
        *,
        task_id: str,
        instruction: str,
        objective_checks: str = "",
        references: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        instruction_hash = stable_hash(instruction)
        raw = self.client.complete_json(rubric_messages(
            task_id=task_id,
            instruction=instruction,
            objective_checks=objective_checks,
            reference_inventory=artifact_inventory(references or []),
        ))
        return normalize_rubric(raw, task_id=task_id, instruction_hash=instruction_hash)

    def evaluate(
        self,
        *,
        instruction: str,
        rubric: dict[str, Any],
        artifacts: list[dict[str, Any]],
        references: list[dict[str, Any]] | None = None,
        rule_result: dict[str, Any] | None = None,
        judge_runs: int = 1,
        rule_weight: float = 0.4,
    ) -> dict[str, Any]:
        rubric = validate_rubric(rubric, instruction_hash=stable_hash(instruction))
        gate = apply_rule_gate(rule_result)
        if gate.hard_failed:
            empty_runs = [{
                "criteria": [
                    {"id": item["id"], "status": "cannot_assess", "score": None, "evidence": [],
                     "rationale": "规则硬门控失败，未调用 LLM judge。", "confidence": 1.0}
                    for item in rubric["criteria"]
                ],
                "summary": "规则硬门控失败。",
            }]
            aggregate = aggregate_judgments(empty_runs, rubric)
        else:
            if not 1 <= judge_runs <= 9:
                raise SchemaError("judge_runs must be between 1 and 9")
            tagged_artifacts = [{**item, "path": f"outputs/{item['path']}"} for item in artifacts]
            tagged_references = [{**item, "path": f"references/{item['path']}"} for item in (references or [])]
            evidence_sources = {
                item["path"]: str(item.get("content", ""))
                for item in tagged_artifacts + tagged_references
            }
            messages = judge_messages(
                instruction=instruction,
                rubric=rubric,
                artifacts=tagged_artifacts,
                references=tagged_references,
            )
            runs = [
                normalize_judgment(self.client.complete_json(messages), rubric, evidence_sources)
                for _ in range(judge_runs)
            ]
            aggregate = aggregate_judgments(runs, rubric)
        score = hybrid_score(aggregate, gate, rule_weight=rule_weight)
        return {
            "schema_version": "1.0",
            "task_id": rubric["task_id"],
            "rubric_hash": rubric["rubric_hash"],
            "instruction_hash": rubric["instruction_hash"],
            "artifact_bundle_hash": stable_hash(artifacts),
            "reference_bundle_hash": stable_hash(references or []),
            **aggregate,
            **score,
        }


def load_task_inputs(
    task_dir: Path,
    outputs_dir: Path,
    reference_dir: Path | None,
    *,
    max_files: int = 20,
    total_chars: int = 60000,
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    checks_path = task_dir / "evaluator" / "checks.yaml"
    objective_checks = checks_path.read_text(encoding="utf-8") if checks_path.is_file() else ""
    artifacts = (
        collect_artifacts(outputs_dir, max_files=max_files, total_char_limit=total_chars)
        if outputs_dir.is_dir()
        else []
    )
    references = []
    if reference_dir and reference_dir.is_dir():
        references = collect_artifacts(reference_dir, max_files=max_files, total_char_limit=total_chars)
    return instruction, objective_checks, artifacts, references
