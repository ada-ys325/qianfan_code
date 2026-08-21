from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .runner import JudgeRunner, load_task_inputs, read_json, write_json


def parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            value = json.loads(stdout[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


class LLMClient:
    def __init__(
        self,
        *,
        model: str,
        base_url: str | None,
        api_key_env: str,
        temperature: float,
        max_tokens: int,
        retries: int = 5,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retries = max(0, retries)

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai>=1.x is required to call the LLM judge") from exc

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is required to call the LLM judge")
        client = OpenAI(
            api_key=api_key,
            base_url=self.base_url or os.environ.get("OPENAI_BASE_URL"),
            max_retries=0,
        )
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    response_format={"type": "json_object"},
                )
                parsed = json.loads(response.choices[0].message.content or "")
                if not isinstance(parsed, dict):
                    raise RuntimeError("LLM judge returned non-object JSON")
                return parsed
            except Exception as exc:  # provider SDK exceptions vary by version
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(
            f"LLM judge JSON request failed after {self.retries + 1} attempts: {last_exc}"
        ) from last_exc


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--outputs-dir", default=None, help="Defaults to <task-dir>/run_outputs")
    parser.add_argument("--reference-dir", default=None, help="Defaults to <task-dir>/workspace_seed; use '-' to disable")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--total-chars", type=int, default=60000)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rubric-based LLM judge for DuMateBench textual artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-rubric", help="Generate and lock a task-specific rubric")
    _common(generate)
    generate.add_argument("--rubric-out", required=True)

    evaluate = sub.add_parser("evaluate", help="Evaluate outputs using an existing locked rubric")
    _common(evaluate)
    evaluate.add_argument("--rubric", required=True)
    evaluate.add_argument("--result-out", required=True)
    evaluate.add_argument("--rule-result", default=None)
    evaluate.add_argument("--run-rule-evaluator", action="store_true")
    evaluate.add_argument("--judge-runs", type=int, default=1)
    evaluate.add_argument("--rule-weight", type=float, default=0.4)

    run = sub.add_parser("run", help="Generate a locked rubric and evaluate in one command")
    _common(run)
    run.add_argument("--rubric-out", required=True)
    run.add_argument("--result-out", required=True)
    run.add_argument("--rule-result", default=None)
    run.add_argument("--run-rule-evaluator", action="store_true")
    run.add_argument("--judge-runs", type=int, default=1)
    run.add_argument("--rule-weight", type=float, default=0.4)
    return parser.parse_args(argv)


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path | None]:
    task_dir = Path(args.task_dir).expanduser().resolve()
    outputs = Path(args.outputs_dir).expanduser().resolve() if args.outputs_dir else task_dir / "run_outputs"
    if args.reference_dir == "-":
        references = None
    elif args.reference_dir:
        references = Path(args.reference_dir).expanduser().resolve()
    else:
        references = task_dir / "workspace_seed"
    return task_dir, outputs, references


def _rule_result(args: argparse.Namespace, task_dir: Path) -> dict[str, Any] | None:
    if getattr(args, "run_rule_evaluator", False):
        evaluator = task_dir / "evaluator" / "evaluator.py"
        env = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, str(evaluator), "--task-dir", str(task_dir)],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        parsed = parse_json_stdout(proc.stdout)
        if parsed is None:
            raise RuntimeError(f"rule evaluator did not return JSON: {proc.stderr[-800:]}")
        return parsed
    if getattr(args, "rule_result", None):
        return read_json(Path(args.rule_result))
    default = task_dir / "run_outputs" / "reward.json"
    return read_json(default) if default.is_file() else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    task_dir, outputs_dir, reference_dir = _paths(args)
    instruction, checks, artifacts, references = load_task_inputs(
        task_dir,
        outputs_dir,
        reference_dir,
        max_files=args.max_files,
        total_chars=args.total_chars,
    )
    client = LLMClient(
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        temperature=0.0,
        max_tokens=8000,
    )
    runner = JudgeRunner(client)
    task_id = task_dir.name

    if args.command in {"generate-rubric", "run"}:
        rubric = runner.generate_rubric(
            task_id=task_id,
            instruction=instruction,
            objective_checks=checks,
            references=references,
        )
        write_json(Path(args.rubric_out), rubric)
    else:
        rubric = read_json(Path(args.rubric))

    if args.command in {"evaluate", "run"}:
        result = runner.evaluate(
            instruction=instruction,
            rubric=rubric,
            artifacts=artifacts,
            references=references,
            rule_result=_rule_result(args, task_dir),
            judge_runs=args.judge_runs,
            rule_weight=args.rule_weight,
        )
        write_json(Path(args.result_out), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(rubric, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
