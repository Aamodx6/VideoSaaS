import gzip
import json
import math
import os
from collections import Counter
from datetime import datetime, timedelta

import boto3

from drift_math import confidence_drop, embedding_distance, histogram, psi


def read_window(client, bucket, start, end):
    # Firehose partitions by arrival time. Include adjacent partitions for delivery lag.
    for hour in (start - timedelta(hours=1), start, end):
        prefix = hour.strftime("events/dt=%Y-%m-%d/hour=%H/")
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                with client.get_object(Bucket=bucket, Key=obj["Key"])["Body"] as body:
                    with gzip.GzipFile(fileobj=body) as stream:
                        for line in stream:
                            try:
                                value = json.loads(line)
                                if isinstance(value, dict):
                                    yield value
                            except ValueError:
                                continue


def handler(event, context):
    bucket, endpoint, version = (os.environ[name] for name in ("BUCKET", "ENDPOINT", "MODEL_VERSION"))
    client = boto3.client("s3")
    key = f"baselines/{version}.json"
    with client.get_object(Bucket=bucket, Key=key)["Body"] as body:
        baseline = json.load(body)
    if baseline["model_version"] != version or baseline["schema_version"] != 1:
        raise ValueError("Baseline version mismatch")
    end = datetime.fromisoformat(event["time"].replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    samples = {}
    for row in read_window(client, bucket, start, end):
        stamp = row.get("timestamp_ms")
        if (isinstance(stamp, int) and start_ms <= stamp < end_ms and row.get("drift_sample")
                and row.get("event_type") == "sentiment_inference_v1"
                and row.get("model_version") == version and row.get("endpoint") == endpoint
                and row.get("Errors") == 0 and row.get("event_id")):
            samples[row["event_id"]] = row
    minimum = int(os.getenv("MIN_SAMPLES", "100"))
    if minimum < 2:
        raise ValueError("MIN_SAMPLES must be at least two")
    counts = {"DriftSampleCount": len(samples)}
    scores = {}
    if len(samples) >= minimum:
        speech = [row["has_speech"] for row in samples.values() if isinstance(row.get("has_speech"), bool)]
        if len(speech) >= minimum:
            counts["NoSpeechRate"] = 1 - sum(speech) / len(speech)
            counts["NoSpeechIncrease"] = max(0, counts["NoSpeechRate"] - baseline["no_speech_rate"])
        confidence = [(row["emotion_confidence"] + row["sentiment_confidence"]) / 2 for row in samples.values()
                      if "emotion_confidence" in row and "sentiment_confidence" in row]
        if len(confidence) >= minimum:
            counts["ConfidenceMean"] = sum(confidence) / len(confidence)
            counts["ConfidenceDrop"] = confidence_drop(baseline["confidence_mean"], counts["ConfidenceMean"])
        for label, expected in baseline["predicted"].items():
            observed = Counter(row.get(f"{label}_class") for row in samples.values())
            if sum(observed[name] for name in expected) >= minimum:
                scores[f"{label.title()}PSI"] = psi(list(expected.values()), [observed[name] for name in expected])
        for field, reference in baseline["histograms"].items():
            if field == "confidence":
                continue
            values = [row[field] for row in samples.values() if isinstance(row.get(field), (int, float))
                      and math.isfinite(row[field])]
            if len(values) >= minimum:
                scores[f"{field}PSI"] = psi(reference["counts"], histogram(values, reference["edges"]))
        reference = baseline["embedding"]
        vectors = [row["embedding"] for row in samples.values()
                   if isinstance(row.get("embedding"), list) and len(row["embedding"]) == reference["size"]
                   and all(isinstance(v, (float, int)) and math.isfinite(v) for v in row["embedding"])]
        if len(vectors) >= minimum:
            mean = [sum(column) / len(vectors) for column in zip(*vectors)]
            scores["EmbeddingDistance"] = embedding_distance(reference["mean"], reference["covariance"], mean)
        if scores:
            embedding_scale = float(os.getenv("EMBEDDING_SHIFT_THRESHOLD", "0.5"))
            psi_scale = float(os.getenv("PSI_THRESHOLD", "0.25"))
            if not all(math.isfinite(scale) and scale > 0 for scale in (embedding_scale, psi_scale)):
                raise ValueError("Drift thresholds must be finite and positive")
            counts["DriftScore"] = max(value / (embedding_scale if name == "EmbeddingDistance" else psi_scale)
                                       for name, value in scores.items())
    print(json.dumps({"window_end": end.isoformat(), "endpoint": endpoint, "model_version": version,
                      **counts, **scores, "_aws": {
                          "Timestamp": int(datetime.now(end.tzinfo).timestamp() * 1000),
                          "CloudWatchMetrics": [{"Namespace": "VideoSentiment/Monitoring",
                                                  "Dimensions": [["endpoint", "model_version"]],
                                                  "Metrics": [{"Name": name, "Unit": "None"} for name in counts]}],
                      }}, separators=(",", ":"), allow_nan=False))
