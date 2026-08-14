from __future__ import annotations

import unittest

from leaderboard.ci import harbor_verify


class HarborVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {"schema_version": 1, "harbor_job_id": "job-12345678"}
        self.job = {
            "name": "source/job-12345678",
            "finished_at": "2026-08-14T00:00:00Z",
            "config": {"datasets": [{"name": "dumatebench/dataset"}]},
        }
        self.trials = [
            {"id": "trial-1", "source": "dumatebench/dataset", "task_name": "task-1", "reward": 0.99},
            {"id": "trial-2", "source": "dumatebench/dataset", "task_name": "task-2", "reward": 0.01},
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
