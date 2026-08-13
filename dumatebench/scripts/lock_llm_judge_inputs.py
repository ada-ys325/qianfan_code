#!/usr/bin/env python3
"""Lock per-task LLM judge criteria, references, and output targets from prior runs."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEFAULT_RUNS_DIR = REPO_ROOT / "cc_runs"
DEFAULT_OUT_DIR = ROOT / "locked_llm_judge_inputs"
DEFAULT_MODEL = "gemini-3.1-pro-preview"

CODE_SUFFIXES = {
    ".py",
    ".pyc",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".sql",
    ".r",
    ".m",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".cxx",
    ".cs",
    ".rb",
    ".php",
    ".lua",
    ".pl",
    ".pm",
    ".dart",
    ".ex",
    ".exs",
    ".erl",
    ".hrl",
    ".clj",
    ".cljs",
    ".fs",
    ".fsx",
    ".jl",
    ".nim",
    ".zig",
    ".vue",
    ".svelte",
    ".astro",
}
SUPPORTED_SUFFIXES = {
    ".doc",
    ".docx",
    ".txt",
    ".md",
    ".json",
    ".html",
    ".htm",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xltx",
    ".xltm",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".aac",
    ".ogg",
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
}
DIMENSION_ALIASES = {
    "content_correctness_faithfulness": "factual_correctness_faithfulness",
    "semantic_factual_correctness": "factual_correctness_faithfulness",
    "instruction_following": "requirement_completeness",
    "instruction_content_fidelity": "requirement_completeness",
    "instruction_coverage": "requirement_completeness",
    "document_structure": "structure_coherence",
    "layout_readability": "presentation_readability",
    "visual_quality_consistency": "presentation_readability",
    "composition_visual_hierarchy": "presentation_readability",
    "text_readability": "presentation_readability",
    "style_aesthetic_consistency": "presentation_readability",
    "artifact_integrity": "technical_quality",
    "technical_completeness": "technical_quality",
    "reference_fidelity": "factual_correctness_faithfulness",
}
DEFAULT_DIMENSION = "requirement_completeness"
GENERAL_DIMENSIONS = {
    "content_relevance",
    "factual_correctness_faithfulness",
    "requirement_completeness",
    "structure_coherence",
    "language_style",
    "presentation_readability",
    "edit_fidelity",
    "technical_quality",
}
CODE_DIMENSIONS: dict[str, str] = {
    "functional_correctness": "代码是否实现任务要求、核心行为和预期输出。",
    "bug_risk_defect": "代码是否存在明显缺陷、运行时错误、逻辑漏洞、状态不一致、异常路径失败或隐藏 bug 风险。",
    "reference_fidelity": "代码是否忠实使用给定 reference、接口说明、数据 schema 或正确答案。",
    "repo_integration": "代码是否正确集成到现有项目结构、API、依赖和调用链中。",
    "regression_safety": "是否避免破坏已有功能、公开接口、文件格式、兼容行为。",
    "edge_case_robustness": "是否处理边界条件、异常输入、空值、错误状态和 contracts。",
    "algorithmic_efficiency": "时间/空间复杂度、批量数据规模和实现效率是否合理。",
    "maintainability_readability": "代码是否清晰、局部、可维护，符合项目风格，避免过度复杂。",
    "security_safety": "是否避免注入、路径穿越、敏感信息泄露、危险执行和不安全依赖。",
}
TARGET_SCHEMA_KEYS = ("id", "dimension", "description", "weight", "evidence_required", "levels")
SCORE_LEVELS = ("0", "1", "2", "3", "4")


@dataclass
class CriterionSource:
    path: str
    run_backend: str
    output_file: str | None
    critical: bool


@dataclass
class TargetRecord:
    output_file: str
    artifact_type: str
    count: int = 0
    criteria: list[dict[str, Any]] = field(default_factory=list)
    references: Counter[str] = field(default_factory=Counter)
    sources: list[str] = field(default_factory=list)


@dataclass
class TaskRecord:
    task_id: str
    task_name: str = ""
    instruction: str = ""
    instruction_outputs: list[str] = field(default_factory=list)
    runs: set[str] = field(default_factory=set)
    targets: dict[str, TargetRecord] = field(default_factory=dict)
    references: Counter[str] = field(default_factory=Counter)
    source_task_views: list[Path] = field(default_factory=list)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_name(value: str, fallback: str = "item") -> str:
    name = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("._")
    return name[:120] or fallback


def readable_file_name(value: str, fallback: str = "item") -> str:
    name = Path(value).name.strip()
    name = re.sub(r"[/\\:\0-\x1f]+", "_", name).strip(" .")
    return name or fallback


def load_task_yaml(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    result: dict[str, str] = {}
    for key in ("task_id", "task_name"):
        match = re.search(rf"^{key}:\s*(.+?)\s*$", text, flags=re.M)
        if match:
            result[key] = match.group(1).strip().strip("'\"")
    return result


def canonical_task_id(raw: str) -> str:
    return raw.strip()


def artifact_type(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or ("code" if is_code_output(path) else "")


def is_code_output(path: str) -> bool:
    parts = Path(path).parts
    return Path(path).suffix.lower() in CODE_SUFFIXES or "code" in parts or "scripts" in parts


def is_supported_output(path: str) -> bool:
    return is_code_output(path) or Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def iter_run_task_views(runs_dir: Path) -> list[Path]:
    views = sorted({path.parent for path in runs_dir.glob("**/task_view/task.yaml")})
    if views:
        return views
    return sorted({path.parent for path in runs_dir.glob("**/task.yaml")})


def extract_detail_json(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def add_output_candidate(candidates: list[str], value: Any) -> None:
    if isinstance(value, str):
        text = normalize_output_path(value)
        if text.startswith("run_outputs/"):
            candidates.append(text)


def normalize_output_path(value: str) -> str:
    text = value.strip().strip("`").rstrip("，。；,;")
    if text.startswith("/outputs/"):
        text = "run_outputs/" + text[len("/outputs/") :]
    elif text.startswith("outputs/"):
        text = "run_outputs/" + text[len("outputs/") :]
    return text


def infer_outputs_from_rule_result(rule_result: dict[str, Any] | None) -> list[str]:
    candidates: list[str] = []
    checks = rule_result.get("checks") if isinstance(rule_result, dict) else []
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        detail = extract_detail_json(check.get("detail"))
        if not detail:
            continue
        for key in ("file", "output_file"):
            add_output_candidate(candidates, detail.get(key))
        for key in ("expected_files", "required_files"):
            values = detail.get(key)
            if isinstance(values, list):
                for item in values:
                    add_output_candidate(candidates, item)
    return dedupe_keep_order(candidates)


def infer_outputs_from_final_report(path: Path) -> list[str]:
    data = read_json(path)
    reports = data.get("artifact_reports") if isinstance(data, dict) else None
    candidates: list[str] = []
    for item in reports if isinstance(reports, list) else []:
        if isinstance(item, dict):
            add_output_candidate(candidates, item.get("output_file"))
    return dedupe_keep_order(candidates)


def infer_outputs_from_instruction(instruction: str) -> list[str]:
    candidates: list[str] = []
    for match in re.finditer(r"(?:run_outputs|/outputs|outputs)/[^\s`\"'，。；,;）)\]}<>]+", instruction):
        add_output_candidate(candidates, match.group(0))
    return dedupe_keep_order(candidates)


def dedupe_keep_order(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            output.append(value)
            seen.add(value)
    return output


def normalize_dimension(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_DIMENSION
    if raw in GENERAL_DIMENSIONS:
        return raw
    return DIMENSION_ALIASES.get(raw, DEFAULT_DIMENSION)


def normalize_code_dimension(value: Any) -> str:
    raw = str(value or "").strip()
    if raw in CODE_DIMENSIONS:
        return raw
    compact = re.sub(r"[\s-]+", "_", raw.lower()).strip("_")
    if compact in CODE_DIMENSIONS:
        return compact
    return "functional_correctness"


def default_levels(description: str) -> dict[str, str]:
    return {
        "0": f"完全未满足：{description}",
        "1": f"仅极少满足：{description}",
        "2": f"部分满足但缺口明显：{description}",
        "3": f"基本满足，仅有小缺口：{description}",
        "4": f"完整满足：{description}",
    }


def normalize_criterion(raw: dict[str, Any], index: int, *, source: CriterionSource) -> dict[str, Any] | None:
    description = str(raw.get("description") or raw.get("criterion") or raw.get("id") or "").strip()
    if not description:
        return None
    cid = str(raw.get("id") or f"criterion_{index}").strip() or f"criterion_{index}"
    cid = re.sub(r"[^0-9A-Za-z_]+", "_", cid).strip("_") or f"criterion_{index}"
    levels_raw = raw.get("levels") if isinstance(raw.get("levels"), dict) else default_levels(description)
    levels = {level: str(levels_raw.get(level, levels_raw.get(int(level), default_levels(description)[level]))).strip() for level in SCORE_LEVELS}
    try:
        weight = float(raw.get("weight", 1.0))
    except (TypeError, ValueError):
        weight = 1.0
    if source.critical or bool(raw.get("critical")):
        weight *= 1.5
    item = {
        "id": cid,
        "dimension": normalize_dimension(raw.get("dimension")),
        "description": description,
        "weight": max(weight, 0.01),
        "evidence_required": bool(raw.get("evidence_required", True)),
        "levels": levels,
        "_source": {
            "path": source.path,
            "run_backend": source.run_backend,
            "output_file": source.output_file,
            "critical": bool(source.critical or raw.get("critical")),
        },
    }
    if raw.get("modality"):
        item["modality"] = str(raw["modality"])
    if raw.get("evidence_hint"):
        item["evidence_hint"] = str(raw["evidence_hint"])
    return item


def extract_criteria_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = [
        data.get("criteria"),
        data.get("task_rubrics"),
    ]
    rubric = data.get("rubric")
    if isinstance(rubric, dict):
        candidates.extend([rubric.get("criteria"), rubric.get("task_rubrics")])
    report = data.get("judge_report")
    if isinstance(report, dict):
        candidates.extend([report.get("criteria"), report.get("task_rubrics")])
        report_rubric = report.get("rubric")
        if isinstance(report_rubric, dict):
            candidates.extend([report_rubric.get("criteria"), report_rubric.get("task_rubrics")])
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            return [item for item in candidate if isinstance(item, dict)]
    return []


def extract_reference_selection(data: dict[str, Any]) -> list[str]:
    reports = [data]
    if isinstance(data.get("judge_report"), dict):
        reports.append(data["judge_report"])
    result = data.get("result")
    if isinstance(result, dict):
        reports.append(result)
    refs: list[str] = []
    for report in reports:
        selection = report.get("reference_selection")
        if not isinstance(selection, dict):
            continue
        files = selection.get("files")
        for item in files if isinstance(files, list) else []:
            if not isinstance(item, dict) or not item.get("selected", False):
                continue
            path = str(item.get("path") or "").strip()
            if path:
                refs.append(normalize_reference_path(path))
    return dedupe_keep_order(refs)


def normalize_reference_path(path: str) -> str:
    path = path.strip().lstrip("/")
    if path.startswith(("workspace_seed/", "web_reference/", ".llm_judge_selected_references/")):
        return path
    if path.startswith(("history_agent_files/", "uploads/")):
        return f"workspace_seed/{path}"
    return path


def collect_task_records(runs_dir: Path, task_id_filter: set[str] | None = None) -> dict[str, TaskRecord]:
    records: dict[str, TaskRecord] = {}
    for task_view in iter_run_task_views(runs_dir):
        meta = load_task_yaml(task_view / "task.yaml")
        task_id = canonical_task_id(meta.get("task_id") or task_view.parent.name)
        if task_id_filter and task_id not in task_id_filter and task_id.lower() not in task_id_filter:
            continue
        backend = task_view.parents[1].name if task_view.name == "task_view" else task_view.parent.name
        record = records.setdefault(task_id, TaskRecord(task_id=task_id))
        record.task_name = record.task_name or meta.get("task_name", "")
        record.runs.add(backend)
        record.source_task_views.append(task_view)
        instruction_path = task_view / "instruction.md"
        if instruction_path.is_file() and not record.instruction:
            record.instruction = instruction_path.read_text(encoding="utf-8", errors="ignore")
            record.instruction_outputs = infer_outputs_from_instruction(record.instruction)

        outputs = infer_outputs_from_final_report(task_view / "run_outputs" / "llm_judge_score.json")
        for score_file in sorted((task_view / "run_outputs" / "llm_judge_scores").glob("*.json")):
            data = read_json(score_file)
            if not data:
                continue
            score_outputs = infer_outputs_from_rule_result(data.get("rule_result"))
            if len(score_outputs) == 1:
                output_file = score_outputs[0]
            else:
                output_file = match_score_file_to_output(score_file, score_outputs or outputs)
            if output_file:
                outputs.append(output_file)
            criteria = extract_criteria_payload(data)
            references = extract_reference_selection(data)
            if output_file and is_supported_output(output_file):
                target = ensure_target(record, output_file)
                target.count += 1
                target.sources.append(score_file.as_posix())
                target.references.update(references)
                source = CriterionSource(score_file.as_posix(), backend, output_file, False)
                target.criteria.extend(filter(None, (normalize_criterion(item, len(target.criteria) + i + 1, source=source) for i, item in enumerate(criteria))))
                record.references.update(references)

        for rubric_file in sorted((task_view / "run_outputs").glob("*_llm_judge/rubric.json")):
            data = read_json(rubric_file)
            if not data:
                continue
            criteria = extract_criteria_payload(data)
            if not criteria:
                continue
            output_file = match_rubric_to_output(rubric_file, outputs)
            if not output_file or not is_supported_output(output_file):
                continue
            target = ensure_target(record, output_file)
            target.sources.append(rubric_file.as_posix())
            source = CriterionSource(rubric_file.as_posix(), backend, output_file, False)
            target.criteria.extend(filter(None, (normalize_criterion(item, len(target.criteria) + i + 1, source=source) for i, item in enumerate(criteria))))

        for output in outputs:
            if is_supported_output(output):
                ensure_target(record, output).count += 1
    return records


def ensure_target(record: TaskRecord, output_file: str) -> TargetRecord:
    if output_file not in record.targets:
        record.targets[output_file] = TargetRecord(output_file=output_file, artifact_type=artifact_type(output_file))
    return record.targets[output_file]


def match_score_file_to_output(score_file: Path, outputs: list[str]) -> str | None:
    if not outputs:
        return None
    stem = score_file.name.lower()
    scored = []
    for output in outputs:
        output_name = Path(output).name.lower()
        output_stem = Path(output).stem.lower()
        score = 0
        if output_name in stem:
            score += 100
        if output_stem and output_stem in stem:
            score += 50
        if Path(output).suffix.lower().lstrip(".") in stem:
            score += 10
        scored.append((score, output))
    scored.sort(reverse=True)
    return scored[0][1] if scored[0][0] > 0 else outputs[0]


def match_rubric_to_output(rubric_file: Path, outputs: list[str]) -> str | None:
    parent = rubric_file.parent.name
    suffix_preference: set[str]
    if parent.startswith("pdf"):
        suffix_preference = {".pdf"}
    elif parent.startswith("image"):
        suffix_preference = {".png", ".jpg", ".jpeg", ".webp"}
    elif parent.startswith("multimodal"):
        suffix_preference = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".mp4", ".mov", ".webm", ".mkv"}
    else:
        suffix_preference = set()
    for output in outputs:
        if Path(output).suffix.lower() in suffix_preference:
            return output
    return outputs[0] if outputs else None


def compact_task_for_llm(record: TaskRecord, max_criteria_per_target: int) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "task_name": record.task_name,
        "instruction": record.instruction[:12000],
        "literal_instruction_output_candidates": record.instruction_outputs,
        "runs": sorted(record.runs),
        "targets": [
            {
                "output_file": target.output_file,
                "artifact_type": target.artifact_type,
                "observed_count": target.count,
                "selected_references": list(target.references),
                "criteria": [
                    {key: item[key] for key in item.keys() if key != "_source"}
                    | {"source": item.get("_source")}
                    for item in target.criteria[:max_criteria_per_target]
                ],
            }
            for target in sorted(record.targets.values(), key=lambda item: (-item.count, item.output_file))
        ],
        "task_level_references": list(record.references),
    }


def build_prompt(record: TaskRecord, max_criteria_per_target: int) -> list[dict[str, str]]:
    payload = compact_task_for_llm(record, max_criteria_per_target)
    system = (
        "You consolidate DuMateBench LLM-judge inputs. Return only valid JSON. "
        "Merge duplicate criteria, preserve task-specific observable requirements, and convert every criterion to the locked schema. "
        "Give larger weights to criteria whose source critical flag is true."
    )
    user = {
        "task": payload,
        "required_output_schema": {
            "task_id": "string",
            "instruction_output_files": ["run_outputs/... paths that instruction.md requires the agent to create; convert /outputs/... paths in instruction.md to run_outputs/..."],
            "output_files": ["same as instruction_output_files after removing unsupported artifacts; keep code outputs if instruction.md asks for them"],
            "references": ["workspace_seed/... or web_reference/... files selected for judging"],
            "criteria": [
                {
                    "id": "snake_case_unique_id",
                    "dimension": "one of content_relevance, factual_correctness_faithfulness, requirement_completeness, structure_coherence, language_style, presentation_readability, edit_fidelity, technical_quality",
                    "description": "task-specific Chinese description",
                    "weight": "positive number; critical criteria should be larger",
                    "evidence_required": "boolean",
                    "levels": {"0": "...", "1": "...", "2": "...", "3": "...", "4": "..."},
                }
            ],
            "targets": [
                {
                    "output_file": "run_outputs/...",
                    "references": ["..."],
                    "criteria": [
                        {
                            "id": "snake_case_unique_id",
                            "dimension": "same enum as above",
                            "description": "same schema as above",
                            "weight": "positive number",
                            "evidence_required": "boolean",
                            "levels": {"0": "...", "1": "...", "2": "...", "3": "...", "4": "..."},
                        }
                    ],
                }
            ],
            "notes": ["short review notes in Chinese"],
        },
        "rules": [
            "Output criteria must have exactly id, dimension, description, weight, evidence_required, levels; do not include critical/status/score/evidence/rationale/source.",
            "First read instruction.md and extract only the output paths that the task asks the agent to produce. Convert container paths /outputs/... or outputs/... to run_outputs/... and put them in instruction_output_files.",
            "Do not include an output_file merely because it appears in historical checks, judge reports, or run artifacts. If instruction.md does not require it as an agent output, exclude it.",
            "output_files and targets must be a subset of instruction_output_files after removing unsupported artifact types.",
            "Normalize all levels to string keys 0..4.",
            "Deduplicate criteria semantically and by id.",
            "Do not over-merge: keep distinct observable requirements as separate criteria. Prefer 3-16 criteria for each non-code artifact when historical candidates exist.",
            "For code outputs such as .py/.js/.sh files, include the target output_file but you may leave criteria empty; the script will generate code criteria with a dedicated code judge rubric prompt.",
            "Prefer references that were selected in multiple successful runs or are explicitly named in instruction.md.",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_code_rubric_prompt(record: TaskRecord, output_file: str, references: list[str]) -> list[dict[str, str]]:
    dimensions = json.dumps(CODE_DIMENSIONS, ensure_ascii=False, indent=2)
    user = (
        f"任务 ID：{record.task_id}\n\n"
        f"代码产物路径：{output_file}\n\n"
        f"任务要求：\n<instruction>\n{record.instruction}\n</instruction>\n\n"
        f"固定 reference 文件清单：\n{json.dumps(references, ensure_ascii=False, indent=2)}\n\n"
        "请只为上述代码产物从头生成 code LLM judge rubric，不要复用历史普通 artifact judge criteria。"
        "代码评估维度定义如下；只选择适用维度：\n"
        f"{dimensions}\n\n"
        "请生成 5-12 个原子评分项。每项只评价一个可观察目标；description 必须说明代码应实现的具体行为、接口、数据处理、"
        "集成方式、鲁棒性或安全性。不要写成笼统的“代码质量”。需要基于 reference、正确答案、测试期望、API contract "
        "或 ground truth 才能可靠评分的项目设置 evidence_required=true。每项给出 0-4 五档、相互可区分的 task-specific levels："
        "0=完全失败或相反，1=严重不足，2=部分满足，3=基本满足但有小缺口，4=充分满足。权重为正数，无需预先归一化。\n\n"
        "严格输出 JSON："
        '{"criteria":[{"id":"snake_case","dimension":"代码维度键",'
        '"description":"原子标准","weight":1,"evidence_required":true,'
        '"levels":{"0":"...","1":"...","2":"...","3":"...","4":"..."}}]}'
    )
    return [
        {
            "role": "system",
            "content": (
                "你是 DuMateBench 的资深代码评估标准设计者。请把代码产物任务拆成原子、可审计、候选无关的评分项。"
                "你只能设计 rubric，不能猜测或评价尚未提供的候选代码。只输出 JSON。"
            ),
        },
        {"role": "user", "content": user},
    ]


def parse_llm_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        content = "".join(parts)
    text = str(content or "").strip()
    if not text:
        raise ValueError("LLM response content is empty")
    value = parse_jsonish_text(text)
    for _ in range(2):
        if isinstance(value, str):
            value = parse_jsonish_text(value.strip())
            continue
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            value = value[0]
            continue
        break
    if not isinstance(value, dict):
        preview = text[:300].replace("\n", "\\n")
        raise ValueError(f"LLM response JSON is not an object: {type(value).__name__}; preview={preview}")
    return value


def parse_jsonish_text(text: str) -> Any:
    stripped = strip_markdown_json_fence(text.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        last_error = exc
    extracted = extract_first_json_value(stripped)
    if extracted is None:
        raise last_error
    return json.loads(extracted)


def strip_markdown_json_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_first_json_value(text: str) -> str | None:
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return None
    start = min(starts)
    stack: list[str] = []
    in_string = False
    escape = False
    pairs = {"{": "}", "[": "]"}
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def call_openai_compatible_json(
    messages: list[dict[str, str]],
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            parsed = json.loads(body)
            content = parsed["choices"][0]["message"]["content"]
            return parse_llm_json_content(content)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM request failed after {retries + 1} attempts: {last_error}")


def default_code_criteria(output_file: str) -> list[dict[str, Any]]:
    output_name = Path(output_file).name
    return [
        {
            "id": "functional_correctness",
            "dimension": "functional_correctness",
            "description": f"评估 {output_name} 是否实现 instruction.md 中要求该代码产物完成的核心功能和预期输出。",
            "weight": 2,
            "evidence_required": True,
            "levels": {
                "0": "代码未实现核心功能或与任务目标相反。",
                "1": "仅实现极少功能，主要行为缺失或无法完成预期输出。",
                "2": "实现部分核心功能，但关键路径或输出仍有明显缺口。",
                "3": "基本实现核心功能，仅有小缺口或边界行为不足。",
                "4": "完整实现核心功能和预期输出。",
            },
        },
        {
            "id": "bug_risk_defect",
            "dimension": "bug_risk_defect",
            "description": f"评估 {output_name} 是否存在明显运行时错误、逻辑缺陷、异常路径失败或隐藏 bug 风险。",
            "weight": 1.5,
            "evidence_required": True,
            "levels": {
                "0": "存在会阻断主要功能的严重缺陷或运行时错误。",
                "1": "存在多处高风险缺陷，主要场景很可能失败。",
                "2": "存在若干明显缺陷，部分场景会失败或结果不可靠。",
                "3": "整体可靠，仅有少量低风险问题或边界不足。",
                "4": "未发现明显缺陷，主要路径和异常路径处理稳健。",
            },
        },
        {
            "id": "reference_fidelity",
            "dimension": "reference_fidelity",
            "description": f"评估 {output_name} 是否忠实遵循固定 reference、输入数据 schema、接口说明或正确答案要求。",
            "weight": 1,
            "evidence_required": True,
            "levels": {
                "0": "明显违背 reference、schema、接口或正确答案要求。",
                "1": "大部分实现与 reference 或接口要求不一致。",
                "2": "部分遵循 reference，但关键字段、接口或数据处理有明显偏差。",
                "3": "基本忠实，仅有小遗漏或轻微偏差。",
                "4": "完整且准确地忠实于 reference、schema 和接口要求。",
            },
        },
        {
            "id": "edge_case_robustness",
            "dimension": "edge_case_robustness",
            "description": f"评估 {output_name} 是否合理处理边界条件、空输入、错误状态、缺失文件或异常数据。",
            "weight": 1,
            "evidence_required": False,
            "levels": {
                "0": "几乎不处理边界或异常情况，容易崩溃或产生严重错误。",
                "1": "仅处理极少异常，大量常见边界情况失败。",
                "2": "处理部分边界情况，但仍有明显漏洞。",
                "3": "多数边界和异常情况处理合理，仅有小缺口。",
                "4": "边界条件和异常路径处理全面稳健。",
            },
        },
        {
            "id": "maintainability_readability",
            "dimension": "maintainability_readability",
            "description": f"评估 {output_name} 是否清晰、局部、可维护，并符合任务代码的合理结构和风格。",
            "weight": 1,
            "evidence_required": False,
            "levels": {
                "0": "代码混乱不可维护，难以理解或修改。",
                "1": "结构和命名严重混乱，维护成本很高。",
                "2": "基本可读，但存在明显重复、耦合或复杂度问题。",
                "3": "整体清晰可维护，仅有少量风格或组织问题。",
                "4": "代码结构清晰、局部、易读且易维护。",
            },
        },
    ]


def normalize_code_criteria(raw: dict[str, Any], output_file: str) -> list[dict[str, Any]]:
    values = raw.get("criteria")
    if not isinstance(values, list):
        values = []
    criteria = [clean_criterion(item, code=True) for item in values if isinstance(item, dict)]
    criteria = dedupe_code_criteria(criteria)
    if len(criteria) < 3:
        criteria = dedupe_code_criteria(criteria + default_code_criteria(output_file))
    if len(criteria) > 16:
        criteria = criteria[:16]
    normalize_weights(criteria)
    return criteria


def dedupe_code_criteria(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in criteria:
        clean = clean_criterion(item, code=True)
        key = clean["id"]
        if key not in by_id or float(clean["weight"]) > float(by_id[key]["weight"]):
            by_id[key] = clean
    return list(by_id.values())


def generate_code_criteria(
    record: TaskRecord,
    output_file: str,
    references: list[str],
    args: argparse.Namespace,
    api_key: str,
) -> list[dict[str, Any]]:
    if args.dry_run:
        criteria = default_code_criteria(output_file)
        normalize_weights(criteria)
        return criteria
    raw = call_openai_compatible_json(
        build_code_rubric_prompt(record, output_file, references),
        model=args.model,
        base_url=args.base_url,
        api_key=api_key,
        timeout=args.timeout,
        retries=args.retries,
    )
    return normalize_code_criteria(raw, output_file)


def fill_code_criteria(payload: dict[str, Any], record: TaskRecord, args: argparse.Namespace, api_key: str) -> None:
    for target in payload.get("targets", []):
        if not isinstance(target, dict):
            continue
        output_file = str(target.get("output_file") or "")
        if not is_code_output(output_file):
            continue
        target["criteria"] = generate_code_criteria(record, output_file, payload.get("references", []), args, api_key)


def merge_without_llm(record: TaskRecord) -> dict[str, Any]:
    targets = []
    all_criteria: list[dict[str, Any]] = []
    for output_file in record.instruction_outputs:
        if not is_supported_output(output_file):
            continue
        target = record.targets.get(output_file) or TargetRecord(output_file=output_file, artifact_type=artifact_type(output_file))
        criteria = dedupe_criteria(target.criteria)
        targets.append({"output_file": target.output_file, "references": list(target.references), "criteria": criteria})
        all_criteria.extend(criteria)
    merged = dedupe_criteria(all_criteria)
    return {
        "task_id": record.task_id,
        "instruction_output_files": record.instruction_outputs,
        "output_files": [item["output_file"] for item in targets],
        "references": list(record.references),
        "criteria": merged,
        "targets": targets,
        "notes": ["dry-run heuristic merge; review before use"],
    }


def dedupe_criteria(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for item in criteria:
        clean = clean_criterion(item)
        key = re.sub(r"\W+", "", (clean["id"] + clean["description"][:30]).lower())
        if key not in by_key or float(clean["weight"]) > float(by_key[key]["weight"]):
            by_key[key] = clean
    values = list(by_key.values())[:16]
    normalize_weights(values)
    return values


def clean_criterion(item: dict[str, Any], *, code: bool = False) -> dict[str, Any]:
    description = str(item.get("description") or item.get("criterion") or item.get("id") or "").strip()
    cid = re.sub(r"[^0-9A-Za-z_]+", "_", str(item.get("id") or description[:40] or "criterion")).strip("_") or "criterion"
    levels_raw = item.get("levels") if isinstance(item.get("levels"), dict) else default_levels(description)
    try:
        weight = float(item.get("weight", 1.0))
    except (TypeError, ValueError):
        weight = 1.0
    return {
        "id": cid,
        "dimension": normalize_code_dimension(item.get("dimension")) if code else normalize_dimension(item.get("dimension")),
        "description": description,
        "weight": max(weight, 0.01),
        "evidence_required": bool(item.get("evidence_required", True)),
        "levels": {level: str(levels_raw.get(level, levels_raw.get(int(level), default_levels(description)[level]))).strip() for level in SCORE_LEVELS},
    }


def normalize_weights(criteria: list[dict[str, Any]]) -> None:
    total = sum(max(0.0, float(item.get("weight", 0.0))) for item in criteria)
    if total <= 0:
        total = float(len(criteria) or 1)
        for item in criteria:
            item["weight"] = 1.0
    for item in criteria:
        item["weight"] = round(float(item["weight"]) / total, 8)
    if criteria:
        drift = round(1.0 - sum(float(item["weight"]) for item in criteria), 8)
        criteria[-1]["weight"] = round(float(criteria[-1]["weight"]) + drift, 8)


def default_artifact_criteria(output_file: str) -> list[dict[str, Any]]:
    kind = artifact_type(output_file) or "artifact"
    output_name = Path(output_file).name
    return [
        {
            "id": "requirement_completeness",
            "dimension": "requirement_completeness",
            "description": f"评估 {output_name} 是否完整覆盖任务指令中对该 {kind} 产物的核心要求和交付约束。",
            "weight": 1,
            "evidence_required": True,
            "levels": {
                "0": "几乎未覆盖任务要求，关键交付内容缺失。",
                "1": "仅覆盖很少任务要求，主要内容或约束大量缺失。",
                "2": "覆盖部分任务要求，但存在明显缺口。",
                "3": "基本覆盖任务要求，仅有少量次要遗漏。",
                "4": "完整覆盖任务要求和交付约束。",
            },
        },
        {
            "id": "reference_fidelity",
            "dimension": "factual_correctness_faithfulness",
            "description": f"评估 {output_name} 是否忠实使用并反映固定 reference 文件中的关键信息。",
            "weight": 1,
            "evidence_required": True,
            "levels": {
                "0": "与 reference 明显矛盾或基本忽略 reference。",
                "1": "大部分内容与 reference 不一致。",
                "2": "部分匹配 reference，但关键事实或素材有明显偏差。",
                "3": "整体忠实于 reference，仅有小遗漏或轻微偏差。",
                "4": "完整且准确地忠实于 reference。",
            },
        },
        {
            "id": "technical_quality",
            "dimension": "technical_quality",
            "description": f"评估 {output_name} 的文件可用性、格式完整性和呈现质量是否满足任务场景。",
            "weight": 1,
            "evidence_required": True,
            "levels": {
                "0": "文件不可用、无法打开，或技术质量严重阻断评估。",
                "1": "文件可部分打开，但存在严重格式、渲染或播放问题。",
                "2": "文件基本可用，但技术问题明显影响阅读、查看或播放。",
                "3": "文件可用且技术质量基本合格，仅有小瑕疵。",
                "4": "文件完整可用，格式和呈现质量良好。",
            },
        },
    ]


def ensure_minimum_criteria(criteria: list[dict[str, Any]], output_file: str, minimum: int) -> list[dict[str, Any]]:
    if is_code_output(output_file):
        return criteria
    if len(criteria) >= minimum:
        return criteria
    existing_ids = {item["id"] for item in criteria}
    for item in default_artifact_criteria(output_file):
        if len(criteria) >= minimum:
            break
        if item["id"] not in existing_ids:
            criteria.append(clean_criterion(item))
            existing_ids.add(item["id"])
    return criteria


def fallback_criteria_for_target(record: TaskRecord, output_file: str) -> list[dict[str, Any]]:
    target = record.targets.get(output_file)
    values = list(target.criteria) if target else []
    current_type = artifact_type(output_file)
    if len(values) < 3:
        for other in record.targets.values():
            if other.output_file != output_file and other.artifact_type == current_type:
                values.extend(other.criteria)
    return dedupe_criteria(values)


def validate_locked_payload(payload: dict[str, Any], record: TaskRecord, *, min_criteria_per_artifact: int) -> dict[str, Any]:
    raw_instruction_outputs = payload.get("instruction_output_files")
    if isinstance(raw_instruction_outputs, list):
        instruction_outputs = [normalize_output_path(str(value)) for value in raw_instruction_outputs if isinstance(value, str)]
    else:
        instruction_outputs = record.instruction_outputs
    instruction_outputs = dedupe_keep_order([value for value in instruction_outputs if value.startswith("run_outputs/")])
    allowed_outputs = {value for value in instruction_outputs if is_supported_output(value)}
    requested_outputs = [value for value in payload.get("output_files", []) if isinstance(value, str)]
    if requested_outputs:
        output_files = [value for value in requested_outputs if value in allowed_outputs]
    else:
        output_files = [value for value in instruction_outputs if value in allowed_outputs]
    references = dedupe_keep_order([normalize_reference_path(str(value)) for value in payload.get("references", []) if isinstance(value, str)])
    targets = []
    payload_targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []
    by_output = {str(item.get("output_file")): item for item in payload_targets if isinstance(item, dict)}
    all_criteria: list[dict[str, Any]] = []
    for output_file in output_files:
        source_target = by_output.get(output_file)
        fallback = record.targets.get(output_file)
        if is_code_output(output_file):
            criteria = []
        else:
            raw_criteria = source_target.get("criteria") if isinstance(source_target, dict) else None
            if not isinstance(raw_criteria, list) or not raw_criteria:
                raw_criteria = fallback.criteria if fallback else []
            criteria = [clean_criterion(item) for item in raw_criteria if isinstance(item, dict)][:16]
            fallback_criteria = fallback_criteria_for_target(record, output_file)
            if len(criteria) < min_criteria_per_artifact and len(fallback_criteria) > len(criteria):
                criteria = dedupe_criteria(criteria + fallback_criteria)[:16]
            criteria = ensure_minimum_criteria(criteria, output_file, min_criteria_per_artifact)
        normalize_weights(criteria)
        target_refs = source_target.get("references") if isinstance(source_target, dict) else []
        if not isinstance(target_refs, list):
            target_refs = []
        target_refs = dedupe_keep_order([normalize_reference_path(str(value)) for value in target_refs if isinstance(value, str)] or (list(fallback.references) if fallback else []))
        targets.append(
            {
                "id": "",
                "output_file": output_file,
                "artifact_type": artifact_type(output_file),
                "references": target_refs,
                "criteria": criteria,
                "criteria_file": "",
            }
        )
        all_criteria.extend(criteria)
        references.extend(target_refs)
    task_criteria = [clean_criterion(item) for item in payload.get("criteria", []) if isinstance(item, dict)]
    if not task_criteria:
        task_criteria = dedupe_criteria(all_criteria)
    if not output_files:
        task_criteria = []
    normalize_weights(task_criteria)
    return {
        "schema_version": "1.0",
        "task_id": record.task_id,
        "task_name": record.task_name,
        "instruction_hash": stable_hash(record.instruction),
        "output_files": output_files,
        "references": dedupe_keep_order(references),
        "criteria": task_criteria[:16],
        "targets": targets,
        "notes": [str(value) for value in payload.get("notes", []) if isinstance(value, str)],
    }


def artifact_id(output_file: str, used: set[str]) -> str:
    path = Path(output_file)
    suffix = path.suffix.lower().lstrip(".") or "artifact"
    base = safe_name(readable_file_name(path.stem, "artifact"), "artifact")
    candidate = f"{base}_{suffix}"
    if candidate not in used:
        used.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used:
        index += 1
    result = f"{candidate}_{index}"
    used.add(result)
    return result


def criteria_filename(output_file: str, used: set[str]) -> str:
    base = readable_file_name(output_file, "criteria")
    candidate = f"{base}.json"
    if candidate not in used:
        used.add(candidate)
        return candidate
    index = 2
    stem = Path(base).stem or "criteria"
    suffix = "".join(Path(base).suffixes)
    while f"{stem}_{index}{suffix}.json" in used:
        index += 1
    result = f"{stem}_{index}{suffix}.json"
    used.add(result)
    return result


def reference_alias(path: str, used: set[str]) -> str:
    alias = Path(path).name or safe_name(path, "reference")
    if alias not in used:
        used.add(alias)
        return alias
    stem = Path(alias).stem or "reference"
    suffix = Path(alias).suffix
    index = 2
    while f"{stem}_{index}{suffix}" in used:
        index += 1
    result = f"{stem}_{index}{suffix}"
    used.add(result)
    return result


def reference_entries(references: list[str]) -> list[dict[str, str]]:
    used: set[str] = set()
    return [{"path": path, "as": reference_alias(path, used)} for path in references]


def write_task_outputs(
    record: TaskRecord,
    payload: dict[str, Any],
    out_dir: Path,
    *,
    copy_references: bool,
    references_format: str,
) -> None:
    task_dir = out_dir / safe_name(record.task_id, "task")
    task_dir.mkdir(parents=True, exist_ok=True)
    evaluator_dir = task_dir / "evaluator"
    (task_dir / "instruction.md").write_text(record.instruction, encoding="utf-8")
    used_artifact_ids: set[str] = set()
    used_criteria_names: set[str] = set()
    artifacts = []
    for target in payload["targets"]:
        criteria_name = criteria_filename(target["output_file"], used_criteria_names)
        criteria_file = f"evaluator/criteria/{criteria_name}"
        artifact = {
            "id": artifact_id(target["output_file"], used_artifact_ids),
            "output_file": target["output_file"],
            "artifact_type": target["artifact_type"],
            "criteria_file": criteria_file,
        }
        artifacts.append(artifact)
        target["id"] = artifact["id"]
        target["criteria_file"] = criteria_file
        write_json(task_dir / criteria_file, {"criteria": target["criteria"]})
    write_json(
        evaluator_dir / "llm_judge_artifacts.json",
        {
            "reference_file": "evaluator/llm_judge_references.json",
            "artifacts": artifacts,
        },
    )
    references = payload["references"]
    if references_format == "text":
        (evaluator_dir / "llm_judge_references.json").write_text("\n".join(references) + ("\n" if references else ""), encoding="utf-8")
    else:
        write_json(evaluator_dir / "llm_judge_references.json", {"references": reference_entries(references)})
    if copy_references:
        copy_selected_references(record, payload, task_dir / "references")


def copy_selected_references(record: TaskRecord, payload: dict[str, Any], dest_dir: Path) -> None:
    if not record.source_task_views:
        return
    source_task_view = record.source_task_views[0]
    for rel in payload.get("references", []):
        source = source_task_view / rel
        if not source.is_file():
            continue
        dest = dest_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)


def read_existing_references(path: Path) -> list[str]:
    data = read_json(path)
    if isinstance(data, dict):
        values = data.get("references")
        refs = []
        for item in values if isinstance(values, list) else []:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                refs.append(normalize_reference_path(item["path"]))
            elif isinstance(item, str):
                refs.append(normalize_reference_path(item))
        return dedupe_keep_order(refs)
    if not path.is_file():
        return []
    refs = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            refs.append(normalize_reference_path(line))
    return dedupe_keep_order(refs)


def process_code_rerun(args: argparse.Namespace, record: TaskRecord, api_key: str) -> dict[str, Any] | None:
    task_out = args.out_dir / safe_name(record.task_id, "task")
    evaluator_dir = task_out / "evaluator"
    artifacts_path = evaluator_dir / "llm_judge_artifacts.json"
    if not artifacts_path.is_file():
        return {
            "task_id": record.task_id,
            "task_name": record.task_name,
            "status": "skipped_missing_existing",
            "output_dir": str(task_out),
            "runs": sorted(record.runs),
            "output_files": [],
            "references": [],
            "criteria_count": 0,
            "target_count": 0,
            "code_added": 0,
        }
    artifact_doc = read_json(artifacts_path) or {}
    artifacts = artifact_doc.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    references = read_existing_references(evaluator_dir / "llm_judge_references.json")
    existing_by_output = {
        str(item.get("output_file")): item
        for item in artifacts
        if isinstance(item, dict) and isinstance(item.get("output_file"), str)
    }
    used_artifact_ids = {str(item.get("id")) for item in artifacts if isinstance(item, dict) and item.get("id")}
    used_criteria_names = {
        Path(str(item.get("criteria_file"))).name
        for item in artifacts
        if isinstance(item, dict) and item.get("criteria_file")
    }
    code_outputs = [path for path in record.instruction_outputs if is_code_output(path) and is_supported_output(path)]
    added = 0
    updated = False
    for output_file in code_outputs:
        existing = existing_by_output.get(output_file)
        if existing is not None:
            criteria_file = str(existing.get("criteria_file") or "")
            criteria_path = task_out / criteria_file if criteria_file else None
            if criteria_file and criteria_path and criteria_path.is_file():
                continue
            if not criteria_file:
                criteria_file = f"evaluator/criteria/{criteria_filename(output_file, used_criteria_names)}"
                existing["criteria_file"] = criteria_file
                existing["artifact_type"] = artifact_type(output_file)
                existing.setdefault("id", artifact_id(output_file, used_artifact_ids))
                updated = True
        else:
            criteria_file = f"evaluator/criteria/{criteria_filename(output_file, used_criteria_names)}"
            existing = {
                "id": artifact_id(output_file, used_artifact_ids),
                "output_file": output_file,
                "artifact_type": artifact_type(output_file),
                "criteria_file": criteria_file,
            }
            artifacts.append(existing)
            existing_by_output[output_file] = existing
            updated = True
        criteria = generate_code_criteria(record, output_file, references, args, api_key)
        write_json(task_out / criteria_file, {"criteria": criteria})
        added += 1
    if updated or added:
        artifact_doc["reference_file"] = artifact_doc.get("reference_file") or "evaluator/llm_judge_references.json"
        artifact_doc["artifacts"] = artifacts
        write_json(artifacts_path, artifact_doc)
    return {
        "task_id": record.task_id,
        "task_name": record.task_name,
        "status": "code_rerun",
        "output_dir": str(task_out),
        "runs": sorted(record.runs),
        "output_files": code_outputs,
        "references": references,
        "criteria_count": added,
        "target_count": len(artifacts),
        "code_added": added,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--task-id", action="append", default=[], help="Limit to one task_id. Repeatable.")
    parser.add_argument("--model", default=os.environ.get("DUMATE_LLM_JUDGE_LOCK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1, help="Number of task-level workers for LLM consolidation.")
    parser.add_argument("--max-criteria-per-target", type=int, default=32)
    parser.add_argument("--min-criteria-per-artifact", type=int, default=3, help="Backfill historical criteria when an LLM-merged artifact has fewer criteria than this.")
    parser.add_argument("--references-format", choices=("json", "text"), default="json", help="Write evaluator/llm_judge_references.json as JSON or newline-delimited text.")
    parser.add_argument("--mode", choices=("full", "code-rerun"), default="full", help="full rewrites a task output; code-rerun only appends missing code artifacts/criteria to existing outputs.")
    parser.add_argument("--dry-run", action="store_true", help="Do not call the LLM; write a deterministic heuristic merge.")
    parser.add_argument("--copy-references", action="store_true", help="Copy selected reference files into each output task directory.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing task output directories.")
    return parser.parse_args(argv)


def process_record(args: argparse.Namespace, record: TaskRecord, api_key: str) -> dict[str, Any] | None:
    if args.mode == "code-rerun":
        return process_code_rerun(args, record, api_key)
    task_out = args.out_dir / safe_name(record.task_id, "task")
    if task_out.exists() and args.overwrite:
        shutil.rmtree(task_out)
    if task_out.exists():
        return {
            "task_id": record.task_id,
            "task_name": record.task_name,
            "status": "skipped_existing",
            "output_dir": str(task_out),
            "runs": sorted(record.runs),
            "output_files": [],
            "references": [],
            "criteria_count": 0,
            "target_count": 0,
        }
    if args.dry_run:
        raw_payload = merge_without_llm(record)
    else:
        raw_payload = call_openai_compatible_json(
            build_prompt(record, args.max_criteria_per_target),
            model=args.model,
            base_url=args.base_url,
            api_key=api_key,
            timeout=args.timeout,
            retries=args.retries,
        )
    payload = validate_locked_payload(raw_payload, record, min_criteria_per_artifact=max(0, args.min_criteria_per_artifact))
    fill_code_criteria(payload, record, args, api_key)
    write_task_outputs(record, payload, args.out_dir, copy_references=args.copy_references, references_format=args.references_format)
    return {
        "task_id": record.task_id,
        "task_name": record.task_name,
        "status": "wrote",
        "output_dir": str(task_out),
        "runs": sorted(record.runs),
        "output_files": payload["output_files"],
        "references": payload["references"],
        "criteria_count": len(payload["criteria"]),
        "target_count": len(payload["targets"]),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    task_filter = {canonical_task_id(item) for item in args.task_id} or None
    records = collect_task_records(args.runs_dir, task_filter)
    if not records:
        print(f"no task records found under {args.runs_dir}", file=sys.stderr)
        return 1
    api_key = os.environ.get(args.api_key_env, "")
    if not args.dry_run and not api_key:
        print(f"{args.api_key_env} is required unless --dry-run is used", file=sys.stderr)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    failures: list[tuple[str, BaseException]] = []
    ordered_records = sorted(records.values(), key=lambda item: item.task_id)
    workers = max(1, int(args.workers))
    if workers == 1:
        for record in ordered_records:
            try:
                result = process_record(args, record, api_key)
            except Exception as exc:  # noqa: BLE001 - keep processing other tasks below.
                failures.append((record.task_id, exc))
                print(f"failed {record.task_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            if result:
                summary.append(result)
                print_task_result(result)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_record, args, record, api_key): record for record in ordered_records}
            for future in concurrent.futures.as_completed(futures):
                record = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - report all task failures after worker drain.
                    failures.append((record.task_id, exc))
                    print(f"failed {record.task_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    continue
                if result:
                    summary.append(result)
                    print_task_result(result)
    summary.sort(key=lambda item: item["task_id"])
    write_json(args.out_dir / "summary.json", summary)
    return 1 if failures else 0


def print_task_result(result: dict[str, Any]) -> None:
    if result.get("status") == "skipped_existing":
        print(f"skip existing {result['output_dir']}; pass --overwrite to regenerate")
        return
    if result.get("status") == "skipped_missing_existing":
        print(f"skip missing existing lock output {result['output_dir']} for code-rerun")
        return
    if result.get("status") == "code_rerun":
        print(f"code-rerun {result['output_dir']} (added {result.get('code_added', 0)} code criteria files)")
        return
    print(f"wrote {result['output_dir']} ({result['target_count']} targets, {result['criteria_count']} criteria)")


if __name__ == "__main__":
    raise SystemExit(main())
