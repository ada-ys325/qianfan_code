from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any


CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".cpp", ".cc", ".cxx",
    ".c", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".kts", ".scala",
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".r", ".lua", ".pl", ".pm", ".dart",
    ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs", ".fs", ".fsx", ".jl", ".nim",
    ".zig", ".vue", ".svelte", ".astro",
}
REFERENCE_TEXT_SUFFIXES = {".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
SUPPORTED_SUFFIXES = CODE_SUFFIXES | REFERENCE_TEXT_SUFFIXES
SUPPORTED_FILENAMES = {"Dockerfile", "Makefile", "Rakefile", "Gemfile", "Jenkinsfile"}
IGNORED_FILENAMES = {"reward.json", "judge_result.json", "rubric.json"}


class ArtifactError(ValueError):
    pass


def _sample_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = int(limit * 0.55)
    middle = int(limit * 0.2)
    tail = limit - head - middle
    middle_start = max(head, len(text) // 2 - middle // 2)
    sampled = (
        text[:head]
        + f"\n\n[...middle omitted {middle_start - head} chars...]\n\n"
        + text[middle_start : middle_start + middle]
        + f"\n\n[...tail omitted {len(text) - (middle_start + middle) - tail} chars...]\n\n"
        + text[-tail:]
    )
    return sampled, True


def _is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES or path.name in SUPPORTED_FILENAMES


def extract_artifact(path: Path, *, root: Path, char_limit: int = 80000) -> dict[str, Any]:
    root = root.resolve()
    path = path.resolve()
    if root != path and root not in path.parents:
        raise ArtifactError(f"artifact escapes root: {path}")
    if not path.is_file():
        raise ArtifactError(f"artifact is not a file: {path}")
    if not _is_supported(path):
        raise ArtifactError(f"unsupported code artifact type: {path.suffix.lower() or path.name}")

    text = path.read_text(encoding="utf-8", errors="replace")
    sampled, truncated = _sample_text(text, char_limit)
    suffix = path.suffix.lower().lstrip(".")
    return {
        "path": path.relative_to(root).as_posix(),
        "kind": suffix or path.name.lower(),
        "size_bytes": path.stat().st_size,
        "truncated": truncated,
        "structure": {
            "line_count": len(text.splitlines()),
            "character_count": len(text),
            "extension": path.suffix.lower(),
            "filename": path.name,
        },
        "content": sampled,
    }


def collect_artifacts(
    root: Path,
    *,
    max_files: int = 40,
    char_limit_per_file: int = 80000,
    total_char_limit: int = 240000,
) -> list[dict[str, Any]]:
    root = root.resolve()
    if not root.is_dir():
        raise ArtifactError(f"artifact root is not a directory: {root}")
    paths = sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and _is_supported(path)
        and path.name not in IGNORED_FILENAMES
        and not any(part.startswith(".") or part == "uploads_raw" for part in path.relative_to(root).parts)
    )
    if len(paths) > max_files:
        paths = paths[:max_files]

    artifacts: list[dict[str, Any]] = []
    remaining = total_char_limit
    for path in paths:
        if remaining <= 0:
            break
        artifact = extract_artifact(path, root=root, char_limit=min(char_limit_per_file, remaining))
        remaining -= len(artifact["content"])
        artifacts.append(artifact)
    return artifacts


def artifact_inventory(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suffix_counts: Counter[str] = Counter(str(item.get("kind") or "") for item in artifacts)
    return [
        {
            "path": item["path"],
            "kind": item["kind"],
            "size_bytes": item["size_bytes"],
            "truncated": item["truncated"],
            "structure": item["structure"],
            "bundle_kind_counts": dict(suffix_counts),
        }
        for item in artifacts
    ]

