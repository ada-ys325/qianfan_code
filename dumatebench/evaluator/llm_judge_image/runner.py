from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .artifacts import collect_artifacts, discover_images, public_inventory
from .checks import summarize_checks
from .prompts import judge_messages, rubric_messages
from .schema import SchemaError, judge_response_format, normalize_rubric, rubric_response_format, stable_hash, validate_judge_result, validate_rubric

class JsonClient(Protocol):
    def complete_json(
        self,
        messages: list[dict[str, Any]],
        attachments: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def load_task_inputs(task_dir: Path) -> tuple[str, dict[str, Any], Path]:
    instruction = _read_text(task_dir / "instruction.md")
    task = {}
    task_path = task_dir / "task.yaml"
    if task_path.exists():
        try:
            import yaml
            task = yaml.safe_load(task_path.read_text(encoding="utf-8")) or {}
        except ImportError as exc:
            raise RuntimeError("PyYAML is required to read task.yaml") from exc
    checks_path = task_dir / "evaluator" / "checks.yaml"
    return instruction, task, checks_path

def _client_call(
    client: JsonClient,
    messages: list[dict[str, Any]],
    attachments: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return client.complete_json(messages, attachments=attachments, response_format=response_format)

class ImageJudgeRunner:
    def __init__(self, client: JsonClient, *, max_bytes: int = 20 * 1024 * 1024, max_count: int = 32, max_total_bytes: int = 100 * 1024 * 1024, transport_mode: str = "data_url", svg_policy: str = "rasterize") -> None:
        self.client, self.options = client, dict(max_bytes=max_bytes, max_count=max_count, max_total_bytes=max_total_bytes, transport_mode=transport_mode, svg_policy=svg_policy)
    def generate_rubric(
        self,
        task_dir: Path,
        reference_dir: Path | None = None,
        reference_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        instruction, _, checks_path = load_task_inputs(task_dir)
        source = reference_dir or (task_dir / "workspace_seed")
        inventory = discover_images(source, "reference", max_bytes=self.options["max_bytes"], max_count=self.options["max_count"], svg_policy=self.options["svg_policy"])
        raw = _client_call(
            self.client,
            rubric_messages(instruction, summarize_checks(checks_path), public_inventory(inventory), reference_context),
            response_format=rubric_response_format(),
        )
        rubric = validate_rubric(raw)
        return {"schema_version": "1.0", "rubric": rubric, "rubric_hash": stable_hash(rubric), "checks": summarize_checks(checks_path)}
    def judge(
        self,
        task_dir: Path,
        candidate_dir: Path,
        reference_dir: Path | None,
        rubric_doc: dict[str, Any],
        reference_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        instruction, _, checks_path = load_task_inputs(task_dir)
        rubric = validate_rubric(rubric_doc.get("rubric", rubric_doc))
        inventory = collect_artifacts(candidate_dir, reference_dir, **{k: v for k, v in self.options.items() if k != "transport_mode"})
        public = public_inventory(inventory)
        gate_reasons = [f"{x['id']}: {x.get('transport', {}).get('reason', 'unavailable')}" for x in inventory if x.get("transport", {}).get("status") != "ready"]
        requires_reference = any(x["dimension"] == "reference_fidelity" for x in rubric["criteria"])
        if requires_reference and not any(x["role"] == "reference" for x in inventory):
            gate_reasons.append("reference: no reference images were found")
        raw = _client_call(
            self.client,
            judge_messages(instruction, summarize_checks(checks_path), rubric, public, reference_context),
            inventory,
            judge_response_format([item["id"] for item in rubric["criteria"]]),
        )
        result = validate_judge_result(raw, rubric)
        if gate_reasons:
            unavailable_candidate = any(x["role"] == "candidate" and x.get("transport", {}).get("status") != "ready" for x in inventory)
            unavailable_reference = not any(x["role"] == "reference" for x in inventory) or any(x["role"] == "reference" and x.get("transport", {}).get("status") != "ready" for x in inventory)
            for criterion in result["criteria_results"]:
                cid = str(criterion.get("id", criterion.get("criterion_id", "")))
                dimension = next(x["dimension"] for x in rubric["criteria"] if x["id"] == cid)
                if unavailable_candidate or (unavailable_reference and dimension == "reference_fidelity"):
                    criterion.update(status="cannot_assess", score=None, evidence="Required image evidence was unavailable: " + "; ".join(gate_reasons))
            result["weighted_score"] = round(sum(next(x["weight"] for x in rubric["criteria"] if x["id"] == str(item.get("id", item.get("criterion_id", "")))) * item["score"] for item in result["criteria_results"] if item["score"] is not None), 4)
            result["gate"] = {"status": "blocked", "reasons": gate_reasons}
            result["summary"] = (result.get("summary", "") + " Attachment gate: " + "; ".join(gate_reasons)).strip()
        else:
            _convert_unjustified_cannot_assess_to_fail(result, rubric)
        result["rubric_hash"] = stable_hash(rubric)
        result["attachments"] = public
        return result
    def run(
        self,
        task_dir: Path,
        candidate_dir: Path,
        reference_dir: Path | None,
        output_dir: Path,
        reference_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        rubric_doc = self.generate_rubric(task_dir, reference_dir, reference_context)
        (output_dir / "rubric.json").write_text(json.dumps(rubric_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        result = self.judge(task_dir, candidate_dir, reference_dir, rubric_doc, reference_context)
        (output_dir / "judge_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {"rubric_hash": result["rubric_hash"], "weighted_score": result["weighted_score"], "gate": result["gate"], "criteria_count": len(result["criteria_results"])}
        (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def _convert_unjustified_cannot_assess_to_fail(result: dict[str, Any], rubric: dict[str, Any]) -> None:
    converted = []
    for criterion in result.get("criteria_results", []):
        if criterion.get("status") != "cannot_assess":
            continue
        criterion["status"] = "fail"
        criterion["score"] = 0
        criterion["evidence"] = (
            "模型返回 cannot_assess，但所需图片附件可用；按可见证据不足或未满足要求计为失败。"
            f" 原始说明：{criterion.get('evidence', '')}"
        ).strip()
        converted.append(str(criterion.get("id", criterion.get("criterion_id", ""))))
    if not converted:
        return
    weights = {item["id"]: float(item["weight"]) for item in rubric.get("criteria", [])}
    result["weighted_score"] = round(sum(
        weights.get(str(item.get("id", item.get("criterion_id", ""))), 0.0) * (0.0 if item.get("score") is None else float(item.get("score", 0)))
        for item in result.get("criteria_results", [])
    ), 4)
    note = "Converted unjustified cannot_assess to fail for: " + ", ".join(converted)
    result["summary"] = (str(result.get("summary", "")).strip() + " " + note).strip()
