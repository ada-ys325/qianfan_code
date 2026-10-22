from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dumatebench_cli.adapter import compose_cmd, compose_service


class AdapterComposeTests(unittest.TestCase):
    def test_uses_legacy_authored_compose_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = Path(temp_dir)
            environment = task / "environment"
            environment.mkdir()
            authored = environment / "docker-compose.yaml"
            authored.write_text("services:\n  task: {}\n", encoding="utf-8")

            self.assertEqual(compose_service(task), "task")
            self.assertEqual(compose_cmd(task)[-1], str(authored))

    def test_generates_harbor_compatible_compose_for_filled_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = Path(temp_dir)
            (task / "environment").mkdir()

            command = compose_cmd(task)

            self.assertEqual(compose_service(task), "main")
            generated = Path(command[-1])
            self.assertTrue(generated.is_file())
            self.assertIn("main:", generated.read_text(encoding="utf-8"))

    def test_compose_project_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = Path(temp_dir)
            (task / "environment").mkdir()

            command = compose_cmd(task, "dumatebench-test-123")

            self.assertEqual(command[:4], ["docker", "compose", "-p", "dumatebench-test-123"])


if __name__ == "__main__":
    unittest.main()
