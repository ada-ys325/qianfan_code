import importlib.util
import tempfile
import unittest
from pathlib import Path


EVALUATOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "datasets/dev/odyssey_2_12_smoke/evaluator/evaluator.py"
)


def load_smoke_evaluator():
    spec = importlib.util.spec_from_file_location("smoke_task_evaluator", EVALUATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SmokeTaskEvaluatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "evaluator").mkdir()
        (self.root / "run_outputs").mkdir()
        (self.root / "run_logs").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_smoke_evaluator_can_call_common_evaluator_functions(self):
        (self.root / "run_outputs" / "answer.txt").write_text("Project Alpha is complete.")
        (self.root / "run_logs" / "tool_faults.jsonl").write_text('{"tool": "tesseract"}\n')
        (self.root / "evaluator" / "checks.yaml").write_text(
            "\n".join(
                [
                    "checks:",
                    "  - id: output_exists",
                    "    type: file_exists",
                    "    path: run_outputs/answer.txt",
                    "    weight: 0.25",
                    "  - id: output_contains",
                    "    type: evaluator_function",
                    "    function: evaluate_contain",
                    "    testbed_dir: run_outputs",
                    "    args:",
                    "      doc_type: txt",
                    "      file: answer.txt",
                    "      keywords: [Project Alpha]",
                    "    weight: 0.50",
                    "  - id: tool_log",
                    "    type: log_contains",
                    "    path: run_logs/tool_faults.jsonl",
                    "    value: tesseract",
                    "    weight: 0.25",
                ]
            )
            + "\n"
        )

        result = load_smoke_evaluator().evaluate_task(self.root)

        self.assertEqual(result["complete_pass"], 1)
        self.assertEqual(result["partial_pass"], 1.0)
        self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_smoke_evaluator_accepts_evaluate_function_as_type(self):
        (self.root / "run_outputs" / "answer.txt").write_text("Budget review is ready.")
        (self.root / "evaluator" / "checks.yaml").write_text(
            "\n".join(
                [
                    "checks:",
                    "  - id: direct_type",
                    "    type: evaluate_contain",
                    "    testbed_dir: run_outputs",
                    "    args:",
                    "      doc_type: txt",
                    "      file: answer.txt",
                    "      keywords: [Budget review]",
                    "    weight: 1.0",
                ]
            )
            + "\n"
        )

        result = load_smoke_evaluator().evaluate_task(self.root)

        self.assertEqual(result["complete_pass"], 1)
        self.assertEqual(result["checks"][0]["detail"], "evaluate_contain")


if __name__ == "__main__":
    unittest.main()
