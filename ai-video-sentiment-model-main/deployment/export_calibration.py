"""Export raw model outputs only from an explicitly supplied offline MELD split."""

import argparse
import csv
import hashlib
import os
import tempfile
from pathlib import Path

import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--videos", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--split", choices=["training", "calibration"], required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    os.environ.pop("QUALITY_POLICY_PATH", None)
    os.environ["REQUIRE_QUALITY_POLICY"] = "0"
    from inference import EMOTION_MAP, SENTIMENT_MAP, model_fn, predict_video

    model = model_fn(args.model_dir)
    emotions, sentiments = {v: k for k, v in EMOTION_MAP.items()}, {v: k for k, v in SENTIMENT_MAP.items()}
    rows = []
    with open(args.csv, encoding="utf-8-sig", newline="") as stream, tempfile.TemporaryDirectory() as temp:
        for record in csv.DictReader(stream):
            path = Path(args.videos) / f"dia{int(record['Dialogue_ID'])}_utt{int(record['Utterance_ID'])}.mp4"
            outputs = []
            predict_video(str(path), model, temp, calibration_sink=outputs)
            if len(outputs) != 1:
                raise ValueError("Calibration requires exactly one classified utterance per MELD clip")
            rows.append({**outputs[0], "emotion_labels": emotions[record["Emotion"].lower()],
                         "sentiment_labels": sentiments[record["Sentiment"].lower()],
                         "clip_ids": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not rows:
        raise ValueError("No calibration rows")
    with Path(args.output).open("xb") as destination:
        np.savez_compressed(destination, **{name: np.asarray([row[name] for row in rows]) for name in rows[0]},
                            split=np.asarray(args.split))


if __name__ == "__main__":
    main()
