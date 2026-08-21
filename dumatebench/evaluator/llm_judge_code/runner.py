from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from dumatebench.evaluator.llm_judge.runner import (
    aggregate_judgments,
    hybrid_score,
    normalize_judgment,
    read_json,
    write_json,
)
from dumatebench.evaluator.llm_judge.schema import apply_rule_gate

from .artifacts import artifact_inventory, collect_artifacts
from .prompts import judge_messages, rubric_messages
from .schema import SchemaError, normalize_rubric, stable_hash, validate_rubric


class JsonClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class CodeJudgeRunner:
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
        rule_weight: float = 0.0,
    ) -> dict[str, Any]:
        rubric = validate_rubric(rubric, instruction_hash=stable_hash(instruction))
        gate = apply_rule_gate(rule_result)
        if gate.hard_failed:
            empty_runs = [{
                "criteria": [
                    {
                        "id": item["id"],
                        "status": "cannot_assess",
                        "score": None,
                        "evidence": [],
                        "rationale": "规则硬门控失败，未调用 LLM judge。",
                        "confidence": 1.0,
                    }
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
            for item in aggregate.get("criteria", []):
                score = item.get("score")
                if isinstance(score, float) and score.is_integer():
                    item["score"] = int(score)
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
    max_files: int = 40,
    total_chars: int = 120000,
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8", errors="ignore")
    checks_path = task_dir / "evaluator" / "checks.yaml"
    objective_checks = checks_path.read_text(encoding="utf-8") if checks_path.is_file() else ""
    artifacts = collect_artifacts(outputs_dir, max_files=max_files, total_char_limit=total_chars) if outputs_dir.is_dir() else []
    references = collect_artifacts(reference_dir, max_files=max_files, total_char_limit=total_chars) if reference_dir and reference_dir.is_dir() else []
    return instruction, objective_checks, artifacts, references
