# Submission intake

A participant adds one new Harbor job manifest here in a pull request. The
file layout is:

```text
submissions/dumatebench/<version>/<agent>__<model>.json
```

The minimum manifest is:

```json
{
  "schema_version": 1,
  "harbor_job_id": "job-12345678"
}
```

Do not add `score`, `accuracy`, `metrics`, or `verification`. CI runs the
trusted verifier from the base branch, fetches the job and its trials from
Harbor, checks the configured canonical dataset revision and task digests, and
recomputes DuMateBench's `complete_pass`/`partial_pass` from Harbor verifier
results. When LLM-judge fields are present, it also recomputes and checks
`final_score` using DuMateBench's 30% complete + 30% partial + 40% judge formula.

After the source job passes, CI copies it into a leaderboard-owned Harbor job,
re-checks the copy, and opens a bot PR. The bot PR is the record that a
maintainer reviews and merges. A source job ID by itself is not accepted as
proof unless the Harbor API checks pass.

The local evidence bundle from `dumate submission pack` is useful for debugging
and manual review, but it is not an official score source. Official submission
verification uses Harbor rather than copied `reward.json` files.
