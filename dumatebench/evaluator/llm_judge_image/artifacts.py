from __future__ import annotations

import hashlib
import io
import mimetypes
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
SVG_SUFFIX = ".svg"
MAX_DECODED_PIXELS = 100_000_000
IGNORED_NAMES = {"rubric.json", "judge_result.json", "summary.json"}

class ArtifactError(ValueError):
    pass

class NoCandidateImagesError(ArtifactError):
    pass

class CorruptImageError(ArtifactError):
    pass

def _safe_path(path: Path, root: Path) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ArtifactError(f"image is outside allowed root: {path}") from exc
    return resolved

def _metadata(path: Path, role: str, source: str, max_bytes: int, svg_policy: str) -> dict[str, Any]:
    item = {"path": str(path), "role": role, "source": source, "mime": None, "bytes": 0, "width": None, "height": None, "mode": None, "sha256": None, "transport": {"status": "pending"}}
    if path.stat().st_size > max_bytes:
        item["transport"] = {"status": "unavailable", "reason": f"size exceeds limit {max_bytes} bytes"}
        return item
    raw = path.read_bytes()
    item["bytes"] = len(raw)
    item["sha256"] = hashlib.sha256(raw).hexdigest()
    item["_bytes"] = raw
    suffix = path.suffix.lower()
    if suffix == SVG_SUFFIX:
        item["mime"] = "image/svg+xml"
        if svg_policy == "reject":
            item["transport"] = {"status": "unavailable", "reason": "SVG rasterization disabled"}
            return item
        try:
            import cairosvg
        except ImportError:
            item["transport"] = {"status": "unavailable", "reason": "cairosvg is not installed for SVG rasterization"}
            return item
        try:
            png = cairosvg.svg2png(bytestring=raw)
            if len(png) > max_bytes:
                item["transport"] = {"status": "unavailable", "reason": f"rasterized SVG exceeds limit {max_bytes} bytes"}
                return item
            with Image.open(io.BytesIO(png)) as image:
                if image.width * image.height > MAX_DECODED_PIXELS:
                    raise ValueError(f"decoded image exceeds {MAX_DECODED_PIXELS} pixels")
                item.update({"mime": "image/png", "bytes": len(png), "width": image.width, "height": image.height, "mode": image.mode, "sha256": hashlib.sha256(png).hexdigest()})
            item["_bytes"] = png
            item["transport"] = {"status": "ready", "encoding": "memory-rasterized"}
            return item
        except Exception as exc:
            item["transport"] = {"status": "unavailable", "reason": f"SVG rasterization failed: {exc}"}
            return item
    if suffix not in IMAGE_SUFFIXES:
        raise ArtifactError(f"unsupported image suffix: {path.suffix}")
    item["mime"] = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[suffix]
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_DECODED_PIXELS:
                raise ValueError(f"decoded image exceeds {MAX_DECODED_PIXELS} pixels")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            item.update({"width": image.width, "height": image.height, "mode": image.mode})
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        item["transport"] = {"status": "unavailable", "reason": f"corrupt image: {exc}"}
        return item
    item["transport"] = {"status": "ready", "encoding": "file"}
    return item

def _image_paths(root: Path) -> list[Path]:
    root = root.resolve()
    if not root.exists():
        return []
    return sorted(
        _safe_path(p, root)
        for p in root.rglob("*")
        if p.is_file()
        and p.name not in IGNORED_NAMES
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
        and p.suffix.lower() in IMAGE_SUFFIXES | {SVG_SUFFIX}
    )

def discover_images(root: Path, role: str, *, max_bytes: int = 20 * 1024 * 1024, max_count: int = 32, svg_policy: str = "rasterize") -> list[dict[str, Any]]:
    root = root.resolve()
    paths = _image_paths(root)
    if len(paths) > max_count:
        raise ArtifactError(f"{role} image count {len(paths)} exceeds limit {max_count}")
    output = []
    for path in paths:
        safe = _safe_path(path, root)
        output.append(_metadata(safe, role, str(root), max_bytes, svg_policy))
    return output

def collect_artifacts(candidate_root: Path, reference_root: Path | None = None, *, max_bytes: int = 20 * 1024 * 1024, max_count: int = 32, max_total_bytes: int = 100 * 1024 * 1024, svg_policy: str = "rasterize") -> list[dict[str, Any]]:
    candidate_paths = _image_paths(candidate_root)
    reference_paths = _image_paths(reference_root) if reference_root else []
    if not candidate_paths:
        raise NoCandidateImagesError(f"no candidate images found in {candidate_root}")
    if len(candidate_paths) + len(reference_paths) > max_count:
        raise ArtifactError(f"total image count {len(candidate_paths) + len(reference_paths)} exceeds limit {max_count}")
    total = sum(path.stat().st_size for path in candidate_paths + reference_paths)
    if total > max_total_bytes:
        raise ArtifactError(f"total image bytes {total} exceeds limit {max_total_bytes}")
    candidates = discover_images(candidate_root, "candidate", max_bytes=max_bytes, max_count=max_count, svg_policy=svg_policy)
    references = discover_images(reference_root, "reference", max_bytes=max_bytes, max_count=max_count, svg_policy=svg_policy) if reference_root else []
    all_items = candidates + references
    role_counts = {"candidate": 0, "reference": 0}
    for item in all_items:
        role_counts[item["role"]] += 1
        item["id"] = f"{item['role']}_{role_counts[item['role']]:03d}"
    return all_items

def public_inventory(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inventory = []
    for item in items:
        public = {k: v for k, v in item.items() if k not in {"_bytes", "source"}}
        scope = "outputs" if item.get("role") == "candidate" else "references"
        public["path"] = f"{scope}/{Path(str(item.get('path', ''))).name}"
        inventory.append(public)
    return inventory
