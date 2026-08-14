from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dumatebench_cli.submission import validate_submission, validate_submission_manifest


def make_bundle(root: Path) -> Path:
    bundle = root / "submission"
    task = bundle / "task-alpha"
    task.mkdir(parents=True)
    (bundle / "metadata.yaml").write_text(
        "agent_display_name: Example Agent\n"
        "agent_org_display_name: Example Org\n"
        "models:\n"
        "  - model_name: example-model\n"
        "    model_provider: example\n",
        encoding="utf-8",
    )
    (bundle / "batch_summary.jsonl").write_text(
        json.dumps({
            "task_id": "task-alpha",
            "status": "completed",
            "evaluator_returncode": 0,
        }) + "\n",
        encoding="utf-8",
    )
    (bundle / "config.json").write_text(json.dumps({"max_steps": 20}), encoding="utf-8")
    (task / "reward.json").write_text(
        json.dumps({
            "task_id": "task-alpha",
            "complete_pass": 1,
            "partial_pass": 1.0,
            "checks": [],
        }),
        encoding="utf-8",
    )
    (task / "agent_adapter.jsonl").write_text('{"step": 1}\n', encoding="utf-8")
    (task / "agent_status.json").write_text('{}\n', encoding="utf-8")
    (task / "compose.log").write_text("done\n", encoding="utf-8")
    return bundle


class SubmissionValidationTests(unittest.TestCase):
    def test_harbor_manifest_passes_without_a_claimed_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "submission.json"
            manifest.write_text(
                json.dumps({"schema_version": 1, "harbor_job_id": "job-12345678"}),
                encoding="utf-8",
            )
            self.assertEqual(validate_submission_manifest(manifest), [])

    def test_harbor_manifest_rejects_claimed_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "submission.json"
            manifest.write_text(
                json.dumps({
                    "schema_version": 1,
                    "harbor_job_id": "job-12345678",
                    "score": 0.99,
                }),
                encoding="utf-8",
            )
            errors = validate_submission_manifest(manifest)
            self.assertTrue(any("must not claim results" in error for error in errors))

    def test_complete_bundle_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(validate_submission(make_bundle(Path(tmp))), [])

    def test_missing_trajectory_log_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            (bundle / "task-alpha" / "agent_adapter.jsonl").unlink()
            errors = validate_submission(bundle)
            self.assertTrue(any("missing agent_adapter.jsonl" in error for error in errors))

    def test_claimed_config_score_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            (bundle / "config.json").write_text('{"score": 0.99}\n', encoding="utf-8")
            errors = validate_submission(bundle)
            self.assertTrue(any("claimed results" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
