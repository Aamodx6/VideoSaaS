import json
import os
import time
from datetime import datetime, timedelta

import boto3


def handler(event, context):
    db = boto3.client("dynamodb")
    table = os.environ["FEEDBACK_TABLE"]
    version, endpoint = os.environ["MODEL_VERSION"], os.environ["ENDPOINT"]
    end = datetime.fromisoformat(event["time"].replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0)
    hours = int(os.getenv("ACCURACY_WINDOW_HOURS", "168"))
    correct = {"emotion": 0, "sentiment": 0}
    count = 0
    keys = []
    for offset in range(1, hours + 1):
        hour = (end - timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        keys.extend({"id": {"S": f"acc#{version}#{endpoint}#{hour}#{shard:x}"}} for shard in range(16))
    for offset in range(0, len(keys), 100):
        pending = {table: {"Keys": keys[offset:offset + 100], "ConsistentRead": True}}
        for attempt in range(8):
            batch = db.batch_get_item(RequestItems=pending)
            for item in batch["Responses"].get(table, []):
                count += int(item["feedback_count"]["N"])
                for kind in correct:
                    correct[kind] += int(item[f"{kind}_correct"]["N"])
            pending = batch.get("UnprocessedKeys", {})
            if not pending:
                break
            time.sleep(min(2 ** attempt / 10, 3))
        if pending:
            raise RuntimeError("DynamoDB did not return all accuracy counters")
    metrics = {"FeedbackCount": count}
    if count:
        metrics.update({f"{kind.title()}Accuracy": value / count for kind, value in correct.items()})
        metrics["Accuracy"] = sum(correct.values()) / (2 * count)
    print(json.dumps({"endpoint": endpoint, "model_version": version, **metrics, "_aws": {
        "Timestamp": int(end.timestamp() * 1000),
        "CloudWatchMetrics": [{"Namespace": "VideoSentiment/Monitoring",
                                "Dimensions": [["endpoint", "model_version"]],
                                "Metrics": [{"Name": name, "Unit": "None"} for name in metrics]}]}}))
