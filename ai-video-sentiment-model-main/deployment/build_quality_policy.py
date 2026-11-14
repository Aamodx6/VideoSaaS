"""Build a versioned serving policy from independently prepared split artifacts."""

import argparse
import json
import re
from pathlib import Path

import numpy as np

from evaluate_ood import temperature
from quality import distance, entropy, precision_matrix


def build(training, calibration, model_version):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", model_version):
        raise ValueError("Invalid model version")
    embeddings = np.asarray(training, dtype=float)
    if embeddings.ndim != 2 or embeddings.shape[1] != 256 or len(embeddings) < 257 or not np.isfinite(embeddings).all():
        raise ValueError("Need at least 257 finite training embeddings of size 256")
    covariance = np.cov(embeddings, rowvar=False)
    mean = embeddings.mean(axis=0)
    precision = precision_matrix(covariance)
    held_out = calibration["embeddings"]
    if held_out.ndim != 2 or held_out.shape[1] != 256 or len(held_out) < 2 or not np.isfinite(held_out).all():
        raise ValueError("Invalid held-out embeddings")
    temperatures = {}
    confidences, entropies = [], []
    for name in ("emotion", "sentiment"):
        logits = calibration[name + "_logits"]
        if logits.shape != (len(held_out), 7 if name == "emotion" else 3):
            raise ValueError("Held-out rows must align")
        temperatures[name] = temperature(logits, calibration[name + "_labels"])
        scaled = logits / temperatures[name]
        scaled -= scaled.max(axis=1, keepdims=True)
        probabilities = np.exp(scaled)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        confidences.append(probabilities.max(axis=1))
        entropies.append([entropy(row) for row in probabilities])
    return {
        "schema_version": 1, "model_version": model_version,
        "temperature": temperatures, "embedding": {"mean": mean.tolist(), "covariance": covariance.tolist()},
        "limits": {
            "max_distance": float(np.quantile([distance(row, mean, precision) for row in held_out], .99)),
            "min_confidence": float(np.quantile(np.min(confidences, axis=0), .05)),
            "max_entropy": float(np.quantile(np.max(entropies, axis=0), .95)),
            "max_silence_ratio": .9,
            "max_bleep_ratio": .5,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--training", required=True, help="NPZ containing training embeddings")
    parser.add_argument("--calibration", required=True, help="Disjoint held-out logits, labels and embeddings")
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with np.load(args.training, allow_pickle=False) as train, np.load(args.calibration, allow_pickle=False) as held:
        if train["split"].item() != "training" or held["split"].item() != "calibration":
            raise ValueError("Incorrect calibration split roles")
        if set(train["clip_ids"]) & set(held["clip_ids"]):
            raise ValueError("Training and held-out clips overlap")
        policy = build(train["embeddings"], held, args.model_version)
    with Path(args.output).open("x", encoding="utf-8") as stream:
        json.dump(policy, stream, allow_nan=False)
