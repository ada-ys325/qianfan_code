from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Protocol

from .artifacts import MediaConfig, artifact_inventory, collect_artifacts
from .checks import filter_duplicate_criteria, load_checks, checks_prompt_text
from .llm import build_multimodal_messages
from .prompts import judge_messages, rubric_messages
from .schema import (
    SchemaError, force_cannot_assess, force_media_unavailable, normalize_judgment,
    judgment_response_format, normalize_rubric, rubric_response_format, stable_hash, validate_rubric,
)


class JsonClient(Protocol):
    def complete_json(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SchemaError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def aggregate_judgments(runs: list[dict[str, Any]], rubric: dict[str, Any]) -> dict[str, Any]:
    by_id = {item["id"]: item for item in rubric["criteria"]}
    aggregate = []
    for criterion in rubric["criteria"]:
        values = [item for run in runs for item in run.get("criteria", []) if item.get("id") == criterion["id"]]
        assessed = [item for item in values if item.get("status") != "cannot_assess" and isinstance(item.get("score"), (int, float))]
        if not assessed:
            aggregate.append({"id": criterion["id"], "status": "cannot_assess", "score": None, "evidence": [], "confidence": 0.0})
            continue
        score = int(statistics.median([int(item["score"]) for item in assessed]))
        aggregate.append({"id": criterion["id"], "status": "assessed", "score": round(score, 4),
            "evidence": [e for item in assessed for e in item.get("evidence", [])],
            "confidence": round(sum(float(item.get("confidence", 0)) for item in assessed) / len(assessed), 4)})
    assessed_weight = sum(by_id[item["id"]]["weight"] for item in aggregate if item["status"] == "assessed")
    score = sum(by_id[item["id"]]["weight"] * (item["score"] or 0) / 4 for item in aggregate if item["status"] == "assessed")
    return {"criteria": aggregate, "judge_score_conservative": round(score, 4),
        "judge_score_assessed_only": round(score / assessed_weight, 4) if assessed_weight else 0.0,
        "assessment_coverage": round(assessed_weight, 4),
        "needs_human_review": assessed_weight < 0.9999 or any(item["status"] == "cannot_assess" for item in aggregate),
        "run_summaries": [run.get("summary", "") for run in runs]}


class JudgeRunner:
    def __init__(self, client: JsonClient, *, media_config: MediaConfig | None = None) -> None:
        self.client = client
        self.media_config = media_config or MediaConfig.from_env()

    def generate_rubric(self, *, task_id: str, instruction: str, checks_path: Path | None = None,
                        objective_checks: str = "", references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        checks = load_checks(checks_path) if checks_path else []
        summary = checks_prompt_text(checks) if checks else objective_checks
        raw = self.client.complete_json(
            rubric_messages(task_id=task_id, instruction=instruction,
                            checks_summary=summary, reference_inventory=artifact_inventory(references or [])),
            response_format=rubric_response_format(),
        )
        criteria = filter_duplicate_criteria(raw.get("criteria", []), checks)
        if len(criteria) < 3:
            criteria = [item for item in raw.get("criteria", []) if isinstance(item, dict)]
        raw["criteria"] = criteria
        return normalize_rubric(raw, task_id=task_id, instruction_hash=stable_hash(instruction))

    def evaluate(self, *, instruction: str, rubric: dict[str, Any], artifacts: list[dict[str, Any]],
                 references: list[dict[str, Any]] | None = None, judge_runs: int = 1) -> dict[str, Any]:
        rubric = validate_rubric(rubric, instruction_hash=stable_hash(instruction))
        if not 1 <= judge_runs <= 9:
            raise SchemaError("judge_runs must be between 1 and 9")
        tagged_artifacts = [{**item, "path": f"outputs/{item['path']}"} for item in artifacts]
        tagged_references = [{**item, "path": f"references/{item['path']}"} for item in (references or [])]
        media = [item for item in tagged_artifacts + tagged_references if item.get("category") in {"audio", "video"}]
        media_content = []
        if media:
            media_content = build_multimodal_messages(
                text="", artifacts=media, media_config=self.media_config,
            )[0]["content"][1:]
        unavailable = [item for item in media if (item.get("transport") or {}).get("status") != "ready"]
        messages = judge_messages(instruction=instruction, rubric=rubric, artifacts=artifact_inventory(tagged_artifacts),
            references=artifact_inventory(tagged_references), media_messages=media_content)
        unavailable_categories = {item["category"] for item in unavailable}
        response_format = judgment_response_format([item["id"] for item in rubric["criteria"]])
        runs = []
        for _ in range(judge_runs):
            run = normalize_judgment(self.client.complete_json(messages, response_format=response_format), rubric)
            run = force_cannot_assess(run, rubric)
            run = force_media_unavailable(run, rubric, unavailable_categories)
            runs.append(run)
        result = aggregate_judgments(runs, rubric)
        result["attachments"] = self.attachment_audit(tagged_artifacts, tagged_references)
        return result

    def attachment_audit(self, artifacts: list[dict[str, Any]], references: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"scope": scope, "path": item["path"], "mime_type": item["mime_type"],
            "size_bytes": item["size_bytes"], "category": item["category"],
            "transport_status": (item.get("transport") or {}).get("status", "not_applicable"),
            "mode": (item.get("transport") or {}).get("mode"),
            "cannot_assess_reason": (item.get("transport") or {}).get("reason"),
            "representation": self.media_config.video_mode if item["category"] == "video" else "direct"}
            for scope, values in (("output", artifacts), ("reference", references)) for item in values]


def load_task_inputs(task_dir: Path, *, outputs_dir: Path | None = None, reference_dir: Path | None = None,
                     media_config: MediaConfig | None = None) -> dict[str, Any]:
    instruction_path = task_dir / "instruction.md"
    if not instruction_path.exists():
        raise FileNotFoundError(instruction_path)
    outputs = collect_artifacts(outputs_dir or task_dir / "run_outputs", media_config=media_config)
    references = [] if reference_dir is None else collect_artifacts(reference_dir, media_config=media_config)
    return {"instruction": instruction_path.read_text(encoding="utf-8"), "artifacts": outputs,
        "references": references, "checks_path": task_dir / "evaluator" / "checks.yaml"}
