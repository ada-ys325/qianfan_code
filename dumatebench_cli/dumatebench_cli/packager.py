"""Task-package authoring checks (for dumatebench task authors, not players).

DuMateBench does not physically hide evaluator/gold-reference files from the
distributed task package (same posture as terminal-bench): isolation from
the agent's live command channel is procedural, not filesystem-permission
based.

- ``evaluator/evaluator.py`` is invoked from the *host* batch runner via
  ``sys.executable``, never via ``docker compose exec`` inside the task
  container, so the in-container agent shell never gets pointed at it.
- ``evaluator/`` and ``web_reference/`` (gold references) are still ``COPY``'d
  into the built image today (for convenience/debugging), which means a
  sufficiently curious agent *could* read them via shell access. This check
  flags that so task authors can decide whether to tighten a given task's
  Dockerfile, but does not fail the check outright since it mirrors current
  behavior across existing tasks.

This module is meant for `dumate package check`, used by dumatebench
maintainers before publishing a task, not by players.
"""

from __future__ import annotations

import glob
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dumatebench_cli.task_metadata import TaskMetadataError, load_task_metadata, shared_evaluate_path

REQUIRED_FILES = ("task.yaml", "instruction.md")
REQUIRED_DIRS = ("environment", "evaluator")
GOLD_REFERENCE_DIRS = ("evaluator", "web_reference")


@dataclass
class Check:
    ok: bool
    message: str
    advisory: bool = False


@dataclass
class CheckResult:
    passed: bool
    checks: list[Check] = field(default_factory=list)


def _dockerfile_copies_dir(dockerfile_text: str, dir_name: str) -> bool:
    pattern = re.compile(rf"^\s*COPY\s+.*{re.escape(dir_name)}/?\s+", re.MULTILINE)
    return bool(pattern.search(dockerfile_text))


def _dockerfile_sources(dockerfile_text: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Extract local COPY/ADD sources and parser errors from a Dockerfile."""
    logical = re.sub(r"\\\r?\n", " ", dockerfile_text)
    instructions: list[tuple[str, list[str]]] = []
    errors: list[str] = []
    for raw_line in logical.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(COPY|ADD)\s+(.*)$", line, flags=re.IGNORECASE)
        if not match:
            continue
        instruction = match.group(1).upper()
        rest = match.group(2).strip()
        try:
            if rest.startswith("["):
                values = json.loads(rest)
                if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                    raise ValueError("JSON form must be a string array")
                tokens = values
            else:
                tokens = shlex.split(rest, comments=True)
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{instruction} line is invalid: {exc}")
            continue
        if any(token.startswith("--from=") for token in tokens):
            continue
        while tokens and tokens[0].startswith("--"):
            tokens.pop(0)
        if len(tokens) < 2:
            errors.append(f"{instruction} must have at least one source and a destination")
            continue
        instructions.append((instruction, tokens[:-1]))
    return instructions, errors


def _dockerfile_context_checks(dockerfile: Path, context: Path) -> list[Check]:
    checks: list[Check] = []
    instructions, parse_errors = _dockerfile_sources(dockerfile.read_text(encoding="utf-8"))
    checks.extend(Check(False, f"environment/Dockerfile: {error}") for error in parse_errors)
    context = context.resolve()
    for instruction, sources in instructions:
        for source in sources:
            if instruction == "ADD" and source.startswith(("http://", "https://")):
                continue
            if "$" in source:
                checks.append(Check(
                    False,
                    f"environment/Dockerfile {instruction} source {source!r} uses an unresolved variable",
                ))
                continue
            candidate_pattern = str(context / source)
            matches = [Path(match).resolve() for match in glob.glob(candidate_pattern, recursive=True)]
            if not matches:
                checks.append(Check(
                    False,
                    f"environment/Dockerfile {instruction} source {source!r} is missing from build context",
                ))
                continue
            for match in matches:
                try:
                    match.relative_to(context)
                except ValueError:
                    checks.append(Check(
                        False,
                        f"environment/Dockerfile {instruction} source {source!r} escapes build context",
                    ))
    if not checks:
        checks.append(Check(True, "environment/Dockerfile COPY/ADD sources exist in its build context"))
    return checks


def _build_context(task_dir: Path, *, harbor_compatible: bool) -> tuple[Path, Path, str | None]:
    """Resolve the Dockerfile/context used by local Compose or Harbor."""
    environment = task_dir / "environment"
    if harbor_compatible:
        return environment, environment / "Dockerfile", None

    compose_file = environment / "docker-compose.yaml"
    if not compose_file.is_file():
        return environment, environment / "Dockerfile", None
    try:
        compose = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
        services = compose.get("services") if isinstance(compose, dict) else None
        service = next(iter(services.values())) if isinstance(services, dict) and services else None
        build = service.get("build") if isinstance(service, dict) else None
        if isinstance(build, str):
            context = (compose_file.parent / build).resolve()
            return context, context / "Dockerfile", None
        if isinstance(build, dict):
            context = (compose_file.parent / str(build.get("context", "."))).resolve()
            dockerfile = context / str(build.get("dockerfile", "Dockerfile"))
            return context, dockerfile, None
        # Legacy compose files may rely on a prebuilt image or an external
        # override. Keep the local package check useful; Harbor export still
        # rejects the legacy compose file explicitly.
        return environment, environment / "Dockerfile", None
    except (OSError, yaml.YAMLError) as exc:
        return environment, environment / "Dockerfile", f"cannot read docker-compose.yaml build context: {exc}"


def _checks_yaml_check(path: Path) -> Check:
    if not path.is_file():
        return Check(False, "evaluator/checks.yaml exists")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return Check(False, f"evaluator/checks.yaml is valid YAML: {exc}")
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list) or not checks:
        return Check(False, "evaluator/checks.yaml contains a non-empty checks list")
    for index, item in enumerate(checks, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item.get("id"):
            return Check(False, f"evaluator/checks.yaml check {index} has a non-empty id")
        if not isinstance(item.get("type"), str) or not item.get("type"):
            return Check(False, f"evaluator/checks.yaml check {index} has a non-empty type")
    return Check(True, "evaluator/checks.yaml contains valid check items")


def check_task_dir(task_dir: Path, *, harbor_compatible: bool = False) -> CheckResult:
    checks: list[Check] = []

    for name in REQUIRED_FILES:
        checks.append(Check((task_dir / name).exists(), f"{name} exists"))

    for name in REQUIRED_DIRS:
        checks.append(Check((task_dir / name).is_dir(), f"{name}/ exists"))

    try:
        task_yaml, task_id = load_task_metadata(task_dir)
        checks.append(Check(True, f"task.yaml.task_id is valid ({task_id})"))
    except TaskMetadataError as exc:
        task_yaml = {}
        checks.append(Check(False, str(exc)))

    environment_cfg = task_yaml.get("environment")
    if not isinstance(environment_cfg, dict):
        checks.append(Check(False, "task.yaml environment is a mapping"))
    elif "allow_internet" not in environment_cfg:
        checks.append(Check(False, "task.yaml environment.allow_internet is explicitly set"))
    elif not isinstance(environment_cfg["allow_internet"], bool):
        checks.append(Check(False, "task.yaml environment.allow_internet is a YAML boolean"))
    else:
        checks.append(Check(True, "task.yaml environment.allow_internet is a YAML boolean"))

    build_context, dockerfile, compose_error = _build_context(task_dir, harbor_compatible=harbor_compatible)
    if compose_error:
        checks.append(Check(False, compose_error))
    if harbor_compatible and (task_dir / "environment" / "docker-compose.yaml").is_file():
        checks.append(Check(
            False,
            "Harbor-compatible task must remove legacy environment/docker-compose.yaml; "
            "run template fill --overwrite",
        ))
    if dockerfile.exists():
        text = dockerfile.read_text()
        checks.extend(_dockerfile_context_checks(dockerfile, build_context))
        for gold_dir in GOLD_REFERENCE_DIRS:
            if not (task_dir / gold_dir).exists():
                continue
            copied = _dockerfile_copies_dir(text, gold_dir)
            if copied:
                checks.append(Check(
                    ok=True,
                    message=f"environment/Dockerfile COPYs {gold_dir}/ into the image (agent shell could read "
                    f"it; acceptable under current isolation model, but review before publishing)",
                    advisory=True,
                ))
            else:
                checks.append(Check(True, f"environment/Dockerfile does not COPY {gold_dir}/ into the image"))
    else:
        checks.append(Check(False, "environment/Dockerfile exists"))

    evaluator_py = task_dir / "evaluator" / "evaluator.py"
    checks.append(Check(evaluator_py.exists(), "evaluator/evaluator.py exists"))
    checks.append(_checks_yaml_check(task_dir / "evaluator" / "checks.yaml"))
    checks.append(Check(shared_evaluate_path().is_file(), "shared dumatebench/evaluator/evaluate.py exists"))

    compose_file = task_dir / "environment" / "docker-compose.yaml"
    if compose_file.is_file():
        checks.append(Check(
            ok=True,
            message="environment/docker-compose.yaml exists (legacy task compose; retained for compatibility)",
            advisory=True,
        ))
    else:
        # Filled tasks intentionally omit a task-authored compose file. The
        # local adapter writes .dumate-compose.yaml and Harbor supplies its
        # own base overlay, so requiring this legacy file makes `template
        # fill` output fail its own package check.
        checks.append(Check(
            ok=True,
            message=(
                "environment/docker-compose.yaml not present (optional; "
                "dumate run/Harbor supplies the compose definition)"
            ),
        ))

    passed = all(c.ok for c in checks)
    return CheckResult(passed=passed, checks=checks)
