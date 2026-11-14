"""Operator workflow. Run with dedicated, temporary erasure permissions."""
import argparse
import os
import re

import boto3
from boto3.dynamodb.conditions import Key


def delete_versions(s3, bucket, prefix, exact=False):
    pages = s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix)
    for page in pages:
        versions = [{"Key": obj["Key"], "VersionId": obj["VersionId"]}
                    for name in ("Versions", "DeleteMarkers") for obj in page.get(name, [])
                    if not exact or obj["Key"] == prefix]
        for start in range(0, len(versions), 1000):
            response = s3.delete_objects(Bucket=bucket, Delete={"Objects": versions[start:start + 1000]})
            if response.get("Errors"):
                raise RuntimeError("Object version erasure failed")


def by_user(table, user):
    query = {"IndexName": "ByUser", "KeyConditionExpression": Key("user_id").eq(user)}
    while True:
        page = table.query(**query)
        yield from page["Items"]
        if not page.get("LastEvaluatedKey"):
            return
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def erase(user, tenants):
    s3 = boto3.client("s3")
    ddb = boto3.resource("dynamodb")
    keys, jobs = ddb.Table(os.environ["KEYS"]), ddb.Table(os.environ["JOBS"])
    for row in list(by_user(keys, user)):
        keys.update_item(Key={"key_hash": row["key_hash"]}, UpdateExpression="SET revoked = :yes",
                         ExpressionAttributeValues={":yes": True})
    active = []
    rows = list(by_user(jobs, user))
    for row in rows:
        identity = {k: row[k] for k in ("tenant_id", "job_id")}
        current = jobs.get_item(Key=identity, ConsistentRead=True).get("Item", {})
        if current.get("status") in ("PENDING_MODERATION", "MODERATION_COMMITTING", "REVIEW_COMMITTING", "SUBMITTING", "RUNNING"):
            active.append(identity)
        elif current.get("status") == "QUEUED":
            jobs.update_item(Key=identity, UpdateExpression="SET #status = :erased",
                             ConditionExpression="#status = :queued",
                             ExpressionAttributeNames={"#status": "status"},
                             ExpressionAttributeValues={":erased": "ERASING", ":queued": "QUEUED"})
    if active:
        raise RuntimeError("Keys revoked. Drain active inference before retrying erasure.")
    for tenant in set(tenants) | {r["tenant_id"] for r in rows}:
        for prefix in ("uploads", "requests"):
            delete_versions(s3, os.environ["VIDEOS"], f"{prefix}/{tenant}/{user}/")
        if os.getenv("QUARANTINE"):
            for prefix in ("uploads", "checked", "hold"):
                delete_versions(s3, os.environ["QUARANTINE"], f"{prefix}/{tenant}/{user}/")
    for row in rows:
        current = jobs.get_item(Key={k: row[k] for k in ("tenant_id", "job_id")},
                                ConsistentRead=True).get("Item", {})
        if current.get("result_key"):
            delete_versions(s3, os.environ["RESULTS"], current["result_key"], exact=True)
        if os.getenv("QUARANTINE"):
            delete_versions(s3, os.environ["QUARANTINE"],
                            f"toxicity/moderation-{row['job_id']}.json", exact=True)
            delete_versions(s3, os.environ["QUARANTINE"],
                            f"toxicity/language-{row['job_id']}.json", exact=True)
        # SageMaker returns an output and failure pointer at submission time.
        for field in ("expected_output", "expected_failure"):
            if current.get(field):
                delete_versions(s3, os.environ["RESULTS"], current[field], exact=True)
        jobs.delete_item(Key={k: row[k] for k in ("tenant_id", "job_id")})
    for row in list(by_user(keys, user)):
        keys.delete_item(Key={"key_hash": row["key_hash"]})
    if os.getenv("SAFETY_CLUSTER_ARN"):
        from safety_store import execute
        execute("DELETE FROM aspect.strikes WHERE user_id=:subject", subject=user)
        execute("DELETE FROM aspect.moderation_events WHERE user_id=:subject", subject=user)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id")
    parser.add_argument("tenant_ids", nargs="+")
    args = parser.parse_args()
    if not all(re.fullmatch(r"[a-f0-9-]{32,36}", key) for key in [args.user_id, *args.tenant_ids]):
        parser.error("Expected opaque user and tenant identifiers")
    erase(args.user_id, args.tenant_ids)
