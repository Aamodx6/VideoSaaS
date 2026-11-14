import hashlib
import json
import os
import re
import socket
import ssl
import time
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError


ddb = boto3.client("dynamodb")
s3 = boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
encode = TypeSerializer().serialize
decode = TypeDeserializer().deserialize
UUID = re.compile(r"[a-f0-9]{32}")
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_BYTES", str(128 * 1024**2)))


def item(values):
    return {key: encode(value) for key, value in values.items()}


def fetch(table, **key):
    response = ddb.get_item(TableName=os.environ[table], Key=item(key), ConsistentRead=True)
    return {k: decode(v) for k, v in response.get("Item", {}).items()}


def reply(status_code, **body):
    return {"statusCode": status_code, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body)}


def cache_command(*args):
    try:
        wire = f"*{len(args)}\r\n" + "".join(f"${len(arg)}\r\n{arg}\r\n" for arg in args)
        host = os.environ["CACHE_HOST"]
        with socket.create_connection((host, 6379), timeout=0.25) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as conn:
                conn.sendall(wire.encode())
                with conn.makefile("rb") as response:
                    return response.readline(64)
    except (OSError, ValueError):
        return b""


def rate_allowed(tenant, now, limit):
    period = "rate#" + str(now // 60)
    script = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],120) end; return n"
    result = cache_command("EVAL", script, "1", f"{tenant}:{period}")
    if result.startswith(b":"):
        return int(result[1:]) <= limit
    try:
        ddb.update_item(TableName=os.environ["QUOTAS"], Key=item({"tenant_id": tenant, "period": period}),
                        UpdateExpression="SET expires_at = :ttl ADD used :one",
                        ConditionExpression="attribute_not_exists(used) OR used < :limit",
                        ExpressionAttributeValues=item({":ttl": now + 180, ":one": 1, ":limit": limit}))
    except ddb.exceptions.ConditionalCheckFailedException:
        return False
    return True


def quota_available(tenant, period, limit):
    # Positive cached entries only reject early. The DynamoDB transaction
    # remains authoritative when the cache is missing, stale, or unavailable.
    cache_key = f"{tenant}:quota:{period}:{limit}"
    if cache_command("EXISTS", cache_key) == b":1\r\n":
        return False
    exhausted = fetch("QUOTAS", tenant_id=tenant, period=period).get("used", 0) >= limit
    if exhausted:
        cache_command("SET", cache_key, "1", "EX", "15")
    return not exhausted


def api(event, _context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = headers.get("x-aspect-api-key", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        return reply(401, error="Invalid API key")
    identity = fetch("KEYS", key_hash=hashlib.sha256(token.encode()).hexdigest())
    now = int(time.time())
    if not identity or identity.get("revoked") or identity.get("expires_at", 0) <= now:
        return reply(401, error="Invalid API key")
    tenant, user = identity["tenant_id"], identity["user_id"]
    quarantine = os.getenv("QUARANTINE")
    policy = "active"
    if quarantine and event["httpMethod"] == "POST":
        from safety_store import account_policy
        try:
            policy = account_policy(tenant, user)
        except Exception:
            return reply(503, code="SAFETY_CHECK_UNAVAILABLE", error="Safety verification unavailable")
        if policy == "suspend":
            return reply(403, code="ACCOUNT_SUSPENDED", error="Account suspended")
    rate = int(identity.get("rate_per_minute", 60))
    if policy == "throttle":
        rate = min(rate, 5)
    if not rate_allowed(tenant, now, rate):
        return reply(429, error="Rate limit exceeded")
    period = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    if event["httpMethod"] == "GET":
        if event.get("resource") == "/quota":
            counter = fetch("QUOTAS", tenant_id=tenant, period=period)
            return reply(200, period=period, requests_used=int(counter.get("used", 0)),
                         max_requests=int(identity["monthly_limit"]))
        job_id = (event.get("pathParameters") or {}).get("jobId", "")
        job = fetch("JOBS", tenant_id=tenant, job_id=job_id)
        if not job or job.get("user_id") != user or job["expires_at"] <= now:
            return reply(404, error="Job not found")
        output = {"job_id": job_id, "status": job["status"], "account_policy": job.get("account_policy", "active")}
        if job["status"] == "BLOCKED":
            return reply(422, **output, code=job["moderation_code"])
        if job["status"] == "SUCCEEDED":
            result = s3.get_object(Bucket=os.environ["RESULTS"], Key=job["result_key"])
            if result["ContentLength"] > 1024**2:
                return reply(413, code="RESULT_TOO_LARGE", error="Analysis exceeds result limit")
            with result["Body"] as stream:
                output["analysis"] = json.load(stream)
        return reply(200, **output)
    try:
        payload = json.loads(event.get("body") or "{}")
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return reply(400, error="Expected a JSON object")
    if event["resource"] == "/uploads":
        if not quota_available(tenant, period, identity["monthly_limit"]):
            return reply(429, error="Monthly quota exceeded")
        upload_id = uuid.uuid4().hex
        key = f"uploads/{tenant}/{user}/{upload_id}.mp4"
        if quarantine:
            upload = s3.generate_presigned_post(
                quarantine, key, Fields={"Content-Type": "video/mp4"},
                Conditions=[{"Content-Type": "video/mp4"}, ["content-length-range", 1, MAX_VIDEO_BYTES]],
                ExpiresIn=600)
            return reply(200, upload_id=upload_id, method="POST", **upload)
        url = s3.generate_presigned_url("put_object", Params={
            "Bucket": os.environ["VIDEOS"], "Key": key, "ContentType": "video/mp4",
            "IfNoneMatch": "*"}, ExpiresIn=600)
        return reply(200, upload_id=upload_id, url=url, headers={
            "Content-Type": "video/mp4", "If-None-Match": "*"})
    upload_id = payload.get("upload_id")
    if not isinstance(upload_id, str) or not UUID.fullmatch(upload_id):
        return reply(400, error="Invalid upload identifier")
    job_id = upload_id
    key = f"uploads/{tenant}/{user}/{upload_id}.mp4"
    existing = fetch("JOBS", tenant_id=tenant, job_id=job_id)
    if existing:
        return reply(202, job_id=job_id) if existing["user_id"] == user else reply(404, error="Job not found")
    try:
        uploaded = s3.head_object(Bucket=quarantine or os.environ["VIDEOS"], Key=key)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] in (403, 404):
            return reply(404, error="Upload not found")
        raise
    if not 0 < uploaded["ContentLength"] <= MAX_VIDEO_BYTES:
        return reply(413, error="Video exceeds upload limits")
    if quarantine and not uploaded.get("VersionId"):
        return reply(503, error="Upload version unavailable")
    moderation_fields = {"upload_version": uploaded["VersionId"]} if quarantine else {}
    try:
        ddb.transact_write_items(TransactItems=[
            {"Update": {"TableName": os.environ["QUOTAS"], "Key": item({"tenant_id": tenant, "period": period}),
                        "UpdateExpression": "SET expires_at = :ttl ADD used :one",
                        "ConditionExpression": "attribute_not_exists(used) OR used < :limit",
                        "ExpressionAttributeValues": item({":ttl": now + 93 * 86400, ":one": 1,
                                                           ":limit": identity["monthly_limit"]})}},
            {"Put": {"TableName": os.environ["JOBS"], "ConditionExpression": "attribute_not_exists(job_id)",
                     "Item": item({"tenant_id": tenant, "job_id": job_id, "user_id": user,
                                   "status": "PENDING_MODERATION" if quarantine else "QUEUED",
                                   "account_policy": policy,
                                   "video_key": key, "created_at": now, "expires_at": now + 30 * 86400,
                                   **moderation_fields})}},
        ])
    except ddb.exceptions.TransactionCanceledException as exc:
        reasons = exc.response.get("CancellationReasons", [])
        existing = fetch("JOBS", tenant_id=tenant, job_id=job_id)
        if existing and existing["user_id"] == user:
            return reply(202, job_id=job_id)
        if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            return reply(429, error="Monthly quota exceeded")
        raise
    return reply(202, job_id=job_id)


def dispatch(event, _context):
    sqs = boto3.client("sqs")
    for record in event["Records"]:
        row = record["dynamodb"]["NewImage"]
        payload = json.dumps({key: row[key]["S"] for key in ("tenant_id", "job_id")})
        if row["status"]["S"] == "PENDING_MODERATION":
            machine = boto3.client("stepfunctions")
            try:
                machine.start_execution(stateMachineArn=os.environ["MODERATION_WORKFLOW"],
                                        name=row["job_id"]["S"], input=payload)
            except machine.exceptions.ExecutionAlreadyExists:
                continue
        else:
            sqs.send_message(QueueUrl=os.environ["QUEUE"], MessageBody=payload)


def submit(event, _context):
    runtime = boto3.client("sagemaker-runtime", config=Config(retries={"total_max_attempts": 1}))
    for record in event["Records"]:
        identity = json.loads(record["body"])
        job = fetch("JOBS", **identity)
        if not job or job["status"] not in ("QUEUED", "SUBMITTING"):
            continue
        if os.getenv("QUARANTINE") and job.get("moderation_decision") != "PASS":
            raise RuntimeError("Job has no moderation approval")
        if os.getenv("QUARANTINE"):
            from safety_store import account_policy
            if account_policy(job["tenant_id"], job["user_id"]) == "suspend":
                ddb.update_item(TableName=os.environ["JOBS"], Key=item(identity),
                                UpdateExpression="SET #status = :blocked, moderation_code = :code",
                                ConditionExpression="#status = :queued",
                                ExpressionAttributeNames={"#status": "status"},
                                ExpressionAttributeValues=item({":queued": "QUEUED", ":blocked": "BLOCKED",
                                                                 ":code": "ACCOUNT_SUSPENDED"}))
                continue
        key = f"requests/{job['tenant_id']}/{job['user_id']}/{job['job_id']}.json"
        s3.put_object(Bucket=os.environ["VIDEOS"], Key=key, ContentType="application/json",
                      Body=json.dumps({"video_path": f"s3://{os.environ['VIDEOS']}/{job['video_key']}"}))
        try:
            ddb.update_item(TableName=os.environ["JOBS"], Key=item(identity),
                            UpdateExpression="SET #status = :submitting, lease_until = :until",
                            ConditionExpression="#status = :queued OR (#status = :submitting AND lease_until < :now)",
                            ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues=item({
                                ":queued": "QUEUED", ":submitting": "SUBMITTING",
                                ":until": int(time.time()) + 300, ":now": int(time.time())}))
        except ddb.exceptions.ConditionalCheckFailedException:
            if fetch("JOBS", **identity).get("status") == "SUBMITTING":
                raise
            continue
        result = runtime.invoke_endpoint_async(EndpointName=os.environ["ENDPOINT"], ContentType="application/json",
                                      InputLocation=f"s3://{os.environ['VIDEOS']}/{key}", InferenceId=job["job_id"],
                                      RequestTTLSeconds=3600, InvocationTimeoutSeconds=900)
        root = f"s3://{os.environ['RESULTS']}/"
        pointers = {field: result.get(name, "").removeprefix(root) if result.get(name, "").startswith(root) else ""
                    for field, name in ((":output", "OutputLocation"), (":failure", "FailureLocation"))}
        try:
            ddb.update_item(TableName=os.environ["JOBS"], Key=item(identity),
                            UpdateExpression="SET #status = :next, expected_output = :output, expected_failure = :failure",
                            ConditionExpression="#status = :current", ExpressionAttributeNames={"#status": "status"},
                            ExpressionAttributeValues=item({":next": "RUNNING", ":current": "SUBMITTING", **pointers}))
        except ddb.exceptions.ConditionalCheckFailedException:
            pass  # The result event can win the race.


def result_event(event, _context):
    entries = []
    for record in event["Records"]:
        message = json.loads(record["Sns"]["Message"])
        if message.get("requestParameters", {}).get("endpointName") != os.environ["ENDPOINT"]:
            continue
        entries.append({
            "EventBusName": os.environ["RESULT_BUS"], "Source": "aspect.sagemaker",
            "DetailType": "Inference result", "Detail": json.dumps({
                "job_id": message["inferenceId"], "status": message["invocationStatus"],
                "output": message.get("responseParameters", {}).get("outputLocation"),
            }),
        })
    if entries and boto3.client("events").put_events(Entries=entries)["FailedEntryCount"]:
        raise RuntimeError("Could not publish model result")


def complete(event, _context):
    detail = event["detail"]
    job_id = detail["job_id"]
    if not UUID.fullmatch(job_id):
        raise ValueError("Unexpected inference identifier")
    status = "SUCCEEDED" if detail["status"] == "Completed" else "FAILED"
    prefix = f"s3://{os.environ['RESULTS']}/output/"
    uri = detail.get("output") or ""
    if status == "SUCCEEDED" and not uri.startswith(prefix):
        raise ValueError("Unexpected SageMaker output location")
    key = uri.removeprefix(f"s3://{os.environ['RESULTS']}/") if status == "SUCCEEDED" else ""
    response = ddb.query(TableName=os.environ["JOBS"], IndexName="ByJob",
                         KeyConditionExpression="job_id = :id", ExpressionAttributeValues=item({":id": job_id}))
    rows = response["Items"]
    if not rows:
        return  # An operator may have erased the job while the endpoint drained.
    if len(rows) != 1:
        raise ValueError("SageMaker result has no unique job")
    identity = {field: decode(rows[0][field]) for field in ("tenant_id", "job_id")}
    try:
        ddb.update_item(TableName=os.environ["JOBS"], Key=item(identity),
                        UpdateExpression="SET #status = :next, result_key = :result, completed_at = :now",
                        ConditionExpression="#status IN (:running, :submitting)",
                        ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues=item({
                            ":next": status, ":result": key, ":now": int(time.time()),
                            ":running": "RUNNING", ":submitting": "SUBMITTING"}))
    except ddb.exceptions.ConditionalCheckFailedException:
        row = fetch("JOBS", **identity)
        if row.get("status") != status or row.get("result_key") != key:
            return
    boto3.client("sns").publish(TopicArn=os.environ["NOTIFICATIONS"],
                                Message=json.dumps({"job_id": job_id, "status": status}))
