import tempfile
import unittest
from pathlib import Path

from dumatebench.agents import adapter_runner


class AdapterRunnerTest(unittest.TestCase):
    def test_parse_action_accepts_command(self):
        action = adapter_runner.parse_action('{"command": "ls /workspace", "reason": "inspect"}')

        self.assertFalse(action["finish"])
        self.assertEqual(action["command"], "ls /workspace")
        self.assertEqual(action["reason"], "inspect")

    def test_parse_action_accepts_finish(self):
        action = adapter_runner.parse_action('{"finish": true, "reason": "done"}')

        self.assertTrue(action["finish"])
        self.assertEqual(action["reason"], "done")

    def test_parse_action_rejects_missing_command(self):
        with self.assertRaises(ValueError):
            adapter_runner.parse_action('{"reason": "no action"}')

    def test_build_state_contains_task_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "instruction.md").write_text("Do task. Save `/outputs/out.txt`.")
            (task_dir / "task.yaml").write_text("task_id: demo\n")
            history = [
                {
                    "action": {"command": "echo hi", "reason": "test"},
                    "observation": {"returncode": 0, "elapsed_sec": 0.1, "output": "hi\n"},
                }
            ]

            state = adapter_runner.build_state(task_dir, 2, 5, history)

        self.assertEqual(state["schema_version"], "0.1")
        self.assertEqual(state["step"], 2)
        self.assertEqual(state["max_steps"], 5)
        self.assertIn("/outputs/out.txt", state["instruction"])
        self.assertNotIn("task_yaml", state)
        self.assertEqual(state["last_observation"]["output"], "hi\n")
        self.assertIn("finish=true", state["system_prompt"])


if __name__ == "__main__":
    unittest.main()
