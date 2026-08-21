from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dumatebench_cli.packager import check_task_dir
from dumatebench_cli.template import fill_task


class TemplateFillTests(unittest.TestCase):
    def test_fill_creates_self_contained_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            template = root / "template"
            task = root / "task"
            (template / "environment").mkdir(parents=True)
            (template / "environment" / "Dockerfile").write_text(
                "FROM python:3.12-slim\n"
                "COPY datasets/dev/template/environment/setup.sh /setup.sh\n"
                "COPY datasets/dev/template/workspace_seed/ /workspace_seed/\n"
                "COPY datasets/dev/template/task.yaml /opt/dumate/task/task.yaml\n",
                encoding="utf-8",
            )
            (template / "environment" / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            for name in ("network_faults.yaml", "tool_faults.yaml"):
                (template / name).write_text("{}\n", encoding="utf-8")
            (task / "workspace_seed").mkdir(parents=True)
            (task / "workspace_seed" / "input.txt").write_text("input", encoding="utf-8")
            (task / "evaluator").mkdir()
            (task / "evaluator" / "evaluator.py").write_text("print('ok')\n", encoding="utf-8")
            (task / "task.yaml").write_text("task_id: task\n", encoding="utf-8")
            (task / "instruction.md").write_text("Complete the task.\n", encoding="utf-8")

            result = fill_task(task, template)

            self.assertTrue(result.filled)
            self.assertFalse((task / "environment" / "docker-compose.yaml").exists())
            self.assertTrue((task / "environment" / "workspace_seed" / "input.txt").is_file())
            self.assertTrue((task / "environment" / "task_root" / "task.yaml").is_file())
            dockerfile = (task / "environment" / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("COPY setup.sh /setup.sh", dockerfile)
            self.assertIn("COPY workspace_seed/ /workspace_seed/", dockerfile)
            self.assertIn("COPY task_root/task.yaml /opt/dumate/task/task.yaml", dockerfile)

            package_result = check_task_dir(task)
            self.assertTrue(package_result.passed)
            compose_check = next(
                check for check in package_result.checks
                if "docker-compose.yaml" in check.message
            )
            self.assertTrue(compose_check.ok)
            self.assertIn("optional", compose_check.message)

            # Older task packages may still provide an authored compose file;
            # package check must continue to accept those packages.
            (task / "environment" / "docker-compose.yaml").write_text(
                "services:\n  task: {}\n", encoding="utf-8"
            )
            legacy_result = check_task_dir(task)
            self.assertTrue(legacy_result.passed)
            legacy_check = next(
                check for check in legacy_result.checks
                if "docker-compose.yaml" in check.message
            )
            self.assertTrue(legacy_check.advisory)
            self.assertIn("legacy", legacy_check.message)


if __name__ == "__main__":
    unittest.main()
