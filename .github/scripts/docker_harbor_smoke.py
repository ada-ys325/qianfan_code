#!/usr/bin/env python3
"""Run one minimal task through the local and Harbor execution paths.

This is intentionally a script rather than a collected pytest test: ordinary
Python tests stay runnable without Docker, while CI can opt into this smoke
test on runners that provide Docker and Harbor.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 600,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cwd = cwd or REPO_ROOT
    print("+", " ".join(str(part) for part in command), flush=True)
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if check and result.returncode:
        raise RuntimeError(f"command exited with {result.returncode}: {' '.join(command)}")
    return result


def _dump_container_logs(output: str) -> None:
    """Print logs for containers named by Harbor before it cleans them up."""
    names = list(dict.fromkeys(re.findall(r"(?:Container|container)\s+([A-Za-z0-9_.-]+)", output)))
    for name in names:
        result = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=False)
        print(f"--- docker logs {name} (exit {result.returncode}) ---", flush=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)


def _write_fixture(root: Path) -> tuple[Path, Path, Path]:
    # The host-side dumate runner writes the bind-mounted logs after the
    # container exits. Match the fixture user to the runner uid so chown does
    # not make those files inaccessible on Linux CI hosts.
    agent_uid = os.getuid() or 1000
    raw = root / "raw"
    task = raw / "task_1"
    template = root / "template"
    task.mkdir(parents=True)
    (task / "workspace_seed").mkdir()
    (task / "workspace_seed" / "seed.txt").write_text("smoke\n", encoding="utf-8")
    (task / "task.yaml").write_text(
        """task_id: e2e-task
task_name: Minimal Docker and Harbor smoke task
agent:
  timeout_sec: 30
  user: agent
  workdir: /workspace
environment:
  # Keep the smoke test independent of Harbor's optional egress sidecar image.
  allow_internet: true
""",
        encoding="utf-8",
    )
    (task / "instruction.md").write_text(
        "Create a short report that includes the phrase Harbor LLM judge smoke artifact.\n",
        encoding="utf-8",
    )
    (task / "evaluator").mkdir()
    (task / "evaluator" / "checks.yaml").write_text(
        "checks:\n  - id: smoke\n    type: evaluate_file_exist\n", encoding="utf-8"
    )
    (task / "evaluator" / "evaluator.py").write_text(
        """from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--task-dir', default='.')
args = parser.parse_args()
task_dir = Path(args.task_dir).resolve()
out = task_dir / 'run_outputs' / 'reward.json'
out.parent.mkdir(parents=True, exist_ok=True)
artifact = out.parent / 'report.md'
artifact.write_text('Harbor LLM judge smoke artifact\\n', encoding='utf-8')
out.write_text(json.dumps({
    'task_id': 'e2e-task',
    'complete_pass': 1,
    'partial_pass': 1.0,
    'checks': [{'id': 'smoke', 'passed': True, 'detail': json.dumps({'file': 'run_outputs/report.md'})}],
}), encoding='utf-8')
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    rubric = {
        "criteria": [
            {
                "id": criterion_id,
                "dimension": dimension,
                "description": f"Smoke criterion for {criterion_id}.",
                "weight": 1,
                "evidence_required": True,
                "levels": {str(score): f"Level {score}." for score in range(5)},
            }
            for criterion_id, dimension in (
                ("smoke_relevance", "content_relevance"),
                ("smoke_completeness", "requirement_completeness"),
                ("smoke_style", "language_style"),
            )
        ]
    }
    (task / "evaluator" / "llm_judge_criteria.json").write_text(
        json.dumps(rubric, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    environment = template / "environment"
    environment.mkdir(parents=True)
    (environment / "Dockerfile").write_text(
        f"""FROM python:3.12-slim
RUN useradd --uid {agent_uid} -ms /bin/bash agent && mkdir -p /workspace /outputs /logs /opt/dumate/task
COPY workspace_seed/ /workspace_seed/
COPY task.yaml /opt/dumate/task/task.yaml
COPY setup.sh /opt/dumate/setup.sh
COPY entrypoint.sh /opt/dumate/entrypoint.sh
RUN chmod +x /opt/dumate/setup.sh /opt/dumate/entrypoint.sh && \
    chown -R agent:agent /workspace /outputs /logs /workspace_seed /opt/dumate
WORKDIR /workspace
ENTRYPOINT ["/opt/dumate/entrypoint.sh"]
""",
        encoding="utf-8",
    )
    (environment / "setup.sh").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
mkdir -p /workspace /outputs /logs
cp -a /workspace_seed/. /workspace/
chown -R agent:agent /workspace /outputs /logs
# The same bind mounts are written by the host-side dumate runner after the
# container exits. Keep them writable when the host uid differs from agent.
chmod -R a+rwX /outputs /logs
""",
        encoding="utf-8",
    )
    (environment / "entrypoint.sh").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
/opt/dumate/setup.sh
if [ "$#" -eq 0 ]; then
  exec sleep infinity
fi
exec "$@"
""",
        encoding="utf-8",
    )
    (template / "network_faults.yaml").write_text("faults: []\n", encoding="utf-8")
    (template / "tool_faults.yaml").write_text("faults: []\n", encoding="utf-8")
    (root / "mock_openai.py").write_text(
        """from __future__ import annotations

import json
from types import SimpleNamespace


JUDGMENT = {
    "criteria": [
        {
            "id": criterion_id,
            "status": "pass",
            "score": 4,
            "evidence": [{
                "artifact_path": "outputs/report.md",
                "location": "line 1",
                "quote": "Harbor LLM judge smoke artifact",
            }],
            "rationale": "The fixture contains the required smoke artifact.",
            "confidence": 1.0,
        }
        for criterion_id in ("smoke_relevance", "smoke_completeness", "smoke_style")
    ],
    "summary": "Deterministic CI judge response.",
}


class Completions:
    def create(self, *, model: str, messages: list[dict], **_kwargs: object):
        if model != "smoke-model":
            raise RuntimeError(f"unexpected smoke model: {model}")
        prompt = "\\n".join(str(message.get("content", "")) for message in messages)
        if "Harbor LLM judge smoke artifact" not in prompt:
            raise RuntimeError("judge prompt did not contain the candidate artifact")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(JUDGMENT)))]
        )


class OpenAI:
    def __init__(self, *, api_key: str, base_url: str | None = None, **_kwargs: object):
        if api_key != "ci-smoke-key":
            raise RuntimeError("Harbor did not pass the CI judge credential")
        if base_url != "http://ci-mock-openai/v1":
            raise RuntimeError(f"Harbor did not pass the mock judge endpoint: {base_url}")
        self.chat = SimpleNamespace(completions=Completions())
""",
        encoding="utf-8",
    )
    return raw, task, template


def _assert_harbor_results(jobs_dir: Path, output: str) -> None:
    result_files = sorted(jobs_dir.rglob("result.json"))
    if not result_files:
        raise RuntimeError(f"Harbor did not write result.json under {jobs_dir}")
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in result_files]
    error_counts: list[int] = []

    def collect_error_counts(value: object) -> None:
        if isinstance(value, dict):
            for key in ("n_errored_trials", "n_errors", "n_exceptions"):
                count = value.get(key)
                if isinstance(count, int):
                    error_counts.append(count)
            for item in value.values():
                collect_error_counts(item)
        elif isinstance(value, list):
            for item in value:
                collect_error_counts(item)

    for payload in payloads:
        collect_error_counts(payload)
    if error_counts and any(error_counts):
        raise RuntimeError(f"Harbor reported errored trials: {error_counts}")
    if not error_counts and "Exceptions" not in output:
        raise RuntimeError("Harbor results contain no exception count")
    judge_files = sorted(jobs_dir.rglob("reward_with_llm_judge.json"))
    if not judge_files:
        judge_outputs = sorted(jobs_dir.rglob("llm_judge_scores/*.json"))
        details = [f"{path}: {path.read_text(encoding='utf-8', errors='replace')}" for path in judge_outputs]
        raise RuntimeError(
            "Harbor verifier artifacts contain no reward_with_llm_judge.json"
            + (f"; judge reports: {details}" if details else "")
        )
    for path in judge_files:
        reward = json.loads(path.read_text(encoding="utf-8"))
        judge = reward.get("llm_judge_score")
        checklist = reward.get("checklist_score", reward.get("base_partial_pass"))
        final = reward.get("final_score")
        judge_report = reward.get("llm_judge")
        if not isinstance(judge_report, dict) or judge_report.get("status") != "ok":
            raise RuntimeError(f"Harbor judge did not complete successfully in {path}")
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in (judge, checklist, final)):
            raise RuntimeError(f"Harbor judge reward is missing numeric scores in {path}")
        expected = round(0.3 * float(checklist) + 0.7 * float(judge), 4)
        if abs(float(final) - expected) > 0.0001:
            raise RuntimeError(
                f"Harbor final_score mismatch in {path}: got {final}, expected 30/70 score {expected}"
            )
        if abs(float(judge) - 1.0) > 0.0001:
            raise RuntimeError(f"Harbor smoke judge returned {judge} in {path}, expected 1.0")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dumatebench-docker-e2e-") as temp_dir:
        root = Path(temp_dir)
        raw, task, template = _write_fixture(root)
        agent = root / "finish_agent.py"
        agent.write_text(
            "import json\nprint(json.dumps({'finish': True, 'reason': 'smoke'}))\n",
            encoding="utf-8",
        )
        harbor_tasks = root / "harbor_tasks"
        jobs = root / "harbor_jobs"

        _run(["dumate", "template", "fill", "--dataset", str(raw), "--template", str(template), "--task-glob", "task_*"])
        _run(["dumate", "package", "check", str(task)])
        _run([
            "dumate", "run", "--dataset", str(raw), "--task-glob", "task_*", "--agent",
            f"{sys.executable} {agent}", "--max-steps", "1", "--adapter-timeout", "20",
            "--limit", "1", "--concurrency", "1",
        ])
        summary_files = sorted(raw.glob("batch_summary*.jsonl"))
        if not summary_files:
            raise RuntimeError("dumate run did not produce a batch summary")
        records = [json.loads(line) for line in summary_files[-1].read_text(encoding="utf-8").splitlines() if line]
        if len(records) != 1 or records[0].get("status") != "completed" or records[0].get("evaluator_returncode") != 0:
            raise RuntimeError(f"unexpected dumate run summary: {records}")
        _run(["dumate", "harbor", "export", "--task", str(task), "--output", str(harbor_tasks)])
        task_tomls = sorted(harbor_tasks.rglob("task.toml"))
        if len(task_tomls) != 1:
            raise RuntimeError(f"expected one exported Harbor task.toml, found {len(task_tomls)}")
        task_toml = task_tomls[0]
        (task_toml.parent / "tests" / "openai.py").write_text(
            (root / "mock_openai.py").read_text(encoding="utf-8"), encoding="utf-8"
        )
        toml_text = task_toml.read_text(encoding="utf-8")
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DUMATE_LLM_JUDGE_MODEL"):
            placeholder = name + ' = "${' + name + ':-}"'
            if placeholder not in toml_text:
                raise RuntimeError(f"exported Harbor task has no {name} verifier setting")
        env_file = root / "judge.env"
        env_file.write_text(
            "OPENAI_API_KEY=ci-smoke-key\n"
            "OPENAI_BASE_URL=http://ci-mock-openai/v1\n"
            "DUMATE_LLM_JUDGE_MODEL=smoke-model\n",
            encoding="utf-8",
        )
        harbor_env = os.environ.copy()
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DUMATE_LLM_JUDGE_MODEL"):
            harbor_env.pop(name, None)
        harbor_result = _run([
            "harbor", "run", "--path", str(harbor_tasks), "--agent", "nop",
            "--env-file", str(env_file),
            "--jobs-dir", str(jobs), "--n-concurrent", "1", "--n-attempts", "1", "--yes", "--debug",
        ], timeout=900, check=False, env=harbor_env)
        harbor_output = harbor_result.stdout + harbor_result.stderr
        if harbor_result.returncode:
            _dump_container_logs(harbor_output)
            try:
                _assert_harbor_results(jobs, harbor_output)
            except RuntimeError as exc:
                raise RuntimeError(f"harbor run exited with {harbor_result.returncode}: {exc}") from exc
            raise RuntimeError(f"harbor run exited with {harbor_result.returncode}")
        _assert_harbor_results(jobs, harbor_output)
    print("Docker/Harbor smoke E2E passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(f"E2E FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
