# Running native Claude Code and Codex agents

DuMateBench supports three batch agent backends:

| Backend | `--agent-backend` | Model API required |
| --- | --- | --- |
| Existing command-loop ReAct | `react` | OpenAI-compatible Chat Completions |
| OpenAI Codex CLI | `codex` | OpenAI Responses (`POST /v1/responses`) |
| Anthropic Claude Code | `claude-code` | Anthropic Messages (`POST /v1/messages`) |

The native CLIs run inside the task container as the `agent` Linux user. They work directly in
`/workspace`, write artifacts under `/outputs`, inherit the benchmark tool wrappers from `PATH`, and
are stopped at the task's `agent.timeout_sec`. The host runs the existing evaluator after the CLI exits.
Unlike the ReAct backend, native CLIs do not use `--max-steps` by default; they run until the CLI exits
or the task timeout is reached.

## Required credentials and endpoint capabilities

The agent and LLM judge may use different credentials:

```bash
# Native Claude Code or Codex model credential. Recommended for all native runs.
export DUMATE_AGENT_API_KEY="..."

# Existing checklist/unified LLM judge credential and endpoint.
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://judge-gateway.example/v1"
export DUMATE_LLM_JUDGE_MODEL="gpt-4o"
```

Set `--agent-base-url` to the native agent's gateway. A gateway that only implements
`/v1/chat/completions` is insufficient:

- Codex requires the OpenAI Responses protocol at `/v1/responses`.
- Claude Code requires Anthropic Messages at `/v1/messages`; a production-compatible gateway should
  also implement `/v1/messages/count_tokens` and preserve Anthropic beta/version headers.

For Claude Code, `DUMATE_AGENT_API_KEY` is sent as a bearer token. If it is absent, the runner falls
back to `ANTHROPIC_AUTH_TOKEN` and then `ANTHROPIC_API_KEY`. For Codex it falls back to
`OPENAI_API_KEY`. Secrets are passed through Docker environment variables and are not written into the
generated command or Codex config file.

For Claude Code, the runner accepts either `https://gateway.example` or
`https://gateway.example/v1` and normalizes the value before launching the CLI, preventing accidental
`/v1/v1/messages` requests.

## Codex example

```bash
export DUMATE_AGENT_API_KEY="..."
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://cn.huayanapi.com:27502/v1"
export DUMATE_LLM_JUDGE_MODEL="gpt-4o"

nohup python3 dumatebench/scripts/run_task_batch.py \
  --tasks-dir dumatebench/datasets/claude-opus-4-8-group6 \
  --template-task dumatebench/datasets/dev/template_task \
  --task-glob '*' \
  --agent-backend codex \
  --agent-model gpt-5.5 \
  --agent-base-url https://cn.huayanapi.com:27502/v1 \
  --codex-version latest \
  > codex-gpt-5.5-group6.out 2>&1 &
```

The configured endpoint must accept the requested model ID and the Responses API. Replace `gpt-5.5`
with the exact model ID returned by the gateway.

## Claude Code example

```bash
export DUMATE_AGENT_API_KEY="..."
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://cn.huayanapi.com:27502/v1"
export DUMATE_LLM_JUDGE_MODEL="gpt-4o"

nohup python3 dumatebench/scripts/run_task_batch.py \
  --tasks-dir dumatebench/datasets/claude-opus-4-8-group6 \
  --template-task dumatebench/datasets/dev/template_task \
  --task-glob '*' \
  --agent-backend claude-code \
  --agent-model claude-opus-4-8 \
  --agent-base-url https://anthropic-compatible-gateway.example/v1 \
  --claude-code-version stable \
  > claude-code-opus-group6.out 2>&1 &
```

`--claude-code-version` accepts `stable`, `latest`, or a concrete Claude Code version.
`--codex-version` controls the official npm-package fallback used when the standalone installer cannot
reach GitHub release assets. Every run records the resolved CLI version. If you need a hard Claude Code
turn cap for a special experiment, pass `--agent-max-turns N`; otherwise leave it unset.

### Claude Code with a GPT model

This is possible only when the configured gateway translates Anthropic Messages, streaming, and tool
use to the upstream GPT API:

```bash
--agent-backend claude-code \
--agent-model gpt-5.5 \
--agent-base-url https://anthropic-to-gpt-gateway.example/v1
```

Anthropic does not officially support routing Claude Code to non-Claude models. Treat this as an
experimental agent/model combination and record the gateway name and version with the benchmark result.

## Build and network behavior

The first run for each native backend builds a backend-specific task image and downloads the official
CLI installer. Docker therefore needs outbound access to `claude.ai` for Claude Code or `chatgpt.com`
and GitHub release assets for Codex. Both installers have an npm-package fallback, so the Docker build
also needs access to the configured npm registry when release assets are unavailable. Later task builds
can reuse Docker layers.

By default, the native runner sends model API traffic through a root-side loopback proxy. This matches
the existing ReAct runner: benchmark network faults affect task commands but do not randomly break the
controller's model connection. Pass `--native-direct-model-network` to deliberately include model API
traffic in the task's network-fault scope.

Native run artifacts include:

```text
<package-root>/runs/<run-id>/<task-id>/batch runtime and task view
run_logs/agent_status.json
run_logs/native_agent.jsonl
run_logs/native_agent.stderr.log
run_logs/model_proxy.jsonl
run_outputs/reward.json
run_outputs/reward_with_llm_judge.json
```

Batch runs are isolated by default under `<package-root>/runs/<run-id>/`. Use `--run-id` to name each
agent/model experiment, and `--runs-root` to put all experiment artifacts elsewhere.
Only the selected run directory is cleared; the original task's `run_outputs/` and `run_logs/` are
not used for batch results.
