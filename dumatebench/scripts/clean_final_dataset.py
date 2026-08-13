#!/usr/bin/env python3
"""Move misplaced tasks into a dataset root and remove task-level extras.

The command is deliberately a dry run unless ``--apply`` is supplied::

    python dumatebench/scripts/clean_final_dataset.py --root /data/sx/final_dataset_clean
    python dumatebench/scripts/clean_final_dataset.py --root /data/sx/final_dataset_clean --apply
    python dumatebench/scripts/clean_final_dataset.py --apply --rename-tasks

Only the six documented names are retained at the top level of each task.  The
contents below retained directories are left untouched.  With
``--rename-tasks``, tasks are renamed in discovery order to ``task_1`` through
``task_200``.  The original-to-new name mapping is written to
``task_name_mapping.json`` in the dataset root.  After a successful rename,
root-level directories whose names do not match ``task_N`` are removed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ROOT = Path("/data/sx/final_dataset_clean")
ALLOWED_TOP_LEVEL = frozenset(
    {
        "evaluator",
        "web_reference",
        "workspace_seed",
        "instruction.md",
        "task_type_feature.json",
        "task.yaml",
    }
)
TASK_FILE_MARKERS = ("instruction.md", "task.yaml", "task_type_feature.json")
TASK_DIRECTORY_MARKERS = ("evaluator", "web_reference", "workspace_seed")
TASK_DIR_NAME = re.compile(r"task_\d+\Z")


@dataclass(frozen=True)
class Task:
    """A task and the path at which it currently lives."""

    source: Path
    name: str
    destination: Path


@dataclass(frozen=True)
class Rename:
    """A task's original, intermediate, and final directory paths."""

    task: Task
    current: Path
    final: Path


def is_task_dir(path: Path) -> bool:
    """Return whether *path* looks like a task directory.

    Any of the expected top-level files or directories is enough to identify a
    task.  Symlinked directories are skipped because deleting through one
    could affect data outside the dataset root.
    """

    if not path.is_dir() or path.is_symlink():
        return False
    return any((path / marker).is_file() for marker in TASK_FILE_MARKERS) or any(
        (path / marker).is_dir() for marker in TASK_DIRECTORY_MARKERS
    )


def discover_tasks(root: Path) -> list[Task]:
    """Find tasks directly under *root* and one directory below *root*."""

    direct: list[Task] = []
    containers: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith(".clean_final_dataset_rename_"):
            continue
        if is_task_dir(entry):
            direct.append(Task(entry, entry.name, entry))
        else:
            containers.append(entry)

    nested: list[Task] = []
    for container in containers:
        for entry in sorted(container.iterdir(), key=lambda item: item.name):
            if is_task_dir(entry):
                nested.append(Task(entry, entry.name, root / entry.name))

    return direct + nested


def choose_destination(task: Task, used_names: set[str], on_conflict: str) -> Path:
    """Choose a non-conflicting destination for a task."""

    if task.destination.name not in used_names and not task.destination.exists():
        used_names.add(task.destination.name)
        return task.destination
    if on_conflict == "skip":
        return task.destination
    if on_conflict == "error":
        raise FileExistsError(
            f"destination already exists for {task.source}: {task.destination}"
        )

    index = 1
    while True:
        candidate = task.destination.with_name(f"{task.name}__moved_{index}")
        if candidate.name not in used_names and not candidate.exists():
            used_names.add(candidate.name)
            return candidate
        index += 1


def remove_extras(task_dir: Path, *, apply: bool) -> list[Path]:
    """List, and optionally remove, disallowed top-level entries."""

    extras = [
        entry
        for entry in sorted(task_dir.iterdir(), key=lambda item: item.name)
        if entry.name not in ALLOWED_TOP_LEVEL
    ]
    if not apply:
        return extras

    for entry in extras:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    return extras


def non_task_root_dirs(root: Path, current_task_paths: set[Path]) -> list[Path]:
    """Return root directories that will not remain as ``task_N`` directories."""

    result: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry in current_task_paths:
            continue
        if not TASK_DIR_NAME.fullmatch(entry.name):
            result.append(entry)
    return result


def remove_root_dirs(paths: list[Path]) -> None:
    """Remove root-level directories or directory symlinks after task renaming."""

    for path in paths:
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"dataset root (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform moves and deletions (without this flag, only print the plan)",
    )
    parser.add_argument(
        "--on-conflict",
        choices=("error", "skip", "rename"),
        default="error",
        help="handling when a moved task name exists at root (default: error)",
    )
    parser.add_argument(
        "--rename-tasks",
        action="store_true",
        help="rename discovered tasks sequentially to task_1 through task_200",
    )
    parser.add_argument(
        "--rename-map-file",
        type=Path,
        help="JSON file for original-to-new task names (default: ROOT/task_name_mapping.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = args.root.expanduser()
    if not root.is_dir() or root.is_symlink():
        print(
            f"dataset root does not exist or is not a real directory: {root}",
            file=sys.stderr,
        )
        return 2

    tasks = discover_tasks(root)
    direct_count = sum(task.source == task.destination for task in tasks)
    nested_count = len(tasks) - direct_count
    print(f"Root: {root.resolve()}")
    print(f"Tasks found: {len(tasks)} (direct={direct_count}, nested={nested_count})")
    if args.rename_tasks and len(tasks) > 200:
        print(
            f"ERROR: found {len(tasks)} tasks, but --rename-tasks supports at most 200.\n"
            "No changes made.",
            file=sys.stderr,
        )
        return 1

    used_names = {task.name for task in tasks if task.source == task.destination}
    moves: list[tuple[Task, Path]] = []
    skipped: list[Task] = []
    try:
        for task in tasks:
            if task.source == task.destination:
                continue
            destination = choose_destination(task, used_names, args.on_conflict)
            if destination == task.destination and args.on_conflict == "skip":
                skipped.append(task)
            else:
                moves.append((task, destination))
    except FileExistsError as exc:
        print(
            f"ERROR: {exc}\nNo changes made. Use --on-conflict skip or rename.",
            file=sys.stderr,
        )
        return 1

    if moves:
        print("Moves:")
        for task, destination in moves:
            print(f"  {task.source} -> {destination}")
    if skipped:
        print("Skipped due to destination conflicts:")
        for task in skipped:
            print(f"  {task.source} (destination: {task.destination})")

    destination_by_task = dict(moves)
    planned_cleanups: list[tuple[Task, list[Path]]] = []
    for task in tasks:
        if task in skipped:
            continue
        extras = remove_extras(task.source, apply=False)
        planned_cleanups.append((task, extras))
    print("Cleanup:")
    for task, extras in planned_cleanups:
        destination = destination_by_task.get(task, task.destination)
        if extras:
            print(f"  {destination}: remove {', '.join(item.name for item in extras)}")
        else:
            print(f"  {destination}: already clean")

    rename_plan: list[Rename] = []
    mapping_file: Path | None = None
    root_dirs_to_remove: list[Path] = []
    if args.rename_tasks:
        rename_tasks = [task for task in tasks if task not in skipped]
        current_task_paths = [
            destination_by_task.get(task, task.destination) for task in rename_tasks
        ]
        current_task_set = set(current_task_paths)
        mapping_file = (
            args.rename_map_file.expanduser()
            if args.rename_map_file is not None
            else root / "task_name_mapping.json"
        )
        if mapping_file.is_dir():
            print(
                f"ERROR: rename mapping path is a directory: {mapping_file}\nNo changes made.",
                file=sys.stderr,
            )
            return 1
        for index, (task, current) in enumerate(
            zip(rename_tasks, current_task_paths), start=1
        ):
            final = root / f"task_{index}"
            if final.exists() and final not in current_task_set:
                print(
                    f"ERROR: rename destination already exists and is not a task: {final}\n"
                    "No changes made.",
                    file=sys.stderr,
                )
                return 1
            rename_plan.append(Rename(task, current, final))
        print("Renames:")
        for rename in rename_plan:
            print(f"  {rename.current} -> {rename.final}")
        print(f"Rename mapping file: {mapping_file}")
        root_dirs_to_remove = non_task_root_dirs(root, current_task_set)
        if root_dirs_to_remove:
            print("Root directories to remove after renaming:")
            for directory in root_dirs_to_remove:
                print(f"  {directory}")

    if not args.apply:
        print("Dry run: no changes made. Re-run with --apply to execute this plan.")
        return 0

    for task, destination in moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(task.source), str(destination))
    removed = 0
    for task, _ in planned_cleanups:
        task_dir = destination_by_task.get(task, task.destination)
        removed += len(remove_extras(task_dir, apply=True))

    if rename_plan:
        staging = root / f".clean_final_dataset_rename_{uuid.uuid4().hex}"
        staging.mkdir()
        staged: list[tuple[Path, Path]] = []
        try:
            for index, rename in enumerate(rename_plan, start=1):
                temporary = staging / str(index)
                rename.current.rename(temporary)
                staged.append((temporary, rename.final))
            for temporary, final in staged:
                temporary.rename(final)
        finally:
            if staging.exists() and not any(staging.iterdir()):
                staging.rmdir()

    if mapping_file is not None:
        mapping_file.parent.mkdir(parents=True, exist_ok=True)
        mapping = {
            "dataset_root": str(root.resolve()),
            "tasks": [
                {
                    "original_name": rename.task.name,
                    "original_path": str(rename.task.source),
                    "pre_rename_path": str(rename.current),
                    "new_name": rename.final.name,
                    "new_path": str(rename.final),
                }
                for rename in rename_plan
            ],
        }
        temporary_map = mapping_file.with_name(
            f".{mapping_file.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary_map.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_map.replace(mapping_file)

    remove_root_dirs(root_dirs_to_remove)

    print(
        f"Applied: moved={len(moves)}, removed={removed}, "
        f"renamed={len(rename_plan)}, root_dirs_removed={len(root_dirs_to_remove)}, "
        f"skipped={len(skipped)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
