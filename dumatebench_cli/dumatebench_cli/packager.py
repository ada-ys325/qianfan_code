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

import re
from dataclasses import dataclass, field
from pathlib import Path

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


def check_task_dir(task_dir: Path) -> CheckResult:
    checks: list[Check] = []

    for name in REQUIRED_FILES:
        checks.append(Check((task_dir / name).exists(), f"{name} exists"))

    for name in REQUIRED_DIRS:
        checks.append(Check((task_dir / name).is_dir(), f"{name}/ exists"))

    dockerfile = task_dir / "environment" / "Dockerfile"
    if dockerfile.exists():
        text = dockerfile.read_text()
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
