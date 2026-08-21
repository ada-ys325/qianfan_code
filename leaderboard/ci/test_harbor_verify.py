from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from leaderboard.ci import harbor_verify


class HarborVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {
            "schema_version": 2,
            "harbor_job_id": "job-12345678",
            "metadata": {
                "agent_display_name": "agent",
                "agent_org_display_name": "org",
                "models": [{"model_name": "model-a", "model_provider": "provider"}],
            },
        }
        self.job = {
            "name": "source/job-12345678",
            "finished_at": "2026-08-14T00:00:00Z",
            "config": {"datasets": [{"name": "dumatebench/dataset"}]},
        }
        self.trials = [
            {
                "id": "trial-1",
                "source": "dumatebench/dataset",
                "task_name": "task-1",
                "reward": 0.99,
                "agent_name": "agent",
                "agent_version": "1.0.0",
                "model_provider": "provider",
                "model_name": "model-a",
            },
            {
                "id": "trial-2",
                "source": "dumatebench/dataset",
                "task_name": "task-2",
                "reward": 0.01,
                "agent_name": "agent",
                "agent_version": "1.0.0",
                "model_provider": "provider",
                "model_name": "model-a",
            },
        ]
        self.details = {
            "trial-1": {
                "config": {"task": {"name": "task-1", "ref": "sha256:one"}},
                "trajectory_path": "trials/trial-1.jsonl",
                "verifier_result": {
                    "rewards": {
                        "complete_pass": 1,
                        "partial_pass": 1.0,
                        "llm_judge_score": 0.8,
                        "final_score": 0.92,
                    }
                },
            },
            "trial-2": {
                "config": {"task": {"name": "task-2", "ref": "sha256:two"}},
                "trajectory_path": "trials/trial-2.jsonl",
                "verifier_result": {
                    "rewards": {
                        "complete_pass": 0,
                        "partial_pass": 0.5,
                        "llm_judge_score": 0.4,
                        "final_score": 0.31,
                    }
                },
            },
        }

    def verify(self, **kwargs):
        return harbor_verify.verify_job(
            self.manifest,
            dataset="dumatebench/dataset",
            dataset_ref="sha256:dataset",
            expected_task_count=2,
            min_trials_per_task=1,
            job_loader=lambda _args: self.job,
            trial_loader=lambda _job_id: self.trials,
            detail_loader=lambda _trial_ids: self.details,
            digest_loader=lambda _dataset, _revision: {
                "task-1": "sha256:one",
                "task-2": "sha256:two",
            },
            **kwargs,
        )

    def test_verifies_harbor_data_and_recomputes_dumatebench_summary(self):
        report = self.verify()

        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["task_count"], 2)
        self.assertEqual(report["trial_count"], 2)
        self.assertEqual(report["dumatebench_score_mode"], "with_llm_judge")
        self.assertEqual(report["complete_pass_mean"], 0.5)
        self.assertEqual(report["partial_pass_mean"], 0.75)
        self.assertEqual(report["final_score_mean"], 0.615)

    def test_ignores_generic_harbor_reward_when_dumate_metrics_are_present(self):
        self.details["trial-1"]["verifier_result"]["rewards"].pop("final_score")
        self.details["trial-1"]["verifier_result"]["rewards"].pop("llm_judge_score")
        self.details["trial-2"]["verifier_result"]["rewards"].pop("final_score")
        self.details["trial-2"]["verifier_result"]["rewards"].pop("llm_judge_score")

        report = self.verify()

        self.assertEqual(report["dumatebench_score_mode"], "checklist")
        self.assertEqual(report["complete_pass_mean"], 0.5)
        self.assertEqual(report["partial_pass_mean"], 0.75)
        self.assertIsNone(report["final_score_mean"])

    def test_rejects_generic_harbor_reward_without_dumate_metrics(self):
        for detail in self.details.values():
            detail["verifier_result"] = {"rewards": {"reward": 1.0}}

        with self.assertRaisesRegex(harbor_verify.HarborVerificationError, "complete_pass"):
            self.verify()

    def test_recomputes_weighted_partial_pass_from_dumate_checks(self):
        reward = self.details["trial-1"]["verifier_result"]["rewards"]
        reward["checks"] = [
            {"passed": True, "weight": 0.25},
            {"passed": True, "weight": 0.75},
        ]
        report = self.verify()

        self.assertEqual(report["partial_pass_mean"], 0.75)

    def test_rejects_final_score_that_does_not_match_dumate_formula(self):
        self.details["trial-2"]["verifier_result"]["rewards"]["final_score"] = 0.5

        with self.assertRaisesRegex(harbor_verify.HarborVerificationError, "final_score mismatch"):
            self.verify()

    def test_rejects_job_timeout_or_resource_overrides(self):
        self.job["config"]["environment"] = {"override_memory_mb": 8192}

        with self.assertRaisesRegex(harbor_verify.HarborVerificationError, "overrides"):
            self.verify()

    def test_rejects_per_trial_timeout_or_resource_overrides(self):
        self.details["trial-2"]["config"]["agent"] = {"override_timeout_sec": 3600}

        with self.assertRaisesRegex(harbor_verify.HarborVerificationError, "overrides"):
            self.verify()

    def test_requires_trajectory_for_a_passing_trial(self):
        self.details["trial-1"].pop("trajectory_path")

        with self.assertRaisesRegex(harbor_verify.HarborVerificationError, "trajectory_path"):
            self.verify()

    def test_rejects_mixed_agent_or_model_identities(self):
        for trial in self.trials:
            trial.update(
                {
                    "agent_name": "agent",
                    "agent_version": "1.0.0",
                    "model_provider": "provider",
                    "model_name": "model-a",
                }
            )
        self.trials[1]["model_name"] = "model-b"

        with self.assertRaisesRegex(harbor_verify.HarborVerificationError, "mixes multiple"):
            self.verify()

    def test_rejects_manifest_identity_that_does_not_match_harbor(self):
        self.manifest["metadata"]["models"][0]["model_name"] = "model-b"

        with self.assertRaisesRegex(harbor_verify.HarborVerificationError, "does not match Harbor"):
            self.verify()

    def test_requires_harbor_agent_and_model_identity(self):
        for trial in self.trials:
            for key in ("agent_name", "agent_version", "model_provider", "model_name"):
                trial.pop(key)

        with self.assertRaisesRegex(harbor_verify.HarborVerificationError, "no agent/model identity"):
            self.verify()

    def test_detects_duplicate_source_job_in_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "submissions" / "dumatebench" / "v1" / "existing.json"
            existing.parent.mkdir(parents=True)
            existing.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "harbor_job_id": "job-87654321",
                        "verification": {
                            "source_harbor_job_id": "job-12345678",
                        },
                    }
                ),
                encoding="utf-8",
            )

            duplicates = harbor_verify.duplicate_submission_paths(
                {"harbor_job_id": "https://harbor.example/jobs/job-12345678"},
                root,
            )

            self.assertEqual(duplicates, [existing])

    def test_report_carries_a_run_fingerprint(self):
        report = self.verify()

        self.assertTrue(report["run_fingerprint"].startswith("sha256:"))

    def test_run_fingerprint_ignores_job_and_trial_ids(self):
        original = self.verify()["run_fingerprint"]

        # A `harbor hub job copy` keeps the scored results but gets fresh IDs.
        self.manifest["harbor_job_id"] = "job-99999999"
        self.job["name"] = "copier/job-99999999"
        for index, trial in enumerate(self.trials, start=1):
            trial["id"] = f"copied-trial-{index}"
        self.details = {
            f"copied-trial-{index}": detail
            for index, detail in enumerate(self.details.values(), start=1)
        }

        self.assertEqual(self.verify()["run_fingerprint"], original)

    def test_run_fingerprint_changes_with_scores(self):
        original = self.verify()["run_fingerprint"]
        self.details["trial-2"]["verifier_result"]["rewards"].update(
            {"partial_pass": 0.25, "final_score": 0.235}
        )

        self.assertNotEqual(self.verify()["run_fingerprint"], original)

    def test_detects_a_copied_run_by_fingerprint(self):
        report = self.verify()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "submissions" / "dumatebench" / "v1" / "existing.json"
            existing.parent.mkdir(parents=True)
            existing.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "harbor_job_id": "job-87654321",
                        "verification": {"run_fingerprint": report["run_fingerprint"]},
                    }
                ),
                encoding="utf-8",
            )

            duplicates = harbor_verify.duplicate_submission_paths(
                {
                    "harbor_job_id": "job-99999999",
                    "verification": {"run_fingerprint": report["run_fingerprint"]},
                },
                root,
            )

            self.assertEqual(duplicates, [existing])

    def test_rejects_wrong_task_digest(self):
        self.details["trial-2"]["config"]["task"]["ref"] = "sha256:tampered"

        with self.assertRaises(harbor_verify.HarborVerificationError):
            self.verify()

    def test_rejects_unfinished_job(self):
        self.job["finished_at"] = None
        self.job["status"] = "running"

        with self.assertRaises(harbor_verify.HarborVerificationError):
            self.verify()

    def test_rejects_failed_job_even_when_it_has_finished_at(self):
        self.job["status"] = "failed"

        with self.assertRaises(harbor_verify.HarborVerificationError):
            self.verify()

    def test_requires_leaderboard_owned_clone_when_requested(self):
        self.job["name"] = "lb-pr-7/job-12345678"
        report = self.verify(clone_prefix="lb-pr-7")

        self.assertEqual(report["harbor_job_id"], "job-12345678")


if __name__ == "__main__":
    unittest.main()
