from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from dumatebench.scripts.run_harbor_llm_judge import _artifact_specs, _checklist_score, _fixed_rubric_args, _outputs, main


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

    def test_manifest_selects_artifacts_and_fixed_rubrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir)
            (task_dir / "evaluator" / "criteria").mkdir(parents=True)
            (task_dir / "run_outputs").mkdir()
            (task_dir / "run_outputs" / "figure.svg").write_text("<svg/>", encoding="utf-8")
            (task_dir / "evaluator" / "criteria" / "figure.json").write_text('{"criteria": []}', encoding="utf-8")
            (task_dir / "evaluator" / "llm_judge_artifacts.json").write_text(json.dumps({
                "reference_file": "evaluator/llm_judge_references.json",
                "artifacts": [{"output_file": "run_outputs/figure.svg", "criteria_file": "evaluator/criteria/figure.json"}],
            }), encoding="utf-8")
            reward = {"partial_pass": 1.0, "checks": []}
            (task_dir / "run_outputs" / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
            specs = _artifact_specs(task_dir, reward)
            self.assertEqual(len(specs), 1)
            self.assertEqual(_fixed_rubric_args(task_dir, specs[0])["criteria_file"], "evaluator/criteria/figure.json")
            with patch("sys.argv", ["run_harbor_llm_judge.py", "--task-dir", str(task_dir)]), patch(
                "dumatebench.scripts.run_harbor_llm_judge.run_llm_judge_score",
                return_value={"status": "ok", "judge_score": 0.75},
            ) as judge:
                self.assertEqual(main(), 0)
            passed = judge.call_args.args[1]
            self.assertEqual(passed["output_file"], "run_outputs/figure.svg")
            self.assertEqual(passed["criteria_file"], "evaluator/criteria/figure.json")
            self.assertEqual(passed["reference_file"], "evaluator/llm_judge_references.json")

    def test_missing_fixed_rubric_falls_back_to_dynamic_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            options = _fixed_rubric_args(Path(temp_dir), {
                "output_file": "run_outputs/figure.svg",
                "criteria_file": "evaluator/criteria/missing.json",
                "reference_file": "evaluator/references.json",
            })
            self.assertNotIn("criteria_file", options)
            self.assertEqual(options["reference_file"], "evaluator/references.json")

    def test_missing_artifact_rubric_uses_existing_task_rubric(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir)
            (task_dir / "evaluator").mkdir()
            (task_dir / "evaluator" / "llm_judge_criteria.json").write_text(
                '{"criteria": []}', encoding="utf-8"
            )
            options = _fixed_rubric_args(task_dir, {
                "output_file": "run_outputs/figure.svg",
                "criteria_file": "evaluator/criteria/missing.json",
                "judge_args": {"llm_judge_rubric_file": "evaluator/also_missing.json"},
            })
            self.assertEqual(options["criteria_file"], "evaluator/llm_judge_criteria.json")
            self.assertNotIn("llm_judge_rubric_file", options)

    def test_missing_rubric_generates_svg_rubric_and_judge_score(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir)
            (task_dir / "evaluator").mkdir()
            (task_dir / "run_outputs").mkdir()
            (task_dir / "instruction.md").write_text("Draw a red square.", encoding="utf-8")
            (task_dir / "run_outputs" / "figure.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="red"/></svg>',
                encoding="utf-8",
            )
            rubric = {"criteria": [
                {"id": key, "dimension": dimension, "description": key, "evidence_hint": "Visible image",
                 "weight": 1, "levels": {str(score): f"Level {score}" for score in range(5)}}
                for key, dimension in (
                    ("content", "instruction_content_fidelity"),
                    ("quality", "composition_visual_hierarchy"),
                    ("technical", "technical_completeness"),
                )
            ]}
            judgment = {"criteria_results": [
                {"criterion_id": key, "status": "pass", "score": 4, "evidence": "Visible"}
                for key in ("content", "quality", "technical")
            ], "gate": {"status": "ok", "reasons": []}, "summary": "ok"}
            (task_dir / "evaluator" / "llm_judge_artifacts.json").write_text(json.dumps({
                "artifacts": [{"output_file": "run_outputs/figure.svg",
                               "criteria_file": "evaluator/criteria/missing.json",
                               "judge_args": {"mock_image_rubric": rubric, "mock_image_judgment": judgment}}],
            }), encoding="utf-8")
            (task_dir / "run_outputs" / "reward.json").write_text(json.dumps({
                "partial_pass": 1.0, "checks": []
            }), encoding="utf-8")
            with patch("sys.argv", ["run_harbor_llm_judge.py", "--task-dir", str(task_dir)]):
                self.assertEqual(main(), 0)
            judge = json.loads((task_dir / "run_outputs" / "llm_judge_scores" / "1.json").read_text())
            self.assertEqual(judge["status"], "ok")
            self.assertEqual(judge["judge_kind"], "image")
            self.assertEqual(judge["judge_score"], 1.0)
            self.assertTrue((task_dir / "run_outputs" / "image_llm_judge" / "rubric.json").is_file())


if __name__ == "__main__":
    unittest.main()
