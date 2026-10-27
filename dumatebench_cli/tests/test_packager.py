from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dumatebench_cli.packager import check_task_dir


class PackageCheckTests(unittest.TestCase):
    def test_filled_task_without_authored_compose_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = Path(temp_dir)
            (task / "environment").mkdir()
            (task / "evaluator").mkdir()
            (task / "task.yaml").write_text("task_id: sample\nenvironment:\n  allow_internet: false\n", encoding="utf-8")
            (task / "instruction.md").write_text("Do the task.\n", encoding="utf-8")
            (task / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
            (task / "evaluator" / "evaluator.py").write_text("print('{}')\n", encoding="utf-8")
            (task / "evaluator" / "checks.yaml").write_text(
                "checks:\n  - id: smoke\n    type: evaluate_file_exist\n", encoding="utf-8"
            )

            result = check_task_dir(task)

        self.assertTrue(result.passed)
        self.assertTrue(
            any("docker-compose.yaml not present (optional" in check.message for check in result.checks)
        )


if __name__ == "__main__":
    unittest.main()
