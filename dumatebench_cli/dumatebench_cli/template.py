"""Fill a task directory's missing ``environment/`` from a template task.

Tasks distributed without their own Docker/fault-injection setup (e.g.
``final_dataset_clean``) rely on a shared template task's ``environment/``
plus its ``network_faults.yaml``/``tool_faults.yaml``. ``dumatebench/scripts/
run_task_batch.py``'s ``prepare_runtime()`` does this copy into a throwaway
``.batch_runtime/`` directory on every run. This module performs the same
copy once, permanently, directly into the task directory, so the result is a
standalone task package that ``harbor_export.py`` (and any other consumer)
can treat like a task that already had its own ``environment/``.

Unlike the pre-Harbor design, the filled ``environment/`` is a *fully
self-contained Docker build context*: every file the Dockerfile ``COPY``s
lives inside ``environment/`` itself (task-root files land under
``environment/task_root/``). This matches Harbor's ``DockerEnvironment``,
which hardcodes the ``main`` service's build context to the task's
``environment/`` directory and cannot see anything outside it -- see
``harbor/environments/docker/docker.py``'s ``context_dir=environment_dir``.
No task-authored ``docker-compose.yaml`` is generated any more: Harbor's own
base compose overlay already defines the ``main`` service, and
``dumatebench_cli.adapter.compose_cmd`` generates an equivalent one for
``dumate run`` (see ``adapter.py``), so both paths build the same
``environment/Dockerfile`` under the same service name.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

_IGNORE_NAMES = {".DS_Store", "__pycache__", ".pytest_cache", "run_outputs", "run_logs"}
FAULT_CONFIG_FILES = ("network_faults.yaml", "tool_faults.yaml")
TASK_ROOT_DIRNAME = "task_root"

# Task-root files the template Dockerfile COPYs in are relocated under
# `environment/task_root/` (see `fill_task`) so the whole build context stays
# inside `environment/`. `evaluator/` is dropped -- it becomes Harbor's
# `tests/` and is injected at verify time, not baked into the image (see
# `harbor_export.py`). `instruction.md` is dropped -- it's read on the host
# side to build agent prompts, never needed inside the container. The two
# `agents/*.py` lines copy the internal ReAct harness, which the adapter
# protocol runtime never invokes (dumatebench_cli drives tasks purely via
# stdin/stdout, not by exec'ing those scripts in-container) -- dropped rather
# than rewritten, so filled tasks don't need their own copy of `agents/`.
_DROP_COPY_SUFFIXES = ("instruction.md", "evaluator/")
_DROP_COPY_SOURCES = ("agents/command_agent.py", "agents/native_agent.py")
_TASK_ROOT_COPY_SUFFIXES = ("task.yaml",) + FAULT_CONFIG_FILES

# Mirrors run_task_batch.py's _write_generic_setup(): the template's own
# setup.sh bakes in smoke-task-specific fixture noise (e.g. copying
# meeting_agenda.pdf), which doesn't exist in other tasks' workspace_seed/.
# A filled task gets this generic version instead, unless it ships its own.
GENERIC_SETUP_SH = """#!/usr/bin/env bash
set -euo pipefail

mkdir -p /workspace /outputs /logs
rm -rf /workspace/*
cp -a /workspace_seed/. /workspace/

chown -R agent:agent /workspace /outputs /logs
"""


class TemplateFillError(RuntimeError):
    """Raised when a task or template directory cannot be filled."""


@dataclass
class FillResult:
    task_id: str
    filled: bool
    warnings: list[str] = field(default_factory=list)


def _ignore_noise(dir_name: str, names: list[str]) -> set[str]:
    del dir_name
    return {
        name
        for name in names
        if name in _IGNORE_NAMES or name.endswith(".pyc") or name.startswith("._")
    }


def needs_fill(task_dir: Path) -> bool:
    return not (task_dir / "environment").is_dir()


def _rewrite_dockerfile(text: str) -> str:
    """Rewrite the template Dockerfile's COPY sources for a self-contained ``environment/``.

    The template's Dockerfile is authored assuming it is built with the
    dumatebench package root as context (paths like
    ``datasets/dev/<task_id>/instruction.md`` or
    ``datasets/dev/<task_id>/environment/setup.sh``). A filled task
    instead builds with ``environment/`` itself as context (matching
    Harbor's hardcoded ``main``-service context), so every COPY source must
    resolve inside ``environment/``:

    - Sources under ``.../environment/...`` lose that prefix (the context
      root already *is* that directory).
    - ``workspace_seed/`` loses its package-root-relative prefix entirely and
      resolves to a bare ``workspace_seed/`` -- ``fill_task`` moves the task's
      ``workspace_seed/`` directly into ``environment/workspace_seed/`` (no
      ``task_root/`` intermediary), matching terminal-bench 2.1's layout.
    - Task-root sources (``task.yaml``, fault configs) are redirected under
      ``environment/task_root/`` (see ``_TASK_ROOT_COPY_SUFFIXES``;
      ``fill_task`` populates that directory).
    - ``instruction.md``/``evaluator/`` lines are dropped entirely (see
      ``_DROP_COPY_SUFFIXES``) -- neither is needed inside the agent image.
    - The two ``agents/*.py`` COPY lines are dropped entirely (see
      ``_DROP_COPY_SOURCES``), so their destinations -- referenced later by a
      ``RUN chmod +x ...`` line -- must be dropped from that line too, or the
      build fails on a missing file.
    """
    drop_dests = set()
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            out.append(line)
            continue
        if any(stripped.startswith(f"COPY {src}") for src in _DROP_COPY_SOURCES):
            drop_dests.add(stripped.split()[-1])
            continue
        parts = stripped.split()
        if len(parts) != 3:
            out.append(line)
            continue
        _, source, dest = parts
        if any(source.endswith(suffix) for suffix in _DROP_COPY_SUFFIXES):
            drop_dests.add(dest)
            continue
        if "/environment/" in source:
            rewritten = source.split("/environment/", 1)[1]
            out.append(line.replace(source, rewritten, 1))
            continue
        if source.endswith("workspace_seed/"):
            out.append(line.replace(source, "workspace_seed/", 1))
            continue
        for suffix in _TASK_ROOT_COPY_SUFFIXES:
            if source.endswith(suffix):
                out.append(line.replace(source, f"{TASK_ROOT_DIRNAME}/{suffix}", 1))
                break
        else:
            out.append(line)

    if not drop_dests:
        return "".join(out)

    text = "".join(out)
    for dest in drop_dests:
        text = re.sub(rf"[ \t]*{re.escape(dest)}[ \t]*\\\n", "\n", text)
    return text


def fill_task(
    task_dir: Path, template_dir: Path, *, overwrite: bool = False, reuse_template_setup: bool = False
) -> FillResult:
    """Copy ``environment/`` from template_dir into task_dir, self-contained.

    Skips (returns ``filled=False``) if task_dir already has an
    ``environment/`` directory, unless ``overwrite`` is set.

    Besides the template's own ``environment/`` contents, this also moves the
    task's own ``workspace_seed/`` directly into ``environment/workspace_seed/``
    (no intermediary -- the task-root original is deleted, matching
    terminal-bench 2.1's layout) and populates ``environment/task_root/``
    with the task's ``task.yaml`` and fault-config files, since the rewritten
    Dockerfile (see ``_rewrite_dockerfile``) ``COPY``s them from there --
    Harbor's ``main``-service build context is ``environment/`` itself and
    cannot reach anything outside it. No ``docker-compose.yaml`` is written
    any more: Harbor's own base overlay and
    ``dumatebench_cli.adapter.compose_cmd`` (for ``dumate run``) both supply
    an equivalent ``main`` service on top of this same ``environment/``.
    ``evaluator/`` is deliberately left out of ``task_root/`` -- it is
    exported as Harbor's ``tests/`` and injected at verify time instead (see
    ``harbor_export.py``), never baked into the image.

    A task's root-level fault-config files are also (re)copied from the
    template when missing, since ``dumate run`` reads them directly from the
    task root. Unless ``reuse_template_setup`` is set, ``setup.sh`` is
    replaced with a generic version (mirroring ``prepare_runtime()``'s
    default), since the template's own ``setup.sh`` may bake in fixture
    noise specific to the template task's own ``workspace_seed/``.
    """
    task_dir = task_dir.resolve()
    template_dir = template_dir.resolve()
    task_id = task_dir.name
    warnings: list[str] = []

    if not task_dir.is_dir():
        raise TemplateFillError(f"Task directory does not exist: {task_dir}")
    template_env = template_dir / "environment"
    if not template_env.is_dir():
        raise TemplateFillError(f"Template has no environment/: {template_env}")

    task_env = task_dir / "environment"
    if task_env.is_dir() and not overwrite:
        return FillResult(task_id=task_id, filled=False, warnings=["environment/ already present, skipped"])

    if task_env.exists():
        shutil.rmtree(task_env)
    shutil.copytree(template_env, task_env, ignore=_ignore_noise)

    dockerfile = task_env / "Dockerfile"
    if dockerfile.is_file():
        dockerfile.write_text(_rewrite_dockerfile(dockerfile.read_text(encoding="utf-8")), encoding="utf-8")
    else:
        warnings.append("template environment/ has no Dockerfile")

    # The template's own docker-compose.yaml assumes a package-root-relative
    # build context (e.g. `context: ../../../..`) that is meaningless once
    # relocated into a filled task -- and, worse, Harbor treats any
    # environment/docker-compose.yaml as an override merged on top of its own
    # base overlay, so a stale copy of it breaks the `main` service's build
    # context there too. Neither `dumate run` (via `adapter.py`'s
    # `compose_cmd`, generated fresh per invocation) nor `harbor run` (via its
    # own base overlay) needs a task-authored compose file any more.
    stale_compose = task_env / "docker-compose.yaml"
    if stale_compose.exists():
        stale_compose.unlink()

    if not reuse_template_setup:
        setup_sh = task_env / "setup.sh"
        setup_sh.write_text(GENERIC_SETUP_SH, encoding="utf-8")
        setup_sh.chmod(0o755)

    for name in FAULT_CONFIG_FILES:
        source = template_dir / name
        dest = task_dir / name
        if not source.is_file():
            warnings.append(f"template is missing {name}, task will not have it either")
            continue
        if dest.exists() and not overwrite:
            continue
        shutil.copy2(source, dest)

    task_root_dir = task_env / TASK_ROOT_DIRNAME
    task_root_dir.mkdir(parents=True, exist_ok=True)

    workspace_seed = task_dir / "workspace_seed"
    target_seed = task_env / "workspace_seed"
    if workspace_seed.is_dir():
        if target_seed.exists():
            shutil.rmtree(target_seed)
        shutil.move(str(workspace_seed), str(target_seed))
    else:
        warnings.append("task has no workspace_seed/; image build will fail on that COPY")

    task_yaml = task_dir / "task.yaml"
    if task_yaml.is_file():
        shutil.copy2(task_yaml, task_root_dir / "task.yaml")
    else:
        warnings.append("task has no task.yaml; image build will fail on that COPY")

    for name in FAULT_CONFIG_FILES:
        source = task_dir / name
        if source.is_file():
            shutil.copy2(source, task_root_dir / name)
        else:
            warnings.append(f"task has no {name}; image build will fail on that COPY")

    if not (task_dir / "evaluator" / "evaluator.py").exists():
        warnings.append("evaluator/evaluator.py not found; task will fail at evaluation time")

    return FillResult(task_id=task_id, filled=True, warnings=warnings)


def fill_batch(
    tasks_root: Path,
    template_dir: Path,
    *,
    task_glob: str = "*",
    overwrite: bool = False,
    reuse_template_setup: bool = False,
) -> list[FillResult]:
    from dumatebench_cli.runner import discover_tasks

    task_dirs = discover_tasks(tasks_root, task_glob=task_glob, recursive=True)
    if not task_dirs:
        raise TemplateFillError(f"No task directories found under {tasks_root} (glob={task_glob!r}).")

    results: list[FillResult] = []
    for task_dir in task_dirs:
        if task_dir.resolve() == template_dir.resolve():
            continue
        results.append(
            fill_task(task_dir, template_dir, overwrite=overwrite, reuse_template_setup=reuse_template_setup)
        )
    return results
