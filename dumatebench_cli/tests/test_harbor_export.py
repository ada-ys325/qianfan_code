from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from dumatebench_cli.harbor_export import export_task, render_task_toml


class HarborExportTests(unittest.TestCase):
    def test_render_maps_resources_and_collects_outputs(self) -> None:
        warnings: list[str] = []
        text = render_task_toml(
            {
                "task_id": "sample-task",
                "task_name": "Sample",
                "agent": {"timeout_sec": 900, "user": "agent", "workdir": "/workspace"},
                "environment": {
                    "allow_internet": False,
                    "cpus": 2,
                    "memory_mb": 4096,
                    "storage_mb": 12000,
                    "gpus": 0,
                },
            },
            warnings,
        )

        config = tomllib.loads(text)
        self.assertEqual(config["artifacts"], [{"source": "/outputs", "destination": "outputs"}])
        self.assertEqual(config["environment"]["network_mode"], "none")
        self.assertEqual(config["environment"]["cpus"], 2)
        self.assertEqual(config["environment"]["memory_mb"], 4096)
        self.assertEqual(config["environment"]["storage_mb"], 12000)
        self.assertEqual(config["environment"]["gpus"], 0)
        self.assertEqual(warnings, [])

    def test_export_writes_harbor_package_without_runtime_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = root / "task"
            output = root / "exported"
            (task / "environment").mkdir(parents=True)
            (task / "evaluator").mkdir()
            (task / "run_outputs").mkdir()
            (task / "run_outputs" / "old.txt").write_text("old", encoding="utf-8")
            (task / "task.yaml").write_text(
                "task_id: sample-task\nagent:\n  timeout_sec: 30\nenvironment:\n  allow_internet: true\n",
                encoding="utf-8",
            )
            (task / "instruction.md").write_text("Do the task", encoding="utf-8")
            (task / "evaluator" / "checks.yaml").write_text("checks: []\n", encoding="utf-8")
            (task / "evaluator" / "evaluator.py").write_text("print('ok')\n", encoding="utf-8")

            result = export_task(task, output)

            self.assertEqual(result.task_id, "sample-task")
            self.assertTrue((output / "task.toml").is_file())
            self.assertTrue((output / "tests" / "test.sh").is_file())
            self.assertTrue((output / "tests" / "evaluator.py").is_file())
            self.assertFalse((output / "run_outputs").exists())


if __name__ == "__main__":
    unittest.main()
