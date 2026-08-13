#!/usr/bin/env python3
"""Fill missing task.yaml files for DuMateBench task directories."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


IGNORED_DIRS = {
    ".batch_runtime",
    ".git",
    "__pycache__",
    "environment",
    "evaluator",
    "run_logs",
    "run_outputs",
    "runs",
    "workspace_seed",
}


@dataclass(frozen=True)
class FillResult:
    task_dir: Path
    status: str
    task_yaml: Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks_dir", type=Path, help="Directory tree containing DuMateBench tasks.")
    parser.add_argument("--split", default="dev")
    parser.add_argument("--difficulty", default="unreviewed")
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--cpus", default="2")
    parser.add_argument("--memory-mb", type=int, default=4096)
    parser.add_argument("--storage-mb", type=int, default=12000)
    parser.add_argument("--tag", action="append", default=[], help="Default tag to add. Can be repeated.")
    parser.add_argument("--overwrite", action="store_true", help="Rewrite existing task.yaml files too.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files.")
    return parser.parse_args(argv)


def is_ignored(path: Path) -> bool:
    return any(part in IGNORED_DIRS or part.startswith(".") for part in path.parts)


def discover_task_dirs(tasks_dir: Path) -> list[Path]:
    tasks: list[Path] = []
    for instruction in sorted(tasks_dir.rglob("instruction.md")):
        if not instruction.is_file():
            continue
        try:
            rel = instruction.parent.relative_to(tasks_dir)
        except ValueError:
            rel = instruction.parent
        if is_ignored(rel):
            continue
        tasks.append(instruction.parent)
    return tasks


def yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def instruction_title(task_dir: Path) -> str:
    instruction = task_dir / "instruction.md"
    if not instruction.is_file():
        return task_dir.name
    for line in instruction.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return task_dir.name


def task_yaml_text(task_dir: Path, args: argparse.Namespace) -> str:
    tags = list(dict.fromkeys(args.tag))
    tag_block = "tags: []\n" if not tags else "tags:\n" + "".join(f"  - {yaml_scalar(tag)}\n" for tag in tags)
    return (
        "schema_version: 0.1\n"
        f"task_id: {yaml_scalar(task_dir.name)}\n"
        f"task_name: {yaml_scalar(instruction_title(task_dir))}\n"
        f"split: {yaml_scalar(args.split)}\n"
        f"difficulty: {yaml_scalar(args.difficulty)}\n"
        f"{tag_block}"
        "agent:\n"
        f"  timeout_sec: {args.timeout_sec}\n"
        "  user: agent\n"
        "  workdir: /workspace\n"
        "  direct_shell: true\n"
        "  runtime: in_container\n"
        "environment:\n"
        "  backend: docker-compose\n"
        "  dockerfile: environment/Dockerfile\n"
        "  compose_file: environment/docker-compose.yaml\n"
        f"  cpus: {args.cpus}\n"
        f"  memory_mb: {args.memory_mb}\n"
        f"  storage_mb: {args.storage_mb}\n"
        "  allow_internet: true\n"
        "  allow_package_install: true\n"
        "evaluation:\n"
        "  entrypoint: evaluator/evaluator.py\n"
        "  checks_file: evaluator/checks.yaml\n"
        "  hidden_tests: false\n"
        "  metrics:\n"
        "    - complete_pass\n"
        "    - partial_pass\n"
    )


def fill_task_yaml(task_dir: Path, args: argparse.Namespace) -> FillResult:
    task_yaml = task_dir / "task.yaml"
    if task_yaml.exists() and not args.overwrite:
        return FillResult(task_dir, "exists", task_yaml)
    existed = task_yaml.exists()
    if args.dry_run:
        return FillResult(task_dir, "would_overwrite" if existed else "would_write", task_yaml)
    task_yaml.write_text(task_yaml_text(task_dir, args), encoding="utf-8")
    return FillResult(task_dir, "overwritten" if existed else "written", task_yaml)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks_dir = args.tasks_dir.expanduser().resolve()
    if not tasks_dir.is_dir():
        raise SystemExit(f"tasks_dir not found: {tasks_dir}")

    results = [fill_task_yaml(task_dir, args) for task_dir in discover_task_dirs(tasks_dir)]
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status != "exists":
            print(f"[{result.status}] {result.task_yaml}")

    print(
        "summary: "
        f"tasks={len(results)} "
        f"written={counts.get('written', 0)} "
        f"would_write={counts.get('would_write', 0)} "
        f"exists={counts.get('exists', 0)} "
        f"overwritten={counts.get('overwritten', 0)} "
        f"would_overwrite={counts.get('would_overwrite', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
