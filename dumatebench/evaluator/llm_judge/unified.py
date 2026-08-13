"""Unified checklist + LLM-as-judge scoring helpers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .artifacts import collect_artifacts
from .ppt import DEFAULT_JUDGE_OUTPUT as DEFAULT_PPT_JUDGE_OUTPUT
from .ppt import run_pptx_judge
from .reference_selection import materialize_selected_references
from .runner import JudgeRunner, read_json, write_json
from .schema import normalize_rubric as normalize_text_rubric
from .schema import stable_hash as text_stable_hash

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = "run_outputs/llm_judge_score.json"
TEXTUAL_TYPES = {"doc", "docx", "txt", "md", "json", "html", "htm"}
PPT_TYPES = {"ppt", "pptx"}
EXCEL_TYPES = {"xls", "xlsx", "xlsm", "xltx", "xltm"}
PDF_TYPES = {"pdf"}
IMAGE_TYPES = {"png", "jpg", "jpeg", "webp"}
MULTIMODAL_TYPES = {"mp3", "wav", "m4a", "flac", "aac", "ogg", "mp4", "mov", "webm", "mkv"}
CODE_TYPES = {
    "py", "js", "jsx", "ts", "tsx", "java", "go", "rs", "cpp", "cc", "cxx", "c",
    "h", "hpp", "cs", "rb", "php", "swift", "kt", "kts", "scala", "sh", "bash",
    "zsh", "ps1", "sql", "r", "lua", "pl", "pm", "dart", "ex", "exs", "erl",
    "hrl", "clj", "cljs", "fs", "fsx", "jl", "nim", "zig", "vue", "svelte",
    "astro", "xml", "yml", "yaml", "properties", "gradle", "code",
}
TEXTUAL_SUFFIXES = {f".{item}" for item in TEXTUAL_TYPES}
PPT_SUFFIXES = {f".{item}" for item in PPT_TYPES}
EXCEL_SUFFIXES = {f".{item}" for item in EXCEL_TYPES}
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {f".{item}" for item in IMAGE_TYPES}
MULTIMODAL_SUFFIXES = {f".{item}" for item in MULTIMODAL_TYPES} | {".txt", ".md", ".json", ".docx", ".html", ".htm", ".yaml", ".yml"}
CODE_SUFFIXES = {f".{item}" for item in CODE_TYPES if item != "code"}


_TRANSIENT_PROVIDER_MARKERS = (
    "502",
    "503",
    "504",
    "bad gateway",
    "gateway time-out",
    "gateway timeout",
    "service unavailable",
    "internalservererror",
    "internal server error",
    "server error",
    "overloaded",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "timeout",
    "timed out",
)

DEFAULT_LOCKED_RUBRIC_FILES = (
    "evaluator/llm_judge_criteria.json",
    "evaluator/llm_judge_rubric.json",
)


@contextmanager
def _single_artifact_directory(output_path: Path) -> Iterator[Path]:
    """Expose exactly one candidate file to directory-oriented judge backends."""
    output_path = output_path.resolve()
    if not output_path.is_file():
        raise ValueError(f"LLM judge output_file must be a regular file: {output_path}")
    with tempfile.TemporaryDirectory(
        prefix=".llm_judge_artifact_",
        dir=str(output_path.parent),
    ) as directory:
        scoped_dir = Path(directory)
        scoped_file = scoped_dir / output_path.name
        try:
            os.link(output_path, scoped_file)
        except OSError:
            shutil.copy2(output_path, scoped_file)
        yield scoped_dir


def _is_transient_provider_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and status >= 500:
        return True
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in message for marker in _TRANSIENT_PROVIDER_MARKERS)


def _tag_summary_evidence_paths(value: Any, scope: str) -> Any:
    """Copy an evidence summary while tagging relative evidence paths by scope."""
    if isinstance(value, list):
        return [_tag_summary_evidence_paths(item, scope) for item in value]
    if not isinstance(value, dict):
        return value
    tagged: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"path", "relative_path"} and isinstance(item, str) and item:
            if item.startswith(("outputs/", "references/")):
                tagged[key] = item
            else:
                path = Path(item)
                display = path.name if path.is_absolute() else item.lstrip("/")
                tagged[key] = f"{scope}/{display}"
        else:
            tagged[key] = _tag_summary_evidence_paths(item, scope)
    return tagged


def _parse_json_object_response(content: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating provider-added prose or Markdown fences."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as direct_error:
        decoder = json.JSONDecoder()
        for index, character in enumerate(content):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(content[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        preview = content[:300].replace("\n", "\\n")
        raise RuntimeError(f"LLM judge returned invalid JSON; response starts with: {preview!r}") from direct_error
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM judge returned non-object JSON")
    return parsed


class _OpenAIJsonClient:
    def __init__(self, model: str, base_url: str | None = None) -> None:
        self.model = model
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self.max_tokens = int(os.environ.get("DUMATE_LLM_JUDGE_MAX_TOKENS", "16000"))
        self.retries = int(os.environ.get("DUMATE_LLM_JUDGE_RETRIES", "5"))

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai>=1.x is required to call the LLM judge") from exc

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required to call the LLM judge")
        client = OpenAI(api_key=api_key, base_url=self.base_url, max_retries=0)
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request_messages = messages
                if attempt > 0:
                    request_messages = messages + [
                        {
                            "role": "user",
                            "content": "The previous judge response was empty or invalid JSON. Return only one valid JSON object matching the requested schema.",
                        }
                    ]
                response = client.chat.completions.create(
                    model=self.model,
                    messages=request_messages,
                    temperature=0,
                    max_tokens=self.max_tokens,
                    response_format=response_format or {"type": "json_object"},
                )
                content = response.choices[0].message.content
                if isinstance(content, list):
                    content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
                content = str(content or "").strip()
                if not content:
                    raise RuntimeError("LLM judge returned empty content")
                return _parse_json_object_response(content)
            except (KeyError, IndexError, json.JSONDecodeError, RuntimeError, ConnectionError, TimeoutError) as exc:
                last_exc = exc
                if attempt < self.retries:
                    logger.warning("LLM judge JSON response failed on attempt %s/%s: %s", attempt + 1, self.retries + 1, exc)
                    time.sleep(1.5 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 - retry transient provider/server errors (5xx, gateway, connection)
                if not _is_transient_provider_error(exc):
                    raise
                last_exc = exc
                if attempt < self.retries:
                    logger.warning("LLM judge provider error on attempt %s/%s: %s", attempt + 1, self.retries + 1, exc)
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM judge JSON request failed after {self.retries + 1} attempts: {last_exc}") from last_exc


class _MockJsonClient:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del messages
        del response_format
        del attachments
        if not self.payloads:
            raise RuntimeError("mock JSON client has no remaining payloads")
        return self.payloads.pop(0)


def _task_path(testbed_dir: str | os.PathLike[str], file_path: str | os.PathLike[str]) -> Path:
    path = Path(file_path)
    if path.is_absolute():
        return path
    return Path(testbed_dir) / path


def _load_locked_rubric_raw(task_dir: Path, args: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("criteria", "task_rubrics", "rubric"):
        value = args.get(key)
        if isinstance(value, list):
            return {"criteria": value}
        if isinstance(value, dict):
            return value
        if key == "rubric" and value:
            return read_json(_task_path(task_dir, value))

    for key in ("criteria_file", "rubric_file", "llm_judge_criteria_file", "llm_judge_rubric_file"):
        value = args.get(key)
        if value:
            return read_json(_task_path(task_dir, value))

    for rel in DEFAULT_LOCKED_RUBRIC_FILES:
        path = _task_path(task_dir, rel)
        if path.is_file():
            return read_json(path)
    return None


def _criteria_from_locked_rubric(raw: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("criteria"), list):
        return [item for item in raw["criteria"] if isinstance(item, dict)]
    if isinstance(raw.get("task_rubrics"), list):
        return [item for item in raw["task_rubrics"] if isinstance(item, dict)]
    rubric = raw.get("rubric")
    if isinstance(rubric, dict):
        return _criteria_from_locked_rubric(rubric)
    return None


def _as_criteria_rubric(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw.get("criteria"), list):
        return raw
    criteria = _criteria_from_locked_rubric(raw)
    if criteria is None:
        return raw
    return {"criteria": criteria}


def infer_artifact_type(output_file: str | os.PathLike[str], args: dict[str, Any] | None = None) -> str:
    doc_type = str((args or {}).get("artifact_type") or (args or {}).get("doc_type") or "").lower().lstrip(".")
    if doc_type:
        return doc_type
    return Path(output_file).suffix.lower().lstrip(".")


def normalize_unit_score(value: Any, *, assume_percent_above_one: bool = True) -> float:
    if value is None:
        return 0.0
    score = float(value)
    if assume_percent_above_one and score > 1.0:
        score /= 100.0
    return max(0.0, min(1.0, score))


def merge_unit_scores(
    complete_pass: float | None,
    checklist_score: float | None,
    judge_score: float | None,
) -> float:
    from ..scoring import final_score

    return final_score(complete_pass, checklist_score, judge_score)


def _run_textual_judge(
    testbed_dir: str | os.PathLike[str],
    args: dict[str, Any],
    rule_result: dict[str, Any],
) -> dict[str, Any]:
    task_dir = Path(testbed_dir)
    output_file = args["output_file"]
    output_path = _task_path(task_dir, output_file)
    outputs_dir = _task_path(task_dir, args.get("outputs_dir", output_path.parent))
    instruction_path = _task_path(task_dir, args.get("instruction_file", "instruction.md"))
    instruction = instruction_path.read_text(encoding="utf-8", errors="ignore") if instruction_path.is_file() else ""
    if not instruction:
        instruction = str(args.get("instruction", ""))

    try:
        reference_dir = _selected_reference_dir(task_dir, args, suffixes=None, label="textual")
        max_files = int(args.get("max_files", 20))
        total_chars = int(args.get("total_chars", 60000))
        artifacts = collect_artifacts(outputs_dir, max_files=max_files, total_char_limit=total_chars)
        references = (
            collect_artifacts(reference_dir, max_files=max_files, total_char_limit=total_chars)
            if reference_dir and reference_dir.is_dir()
            else []
        )

        locked_rubric = _as_criteria_rubric(_load_locked_rubric_raw(task_dir, args))
        if locked_rubric is not None:
            rubric = normalize_text_rubric(
                locked_rubric,
                task_id=str(args.get("task_id") or task_dir.name),
                instruction_hash=text_stable_hash(instruction),
            )
        else:
            mock_rubric = args.get("mock_rubric")
            if not isinstance(mock_rubric, dict):
                mock_rubric = None
            client_for_rubric = _MockJsonClient([mock_rubric]) if mock_rubric else _OpenAIJsonClient(
                model=args.get("model", "gpt-4o-mini"),
                base_url=args.get("base_url"),
            )
            rubric = JudgeRunner(client_for_rubric).generate_rubric(
                task_id=str(args.get("task_id") or task_dir.name),
                instruction=instruction,
                objective_checks=json.dumps(rule_result.get("checks", []), ensure_ascii=False),
                references=references,
            )

        mock_judgments = args.get("mock_judgments")
        if mock_judgments is None and args.get("mock_judgment") is not None:
            mock_judgments = [args["mock_judgment"]]
        if isinstance(mock_judgments, list):
            client = _MockJsonClient([item for item in mock_judgments if isinstance(item, dict)])
        else:
            client = _OpenAIJsonClient(model=args.get("model", "gpt-4o-mini"), base_url=args.get("base_url"))

        result = JudgeRunner(client).evaluate(
            instruction=instruction,
            rubric=rubric,
            artifacts=artifacts,
            references=references,
            rule_result=None,
            judge_runs=int(args.get("judge_runs", 1)),
            rule_weight=0.0,
        )
        result["reference_selection"] = args.get("_reference_selection")
        result["judge_score"] = normalize_unit_score(result.get("judge_score_conservative", 0.0))
        result.setdefault("status", "ok")
        result.setdefault("reason", "Unified textual LLM judge completed.")
        return result
    except Exception as exc:
        return _failure_report("textual_llm_judge", f"Textual LLM judge failed: {type(exc).__name__}: {exc}", args.get("model", "gpt-4o-mini"), outputs_dir)


def _run_code_judge(
    testbed_dir: str | os.PathLike[str],
    args: dict[str, Any],
    rule_result: dict[str, Any],
) -> dict[str, Any]:
    task_dir = Path(testbed_dir)
    output_file = args["output_file"]
    output_path = _task_path(task_dir, output_file)
    outputs_dir = _task_path(task_dir, args.get("outputs_dir", output_path.parent))
    instruction_path = _task_path(task_dir, args.get("instruction_file", "instruction.md"))
    instruction = instruction_path.read_text(encoding="utf-8", errors="ignore") if instruction_path.is_file() else ""
    if not instruction:
        instruction = str(args.get("instruction", ""))

    try:
        from ..llm_judge_code.artifacts import collect_artifacts as collect_code_artifacts
        from ..llm_judge_code.runner import CodeJudgeRunner
        from ..llm_judge_code.schema import normalize_rubric as normalize_code_rubric
        from ..llm_judge_code.schema import stable_hash as code_stable_hash

        reference_dir = _selected_reference_dir(task_dir, args, suffixes=None, label="code")
        max_files = int(args.get("max_files", 40))
        total_chars = int(args.get("total_chars", 120000))
        artifacts = collect_code_artifacts(outputs_dir, max_files=max_files, total_char_limit=total_chars)
        references = (
            collect_code_artifacts(reference_dir, max_files=max_files, total_char_limit=total_chars)
            if reference_dir and reference_dir.is_dir()
            else []
        )

        locked_rubric = _as_criteria_rubric(_load_locked_rubric_raw(task_dir, args))
        if locked_rubric is not None:
            rubric = normalize_code_rubric(
                locked_rubric,
                task_id=str(args.get("task_id") or task_dir.name),
                instruction_hash=code_stable_hash(instruction),
            )
        else:
            mock_rubric = args.get("mock_code_rubric")
            if not isinstance(mock_rubric, dict):
                mock_rubric = None
            client_for_rubric = _MockJsonClient([mock_rubric]) if mock_rubric else _OpenAIJsonClient(
                model=args.get("model", "gpt-4o-mini"),
                base_url=args.get("base_url"),
            )
            rubric = CodeJudgeRunner(client_for_rubric).generate_rubric(
                task_id=str(args.get("task_id") or task_dir.name),
                instruction=instruction,
                objective_checks=json.dumps(rule_result.get("checks", []), ensure_ascii=False),
                references=references,
            )

        mock_judgments = args.get("mock_code_judgments")
        if mock_judgments is None and args.get("mock_code_judgment") is not None:
            mock_judgments = [args["mock_code_judgment"]]
        if isinstance(mock_judgments, list):
            client = _MockJsonClient([item for item in mock_judgments if isinstance(item, dict)])
        else:
            client = _OpenAIJsonClient(model=args.get("model", "gpt-4o-mini"), base_url=args.get("base_url"))

        result = CodeJudgeRunner(client).evaluate(
            instruction=instruction,
            rubric=rubric,
            artifacts=artifacts,
            references=references,
            rule_result=None,
            judge_runs=int(args.get("judge_runs", 1)),
            rule_weight=0.0,
        )
        result["reference_selection"] = args.get("_reference_selection")
        result["judge_score"] = normalize_unit_score(result.get("judge_score_conservative", 0.0))
        result.setdefault("status", "ok")
        result.setdefault("reason", "Unified code LLM judge completed.")
        return {
            "judge_type": "code_llm_judge",
            "model": args.get("model", "gpt-4o-mini"),
            "artifact_dir": str(outputs_dir),
            "rubric": rubric,
            "result": result,
            "reference_selection": args.get("_reference_selection"),
            "judge_score": result["judge_score"],
            "status": result["status"],
            "reason": result["reason"],
        }
    except Exception as exc:
        return _failure_report("code_llm_judge", f"Code LLM judge failed: {type(exc).__name__}: {exc}", args.get("model", "gpt-4o-mini"), outputs_dir)


def _run_ppt_judge(testbed_dir: str | os.PathLike[str], args: dict[str, Any]) -> dict[str, Any]:
    task_dir = Path(testbed_dir)
    reference_dir = _selected_reference_dir(task_dir, args, suffixes=None, label="ppt")
    locked_rubrics = _criteria_from_locked_rubric(_load_locked_rubric_raw(task_dir, args))
    report = run_pptx_judge(
        testbed_dir,
        instruction_file=args.get("instruction_file", "instruction.md"),
        # Reference evidence must come exclusively from the selected/reference manifest.
        # prepare_evidence will select an input PPT from this materialized directory.
        input_file=None,
        output_file=args["output_file"],
        reference_dir=str(reference_dir) if reference_dir is not None else None,
        reference_summary=_reference_summary(task_dir, args, reference_dir=reference_dir),
        model=args.get("model"),
        min_score=float(args.get("min_score", 70.0)),
        judge_output_file=args.get("ppt_judge_output_file", DEFAULT_PPT_JUDGE_OUTPUT),
        render_slides=bool(args.get("render_slides", True)),
        max_rendered_slides=int(args.get("max_rendered_slides", 8)),
        locked_rubrics=locked_rubrics,
        mock_response=args.get("mock_response"),
    )
    report["reference_selection"] = args.get("_reference_selection")
    score = report.get("result", {}).get("score")
    judge_score = (
        normalize_unit_score(score)
        if report.get("status", "ok") == "ok" and score is not None
        else None
    )
    return {**report, "judge_score": judge_score}


def _excel_failure_report(message: str, model: str, artifact_dir: Path) -> dict[str, Any]:
    provider_unavailable = _is_judge_provider_unavailable(message)
    result = {
        "checklist_deduplication": {"covered_by_checklist": [], "excluded_from_rubric": []},
        "task_rubrics": [],
        "criteria_results": [],
        "check_results": [],
        "dimension_scores": {},
        "overall_score": None if provider_unavailable else 0.0,
        "verdict": "unavailable" if provider_unavailable else "fail",
        "failure_modes": [message],
        "recommendations": ["Inspect judge API availability and retry the LLM judge when the provider is healthy."],
    }
    return {
        "judge_type": "excel_llm_judge",
        "model": model,
        "artifact_dir": str(artifact_dir),
        "status": "skipped_unavailable" if provider_unavailable else "failed",
        "reason": message,
        "result": result,
        "judge_score": None if provider_unavailable else 0.0,
    }


def _is_judge_provider_unavailable(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "llm judge json request failed",
        "llm request failed",
        "empty content",
        "expecting value",
        "provider returned an html error page",
        "provider http error",
    ) + _TRANSIENT_PROVIDER_MARKERS
    return any(marker in lowered for marker in markers)


def _failure_report(judge_type: str, message: str, model: str, artifact_dir: Path) -> dict[str, Any]:
    provider_unavailable = _is_judge_provider_unavailable(message)
    status = "skipped_unavailable" if provider_unavailable else "failed"
    result = {
        "judge_score_conservative": None if provider_unavailable else 0.0,
        "assessment_coverage": 0.0,
        "needs_human_review": True,
        "failure_modes": [message],
    }
    return {
        "judge_type": judge_type,
        "model": model,
        "artifact_dir": str(artifact_dir),
        "status": status,
        "reason": message,
        "result": result,
        "judge_score": None if provider_unavailable else 0.0,
    }


def _selected_reference_dir(
    task_dir: Path,
    args: dict[str, Any],
    *,
    suffixes: set[str] | None,
    label: str,
) -> Path | None:
    reference_dir = _reference_dir(task_dir, args)
    if not bool(args.get("select_reference_files", True)):
        return reference_dir
    selected_dir, report = materialize_selected_references(
        task_dir=task_dir,
        reference_dir=reference_dir,
        output_file=args.get("output_file"),
        suffixes=suffixes,
        label=label,
        max_files=int(args.get("max_reference_files", args.get("max_files", 20))),
        instruction_file=str(args.get("instruction_file", "instruction.md")),
    )
    selections = args.setdefault("_reference_selections", {})
    if isinstance(selections, dict):
        selections[label] = report
    args["_reference_selection"] = report
    return selected_dir


def _reference_summary(task_dir: Path, args: dict[str, Any], reference_dir: Path | None = None) -> dict[str, Any]:
    """Collect bounded workspace/reference evidence for judges without native reference readers."""
    if reference_dir is None:
        reference_dir = _reference_dir(task_dir, args)
    if reference_dir is None:
        return {"status": "disabled", "reference_dir": None, "text_artifacts": [], "file_inventory": []}
    summary: dict[str, Any] = {
        "status": "ok" if reference_dir.is_dir() else "missing",
        "reference_dir": str(reference_dir),
        "text_artifacts": [],
        "file_inventory": [],
        "errors": [],
        "limits": {
            "max_reference_files": int(args.get("max_reference_files", args.get("max_files", 20))),
            "max_reference_chars": int(args.get("max_reference_chars", args.get("total_chars", 60000))),
            "max_reference_inventory_files": int(args.get("max_reference_inventory_files", 100)),
        },
    }
    if not reference_dir.is_dir():
        return summary

    max_files = int(summary["limits"]["max_reference_files"])
    total_chars = int(summary["limits"]["max_reference_chars"])
    try:
        summary["text_artifacts"] = collect_artifacts(reference_dir, max_files=max_files, total_char_limit=total_chars)
    except Exception as exc:
        summary["errors"].append(f"text artifact collection failed: {type(exc).__name__}: {exc}")

    inventory_limit = int(summary["limits"]["max_reference_inventory_files"])
    files = [
        path
        for path in sorted(reference_dir.rglob("*"))
        if path.is_file()
        and not any(part.startswith(".") or part == "uploads_raw" for part in path.relative_to(reference_dir).parts)
    ]
    summary["file_inventory"] = [
        {
            "path": path.relative_to(reference_dir).as_posix(),
            "suffix": path.suffix.lower(),
            "size_bytes": path.stat().st_size,
        }
        for path in files[:inventory_limit]
    ]
    summary["omitted_file_count"] = max(0, len(files) - inventory_limit)
    _add_typed_reference_evidence(summary, reference_dir, args)
    return summary


def _add_typed_reference_evidence(summary: dict[str, Any], reference_dir: Path, args: dict[str, Any]) -> None:
    """Attach best-effort parsed evidence for non-text reference files."""
    try:
        from .ppt import summarize_reference_ppts

        summary["ppt_artifacts"] = summarize_reference_ppts(
            reference_dir,
            max_files=int(args.get("max_reference_files", args.get("max_files", 20))),
        )
    except Exception as exc:
        summary["errors"].append(f"PPT reference parsing failed: {type(exc).__name__}: {exc}")

    try:
        from ..excel_llm_judge.excel_judge.artifact_summary import summarize_artifacts

        summary["excel_artifacts"] = summarize_artifacts(
            reference_dir,
            max_files=int(args.get("max_reference_files", args.get("max_files", 20))),
        )
    except Exception as exc:
        summary["errors"].append(f"Excel reference parsing failed: {type(exc).__name__}: {exc}")

    try:
        from ..llm_judge_pdf.artifacts import artifact_inventory as pdf_artifact_inventory
        from ..llm_judge_pdf.artifacts import collect_artifacts as collect_pdf_artifacts

        with collect_pdf_artifacts(
            reference_dir,
            max_files=int(args.get("max_reference_files", args.get("max_files", 20))),
            max_pages=int(args.get("max_reference_pdf_pages", args.get("max_pages", 4))),
            total_chars=int(args.get("max_reference_chars", args.get("total_chars", 60000))),
            require_pdf=False,
        ) as (pdf_artifacts, pdf_errors):
            summary["pdf_artifacts"] = pdf_artifact_inventory(pdf_artifacts)
            for artifact in pdf_artifacts:
                if artifact.get("text"):
                    summary.setdefault("pdf_text_artifacts", []).append(
                        {
                            "path": artifact.get("path"),
                            "suffix": artifact.get("suffix"),
                            "size_bytes": artifact.get("size_bytes"),
                            "text": artifact.get("text"),
                            "text_truncated": artifact.get("text_truncated"),
                            "page_count": artifact.get("page_count"),
                        }
                    )
            summary["errors"].extend(pdf_errors)
    except Exception as exc:
        summary["errors"].append(f"PDF reference parsing failed: {type(exc).__name__}: {exc}")


def _run_excel_judge(
    testbed_dir: str | os.PathLike[str],
    args: dict[str, Any],
    rule_result: dict[str, Any],
) -> dict[str, Any]:
    task_dir = Path(testbed_dir)
    output_file = args["output_file"]
    output_path = _task_path(task_dir, output_file)
    artifact_dir = _task_path(task_dir, args.get("artifact_dir") or args.get("outputs_dir") or output_path.parent)
    instruction_path = _task_path(task_dir, args.get("instruction_file", "instruction.md"))
    instruction = instruction_path.read_text(encoding="utf-8", errors="ignore") if instruction_path.is_file() else ""
    if not instruction:
        instruction = str(args.get("instruction", ""))
    checklist = args.get("checklist")
    if checklist is None:
        checklist = json.dumps(rule_result.get("checks", []), ensure_ascii=False, indent=2)
    else:
        checklist_path = _task_path(task_dir, checklist)
        checklist = checklist_path.read_text(encoding="utf-8", errors="ignore") if checklist_path.is_file() else str(checklist)

    out_dir = _task_path(task_dir, args.get("excel_judge_output_dir", "run_outputs/excel_llm_judge"))
    out_dir.mkdir(parents=True, exist_ok=True)
    model = args.get("model") or os.environ.get("OPENAI_MODEL") or "gpt-4o"

    try:
        from ..excel_llm_judge.excel_judge.artifact_summary import summarize_artifacts
        from ..excel_llm_judge.excel_judge.prompt import SYSTEM_PROMPT, build_user_prompt
        from ..excel_llm_judge.excel_llm_judge import _call_openai_compatible, _parse_json_object, normalize_judge_result, render_markdown_report
    except ImportError as exc:
        report = _excel_failure_report(f"Excel LLM judge import failed: {exc}", model, artifact_dir)
    else:
        try:
            artifact_summary = summarize_artifacts(artifact_dir)
            reference_dir = _selected_reference_dir(task_dir, args, suffixes=None, label="excel")
            if reference_dir is not None and reference_dir.is_dir():
                reference_artifact_summary = summarize_artifacts(
                    reference_dir,
                    max_files=int(args.get("max_reference_files", args.get("max_files", 80))),
                )
            else:
                reference_artifact_summary = {
                    "artifact_dir": str(reference_dir) if reference_dir is not None else None,
                    "status": "disabled" if reference_dir is None else "missing",
                    "file_inventory": [],
                    "excel_file_count": 0,
                    "workbooks": [],
                }
            reference_summary = _reference_summary(task_dir, args, reference_dir=reference_dir)
            locked_rubrics = _criteria_from_locked_rubric(_load_locked_rubric_raw(task_dir, args))
            user_prompt = build_user_prompt(
                instruction,
                str(checklist),
                _tag_summary_evidence_paths(artifact_summary, "outputs"),
                _tag_summary_evidence_paths(reference_summary, "references"),
                _tag_summary_evidence_paths(reference_artifact_summary, "references"),
                locked_rubrics=locked_rubrics,
            )
            judge_input = {
                "system_prompt": SYSTEM_PROMPT,
                "user_prompt": json.loads(user_prompt),
                "runtime": {
                    "provider": "openai_compatible",
                    "base_url": args.get("base_url") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                    "model": model,
                    "api_key_env": args.get("api_key_env", "OPENAI_API_KEY"),
                    "temperature": float(args.get("temperature", 0.0)),
                    "max_tokens": int(args.get("max_tokens", 8000)),
                    "dry_run": bool(args.get("dry_run", False)),
                },
            }
            mock_result = args.get("mock_excel_result")
            if isinstance(mock_result, dict):
                result = mock_result
                raw_text = json.dumps(result, ensure_ascii=False, indent=2)
                elapsed_ms = 0
                dry_run = False
            elif args.get("dry_run"):
                from ..excel_llm_judge.excel_judge.prompt import build_dry_run_result

                result = build_dry_run_result()
                raw_text = json.dumps(result, ensure_ascii=False, indent=2)
                elapsed_ms = 0
                dry_run = True
            else:
                import time

                started = time.time()
                raw_text = _call_openai_compatible(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    base_url=str(judge_input["runtime"]["base_url"]),
                    model=model,
                    api_key_env=str(judge_input["runtime"]["api_key_env"]),
                    temperature=float(judge_input["runtime"]["temperature"]),
                    max_tokens=int(judge_input["runtime"]["max_tokens"]),
                    timeout=float(args.get("timeout", 120.0)),
                    retries=int(args.get("retries", 5)),
                )
                elapsed_ms = int((time.time() - started) * 1000)
                result = _parse_json_object(raw_text)
                dry_run = False
            result = normalize_judge_result(result, locked_rubrics=locked_rubrics)
            score = normalize_unit_score(result.get("overall_score", 0.0))
            report = {
                "judge_type": "excel_llm_judge",
                "model": model,
                "elapsed_ms": elapsed_ms,
                "dry_run": dry_run,
                "artifact_dir": str(artifact_dir),
                "result": result,
                "raw_text": raw_text,
                "artifact_summary": artifact_summary,
                "reference_summary": reference_summary,
                "reference_artifact_summary": reference_artifact_summary,
                "reference_selection": args.get("_reference_selection"),
                "judge_score": score,
            }
            (out_dir / "judge_input.json").write_text(json.dumps(judge_input, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (out_dir / "judge_report.md").write_text(
                render_markdown_report(
                    instruction=instruction,
                    checklist=str(checklist),
                    artifact_summary=artifact_summary,
                    result=result,
                    dry_run=dry_run,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            report = _excel_failure_report(f"Excel LLM judge failed: {type(exc).__name__}: {exc}", model, artifact_dir)

    (out_dir / "judge_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _reference_dir(task_dir: Path, args: dict[str, Any]) -> Path | None:
    reference_file_dir = _reference_dir_from_file(task_dir, args)
    if reference_file_dir is not None:
        return reference_file_dir
    reference_dir_arg = args.get("reference_dir", "workspace_seed")
    if reference_dir_arg in {None, "-"}:
        return None
    primary = _task_path(task_dir, reference_dir_arg)
    web_reference_arg = args.get("web_reference_dir", "web_reference")
    include_web_reference = bool(args.get("include_web_reference", True))
    web_reference = None if web_reference_arg in {None, "-"} else _task_path(task_dir, web_reference_arg)
    if not include_web_reference or web_reference is None or not web_reference.is_dir():
        return primary
    if not primary.is_dir():
        return web_reference

    merged = task_dir / ".llm_judge_references" / "merged"
    if merged.exists():
        shutil.rmtree(merged)
    _copy_reference_tree(primary, merged / Path(reference_dir_arg).name)
    _copy_reference_tree(web_reference, merged / Path(web_reference_arg).name)
    return merged


def _reference_dir_from_file(task_dir: Path, args: dict[str, Any]) -> Path | None:
    reference_file_arg = (
        args.get("reference_file")
        or args.get("references_file")
        or args.get("reference_manifest")
        or args.get("references_manifest")
    )
    if reference_file_arg in {None, ""}:
        default_manifest = task_dir / "evaluator" / "llm_judge_references.json"
        if not default_manifest.is_file():
            return None
        reference_file_arg = default_manifest
    elif reference_file_arg == "-":
        return None
    manifest_path = _task_path(task_dir, reference_file_arg)
    references = _read_reference_manifest(manifest_path)
    target_root = task_dir / ".llm_judge_references" / "from_manifest"
    if target_root.exists():
        shutil.rmtree(target_root)
    target_root.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(references, start=1):
        source = _task_path(task_dir, item["path"])
        if not source.is_file():
            raise FileNotFoundError(f"Reference manifest entry does not exist or is not a file: {source}")
        rel = Path(item.get("as") or item.get("name") or item["path"])
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            rel = Path(f"reference_{index}") / source.name
        destination = target_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return target_root


def _read_reference_manifest(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        values: Any = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        if isinstance(raw, dict):
            values = None
            for key in ("references", "reference_files", "files"):
                if key in raw:
                    values = raw[key]
                    break
        else:
            values = raw
    if not isinstance(values, list):
        raise ValueError(f"reference manifest must be a list or contain references/reference_files/files: {path}")
    references: list[dict[str, str]] = []
    for index, item in enumerate(values, start=1):
        if isinstance(item, str):
            references.append({"path": item})
        elif isinstance(item, dict):
            ref_path = str(item.get("path") or item.get("file") or item.get("reference_file") or "").strip()
            if not ref_path:
                raise ValueError(f"reference manifest item #{index} is missing path/file/reference_file")
            entry = {"path": ref_path}
            if item.get("as") not in (None, ""):
                entry["as"] = str(item["as"])
            if item.get("name") not in (None, ""):
                entry["name"] = str(item["name"])
            references.append(entry)
        else:
            raise ValueError(f"reference manifest item #{index} must be a string or object")
    return references


def _copy_reference_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        if any(part.startswith(".") or part == "uploads_raw" for part in rel.parts):
            continue
        if path.name in {"annotation_review.json"}:
            continue
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _run_pdf_judge(
    testbed_dir: str | os.PathLike[str],
    args: dict[str, Any],
    rule_result: dict[str, Any],
) -> dict[str, Any]:
    task_dir = Path(testbed_dir)
    output_file = args["output_file"]
    output_path = _task_path(task_dir, output_file)
    outputs_dir = _task_path(task_dir, args.get("outputs_dir", output_path.parent))
    reference_dir = _selected_reference_dir(task_dir, args, suffixes=None, label="pdf")
    out_dir = _task_path(task_dir, args.get("pdf_judge_output_dir", "run_outputs/pdf_llm_judge"))
    out_dir.mkdir(parents=True, exist_ok=True)
    model = args.get("model") or os.environ.get("OPENAI_MODEL") or "gpt-4o"

    try:
        from ..llm_judge_pdf.runner import JudgeRunner as PdfJudgeRunner
        from ..llm_judge_pdf.runner import load_task_inputs, reference_inventory, write_json
        from ..llm_judge_pdf.schema import normalize_rubric as normalize_pdf_rubric
        from ..llm_judge_pdf.schema import stable_hash as pdf_stable_hash
    except ImportError as exc:
        report = _failure_report("pdf_llm_judge", f"PDF LLM judge import failed: {exc}", model, outputs_dir)
    else:
        try:
            task_id, instruction, objective_checks = load_task_inputs(task_dir)
            locked_rubric = _as_criteria_rubric(_load_locked_rubric_raw(task_dir, args))
            mock_rubric = args.get("mock_pdf_rubric")
            mock_judgment = args.get("mock_pdf_judgment")
            payloads = [item for item in (() if locked_rubric is not None else (mock_rubric,)) + (mock_judgment,) if isinstance(item, dict)]
            client = _MockJsonClient(payloads) if payloads else _OpenAIJsonClient(model=model, base_url=args.get("base_url"))
            runner = PdfJudgeRunner(client)
            if locked_rubric is not None:
                rubric = normalize_pdf_rubric(
                    locked_rubric,
                    task_id=task_id,
                    instruction_hash=pdf_stable_hash(instruction),
                )
                result = runner.evaluate(
                    instruction=instruction,
                    rubric=rubric,
                    outputs_dir=outputs_dir,
                    reference_dir=reference_dir,
                    rule_result=rule_result,
                    judge_runs=int(args.get("judge_runs", 1)),
                    rule_weight=0.0,
                    max_files=int(args.get("max_files", 20)),
                    max_pages=int(args.get("max_pages", 12)),
                    total_chars=int(args.get("total_chars", 80000)),
                    model=model,
                )
            else:
                rubric, result = runner.run(
                task_id=task_id,
                instruction=instruction,
                objective_checks=objective_checks,
                outputs_dir=outputs_dir,
                references=reference_inventory(reference_dir, max_files=int(args.get("max_files", 20))),
                reference_dir=reference_dir,
                rule_result=rule_result,
                judge_runs=int(args.get("judge_runs", 1)),
                rule_weight=0.0,
                max_files=int(args.get("max_files", 20)),
                max_pages=int(args.get("max_pages", 12)),
                total_chars=int(args.get("total_chars", 80000)),
                model=model,
                )
            write_json(out_dir / "rubric.json", rubric)
            write_json(out_dir / "judge_result.json", result)
            report = {
                "judge_type": "pdf_llm_judge",
                "model": model,
                "artifact_dir": str(outputs_dir),
                "rubric": rubric,
                "result": result,
                "reference_selection": args.get("_reference_selection"),
                "judge_score": normalize_unit_score(
                    result.get("aggregate", {}).get("judge_score_conservative", 0.0)
                ),
            }
        except Exception as exc:
            report = _failure_report("pdf_llm_judge", f"PDF LLM judge failed: {type(exc).__name__}: {exc}", model, outputs_dir)
            (out_dir / "judge_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _run_image_judge(
    testbed_dir: str | os.PathLike[str],
    args: dict[str, Any],
    rule_result: dict[str, Any],
) -> dict[str, Any]:
    del rule_result
    task_dir = Path(testbed_dir)
    output_file = args["output_file"]
    output_path = _task_path(task_dir, output_file)
    candidate_dir = _task_path(task_dir, args.get("outputs_dir", output_path.parent))
    reference_dir = _selected_reference_dir(task_dir, args, suffixes=None, label="image")
    out_dir = _task_path(task_dir, args.get("image_judge_output_dir", "run_outputs/image_llm_judge"))
    out_dir.mkdir(parents=True, exist_ok=True)
    model = args.get("model") or os.environ.get("OPENAI_MODEL") or "gpt-4o"

    try:
        from ..llm_judge_image.llm import OpenAIJsonClient
        from ..llm_judge_image.runner import ImageJudgeRunner
    except ImportError as exc:
        report = _failure_report("image_llm_judge", f"Image LLM judge import failed: {exc}", model, candidate_dir)
    else:
        try:
            mock_rubric = args.get("mock_image_rubric")
            mock_judgment = args.get("mock_image_judgment")
            locked_rubric = _as_criteria_rubric(_load_locked_rubric_raw(task_dir, args))
            payloads = [item for item in (() if locked_rubric is not None else (mock_rubric,)) + (mock_judgment,) if isinstance(item, dict)]
            if payloads:
                client: Any = _MockJsonClient(payloads)
            else:
                client = OpenAIJsonClient(
                    model=model,
                    base_url=args.get("base_url") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                    api_key=os.environ.get(args.get("api_key_env", "OPENAI_API_KEY"), ""),
                    timeout=int(args.get("timeout", 120)),
                    retries=int(args.get("retries", 5)),
                    transport_mode=args.get("image_transport_mode", args.get("media_mode", "data_url")),
                )
            reference_summary = _reference_summary(task_dir, args, reference_dir=reference_dir)
            runner = ImageJudgeRunner(
                client,
                max_bytes=int(args.get("image_max_bytes", args.get("media_max_bytes", 20 * 1024 * 1024))),
                max_count=int(args.get("image_max_count", 32)),
                max_total_bytes=int(args.get("image_max_total_bytes", 100 * 1024 * 1024)),
                transport_mode=args.get("image_transport_mode", args.get("media_mode", "data_url")),
                svg_policy=args.get("svg_policy", "rasterize"),
            )
            if locked_rubric is not None:
                from ..llm_judge_image.schema import stable_hash as image_stable_hash
                from ..llm_judge_image.schema import validate_rubric as validate_image_rubric

                rubric = validate_image_rubric(locked_rubric)
                rubric_doc = {"schema_version": "1.0", "rubric": rubric, "rubric_hash": image_stable_hash(rubric)}
                (out_dir / "rubric.json").write_text(json.dumps(rubric_doc, ensure_ascii=False, indent=2), encoding="utf-8")
                result = runner.judge(task_dir, candidate_dir, reference_dir, rubric_doc, reference_summary)
                (out_dir / "judge_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                result = runner.run(task_dir, candidate_dir, reference_dir, out_dir, reference_summary)
            report = {
                "judge_type": "image_llm_judge",
                "model": model,
                "artifact_dir": str(candidate_dir),
                "result": result,
                "reference_summary": reference_summary,
                "reference_selection": args.get("_reference_selection"),
                "judge_score": normalize_unit_score(
                    float(result.get("weighted_score", 0.0)) / 4.0,
                    assume_percent_above_one=False,
                ),
            }
        except Exception as exc:
            report = _failure_report("image_llm_judge", f"Image LLM judge failed: {type(exc).__name__}: {exc}", model, candidate_dir)
            (out_dir / "judge_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _run_multimodal_judge(
    testbed_dir: str | os.PathLike[str],
    args: dict[str, Any],
    rule_result: dict[str, Any],
) -> dict[str, Any]:
    del rule_result
    task_dir = Path(testbed_dir)
    output_file = args["output_file"]
    output_path = _task_path(task_dir, output_file)
    outputs_dir = _task_path(task_dir, args.get("outputs_dir", output_path.parent))
    reference_dir = _selected_reference_dir(task_dir, args, suffixes=None, label="multimodal")
    out_dir = _task_path(task_dir, args.get("multimodal_judge_output_dir", "run_outputs/multimodal_llm_judge"))
    out_dir.mkdir(parents=True, exist_ok=True)
    model = args.get("model") or os.environ.get("OPENAI_MODEL") or "gpt-4o"

    try:
        from ..llm_judge_mm.artifacts import MediaConfig
        from ..llm_judge_mm.runner import JudgeRunner as MultimodalJudgeRunner
        from ..llm_judge_mm.runner import load_task_inputs, write_json
        from ..llm_judge_mm.schema import normalize_rubric as normalize_multimodal_rubric
        from ..llm_judge_mm.schema import stable_hash as multimodal_stable_hash
    except ImportError as exc:
        report = _failure_report("multimodal_llm_judge", f"Multimodal LLM judge import failed: {exc}", model, outputs_dir)
    else:
        try:
            config = MediaConfig(
                mode=args.get("media_mode") or os.environ.get("DU_MATE_MEDIA_MODE", "data_url"),
                base_url=args.get("media_base_url") or os.environ.get("DU_MATE_MEDIA_BASE_URL") or None,
                max_bytes=int(args.get("media_max_bytes") or os.environ.get("DU_MATE_MEDIA_MAX_BYTES", 20 * 1024 * 1024)),
                video_mode=args.get("video_mode") or os.environ.get("DU_MATE_VIDEO_MODE", "frames"),
                video_frame_count=int(args.get("video_frame_count") or os.environ.get("DU_MATE_VIDEO_FRAME_COUNT", 8)),
                video_frame_max_bytes=int(args.get("video_frame_max_bytes") or os.environ.get("DU_MATE_VIDEO_FRAME_MAX_BYTES", 2 * 1024 * 1024)),
                ffmpeg_path=args.get("ffmpeg_path") or os.environ.get("DU_MATE_FFMPEG_PATH", "ffmpeg"),
                ffprobe_path=args.get("ffprobe_path") or os.environ.get("DU_MATE_FFPROBE_PATH", "ffprobe"),
            )
            config.validate()
            inputs = load_task_inputs(task_dir, outputs_dir=outputs_dir, reference_dir=reference_dir, media_config=config)
            locked_rubric = _as_criteria_rubric(_load_locked_rubric_raw(task_dir, args))
            mock_rubric = args.get("mock_multimodal_rubric")
            mock_judgment = args.get("mock_multimodal_judgment")
            payloads = [item for item in (() if locked_rubric is not None else (mock_rubric,)) + (mock_judgment,) if isinstance(item, dict)]
            client = _MockJsonClient(payloads) if payloads else _OpenAIJsonClient(model=model, base_url=args.get("base_url"))
            runner = MultimodalJudgeRunner(client, media_config=config)
            if locked_rubric is not None:
                rubric = normalize_multimodal_rubric(
                    locked_rubric,
                    task_id=task_dir.name,
                    instruction_hash=multimodal_stable_hash(inputs["instruction"]),
                    min_criteria=1,
                )
            else:
                rubric = runner.generate_rubric(
                    task_id=task_dir.name,
                    instruction=inputs["instruction"],
                    checks_path=inputs["checks_path"],
                    references=inputs["references"],
                )
            result = runner.evaluate(
                instruction=inputs["instruction"],
                rubric=rubric,
                artifacts=inputs["artifacts"],
                references=inputs["references"],
                judge_runs=int(args.get("judge_runs", 1)),
            )
            write_json(out_dir / "rubric.json", rubric)
            write_json(out_dir / "judge_result.json", result)
            report = {
                "judge_type": "multimodal_llm_judge",
                "model": model,
                "artifact_dir": str(outputs_dir),
                "rubric": rubric,
                "result": result,
                "reference_selection": args.get("_reference_selection"),
                "judge_score": normalize_unit_score(
                    result.get("judge_score_conservative", 0.0),
                    assume_percent_above_one=False,
                ),
            }
        except Exception as exc:
            report = _failure_report(
                "multimodal_llm_judge",
                f"Multimodal LLM judge failed: {type(exc).__name__}: {exc}",
                model,
                outputs_dir,
            )
            (out_dir / "judge_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def run_llm_judge_score(
    testbed_dir: str | os.PathLike[str],
    args: dict[str, Any],
    *,
    checklist_runner: Any | None = None,
) -> dict[str, Any]:
    """Run checklist scoring, select the artifact judge, and merge score components."""
    output_file = args["output_file"]
    artifact_type = infer_artifact_type(output_file, args)
    rule_result = args.get("rule_result") or {}
    if isinstance(rule_result, dict):
        from ..scoring import equal_weight_partial_pass

        rule_result = dict(rule_result)
        rule_result["partial_pass"] = equal_weight_partial_pass(
            rule_result.get("checks"), fallback=rule_result.get("partial_pass", 0.0)
        )
    checklist_score = (
        normalize_unit_score(rule_result["partial_pass"])
        if isinstance(rule_result, dict) and "partial_pass" in rule_result
        else None
    )

    if checklist_runner is not None and args.get("checks"):
        rule_result = checklist_runner(testbed_dir, args["checks"])
        checklist_score = normalize_unit_score(rule_result.get("partial_pass", 0.0))

    output_path = _task_path(testbed_dir, output_file)
    if artifact_type in PPT_TYPES:
        # The PPT backend already reads output_file directly instead of scanning its parent.
        judge_report = _run_ppt_judge(testbed_dir, args)
        judge_kind = "ppt"
    else:
        # Other backends consume a directory. Give them a temporary directory containing
        # only output_file so sibling artifacts can never affect this artifact's score.
        with _single_artifact_directory(output_path) as scoped_dir:
            scoped_args = dict(args)
            scoped_args["outputs_dir"] = str(scoped_dir)
            scoped_args["artifact_dir"] = str(scoped_dir)
            if artifact_type in CODE_TYPES:
                judge_report = _run_code_judge(testbed_dir, scoped_args, rule_result)
                judge_kind = "code"
            elif artifact_type in TEXTUAL_TYPES:
                judge_report = _run_textual_judge(testbed_dir, scoped_args, rule_result)
                judge_kind = "textual"
            elif artifact_type in EXCEL_TYPES:
                judge_report = _run_excel_judge(testbed_dir, scoped_args, rule_result)
                judge_kind = "excel"
            elif artifact_type in PDF_TYPES:
                judge_report = _run_pdf_judge(testbed_dir, scoped_args, rule_result)
                judge_kind = "pdf"
            elif artifact_type in IMAGE_TYPES:
                judge_report = _run_image_judge(testbed_dir, scoped_args, rule_result)
                judge_kind = "image"
            elif artifact_type in MULTIMODAL_TYPES:
                judge_report = _run_multimodal_judge(testbed_dir, scoped_args, rule_result)
                judge_kind = "multimodal"
            else:
                raise ValueError(f"Unsupported LLM judge artifact type: {artifact_type}")
        judge_report["artifact_path"] = str(output_path)
        if "artifact_dir" in judge_report:
            judge_report["artifact_dir"] = str(output_path.parent)

    raw_judge_score = judge_report.get("judge_score")
    judge_score = normalize_unit_score(raw_judge_score) if raw_judge_score is not None else None
    complete_pass = normalize_unit_score(rule_result.get("complete_pass", 0.0)) if isinstance(rule_result, dict) else 0.0
    threshold = normalize_unit_score(args.get("min_final_score", 0.7))
    judge_status = str(judge_report.get("status") or "ok")
    judge_reason = str(judge_report.get("reason") or "Unified LLM judge completed.")
    final_score = (
        merge_unit_scores(complete_pass, checklist_score, judge_score)
        if judge_status == "ok" and judge_score is not None
        else None
    )
    report = {
        "schema_version": "1.0",
        "status": judge_status,
        "reason": judge_reason,
        "artifact_type": artifact_type,
        "judge_kind": judge_kind,
        "checklist_score": round(checklist_score, 4) if checklist_score is not None else None,
        "judge_score": round(judge_score, 4) if judge_score is not None else None,
        "final_score": final_score,
        "pass": bool(final_score is not None and final_score >= threshold),
        "min_final_score": threshold,
        "rule_result": rule_result,
        "judge_report": judge_report,
    }
    output_path = _task_path(testbed_dir, args.get("judge_output_file", DEFAULT_OUTPUT))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
