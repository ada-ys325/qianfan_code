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
``/opt/dumate/task``, then writes the aggregate score from
``run_outputs/reward.json`` into ``/logs/verifier/reward.json``. Harbor 0.5
requires one scalar reward per trial; the complete DuMateBench report remains
in ``run_outputs``.
When ``OPENAI_API_KEY`` and a judge model are supplied, the generated verifier
also runs the unified LLM judge and includes its normalized score fields.

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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dumatebench_cli.packager import check_task_dir
from dumatebench_cli.task_metadata import (
    TaskMetadataError,
    load_task_metadata,
    shared_evaluate_path,
    task_id_from_yaml,
)

_EVALUATOR_ROOT = Path(__file__).resolve().parents[2] / "dumatebench" / "evaluator"
_DUMATEBENCH_REQUIREMENTS = _EVALUATOR_ROOT.parent / "requirements.txt"
_SHARED_EVALUATE_PY = _EVALUATOR_ROOT / "evaluate.py"

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
OPENAI_API_KEY = "${{OPENAI_API_KEY:-}}"
OPENAI_BASE_URL = "${{OPENAI_BASE_URL:-}}"
OPENAI_MODEL = "${{OPENAI_MODEL:-}}"
DUMATE_LLM_JUDGE_MODEL = "${{DUMATE_LLM_JUDGE_MODEL:-}}"

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
cp -a /tests/task_evaluator/. /opt/dumate/task/evaluator/
cp /tests/instruction.md /opt/dumate/task/instruction.md
if [ -d /tests/web_reference ]; then
  rm -rf /opt/dumate/task/web_reference
  cp -a /tests/web_reference /opt/dumate/task/web_reference
fi
DUMATE_EVALUATE_PY=/tests/evaluate.py python3 /tests/evaluator.py --task-dir /opt/dumate/task || true
if [ -n "${OPENAI_API_KEY:-}" ]; then
  # Harbor runs the verifier inside the task image, which may not include
  # DuMateBench's host-side judge dependencies. Install them only when a judge
  # is enabled and imports are missing; preserve the deterministic reward if
  # the external judge or package index is unavailable.
  if ! python3 -c "import openai, openpyxl, docx, pptx, PIL, pypdf, fitz, yaml" >/dev/null 2>&1; then
    python3 -m pip install --disable-pip-version-check --quiet \
      -r /tests/llm_judge_requirements.txt \
      > /logs/verifier/llm_judge.install.log 2>&1 || true
  fi
  # The runner defaults to gpt-4o when no model variable is supplied. Keep
  # stderr as an artifact so a transient/API configuration failure is visible
  # while preserving the deterministic DuMateBench reward.
  PYTHONPATH=/tests DUMATE_EVALUATE_PY=/tests/evaluate.py \
    python3 /tests/run_harbor_llm_judge.py --task-dir /opt/dumate/task \
    --model "${DUMATE_LLM_JUDGE_MODEL:-${OPENAI_MODEL:-gpt-4o}}" \
    --base-url "${OPENAI_BASE_URL:-}" \
    2>/logs/verifier/llm_judge.stderr.log || true
fi
python3 -c "
import json
import shutil
from pathlib import Path
root = Path('/opt/dumate/task/run_outputs')
path = root / 'reward_with_llm_judge.json'
if not path.is_file():
    path = root / 'reward.json'
raw = json.load(path.open())
# Harbor 0.5's built-in mean metric accepts exactly one scalar per trial.
# Keep the complete DuMateBench report in the task outputs and expose its
# aggregate score as Harbor's scalar reward.
score = raw.get('final_score')
if not isinstance(score, (int, float)) or isinstance(score, bool):
    score = raw.get('partial_pass', 0.0)
json.dump({'reward': score}, open('/logs/verifier/reward.json', 'w'))
artifact_name = (
    'reward_with_llm_judge.json'
    if path.name == 'reward_with_llm_judge.json'
    else 'dumate_reward.json'
)
artifact = Path('/logs/verifier') / artifact_name
artifact.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(path, artifact)
judge_outputs = root / 'llm_judge_scores'
if judge_outputs.is_dir():
    shutil.copytree(
        judge_outputs,
        Path('/logs/verifier/llm_judge_scores'),
        dirs_exist_ok=True,
    )
"
"""

_EXPORT_IGNORE_NAMES = {
    "run_outputs",
    "run_logs",
    ".batch_runtime",
    ".dumate-compose.yaml",
    "__pycache__",
    ".pytest_cache",
}


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


def _make_harbor_package_portable(output_dir: Path, source_task_dir: Path) -> None:
    """Rewrite the source-repo Docker files for a standalone Harbor package."""
    environment = output_dir / "environment"
    dockerfile = environment / "Dockerfile"
    compose = environment / "docker-compose.yaml"
    if dockerfile.is_file():
        text = dockerfile.read_text(encoding="utf-8")
        try:
            source_prefix = source_task_dir.relative_to(_EVALUATOR_ROOT.parent).as_posix()
        except ValueError:
            source_prefix = ""
        text = text.replace(f"datasets/dev/template_task/", "")
        if source_prefix:
            text = text.replace(f"{source_prefix}/", "")
        # The unified judge dispatches by artifact type.  Keep every judge
        # backend usable in a standalone Harbor image, including optional
        # document/image/PDF readers and PPT/PDF rendering tools.
        if "      libreoffice \\" not in text:
            text = text.replace(
                "      poppler-utils \\\n",
                "      poppler-utils \\\n      libreoffice \\\n      ffmpeg \\\n",
                1,
            )
        if "pip install --no-cache-dir \"openai>=1.0.0\"" not in text:
            text = text.replace(
                "\nRUN useradd -ms /bin/bash agent",
                "\nRUN pip install --no-cache-dir \\\n      \"openai>=1.0.0\" \\\n      \"openpyxl>=3.1.5\" \\\n      \"python-docx>=1.1.2\" \\\n      \"python-pptx>=1.0.2\" \\\n      \"Pillow>=9.0\" \\\n      \"pypdf>=5.0\" \\\n      \"PyMuPDF>=1.24\" \\\n      \"PyYAML>=6.0\" \\\n      \"icalendar>=5.0.13\" \\\n      \"pytz>=2024.2\" \\\n      \"cairosvg>=2.7\"\n\nRUN useradd -ms /bin/bash agent",
                1,
            )
        dockerfile.write_text(text, encoding="utf-8")
    if compose.is_file():
        text = compose.read_text(encoding="utf-8")
        # Harbor's Docker environment contract names the task service `main`.
        text = re.sub(r"(?m)^(\s*)task:\s*$", r"\1main:", text, count=1)
        text = re.sub(r"(?m)^(\s*)context:\s*.*$", r"\1context: ..", text)
        text = re.sub(r"(?m)^(\s*)dockerfile:\s*.*$", r"\1dockerfile: environment/Dockerfile", text)
        compose.write_text(text, encoding="utf-8")


def load_task_yaml(task_dir: Path) -> dict[str, Any]:
    try:
        data, _task_id = load_task_metadata(task_dir)
    except TaskMetadataError as exc:
        raise HarborExportError(str(exc)) from exc
    return data


def render_task_toml(task_yaml: dict[str, Any], warnings: list[str]) -> str:
    """Map dumatebench task.yaml fields directly onto task.toml fields.

    dumatebench-specific fields (network_faults, tool_faults, noise config,
    the whole ``complexity``/``evaluation`` sections) are not translated --
    they have no Harbor equivalent and remain in the original task.yaml,
    which ships alongside task.toml in the exported directory.
    """
    agent_cfg = task_yaml.get("agent") or {}
    env_cfg = dict(task_yaml.get("environment") or {})
    # DuMateBench paper defaults: 2 vCPU, 8 GiB RAM, and 12 GiB workspace.
    # Task-specific values still take precedence when present.
    env_cfg.setdefault("cpus", 2)
    env_cfg.setdefault("memory_mb", 8192)
    env_cfg.setdefault("storage_mb", 12000)

    task_id = str(task_yaml["task_id"])
    task_name = str(task_yaml.get("task_name") or task_id)
    tags = task_yaml.get("tags") or []
    if not isinstance(tags, list):
        warnings.append(f"{task_id}: tags is not a list, ignoring for keywords")
        tags = []

    agent_timeout = agent_cfg.get("timeout_sec")
    if agent_timeout is None:
        warnings.append(f"{task_id}: agent.timeout_sec missing, defaulting to 1800.0")
        agent_timeout = 1800
    agent_user = agent_cfg.get("user") or "agent"

    workdir = agent_cfg.get("workdir") or "/workspace"
    if "allow_internet" not in env_cfg:
        raise HarborExportError(f"{task_id}: environment.allow_internet must be explicitly set to true or false")
    allow_internet = env_cfg["allow_internet"]
    if not isinstance(allow_internet, bool):
        raise HarborExportError(
            f"{task_id}: environment.allow_internet must be a YAML boolean, "
            f"not {type(allow_internet).__name__}"
        )
    network_mode = "public" if allow_internet else "no-network"

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
    task_id = task_id_from_yaml(task_yaml, task_dir)

    package_result = check_task_dir(task_dir, harbor_compatible=True)
    if not package_result.passed:
        failures = [check.message for check in package_result.checks if not check.ok]
        raise HarborExportError(f"{task_id}: package check failed: {'; '.join(failures)}")

    if output_dir.exists():
        if not overwrite:
            raise HarborExportError(f"Output directory already exists, refusing to overwrite: {output_dir}")
        shutil.rmtree(output_dir)

    shutil.copytree(task_dir, output_dir, ignore=_ignore_runtime_state)

    # The source Dockerfile imports the shared agent runners from the repo
    # root. Include them in the exported package and rewrite source-relative
    # COPY/build-context paths so the package can be moved anywhere.
    shared_agents = _EVALUATOR_ROOT.parent / "agents"
    if shared_agents.is_dir():
        shutil.copytree(
            shared_agents,
            output_dir / "agents",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    _make_harbor_package_portable(output_dir, task_dir)

    (output_dir / "task.toml").write_text(render_task_toml(task_yaml, warnings), encoding="utf-8")

    tests_dir = output_dir / "tests"
    tests_dir.mkdir(exist_ok=True)

    instruction_file = task_dir / "instruction.md"
    if instruction_file.is_file():
        shutil.copy2(instruction_file, tests_dir / "instruction.md")
    else:
        warnings.append(f"{task_id}: instruction.md not found; Harbor judge will not have task instructions")

    web_reference_dir = task_dir / "web_reference"
    if web_reference_dir.is_dir():
        shutil.copytree(web_reference_dir, tests_dir / "web_reference", dirs_exist_ok=True)

    evaluator_dir = output_dir / "evaluator"
    if evaluator_dir.is_dir():
        task_evaluator_dir = tests_dir / "task_evaluator"
        task_evaluator_dir.mkdir(exist_ok=True)
        for item in evaluator_dir.iterdir():
            dest = tests_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
                if item.suffix.lower() in {".json", ".yaml", ".yml"}:
                    shutil.copy2(item, task_evaluator_dir / item.name)
    else:
        warnings.append(f"{task_id}: evaluator/ not found; tests/test.sh will fail at run time")

    test_sh = tests_dir / "test.sh"
    test_sh.write_text(TEST_SH_TEMPLATE, encoding="utf-8")
    test_sh.chmod(0o755)

    shutil.copy2(_SHARED_EVALUATE_PY, tests_dir / "evaluate.py")

    if _DUMATEBENCH_REQUIREMENTS.is_file():
        shutil.copy2(_DUMATEBENCH_REQUIREMENTS, tests_dir / "llm_judge_requirements.txt")
    else:
        warnings.append(
            f"{task_id}: DuMateBench requirements not found at {_DUMATEBENCH_REQUIREMENTS}; "
            "Harbor judge may lack Python dependencies"
        )

    # The Harbor verifier runs inside the task image. Ship the judge package
    # and its small runner beside test.sh so PYTHONPATH=/tests is sufficient.
    judge_package = tests_dir / "dumatebench" / "evaluator"
    if _EVALUATOR_ROOT.is_dir():
        shutil.copytree(_EVALUATOR_ROOT, judge_package, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", "LLM Judge doc"))
    harbor_judge = Path(__file__).resolve().parents[2] / "dumatebench" / "scripts" / "run_harbor_llm_judge.py"
    if harbor_judge.is_file():
        shutil.copy2(harbor_judge, tests_dir / "run_harbor_llm_judge.py")
    else:
        warnings.append(f"{task_id}: Harbor judge runner not found at {harbor_judge}")

    return ExportResult(task_id=task_id, output_dir=output_dir, warnings=warnings)


def export_batch(
    tasks_root: Path,
    output_root: Path,
    *,
    task_glob: str = "*",
    overwrite: bool = False,
) -> list[ExportResult]:
    from dumatebench_cli.runner import discover_tasks

    task_dirs = discover_tasks(tasks_root, task_glob=task_glob, recursive=True)
    if not task_dirs:
        raise HarborExportError(f"No task directories found under {tasks_root} (glob={task_glob!r}).")

    results: list[ExportResult] = []
    for task_dir in task_dirs:
        task_yaml = load_task_yaml(task_dir)
        task_id = task_id_from_yaml(task_yaml, task_dir)
        results.append(export_task(task_dir, output_root / task_id, overwrite=overwrite))
    return results
