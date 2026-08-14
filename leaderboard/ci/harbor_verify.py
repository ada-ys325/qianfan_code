"""Verify a DuMateBench submission against Harbor Hub.

The submission manifest is untrusted input. This module treats only the Harbor
job referenced by the manifest as the source of truth, then checks the job,
its trial metadata, and the canonical dataset task digests. Locally copied
reward files and claimed metrics are never used for verification. DuMateBench
metrics are read from Harbor's verifier result and checked independently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from dumatebench.evaluator.scoring import final_score as dumate_final_score
except ModuleNotFoundError:  # Running the script directly from a checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from dumatebench.evaluator.scoring import final_score as dumate_final_score

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# These are the Harbor knobs Terminal-Bench treats as fairness-sensitive. A
# submission may choose its agent/model, but it may not stretch timeouts or
# change the verifier/environment resources used by the benchmark.
_MULTIPLIERS = (
    "agent_timeout_multiplier",
    "verifier_timeout_multiplier",
    "agent_setup_timeout_multiplier",
    "environment_build_timeout_multiplier",
)
_AGENT_OVERRIDES = ("override_timeout_sec", "override_setup_timeout_sec", "max_timeout_sec")
_VERIFIER_OVERRIDES = ("override_timeout_sec", "max_timeout_sec")
_ENVIRONMENT_OVERRIDES = (
    "override_cpus",
    "override_gpus",
    "override_memory_mb",
    "override_storage_mb",
)


class HarborVerificationError(RuntimeError):
    """Raised when Harbor data cannot prove a submission is valid."""


def _validate_manifest(path: Path) -> list[str]:
    """Load the CLI validator only for the command path that needs YAML."""
    try:
        from dumatebench_cli.submission import validate_submission_manifest
    except ModuleNotFoundError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dumatebench_cli"))
        from dumatebench_cli.submission import validate_submission_manifest
    return validate_submission_manifest(path)


def job_uuid(value: str) -> str:
    """Extract a Harbor job UUID from a URL or a bare ID."""
    return value.rstrip("/").rsplit("/", 1)[-1]


def harbor_json(args: list[str]) -> dict[str, Any]:
    """Run the authenticated Harbor CLI and parse its JSON response."""
    try:
        result = subprocess.run(
            ["harbor", "hub", *args, "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise HarborVerificationError(
            "Harbor CLI is not installed in the CI environment."
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise HarborVerificationError(
            f"Harbor query failed: harbor hub {' '.join(args)}: {detail}"
        )
    try:
        value = json.loads(ANSI_ESCAPE.sub("", result.stdout))
    except json.JSONDecodeError as exc:
        raise HarborVerificationError("Harbor returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise HarborVerificationError("Harbor returned a JSON value, not an object.")
    return value


def harbor_job_trials(job_id: str, page_size: int = 500) -> list[dict[str, Any]]:
    """Read all latest-attempt trial metadata for one Harbor job."""
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = harbor_json(
            ["job", "trials", job_id, "--limit", str(page_size), "--page", str(page)]
        )
        page_items = payload.get("items", [])
        if not isinstance(page_items, list):
            raise HarborVerificationError("Harbor job trials response has no item list.")
        items.extend(item for item in page_items if isinstance(item, dict))
        total_pages = payload.get("total_pages", 1)
        if page >= int(total_pages):
            return items
        page += 1


async def _trial_details_async(trial_ids: list[str]) -> dict[str, dict[str, Any]]:
    from harbor.hub.client import HubClient

    client = HubClient()
    semaphore = asyncio.Semaphore(24)

    async def fetch(trial_id: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                payload = (await client.get_trial_detail(trial_id)).raw
            except Exception as exc:  # noqa: BLE001 - report the trial failure
                raise HarborVerificationError(
                    f"Could not fetch Harbor trial {trial_id}: {type(exc).__name__}"
                ) from exc
        if not isinstance(payload, dict):
            raise HarborVerificationError(f"Harbor trial {trial_id} is not an object.")
        return trial_id, payload

    pairs = await asyncio.gather(*(fetch(trial_id) for trial_id in trial_ids))
    return dict(pairs)


def harbor_trial_details(trial_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not trial_ids:
        return {}
    return asyncio.run(_trial_details_async(trial_ids))


async def _dataset_task_digests_async(dataset: str, revision: str) -> dict[str, str]:
    from harbor.registry.client.package import PackageDatasetClient

    metadata = await PackageDatasetClient().get_dataset_metadata(f"{dataset}@{revision}")
    digests: dict[str, str] = {}
    for task in metadata.task_ids:
        name = f"{task.org}/{task.name}"
        digests[name] = task.ref
        digests.setdefault(task.name, task.ref)
    return digests


def dataset_task_digests(dataset: str, revision: str) -> dict[str, str]:
    try:
        return asyncio.run(_dataset_task_digests_async(dataset, revision))
    except HarborVerificationError:
        raise
    except Exception as exc:  # noqa: BLE001 - turn registry failures into CI errors
        raise HarborVerificationError(
            f"Could not read canonical Harbor dataset {dataset}@{revision}: "
            f"{type(exc).__name__}"
        ) from exc


def _dataset_names(job: dict[str, Any]) -> set[str]:
    config = job.get("config") or {}
    values = config.get("datasets") or job.get("datasets") or []
    names: set[str] = set()
    for value in values:
        if isinstance(value, str):
            names.add(value)
        elif isinstance(value, dict):
            for key in ("name", "dataset", "id"):
                if value.get(key):
                    names.add(str(value[key]))
    return names


def _task_name(trial: dict[str, Any], detail: dict[str, Any]) -> str | None:
    value = trial.get("task_name") or trial.get("task_id") or trial.get("task")
    if isinstance(value, dict):
        value = value.get("name") or value.get("id")
    if value:
        return str(value)
    config_task = ((detail.get("config") or {}).get("task") or {})
    if isinstance(config_task, dict):
        value = config_task.get("name") or config_task.get("id")
        if value:
            return str(value)
    return None


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _section_overrides(
    section: Any, keys: tuple[str, ...], prefix: str = ""
) -> list[str]:
    if not isinstance(section, dict):
        return []
    return [f"{prefix}{key}={section[key]}" for key in keys if section.get(key) is not None]


def _config_failures(config: Any) -> list[str]:
    """Return fairness-sensitive overrides in a Harbor job/trial config."""
    if not isinstance(config, dict):
        return ["missing config"]
    failures: list[str] = []
    timeout_multiplier = config.get("timeout_multiplier")
    if timeout_multiplier is not None and timeout_multiplier != 1.0:
        failures.append(f"timeout_multiplier={timeout_multiplier}")
    failures.extend(_section_overrides(config, _MULTIPLIERS))

    agents = config.get("agents")
    if isinstance(agents, list):
        for index, agent in enumerate(agents):
            failures.extend(
                _section_overrides(agent, _AGENT_OVERRIDES, f"agents[{index}].")
            )
    failures.extend(_section_overrides(config.get("agent"), _AGENT_OVERRIDES, "agent."))
    failures.extend(
        _section_overrides(config.get("verifier"), _VERIFIER_OVERRIDES, "verifier.")
    )
    failures.extend(
        _section_overrides(config.get("environment"), _ENVIRONMENT_OVERRIDES, "environment.")
    )
    return failures


def _assert_config_clean(config: Any, *, label: str) -> None:
    failures = _config_failures(config)
    if failures:
        raise HarborVerificationError(
            f"{label} contains fairness-sensitive overrides: {', '.join(failures)}."
        )


def _trajectory_path(trial: dict[str, Any], detail: dict[str, Any]) -> Any:
    value = detail.get("trajectory_path") or trial.get("trajectory_path")
    if isinstance(value, dict):
        return value.get("path") or value.get("url")
    return value


def _trial_identity(trial: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
    """Read the Harbor agent/model identity when the bulk row exposes it."""
    keys = ("agent_name", "agent_version", "model_provider", "model_name")
    if not any(trial.get(key) is not None for key in keys):
        return None
    missing = [key for key in keys if trial.get(key) in (None, "")]
    if missing:
        raise HarborVerificationError(
            f"Trial {trial.get('id') or trial.get('trial_id')} has incomplete agent/model identity: "
            f"{', '.join(missing)}."
        )
    return tuple((key, str(trial[key])) for key in keys)


def _score_sources(trial: dict[str, Any], detail: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return Harbor fields that can contain DuMateBench reward artifacts.

    Harbor's trial-detail envelope has changed shape between integrations. The
    verifier deliberately follows only result-shaped fields, rather than
    recursively searching arbitrary task/config data for a key named
    ``final_score``.
    """
    sources: list[tuple[str, dict[str, Any]]] = []
    seen: set[int] = set()
    nested_keys = {
        "verifier_result",
        "reward",
        "rewards",
        "metrics",
        "result",
        "rule_result",
        "llm_judge",
        "report",
    }

    def visit(value: Any, path: str, depth: int) -> None:
        if not isinstance(value, dict) or id(value) in seen or depth > 5:
            return
        seen.add(id(value))
        sources.append((path, value))
        for key in nested_keys:
            child = value.get(key)
            if isinstance(child, dict):
                visit(child, f"{path}.{key}", depth + 1)

    visit(trial, "trial", 0)
    visit(detail, "detail", 0)
    return sources


def _metric_value(
    sources: list[tuple[str, dict[str, Any]]],
    names: tuple[str, ...],
    *,
    required: bool,
    trial_id: str,
    label: str,
) -> float | None:
    values: list[tuple[str, float]] = []
    for path, source in sources:
        for name in names:
            if name not in source:
                continue
            value = _numeric(source[name])
            if value is None:
                raise HarborVerificationError(
                    f"Trial {trial_id} has non-numeric DuMateBench {label} at {path}.{name}."
                )
            values.append((f"{path}.{name}", value))

    if not values:
        if required:
            raise HarborVerificationError(
                f"Trial {trial_id} has no DuMateBench {label}; Harbor reward is not enough."
            )
        return None

    first = values[0][1]
    if any(value != first for _, value in values[1:]):
        locations = ", ".join(f"{path}={value}" for path, value in values)
        raise HarborVerificationError(
            f"Trial {trial_id} has conflicting DuMateBench {label}: {locations}."
        )
    return first


def _validate_unit_score(value: float, *, trial_id: str, label: str) -> float:
    if not 0 <= value <= 1:
        raise HarborVerificationError(
            f"Trial {trial_id} DuMateBench {label} is outside [0, 1]: {value}."
        )
    return value


def _validate_judge_score(value: float, *, trial_id: str) -> float:
    if not 0 <= value <= 100:
        raise HarborVerificationError(
            f"Trial {trial_id} DuMateBench llm_judge_score is outside [0, 100]: {value}."
        )
    return value / 100 if value > 1 else value


def _checks_score(source: dict[str, Any], *, trial_id: str) -> tuple[int, float] | None:
    checks = source.get("checks")
    if not isinstance(checks, list):
        return None
    if not checks:
        raise HarborVerificationError(f"Trial {trial_id} has an empty DuMateBench checks list.")

    weighted_total = 0.0
    weighted_passed = 0.0
    all_passed = True
    for index, item in enumerate(checks, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("passed"), bool):
            raise HarborVerificationError(
                f"Trial {trial_id} has an invalid DuMateBench check at index {index}."
            )
        raw_weight = item.get("weight", 1.0)
        weight = _numeric(raw_weight)
        if weight is None or weight <= 0:
            raise HarborVerificationError(
                f"Trial {trial_id} has an invalid DuMateBench check weight at index {index}."
            )
        weighted_total += weight
        weighted_passed += weight if item["passed"] else 0.0
        all_passed = all_passed and item["passed"]
    return int(all_passed), round(weighted_passed / weighted_total, 4)


def extract_dumate_scores(
    trial: dict[str, Any], detail: dict[str, Any], *, trial_id: str
) -> dict[str, float | int | None]:
    """Extract and validate DuMateBench scores from one Harbor trial detail.

    ``reward`` is intentionally not a fallback: it is Harbor's generic scalar
    and does not identify DuMateBench's checklist metrics. LLM fields are
    optional because the standard Harbor evaluator emits only the checklist
    metrics; when present, the final score is recomputed with DuMateBench's
    shared 30/30/40 formula.
    """
    sources = _score_sources(trial, detail)
    complete = _metric_value(
        sources,
        ("base_complete_pass", "complete_pass"),
        required=False,
        trial_id=trial_id,
        label="complete_pass",
    )
    partial = _metric_value(
        sources,
        ("base_partial_pass", "partial_pass"),
        required=False,
        trial_id=trial_id,
        label="partial_pass",
    )

    # If a raw reward object includes checks, use those checks to prove that
    # the emitted aggregate is consistent with the DuMateBench evaluator.
    for path, source in sources:
        checked = _checks_score(source, trial_id=trial_id)
        if checked is None:
            continue
        check_complete, check_partial = checked
        if complete is not None and complete != check_complete:
            raise HarborVerificationError(
                f"Trial {trial_id} complete_pass disagrees with checks at {path}."
            )
        if partial is not None and round(partial, 4) != check_partial:
            raise HarborVerificationError(
                f"Trial {trial_id} partial_pass disagrees with checks at {path}."
            )
        complete = float(check_complete)
        partial = check_partial
        break

    if complete is None or partial is None:
        if trial.get("error_type") is not None:
            complete, partial = 0.0, 0.0
        else:
            missing = "complete_pass" if complete is None else "partial_pass"
            raise HarborVerificationError(
                f"Trial {trial_id} has no DuMateBench {missing}; Harbor reward is not enough."
            )

    complete = _validate_unit_score(float(complete), trial_id=trial_id, label="complete_pass")
    partial = _validate_unit_score(float(partial), trial_id=trial_id, label="partial_pass")
    judge_raw = _metric_value(
        sources,
        ("llm_judge_score", "ppt_llm_judge_score"),
        required=False,
        trial_id=trial_id,
        label="llm_judge_score",
    )
    final = _metric_value(
        sources,
        ("final_score",),
        required=False,
        trial_id=trial_id,
        label="final_score",
    )
    judge = _validate_judge_score(judge_raw, trial_id=trial_id) if judge_raw is not None else None
    if (judge is None) != (final is None):
        raise HarborVerificationError(
            f"Trial {trial_id} must provide both llm_judge_score and final_score, or neither."
        )
    if final is not None:
        final = _validate_unit_score(float(final), trial_id=trial_id, label="final_score")
        recomputed = dumate_final_score(complete, partial, judge)
        if abs(final - recomputed) > 0.0001:
            raise HarborVerificationError(
                f"Trial {trial_id} final_score mismatch: recorded {final}, "
                f"recomputed {recomputed}."
            )

    return {
        "complete_pass": round(complete, 4),
        "partial_pass": round(partial, 4),
        "base_complete_pass": round(complete, 4),
        "base_partial_pass": round(partial, 4),
        "llm_judge_score": round(judge, 4) if judge is not None else None,
        "final_score": round(final, 4) if final is not None else None,
    }


def _job_is_finished(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or job.get("state") or "").lower()
    if status in {"failed", "failure", "error", "errored", "cancelled", "canceled", "aborted", "timeout", "timed_out"}:
        return False
    if not status and job.get("finished_at"):
        return True
    return status in {"completed", "complete", "succeeded", "success", "finished"}


def verify_job(
    manifest: dict[str, Any],
    *,
    dataset: str,
    dataset_ref: str,
    expected_task_count: int,
    min_trials_per_task: int,
    clone_prefix: str | None = None,
    job_loader: Callable[[list[str]], dict[str, Any]] = harbor_json,
    trial_loader: Callable[[str], list[dict[str, Any]]] = harbor_job_trials,
    detail_loader: Callable[[list[str]], dict[str, dict[str, Any]]] = harbor_trial_details,
    digest_loader: Callable[[str, str], dict[str, str]] = dataset_task_digests,
) -> dict[str, Any]:
    """Verify one manifest using only Harbor as the result source."""
    if not dataset or not dataset_ref:
        raise HarborVerificationError(
            "Both the canonical Harbor dataset name and revision must be configured."
        )
    if expected_task_count <= 0 or min_trials_per_task <= 0:
        raise HarborVerificationError("Task count and minimum trials must be positive.")

    job_id = job_uuid(str(manifest["harbor_job_id"]))
    if not job_id:
        raise HarborVerificationError("Submission has an empty Harbor job ID.")
    overview = job_loader(["job", "show", job_id])

    if dataset not in _dataset_names(overview):
        names = ", ".join(sorted(_dataset_names(overview))) or "none"
        raise HarborVerificationError(
            f"Harbor job {job_id} did not run {dataset}; recorded datasets: {names}"
        )
    if not _job_is_finished(overview):
        raise HarborVerificationError(f"Harbor job {job_id} has not completed successfully.")
    _assert_config_clean(overview.get("config"), label=f"Harbor job {job_id}")
    if clone_prefix:
        name = str(overview.get("name") or "")
        if not name.startswith(f"{clone_prefix}/"):
            raise HarborVerificationError(
                f"Harbor job {job_id} is not a leaderboard-owned clone: {name!r}"
            )

    trials = trial_loader(job_id)
    if not trials:
        raise HarborVerificationError(f"Harbor job {job_id} has no trials.")
    trial_ids = [str(t.get("id") or t.get("trial_id") or "") for t in trials]
    if any(not value for value in trial_ids):
        raise HarborVerificationError("Every Harbor trial must expose an ID.")
    if len(trial_ids) != len(set(trial_ids)):
        raise HarborVerificationError("Harbor returned duplicate trial IDs.")

    details = detail_loader(trial_ids)
    if set(details) != set(trial_ids):
        raise HarborVerificationError("Harbor did not return details for every trial.")
    expected_digests = digest_loader(dataset, dataset_ref)

    task_counts: dict[str, int] = {}
    trial_identities: set[tuple[tuple[str, str], ...]] = set()
    dumate_scores: list[dict[str, float | int | None]] = []
    for trial, trial_id in zip(trials, trial_ids):
        detail = details[trial_id]
        identity = _trial_identity(trial)
        if identity is not None:
            trial_identities.add(identity)
        source = trial.get("source") or trial.get("dataset")
        if source is not None and source != dataset:
            raise HarborVerificationError(
                f"Trial {trial_id} belongs to {source!r}, not {dataset!r}."
            )
        name = _task_name(trial, detail)
        if not name:
            raise HarborVerificationError(f"Trial {trial_id} has no task identity.")
        task_counts[name] = task_counts.get(name, 0) + 1

        config = detail.get("config")
        _assert_config_clean(config, label=f"Harbor trial {trial_id}")
        if not isinstance(config, dict):
            raise HarborVerificationError(f"Trial {trial_id} has no config.")
        task_config = config.get("task") or {}
        recorded_ref = task_config.get("ref") if isinstance(task_config, dict) else None
        expected_ref = expected_digests.get(name)
        if expected_ref is None:
            raise HarborVerificationError(
                f"Trial {trial_id} task {name!r} is not in {dataset}@{dataset_ref}."
            )
        if recorded_ref != expected_ref:
            raise HarborVerificationError(
                f"Trial {trial_id} task digest mismatch for {name!r}."
            )

        scores = extract_dumate_scores(trial, detail, trial_id=trial_id)
        if float(scores["partial_pass"] or 0) > 0 and not _trajectory_path(trial, detail):
            raise HarborVerificationError(
                f"Trial {trial_id} has a passing DuMateBench result but no trajectory_path."
            )
        dumate_scores.append(scores)

    if len(task_counts) != expected_task_count:
        raise HarborVerificationError(
            f"Expected {expected_task_count} tasks, found {len(task_counts)}."
        )
    underfilled = sorted(
        task for task, count in task_counts.items() if count < min_trials_per_task
    )
    if underfilled:
        raise HarborVerificationError(
            f"Tasks below {min_trials_per_task} trial(s): {underfilled[:10]}"
        )
    if len(trial_identities) > 1:
        identities = [dict(identity) for identity in sorted(trial_identities)]
        raise HarborVerificationError(
            f"Harbor job {job_id} mixes multiple agent/model identities: {identities}."
        )

    final_scores = [score["final_score"] for score in dumate_scores]
    has_final_score = any(value is not None for value in final_scores)
    if has_final_score and not all(value is not None for value in final_scores):
        raise HarborVerificationError(
            "Every Harbor trial must provide a DuMateBench final_score when any trial uses LLM judge scoring."
        )

    def mean(field: str) -> float:
        return round(sum(float(score[field]) for score in dumate_scores) / len(dumate_scores), 6)

    report: dict[str, Any] = {
        "status": "verified",
        "harbor_job_id": job_id,
        "dataset": dataset,
        "dataset_ref": dataset_ref,
        "task_count": len(task_counts),
        "trial_count": len(trials),
        "trials_by_task": dict(sorted(task_counts.items())),
        "trial_ids": sorted(trial_ids),
        "trial_identity": dict(next(iter(trial_identities))) if trial_identities else None,
        "dumatebench_score_mode": "with_llm_judge" if has_final_score else "checklist",
        "complete_pass_mean": mean("complete_pass"),
        "partial_pass_mean": mean("partial_pass"),
        "final_score_mean": mean("final_score") if has_final_score else None,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    return report


def verify_manifest(
    path: Path,
    *,
    dataset: str,
    dataset_ref: str,
    expected_task_count: int,
    min_trials_per_task: int,
    clone_prefix: str | None = None,
    write_verification: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    errors = _validate_manifest(path)
    if errors:
        raise HarborVerificationError("Manifest validation failed: " + "; ".join(errors))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    report = verify_job(
        manifest,
        dataset=dataset,
        dataset_ref=dataset_ref,
        expected_task_count=expected_task_count,
        min_trials_per_task=min_trials_per_task,
        clone_prefix=clone_prefix,
    )
    if write_verification:
        previous = manifest.get("verification")
        if isinstance(previous, dict):
            for key in ("source_harbor_job_id", "snapshot_name_prefix"):
                if key in previous:
                    report[key] = previous[key]
        manifest["verification"] = report
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if report_path:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a DuMateBench Harbor submission.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--dataset-ref", required=True)
    parser.add_argument("--expected-task-count", type=int, required=True)
    parser.add_argument("--min-trials-per-task", type=int, default=1)
    parser.add_argument("--clone-prefix")
    parser.add_argument("--write-verification", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = verify_manifest(
            args.manifest,
            dataset=args.dataset,
            dataset_ref=args.dataset_ref,
            expected_task_count=args.expected_task_count,
            min_trials_per_task=args.min_trials_per_task,
            clone_prefix=args.clone_prefix,
            write_verification=args.write_verification,
            report_path=args.report,
        )
    except (HarborVerificationError, OSError, json.JSONDecodeError) as exc:
        print(f"Harbor verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Harbor verification passed: {report['task_count']} task(s), "
        f"{report['trial_count']} trial(s), "
        f"partial_pass={report['partial_pass_mean']}, "
        f"job {report['harbor_job_id']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
