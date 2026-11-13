"""Normalize TorchServe messages and re-emit bare EMF to CloudWatch."""

import base64
import gzip
import json
import sys


MARKER = '{"event_type":"sentiment_inference_v1"'


def handler(event, context):
    result = []
    metrics = []
    for record in event["records"]:
        try:
            envelope = json.loads(gzip.decompress(base64.b64decode(record["data"])))
            lines = []
            normalized = []
            for entry in envelope.get("logEvents", []):
                if MARKER not in entry["message"]:
                    continue
                item = json.loads(entry["message"][entry["message"].index(MARKER):])
                if item.get("event_type") == "sentiment_inference_v1":
                    line = json.dumps(item, separators=(",", ":"), allow_nan=False) + "\n"
                    lines.append(line)
                    if not entry["message"].startswith(MARKER):
                        normalized.append(line)
            data = base64.b64encode("".join(lines).encode()).decode()
            result.append({"recordId": record["recordId"], "result": "Ok" if lines else "Dropped",
                           "data": data})
            metrics.extend(normalized)
        except (ValueError, KeyError, OSError, TypeError):
            result.append({"recordId": record["recordId"], "result": "ProcessingFailed", "data": record["data"]})
    if len(json.dumps({"records": result})) > 5_500_000:
        return {"records": [{"recordId": record["recordId"], "result": "ProcessingFailed",
                             "data": record["data"]} for record in event["records"]]}
    for line in metrics:
        sys.stdout.write(line)
    sys.stdout.flush()
    return {"records": result}
