from __future__ import annotations

import unittest

from dumatebench.evaluator.scoring import final_score


class FinalScoreTests(unittest.TestCase):
    def test_uses_complete_partial_and_judge_weights(self) -> None:
        self.assertEqual(final_score(0, 0.4, 0.8), 0.44)

    def test_normalizes_percentage_inputs(self) -> None:
        self.assertEqual(final_score(100, 50, 25), 0.55)

    def test_clamps_invalid_inputs_to_unit_interval(self) -> None:
        self.assertEqual(final_score(-1, 2, "invalid"), 0.006)


if __name__ == "__main__":
    unittest.main()
