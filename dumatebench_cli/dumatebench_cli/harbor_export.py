"""Export DuMateBench task directories into Harbor ``task.toml`` packages.

Reads each task's ``task.yaml`` (dumatebench's own schema) and writes a
sibling Harbor-native package: ``task.toml`` (schema_version "1.4") plus
``tests/test.sh``. dumatebench-specific fields that Harbor's task.toml has no
slot for -- ``network_faults``, ``tool_faults``, and any other dumatebench
runner/evaluator-only config -- are intentionally left out of task.toml; they
stay meaningful only because ``setup.sh``/``entrypoint.sh`` inside the copied
``environment/`` still consume the original ``task.yaml`` at container
build/run time.

``task.toml``'s ``[environment]`` deliberately omits ``docker_image``: Harbor
only skips building and pulls a prebuilt image when that key is set (see
``should_use_prebuilt_docker_image()``), and every exported task here does
have a real, buildable ``environment/Dockerfile`` (from ``template.py``'s
``fill_task``). Leaving it unset makes plain ``harbor run`` build from
source, matching terminal-bench 2.1's default and avoiding a spurious
"pull this nonexistent image" attempt.

``evaluator/`` is copied into ``tests/`` alongside the generated
``tests/test.sh`` rather than into the image at build time: Harbor's own
verifier already ``docker compose cp``s ``tests/`` into the running ``main``
container right before running ``test.sh`` (see
``harbor/verifier/verifier.py``'s use of ``environment.upload_dir()``), so
this needs no custom injection code -- it only needs the files to exist at
the Harbor-expected path in the exported package.

``tests/test.sh`` reuses the reward-filtering shim verified in the Harbor
bridge-agent smoke test: it stages ``checks.yaml`` back under
``/opt/dumate/task/evaluator/`` (the path ``evaluator.py`` itself hardcodes,
unchanged from the pre-Harbor layout) before running ``evaluator.py``
(uploaded to ``/tests/evaluator.py`` by Harbor's verifier) against
``/opt/dumate/task``, then copies only the numeric fields of
``run_outputs/reward.json`` (dumatebench's
``complete_pass``/``partial_pass``/etc.) into ``/logs/verifier/reward.json``,
which is the file Harbor's verifier reads with priority over
``reward.txt``.

Every task's ``evaluator/evaluator.py`` also imports a *shared*
``evaluate.py`` (dumatebench's per-check implementation functions, one copy
for the whole dataset, living at ``dumatebench/evaluator/evaluate.py``) via
an ancestor-directory search that assumes the task lives inside the
dumatebench repo tree. Harbor tasks run in a container that never has that
tree, so this export copies ``evaluate.py`` alongside ``evaluator.py`` into
``tests/`` and points ``DUMATE_EVALUATE_PY`` at the uploaded copy
(``/tests/evaluate.py``) in ``test.sh``, short-circuiting the ancestor
search instead of trying to reproduce the repo layout inside the container.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_SHARED_EVALUATE_PY = Path(__file__).resolve().parents[2] / "dumatebench" / "evaluator" / "evaluate.py"

TASK_TOML_TEMPLATE = """schema_version = "1.4"
artifacts = [{{ source = "/outputs", destination = "outputs" }}]

[task]
name = "{name}"
version = "1.0.0"
description = "{description}"
authors = []
keywords = {keywords}

[metadata]

[verifier]
timeout_sec = {verifier_timeout_sec}
collect = []

[verifier.env]

[agent]
timeout_sec = {agent_timeout_sec}
user = "{agent_user}"

[environment]
network_mode = "{network_mode}"
build_timeout_sec = 600.0
os = "linux"
workdir = "{workdir}"
mcp_servers = []
{resource_settings}

[environment.env]

[solution.env]
"""

TEST_SH_TEMPLATE = """#!/bin/bash
set -e

mkdir -p /opt/dumate/task/evaluator
cp /tests/checks.yaml /opt/dumate/task/evaluator/checks.yaml
DUMATE_EVALUATE_PY=/tests/evaluate.py python3 /tests/evaluator.py --task-dir /opt/dumate/task || true
python3 -c "
import json
raw = json.load(open('/opt/dumate/task/run_outputs/reward.json'))
numeric = {k: v for k, v in raw.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
json.dump(numeric, open('/logs/verifier/reward.json', 'w'))
"
"""

REQUIRED_TASK_YAML_KEYS = ("task_id",)
_EXPORT_IGNORE_NAMES = {"run_outputs", "run_logs", ".batch_runtime", "__pycache__", ".pytest_cache"}


class HarborExportError(RuntimeError):
    """Raised when a task directory cannot be exported to task.toml."""


@dataclass
class ExportResult:
    task_id: str
    output_dir: Path
    warnings: list[str] = field(default_factory=list)


def _ignore_runtime_state(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in _EXPORT_IGNORE_NAMES or name.endswith(".pyc") or name.startswith("._")
    }


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_string_array(values: list[str]) -> str:
    return "[" + ", ".join(f'"{_toml_escape(v)}"' for v in values) + "]"


def _environment_resource_settings(
    env_cfg: dict[str, Any], task_id: str, warnings: list[str]
) -> str:
    """Render optional DuMateBench resource limits into Harbor task fields."""
    settings: list[str] = []
    for key in ("cpus", "memory_mb", "storage_mb", "gpus"):
        value = env_cfg.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            warnings.append(f"{task_id}: environment.{key} is invalid, omitting it")
            continue
        if key != "cpus" and not isinstance(value, int):
            warnings.append(f"{task_id}: environment.{key} must be an integer, omitting it")
            continue
        settings.append(f"{key} = {value}")
    return "\n".join(settings)


def load_task_yaml(task_dir: Path) -> dict[str, Any]:
    path = task_dir / "task.yaml"
    if not path.is_file():
        raise HarborExportError(f"{task_dir}: missing task.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise HarborExportError(f"{task_dir}: task.yaml must contain a mapping")
    missing = [key for key in REQUIRED_TASK_YAML_KEYS if key not in data]
    if missing:
        raise HarborExportError(f"{task_dir}: task.yaml missing required keys: {missing}")
    return data


def render_task_toml(task_yaml: dict[str, Any], warnings: list[str]) -> str:
    """Map dumatebench task.yaml fields directly onto task.toml fields.

    dumatebench-specific fields (network_faults, tool_faults, noise config,
    the whole ``complexity``/``evaluation`` sections) are not translated --
    they have no Harbor equivalent and remain in the original task.yaml,
    which ships alongside task.toml in the exported directory.
    """
    agent_cfg = task_yaml.get("agent") or {}
    env_cfg = task_yaml.get("environment") or {}

    task_id = str(task_yaml["task_id"])
    task_name = str(task_yaml.get("task_name") or task_id)
    tags = task_yaml.get("tags") or []
    if not isinstance(tags, list):
        warnings.append(f"{task_id}: tags is not a list, ignoring for keywords")
        tags = []

    agent_timeout = agent_cfg.get("timeout_sec")
    if agent_timeout is None:
        warnings.append(f"{task_id}: agent.timeout_sec missing, defaulting to 900.0")
        agent_timeout = 900
    agent_user = agent_cfg.get("user") or "agent"

    workdir = agent_cfg.get("workdir") or "/workspace"
    allow_internet = env_cfg.get("allow_internet", True)
    network_mode = "public" if allow_internet else "none"
    if "allow_internet" not in env_cfg:
        warnings.append(f"{task_id}: environment.allow_internet missing, defaulting network_mode to public")

    verifier_timeout = agent_timeout + 300

    return TASK_TOML_TEMPLATE.format(
        name=_toml_escape(f"dumate/{task_id}"),
        description=_toml_escape(task_name),
        keywords=_toml_string_array([str(t) for t in tags]),
        verifier_timeout_sec=float(verifier_timeout),
        agent_timeout_sec=float(agent_timeout),
        agent_user=_toml_escape(str(agent_user)),
        network_mode=network_mode,
        workdir=_toml_escape(str(workdir)),
        resource_settings=_environment_resource_settings(env_cfg, task_id, warnings),
    )


def export_task(task_dir: Path, output_dir: Path, *, overwrite: bool = False) -> ExportResult:
    """Export one dumatebench task directory into a Harbor task.toml package at output_dir.

    Copies the task directory as-is (instruction.md, task.yaml, environment/,
    evaluator/, workspace_seed/, etc. all travel together, same as
    dumatebench's existing non-hidden distribution posture) then adds
    task.toml and tests/test.sh on top.
    """
    task_dir = task_dir.resolve()
    warnings: list[str] = []
    task_yaml = load_task_yaml(task_dir)
    task_id = str(task_yaml["task_id"])

    if output_dir.exists():
        if not overwrite:
            raise HarborExportError(f"Output directory already exists, refusing to overwrite: {output_dir}")
        shutil.rmtree(output_dir)

    shutil.copytree(task_dir, output_dir, ignore=_ignore_runtime_state)

    (output_dir / "task.toml").write_text(render_task_toml(task_yaml, warnings), encoding="utf-8")

    tests_dir = output_dir / "tests"
    tests_dir.mkdir(exist_ok=True)

    evaluator_dir = output_dir / "evaluator"
    if evaluator_dir.is_dir():
        for item in evaluator_dir.iterdir():
            dest = tests_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
    else:
        warnings.append(f"{task_id}: evaluator/ not found; tests/test.sh will fail at run time")

    test_sh = tests_dir / "test.sh"
    test_sh.write_text(TEST_SH_TEMPLATE, encoding="utf-8")
    test_sh.chmod(0o755)

    if not (tests_dir / "evaluator.py").exists():
        warnings.append(f"{task_id}: evaluator/evaluator.py not found; tests/test.sh will fail at run time")

    if _SHARED_EVALUATE_PY.is_file():
        shutil.copy2(_SHARED_EVALUATE_PY, tests_dir / "evaluate.py")
    else:
        warnings.append(f"{task_id}: shared evaluate.py not found at {_SHARED_EVALUATE_PY}; tests/test.sh will fail at run time")

    return ExportResult(task_id=task_id, output_dir=output_dir, warnings=warnings)


def export_batch(tasks_root: Path, output_root: Path, *, task_glob: str = "*", overwrite: bool = False) -> list[ExportResult]:
    from dumatebench_cli.runner import discover_tasks

    task_dirs = discover_tasks(tasks_root, task_glob=task_glob, recursive=True)
    if not task_dirs:
        raise HarborExportError(f"No task directories found under {tasks_root} (glob={task_glob!r}).")

    results: list[ExportResult] = []
    for task_dir in task_dirs:
        results.append(export_task(task_dir, output_root / task_dir.name, overwrite=overwrite))
    return results
