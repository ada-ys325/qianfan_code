#!/usr/bin/env python3
"""Run a checklist-aware LLM judge for generated Excel artifacts."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .excel_judge.artifact_summary import summarize_artifacts
    from .excel_judge.prompt import RUBRIC_DIMENSIONS, SYSTEM_PROMPT, build_dry_run_result, build_user_prompt
except ImportError:  # Support running this file directly from its extracted directory.
    from excel_judge.artifact_summary import summarize_artifacts
    from excel_judge.prompt import RUBRIC_DIMENSIONS, SYSTEM_PROMPT, build_dry_run_result, build_user_prompt

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    instruction = _read_text_arg(args.instruction)
    checklist = _read_text_arg(args.checklist) if args.checklist else ""
    artifact_summary = summarize_artifacts(args.artifact_dir)
    locked_rubrics = _read_rubrics_arg(args.rubric_file) if args.rubric_file else None
    user_prompt = build_user_prompt(instruction, checklist, artifact_summary, locked_rubrics=locked_rubrics)
    judge_input = {
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": json.loads(user_prompt),
        "runtime": {
            "provider": "openai_compatible",
            "base_url": args.base_url,
            "model": args.model,
            "api_key_env": args.api_key_env,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "dry_run": args.dry_run,
        },
    }
    _write_json(out_dir / "judge_input.json", judge_input)

    if args.dry_run:
        result = build_dry_run_result()
        raw_text = json.dumps(result, ensure_ascii=False, indent=2)
        elapsed_ms = 0
    else:
        started = time.time()
        raw_text = _call_openai_compatible(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            base_url=args.base_url,
            model=args.model,
            api_key_env=args.api_key_env,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            retries=args.retries,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        try:
            result = _parse_json_object(raw_text)
        except Exception as exc:
            result = {
                "checklist_deduplication": {"covered_by_checklist": [], "excluded_from_rubric": []},
                "task_rubrics": [],
                "criteria_results": [],
                "check_results": [],
                "dimension_scores": {},
                "overall_score": None,
                "verdict": "parse_error",
                "failure_modes": ["LLM judge returned malformed JSON"],
                "recommendations": ["Inspect raw_text in judge_result.json and rerun with a stricter model or repair prompt."],
                "parse_error": str(exc),
            }
    result = normalize_judge_result(result, locked_rubrics=locked_rubrics)

    result_envelope = {
        "model": args.model,
        "elapsed_ms": elapsed_ms,
        "dry_run": args.dry_run,
        "result": result,
        "raw_text": raw_text,
    }
    _write_json(out_dir / "judge_result.json", result_envelope)
    (out_dir / "judge_report.md").write_text(
        render_markdown_report(
            instruction=instruction,
            checklist=checklist,
            artifact_summary=artifact_summary,
            result=result,
            dry_run=args.dry_run,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out_dir / 'judge_input.json'}")
    print(f"wrote {out_dir / 'judge_result.json'}")
    print(f"wrote {out_dir / 'judge_report.md'}")
    return 0


def _default_levels() -> dict[str, str]:
    return {
        "0": "Not satisfied.",
        "1": "Severely deficient.",
        "2": "Partially satisfied.",
        "3": "Mostly satisfied with minor gaps.",
        "4": "Fully satisfied.",
    }


def _normalize_rubrics(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    rubrics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or f"criterion_{index}").strip() or f"criterion_{index}"
        if cid in seen:
            cid = f"{cid}_{index}"
        seen.add(cid)
        dimension = str(item.get("dimension") or "instruction_coverage").strip()
        if dimension not in RUBRIC_DIMENSIONS:
            dimension = "instruction_coverage"
        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        levels_raw = item.get("levels") if isinstance(item.get("levels"), dict) else _default_levels()
        rubrics.append(
            {
                "id": cid,
                "dimension": dimension,
                "description": str(item.get("description") or item.get("criterion") or cid),
                "weight": max(0.0, weight),
                "evidence_required": bool(item.get("evidence_required", True)),
                "levels": {str(level): str(levels_raw.get(str(level), levels_raw.get(level, _default_levels()[str(level)]))) for level in range(5)},
            }
        )
    total = sum(item["weight"] for item in rubrics)
    if total <= 0 and rubrics:
        total = float(len(rubrics))
        for item in rubrics:
            item["weight"] = 1.0
    for item in rubrics:
        item["weight"] = round(item["weight"] / total, 8) if total else 0.0
    return rubrics


def _legacy_dimension_scores_to_criteria(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scores = result.get("dimension_scores")
    if not isinstance(scores, dict):
        return [], []
    rubrics = []
    criteria_results = []
    for dimension, score_obj in scores.items():
        if not isinstance(score_obj, dict):
            continue
        if score_obj.get("applicable") is False:
            continue
        try:
            score_value = float(score_obj.get("score", 0.0))
        except (TypeError, ValueError):
            score_value = 0.0
        cid = str(dimension)
        rubrics.append(
            {
                "id": cid,
                "dimension": cid if cid in RUBRIC_DIMENSIONS else "instruction_coverage",
                "description": str(score_obj.get("reason") or cid),
                "weight": 1.0,
                "evidence_required": True,
                "levels": _default_levels(),
            }
        )
        criteria_results.append(
            {
                "id": cid,
                "score": int(round(max(0.0, min(1.0, score_value)) * 4.0)),
                "evidence": str(score_obj.get("evidence", "")),
                "rationale": str(score_obj.get("reason", "")),
                "confidence": 0.5,
            }
        )
    return _normalize_rubrics(rubrics), criteria_results


def aggregate_criteria_score(rubrics: list[dict[str, Any]], criteria_results: list[dict[str, Any]]) -> float:
    by_id = {str(item.get("id")): item for item in criteria_results if isinstance(item, dict)}
    earned = 0.0
    total = 0.0
    for rubric in rubrics:
        cid = str(rubric.get("id", ""))
        try:
            weight = float(rubric.get("weight", 0.0))
            score = int(by_id.get(cid, {}).get("score", 0))
        except (TypeError, ValueError):
            continue
        total += max(0.0, weight)
        earned += max(0, min(4, score)) / 4.0 * max(0.0, weight)
    return round(earned / total, 4) if total else 0.0


def normalize_judge_result(result: dict[str, Any], locked_rubrics: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    normalized = dict(result)
    rubrics = _normalize_rubrics(locked_rubrics if locked_rubrics is not None else normalized.get("task_rubrics"))
    legacy = False
    if not rubrics:
        legacy = True
        rubrics, legacy_results = _legacy_dimension_scores_to_criteria(normalized)
        if legacy_results:
            normalized["criteria_results"] = legacy_results
    raw_results = normalized.get("criteria_results")
    by_id = {item["id"]: item for item in rubrics}
    criteria_results = []
    seen: set[str] = set()
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or item.get("criterion_id") or "").strip()
        if cid not in by_id or cid in seen:
            continue
        seen.add(cid)
        raw_score = item.get("score")
        if raw_score is None:
            score = None
        else:
            try:
                score = int(raw_score)
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(4, score))
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        criteria_results.append(
            {
                "id": cid,
                "score": score,
                "evidence": str(item.get("evidence", "")),
                "rationale": str(item.get("rationale", item.get("reason", ""))),
                "confidence": max(0.0, min(1.0, confidence)),
            }
        )
    for cid in by_id.keys() - seen:
        criteria_results.append({"id": cid, "score": 0, "evidence": "", "rationale": "Judge did not return this criterion.", "confidence": 0.0})
    normalized["task_rubrics"] = rubrics
    normalized["criteria_results"] = criteria_results
    if not legacy or normalized.get("overall_score") is None:
        normalized["overall_score"] = aggregate_criteria_score(rubrics, criteria_results)
    try:
        overall = float(normalized.get("overall_score", 0.0))
    except (TypeError, ValueError):
        overall = 0.0
    if overall > 1.0:
        overall /= 100.0
    normalized["overall_score"] = round(max(0.0, min(1.0, overall)), 4)
    normalized.setdefault(
        "verdict",
        "pass" if normalized["overall_score"] >= 0.8 else "borderline" if normalized["overall_score"] >= 0.6 else "fail",
    )
    normalized.setdefault("failure_modes", [])
    normalized.setdefault("recommendations", [])
    return normalized


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruction", required=True, help="Task instruction text or path to a text/markdown file.")
    parser.add_argument("--checklist", default="", help="Existing checklist text or path. Covered items are excluded from rubric.")
    parser.add_argument("--artifact-dir", required=True, help="Directory containing the agent-produced Excel artifacts.")
    parser.add_argument("--out-dir", required=True, help="Directory for judge_input.json, judge_result.json, judge_report.md.")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4o"))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="Only write judge input and a placeholder report; do not call the LLM.")
    parser.add_argument("--rubric-file", default="", help="Optional JSON file with fixed criteria/task_rubrics to score instead of generating rubrics.")
    return parser.parse_args(argv)


def _read_text_arg(value: str) -> str:
    possible_path = Path(value).expanduser()
    if possible_path.exists() and possible_path.is_file():
        return possible_path.read_text(encoding="utf-8", errors="replace")
    return value


def _read_rubrics_arg(value: str) -> list[dict[str, Any]]:
    payload = json.loads(Path(value).expanduser().read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("criteria") or payload.get("task_rubrics")
    if not isinstance(payload, list):
        raise ValueError("--rubric-file must contain a JSON array or an object with criteria/task_rubrics")
    return [item for item in payload if isinstance(item, dict)]


def _call_openai_compatible(
    *,
    system_prompt: str,
    user_prompt: str,
    base_url: str,
    model: str,
    api_key_env: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
) -> str:
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing API key env var: {api_key_env}")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 user-configured endpoint
                envelope = json.loads(resp.read().decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            content = str(content or "").strip()
            if not content:
                raise ValueError("LLM judge returned empty content")
            return content
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError, ValueError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM request failed: {last_exc}")


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end < start:
            raise
        obj = json.loads(cleaned[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("judge output is not a JSON object")
    return obj


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_markdown_report(
    *,
    instruction: str,
    checklist: str,
    artifact_summary: dict[str, Any],
    result: dict[str, Any],
    dry_run: bool,
) -> str:
    aggregate = artifact_summary.get("aggregate_signals", {})
    lines = [
        "# Excel LLM Judge Report",
        "",
        f"- Dry run: `{str(dry_run).lower()}`",
        f"- Excel files: `{artifact_summary.get('excel_file_count', 0)}`",
        f"- Readable workbook: `{str(aggregate.get('has_readable_workbook', False)).lower()}`",
        f"- Sheets scanned: `{aggregate.get('total_sheet_count', 0)}`",
        f"- Formula samples scanned: `{aggregate.get('scanned_formula_count', 0)}`",
        f"- Charts scanned: `{aggregate.get('scanned_chart_count', 0)}`",
        "",
        "## Instruction",
        "",
        _fenced(instruction.strip() or "(empty)"),
        "",
        "## Checklist",
        "",
        _fenced(checklist.strip() or "(empty checklist)"),
        "",
        "## Verdict",
        "",
        f"- Verdict: `{result.get('verdict', 'unknown')}`",
        f"- Overall score: `{result.get('overall_score')}`",
        "",
        "## Criterion Scores",
        "",
    ]
    criteria_results = result.get("criteria_results", [])
    rubric_by_id = {
        str(item.get("id")): item
        for item in result.get("task_rubrics", [])
        if isinstance(item, dict)
    }
    if isinstance(criteria_results, list) and criteria_results:
        for item in criteria_results:
            if not isinstance(item, dict):
                continue
            rubric = rubric_by_id.get(str(item.get("id")), {})
            lines.append(
                f"- `{item.get('id')}`: score=`{item.get('score')}`, weight=`{rubric.get('weight')}`, "
                f"confidence=`{item.get('confidence')}`; {item.get('rationale', '')}"
            )
    else:
        lines.append("- No criterion scores returned.")

    lines.extend(["", "## Legacy Dimension Scores", ""])
    dimension_scores = result.get("dimension_scores", {})
    if isinstance(dimension_scores, dict) and dimension_scores:
        for name, score_obj in dimension_scores.items():
            if not isinstance(score_obj, dict):
                continue
            lines.append(
                f"- `{name}`: score=`{score_obj.get('score')}`, applicable=`{score_obj.get('applicable')}`, "
                f"evidence_level=`{score_obj.get('evidence_level')}`; {score_obj.get('reason', '')}"
            )
    else:
        lines.append("- No dimension scores returned.")

    lines.extend(["", "## Checklist Deduplication", ""])
    dedup = result.get("checklist_deduplication", {})
    excluded = dedup.get("excluded_from_rubric", []) if isinstance(dedup, dict) else []
    if isinstance(excluded, list) and excluded:
        for item in excluded:
            if isinstance(item, dict):
                lines.append(f"- Excluded: {item.get('candidate_check', '')} ({item.get('reason', '')})")
    else:
        lines.append("- No excluded rubric items reported.")

    lines.extend(["", "## Failure Modes", ""])
    failure_modes = result.get("failure_modes", [])
    if isinstance(failure_modes, list) and failure_modes:
        lines.extend(f"- {item}" for item in failure_modes)
    else:
        lines.append("- None reported.")

    lines.extend(["", "## Recommendations", ""])
    recommendations = result.get("recommendations", [])
    if isinstance(recommendations, list) and recommendations:
        lines.extend(f"- {item}" for item in recommendations)
    else:
        lines.append("- None reported.")

    return "\n".join(lines) + "\n"


def _fenced(text: str) -> str:
    return "```text\n" + text.replace("```", "'''") + "\n```"


if __name__ == "__main__":
    sys.exit(main())
