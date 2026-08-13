import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dumatebench.scripts.run_llm_judge_only import main, parse_args, run_task_llm_judge
from dumatebench.scripts.run_task_batch import ROOT


class LlmJudgeOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.task = Path(self.tmp.name) / "task"
        (self.task / "workspace_seed").mkdir(parents=True)
        (self.task / "evaluator").mkdir()
        (self.task / "run_outputs" / "reports").mkdir(parents=True)
        (self.task / "instruction.md").write_text("Create the report.", encoding="utf-8")
        (self.task / "task.yaml").write_text("task_id: task\n", encoding="utf-8")
        (self.task / "run_outputs" / "reward.json").write_text(
            '{"complete_pass": 0, "partial_pass": 0.5, "checks": ['
            '{"id": "output", "detail": "{\\"file\\": \\"run_outputs/reports/report.md\\"}"}'
            "]}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_reports_would_run_without_calling_judge(self):
        (self.task / "run_outputs" / "reports" / "report.md").write_text("answer", encoding="utf-8")
        args = parse_args(["--dry-run"])

        result = run_task_llm_judge(self.task, args)

        self.assertEqual(result.status, "would_run")
        self.assertTrue(result.artifact_exists)
        self.assertFalse((self.task / "run_outputs" / "llm_judge_score.json").exists())

    def test_dry_run_force_does_not_delete_existing_reports(self):
        (self.task / "run_outputs" / "reports" / "report.md").write_text("answer", encoding="utf-8")
        report = self.task / "run_outputs" / "llm_judge_score.json"
        report.write_text('{"status": "ok", "judge_score": 1.0}\n', encoding="utf-8")
        args = parse_args(["--dry-run", "--force"])

        result = run_task_llm_judge(self.task, args)

        self.assertEqual(result.status, "would_run")
        self.assertTrue(report.is_file())

    def test_existing_artifact_calls_unified_judge_and_writes_final_reward(self):
        (self.task / "run_outputs" / "reports" / "report.md").write_text("answer", encoding="utf-8")
        args = parse_args(["--llm-judge-model", "mock-model", "--reference-dir", "workspace_seed"])

        def fake_judge(task_dir, judge_args):
            self.assertEqual(Path(task_dir), self.task)
            self.assertEqual(judge_args["output_file"], "run_outputs/reports/report.md")
            self.assertEqual(judge_args["reference_dir"], "workspace_seed")
            self.assertEqual(judge_args["model"], "mock-model")
            (self.task / "run_outputs" / "llm_judge_score.json").write_text(
                '{"status": "ok", "judge_score": 0.8, "final_score": 0.65}\n',
                encoding="utf-8",
            )

        with patch("dumatebench.evaluator.llm_judge.unified.run_llm_judge_score", side_effect=fake_judge) as run_judge:
            result = run_task_llm_judge(self.task, args)

        self.assertEqual(run_judge.call_count, 1)
        self.assertEqual(result.status, "ok")
        final = json.loads((self.task / "run_outputs" / "reward_with_llm_judge.json").read_text(encoding="utf-8"))
        self.assertEqual(final["llm_judge_score"], 0.8)
        self.assertEqual(final["final_score"], 0.65)

    def test_multiple_artifacts_are_judged_and_averaged(self):
        (self.task / "run_outputs" / "reports" / "report.md").write_text("answer", encoding="utf-8")
        (self.task / "run_outputs" / "reports" / "appendix.pdf").write_text("appendix", encoding="utf-8")
        (self.task / "run_outputs" / "reward.json").write_text(
            '{"complete_pass": 0, "partial_pass": 0.5, "checks": ['
            '{"id": "report", "detail": "{\\"file\\": \\"run_outputs/reports/report.md\\"}"},'
            '{"id": "appendix", "detail": "{\\"file\\": \\"run_outputs/reports/appendix.pdf\\"}"}'
            "]}\n",
            encoding="utf-8",
        )
        args = parse_args(["--llm-judge-model", "mock-model"])

        def fake_judge(task_dir, judge_args):
            score = 1.0 if judge_args["output_file"].endswith("report.md") else 0.0
            Path(task_dir, judge_args["judge_output_file"]).parent.mkdir(parents=True, exist_ok=True)
            Path(task_dir, judge_args["judge_output_file"]).write_text(
                json.dumps({"status": "ok", "judge_score": score}) + "\n",
                encoding="utf-8",
            )
            return {"status": "ok", "judge_score": score}

        with patch("dumatebench.evaluator.llm_judge.unified.run_llm_judge_score", side_effect=fake_judge) as run_judge:
            result = run_task_llm_judge(self.task, args)

        self.assertEqual(run_judge.call_count, 2)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output_files, ["run_outputs/reports/report.md", "run_outputs/reports/appendix.pdf"])
        final = json.loads((self.task / "run_outputs" / "reward_with_llm_judge.json").read_text(encoding="utf-8"))
        self.assertEqual(final["llm_judge_score"], 0.5)
        self.assertEqual(final["final_score"], 0.5)

    def test_missing_artifact_writes_zero_final_reward(self):
        args = parse_args([])

        result = run_task_llm_judge(self.task, args)

        self.assertEqual(result.status, "missing_artifact")
        final = json.loads((self.task / "run_outputs" / "reward_with_llm_judge.json").read_text(encoding="utf-8"))
        self.assertEqual(final["llm_judge_score"], 0.0)
        self.assertEqual(final["final_score"], 0.25)
        self.assertEqual(final["llm_judge"]["status"], "missing_artifact")

    def test_main_returns_success_for_tasks_without_inferred_output(self):
        task_without_output = Path(self.tmp.name) / "task_without_output"
        (task_without_output / "workspace_seed").mkdir(parents=True)
        (task_without_output / "evaluator").mkdir()
        (task_without_output / "instruction.md").write_text("No explicit output.", encoding="utf-8")
        (task_without_output / "task.yaml").write_text("task_id: task_without_output\n", encoding="utf-8")

        code = main([
            "--tasks-dir",
            str(Path(self.tmp.name)),
            "--task-glob",
            "*",
            "--dry-run",
        ])

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
