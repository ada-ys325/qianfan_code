# Submitting to the DuMateBench leaderboard

This guide covers the full flow for turning a real `harbor run` job into a
leaderboard entry: exporting tasks, running your agent, opening a PR, and what
happens to that PR afterward.

```text
you: dumate harbor export → harbor run --upload → dumate submission from-harbor-job → open PR
    ↓
CI:  harbor_verify.py (re-fetches the job from Harbor, recomputes metrics)
    ↓
CI:  clone_harbor_job.py (leaderboard-owned snapshot) → bot PR → closes your PR
    ↓
maintainer: reviews and merges the bot PR
    ↓
maintainer: adds the merged manifest path to leaderboard/published.json to make it visible on the site
```

## Before you start

- Your run must use the canonical dataset revision configured for this
  repository (`DUMATEBENCH_HARBOR_DATASET` / `DUMATEBENCH_HARBOR_DATASET_REF`).
  CI checks every trial's task digest against this pinned revision.
- You may choose any agent, model, and reasoning effort. You may not change
  fairness-sensitive Harbor settings: `agent_timeout_multiplier`,
  `verifier_timeout_multiplier`, `agent_setup_timeout_multiplier`,
  `environment_build_timeout_multiplier`, or any per-trial `override_*`
  timeout/resource field. CI rejects submissions that touched these.
- Cover at least `DUMATEBENCH_EXPECTED_TASK_COUNT` tasks (defaults to 200) with
  at least `DUMATEBENCH_MIN_TRIALS_PER_TASK` trials each (defaults to 5). A
  trial that errors still counts toward coverage; it is scored as a failure,
  not excluded.
- Run with `--upload --public` (or `harbor upload <job-dir> --public`
  afterward) so the job is public and readable by CI. The verifier only
  trusts data it can independently re-fetch from Harbor — a local
  `reward.json` or a claimed score is never accepted as proof.
- Trials with a positive DuMateBench result must have a Harbor
  `trajectory_path` so the run stays auditable.

## Step 1 — Prepare tasks locally

Convert the dumatebench tasks you want to evaluate into Harbor's native
`task.toml` + `tests/test.sh` layout:

```bash
dumate harbor export \
  --dataset dumatebench/datasets/dev \
  --output /path/to/harbor_tasks
```

Use `--task` instead of `--dataset` to export a single task.

The full source dataset is distributed outside this repository. The export
command supports local development and maintainer-side packaging; a local
directory has no canonical Harbor dataset identity and cannot be used for a
formal leaderboard submission.

## Step 2 — Run your agent with `harbor run`

For local validation of an exported task or dataset, use `--path`:

```bash
harbor run \
  --path /path/to/harbor_tasks \
  --agent <your-agent> \
  --model <provider/model>
```

For a formal submission, use the canonical Harbor registry dataset and its
pinned revision published by the maintainers:

```bash
harbor run \
  --dataset <org>/<dataset>@<revision> \
  --agent <your-agent> \
  --model <provider/model> \
  --n-attempts 5 \
  --upload --public
```

Any Harbor-compatible agent works; Harbor executes it as a native job. Note
the job directory Harbor prints
(`--jobs-dir` default or the path shown in its summary); you need it in the
next step.

Use the top-level `--model` flag to select the model — this is what sets the
agent's `model_name`. Passing a model through `--ak model=...`
(`--agent-kwarg`) instead does not set `model_name`; the run will get through
agent setup and then fail with `ValueError: Model name is required` once the
agent actually starts. `--ak`/`--agent-kwarg` is for other agent constructor
arguments only (for example, routing the Codex agent through a
non-`openai` provider needs `--ak config=<path-to-toml>`), not for the model
name itself.

## Step 3 — Build and validate the submission manifest

```bash
dumate submission from-harbor-job \
  --job-dir /path/to/harbor/jobs/<job-id> \
  --out submissions/dumatebench/<version>/<agent>__<model>.json \
  --agent-name my-agent \
  --agent-org my-organization \
  --model-name my-model \
  --model-provider openai
```

This records only the Harbor `job_id` and the dumatebench `task_id`s it
covers — no score fields. Do not hand-edit in `score`, `accuracy`, `metrics`,
or `final_score`; CI rejects manifests that carry them.

Validate it locally before opening a PR:

```bash
dumate submission check-manifest submissions/dumatebench/<version>/<agent>__<model>.json
```

## Step 4 — Open a PR

Commit the manifest file and open a pull request to this repository's `main`
branch, touching only your new file under `submissions/`. You don't need to
(and shouldn't) modify benchmark code.

## What to expect after opening a PR

1. **CI verification** (automatic, on every push to the PR): `harbor_verify.py`
   runs from the base branch — a submission cannot replace the validator with
   its own code. It re-fetches your job from Harbor, checks the dataset
   revision and every trial's task digest, confirms no fairness-sensitive
   overrides were used, checks task/trial coverage against the configured
   minimums, and recomputes `complete_pass`/`partial_pass` from Harbor's
   verifier results (and `final_score` too, if your run has DuMateBench
   LLM-judge fields). A generic Harbor `reward` scalar is never used as a
   substitute for these.

2. **Promotion** (automatic, once verification passes): CI copies your source
   job into a leaderboard-owned Harbor snapshot, re-verifies the copy, and
   opens a bot PR (branch `submission/pr-<number>`) containing that snapshot. Your
   original intake PR is closed automatically after successful promotion.

3. **Maintainer review and merge**: a maintainer reviews the bot PR and merges
   it. If a bot PR is closed without merging, a separate workflow deletes only
   the Harbor clone carrying that PR's `lb-pr-<number>` prefix. Merged bot PRs
   keep their clone as the permanent leaderboard record.

4. **Publishing to the site** (manual, separate from merging): merging the bot
   PR adds your manifest to `submissions/`, but that alone does not make it
   appear on the public leaderboard site. A maintainer must open a separate,
   ordinary PR adding your manifest's path to
   [`leaderboard/published.json`](published.json) — the maintainer-curated
   allowlist of what's actually shown. This is an intentional second gate, not
   an oversight; ask a maintainer if your merged submission hasn't been added.

See [`leaderboard/README.md`](README.md) for the CI internals and the
repository configuration maintainers need to set up before accepting
submissions.
