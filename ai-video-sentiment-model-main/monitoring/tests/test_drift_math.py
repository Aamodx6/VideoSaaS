import unittest

from drift_math import confidence_drop, embedding_distance, histogram, psi


class DriftMathTest(unittest.TestCase):
    def test_psi_stable_and_shifted(self):
        self.assertEqual(psi([10, 20, 0], [10, 20, 0]), 0)
        self.assertGreater(psi([90, 10, 0], [0, 10, 90]), 1)
        self.assertAlmostEqual(psi([7, 10], [20, 1]), psi([20, 1], [7, 10]))
        with self.assertRaises(ValueError):
            psi([1, 2], [0, 0])

    def test_histogram_includes_overflow(self):
        self.assertEqual(histogram([-1, 0, 1, 2, 9, float("nan")], [0, 2]), [1, 2, 2])

    def test_embedding_regularization_and_shape(self):
        self.assertEqual(embedding_distance([0, 1], [[4, 0], [0, 0]], [2, 1]), 2 ** -0.5)
        self.assertGreater(embedding_distance([0], [[0]], [1]), 0)
        with self.assertRaises(ValueError):
            embedding_distance([0, 1], [[1]], [0, 1])

    def test_confidence_drop(self):
        self.assertAlmostEqual(confidence_drop(0.9, 0.7), 0.2)
        self.assertEqual(confidence_drop(0.8, 0.9), 0)
        with self.assertRaises(ValueError):
            confidence_drop(1.1, 0.9)
