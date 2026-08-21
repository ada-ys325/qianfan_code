# DuMateBench submission CI

The benchmark code runs agents and produces task-level artifacts. The formal
submission is a small manifest pointing to one Harbor job:

```text
submissions/dumatebench/<version>/<agent>__<model>.json
```

```json
{
  "schema_version": 2,
  "harbor_job_id": "job-12345678",
  "metadata": {
    "agent_display_name": "my-agent",
    "agent_org_display_name": "my-organization",
    "models": [
      {
        "model_name": "my-model",
        "model_provider": "openai"
      }
    ]
  }
}
```

The workflow uses `pull_request_target` but checks out only the base branch.
Files from the PR are fetched through the GitHub API as data, so a submission
cannot replace the validator with code that runs with repository privileges.

The trusted verifier then queries Harbor, checks that the job used the
configured canonical dataset and revision, validates every trial's task digest,
and computes DuMateBench's `complete_pass`/`partial_pass` summary from Harbor's
verifier results. A generic Harbor `reward` scalar is not used as a substitute.
When LLM-judge fields are present, CI recomputes and checks DuMateBench's
`final_score` formula as well. Claimed scores or copied local reward files are
never the official source. It also rejects fairness-sensitive timeout/resource
overrides and requires a Harbor `trajectory_path` for trials with a positive
DuMateBench result so successful runs remain auditable.

CI also binds credit to the run: the manifest's declared agent/model must equal
the agent/model identity Harbor recorded on the trials, and a job that exposes
no identity is rejected rather than trusted on the submitter's word. Each run
may be submitted only once — CI rejects a job ID already claimed by another
submission or another open PR, and compares a `run_fingerprint` computed from
every trial's scored outcome so a `harbor hub job copy` of an already-submitted
run is caught despite its new job ID.

Known gap: Harbor's read APIs do not expose a job's `created_by`, so CI cannot
prove that the submitter personally produced a public job that has never been
submitted before. Requiring submitters to share jobs with the leaderboard
account instead of publishing them publicly is the mitigation under discussion.

After verification, CI copies the source job into a leaderboard-owned Harbor
snapshot, verifies the copy again, and opens a bot PR. The bot PR is the final
reviewable record; the original intake PR is closed automatically.

If an unmerged bot PR is closed, a separate trusted workflow deletes only the
Harbor clone whose name carries that bot PR's `lb-pr-<number>` prefix. Merged
bot PRs retain their clones as the leaderboard record.

## Submission layout

For formal leaderboard intake, add one manifest under:

```text
submissions/dumatebench/<version>/<agent>__<model>.json
```

Its minimum contents are:

```json
{
  "schema_version": 2,
  "harbor_job_id": "job-12345678",
  "metadata": {
    "agent_display_name": "my-agent",
    "agent_org_display_name": "my-organization",
    "models": [
      {
        "model_name": "my-model",
        "model_provider": "openai"
      }
    ]
  }
}
```

Run `dumate submission pack` when you also need a local evidence bundle. That
directory is useful for debugging and manual review, but its copied reward
values are not used by the official Harbor verification.

## Repository configuration

Set these repository variables before enabling formal submissions:

```text
DUMATEBENCH_HARBOR_DATASET
DUMATEBENCH_HARBOR_DATASET_REF
DUMATEBENCH_EXPECTED_TASK_COUNT       # defaults to 200
DUMATEBENCH_MIN_TRIALS_PER_TASK       # defaults to 5
```

Add `HARBOR_API_KEY` as an Actions secret. The workflow fails closed when the
dataset name, fixed revision, or API key is missing.

The complete task source dataset is distributed outside this repository. The
configured Harbor dataset is the canonical packaged revision used by CI; local
`harbor run --path ...` jobs do not carry that registry identity.
