import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dumatebench.evaluator import evaluate_pptx_llm_judge
from dumatebench.evaluator.llm_judge.ppt import (
    aggregate_dimension_score,
    build_ppt_judge_messages,
    parse_judge_response,
    prepare_evidence,
    run_pptx_judge,
)


class PptLlmJudgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "run_outputs" / "pptx").mkdir(parents=True)
        (self.root / "workspace_seed" / "uploads").mkdir(parents=True)
        (self.root / "instruction.md").write_text("优化这个 PPT，保留核心内容和页数。", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_prompt_contains_instruction_and_schema(self):
        messages = build_ppt_judge_messages(
            {
                "instruction": "请优化 PPT。",
                "input_summary": {"slide_count": 1, "slides": []},
                "output_summary": {"slide_count": 1, "slides": []},
                "render_status": {
                    "input": {"status": "skipped", "reason": "test", "images": []},
                    "output": {"status": "skipped", "reason": "test", "images": []},
                },
            }
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        prompt_text = messages[1]["content"][0]["text"]
        self.assertIn("请优化 PPT", prompt_text)
        self.assertIn("expected_json_schema", prompt_text)
        self.assertIn("layout_and_readability", prompt_text)

    def test_parse_judge_response_and_aggregate_score(self):
        response = json.dumps(
            {
                "dimensions": [
                    {"name": "instruction_following", "weight": 0.6, "score": 4},
                    {"name": "visual_design", "weight": 0.4, "score": 3},
                ],
                "critical_failures": [],
                "summary": "Mostly good.",
            }
        )

        parsed = parse_judge_response(response, min_score=70)

        self.assertEqual(aggregate_dimension_score(parsed["dimensions"]), 72.0)
        self.assertEqual(parsed["score"], 72.0)
        self.assertTrue(parsed["pass"])

    def test_parse_judge_response_rejects_malformed_json(self):
        with self.assertRaises(ValueError):
            parse_judge_response("not json")

    def test_evaluator_returns_false_without_calling_llm_when_output_missing(self):
        result = evaluate_pptx_llm_judge(
            self.root,
            {
                "instruction_file": "instruction.md",
                "input_file": "workspace_seed/uploads/source.pptx",
                "output_file": "run_outputs/pptx/missing.pptx",
                "mock_response": json.dumps({"score": 100, "critical_failures": []}),
            },
        )

        self.assertFalse(result)
        report = json.loads((self.root / "run_outputs" / "ppt_llm_judge.json").read_text())
        self.assertEqual(report["result"]["score"], 0.0)
        self.assertIn("missing or unreadable", report["result"]["critical_failures"][0])

    def test_cli_core_writes_report_with_mock_response_for_valid_pptx(self):
        self._write_minimal_pptx(self.root / "run_outputs" / "pptx" / "out.pptx")
        mock = json.dumps({"score": 88, "critical_failures": [], "summary": "Good", "dimensions": []})

        report = run_pptx_judge(
            self.root,
            output_file="run_outputs/pptx/out.pptx",
            render_slides=False,
            mock_response=mock,
        )

        self.assertTrue(report["result"]["pass"])
        self.assertTrue((self.root / "run_outputs" / "ppt_llm_judge.json").is_file())

    def test_prepare_evidence_uses_reference_ppt_as_input_when_input_file_missing(self):
        self._write_minimal_pptx(self.root / "workspace_seed" / "uploads" / "source.pptx")
        self._write_minimal_pptx(self.root / "run_outputs" / "pptx" / "out.pptx")

        evidence = prepare_evidence(
            self.root,
            instruction_file="instruction.md",
            output_file="run_outputs/pptx/out.pptx",
            reference_dir="workspace_seed",
            render_slides=False,
        )

        self.assertTrue(evidence["input_file"].endswith("workspace_seed/uploads/source.pptx"))
        self.assertTrue(evidence["input_valid"])
        self.assertEqual(evidence["reference_ppt_summaries"][0]["reference_path"], "uploads/source.pptx")

    def test_prepare_evidence_prefers_reference_ppt_named_in_instruction(self):
        (self.root / "instruction.md").write_text("请优化 `target.pptx`，不要处理 demo.pptx。", encoding="utf-8")
        self._write_minimal_pptx(self.root / "workspace_seed" / "uploads" / "aaa_demo.pptx")
        self._write_minimal_pptx(self.root / "workspace_seed" / "uploads" / "target.pptx")
        self._write_minimal_pptx(self.root / "run_outputs" / "pptx" / "target_优化.pptx")

        evidence = prepare_evidence(
            self.root,
            instruction_file="instruction.md",
            output_file="run_outputs/pptx/target_优化.pptx",
            reference_dir="workspace_seed",
            render_slides=False,
        )

        self.assertTrue(evidence["input_file"].endswith("workspace_seed/uploads/target.pptx"))
        self.assertEqual(evidence["selected_reference_input"]["path"], "uploads/target.pptx")
        self.assertIn("file name appears in instruction", evidence["selected_reference_input"]["reasons"])

    def test_cli_core_writes_combined_reward_with_normalized_average(self):
        self._write_minimal_pptx(self.root / "run_outputs" / "pptx" / "out.pptx")
        (self.root / "run_outputs" / "reward.json").write_text(
            json.dumps({"complete_pass": 0, "partial_pass": 0.8, "checks": []}),
            encoding="utf-8",
        )
        mock = json.dumps({"score": 75, "critical_failures": [], "summary": "Good", "dimensions": []})

        run_pptx_judge(
            self.root,
            output_file="run_outputs/pptx/out.pptx",
            render_slides=False,
            mock_response=mock,
        )

        combined = json.loads((self.root / "run_outputs" / "reward_with_ppt_judge.json").read_text())
        self.assertEqual(combined["base_partial_pass"], 0.8)
        self.assertEqual(combined["ppt_llm_judge_score"], 0.75)
        self.assertEqual(combined["final_score"], 0.775)
        self.assertEqual(combined["partial_pass_with_ppt_judge"], 0.775)

    def test_cli_core_writes_failure_report_when_judge_call_fails(self):
        self._write_minimal_pptx(self.root / "run_outputs" / "pptx" / "out.pptx")
        (self.root / "run_outputs" / "reward.json").write_text(
            json.dumps({"complete_pass": 1, "partial_pass": 1.0, "checks": []}),
            encoding="utf-8",
        )

        with mock.patch(
            "dumatebench.evaluator.llm_judge.ppt.call_openai_judge",
            side_effect=RuntimeError("openai>=1.x is required to call the LLM judge"),
        ):
            report = run_pptx_judge(self.root, output_file="run_outputs/pptx/out.pptx", render_slides=False)

        self.assertFalse(report["result"]["pass"])
        self.assertIn("PPT LLM judge failed", report["result"]["critical_failures"][0])
        combined = json.loads((self.root / "run_outputs" / "reward_with_ppt_judge.json").read_text())
        self.assertEqual(combined["ppt_llm_judge_score"], 0.0)
        self.assertEqual(combined["final_score"], 0.5)

    @staticmethod
    def _write_minimal_pptx(path: Path) -> None:
        import zipfile

        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
</Types>""",
            )
            archive.writestr(
                "ppt/presentation.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
</p:presentation>""",
            )


if __name__ == "__main__":
    unittest.main()
