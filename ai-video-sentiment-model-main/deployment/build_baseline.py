"""Run the serving pipeline over MELD training clips and publish a versioned baseline."""

import csv
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import boto3
import numpy as np

from inference import model_fn, predict_video
from telemetry import DIMENSION_VALUE, EMBEDDING_SIZE, EMOTIONS, SENTIMENTS, Observation


BINS = {
    "video_duration": [0, 1, 2, 4, 8, 15, 30, 60, 120, 300],
    "frame_count": [0, 15, 30, 60, 120, 240, 480, 960, 1800, 3600],
    "audio_loudness": [-96, -70, -55, -45, -35, -25, -18, -12, -6, 0],
    "confidence": [i / 10 for i in range(1, 10)],
}


def histogram(values, edges):
    return np.bincount(np.searchsorted(edges, values, side="right"), minlength=len(edges) + 1).tolist()


def main():
    version = os.environ["MODEL_VERSION"]
    if not DIMENSION_VALUE.fullmatch(version):
        raise ValueError("MODEL_VERSION must be a short alphanumeric identifier")
    bucket = os.environ["MONITORING_BUCKET"]
    prefix = os.getenv("BASELINE_PREFIX", "baselines/").rstrip("/")
    source = Path(os.environ["MELD_TRAIN_CSV"])
    videos = Path(os.environ["MELD_TRAIN_VIDEO_DIR"])
    model = model_fn(os.environ["MODEL_DIR"])
    truth = {"emotion": Counter(), "sentiment": Counter()}
    predicted = {"emotion": Counter(), "sentiment": Counter()}
    values = {name: [] for name in BINS}
    embeddings = []
    completed = failed = speech = 0
    with source.open(encoding="utf-8-sig", newline="") as stream, tempfile.TemporaryDirectory() as temp:
        for row in csv.DictReader(stream):
            for name in ("emotion", "sentiment"):
                truth[name][row[name.title()].strip().lower()] += 1
            path = videos / f"dia{int(row['Dialogue_ID'])}_utt{int(row['Utterance_ID'])}.mp4"
            observation = Observation(sample_rate=1)
            try:
                predict_video(str(path), model, temp, observation)
            except Exception:
                failed += 1
                continue
            completed += 1
            speech += int(observation.stats["has_speech"])
            for name in ("video_duration", "frame_count", "audio_loudness"):
                if name in observation.stats:
                    values[name].append(observation.stats[name])
            selected = observation.selected
            if selected:
                for name in ("emotion", "sentiment"):
                    predicted[name][selected[f"{name}_class"]] += 1
                values["confidence"].append((selected["emotion_confidence"] + selected["sentiment_confidence"]) / 2)
                if "embedding" in selected:
                    embeddings.append(selected["embedding"])
    if len(embeddings) < 2 or any(not sample for sample in values.values()):
        raise ValueError("Insufficient baseline observations")
    if failed / (completed + failed) > float(os.getenv("BASELINE_MAX_FAILURE_RATE", "0.05")):
        raise ValueError(f"Baseline rejected: {failed} of {completed + failed} training clips failed")
    data = {
        "schema_version": 1, "model_version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "clips": completed, "failed_clips": failed,
        "no_speech_rate": (completed - speech) / completed,
        "predicted": {name: {label: predicted[name][label] for label in labels}
                      for name, labels in (("emotion", EMOTIONS), ("sentiment", SENTIMENTS))},
        "ground_truth": {name: dict(counts) for name, counts in truth.items()},
        "histograms": {name: {"edges": edges, "counts": histogram(values[name], edges)}
                       for name, edges in BINS.items()},
        "confidence_mean": float(np.mean(values["confidence"])),
        "embedding": {"count": len(embeddings), "size": EMBEDDING_SIZE,
                      "mean": np.mean(embeddings, axis=0).tolist(),
                      "covariance": np.cov(embeddings, rowvar=False).tolist()},
    }
    key = f"{prefix}/{version}.json"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, ContentType="application/json",
                                  Body=json.dumps(data, separators=(",", ":"), allow_nan=False).encode(),
                                  IfNoneMatch="*")
    print(f"Saved baseline for {completed} clips ({failed} skipped) to s3://{bucket}/{key}")


if __name__ == "__main__":
    main()
