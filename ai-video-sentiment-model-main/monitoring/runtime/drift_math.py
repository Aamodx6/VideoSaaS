from bisect import bisect_right
from math import isfinite, log, sqrt


def histogram(values, edges):
    counts = [0] * (len(edges) + 1)
    for value in values:
        if isfinite(value):
            counts[bisect_right(edges, value)] += 1
    return counts


def psi(expected, observed, smoothing=0.5):
    if (not expected or len(expected) != len(observed) or not isfinite(smoothing) or smoothing <= 0
            or any(not isfinite(x) or x < 0 for x in (*expected, *observed))
            or sum(expected) <= 0 or sum(observed) <= 0):
        raise ValueError("PSI requires populated matching histograms")
    ref_size, live_size = (sum(counts) + smoothing * len(counts) for counts in (expected, observed))
    score = 0.0
    for ref, live in zip(expected, observed):
        p, q = (ref + smoothing) / ref_size, (live + smoothing) / live_size
        score += (q - p) * log(q / p)
    return score


def embedding_distance(reference, covariance, observed, variance_floor=1e-5):
    """RMS mean shift in reference standard deviations, with regularized variance."""
    size = len(reference)
    if not size or size != len(observed) or size != len(covariance) or variance_floor <= 0:
        raise ValueError("Embedding shapes do not match")
    if any(len(row) != size for row in covariance):
        raise ValueError("Expected square covariance")
    scaled = [(a - b) ** 2 / max(covariance[i][i], variance_floor)
              for i, (a, b) in enumerate(zip(reference, observed))]
    if not all(isfinite(value) for value in scaled):
        raise ValueError("Nonfinite embedding distance")
    return sqrt(sum(scaled) / size)


def confidence_drop(reference, observed):
    if not all(isfinite(value) and 0 <= value <= 1 for value in (reference, observed)):
        raise ValueError("Expected confidence proportions")
    return max(0.0, reference - observed)
