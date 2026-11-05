import unittest

from scripts.verify_quality_review_load import percentile


class QualityReviewLoadTests(unittest.TestCase):
    def test_percentile_uses_nearest_rank_for_small_samples(self):
        values = [10, 20, 30, 40]

        self.assertEqual(percentile(values, 0), 10)
        self.assertEqual(percentile(values, 0.5), 30)
        self.assertEqual(percentile(values, 0.95), 40)

    def test_percentile_handles_empty_samples(self):
        self.assertIsNone(percentile([], 0.95))


if __name__ == "__main__":
    unittest.main()
