#!/usr/bin/env python3
"""Move directories whose batch summary rows have agent_returncode 137."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import re
from pathlib import Path
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move task or run directories under an input root when their rows in "
            "batch_summary.jsonl have agent_returncode=137."
        )
    )
    parser.add_argument(
        "input_path",
        help="Root directory containing task or run folders, e.g. /data/sx/final_dataset or /data/sx/runs/<run-id>.",
    )
    parser.add_argument("batch_summary", help="Path to batch_summary.jsonl.")
    parser.add_argument("transfer_path", help="Destination root for moved folders.")
    parser.add_argument(
        "--path-field",
        choices=("auto", "run_dir", "run_dir_name", "task_dir", "task_id"),
        default="auto",
        help=(
            "Which batch summary field to use. run_dir/task_dir use full path fields; "
            "run_dir_name moves <input_path>/<basename(run_dir)>; task_id moves "
            "<input_path>/<task_id>. Default auto chooses a path under input_path."
        ),
    )
    parser.add_argument(
        "--strip-path-part",
        action="append",
        default=[],
        help=(
            "Remove the first matching path component from summary paths before matching. "
            "May be repeated, e.g. --strip-path-part final_dataset."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print moves without changing the filesystem.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing destination task directory.",
    )
    return parser.parse_args(argv)


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    invalid_count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid_count += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            invalid_count += 1
    return rows, invalid_count


def _agent_returncode(row: dict[str, Any]) -> int | None:
    try:
        return int(row.get("agent_returncode"))
    except (TypeError, ValueError):
        return None


def _strip_path_parts(path: Path, strip_parts: list[str]) -> Path:
    parts = list(path.parts)
    for strip_part in strip_parts:
        if strip_part in parts:
            parts.remove(strip_part)
    if not parts:
        return path
    return Path(*parts).resolve()


def _path_from_row(row: dict[str, Any], key: str, strip_parts: list[str]) -> Path | None:
    value = row.get(key)
    if isinstance(value, str) and value:
        return _strip_path_parts(Path(value).expanduser(), strip_parts)
    return None


def _sanitize_run_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower()
    return cleaned[:96] or "task"


def _task_id_candidates(task_id: str, input_root: Path) -> list[Path]:
    candidates = [
        (input_root / task_id).resolve(),
        (input_root / _sanitize_run_component(task_id)).resolve(),
    ]
    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def _resolve_move_dir(row: dict[str, Any], input_root: Path, path_field: str, strip_parts: list[str]) -> Path | None:
    if path_field == "run_dir_name":
        run_dir = _path_from_row(row, "run_dir", strip_parts)
        if run_dir is not None:
            return (input_root / run_dir.name).resolve()
        return None

    if path_field == "task_id":
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id:
            candidates = _task_id_candidates(task_id, input_root)
            for candidate in candidates:
                if candidate.is_dir():
                    return candidate
            return candidates[-1]
        return None

    if path_field != "auto":
        return _path_from_row(row, path_field, strip_parts)

    for key in ("run_dir", "task_dir"):
        path = _path_from_row(row, key, strip_parts)
        if path is not None and _is_relative_to(path, input_root):
            return path

    task_id = row.get("task_id")
    if isinstance(task_id, str) and task_id:
        candidates = _task_id_candidates(task_id, input_root)
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return candidates[-1]

    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def collect_agent_137_dirs(input_root: Path, batch_summary: Path, path_field: str, strip_parts: list[str]) -> tuple[list[Path], int, int]:
    rows, invalid_count = _read_jsonl(batch_summary)
    seen: set[Path] = set()
    paths: list[Path] = []
    skipped_outside_root = 0
    for row in rows:
        if _agent_returncode(row) != 137:
            continue
        move_dir = _resolve_move_dir(row, input_root, path_field, strip_parts)
        if move_dir is None:
            continue
        if not _is_relative_to(move_dir, input_root):
            skipped_outside_root += 1
            print(f"[skip outside input] {move_dir}", file=sys.stderr, flush=True)
            continue
        if move_dir not in seen:
            seen.add(move_dir)
            paths.append(move_dir)
    return paths, invalid_count, skipped_outside_root


def move_dirs(paths: list[Path], input_root: Path, transfer_root: Path, *, dry_run: bool, overwrite: bool) -> tuple[int, int]:
    moved = 0
    skipped = 0
    if not dry_run:
        transfer_root.mkdir(parents=True, exist_ok=True)

    for source in paths:
        rel = source.relative_to(input_root)
        destination = transfer_root / rel
        if not source.is_dir():
            skipped += 1
            print(f"[skip missing] {source}", file=sys.stderr, flush=True)
            continue
        if destination.exists() and not overwrite:
            skipped += 1
            print(f"[skip exists] {destination}", file=sys.stderr, flush=True)
            continue
        print(f"[move] {source} -> {destination}")
        if dry_run:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and overwrite:
            shutil.rmtree(destination)
        shutil.move(str(source), str(destination))
        moved += 1
    return moved, skipped


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_root = Path(args.input_path).expanduser().resolve()
    batch_summary = Path(args.batch_summary).expanduser().resolve()
    transfer_root = Path(args.transfer_path).expanduser().resolve()

    if not input_root.is_dir():
        raise SystemExit(f"input_path is not a directory: {input_root}")
    if not batch_summary.is_file():
        raise SystemExit(f"batch_summary is not a file: {batch_summary}")
    if transfer_root == input_root or _is_relative_to(transfer_root, input_root):
        raise SystemExit(f"transfer_path must not be inside input_path: {transfer_root}")

    paths, invalid_count, skipped_outside_root = collect_agent_137_dirs(
        input_root,
        batch_summary,
        args.path_field,
        list(args.strip_path_part),
    )
    moved, skipped = move_dirs(
        paths,
        input_root,
        transfer_root,
        dry_run=bool(args.dry_run),
        overwrite=bool(args.overwrite),
    )
    print(
        "summary: "
        f"agent_137_dirs={len(paths)} moved={moved} skipped={skipped} "
        f"invalid_jsonl_rows={invalid_count} skipped_outside_input={skipped_outside_root} "
        f"transfer_path={transfer_root}"
    )
    return 0 if skipped == 0 and skipped_outside_root == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
