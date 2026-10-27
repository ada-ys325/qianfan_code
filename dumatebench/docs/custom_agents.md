# Running Custom Agents and CLIs

DuMateBench supports two ways to run a custom agent. Use an adapter when the
agent can make one decision per invocation and return a DuMateBench action. Use
a native backend when the CLI needs to run as an interactive coding agent
inside the task container and work directly in `/workspace`.

| Path | Where the agent runs | What the agent returns | Entry point |
| --- | --- | --- | --- |
| Adapter | Host machine | One JSON action per step | `dumate run --agent ...` |
| Native CLI | Task Docker container | CLI edits files and runs tools directly | `run_task_batch.py --agent-backend ...` |

## Adapter: Wrap a CLI or Agent

The adapter path works with any executable that can be called from the host.
The `--agent` process starts once per step. It reads one JSON state object
from stdin and writes one JSON action object to stdout. The runner executes the
returned command inside the task container as the `agent` user. The adapter
itself runs on the host and must not execute task commands against the host
workspace.

The state includes the task instruction, system prompt, step number, and
conversation history. Because the runner starts a fresh process for each step,
use the supplied history or store any additional state outside the task
container.

Your adapter can call a third-party CLI in non-interactive mode, send the state
to an API, or use a local policy. It must translate that agent's decision into
one of these outputs:

```json
{"command": "ls -la /workspace", "reason": "inspect the task files"}
```

or:

```json
{"finish": true, "reason": "the required output is ready"}
```

Keep stdout reserved for the single JSON object. Send diagnostics to stderr.
The adapter contract and a minimal implementation are in
[`agent_contract.md`](../agents/agent_contract.md) and
[`echo_agent.py`](../agents/examples/echo_agent.py).

For a third-party CLI that accepts a prompt and returns machine-readable text,
the adapter maps the benchmark state to the CLI input and parses its response
into an action. This mapping pattern leaves the product-specific CLI call in
`call_your_cli`:

```python
import json
import sys

state = json.load(sys.stdin)
decision = call_your_cli(state)  # Implement this with the CLI's non-interactive interface.

if decision["done"]:
    action = {"finish": True, "reason": decision.get("reason", "")}
else:
    action = {
        "command": decision["command"],
        "reason": decision.get("reason", ""),
    }

print(json.dumps(action, ensure_ascii=False))
```

If the CLI is designed to execute shell commands and edit files itself, use
the native path below. The adapter path returns commands for the task
container to execute.

Install your adapter's host-side dependencies and the third-party CLI, then
run one task:

```bash
python3 -m pip install -e dumatebench_cli/
export THIRD_PARTY_API_KEY="..."

dumate run \
  --task /absolute/path/to/task \
  --agent 'python3 /absolute/path/to/my_adapter.py' \
  --max-steps 20 \
  --adapter-timeout 180
```

Run a dataset with the same adapter:

```bash
dumate run \
  --dataset /absolute/path/to/tasks \
  --agent 'python3 /absolute/path/to/my_adapter.py' \
  --task-glob '*' \
  --concurrency 2
```

The adapter inherits the host process environment, so host-side credentials
can be read by the adapter or its child CLI. Do not put secrets in the adapter
source, task files, or command-line arguments. See the
[`dumatebench-cli README`](../../dumatebench_cli/README.md) for runner flags and
result locations.

## Native CLI: Run Inside the Task Container

The native path gives a CLI direct access to the benchmark container. It runs
as the `agent` user, works in `/workspace`, writes deliverables to the paths
requested by the task (usually under `/outputs`), and sees benchmark tool
wrappers through `PATH`. The task timeout in `task.yaml` limits its run. The
host then runs the evaluator and unified LLM judge.

The current native runner has built-in backends for **Codex CLI** and
**Claude Code**. Run it with `dumatebench/scripts/run_task_batch.py`:

```bash
export DUMATE_AGENT_API_KEY="..."
export OPENAI_API_KEY="..."       # Optional, for the separate LLM judge
export OPENAI_BASE_URL="https://judge-gateway.example/v1"
export DUMATE_LLM_JUDGE_MODEL="gpt-4o"

python3 dumatebench/scripts/run_task_batch.py \
  --tasks-dir /absolute/path/to/tasks \
  --template-task dumatebench/datasets/dev/template_task \
  --task-glob '*' \
  --agent-backend codex \
  --agent-model gpt-5.5 \
  --agent-base-url https://responses-compatible.example/v1
```

For Claude Code, use `--agent-backend claude-code`, set `--agent-model` to the
model ID, and point `--agent-base-url` to an Anthropic Messages-compatible
endpoint. Codex requires an endpoint that supports OpenAI Responses;
Chat Completions-only endpoints do not work for this backend. The agent API
credential and judge credential are separate. `DUMATE_AGENT_API_KEY` is the
recommended agent credential; Codex can fall back to `OPENAI_API_KEY`, while
Claude Code can fall back to `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`.

For example, this selects Claude Code and its Anthropic-compatible endpoint:

```bash
python3 dumatebench/scripts/run_task_batch.py \
  --tasks-dir /absolute/path/to/tasks \
  --template-task dumatebench/datasets/dev/template_task \
  --task-glob '*' \
  --agent-backend claude-code \
  --agent-model claude-opus-4-8 \
  --agent-base-url https://anthropic-compatible.example/v1
```

The native runner builds a backend-specific Docker image and installs the CLI
there. It passes credentials into the container at runtime. It writes native
CLI output and errors to `run_logs/native_agent.jsonl` and
`run_logs/native_agent.stderr.log`; inspect those alongside
`run_outputs/reward_with_llm_judge.json`.
See [`native_agents.md`](native_agents.md) for endpoint requirements, version
flags, and network behavior.

### Adding Another Native CLI

An arbitrary third-party CLI cannot use the native path by changing only
`--agent-backend`. The current runner accepts `react`, `codex`, and
`claude-code`; a new native CLI needs a backend implementation. At minimum:

1. Add a backend choice and CLI-specific arguments to
   `dumatebench/scripts/run_task_batch.py`.
2. Add its command builder, credential and endpoint handling, timeout, and log
   collection to `dumatebench/agents/native_agent.py`.
3. Add the CLI's installation steps and version build arguments to
   `dumatebench/datasets/dev/template_task/environment/Dockerfile`, then make
   sure the generated task Dockerfile receives those arguments.
4. Document and verify its API protocol, tool permissions, output paths, and
   behavior under the task's network and resource limits.

If the CLI can instead be called as a host-side decision tool and can return
one action per step, the adapter path usually needs no framework changes.

## Harbor Runs

Harbor has its own agent interface. For a DuMateBench adapter, export the task
and use the bundled bridge agent with `--agent` and `--ak
agent_cmd=...`. For other agents, use a Harbor-compatible agent implementation.
The exported task maps `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, and
`DUMATE_LLM_JUDGE_MODEL` from Harbor's host environment into the verifier. Load
credentials from an env file instead of placing the API key in command-line
arguments:

```bash
harbor run \
  --path /path/to/harbor_tasks \
  --agent dumatebench_cli.harbor_agent:DumateBenchAgent \
  --model smoke/model \
  --ak agent_cmd='python3 /path/to/my_adapter.py' \
  --env-file /path/to/judge.env
```

The env file should define `OPENAI_API_KEY` and may define `OPENAI_BASE_URL`
and either `DUMATE_LLM_JUDGE_MODEL` or `OPENAI_MODEL`. The exported task gives
the agent 1800 seconds and the container 8192 MB by default. Set
`agent.timeout_sec` and `environment.memory_mb` in the source
`task.yaml` before export to override these limits. Inspect
`reward_with_llm_judge.json` in the Harbor job's verifier artifacts: a
successful judge call has `llm_judge.status` equal to `ok` and a numeric
`llm_judge_score`; `final_score` is 30% checklist and 70% judge. If no judge
key is available, the deterministic evaluator still runs, but it does not
provide a judge score. See the Harbor integration section in the repository
[`README`](../../README.md#e-run-harbor-with-a-model).
