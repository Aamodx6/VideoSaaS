"""Model-versioned quality and out-of-domain decisions."""

import math

import numpy as np


def entropy(probabilities):
    values = [float(value) for value in probabilities if value > 0]
    return -sum(value * math.log(value) for value in values)


def precision_matrix(covariance, regularization=0.001):
    covariance = np.asarray(covariance, dtype=float)
    if covariance.ndim != 2 or covariance.shape[0] != covariance.shape[1]:
        raise ValueError("Expected a square covariance matrix")
    if not np.isfinite(covariance).all() or regularization <= 0:
        raise ValueError("Invalid embedding covariance")
    return np.linalg.pinv(covariance + regularization * np.eye(len(covariance)), hermitian=True)


def prepare_policy(policy, model_version):
    if policy["model_version"] != model_version:
        raise ValueError("Quality policy version mismatch")
    mean = np.asarray(policy["embedding"]["mean"], dtype=float)
    covariance = np.asarray(policy["embedding"]["covariance"], dtype=float)
    if mean.shape != (256,) or covariance.shape != (256, 256) or not np.isfinite(mean).all():
        raise ValueError("Quality policy requires a 256-dimensional embedding")
    if not np.allclose(covariance, covariance.T) or not np.isfinite(covariance).all():
        raise ValueError("Invalid embedding covariance")
    for name in ("emotion", "sentiment"):
        value = policy["temperature"][name]
        if not np.isfinite(value) or value <= 0:
            raise ValueError("Invalid calibration temperature")
    limits = policy["limits"]
    if (not all(np.isfinite(v) for v in limits.values()) or limits["max_distance"] <= 0
            or not 0 < limits["min_confidence"] <= 1 or limits["max_entropy"] <= 0):
        raise ValueError("Invalid OOD thresholds")
    policy.update(mean=mean, precision=precision_matrix(covariance))
    return policy


def distance(embedding, mean, precision):
    delta = np.asarray(embedding, dtype=float) - mean
    if delta.ndim != 1 or not np.isfinite(delta).all():
        raise ValueError("Invalid embedding")
    return float(np.sqrt(max(0, delta @ precision @ delta)))


def quality_reason(stats, limits):
    for name, reason, comparison in (
        ("blur_score", "frames_blurred", "min"),
        ("face_rate", "faces_obscured", "min"),
        ("silence_ratio", "audio_muted", "max"),
        ("bleep_ratio", "audio_obscured", "max"),
        ("transcript_confidence", "transcript_uncertain", "min"),
    ):
        threshold = limits.get(f"{comparison}_{name}")
        value = stats.get(name)
        if threshold is None:
            continue
        if value is None or not math.isfinite(value):
            return "quality_signal_unavailable"
        if (comparison == "min" and value < threshold) or (comparison == "max" and value > threshold):
            return reason
    return None


def classify(stats, probabilities, embedding_distance, limits):
    reason = quality_reason(stats, limits)
    if reason:
        return "out_of_domain", reason
    if embedding_distance > limits["max_distance"]:
        return "out_of_domain", "embedding_shift"
    if min(map(max, probabilities)) < limits["min_confidence"]:
        return "low_confidence", "confidence_below_threshold"
    if max(map(entropy, probabilities)) > limits["max_entropy"]:
        return "low_confidence", "entropy_above_threshold"
    return "ok", None

def bleep_ratio(audio):
    samples = np.asarray(audio, dtype=float)
    windows = samples[:len(samples) // 320 * 320].reshape(-1, 320)
    if not len(windows):
        return 0.0
    energy = np.abs(np.fft.rfft(windows, axis=1)) ** 2
    peaks = energy.argmax(axis=1)
    concentration = energy.max(axis=1) / np.maximum(energy.sum(axis=1), 1e-12)
    audible = np.sqrt(np.mean(windows ** 2, axis=1)) > 0.005
    # A narrow-band tone is a quality proxy. It does not identify censored words.
    tonal = (peaks >= 10) & (peaks <= 60) & (concentration > .65) & audible
    return float(np.mean(tonal))
