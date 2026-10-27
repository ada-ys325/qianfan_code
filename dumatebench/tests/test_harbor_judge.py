from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dumatebench.scripts.run_harbor_llm_judge import _checklist_score, _outputs


class HarborJudgeOutputDiscoveryTests(unittest.TestCase):
    def test_checklist_score_uses_equal_weight_per_check(self) -> None:
        reward = {
            "partial_pass": 0.9,
            "checks": [
                {"passed": True, "weight": 0.9},
                {"passed": False, "weight": 0.1},
            ],
        }

        self.assertEqual(_checklist_score(reward), 0.5)

    def test_discovers_unquoted_yaml_path_for_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir)
            (task_dir / "evaluator").mkdir()
            (task_dir / "run_outputs").mkdir()
            (task_dir / "run_outputs" / "answer.md").write_text("answer", encoding="utf-8")
            (task_dir / "evaluator" / "checks.yaml").write_text(
                "checks:\n  - id: answer_exists\n    type: file_exists\n"
                "    path: run_outputs/answer.md\n    weight: 1.0\n",
                encoding="utf-8",
            )

            self.assertEqual(_outputs(task_dir, {"checks": []}), ["run_outputs/answer.md"])

    def test_ignores_missing_and_non_output_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir)
            (task_dir / "evaluator").mkdir()
            (task_dir / "run_outputs").mkdir()
            (task_dir / "evaluator" / "checks.yaml").write_text(
                "checks:\n  - id: input_file\n    path: workspace_seed/input.md\n",
                encoding="utf-8",
            )

            self.assertEqual(_outputs(task_dir, {"checks": []}), [])


if __name__ == "__main__":
    unittest.main()
