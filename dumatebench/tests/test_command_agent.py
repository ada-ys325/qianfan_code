import unittest

from dumatebench.agents.command_agent import run_with_timeout


class CommandAgentTest(unittest.TestCase):
    def test_run_with_timeout_returns_completed_process(self):
        result, elapsed, timed_out = run_with_timeout(["python3", "-c", "print('ok')"], timeout=5)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(timed_out)
        self.assertLess(elapsed, 5)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_run_with_timeout_terminates_long_running_process(self):
        result, elapsed, timed_out = run_with_timeout(["python3", "-c", "import time; time.sleep(5)"], timeout=1)

        self.assertEqual(result.returncode, 124)
        self.assertTrue(timed_out)
        self.assertLess(elapsed, 3)


if __name__ == "__main__":
    unittest.main()
