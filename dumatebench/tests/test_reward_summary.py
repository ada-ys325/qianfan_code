import json
import tempfile
import unittest
from pathlib import Path

from dumatebench.scripts.summarize_llm_judge_rewards import ROOT, aggregate_summaries, collect_summaries, discover_reward_files, iter_scan_roots


class RewardSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collects_nested_task_reward_scores(self):
        task = self.root / "group1" / "5e64adb96e194001823c3ec5a3c4a5df" / "5e64adb96e194001823c3ec5a3c4a5df_ses_11fb82267ffeTOriNoagCSz8D5"
        reward = task / "run_outputs" / "reward_with_llm_judge.json"
        reward.parent.mkdir(parents=True)
        reward.write_text(
            json.dumps(
                {
                    "task_id": task.name,
                    "base_complete_pass": 1,
                    "base_partial_pass": 0.75,
                    "llm_judge_score": 0.6,
                    "final_score": 0.675,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        rewards = discover_reward_files(self.root)
        summaries = collect_summaries(self.root)

        self.assertEqual(rewards, [reward])
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].task_id, task.name)
        self.assertEqual(summaries[0].base_complete_pass, 1)
        self.assertEqual(summaries[0].base_partial_pass, 0.75)
        self.assertEqual(summaries[0].llm_judge_score, 0.6)
        self.assertEqual(summaries[0].final_score, 0.765)

    def test_include_missing_lists_tasks_without_final_reward(self):
        task = self.root / "group" / "task_ses_missing"
        task.mkdir(parents=True)
        (task / "task.yaml").write_text("task_id: missing\n", encoding="utf-8")
        (task / "instruction.md").write_text("instruction\n", encoding="utf-8")

        summaries = collect_summaries(self.root, include_missing=True)

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].task_id, "task_ses_missing")
        self.assertIsNone(summaries[0].base_complete_pass)
        self.assertIsNone(summaries[0].base_partial_pass)
        self.assertIsNone(summaries[0].llm_judge_score)
        self.assertIsNone(summaries[0].final_score)

    def test_aggregate_counts_tasks_and_average_scores(self):
        for index, final_score in enumerate((0.25, 0.75), start=1):
            task = self.root / f"task_{index}"
            reward = task / "run_outputs" / "reward_with_llm_judge.json"
            reward.parent.mkdir(parents=True)
            reward.write_text(
                json.dumps(
                    {
                        "task_id": task.name,
                        "base_complete_pass": index - 1,
                        "base_partial_pass": final_score,
                        "llm_judge_score": final_score + 0.1,
                        "final_score": final_score,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        missing = self.root / "missing_task"
        missing.mkdir()
        (missing / "task.yaml").write_text("task_id: missing\n", encoding="utf-8")
        (missing / "instruction.md").write_text("instruction\n", encoding="utf-8")

        stats = aggregate_summaries(collect_summaries(self.root, include_missing=True))

        self.assertEqual(stats["task_count"], 3)
        self.assertEqual(stats["reward_count"], 2)
        self.assertEqual(stats["missing_reward_count"], 1)
        self.assertEqual(stats["avg_base_complete_pass"], 0.5)
        self.assertEqual(stats["avg_base_partial_pass"], 0.5)
        self.assertEqual(stats["avg_llm_judge_score"], 0.6)
        self.assertEqual(stats["avg_final_score"], 0.54)

    def test_dir_glob_limits_scan_to_matching_directories(self):
        wanted = self.root / "group_1" / "task_a"
        ignored = self.root / "archive" / "task_b"
        for task, final_score in ((wanted, 0.8), (ignored, 0.1)):
            reward = task / "run_outputs" / "reward_with_llm_judge.json"
            reward.parent.mkdir(parents=True)
            reward.write_text(
                json.dumps(
                    {
                        "task_id": task.name,
                        "base_complete_pass": 1,
                        "base_partial_pass": final_score,
                        "llm_judge_score": final_score,
                        "final_score": final_score,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

        summaries = collect_summaries(self.root, dir_glob="group_*")

        self.assertEqual([Path(summary.task_dir).name for summary in summaries], ["task_a"])
        self.assertEqual(summaries[0].final_score, 0.86)

    def test_dir_glob_uses_direct_child_directories_only(self):
        direct = self.root / "group_1"
        nested = self.root / "nested" / "group_2"
        direct.mkdir()
        nested.mkdir(parents=True)

        self.assertEqual(iter_scan_roots(self.root, "group_*"), [direct])


if __name__ == "__main__":
    unittest.main()
