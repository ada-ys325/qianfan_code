from __future__ import annotations

import base64
import importlib
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

IGNORED_FILENAMES = {"reward.json", "judge_result.json", "rubric.json"}
AUXILIARY_SUFFIXES = {".txt", ".md", ".json"}


class ArtifactError(ValueError):
    pass


def sample_page_numbers(page_count: int, max_pages: int) -> list[int]:
    if page_count < 1 or max_pages < 1:
        return []
    if page_count <= max_pages:
        return list(range(page_count))
    if max_pages == 1:
        return [0]
    return sorted({round(index * (page_count - 1) / (max_pages - 1)) for index in range(max_pages)})


def _sample_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = int(limit * 0.6)
    tail = limit - head
    return text[:head] + f"\n\n[... omitted {len(text) - limit} characters ...]\n\n" + text[-tail:], True


def _safe_path(path: Path, root: Path) -> Path:
    path = path.resolve()
    root = root.resolve()
    if path != root and root not in path.parents:
        raise ArtifactError(f"artifact escapes root: {path}")
    return path


def _load_fitz() -> Any:
    try:
        return importlib.import_module("fitz")
    except ImportError as exc:
        raise ArtifactError("PyMuPDF is required to render PDF evidence; install DataAnnotation/requirements.txt") from exc


def extract_pdf(path: Path, *, root: Path, render_dir: Path, max_pages: int = 12, char_limit: int = 80000) -> dict[str, Any]:
    path = _safe_path(path, root)
    try:
        pdf_reader = importlib.import_module("pypdf").PdfReader
    except ImportError as exc:
        raise ArtifactError("pypdf is required to inspect PDF artifacts; install DataAnnotation/requirements.txt") from exc

    try:
        reader = pdf_reader(str(path))
        page_count = len(reader.pages)
        page_texts = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ArtifactError(f"cannot parse PDF {path.name}: {exc}") from exc
    if page_count < 1:
        raise ArtifactError(f"PDF has no pages: {path.name}")

    combined_text, truncated = _sample_text("\n\n".join(
        f"[Page {index}]\n{text}" for index, text in enumerate(page_texts, start=1)
    ), char_limit)
    selected = sample_page_numbers(page_count, max_pages)
    fitz = _load_fitz()
    images: list[dict[str, Any]] = []
    try:
        document = fitz.open(str(path))
        matrix = fitz.Matrix(1.5, 1.5)
        for page_index in selected:
            image_path = render_dir / f"{path.stem}-page-{page_index + 1}.png"
            document.load_page(page_index).get_pixmap(matrix=matrix, alpha=False).save(str(image_path))
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            images.append({
                "page": page_index + 1,
                "mime_type": "image/png",
                "data_url": f"data:image/png;base64,{encoded}",
            })
        document.close()
    except Exception as exc:
        raise ArtifactError(f"cannot render PDF {path.name}: {exc}") from exc

    relative_path = path.relative_to(root.resolve()).as_posix()
    return {
        "path": relative_path,
        "suffix": ".pdf",
        "size_bytes": path.stat().st_size,
        "page_count": page_count,
        "sampled_pages": [number + 1 for number in selected],
        "omitted_pages": [number + 1 for number in range(page_count) if number not in selected],
        "text": combined_text,
        "text_truncated": truncated,
        "page_character_counts": [len(text) for text in page_texts],
        "images": images,
    }


def _extract_auxiliary(path: Path, root: Path, char_limit: int) -> dict[str, Any]:
    path = _safe_path(path, root)
    if path.suffix.lower() == ".json":
        try:
            text = json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactError(f"cannot read JSON artifact {path.name}: {exc}") from exc
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactError(f"cannot read text artifact {path.name}: {exc}") from exc
    text, truncated = _sample_text(text, char_limit)
    return {
        "path": path.relative_to(root.resolve()).as_posix(),
        "suffix": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "text": text,
        "text_truncated": truncated,
        "images": [],
    }


@contextmanager
def collect_artifacts(
    root: Path,
    *,
    max_files: int = 20,
    max_pages: int = 12,
    total_chars: int = 80000,
    require_pdf: bool = True,
) -> Iterator[tuple[list[dict[str, Any]], list[str]]]:
    root = root.resolve()
    if not root.is_dir():
        raise ArtifactError(f"artifact directory does not exist: {root}")
    paths = [
        path for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.name not in IGNORED_FILENAMES
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
        and (path.suffix.lower() == ".pdf" or path.suffix.lower() in AUXILIARY_SUFFIXES)
    ][:max_files]
    pdf_paths = [path for path in paths if path.suffix.lower() == ".pdf"]
    if require_pdf and not pdf_paths:
        yield [], ["no PDF artifact found"]
        return

    artifacts: list[dict[str, Any]] = []
    errors: list[str] = []
    per_file_chars = max(2000, total_chars // max(1, len(paths)))
    with tempfile.TemporaryDirectory(prefix="pdf-judge-") as directory:
        render_dir = Path(directory)
        for path in paths:
            try:
                if path.suffix.lower() == ".pdf":
                    artifacts.append(extract_pdf(path, root=root, render_dir=render_dir, max_pages=max_pages, char_limit=per_file_chars))
                else:
                    artifacts.append(_extract_auxiliary(path, root, per_file_chars))
            except ArtifactError as exc:
                errors.append(str(exc))
        yield artifacts, errors


def artifact_inventory(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key not in {"text", "images"}} for item in artifacts]
