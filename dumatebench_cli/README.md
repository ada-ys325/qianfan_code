# dumatebench-cli

Harbor-style command-line tool for running your own agent against
[DuMateBench](../dumatebench) tasks: build the task's Docker image, drive
your agent through the existing `agent_contract.md` stdin/stdout protocol,
run the task's evaluator, and collect a `reward.json` / `batch_summary.jsonl`.

## Install

The supported installation mode is an editable install from a complete
DuMateBench source checkout, run from its repository root:

```bash
pip install -e dumatebench_cli/
```

The CLI is not currently distributed as a self-contained PyPI or wheel
package. Keep the sibling `dumatebench/` directory in the checkout because
`dumate run` and `dumate harbor export` use its shared evaluator at
`dumatebench/evaluator/evaluate.py`. Do not use `pip install .` or install a
standalone wheel for these workflows.

Requires Docker (with the `docker compose` plugin) on your machine — tasks
build and run as local containers, images are never pulled from a registry.

## Quickstart

Run a single task with the bundled echo agent (for a smoke test):

```bash
dumate run \
  --task dumatebench/datasets/dev/template_task \
  --agent 'python3 dumatebench/agents/examples/echo_agent.py' \
  --max-steps 3
```

Run your own agent — any executable that reads the JSON state on stdin and
writes a JSON action to stdout per `dumatebench/agents/agent_contract.md`:

```bash
dumate run \
  --task path/to/a/task \
  --agent 'python3 my_agent.py' \
  --max-steps 20
```

Run a batch of tasks under a directory:

```bash
dumate run \
  --dataset dumatebench/datasets/dev \
  --agent 'python3 my_agent.py' \
  --task-glob '*' \
  --limit 5 \
  --concurrency 2
```

This writes `batch_summary.<run-id>.jsonl` under the dataset directory, one
line per task with status, step count, evaluator return code, and the path
to that task's `run_outputs/reward.json`.

When installed from the DuMateBench source repository, the CLI automatically
passes `dumatebench/evaluator/evaluate.py` to task evaluators. A non-passing
task is still a completed run when it produces a valid `reward.json`; an
evaluator crash or missing/invalid reward is reported as a run error.

List discoverable tasks under a directory without running anything:

```bash
dumate datasets list dumatebench/datasets/dev
```

## Agent protocol

Your `--agent` command is invoked once per step. It receives a JSON object
on stdin (`schema_version`, `step`, `max_steps`, `instruction`,
`system_prompt`, `history`, `last_observation`) and must print exactly one
JSON object to stdout:

```json
{"command": "cat /workspace/notes.txt", "reason": "checking input file"}
```

or, when done:

```json
{"finish": true, "reason": "artifact written to /outputs"}
```

See `dumatebench/agents/agent_contract.md` for the full spec and
`dumatebench/agents/examples/echo_agent.py` for a minimal reference
implementation.

## Task package checks (for task authors)

```bash
dumate package check path/to/task
```

Verifies a task directory has the files a runnable task needs
(`task.yaml`, `instruction.md`, `environment/`, `evaluator/`), requires
`evaluator/evaluator.py` and `evaluator/checks.yaml`, validates the shared
source-checkout evaluator and every local `COPY`/`ADD` source in the
Dockerfile, and flags whether `environment/Dockerfile` bakes `evaluator/` or
`web_reference/` (gold-reference material) into the built image. The task YAML
must explicitly set `environment.allow_internet` to the boolean `true` or
`false`; quoted strings are invalid. A task-authored
`environment/docker-compose.yaml` is not accepted by this release check:
filled tasks omit this legacy file because `dumate run` generates its compose
definition and Harbor supplies its own overlay. The local runner still
supports existing authored compose files for backwards compatibility.

## Useful flags

- `--max-steps N` — cap agent steps per task (default 20)
- `--adapter-timeout SECONDS` — per-step timeout for your agent process (default 180)
- `--no-build` — skip `docker compose build` if the image is already built
- `--keep-containers` — leave containers up after a task for debugging
- `--concurrency N` — run N tasks in parallel during a batch
- `--stop-on-failure` — stop a batch after the first task error

Run `dumate run --help`, `dumate datasets --help`, or `dumate package --help`
for the full option list.
