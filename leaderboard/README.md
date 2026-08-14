# DuMateBench submission CI

The benchmark code runs agents and produces task-level artifacts. The
submission CI is the receiving side of that process: a participant proposes a
new result by adding one submission bundle under `submissions/` in a pull
request.

The formal submission format is a small JSON manifest containing a Harbor job
ID. The first version checks that manifest's shape and intentionally rejects a
claimed score. The next verification phase will use the job ID as the source
of truth, fetch the original trials, check the canonical dataset and runtime
configuration, and recompute metrics. The existing bundle format is also
accepted as a local-evidence intake format during this transition.

The workflow uses `pull_request_target` but checks out only the base branch.
Files from the PR are fetched through the GitHub API as data, so a submission
cannot replace the validator with code that runs with repository privileges.

## Submission layout

For formal leaderboard intake, add one manifest under:

```text
submissions/dumatebench/<version>/<agent>__<model>.json
```

Its minimum contents are:

```json
{
  "schema_version": 1,
  "harbor_job_id": "job-12345678"
}
```

Run `dumate submission pack` when you also need a local evidence bundle. That
directory may be placed under `submissions/` and is checked for completeness,
but its copied reward values are not trusted as the official score.
