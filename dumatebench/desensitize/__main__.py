"""CLI for desensitizing DuMateBench dataset files."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from .core import MaskStats, create_whitelist_fields, mask_json_bytes, mask_text


WORKSPACE_SEED_DIRS = {
    "workspace_seed",
    "work_space_seed",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Desensitize text files under a DuMateBench dataset directory.")
    parser.add_argument(
        "--input",
        default="dumatebench/datasets/dev",
        type=Path,
        help="Input file or directory. Defaults to dumatebench/datasets/dev.",
    )
    parser.add_argument("--output-dir", type=Path, help="Write a desensitized copy under this directory.")
    parser.add_argument("--in-place", action="store_true", help="Rewrite input files in place.")
    parser.add_argument("--dry-run", action="store_true", help="Scan and report matches without writing files.")
    parser.add_argument(
        "--whitelist-fields",
        default="",
        help='Extra JSON field names or dot paths to skip, as comma text or JSON array, e.g. "token,trace.id".',
    )
    parser.add_argument("--include-dlp", action=argparse.BooleanOptionalAction, default=True, help="Enable dlp_v1 PII rules.")
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden files such as .DS_Store if decodable.")
    parser.add_argument("--encoding", default="utf-8", help="Text encoding to use. Defaults to utf-8.")
    args = parser.parse_args(argv)

    if args.in_place and args.output_dir:
        parser.error("--in-place and --output-dir are mutually exclusive")
    if not args.dry_run and not args.in_place and not args.output_dir:
        parser.error("choose --dry-run, --in-place, or --output-dir")
    if not args.input.exists():
        parser.error(f"input path does not exist: {args.input}")

    whitelist = create_whitelist_fields(args.whitelist_fields)
    stats = MaskStats()
    changed_files: list[Path] = []

    for path in _iter_files(args.input, include_hidden=args.include_hidden):
        stats.files_scanned += 1
        if not _should_process(path):
            stats.files_skipped += 1
            _copy_unchanged_if_needed(path, args)
            continue

        try:
            data = path.read_bytes()
            text = data.decode(args.encoding)
        except UnicodeDecodeError:
            stats.files_skipped += 1
            _copy_unchanged_if_needed(path, args)
            continue

        if path.suffix.lower() == ".jsonl":
            masked, file_stats = _mask_jsonl_text(text, whitelist, include_dlp=args.include_dlp)
        elif path.suffix.lower() == ".json":
            masked, file_stats = mask_json_bytes(text.encode("utf-8"), whitelist, include_dlp=args.include_dlp)
        else:
            masked_text, file_stats = mask_text(text, include_dlp=args.include_dlp)
            masked = masked_text.encode("utf-8")
        stats.merge(file_stats)
        if masked == text.encode("utf-8"):
            _copy_unchanged_if_needed(path, args)
            continue

        stats.files_changed += 1
        changed_files.append(path)
        if args.dry_run:
            continue
        target = _target_path(path, args)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(masked)

    _print_summary(stats, changed_files, args.input)
    return 0


def _iter_files(path: Path, include_hidden: bool) -> list[Path]:
    if path.is_file():
        return [path]
    files = []
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        if not include_hidden and any(part.startswith(".") for part in child.relative_to(path).parts):
            continue
        files.append(child)
    return files


def _should_process(path: Path) -> bool:
    """Return whether the default task-file whitelist allows this file."""

    parts = path.parts
    name = path.name
    suffix = path.suffix.lower()
    if name in {"instruction.md", "session_chat_history.json"}:
        return True
    if len(parts) >= 2 and parts[-2:] == ("evaluator", "checks.yaml"):
        return True
    if any(part in WORKSPACE_SEED_DIRS for part in parts):
        return suffix in {".md", ".json"}
    return False


def _mask_jsonl_text(text: str, whitelist: set[str], include_dlp: bool) -> tuple[bytes, MaskStats]:
    stats = MaskStats()
    output_lines: list[str] = []
    keep_final_newline = text.endswith("\n")
    for line in text.splitlines():
        if not line.strip():
            output_lines.append(line)
            continue
        masked, line_stats = mask_json_bytes(line.encode("utf-8"), whitelist, include_dlp=include_dlp, json_indent=None)
        stats.merge(line_stats)
        output_lines.append(masked.decode("utf-8").rstrip("\n"))
    serialized = "\n".join(output_lines)
    if keep_final_newline:
        serialized += "\n"
    return serialized.encode("utf-8"), stats


def _target_path(path: Path, args: argparse.Namespace) -> Path:
    if args.in_place:
        return path
    if args.input.is_file():
        return args.output_dir / path.name
    return args.output_dir / path.relative_to(args.input)


def _copy_unchanged_if_needed(path: Path, args: argparse.Namespace) -> None:
    if args.dry_run or args.in_place or args.output_dir is None:
        return
    target = _target_path(path, args)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)


def _print_summary(stats: MaskStats, changed_files: list[Path], input_path: Path) -> None:
    print(f"input: {input_path}")
    print(f"files scanned: {stats.files_scanned}")
    print(f"files changed: {stats.files_changed}")
    print(f"files skipped: {stats.files_skipped}")
    print(f"masked values: {stats.masked_secrets}")
    if stats.rule_hits:
        print("rule hits:")
        for rule_id, count in sorted(stats.rule_hits.items()):
            print(f"  {rule_id}: {count}")
    if changed_files:
        print("changed files:")
        for path in changed_files[:50]:
            print(f"  {path}")
        if len(changed_files) > 50:
            print(f"  ... {len(changed_files) - 50} more")


if __name__ == "__main__":
    sys.exit(main())
