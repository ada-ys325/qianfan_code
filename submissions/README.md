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

Do not add `score`, `accuracy`, or `metrics`. CI will fetch the real trials from
Harbor and recompute the official metrics in the next verification phase.

The older local evidence bundle from `dumate submission pack` is accepted under
the corresponding directory layout while the intake format is being migrated.
