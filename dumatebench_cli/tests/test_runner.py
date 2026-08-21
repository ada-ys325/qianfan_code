from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dumatebench_cli import runner
from dumatebench_cli.adapter import AdapterRunResult


class RunnerEvaluatorTests(unittest.TestCase):
    def test_evaluator_env_points_to_shared_source_evaluator(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env = runner._evaluator_env()

        expected = Path(runner.__file__).resolve().parents[2] / "dumatebench" / "evaluator" / "evaluate.py"
        self.assertEqual(Path(env["DUMATE_EVALUATE_PY"]), expected.resolve())

    def test_evaluator_env_preserves_explicit_override(self) -> None:
        with patch.dict(os.environ, {"DUMATE_EVALUATE_PY": "/tmp/custom-evaluate.py"}, clear=True):
            env = runner._evaluator_env()

        self.assertEqual(env["DUMATE_EVALUATE_PY"], "/tmp/custom-evaluate.py")

    def _make_task(self, root: Path) -> Path:
        task = root / "task"
        (task / "environment").mkdir(parents=True)
        (task / "evaluator").mkdir()
        (task / "task.yaml").write_text("task_id: task\n", encoding="utf-8")
        (task / "instruction.md").write_text("Do the task\n", encoding="utf-8")
        (task / "evaluator" / "evaluator.py").write_text("print('ok')\n", encoding="utf-8")
        return task

    def test_missing_reward_is_a_runner_error_and_passes_evaluator_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = self._make_task(Path(temp_dir))
            evaluator_env: dict[str, str] | None = None

            def fake_run(cmd, cwd=None, check=True, capture=True, env=None):
                nonlocal evaluator_env
                if any(str(part).endswith("evaluator.py") for part in cmd):
                    evaluator_env = env
                    return subprocess.CompletedProcess(cmd, 1, "", "evaluator crashed")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(runner, "_run", side_effect=fake_run), patch.object(
                runner,
                "run_adapter_loop",
                return_value=AdapterRunResult(True, "done", False, 1),
            ):
                result = runner.run_single_task(
                    task,
                    "python3 agent.py",
                    max_steps=1,
                    adapter_timeout=10,
                    no_build=True,
                    keep_containers=False,
                )

            self.assertEqual(result.status, "error")
            self.assertIsNone(result.reward_path)
            self.assertIsNotNone(evaluator_env)
            self.assertIn("DUMATE_EVALUATE_PY", evaluator_env or {})
            self.assertIn("evaluator crashed", result.error or "")
            status = json.loads((task / "run_logs" / "agent_status.json").read_text(encoding="utf-8"))
            self.assertIn("evaluator crashed", status["error"])

    def test_nonpassing_reward_is_completed_even_when_evaluator_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = self._make_task(Path(temp_dir))

            def fake_run(cmd, cwd=None, check=True, capture=True, env=None):
                if any(str(part).endswith("evaluator.py") for part in cmd):
                    (task / "run_outputs").mkdir(parents=True, exist_ok=True)
                    (task / "run_outputs" / "reward.json").write_text(
                        json.dumps({"complete_pass": 0, "partial_pass": 0.5}), encoding="utf-8"
                    )
                    return subprocess.CompletedProcess(cmd, 1, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(runner, "_run", side_effect=fake_run), patch.object(
                runner,
                "run_adapter_loop",
                return_value=AdapterRunResult(True, "done", False, 1),
            ):
                result = runner.run_single_task(
                    task,
                    "python3 agent.py",
                    max_steps=1,
                    adapter_timeout=10,
                    no_build=True,
                    keep_containers=False,
                )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.evaluator_returncode, 1)
            self.assertIsNotNone(result.reward_path)
            self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
