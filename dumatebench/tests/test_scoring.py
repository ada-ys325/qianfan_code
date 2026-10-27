import unittest

from dumatebench.evaluator.scoring import final_score


class FinalScoreTest(unittest.TestCase):
    def test_checklist_and_judge_weights_are_thirty_seventy(self):
        self.assertEqual(final_score(0.5, 0.8), 0.71)

    def test_percentage_scores_are_normalized(self):
        self.assertEqual(final_score(50, 80), 0.71)


if __name__ == "__main__":
    unittest.main()
