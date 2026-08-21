# Submission intake

A participant adds one new Harbor job manifest here in a pull request. The
file layout is:

```text
submissions/dumatebench/<version>/<agent>__<model>.json
```

The source manifest must include the agent/model declaration that CI will
compare with Harbor's actual trial identity:

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

Do not add `score`, `accuracy`, `metrics`, or `verification` to a source
manifest. CI runs the trusted verifier from the base branch, fetches the job
and its trials from Harbor, checks the configured canonical dataset revision
and task digests, requires Harbor agent/model identity fields, and compares
them with the manifest declaration. It then recomputes DuMateBench's
`complete_pass`/`partial_pass` from Harbor verifier results. When LLM-judge
fields are present, it also recomputes and checks `final_score` using
DuMateBench's 30% complete + 30% partial + 40% judge formula.

After the source job passes, CI copies it into a leaderboard-owned Harbor job,
re-checks the copy, and opens a bot PR. The bot PR is the record that a
maintainer reviews and merges. A source job ID or self-declared metadata by
itself is not accepted as proof unless the Harbor API checks and identity
comparison pass.

One Harbor run may only be submitted once. CI rejects a job ID that another
submission or another open PR already claims, and it also records a
`run_fingerprint` derived from every trial's scored outcome. Because
`harbor hub job copy` preserves those results, copying an already-submitted
public job into another account is rejected by the fingerprint even though the
copy has a new job ID.

The local evidence bundle from `dumate submission pack` is useful for debugging
and manual review, but it is not an official score source. Official submission
verification uses Harbor rather than copied `reward.json` files.
