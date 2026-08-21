import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

from dumatebench.evaluator import (
    evaluate_llm_judge_score,
    run_checklist_score,
    run_llm_judge_score,
)
from dumatebench.evaluator.llm_judge_pdf.runner import JudgeRunner as PdfJudgeRunner
from dumatebench.evaluator.llm_judge_pdf.schema import normalize_rubric, stable_hash
from dumatebench.evaluator.llm_judge_image.runner import ImageJudgeRunner
from dumatebench.evaluator.llm_judge_image.schema import normalize_rubric as normalize_image_rubric
from dumatebench.evaluator.llm_judge_mm.runner import JudgeRunner as MultimodalJudgeRunner
from dumatebench.evaluator.llm_judge_mm.schema import normalize_rubric as normalize_multimodal_rubric
from dumatebench.evaluator.llm_judge_mm.schema import stable_hash as stable_hash_multimodal

try:
    import openpyxl
except ImportError:
    openpyxl = None


class UnifiedLlmJudgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "run_outputs").mkdir()
        (self.root / "workspace_seed").mkdir()
        (self.root / "instruction.md").write_text("写一份包含项目结论的报告。", encoding="utf-8")
        (self.root / "workspace_seed" / "context.md").write_text("Project Alpha ground truth.", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_checklist_score_runs_weighted_evaluator_functions(self):
        (self.root / "run_outputs" / "answer.md").write_text("Project Alpha is complete.", encoding="utf-8")

        result = run_checklist_score(
            self.root,
            [
                {"id": "exists", "type": "file_exists", "path": "run_outputs/answer.md", "weight": 1},
                {
                    "id": "contains",
                    "function": "evaluate_contain",
                    "testbed_dir": "run_outputs",
                    "args": {"doc_type": "txt", "file": "answer.md", "keywords": ["Project Alpha"]},
                    "weight": 3,
                },
            ],
        )

        self.assertEqual(result["complete_pass"], 1)
        self.assertEqual(result["partial_pass"], 1.0)

    def test_unscoreable_checklist_stops_llm_judge(self):
        output = self.root / "run_outputs" / "answer.md"
        output.write_text("Project Alpha is complete.", encoding="utf-8")

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/answer.md",
                "checks": [
                    {
                        "id": "unsupported",
                        "type": "evaluate_contain",
                        "args": {
                            "file": "run_outputs/answer.md",
                            "doc_type": "unknown",
                            "keywords": ["Project Alpha"],
                        },
                    }
                ],
            },
        )

        self.assertEqual(report["status"], "evaluator_error")
        self.assertEqual(report["judge_kind"], "checklist_gate")
        self.assertIsNone(report["final_score"])
        self.assertFalse(report["pass"])

    def test_textual_unified_score_averages_checklist_and_judge_scores(self):
        (self.root / "run_outputs" / "answer.md").write_text("Project Alpha conclusion.", encoding="utf-8")
        rubric = {
            "criteria": [
                {
                    "id": "conclusion_quality",
                    "dimension": "requirement_completeness",
                    "description": "结论完整。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": True,
                    "levels": {"0": "无", "1": "弱", "2": "部分", "3": "基本", "4": "完整"},
                },
                {
                    "id": "language",
                    "dimension": "language_style",
                    "description": "语言清楚。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": True,
                    "levels": {"0": "无", "1": "弱", "2": "部分", "3": "基本", "4": "完整"},
                },
                {
                    "id": "structure",
                    "dimension": "structure_coherence",
                    "description": "结构合理。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": {"0": "无", "1": "弱", "2": "部分", "3": "基本", "4": "完整"},
                },
            ]
        }
        judgment = {
            "criteria": [
                {
                    "id": "conclusion_quality",
                    "status": "pass",
                    "score": 4,
                    "evidence": [{"artifact_path": "outputs/answer.md", "location": "line 1", "quote": "Project Alpha"}],
                    "rationale": "包含结论。",
                    "confidence": 0.9,
                },
                {
                    "id": "language",
                    "status": "partial",
                    "score": 2,
                    "evidence": [{"artifact_path": "outputs/answer.md", "location": "line 1", "quote": "conclusion"}],
                    "rationale": "表达较短。",
                    "confidence": 0.8,
                },
                {
                    "id": "structure",
                    "status": "partial",
                    "score": 3,
                    "evidence": [],
                    "rationale": "单段结构可接受。",
                    "confidence": 0.7,
                },
            ],
            "summary": "OK",
        }

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/answer.md",
                "checks": [
                    {"id": "exists", "type": "file_exists", "path": "run_outputs/answer.md", "weight": 1},
                    {"id": "missing", "type": "file_exists", "path": "run_outputs/missing.md", "weight": 1},
                ],
                "mock_rubric": rubric,
                "mock_judgment": judgment,
                "judge_output_file": "run_outputs/unified.json",
                "min_final_score": 0.6,
            },
        )

        self.assertEqual(report["checklist_score"], 0.5)
        self.assertEqual(report["judge_score"], 0.75)
        # scoring.final_score: 30% complete + 30% partial + 40% judge.
        self.assertEqual(report["final_score"], 0.45)
        self.assertLess(report["final_score"], report["min_final_score"])
        self.assertFalse(report["pass"])
        self.assertTrue((self.root / "run_outputs" / "unified.json").is_file())

    def test_textual_reference_selection_prefers_file_named_in_instruction(self):
        (self.root / "instruction.md").write_text("请依据 target.md 写报告。", encoding="utf-8")
        (self.root / "workspace_seed" / "target.md").write_text("Ground truth.", encoding="utf-8")
        (self.root / "workspace_seed" / "noise.md").write_text("Distractor.", encoding="utf-8")
        (self.root / "run_outputs" / "answer.md").write_text("Answer.", encoding="utf-8")

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/answer.md",
                "rule_result": {"complete_pass": 1, "partial_pass": 1.0, "checks": []},
                "mock_rubric": self._textual_rubric(),
                "mock_judgment": self._textual_judgment(),
                "judge_output_file": "run_outputs/selected_reference_unified.json",
            },
        )

        selection = report["judge_report"]["reference_selection"]
        self.assertEqual(selection["status"], "selected")
        selected = [item["path"] for item in selection["files"] if item["selected"]]
        self.assertEqual(selected, ["target.md"])

    def test_reference_selection_ignores_annotation_review(self):
        (self.root / "instruction.md").write_text("请写报告，不额外指定输入文件。", encoding="utf-8")
        (self.root / "annotation_review.json").write_text('{"input": "target.md"}', encoding="utf-8")
        (self.root / "workspace_seed" / "target.md").write_text("Should not be selected.", encoding="utf-8")
        (self.root / "run_outputs" / "answer.md").write_text("Answer.", encoding="utf-8")

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/answer.md",
                "rule_result": {"complete_pass": 1, "partial_pass": 1.0, "checks": []},
                "mock_rubric": self._textual_rubric(),
                "mock_judgment": self._textual_judgment(),
                "judge_output_file": "run_outputs/no_annotation_reference_unified.json",
            },
        )

        selection = report["judge_report"]["reference_selection"]
        self.assertEqual(selection["status"], "no_explicit_match")
        self.assertFalse(any(item["selected"] for item in selection["files"]))

    def test_reference_selection_keeps_instruction_named_cross_type_file(self):
        (self.root / "instruction.md").write_text("请依据 requirements.md 制作图表。", encoding="utf-8")
        (self.root / "workspace_seed" / "requirements.md").write_text("Use Project Beta as ground truth.", encoding="utf-8")
        self._write_minimal_png(self.root / "run_outputs" / "images" / "preview.png")

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/images/preview.png",
                "rule_result": {"complete_pass": 1, "partial_pass": 1.0, "checks": []},
                "mock_image_rubric": self._image_rubric(),
                "mock_image_judgment": {
                    "criteria_results": [
                        {"criterion_id": "content", "status": "pass", "score": 4, "evidence": "ok"},
                        {"criterion_id": "quality", "status": "pass", "score": 4, "evidence": "ok"},
                        {"criterion_id": "completeness", "status": "pass", "score": 4, "evidence": "ok"},
                    ],
                    "gate": {"status": "ok", "reasons": []},
                    "summary": "ok",
                },
                "judge_output_file": "run_outputs/image_cross_type_reference_unified.json",
                "image_judge_output_dir": "run_outputs/image_cross_type_judge",
                "media_max_bytes": 10000,
            },
        )

        selection = report["judge_report"]["reference_selection"]
        selected = [item["path"] for item in selection["files"] if item["selected"]]
        self.assertEqual(selected, ["requirements.md"])
        reference_summary = report["judge_report"]["reference_summary"]
        self.assertEqual(reference_summary["text_artifacts"][0]["path"], "requirements.md")

    def test_web_reference_is_merged_into_judge_references(self):
        (self.root / "instruction.md").write_text("请依据 gold.md 检查网络检索报告。", encoding="utf-8")
        (self.root / "web_reference").mkdir()
        (self.root / "web_reference" / "gold.md").write_text("Official search result ground truth.", encoding="utf-8")
        (self.root / "workspace_seed" / "noise.md").write_text("Workspace noise.", encoding="utf-8")
        (self.root / "run_outputs" / "answer.md").write_text("Answer.", encoding="utf-8")

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/answer.md",
                "rule_result": {"complete_pass": 1, "partial_pass": 1.0, "checks": []},
                "mock_rubric": self._textual_rubric(),
                "mock_judgment": self._textual_judgment(),
                "judge_output_file": "run_outputs/web_reference_unified.json",
            },
        )

        selection = report["judge_report"]["reference_selection"]
        selected = [item["path"] for item in selection["files"] if item["selected"]]
        self.assertEqual(selected, ["web_reference/gold.md"])
        reference_selection = report["judge_report"]["reference_selection"]
        self.assertEqual(reference_selection["status"], "selected")

    def test_ppt_unified_score_selects_ppt_judge(self):
        self._write_minimal_pptx(self.root / "workspace_seed" / "source.pptx")
        self._write_minimal_pptx(self.root / "run_outputs" / "deck.pptx")
        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/deck.pptx",
                "checks": [
                    {"id": "exists", "type": "file_exists", "path": "run_outputs/deck.pptx", "weight": 1}
                ],
                "render_slides": False,
                "mock_response": json.dumps({"score": 80, "critical_failures": [], "dimensions": []}),
                "min_final_score": 0.9,
            },
        )

        self.assertEqual(report["judge_kind"], "ppt")
        self.assertEqual(report["checklist_score"], 1.0)
        self.assertEqual(report["judge_score"], 0.8)
        self.assertEqual(report["final_score"], 0.92)
        reference_summary = report["judge_report"]["evidence"]["workspace_reference_summary"]
        self.assertEqual(reference_summary["status"], "ok")
        self.assertEqual(reference_summary["text_artifacts"][0]["path"], "context.md")
        reference_ppts = report["judge_report"]["evidence"]["reference_ppt_summaries"]
        self.assertEqual(reference_ppts[0]["reference_path"], "source.pptx")
        self.assertTrue(evaluate_llm_judge_score(self.root, {**report_args_for_ppt(), "min_final_score": 0.9}))

    @unittest.skipIf(openpyxl is None, "openpyxl is not installed")
    def test_excel_unified_score_selects_excel_judge(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Sales"
        sheet["A1"] = "Region"
        sheet["B1"] = "Sales"
        sheet["A2"] = "North"
        sheet["B2"] = 120
        workbook.save(self.root / "run_outputs" / "sales.xlsx")
        workbook.save(self.root / "workspace_seed" / "source_sales.xlsx")
        workbook.close()

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/sales.xlsx",
                "checks": [
                    {"id": "exists", "type": "file_exists", "path": "run_outputs/sales.xlsx", "weight": 1},
                    {"id": "missing", "type": "file_exists", "path": "run_outputs/missing.xlsx", "weight": 1},
                ],
                "mock_excel_result": {
                    "checklist_deduplication": {"covered_by_checklist": [], "excluded_from_rubric": []},
                    "task_rubrics": [],
                    "check_results": [],
                    "dimension_scores": {
                        "instruction_coverage": {
                            "applicable": True,
                            "score": 0.6,
                            "evidence_level": "medium",
                            "reason": "Mock result.",
                        }
                    },
                    "overall_score": 0.6,
                    "verdict": "borderline",
                    "failure_modes": [],
                    "recommendations": [],
                },
                "judge_output_file": "run_outputs/excel_unified.json",
                "excel_judge_output_dir": "run_outputs/excel_llm_judge",
                "min_final_score": 0.5,
            },
        )

        self.assertEqual(report["judge_kind"], "excel")
        self.assertEqual(report["checklist_score"], 0.5)
        self.assertEqual(report["judge_score"], 0.6)
        self.assertEqual(report["final_score"], 0.39)
        self.assertLess(report["final_score"], report["min_final_score"])
        self.assertFalse(report["pass"])
        self.assertTrue((self.root / "run_outputs" / "excel_unified.json").is_file())
        self.assertTrue((self.root / "run_outputs" / "excel_llm_judge" / "judge_result.json").is_file())
        judge_input = json.loads((self.root / "run_outputs" / "excel_llm_judge" / "judge_input.json").read_text(encoding="utf-8"))
        reference_summary = judge_input["user_prompt"]["workspace_reference_summary"]
        self.assertEqual(reference_summary["status"], "ok")
        # Reference material is namespaced so the judge cannot mistake it for a
        # candidate artifact.
        self.assertEqual(reference_summary["text_artifacts"][0]["path"], "references/context.md")
        reference_excel = judge_input["user_prompt"]["reference_excel_artifact_summary"]
        self.assertEqual(reference_excel["excel_file_count"], 1)
        self.assertEqual(reference_excel["workbooks"][0]["relative_path"], "references/source_sales.xlsx")

    @unittest.skipIf(
        importlib.util.find_spec("pypdf") is None or importlib.util.find_spec("fitz") is None,
        "PDF judge dependencies are not installed",
    )
    def test_pdf_unified_score_selects_pdf_judge(self):
        self._write_minimal_pdf(self.root / "run_outputs" / "report.pdf")
        rubric = self._pdf_rubric()
        judgment = {
            "criteria": [
                {"id": "content", "score": 3, "confidence": 1.0, "evidence": [], "rationale": "ok"},
                {"id": "layout", "score": 3, "confidence": 1.0, "evidence": [], "rationale": "ok"},
                {"id": "integrity", "score": 3, "confidence": 1.0, "evidence": [], "rationale": "ok"},
            ],
            "summary": "ok",
        }

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/report.pdf",
                "rule_result": {"complete_pass": 0, "partial_pass": 0.5, "checks": []},
                "mock_pdf_rubric": rubric,
                "mock_pdf_judgment": judgment,
                "judge_output_file": "run_outputs/pdf_unified.json",
                "pdf_judge_output_dir": "run_outputs/pdf_llm_judge",
            },
        )

        self.assertEqual(report["judge_kind"], "pdf")
        self.assertEqual(report["checklist_score"], 0.5)
        self.assertEqual(report["judge_score"], 0.75)
        self.assertEqual(report["final_score"], 0.45)
        self.assertTrue((self.root / "run_outputs" / "pdf_unified.json").is_file())
        self.assertTrue((self.root / "run_outputs" / "pdf_llm_judge" / "judge_result.json").is_file())

    def test_pdf_rubric_normalizes_human_readable_dimension_aliases(self):
        rubric = self._pdf_rubric()
        rubric["criteria"][0]["dimension"] = "Instruction Following"

        normalized = normalize_rubric(rubric, task_id="task", instruction_hash=stable_hash("instruction"))

        self.assertEqual(normalized["criteria"][0]["dimension"], "instruction_following")

    def test_pdf_rubric_generation_requests_strict_json_schema(self):
        class RecordingClient:
            def __init__(self, payload):
                self.payload = payload
                self.response_format = None

            def complete_json(self, messages, response_format=None):
                del messages
                self.response_format = response_format
                return self.payload

        client = RecordingClient(self._pdf_rubric())
        runner = PdfJudgeRunner(client)

        runner.generate_rubric(task_id="task", instruction="instruction")

        self.assertEqual(client.response_format["type"], "json_schema")
        schema = client.response_format["json_schema"]["schema"]
        dimension_schema = schema["properties"]["criteria"]["items"]["properties"]["dimension"]
        self.assertIn("instruction_following", dimension_schema["enum"])
        self.assertTrue(client.response_format["json_schema"]["strict"])

    def test_pdf_judgment_requests_strict_json_schema(self):
        class RecordingClient:
            def __init__(self, payload):
                self.payload = payload
                self.response_formats = []

            def complete_json(self, messages, response_format=None):
                del messages
                self.response_formats.append(response_format)
                return self.payload

        self._write_minimal_pdf(self.root / "run_outputs" / "report.pdf")
        rubric = normalize_rubric(self._pdf_rubric(), task_id="task", instruction_hash=stable_hash("instruction"))
        client = RecordingClient(
            {
                "criteria": [
                    {"id": "content", "score": 3, "confidence": 0.8, "evidence": [{"path": "report.pdf", "page": 1, "quote": "", "visual_observation": "ok"}], "rationale": "ok"},
                    {"id": "layout", "score": 3, "confidence": 0.8, "evidence": [{"path": "report.pdf", "page": 1, "quote": "", "visual_observation": "ok"}], "rationale": "ok"},
                    {"id": "integrity", "score": 3, "confidence": 0.8, "evidence": [{"path": "report.pdf", "page": 1, "quote": "", "visual_observation": "ok"}], "rationale": "ok"},
                ],
                "summary": "ok",
            },
        )
        runner = PdfJudgeRunner(client)

        runner.evaluate(
            instruction="instruction",
            rubric=rubric,
            outputs_dir=self.root / "run_outputs",
            max_pages=1,
            rule_weight=0.0,
        )

        self.assertEqual(client.response_formats[0]["type"], "json_schema")
        schema = client.response_formats[0]["json_schema"]["schema"]
        self.assertTrue(client.response_formats[0]["json_schema"]["strict"])
        self.assertEqual(schema["properties"]["criteria"]["minItems"], 3)
        item_schema = schema["properties"]["criteria"]["items"]
        self.assertEqual(item_schema["properties"]["id"]["enum"], ["content", "layout", "integrity"])
        evidence_path = item_schema["properties"]["evidence"]["items"]["properties"]["path"]
        # Candidate artifacts are namespaced with an ``outputs/`` prefix so the
        # judge cannot confuse them with reference material.
        self.assertIn("outputs/report.pdf", evidence_path["enum"])

    def test_pdf_judgment_keeps_low_confidence_score_when_evidence_is_filtered(self):
        class JudgmentClient:
            def complete_json(self, messages, response_format=None):
                del messages
                del response_format
                return {
                    "criteria": [
                        {"id": "content", "score": 3, "confidence": 0.9, "evidence": [], "rationale": "内容基本准确。"},
                        {"id": "layout", "score": 4, "confidence": 0.9, "evidence": [], "rationale": "布局清晰。"},
                        {"id": "integrity", "score": 4, "confidence": 0.9, "evidence": [], "rationale": "文件完整。"},
                    ],
                    "summary": "ok",
                }

        self._write_minimal_pdf(self.root / "run_outputs" / "report.pdf")
        rubric_raw = self._pdf_rubric()
        for item in rubric_raw["criteria"]:
            item["evidence_required"] = True
        rubric = normalize_rubric(rubric_raw, task_id="task", instruction_hash=stable_hash("instruction"))
        result = PdfJudgeRunner(JudgmentClient()).evaluate(
            instruction="instruction",
            rubric=rubric,
            outputs_dir=self.root / "run_outputs",
            max_pages=1,
            rule_weight=0.0,
            judge_runs=1,
            model="mock",
        )

        aggregate = result["aggregate"]
        self.assertEqual(aggregate["assessment_coverage"], 1.0)
        self.assertEqual(aggregate["judge_score_conservative"], 91.67)
        # pdf_LLM_AS_JUDGE.md: missing evidence is reported by the judge but the
        # program never zeroes the criterion on its own.
        self.assertEqual(aggregate["criteria"][0]["status"], "assessed")
        self.assertEqual(aggregate["criteria"][0]["score"], 3)
        self.assertEqual(aggregate["criteria"][0]["evidence"], [])

    def test_pdf_judgment_accepts_prefixed_evidence_paths(self):
        absolute_report = str(self.root / "run_outputs" / "report.pdf")

        class JudgmentClient:
            def complete_json(self, messages, response_format=None):
                del messages
                del response_format
                return {
                    "criteria": [
                        {"id": "content", "score": 3, "confidence": 0.8, "evidence": [{"path": "outputs/report.pdf", "page": 1, "quote": "", "visual_observation": "ok"}], "rationale": "ok"},
                        {"id": "layout", "score": 3, "confidence": 0.8, "evidence": [{"path": "candidate/report.pdf", "page": 1, "quote": "", "visual_observation": "ok"}], "rationale": "ok"},
                        {"id": "integrity", "score": 3, "confidence": 0.8, "evidence": [{"path": absolute_report, "page": 1, "quote": "", "visual_observation": "ok"}], "rationale": "ok"},
                    ],
                    "summary": "ok",
                }

        self._write_minimal_pdf(self.root / "run_outputs" / "report.pdf")
        rubric = normalize_rubric(self._pdf_rubric(), task_id="task", instruction_hash=stable_hash("instruction"))
        result = PdfJudgeRunner(JudgmentClient()).evaluate(
            instruction="instruction",
            rubric=rubric,
            outputs_dir=self.root / "run_outputs",
            max_pages=1,
            rule_weight=0.0,
            judge_runs=1,
            model="mock",
        )

        criteria = result["aggregate"]["criteria"]
        self.assertTrue(all(item["status"] == "assessed" for item in criteria))
        # Only the ``outputs/`` namespace identifies a candidate artifact; it is
        # stripped back to the artifact-relative path. Any other prefix, including
        # an absolute path, is not a known artifact and is dropped.
        by_id = {item["id"]: item for item in criteria}
        self.assertEqual(by_id["content"]["evidence"][0]["path"], "report.pdf")
        self.assertEqual(by_id["layout"]["evidence"], [])
        self.assertEqual(by_id["integrity"]["evidence"], [])

    def test_image_unified_score_selects_image_judge(self):
        self._write_minimal_png(self.root / "run_outputs" / "images" / "preview.png")
        rubric = self._image_rubric()
        judgment = {
            "criteria_results": [
                {"criterion_id": "content", "status": "pass", "score": 4, "evidence": "good"},
                {"criterion_id": "quality", "status": "pass", "score": 2, "evidence": "partial"},
                {"criterion_id": "completeness", "status": "pass", "score": 3, "evidence": "ok"},
            ],
            "gate": {"status": "ok", "reasons": []},
            "summary": "ok",
        }

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/images/preview.png",
                "checks": [{"id": "exists", "type": "file_exists", "path": "run_outputs/images/preview.png", "weight": 1}],
                "mock_image_rubric": rubric,
                "mock_image_judgment": judgment,
                "judge_output_file": "run_outputs/image_unified.json",
                "image_judge_output_dir": "run_outputs/image_llm_judge",
                "media_max_bytes": 10000,
            },
        )

        self.assertEqual(report["judge_kind"], "image")
        self.assertEqual(report["checklist_score"], 1.0)
        self.assertEqual(report["judge_score"], 0.75)
        self.assertEqual(report["final_score"], 0.9)
        self.assertTrue((self.root / "run_outputs" / "image_llm_judge" / "judge_result.json").is_file())

    def test_image_judgment_requests_strict_json_schema(self):
        class RecordingClient:
            def __init__(self, rubric):
                self.rubric = rubric
                self.response_formats = []

            def complete_json(self, messages, attachments=None, response_format=None):
                del messages
                del attachments
                self.response_formats.append(response_format)
                if len(self.response_formats) == 1:
                    return self.rubric
                return {
                    "criteria_results": [
                        {"criterion_id": "content", "status": "pass", "score": 4, "evidence": "ok"},
                        {"criterion_id": "quality", "status": "pass", "score": 4, "evidence": "ok"},
                        {"criterion_id": "completeness", "status": "pass", "score": 4, "evidence": "ok"},
                    ],
                    "gate": {"status": "ok", "reasons": []},
                    "summary": "ok",
                }

        self._write_minimal_png(self.root / "run_outputs" / "images" / "preview.png")
        client = RecordingClient(self._image_rubric())

        ImageJudgeRunner(client, max_bytes=10000).run(
            self.root,
            self.root / "run_outputs" / "images",
            None,
            self.root / "run_outputs" / "image_schema_judge",
        )

        self.assertEqual(client.response_formats[0]["type"], "json_schema")
        self.assertTrue(client.response_formats[0]["json_schema"]["strict"])
        rubric_schema = client.response_formats[0]["json_schema"]["schema"]
        rubric_dimension_schema = rubric_schema["properties"]["criteria"]["items"]["properties"]["dimension"]
        self.assertIn("instruction_content_fidelity", rubric_dimension_schema["enum"])
        self.assertEqual(client.response_formats[1]["type"], "json_schema")
        schema = client.response_formats[1]["json_schema"]["schema"]
        self.assertTrue(client.response_formats[1]["json_schema"]["strict"])
        self.assertIn("criteria_results", schema["required"])
        criteria_results_schema = schema["properties"]["criteria_results"]
        self.assertEqual(criteria_results_schema["minItems"], 3)
        self.assertEqual(criteria_results_schema["maxItems"], 3)
        item_schema = criteria_results_schema["items"]
        self.assertEqual(
            item_schema["required"],
            ["id", "status", "score", "evidence", "rationale", "confidence"],
        )
        self.assertEqual(item_schema["properties"]["id"]["enum"], ["content", "quality", "completeness"])

    def test_image_rubric_accepts_chinese_dimension_aliases(self):
        rubric = self._image_rubric()
        rubric["criteria"][0]["dimension"] = "指令与内容忠实度"

        normalized = normalize_image_rubric(rubric)

        self.assertEqual(normalized["criteria"][0]["dimension"], "instruction_content_fidelity")

    def test_image_rubric_requests_strict_json_schema(self):
        class RecordingClient:
            def __init__(self, rubric):
                self.rubric = rubric
                self.response_format = None

            def complete_json(self, messages, attachments=None, response_format=None):
                del messages
                del attachments
                self.response_format = response_format
                return self.rubric

        client = RecordingClient(self._image_rubric())

        ImageJudgeRunner(client, max_bytes=10000).generate_rubric(self.root, None)

        self.assertEqual(client.response_format["type"], "json_schema")
        self.assertTrue(client.response_format["json_schema"]["strict"])
        dimension_schema = client.response_format["json_schema"]["schema"]["properties"]["criteria"]["items"]["properties"]["dimension"]
        self.assertIn("instruction_content_fidelity", dimension_schema["enum"])

    def test_image_judgment_converts_unjustified_cannot_assess_to_fail(self):
        class CannotAssessClient:
            def __init__(self, rubric):
                self.rubric = rubric
                self.calls = 0

            def complete_json(self, messages, attachments=None, response_format=None):
                del messages
                del attachments
                del response_format
                self.calls += 1
                if self.calls == 1:
                    return self.rubric
                return {
                    "criteria_results": [
                        {"criterion_id": "content", "status": "cannot_assess", "score": None, "evidence": "无法确认。"},
                        {"criterion_id": "quality", "status": "pass", "score": 4, "evidence": "清晰。"},
                        {"criterion_id": "completeness", "status": "pass", "score": 4, "evidence": "完整。"},
                    ],
                    "gate": {"status": "ok", "reasons": []},
                    "summary": "ok",
                }

        self._write_minimal_png(self.root / "run_outputs" / "images" / "preview.png")
        client = CannotAssessClient(self._image_rubric())

        result = ImageJudgeRunner(client, max_bytes=10000).run(
            self.root,
            self.root / "run_outputs" / "images",
            None,
            self.root / "run_outputs" / "image_cannot_assess_judge",
        )

        first = result["criteria_results"][0]
        self.assertEqual(first["status"], "fail")
        self.assertEqual(first["score"], 0)
        self.assertIn("所需图片附件可用", first["evidence"])
        self.assertEqual(result["weighted_score"], 2.6667)

    def test_multimodal_unified_score_selects_audio_video_judge(self):
        (self.root / "run_outputs" / "media").mkdir()
        (self.root / "run_outputs" / "media" / "voice.mp3").write_bytes(b"audio")
        (self.root / "run_outputs" / "media" / "clip.mp4").write_bytes(b"video")
        rubric = self._multimodal_rubric("multimodal")
        judgment = {
            "criteria": [
                {"id": "content", "status": "assessed", "score": 3, "evidence": [], "confidence": 1.0},
                {"id": "quality", "status": "assessed", "score": 3, "evidence": [], "confidence": 1.0},
                {"id": "completeness", "status": "assessed", "score": 3, "evidence": [], "confidence": 1.0},
            ],
            "summary": "ok",
        }

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/media/voice.mp3",
                "checks": [{"id": "exists", "type": "file_exists", "path": "run_outputs/media/voice.mp3", "weight": 1}],
                "mock_multimodal_rubric": rubric,
                "mock_multimodal_judgment": judgment,
                "judge_output_file": "run_outputs/media_unified.json",
                "media_max_bytes": 10000,
                "video_mode": "video_url",
            },
        )

        self.assertEqual(report["judge_kind"], "multimodal")
        self.assertEqual(report["judge_score"], 0.75)

    def test_multimodal_judgment_accepts_criteria_results_alias(self):
        (self.root / "run_outputs" / "media").mkdir()
        (self.root / "run_outputs" / "media" / "clip.mp4").write_bytes(b"video")
        rubric = self._multimodal_rubric("video")
        judgment = {
            "criteria_results": [
                {"criterion_id": "content", "status": "pass", "score": 3, "evidence": [], "confidence": 1.0},
                {"criterion_id": "quality", "status": "pass", "score": 3, "evidence": [], "confidence": 1.0},
                {"criterion_id": "completeness", "status": "pass", "score": 3, "evidence": [], "confidence": 1.0},
            ],
            "summary": "ok",
        }

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/media/clip.mp4",
                "rule_result": {"complete_pass": 1, "partial_pass": 1.0, "checks": []},
                "mock_multimodal_rubric": rubric,
                "mock_multimodal_judgment": judgment,
                "judge_output_file": "run_outputs/media_alias_unified.json",
                "media_max_bytes": 10000,
                "video_mode": "video_url",
            },
        )

        self.assertEqual(report["judge_kind"], "multimodal")
        self.assertEqual(report["judge_score"], 0.75)

    def test_multimodal_judgment_requests_strict_json_schema(self):
        class RecordingClient:
            def __init__(self):
                self.response_formats = []

            def complete_json(self, messages, response_format=None):
                del messages
                self.response_formats.append(response_format)
                return {
                    "criteria": [
                        {"id": "content", "status": "assessed", "score": 3, "evidence": [], "rationale": "ok", "confidence": 1.0},
                        {"id": "quality", "status": "assessed", "score": 3, "evidence": [], "rationale": "ok", "confidence": 1.0},
                        {"id": "completeness", "status": "assessed", "score": 3, "evidence": [], "rationale": "ok", "confidence": 1.0},
                    ],
                    "summary": "ok",
                }

        instruction = "instruction"
        rubric = normalize_multimodal_rubric(
            self._multimodal_rubric("text"),
            task_id="task",
            instruction_hash=stable_hash_multimodal(instruction),
        )
        client = RecordingClient()

        MultimodalJudgeRunner(client).evaluate(
            instruction=instruction,
            rubric=rubric,
            artifacts=[
                {
                    "path": "notes.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 10,
                    "category": "text",
                    "truncated": False,
                    "structure": {},
                    "content": "ok",
                    "transport": None,
                }
            ],
        )

        response_format = client.response_formats[0]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["criteria"]["minItems"], 3)
        item_schema = schema["properties"]["criteria"]["items"]
        self.assertEqual(item_schema["properties"]["id"]["enum"], ["content", "quality", "completeness"])

    def test_multimodal_rubric_drops_legacy_evidence_type(self):
        rubric = self._multimodal_rubric("video")
        rubric["criteria"][1]["evidence_type"] = "media_metadata"

        normalized = normalize_multimodal_rubric(
            rubric,
            task_id="task",
            instruction_hash=stable_hash_multimodal("instruction"),
        )

        # MULTIMODAL_LLM_AS_JUDGE_REPORT.md: a legacy rubric's evidence_type is
        # ignored; assessability comes from the visible candidates, the reference
        # evidence pool and media transport state.
        self.assertNotIn("evidence_type", normalized["criteria"][1])

    def test_multimodal_rubric_ignores_legacy_golden_review_source(self):
        rubric = self._multimodal_rubric("video")
        rubric["criteria"][0]["needs_human_golden_review"] = True
        rubric["criteria"][0]["golden_source"] = None
        rubric["criteria"][0]["evidence_type"] = "human_review"

        normalized = normalize_multimodal_rubric(
            rubric,
            task_id="task",
            instruction_hash=stable_hash_multimodal("instruction"),
        )

        self.assertNotIn("needs_human_golden_review", normalized["criteria"][0])
        self.assertNotIn("golden_source", normalized["criteria"][0])
        self.assertNotIn("evidence_type", normalized["criteria"][0])

    def test_multimodal_rubric_requests_strict_json_schema(self):
        class RecordingClient:
            def __init__(self, rubric):
                self.rubric = rubric
                self.response_format = None

            def complete_json(self, messages, response_format=None):
                del messages
                self.response_format = response_format
                return self.rubric

        client = RecordingClient(self._multimodal_rubric("video"))

        MultimodalJudgeRunner(client).generate_rubric(task_id="task", instruction="instruction")

        response_format = client.response_format
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        item_schema = schema["properties"]["criteria"]["items"]
        self.assertNotIn("needs_human_golden_review", item_schema["properties"])
        self.assertNotIn("golden_source", item_schema["properties"])
        self.assertNotIn("evidence_type", item_schema["properties"])
        self.assertIn("covered_check_ids", item_schema["properties"])

    def test_multimodal_rubric_keeps_semantic_criteria_with_keyword_checks(self):
        class StaticClient:
            def complete_json(self, messages, response_format=None):
                del messages
                del response_format
                return self_payload

        checks_path = self.root / "evaluator" / "checks.yaml"
        checks_path.parent.mkdir(parents=True)
        checks_path.write_text(
            "checks:\n"
            "  - id: contains_horse\n"
            "    type: evaluate_contain\n"
            "    description: 输出内容包含马相关关键词\n"
            "    args:\n"
            "      file: run_outputs/media/ten-thousand-horses.mp4\n"
            "      keywords: [马, 草原]\n",
            encoding="utf-8",
        )
        self_payload = self._multimodal_rubric("video")
        self_payload["criteria"][0]["id"] = "content_relevance_horses"
        self_payload["criteria"][0]["description"] = "The video content accurately depicts horses galloping on a vast grassland."

        rubric = MultimodalJudgeRunner(StaticClient()).generate_rubric(
            task_id="task",
            instruction="生成万马奔腾草原视频",
            checks_path=checks_path,
        )

        self.assertGreaterEqual(len(rubric["criteria"]), 3)
        self.assertIn("content_relevance_horses", {item["id"] for item in rubric["criteria"]})

    def test_multimodal_video_frame_failure_marks_media_unavailable(self):
        (self.root / "run_outputs" / "media").mkdir()
        (self.root / "run_outputs" / "media" / "clip.mp4").write_bytes(b"not a real video")
        rubric = self._multimodal_rubric("video")
        judgment = {
            "criteria": [
                {"id": "content", "status": "assessed", "score": 4, "evidence": [], "confidence": 1.0},
                {"id": "quality", "status": "assessed", "score": 4, "evidence": [], "confidence": 1.0},
                {"id": "completeness", "status": "assessed", "score": 4, "evidence": [], "confidence": 1.0},
            ],
            "summary": "ok",
        }

        report = run_llm_judge_score(
            self.root,
            {
                "output_file": "run_outputs/media/clip.mp4",
                "rule_result": {"complete_pass": 1, "partial_pass": 1.0, "checks": []},
                "mock_multimodal_rubric": rubric,
                "mock_multimodal_judgment": judgment,
                "judge_output_file": "run_outputs/media_frame_failure_unified.json",
                "media_max_bytes": 10000,
                "video_frame_count": 1,
            },
        )

        self.assertEqual(report["judge_kind"], "multimodal")
        self.assertEqual(report["judge_report"]["judge_type"], "multimodal_llm_judge")
        result = report["judge_report"]["result"]
        self.assertTrue(result["needs_human_review"])
        self.assertEqual(result["judge_score_conservative"], 0.0)
        self.assertTrue(all(item["status"] == "cannot_assess" for item in result["criteria"]))
        attachment = result["attachments"][0]
        self.assertEqual(attachment["transport_status"], "cannot_assess")
        self.assertIn("video frame extraction", attachment["cannot_assess_reason"])

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

    @staticmethod
    def _write_minimal_pdf(path: Path) -> None:
        from pypdf import PdfWriter

        path.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with path.open("wb") as handle:
            writer.write(handle)

    @staticmethod
    def _write_minimal_png(path: Path) -> None:
        from PIL import Image

        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1, 1), color=(255, 255, 255)).save(path)

    @staticmethod
    def _pdf_rubric() -> dict:
        levels = {"0": "bad", "1": "weak", "2": "partial", "3": "good", "4": "excellent"}
        return {
            "criteria": [
                {
                    "id": "content",
                    "dimension": "requirement_completeness",
                    "description": "内容完整。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                },
                {
                    "id": "layout",
                    "dimension": "layout_readability",
                    "description": "版面可读。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                },
                {
                    "id": "integrity",
                    "dimension": "artifact_integrity",
                    "description": "文件完整。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                },
            ]
        }

    @staticmethod
    def _multimodal_rubric(modality: str) -> dict:
        levels = {"0": "bad", "1": "weak", "2": "partial", "3": "good", "4": "excellent"}
        return {
            "criteria": [
                {
                    "id": "content",
                    "dimension": "content_relevance",
                    "description": "内容相关。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                    "modality": modality,
                    "needs_human_golden_review": False,
                    "golden_source": None,
                    "evidence_type": "media_timestamp" if modality != "text" else "artifact_quote",
                },
                {
                    "id": "quality",
                    "dimension": "technical_quality",
                    "description": "技术质量合格。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                    "modality": modality,
                    "needs_human_golden_review": False,
                    "golden_source": None,
                    "evidence_type": "media_timestamp",
                },
                {
                    "id": "completeness",
                    "dimension": "requirement_completeness",
                    "description": "要求完整。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                    "modality": modality,
                    "needs_human_golden_review": False,
                    "golden_source": None,
                    "evidence_type": "media_timestamp",
                },
            ]
        }

    @staticmethod
    def _image_rubric() -> dict:
        levels = {"0": "bad", "1": "weak", "2": "partial", "3": "good", "4": "excellent"}
        return {
            "criteria": [
                {
                    "id": "content",
                    "dimension": "instruction_content_fidelity",
                    "description": "内容符合指令。",
                    "evidence_hint": "引用图片中可见元素。",
                    "weight": 1,
                    "levels": levels,
                },
                {
                    "id": "quality",
                    "dimension": "composition_visual_hierarchy",
                    "description": "构图和层级清楚。",
                    "evidence_hint": "观察构图、主体和背景。",
                    "weight": 1,
                    "levels": levels,
                },
                {
                    "id": "completeness",
                    "dimension": "technical_completeness",
                    "description": "图片技术完整。",
                    "evidence_hint": "检查是否损坏或缺失。",
                    "weight": 1,
                    "levels": levels,
                },
            ]
        }

    @staticmethod
    def _textual_rubric() -> dict:
        levels = {"0": "bad", "1": "weak", "2": "partial", "3": "good", "4": "excellent"}
        return {
            "criteria": [
                {
                    "id": "content",
                    "dimension": "requirement_completeness",
                    "description": "内容完整。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                },
                {
                    "id": "clarity",
                    "dimension": "language_style",
                    "description": "表达清晰。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                },
                {
                    "id": "structure",
                    "dimension": "structure_coherence",
                    "description": "结构合理。",
                    "weight": 1,
                    "critical": False,
                    "evidence_required": False,
                    "levels": levels,
                },
            ]
        }

    @staticmethod
    def _textual_judgment() -> dict:
        return {
            "criteria": [
                {
                    "id": "content",
                    "status": "pass",
                    "score": 4,
                    "evidence": [],
                    "rationale": "ok",
                    "confidence": 1.0,
                },
                {
                    "id": "clarity",
                    "status": "pass",
                    "score": 4,
                    "evidence": [],
                    "rationale": "ok",
                    "confidence": 1.0,
                },
                {
                    "id": "structure",
                    "status": "pass",
                    "score": 4,
                    "evidence": [],
                    "rationale": "ok",
                    "confidence": 1.0,
                },
            ],
            "summary": "ok",
        }


def report_args_for_ppt():
    return {
        "output_file": "run_outputs/deck.pptx",
        "checks": [{"id": "exists", "type": "file_exists", "path": "run_outputs/deck.pptx", "weight": 1}],
        "render_slides": False,
        "mock_response": json.dumps({"score": 80, "critical_failures": [], "dimensions": []}),
    }


if __name__ == "__main__":
    unittest.main()
