import math
import os
import random
import re
import time
import uuid
from contextlib import contextmanager


EVENT = "sentiment_inference_v1"
NAMESPACE = "VideoSentiment/Inference"
EMOTIONS = ("anger", "disgust", "fear", "joy", "neutral", "sadness", "surprise")
SENTIMENTS = ("negative", "neutral", "positive")
EMBEDDING_SIZE = 256
DIMENSION_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
PSEUDONYM = re.compile(r"[a-f0-9]{64}\Z")
OPAQUE_ID = re.compile(r"[a-f0-9]{32}\Z")
STAGES = {
    "input": "InputLatency",
    "frame": "FrameExtractionLatency",
    "audio": "AudioExtractionLatency",
    "text": "TranscriptionTextLatency",
    "fusion": "FusionLatency",
    "output": "OutputLatency",
}


def dimension(value, default):
    return value if isinstance(value, str) and DIMENSION_VALUE.fullmatch(value) else default


class Observation:
    def __init__(self, sample_rate=None):
        self.started = time.perf_counter()
        self.endpoint = dimension(os.getenv("ENDPOINT_NAME"), "sentiment-analysis-endpoint")
        self.model_version = dimension(os.getenv("MODEL_VERSION"), "unversioned")
        rate = float(os.getenv("DRIFT_SAMPLE_RATE", "0.05")) if sample_rate is None else sample_rate
        if not 0 <= rate <= 1:
            raise ValueError("DRIFT_SAMPLE_RATE must be between zero and one")
        self.sampled = random.random() < rate
        self.event_id = uuid.uuid4().hex
        self.identifiers = {"request_id": self.event_id}
        self.timings = {}
        self.stats = {}
        self.error_type = None
        self.segment_errors = 0
        self.segment_count = 0
        self.valid_segments = 0
        self.selected = None

    def identify(self, payload):
        if not isinstance(payload, dict):
            return
        for field in ("user_id", "tenant_id", "request_id"):
            value = payload.get(field)
            pattern = OPAQUE_ID if field == "request_id" else PSEUDONYM
            if isinstance(value, str) and pattern.fullmatch(value):
                self.identifiers[field] = value

    @contextmanager
    def stage(self, name):
        started = time.perf_counter()
        try:
            yield
        except Exception:
            self.error_type = name + "_error"
            raise
        finally:
            metric = STAGES[name]
            self.timings[metric] = self.timings.get(metric, 0.0) + (time.perf_counter() - started) * 1000

    def segment(self, emotion, sentiment, embedding):
        self.segment_count += 1
        if not all(math.isfinite(item["confidence"]) for item in (emotion, sentiment)):
            return
        self.valid_segments += 1
        # One uniformly selected utterance per request bounds the log size and weight.
        if random.randrange(self.valid_segments) == 0:
            self.selected = {
                "emotion_class": emotion["label"], "sentiment_class": sentiment["label"],
                "emotion_confidence": emotion["confidence"], "sentiment_confidence": sentiment["confidence"],
            }
            if self.sampled and embedding is not None and len(embedding) == EMBEDDING_SIZE:
                if all(math.isfinite(value) for value in embedding):
                    self.selected["embedding"] = [round(value, 5) for value in embedding]

    def event(self, failed):
        fields = {
            "Requests": (1, "Count"), "Errors": (int(failed), "Count"),
            "RequestLatency": ((time.perf_counter() - self.started) * 1000, "Milliseconds"),
            **{name: (value, "Milliseconds") for name, value in self.timings.items()},
        }
        for field, name, unit in (
            ("video_duration", "VideoDuration", "Seconds"), ("frame_count", "FrameCount", "Count"),
            ("audio_loudness", "AudioLoudnessDbfs", "None"), ("has_speech", "HasSpeech", "Count"),
        ):
            if field in self.stats:
                fields[name] = (int(self.stats[field]) if field == "has_speech" else self.stats[field], unit)
        if "ood_detected" in self.stats:
            fields["OodRate"] = (int(self.stats["ood_detected"]), "None")
            fields["DegradedInputRate"] = (int(self.stats["degraded_input"]), "None")
        timestamp = int(time.time() * 1000)
        entry = {
            "event_type": EVENT, "timestamp_ms": timestamp, "event_id": self.event_id,
            "endpoint": self.endpoint, "model_version": self.model_version, **self.identifiers,
            "error_type": self.error_type if failed or self.segment_errors else None,
            "segment_errors": self.segment_errors, "utterance_count": self.segment_count,
            "drift_sample": self.sampled, **self.stats,
        }
        dimensions = ["endpoint", "model_version"]
        directives = [{"Namespace": NAMESPACE, "Dimensions": [dimensions],
                       "Metrics": [{"Name": name, "Unit": unit} for name, (_, unit) in fields.items()]}]
        entry.update({name: round(value, 5) for name, (value, _) in fields.items()})
        if self.selected and not failed:
            entry.update(self.selected)
            for label in ("emotion", "sentiment"):
                metric, confidence = f"{label.title()}ClassCount", f"{label.title()}Confidence"
                entry[metric] = 1
                entry[confidence] = self.selected[label + "_confidence"]
                directives.append({"Namespace": NAMESPACE, "Dimensions": [dimensions + [label + "_class"]],
                                   "Metrics": [{"Name": metric, "Unit": "Count"}]})
                directives[0]["Metrics"].append({"Name": confidence, "Unit": "None"})
        entry["_aws"] = {"Timestamp": timestamp, "CloudWatchMetrics": directives}
        return entry
