"""Fit temperatures or evaluate a held-out OOD score file."""

import argparse
import json

import numpy as np


def temperature(logits, labels):
    logits, labels = np.asarray(logits, dtype=float), np.asarray(labels)
    if logits.ndim != 2 or not logits.size or not np.isfinite(logits).all():
        raise ValueError("Expected finite held-out logits")
    if labels.shape != (len(logits),) or labels.dtype.kind not in "iu" or not np.all(
            (labels >= 0) & (labels < logits.shape[1])):
        raise ValueError("Invalid held-out labels")

    def loss(value):
        scaled = logits / value
        scaled -= scaled.max(axis=1, keepdims=True)
        return np.mean(np.log(np.exp(scaled).sum(axis=1)) - scaled[np.arange(len(labels)), labels])

    return float(min(np.geomspace(0.05, 20, 401), key=loss))


def auroc(in_domain, out_of_domain):
    scores = sorted((x, label) for label, group in enumerate((in_domain, out_of_domain)) for x in group)
    if not len(in_domain) or not len(out_of_domain) or any(not np.isfinite(x) for x, _ in scores):
        raise ValueError("Expected finite scores for both cohorts")
    wins = negatives = cursor = 0
    while cursor < len(scores):
        end = cursor + 1
        while end < len(scores) and scores[end][0] == scores[cursor][0]:
            end += 1
        positives = sum(label for _, label in scores[cursor:end])
        tied_negatives = end - cursor - positives
        wins += positives * (negatives + tied_negatives / 2)
        negatives += tied_negatives
        cursor = end
    return wins / (len(in_domain) * len(out_of_domain))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["calibrate", "evaluate"])
    parser.add_argument("input", help="NPZ of held-out logits/labels or JSON of held-out detection scores")
    args = parser.parse_args()
    if args.mode == "calibrate":
        with np.load(args.input, allow_pickle=False) as data:
            report = {name: temperature(data[f"{name}_logits"], data[f"{name}_labels"])
                      for name in ("emotion", "sentiment")}
    else:
        with open(args.input, encoding="utf-8") as stream:
            data = json.load(stream)
        report = {"auroc": auroc(data["in_domain"], data["out_of_domain"])}
    print(json.dumps(report, allow_nan=False))
