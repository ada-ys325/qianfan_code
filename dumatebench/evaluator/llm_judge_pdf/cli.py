from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

try:
    from annotation_pipeline.llm import LLMClient
except ModuleNotFoundError:
    from DataAnnotation.annotation_pipeline.llm import LLMClient

from .runner import JudgeRunner, load_task_inputs, read_json, reference_inventory, write_json


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--outputs-dir", default=None, help="Defaults to <task-dir>/run_outputs")
    parser.add_argument("--reference-dir", default=None, help="Defaults to <task-dir>/workspace_seed; use '-' to disable")
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--total-chars", type=int, default=80000)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Multimodal rubric-based LLM judge for DuMateBench PDF artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-rubric", help="Generate and lock a task-specific rubric")
    _common(generate)
    generate.add_argument("--rubric-out", required=True)

    evaluate = sub.add_parser("evaluate", help="Evaluate PDF artifacts with a locked rubric")
    _common(evaluate)
    evaluate.add_argument("--rubric", required=True)
    evaluate.add_argument("--result-out", required=True)
    evaluate.add_argument("--rule-result", default=None)
    evaluate.add_argument("--judge-runs", type=int, default=1)
    evaluate.add_argument("--rule-weight", type=float, default=0.4)

    run = sub.add_parser("run", help="Generate a rubric and immediately evaluate PDF artifacts")
    _common(run)
    run.add_argument("--rubric-out", required=True)
    run.add_argument("--result-out", required=True)
    run.add_argument("--rule-result", default=None)
    run.add_argument("--judge-runs", type=int, default=1)
    run.add_argument("--rule-weight", type=float, default=0.4)
    return parser.parse_args(argv)


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path | None]:
    task_dir = Path(args.task_dir).resolve()
    outputs_dir = Path(args.outputs_dir).resolve() if args.outputs_dir else task_dir / "run_outputs"
    if args.reference_dir == "-":
        reference_dir = None
    else:
        reference_dir = Path(args.reference_dir).resolve() if args.reference_dir else task_dir / "workspace_seed"
    return task_dir, outputs_dir, reference_dir


def _client(args: argparse.Namespace) -> LLMClient:
    if not os.environ.get(args.api_key_env, "").strip():
        raise RuntimeError(f"missing API key env var: {args.api_key_env}")
    return LLMClient(
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        max_tokens=12000,
        temperature=0.0,
    )


def main(argv: list[str] | None = None, *, client: Any | None = None) -> int:
    args = parse_args(argv)
    task_dir, outputs_dir, reference_dir = _paths(args)
    task_id, instruction, objective_checks = load_task_inputs(task_dir)
    references = reference_inventory(reference_dir, max_files=args.max_files)
    runner = JudgeRunner(client or _client(args))

    if args.command == "generate-rubric":
        rubric = runner.generate_rubric(
            task_id=task_id,
            instruction=instruction,
            objective_checks=objective_checks,
            references=references,
        )
        write_json(Path(args.rubric_out), rubric)
        return 0

    rule_result = read_json(Path(args.rule_result)) if args.rule_result else None
    if args.command == "evaluate":
        rubric = read_json(Path(args.rubric))
        result = runner.evaluate(
            instruction=instruction,
            rubric=rubric,
            outputs_dir=outputs_dir,
            references=references,
            reference_dir=reference_dir,
            rule_result=rule_result,
            judge_runs=args.judge_runs,
            rule_weight=args.rule_weight,
            max_files=args.max_files,
            max_pages=args.max_pages,
            total_chars=args.total_chars,
            model=args.model,
        )
        write_json(Path(args.result_out), result)
        return 0

    rubric, result = runner.run(
        task_id=task_id,
        instruction=instruction,
        objective_checks=objective_checks,
        outputs_dir=outputs_dir,
        references=references,
        reference_dir=reference_dir,
        rule_result=rule_result,
        judge_runs=args.judge_runs,
        rule_weight=args.rule_weight,
        max_files=args.max_files,
        max_pages=args.max_pages,
        total_chars=args.total_chars,
        model=args.model,
    )
    write_json(Path(args.rubric_out), rubric)
    write_json(Path(args.result_out), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
