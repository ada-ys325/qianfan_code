# DuMateBench Agent Adapter Contract

This contract is for users who want to run their own agent against a DuMateBench task without modifying the task, Docker environment, fault configuration, or evaluator.

## What To Implement

Implement an executable adapter program. On each step, the adapter reads one JSON object from stdin and writes one JSON object to stdout.

The adapter may call any model or local policy it wants. It must not execute task commands itself. It should only decide the next action.

## Adapter Input

The runner sends JSON like this:

```json
{
  "schema_version": "0.1",
  "step": 1,
  "max_steps": 20,
  "instruction": "task instruction text",
  "system_prompt": "runner-level environment and tool rules",
  "history": [
    {
      "action": {"command": "ls -R /workspace", "reason": "inspect workspace"},
      "observation": {"returncode": 0, "elapsed_sec": 0.12, "output": "..."}
    }
  ],
  "last_observation": null
}
```

`instruction` contains only the task goal and required artifact path. Environment rules, tool usage, fault handling, and finish protocol are in `system_prompt`.
`task.yaml` is runner/evaluator metadata and is not sent to the adapter.

## Adapter Output

To run a command:

```json
{"command": "bash command to run", "reason": "short reason"}
```

To finish:

```json
{"finish": true, "reason": "short reason"}
```

The runner executes commands inside the task container as the `agent` user with `/workspace` as the working directory. The adapter should verify required artifacts exist before returning `finish=true`.

## Environment Contract

Do not modify the benchmark task files when evaluating an agent:

```text
instruction.md
task.yaml
network_faults.yaml
tool_faults.yaml
environment/*
evaluator/*
workspace_seed/*
```

Final artifacts should be written to the path requested by the task, usually under `/outputs`. Logs are under `/logs`. The host maps these to task-local `run_outputs/` and `run_logs/`.

Use benchmark tools by their command names as exposed in `PATH`, such as `tesseract`, `ocr_extract`, `calendar_write`, and `mail_send`.
