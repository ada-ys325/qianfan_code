<p align="center">
  <img src="docs/assets/image.png" alt="DuMateBench" />
</p>

# DuMateBench

DuMateBench is a Docker-based benchmark for evaluating agents on real-world
productivity and office-work tasks.

Given a task description and a sandboxed workspace, an agent must inspect the
available files and tools, carry out the requested work, and produce the
required artifact. The benchmark then runs a task-specific evaluator and
reports whether the artifact satisfies the task's checks.

The benchmark is designed to evaluate more than command generation. Tasks may
require an agent to work with documents, spreadsheets, presentations, PDFs,
OCR, calendars, web references, and unreliable tools or network conditions.
Evaluation captures the final artifact, execution logs, step count, and reward
details.

The full task dataset is available on Hugging Face:
[DuMateBench Dataset](https://huggingface.co/datasets/Annihi/dumate_bench).

This repository contains the evaluation framework, a command-line harness, and
development task datasets. The full benchmark dataset is distributed
separately and is intentionally not committed here.

## Repository layout

| Path | Description |
|------|-------------|
| [`dumatebench/`](dumatebench/) | Core benchmark framework, task runners, evaluators, agent contracts, and example tasks |
| [`dumatebench_cli/`](dumatebench_cli/) | The `dumate` CLI for running an agent against one task or a batch of tasks |
| [`dumatebench/datasets/dev/`](dumatebench/datasets/dev/) | Development and smoke-test task packages |
| [`dumatebench/scripts/`](dumatebench/scripts/) | Batch runners, smoke-test scripts, and evaluation utilities |
| [`dumatebench/agents/`](dumatebench/agents/) | Agent adapter contract and example agents |
| [`.github/workflows/`](.github/workflows/) | Automated code and submission checks |
| [`leaderboard/`](leaderboard/) | Submission intake and CI validation logic |
| [`leaderboard/SUBMIT.md`](leaderboard/SUBMIT.md) | Harbor leaderboard submission guide |
| [`submissions/`](submissions/) | Pull-request Harbor job manifests and local evidence bundles |

The complete `final_dataset_clean/` data is raw benchmark material and is not
included in this repository. It is available from the
[DuMateBench Dataset on Hugging Face](https://huggingface.co/datasets/Annihi/dumate_bench).
CI uses the checked-in development tasks for lightweight tests; official score
verification will use the canonical dataset reference and uploaded Harbor
trials.

Each runnable task is self-contained. A typical task package looks like:

```text
task/
├── instruction.md              # Task goal and required artifact
├── task.yaml                   # Task metadata and runner configuration
├── workspace_seed/             # Initial files copied into /workspace
├── environment/                # Dockerfile, Compose file, setup, and wrappers
├── evaluator/                  # Checks and evaluator implementation
├── network_faults.yaml         # Optional network fault configuration
├── tool_faults.yaml            # Optional tool fault configuration
├── run_outputs/                # Generated artifacts and reward.json
└── run_logs/                   # Generated execution logs
```

## How a run works

DuMateBench separates the component being evaluated from the component that
controls the evaluation:

```text
agent -> one JSON action -> runner -> command in Docker
  ^                                      |
  |------------ observation ------------|

agent finishes -> task evaluator -> reward.json
```

- The **agent** decides the next action. It may be backed by an LLM, a local
  program, or a deterministic script.
- The **runner** starts the task container, enforces step and timeout limits,
  executes one agent command at a time, and records the trajectory.
- **Docker** is the isolated command-execution environment. It contains the
  seeded workspace, task tools, and fault-injection wrappers, but normally not
  the agent's LLM.
- The **evaluator** runs after the agent stops and checks the artifact and task
  logs. Runner completion alone does not mean that the task passed.

## Quick start

Use the following checks in order. The first check is deterministic and is the
recommended way to establish that a new installation works before connecting
an LLM or custom agent.

### Prerequisites

You need:

- Python 3.10 or newer; Python 3.12 is recommended
- Docker
- Docker Compose v2, available as `docker compose`

From the repository root:

```bash
cd /path/to/dumate_bench

python3 -m venv .venv
source .venv/bin/activate

python3 -m pip install -r dumatebench/requirements.txt
python3 -m pip install -e dumatebench_cli/
```

Verify the CLI and Docker:

```bash
dumate --help
docker compose version
```

### 1. Run the deterministic smoke test

This test does not require an API key or call an LLM:

```bash
bash dumatebench/scripts/run_odyssey_2_12_smoke.sh
```

The script builds the smoke image, runs a fixed agent script in Docker,
exercises the OCR, calendar, network, and tool-fault paths, writes a calendar
artifact, and runs the task evaluator. It replaces the contents of this
task's `run_outputs/` and `run_logs/` on every run.

A successful run ends with a reward containing:

```json
{
  "complete_pass": 1,
  "partial_pass": 1.0,
  "environment_recovery": 1,
  "network_recovery": 1
}
```

Inspect the generated artifact and full result:

```bash
ls -l dumatebench/datasets/dev/odyssey_2_12_smoke/run_outputs/calendar/Alice.ics
cat dumatebench/datasets/dev/odyssey_2_12_smoke/run_outputs/reward.json
```

The expected messages `OCR service temporarily unavailable` and
`Calendar backend returned a transient permission error` are injected faults,
not installation failures. The fixed smoke agent retries them deliberately.

### 2. Verify the CLI agent protocol

The example echo agent verifies the `dumate` runner and stdin/stdout adapter
contract:

```bash
dumate run \
  --task dumatebench/datasets/dev/odyssey_2_12_smoke \
  --agent 'python3 dumatebench/agents/examples/echo_agent.py' \
  --max-steps 3
```

The echo agent lists the workspace once and then stops. It intentionally does
not create the requested calendar, so the evaluator is expected to report a
non-passing reward. Use this command to test protocol wiring, not benchmark
quality. This run also replaces the smoke task's previous `run_outputs/` and
`run_logs/`.

### 3. Run the smoke task with an OpenAI-compatible LLM

This repository also includes a command-agent integration test. It calls an
OpenAI-compatible Chat Completions endpoint from the task container:

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://your-provider.example/v1"
export DUMATE_MODEL="your-model-id"

# Required when OPENAI_BASE_URL is not already trusted by the script.
export DUMATE_TRUSTED_BASE_URLS="$OPENAI_BASE_URL"

bash dumatebench/scripts/run_odyssey_2_12_agent.sh --max-steps 20
```

Do not commit API keys or place them in task files. The selected endpoint and
model must support `temperature: 0`, OpenAI JSON-object response format, and
the one-action-per-response contract below. API-compatible model names do not
guarantee these behaviors; a provider may reject the request or return several
concatenated JSON objects.

After the run, inspect both agent completion and evaluator success:

```bash
cat dumatebench/datasets/dev/odyssey_2_12_smoke/run_logs/agent_status.json
cat dumatebench/datasets/dev/odyssey_2_12_smoke/run_outputs/reward.json
```

A successful agent process is not sufficient. Treat the task as passed only
when `evaluator_returncode` is `0` and `reward.json` reports
`"complete_pass": 1`. Reaching `--max-steps`, returning `finish: true`, or
producing a plausible artifact can still result in a partial or failed score.

## Setup details

### Host dependencies

The Python dependencies used by the framework and evaluators are listed in
[`dumatebench/requirements.txt`](dumatebench/requirements.txt). They include
support for Office files, PDFs, images, calendars, YAML, and model-backed
evaluators.

Some optional evaluators require host tools in addition to Python packages:

```bash
# macOS
brew install ffmpeg
brew install libreoffice poppler
```

`ffmpeg` and `ffprobe` are needed by video judge workflows. LibreOffice
(`soffice`) and Poppler (`pdftoppm`) are needed when rendering presentation or
PDF artifacts for evaluation.

### Docker task environment

Tasks are built and run locally; task images are not pulled from a registry.
The default smoke-task base image is `python:3.12-slim`. The task Dockerfile
installs the common command-line tools needed by the sandbox, while task
specific dependencies may intentionally need to be installed by the agent at
runtime.

The task container normally exposes:

```text
/workspace   Initial workspace and agent working directory
/outputs     Final artifacts, mapped to task-local run_outputs/
/logs        Execution logs, mapped to task-local run_logs/
```

The task's `environment/` directory also defines any benchmark tools, wrappers,
and fault injection used by that task. Agents should use the command names
exposed through `PATH` and should not modify benchmark configuration or
evaluator files.

If Docker cannot reach the default Debian mirrors, the base image and apt
mirrors can be overridden for the smoke script:

```bash
DUMATE_BASE_IMAGE=your-mirror/python:3.12-slim \
DUMATE_APT_DEBIAN_MIRROR=http://mirrors.aliyun.com/debian \
DUMATE_APT_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security \
bash dumatebench/scripts/run_odyssey_2_12_smoke.sh
```

## Usage

Use `dumate run` for an actual agent evaluation. A runnable task directory must
contain at least `task.yaml`, `instruction.md`, `environment/`, and
`evaluator/`; raw task material without an environment is not directly
runnable by the public CLI. Validate a task package before a long run:

```bash
dumate package check /absolute/path/to/task
```

The development tasks in this repository are suitable for local integration
tests. The canonical full dataset used for official scoring is distributed and
verified separately; local development rewards are not leaderboard results.

### Discover tasks

List task directories that the CLI can run:

```bash
dumate datasets list dumatebench/datasets/dev
```

The CLI discovers directories containing the task markers `task.yaml` and
`instruction.md`.

### Run one task with your own agent

Your agent can be any executable command that implements the
[agent protocol](dumatebench/agents/agent_contract.md):

```bash
dumate run \
  --task /absolute/path/to/task \
  --agent 'python3 /absolute/path/to/my_agent.py' \
  --max-steps 20 \
  --adapter-timeout 180
```

The command named by `--agent` runs on the host and is invoked once per step.
It receives the current state on stdin and returns one action on stdout. The
runner, not the agent process, executes that action inside the task container.
See the protocol section below before connecting an LLM-backed adapter.

Useful options include:

```text
--max-steps N               Maximum agent steps for each task (default: 20)
--adapter-timeout SECONDS   Timeout for each adapter invocation (default: 180)
--no-build                  Reuse an already-built Docker image
--keep-containers           Keep containers running for debugging
```

### Run a batch

Run multiple tasks concurrently:

```bash
dumate run \
  --dataset dumatebench/datasets/dev \
  --agent 'python3 /absolute/path/to/my_agent.py' \
  --task-glob '*' \
  --limit 5 \
  --concurrency 2
```

The batch command writes a JSON Lines summary under the dataset directory:

```text
batch_summary.<run-id>.jsonl
```

Each record contains the task status, number of steps, evaluator return code,
duration, error information, and the path to that task's
`run_outputs/reward.json`.

Stop a batch after the first task error:

```bash
dumate run \
  --dataset dumatebench/datasets/dev \
  --agent 'python3 /absolute/path/to/my_agent.py' \
  --stop-on-failure
```

Run `dumate run --help`, `dumate datasets --help`, or
`dumate package --help` for the complete option list.

## Agent protocol

The adapter is invoked once per step. It reads exactly one JSON state object
from standard input and writes exactly one JSON action object to standard
output.

Input:

```json
{
  "schema_version": "0.1",
  "step": 1,
  "max_steps": 20,
  "instruction": "Task instruction text",
  "system_prompt": "Environment and tool rules",
  "history": [],
  "last_observation": null
}
```

To request a command in the task container:

```json
{
  "command": "ls -la /workspace",
  "reason": "Inspect the initial workspace"
}
```

To finish:

```json
{
  "finish": true,
  "reason": "The required artifact has been written to /outputs"
}
```

The adapter decides what to do; the runner executes the command inside the
container as the `agent` user with `/workspace` as the working directory.
Before returning `finish: true`, the adapter should verify that the required
artifact exists at the path specified by the task.

See [`dumatebench/agents/agent_contract.md`](dumatebench/agents/agent_contract.md)
for the complete contract and
[`dumatebench/agents/examples/echo_agent.py`](dumatebench/agents/examples/echo_agent.py)
for a minimal implementation.

## Task authoring and validation

Before running or sharing a task package, check its required files and Docker
build context:

```bash
dumate package check /absolute/path/to/task
```

The check verifies the presence of `task.yaml`, `instruction.md`,
`environment/`, and `evaluator/`. It also warns when evaluator or
`web_reference/` gold-reference files may be copied into the task image.

When evaluating an agent, do not modify the task definition, fault
configuration, environment, evaluator, or seeded workspace:

```text
instruction.md
task.yaml
network_faults.yaml
tool_faults.yaml
environment/*
evaluator/*
workspace_seed/*
```

## Results and evaluation

Task-specific evaluators write their result to:

```text
<task>/run_outputs/reward.json
```

Depending on the task, the reward may include complete-pass and partial-pass
scores together with the result of each check. Logs and intermediate artifacts
are kept in `run_logs/` and `run_outputs/` for debugging and analysis.

Use the evaluator result as the source of truth:

```text
run_logs/agent_status.json       Agent stop reason, steps, and evaluator return code
run_logs/agent_adapter.jsonl     CLI action/observation trajectory
run_logs/compose.log             Task-container logs
run_outputs/reward.json          Evaluator checks and task score
```

`status: "completed"` in a CLI summary means that orchestration completed
without an exception. It does not mean that the evaluator passed. Likewise,
`agent_finished: true` only means that the agent returned `finish: true`.
For a complete local pass, require both:

```text
evaluator_returncode == 0
reward.json: complete_pass == 1
```

Common non-passing outcomes are:

| Symptom | Meaning |
|---------|---------|
| `JSONDecodeError: Extra data` | The adapter returned multiple top-level JSON actions in one response |
| `Unsupported value: temperature` | The selected API model does not accept the command agent's request parameters |
| `reached max steps` | The agent never returned `finish: true` within the configured budget |
| `partial_pass < 1.0` | The artifact or one or more required recovery behaviors failed an evaluator check |

For lower-level environment details, including workspace initialization,
Docker Compose mounts, tool wrappers, and fault injection, see
[`dumatebench/docker_environment.md`](dumatebench/docker_environment.md).

## Harbor integration and submissions

The full task source dataset is distributed outside this repository. You can
convert a downloaded task package to Harbor's schema for local development:

```bash
dumate harbor export \
  --dataset /path/to/downloaded/tasks \
  --output /path/to/harbor_tasks

harbor run \
  --path /path/to/harbor_tasks \
  --agent <your-agent> \
  --model <provider/model>
```

Local `--path` runs are useful for integration checks. Formal leaderboard runs
must use the canonical Harbor registry dataset revision announced by the
maintainers, cover every task with at least five trials, and use
`--upload --public`.

After a formal Harbor run, create a score-free pointer manifest:

```bash
dumate submission from-harbor-job \
  --job-dir /path/to/harbor/jobs/<job-id> \
  --out submissions/dumatebench/<version>/<agent>__<model>.json \
  --agent-name my-agent \
  --agent-org my-organization \
  --model-name my-model \
  --model-provider openai
```

See [`leaderboard/SUBMIT.md`](leaderboard/SUBMIT.md) for the canonical dataset,
verification, promotion, and manual publication flow.

## Local evidence bundles

After a batch run, package the generated rewards and logs for submission:

```bash
dumate submission pack \
  --summary dumatebench/datasets/dev/batch_summary.<run-id>.jsonl \
  --out /absolute/path/to/submission \
  --agent-name my-agent \
  --agent-org my-organization \
  --model-name my-model \
  --model-provider openai
```

Validate the bundle before sharing it:

```bash
dumate submission check /absolute/path/to/submission
```

The bundle records run metadata and task results for local evidence. Formal
leaderboard CI reads the public Harbor job referenced by the pointer manifest
and independently recomputes its metrics.

## License

See [`LICENSE`](LICENSE) for the license applicable to this repository.
