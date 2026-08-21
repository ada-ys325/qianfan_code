from __future__ import annotations

import json
import statistics
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Protocol

from .artifacts import artifact_inventory, collect_artifacts
from .prompts import Message, judge_messages, rubric_messages
from .schema import (
    GateDecision,
    SchemaError,
    apply_rule_gate,
    judgment_response_format,
    normalize_rubric,
    rubric_response_format,
    stable_hash,
    validate_rubric,
)


class JsonClient(Protocol):
    def complete_json(
        self,
        messages: list[Message],
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SchemaError(f"expected a JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_task_inputs(task_dir: Path) -> tuple[str, str, str]:
    task_dir = task_dir.resolve()
    instruction_path = task_dir / "instruction.md"
    if not instruction_path.is_file():
        raise SchemaError(f"missing task instruction: {instruction_path}")
    instruction = instruction_path.read_text(encoding="utf-8").strip()
    if not instruction:
        raise SchemaError("task instruction is empty")
    checks_path = task_dir / "evaluator" / "checks.yaml"
    objective_checks = checks_path.read_text(encoding="utf-8") if checks_path.is_file() else ""
    return task_dir.name, instruction, objective_checks


def reference_inventory(reference_dir: Path | None, *, max_files: int = 20) -> list[dict[str, Any]]:
    if reference_dir is None or not reference_dir.exists():
        return []
    root = reference_dir.resolve()
    return [
        {
            "path": path.resolve().relative_to(root).as_posix(),
            "suffix": path.suffix.lower(),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    ][:max_files]


def _normalize_path(raw_path: str, *, artifact_paths: set[str]) -> str | None:
    normalized = raw_path.strip().replace("\\", "/")
    if not normalized.startswith("outputs/"):
        return None
    candidate_path = normalized[len("outputs/"):]
    return candidate_path if candidate_path in artifact_paths else None


def _normalize_evidence(raw: Any, *, artifact_paths: set[str], page_counts: dict[str, int]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = _normalize_path(str(item.get("path", "")).strip(), artifact_paths=artifact_paths)
        if path is None:
            continue
        evidence: dict[str, Any] = {"path": path}
        page = item.get("page")
        if page is not None:
            try:
                page_number = int(page)
            except (TypeError, ValueError):
                page_number = 0
            if 1 <= page_number <= page_counts.get(path, 0):
                evidence["page"] = page_number
        quote = str(item.get("quote", "")).strip()
        observation = str(item.get("visual_observation", item.get("observation", ""))).strip()
        if quote:
            evidence["quote"] = quote[:1000]
        if observation:
            evidence["visual_observation"] = observation[:1000]
        if len(evidence) > 1:
            normalized.append(evidence)
    return normalized


def normalize_judgment(raw: dict[str, Any], rubric: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    raw_criteria = raw.get("criteria")
    if not isinstance(raw_criteria, list):
        raise SchemaError("judge criteria must be a list")
    expected = [item["id"] for item in rubric["criteria"]]
    received = [str(item.get("id", "")) for item in raw_criteria if isinstance(item, dict)]
    if len(received) != len(set(received)):
        raise SchemaError("judge returned duplicate criterion ids")
    if set(received) != set(expected):
        raise SchemaError(f"judge criterion ids do not match rubric: expected {expected}, received {received}")

    artifact_paths = {item["path"] for item in artifacts}
    page_counts = {item["path"]: int(item.get("page_count", 0)) for item in artifacts}
    by_id = {str(item["id"]): item for item in raw_criteria}
    normalized: list[dict[str, Any]] = []
    for criterion in rubric["criteria"]:
        item = by_id[criterion["id"]]
        try:
            score = int(item.get("score"))
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"invalid score for {criterion['id']}") from exc
        if score not in (0, 1, 2, 3, 4):
            raise SchemaError(f"score for {criterion['id']} must be 0 to 4")
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"invalid confidence for {criterion['id']}") from exc
        confidence = min(1.0, max(0.0, confidence))
        evidence = _normalize_evidence(item.get("evidence"), artifact_paths=artifact_paths, page_counts=page_counts)
        status = "assessed"
        normalized.append({
            "id": criterion["id"],
            "score": score,
            "confidence": round(confidence, 4),
            "status": status,
            "evidence": evidence,
            "rationale": str(item.get("rationale", "")).strip()[:2000],
        })
    return {"criteria": normalized, "summary": str(raw.get("summary", "")).strip()[:4000]}


def aggregate_judgments(runs: list[dict[str, Any]], rubric: dict[str, Any]) -> dict[str, Any]:
    if not runs:
        raise SchemaError("at least one judge run is required")
    by_run = [{item["id"]: item for item in run["criteria"]} for run in runs]
    criteria: list[dict[str, Any]] = []
    earned = 0.0
    assessed_weight = 0.0
    for criterion in rubric["criteria"]:
        values = [run[criterion["id"]] for run in by_run]
        scores = [int(item["score"]) for item in values]
        score = int(statistics.median(scores))
        chosen = min(values, key=lambda item: abs(int(item["score"]) - score))
        confidence = statistics.mean(float(item["confidence"]) for item in values)
        status = "assessed" if any(item["status"] == "assessed" for item in values) else "unassessed"
        if status == "assessed":
            earned += float(criterion["weight"]) * score / 4.0
            assessed_weight += float(criterion["weight"])
        criteria.append({
            **criterion,
            "status": status,
            "score": score,
            "confidence": round(confidence, 4),
            "evidence": chosen["evidence"],
            "rationale": chosen["rationale"],
            "run_scores": scores,
        })
    conservative = round(earned * 100.0, 2)
    assessed_only = round(earned / assessed_weight * 100.0, 2) if assessed_weight else 0.0
    return {
        "criteria": criteria,
        "judge_score_conservative": conservative,
        "judge_score_assessed_only": assessed_only,
        "assessment_coverage": round(assessed_weight, 4),
        "needs_human_review": assessed_weight < 0.999999 or any(item["confidence"] < 0.5 for item in criteria),
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
    return {
        "final_score": round(0.0 if gate.hard_failed else min(raw_score, cap), 2),
        "raw_hybrid_score": round(raw_score, 2),
        "score_cap": cap,
        "rule_weight": applied_rule_weight,
        "judge_weight": 1.0 - applied_rule_weight,
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
        raw = self.client.complete_json(
            rubric_messages(
                task_id=task_id,
                instruction=instruction,
                objective_checks=objective_checks,
                reference_inventory=references or [],
            ),
            response_format=rubric_response_format(),
        )
        return normalize_rubric(raw, task_id=task_id, instruction_hash=instruction_hash)

    def evaluate(
        self,
        *,
        instruction: str,
        rubric: dict[str, Any],
        outputs_dir: Path,
        reference_dir: Path | None = None,
        rule_result: dict[str, Any] | None = None,
        judge_runs: int = 1,
        rule_weight: float = 0.4,
        max_files: int = 20,
        max_pages: int = 12,
        total_chars: int = 80000,
        model: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= judge_runs <= 9:
            raise SchemaError("judge_runs must be between 1 and 9")
        validate_rubric(rubric, instruction_hash=stable_hash(instruction))
        reference_context = (
            collect_artifacts(
                reference_dir,
                max_files=max_files,
                max_pages=max_pages,
                total_chars=total_chars,
                require_pdf=False,
            )
            if reference_dir is not None and reference_dir.is_dir()
            else nullcontext(([], []))
        )
        with (
            collect_artifacts(outputs_dir, max_files=max_files, max_pages=max_pages, total_chars=total_chars) as (artifacts, artifact_errors),
            reference_context as (reference_artifacts, reference_errors),
        ):
            gate = apply_rule_gate(rule_result, artifact_errors=artifact_errors)
            if gate.hard_failed:
                empty_runs = [{
                    "criteria": [{"id": item["id"], "score": 0, "confidence": 1.0, "status": "assessed", "evidence": [], "rationale": gate.reason} for item in rubric["criteria"]],
                    "summary": gate.reason,
                }]
                aggregate = aggregate_judgments(empty_runs, rubric)
            else:
                messages = judge_messages(
                    instruction=instruction,
                    rubric=rubric,
                    artifacts=artifacts,
                    reference_artifacts=reference_artifacts,
                )
                criteria_ids = [item["id"] for item in rubric["criteria"]]
                artifact_paths = [f"outputs/{item['path']}" for item in artifacts]
                response_format = judgment_response_format(criteria_ids, artifact_paths)
                runs = [
                    normalize_judgment(
                        self.client.complete_json(messages, response_format=response_format),
                        rubric,
                        artifacts,
                    )
                    for _ in range(judge_runs)
                ]
                aggregate = aggregate_judgments(runs, rubric)
            hybrid = hybrid_score(aggregate, gate, rule_weight=rule_weight)
            return {
                "schema_version": "1.0",
                "task_id": rubric["task_id"],
                "instruction_hash": rubric["instruction_hash"],
                "rubric_hash": rubric["rubric_hash"],
                "model": model,
                "artifact_inventory": artifact_inventory(artifacts),
                "artifact_errors": artifact_errors,
                "reference_inventory": artifact_inventory(reference_artifacts),
                "reference_errors": reference_errors,
                "gate": {
                    "status": gate.status,
                    "hard_failed": gate.hard_failed,
                    "cap": gate.cap,
                    "rule_score": gate.rule_score,
                    "failed_check_ids": list(gate.failed_check_ids),
                    "reason": gate.reason,
                },
                "aggregate": aggregate,
                "score": hybrid,
            }

    def run(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        rubric_keys = {"task_id", "instruction", "objective_checks", "references"}
        rubric = self.generate_rubric(**{key: value for key, value in kwargs.items() if key in rubric_keys})
        evaluate_args = {key: value for key, value in kwargs.items() if key not in {"task_id", "objective_checks", "references"}}
        result = self.evaluate(rubric=rubric, **evaluate_args)
        return rubric, result
