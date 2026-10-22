from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dumatebench_cli.submission import pack_submission, validate_submission


class SubmissionCanonicalTaskIdTests(unittest.TestCase):
    def test_pack_uses_task_yaml_id_for_summary_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = root / "task_1"
            (task / "run_outputs").mkdir(parents=True)
            (task / "run_logs").mkdir()
            task_id = "canonical-task-id"
            (task / "task.yaml").write_text(f"task_id: {task_id}\n", encoding="utf-8")
            (task / "run_outputs" / "reward.json").write_text(
                json.dumps({"task_id": task_id, "complete_pass": 1, "partial_pass": 1}),
                encoding="utf-8",
            )
            (task / "run_logs" / "agent_status.json").write_text("{}\n", encoding="utf-8")
            (task / "run_logs" / "agent_adapter.jsonl").write_text(
                '{"step": 1, "action": {"finish": true}}\n', encoding="utf-8"
            )
            (task / "run_logs" / "compose.log").write_text("complete\n", encoding="utf-8")

            summary = root / "batch_summary.jsonl"
            summary.write_text(
                json.dumps({
                    "task_id": "task_1",
                    "task_dir": str(task),
                    "status": "completed",
                    "evaluator_returncode": 0,
                })
                + "\n",
                encoding="utf-8",
            )
            bundle = root / "bundle"

            result = pack_submission(
                summary_path=summary,
                out_dir=bundle,
                agent_name="Agent",
                agent_org="Org",
                model_name="Model",
                model_provider="Provider",
            )

            self.assertEqual(result.task_count, 1)
            self.assertTrue((bundle / task_id / "reward.json").is_file())
            normalized = json.loads((bundle / "batch_summary.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(normalized["task_id"], task_id)
            self.assertEqual(validate_submission(bundle), [])

    def test_pack_reports_malformed_summary_rows_as_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            summary = root / "batch_summary.jsonl"
            # A row that is an array or a bare string must not crash on .get().
            summary.write_text(
                '["task_1", "completed"]\n"task_2"\n',
                encoding="utf-8",
            )
            bundle = root / "bundle"

            result = pack_submission(
                summary_path=summary,
                out_dir=bundle,
                agent_name="Agent",
                agent_org="Org",
                model_name="Model",
                model_provider="Provider",
            )

            self.assertEqual(result.task_count, 0)
            self.assertEqual(len(result.warnings), 2)
            self.assertTrue(all("malformed summary record" in item for item in result.warnings))

    def _make_bundle(self, root: Path, *, evaluator_returncode: int, partial_pass: object = 0.5) -> Path:
        task = root / "task_1"
        (task / "run_outputs").mkdir(parents=True)
        (task / "run_logs").mkdir()
        task_id = "task-1"
        (task / "task.yaml").write_text(f"task_id: {task_id}\n", encoding="utf-8")
        (task / "run_outputs" / "reward.json").write_text(
            json.dumps({"task_id": task_id, "complete_pass": 0, "partial_pass": partial_pass}),
            encoding="utf-8",
        )
        (task / "run_logs" / "agent_status.json").write_text("{}\n", encoding="utf-8")
        (task / "run_logs" / "agent_adapter.jsonl").write_text(
            '{"step": 1, "action": {"finish": true}}\n', encoding="utf-8"
        )
        (task / "run_logs" / "compose.log").write_text("complete\n", encoding="utf-8")
        summary = root / "batch_summary.jsonl"
        summary.write_text(
            json.dumps({
                "task_id": task_id,
                "task_dir": str(task),
                "status": "completed",
                "evaluator_returncode": evaluator_returncode,
            })
            + "\n",
            encoding="utf-8",
        )
        bundle = root / "bundle"
        pack_submission(
            summary_path=summary,
            out_dir=bundle,
            agent_name="Agent",
            agent_org="Org",
            model_name="Model",
            model_provider="Provider",
        )
        return bundle

    def test_nonpassing_evaluator_return_code_is_valid_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = self._make_bundle(Path(temp_dir), evaluator_returncode=1)

            self.assertEqual(validate_submission(bundle), [])

    def test_invalid_reward_is_rejected_even_with_nonpassing_return_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = self._make_bundle(Path(temp_dir), evaluator_returncode=1, partial_pass="invalid")

            errors = validate_submission(bundle)

            self.assertTrue(any("invalid partial_pass" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
