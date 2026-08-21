"""Select task-relevant files from a noisy reference workspace."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

NOISE_NAME_PATTERNS = (
    "old",
    "backup",
    "bak",
    "copy",
    "tmp",
    "temp",
    "noise",
    "draft",
    "示例",
    "样例",
    "旧",
    "备份",
    "副本",
    "临时",
)
IGNORED_REFERENCE_FILENAMES = {
    "annotation_review.json",
    "reward.json",
    "reward_with_llm_judge.json",
    "llm_judge_score.json",
    "judge_result.json",
    "rubric.json",
}


def task_reference_context(task_dir: Path, *, instruction_file: str = "instruction.md") -> str:
    path = task_dir / instruction_file
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")[:120_000]


def select_reference_files(
    reference_dir: Path | None,
    *,
    task_dir: Path,
    output_file: str | os.PathLike[str] | None = None,
    suffixes: set[str] | None = None,
    max_files: int = 20,
    instruction_file: str = "instruction.md",
) -> tuple[list[Path], dict[str, Any]]:
    """Return reference files likely required by the task.

    The selector only narrows the reference set when instruction.md explicitly
    names files or stems. Otherwise callers should keep using the original
    directory, because a broad workspace is safer than a guessed subset.
    """
    if reference_dir is None or not reference_dir.is_dir():
        return [], {"status": "disabled_or_missing", "selected": False, "files": []}

    suffixes = {suffix.lower() for suffix in suffixes} if suffixes else None
    candidates = [
        path
        for path in sorted(reference_dir.rglob("*"))
        if path.is_file()
        and path.name not in IGNORED_REFERENCE_FILENAMES
        and not any(part.startswith(".") or part == "uploads_raw" for part in path.relative_to(reference_dir).parts)
        and (suffixes is None or path.suffix.lower() in suffixes)
    ]
    if not candidates:
        return [], {"status": "no_candidates", "selected": False, "files": []}

    context = task_reference_context(task_dir, instruction_file=instruction_file)
    context_lower = context.lower()
    output_stem = _normalise_stem(Path(output_file).stem) if output_file else ""
    scored: list[tuple[int, str, Path, list[str]]] = []
    for path in candidates:
        rel = path.relative_to(reference_dir).as_posix()
        score, reasons = _score_path(path, rel, context_lower, output_stem)
        scored.append((score, rel, path, reasons))
    scored.sort(key=lambda item: (-item[0], item[1]))

    top_score = scored[0][0]
    selected = top_score >= 50
    chosen = [
        path
        for score, _, path, _ in scored
        if selected and score >= 50
    ][:max_files]
    report_files = [
        {
            "path": rel,
            "score": score,
            "selected": selected and score >= 50 and path in chosen,
            "reasons": reasons,
        }
        for score, rel, path, reasons in scored[: max(max_files, 20)]
    ]
    return chosen, {
        "status": "selected" if selected else "no_explicit_match",
        "selected": selected,
        "files": report_files,
        "candidate_count": len(candidates),
        "max_files": max_files,
        "context_source": instruction_file,
    }


def materialize_selected_references(
    *,
    task_dir: Path,
    reference_dir: Path | None,
    output_file: str | os.PathLike[str] | None,
    suffixes: set[str] | None,
    label: str,
    max_files: int = 20,
    instruction_file: str = "instruction.md",
) -> tuple[Path | None, dict[str, Any]]:
    selected, report = select_reference_files(
        reference_dir,
        task_dir=task_dir,
        output_file=output_file,
        suffixes=suffixes,
        max_files=max_files,
        instruction_file=instruction_file,
    )
    if reference_dir is None or not reference_dir.is_dir() or not report.get("selected"):
        return reference_dir, report

    target = task_dir / ".llm_judge_selected_references" / label
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for source in selected:
        relative = source.relative_to(reference_dir)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (target / ".selection_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target, report


def _score_path(path: Path, relative_path: str, context_lower: str, output_stem: str) -> tuple[int, list[str]]:
    name = path.name
    stem = path.stem
    score = 0
    reasons: list[str] = []
    for needle, points, reason in (
        (relative_path, 120, "relative path appears in instruction"),
        (name, 100, "file name appears in instruction"),
        (stem, 55, "file stem appears in instruction"),
    ):
        if needle and needle.lower() in context_lower:
            score += points
            reasons.append(reason)
    if output_stem and _normalise_stem(stem) == output_stem:
        score += 20
        reasons.append("file stem matches normalized output stem")
    lowered_name = name.lower()
    if any(pattern in lowered_name for pattern in NOISE_NAME_PATTERNS):
        score -= 10
        reasons.append("name looks like possible noise")
    if not reasons:
        reasons.append("no explicit instruction match")
    return score, reasons


def _normalise_stem(stem: str) -> str:
    value = stem.strip()
    suffixes = (
        "_优化",
        "-优化",
        " 优化",
        "_edited",
        "-edited",
        "_output",
        "-output",
        "_final",
        "-final",
        "_result",
        "-result",
        "_结果",
        "-结果",
    )
    changed = True
    while changed:
        changed = False
        lower = value.lower()
        for suffix in suffixes:
            if lower.endswith(suffix.lower()):
                value = value[: -len(suffix)]
                changed = True
                break
    return re.sub(r"\s+", "", value).lower()
