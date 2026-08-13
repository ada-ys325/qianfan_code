"""``dumate`` command-line entry point.

Player-facing surface only. Internal-only knobs (LLM judge, template-task
reuse, memory-scaling retries) are intentionally not exposed here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from dumatebench_cli.packager import CheckResult, check_task_dir
from dumatebench_cli.runner import default_run_id, discover_tasks, run_batch, run_single_task
from dumatebench_cli.submission import SubmissionError, pack_submission, validate_submission

app = typer.Typer(
    name="dumate",
    help="DuMateBench harbor-style CLI: build task images, run your agent, collect rewards.",
    no_args_is_help=True,
)
datasets_app = typer.Typer(help="Inspect local task datasets.")
package_app = typer.Typer(help="Task package authoring checks (for task authors).")
submission_app = typer.Typer(help="Package a completed run for leaderboard submission.")
app.add_typer(datasets_app, name="datasets")
app.add_typer(package_app, name="package")
app.add_typer(submission_app, name="submission")


@app.command()
def run(
    dataset: Optional[Path] = typer.Option(
        None, "--dataset", "-d", help="Path to a task or a directory containing many task directories."
    ),
    task: Optional[Path] = typer.Option(
        None, "--task", "-t", help="Path to a single task directory (alias for --dataset on one task)."
    ),
    agent: str = typer.Option(
        ..., "--agent", "-a", help="Shell command that speaks the agent_contract.md stdin/stdout protocol."
    ),
    task_glob: str = typer.Option("*", "--task-glob", help="Glob used when --dataset is a directory of tasks."),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Search --dataset recursively."),
    limit: int = typer.Option(0, "--limit", help="Only run the first N discovered tasks (0 = no limit)."),
    max_steps: int = typer.Option(20, "--max-steps", help="Max agent steps per task before forced stop."),
    adapter_timeout: int = typer.Option(180, "--adapter-timeout", help="Per-step timeout (seconds) for the agent process."),
    no_build: bool = typer.Option(False, "--no-build", help="Skip `docker compose build` (image already built)."),
    keep_containers: bool = typer.Option(False, "--keep-containers", help="Do not `docker compose down` after each task."),
    concurrency: int = typer.Option(1, "--concurrency", "-c", help="Number of tasks to run in parallel."),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Label for this run, used to name the summary file."),
    summary: Optional[Path] = typer.Option(None, "--summary", help="Explicit path for the batch_summary.jsonl output."),
    stop_on_failure: bool = typer.Option(False, "--stop-on-failure", help="Stop the batch after the first task error."),
) -> None:
    """Run AGENT against one task (--task) or a batch of tasks (--dataset)."""
    target = task or dataset
    if target is None:
        typer.secho("Provide --task <dir> or --dataset <dir>.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    target = target.resolve()
    if not target.exists():
        typer.secho(f"Path does not exist: {target}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    label = run_id or default_run_id(agent.split()[0])

    if task is not None:
        result = run_single_task(
            task_dir=target,
            agent_cmd=agent,
            max_steps=max_steps,
            adapter_timeout=adapter_timeout,
            no_build=no_build,
            keep_containers=keep_containers,
        )
        typer.echo(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        raise typer.Exit(code=0 if result.status == "completed" else 1)

    summary_path = summary or (target / f"batch_summary.{label}.jsonl")
    results = run_batch(
        tasks_root=target,
        agent_cmd=agent,
        task_glob=task_glob,
        recursive=recursive,
        limit=limit,
        max_steps=max_steps,
        adapter_timeout=adapter_timeout,
        no_build=no_build,
        keep_containers=keep_containers,
        concurrency=concurrency,
        summary_path=summary_path,
        stop_on_failure=stop_on_failure,
    )
    errors = sum(1 for r in results if r.status == "error")
    typer.echo(f"Ran {len(results)} task(s), {errors} error(s). Summary: {summary_path}")
    raise typer.Exit(code=1 if errors else 0)


@datasets_app.command("list")
def datasets_list(
    dataset: Path = typer.Argument(..., help="Directory to search for task directories."),
    task_glob: str = typer.Option("*", "--task-glob"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive"),
) -> None:
    """List task directories discoverable under DATASET."""
    tasks = discover_tasks(dataset.resolve(), task_glob=task_glob, recursive=recursive)
    if not tasks:
        typer.secho(f"No task directories found under {dataset}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    for t in tasks:
        typer.echo(str(t))


@package_app.command("check")
def package_check(
    task: Path = typer.Argument(..., help="Task directory to check."),
) -> None:
    """Verify a task's Docker build context does not leak evaluator/gold-reference files."""
    result: CheckResult = check_task_dir(task.resolve())
    for check in result.checks:
        if check.advisory:
            color, label = typer.colors.YELLOW, "WARN"
        elif check.ok:
            color, label = typer.colors.GREEN, "OK  "
        else:
            color, label = typer.colors.RED, "FAIL"
        typer.secho(f"{label} {check.message}", fg=color)
    raise typer.Exit(code=0 if result.passed else 1)


@submission_app.command("pack")
def submission_pack(
    summary: Path = typer.Option(
        ..., "--summary", help="Path to the batch_summary.<run-id>.jsonl produced by `dumate run`."
    ),
    out: Path = typer.Option(..., "--out", help="Directory to write the submission bundle into (must not exist)."),
    agent_name: str = typer.Option(..., "--agent-name", help="Display name of your agent/scaffold."),
    agent_org: str = typer.Option(..., "--agent-org", help="Organization or individual submitting the run."),
    model_name: str = typer.Option(..., "--model-name", help="Model identifier used by the agent."),
    model_provider: str = typer.Option(..., "--model-provider", help="Model provider (e.g. openai, anthropic)."),
    agent_repo: Optional[str] = typer.Option(None, "--agent-repo", help="Optional URL to the agent's source repo."),
    agent_docs: Optional[str] = typer.Option(None, "--agent-docs", help="Optional URL to the agent's docs."),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", help="max_steps used for this run, recorded in config.json."),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", help="concurrency used for this run, recorded in config.json."),
    agent_cmd: Optional[str] = typer.Option(None, "--agent-cmd", help="Agent command used for this run, recorded in config.json."),
) -> None:
    """Collect a run's reward.json/run_logs into a leaderboard-submission bundle."""
    config = {
        k: v
        for k, v in {
            "max_steps": max_steps,
            "concurrency": concurrency,
            "agent_cmd": agent_cmd,
        }.items()
        if v is not None
    }
    try:
        result = pack_submission(
            summary_path=summary,
            out_dir=out,
            agent_name=agent_name,
            agent_org=agent_org,
            model_name=model_name,
            model_provider=model_provider,
            agent_repo=agent_repo,
            agent_docs=agent_docs,
            dumate_run_args=config,
        )
    except SubmissionError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    for warning in result.warnings:
        typer.secho(f"WARN {warning}", fg=typer.colors.YELLOW)
    typer.secho(f"Packed {result.task_count} task(s) into {result.out_dir}", fg=typer.colors.GREEN)
    if result.warnings:
        typer.secho(
            "Some artifacts were missing (see WARN lines above) — this bundle may fail leaderboard validation.",
            fg=typer.colors.YELLOW,
        )
    typer.echo(
        "\nNext steps: fork the leaderboard repo, copy this directory under "
        "submissions/dumatebench/<version>/<agent>__<model>/, and open a PR. "
        "The leaderboard repo's bot will re-run these same checks on your PR."
    )


@submission_app.command("check")
def submission_check(
    bundle: Path = typer.Argument(..., help="Submission bundle directory produced by `dumate submission pack`."),
) -> None:
    """Validate a submission bundle's completeness before opening a PR."""
    errors = validate_submission(bundle)
    if not errors:
        typer.secho("OK   Submission bundle looks complete.", fg=typer.colors.GREEN)
        raise typer.Exit(code=0)
    for error in errors:
        typer.secho(f"FAIL {error}", fg=typer.colors.RED)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
