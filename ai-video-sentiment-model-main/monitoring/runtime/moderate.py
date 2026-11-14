"""Bounded moderation tasks for the Step Functions upload workflow."""

import json
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from data_jobs import MAX_VIDEO_BYTES, ddb, fetch, item, s3
from moderation_policy import decision
from safety_store import audit


def review_level():
    return float(os.getenv("MODERATION_REVIEW_SCORE", "65"))


def block_level():
    return float(os.getenv("MODERATION_BLOCK_SCORE", "90"))


def scan(event, _):
    job = fetch("JOBS", tenant_id=event["tenant_id"], job_id=event["job_id"])
    if not job or job["status"] != "PENDING_MODERATION":
        return {"skip": True}
    key, version = job["video_key"], job["upload_version"]
    source = {"Bucket": os.environ["QUARANTINE"], "Key": key, "VersionId": version}
    if not 0 < s3.head_object(**source)["ContentLength"] <= MAX_VIDEO_BYTES:
        return {"media_valid": False}
    tags = s3.get_object_tagging(**source)["TagSet"]
    malware = next((tag["Value"] for tag in tags if tag["Key"] == "GuardDutyMalwareScanStatus"), None)
    if malware != "NO_THREATS_FOUND":
        return {"malware": malware, "pending": malware is None and time.time() < int(job["created_at"]) + 300}
    rekognition = boto3.client("rekognition")
    with tempfile.TemporaryDirectory() as folder:
        video = Path(folder) / "video.mp4"
        s3.download_file(source["Bucket"], key, str(video), ExtraArgs={"VersionId": version})
        with video.open("rb") as stream:
            header = stream.read(32)
        if header[4:8] != b"ftyp":
            return {"media_valid": False}
        try:
            probe = subprocess.run(["/opt/bin/ffprobe", "-v", "error", "-show_format", "-show_streams",
                                    "-of", "json", str(video)], check=True, capture_output=True, timeout=20)
            metadata = json.loads(probe.stdout)
            duration = float(metadata["format"]["duration"])
            stream = next(s for s in metadata["streams"] if s["codec_type"] == "video")
            if not 0 < duration <= int(os.environ["MAX_VIDEO_SECONDS"]) or not (
                    0 < int(stream["width"]) <= 3840 and 0 < int(stream["height"]) <= 2160):
                return {"media_valid": False}
            has_audio = any(s["codec_type"] == "audio" for s in metadata["streams"])
        except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
            return {"media_valid": False}
        scores = [0.0]
        for index in range(1, 7):
            frame = subprocess.run(["/opt/bin/ffmpeg", "-v", "error", "-ss", str(duration * index / 7),
                                    "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2",
                                    "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                   check=True, capture_output=True, timeout=15)
            if not frame.stdout:
                raise ValueError("Missing sampled frame")
            labels = rekognition.detect_moderation_labels(Image={"Bytes": frame.stdout}, MinConfidence=0)
            scores.extend(label["Confidence"] for label in labels["ModerationLabels"])
        # Managed jobs read a private immutable copy of the validated version.
        staged = f"checked/{job['tenant_id']}/{job['user_id']}/{job['job_id']}.mp4"
        s3.copy_object(Bucket=source["Bucket"], Key=staged, CopySource=source, TaggingDirective="REPLACE")
        return {"media_valid": True, "malware": malware, "frame_score": max(scores),
                "staged": staged, "has_audio": has_audio}


def start(event, _):
    report = event["report"]
    if report.get("media_valid") is not True or report["frame_score"] >= block_level():
        return {}
    bucket = os.environ["QUARANTINE"]
    token = event["job_id"]
    result = {"poll_deadline": int(time.time()) + 300}
    if report["has_audio"]:
        result["audio"] = "moderation-" + token
        result["language"] = "language-" + token
        transcribe = boto3.client("transcribe")
        try:
            transcribe.start_transcription_job(
                TranscriptionJobName=result["language"], IdentifyLanguage=True, MediaFormat="mp4",
                Media={"MediaFileUri": f"s3://{bucket}/{report['staged']}"},
                OutputBucketName=bucket, OutputKey=f"toxicity/{result['language']}.json",
                OutputEncryptionKMSKeyId=os.environ["QUARANTINE_KEY"])
        except transcribe.exceptions.ConflictException:
            pass
    if report["frame_score"] >= review_level():
        result["video"] = boto3.client("rekognition").start_content_moderation(
            Video={"S3Object": {"Bucket": bucket, "Name": report["staged"]}},
            ClientRequestToken=token, MinConfidence=review_level())["JobId"]
    return result


def poll(event, _):
    report, jobs = dict(event["report"]), event["jobs"]
    if not jobs or time.time() >= jobs["poll_deadline"]:
        return report
    if "audio" in jobs:
        audio_result = poll_audio(report, jobs)
        if audio_result.get("pending"):
            return {**report, "pending": True}
        report.update(audio_result)
    else:
        report["audio_toxicity"] = 0
    if "video" in jobs:
        rekognition = boto3.client("rekognition")
        result = rekognition.get_content_moderation(JobId=jobs["video"])
        if result["JobStatus"] == "IN_PROGRESS":
            return {**report, "pending": True}
        if result["JobStatus"] == "SUCCEEDED":
            scores = [0]
            while True:
                scores.extend(row["ModerationLabel"]["Confidence"] for row in result["ModerationLabels"])
                if not result.get("NextToken"):
                    break
                result = rekognition.get_content_moderation(JobId=jobs["video"], NextToken=result["NextToken"])
            report["video_score"] = max(scores)
    return report


def poll_audio(report, jobs):
    transcribe = boto3.client("transcribe")
    language = transcribe.get_transcription_job(TranscriptionJobName=jobs["language"])["TranscriptionJob"]
    if language["TranscriptionJobStatus"] in ("QUEUED", "IN_PROGRESS"):
        return {"pending": True}
    if (language["TranscriptionJobStatus"] != "COMPLETED" or language.get("LanguageCode") != "en-US"
            or language.get("IdentifiedLanguageScore", 0) < 0.8):
        return {}
    try:
        transcribe.start_transcription_job(
            TranscriptionJobName=jobs["audio"], LanguageCode="en-US", MediaFormat="mp4",
            Media={"MediaFileUri": f"s3://{os.environ['QUARANTINE']}/{report['staged']}"},
            OutputBucketName=os.environ["QUARANTINE"], OutputKey=f"toxicity/{jobs['audio']}.json",
            OutputEncryptionKMSKeyId=os.environ["QUARANTINE_KEY"],
            ToxicityDetection=[{"ToxicityCategories": ["ALL"]}])
    except transcribe.exceptions.ConflictException:
        pass
    audio = transcribe.get_transcription_job(TranscriptionJobName=jobs["audio"])["TranscriptionJob"]
    if audio["TranscriptionJobStatus"] in ("QUEUED", "IN_PROGRESS"):
        return {"pending": True}
    if audio["TranscriptionJobStatus"] != "COMPLETED":
        return {}
    response = s3.get_object(Bucket=os.environ["QUARANTINE"], Key=f"toxicity/{jobs['audio']}.json")
    with response["Body"] as body:
        payload = json.load(body)
    segments = payload.get("results", {}).get("toxicity_detection")
    if not isinstance(segments, list) or not segments:
        return {}  # Empty output cannot establish that speech was safe.
    return {"audio_toxicity": max(100 * row["toxicity"] for row in segments)}


def commit_decision(identity, job, outcome, reason, report, source):
    pending = "PENDING_MODERATION" if source == "automated" else "REVIEW"
    committing = "MODERATION_COMMITTING" if source == "automated" else "REVIEW_COMMITTING"
    status = {"PASS": "QUEUED", "REVIEW": "REVIEW", "BLOCK": "BLOCKED"}[outcome]
    serialized = json.dumps(report, allow_nan=False)
    try:
        ddb.update_item(TableName=os.environ["JOBS"], Key=item(identity),
                        UpdateExpression="SET #status = :committing, pending_decision = :decision, "
                        "pending_code = :code, pending_report = :report",
                        ConditionExpression="#status = :pending OR "
                        "(#status = :committing AND pending_decision = :decision AND pending_code = :code)",
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues=item({":committing": committing, ":pending": pending,
                            ":decision": outcome, ":code": reason or "APPROVED", ":report": serialized}))
    except ddb.exceptions.ConditionalCheckFailedException:
        return False
    if outcome == "PASS":
        copy_once(os.environ["VIDEOS"], job["video_key"], job)
    audit(identity["tenant_id"], job["user_id"], identity["job_id"], outcome, reason, source)
    if outcome != "REVIEW":
        cleanup(job, identity, outcome)
    expression = ("SET #status = :next, moderation_decision = :decision, moderation_code = :code, "
                  "moderation_report = :report")
    values = {":next": status, ":decision": outcome, ":code": reason or "APPROVED",
              ":committing": committing, ":report": serialized}
    if outcome == "REVIEW":
        expression += ", review_tenant = :tenant"
        values[":tenant"] = identity["tenant_id"]
    else:
        expression += " REMOVE review_tenant"
    try:
        ddb.update_item(TableName=os.environ["JOBS"], Key=item(identity),
                        UpdateExpression=expression, ConditionExpression="#status = :committing",
                        ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues=item(values))
    except ddb.exceptions.ConditionalCheckFailedException:
        pass  # Another retry completed the same immutable decision.
    return True


def delete_versions(bucket, key):
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=key):
        objects = [{"Key": row["Key"], "VersionId": row["VersionId"]}
                   for kind in ("Versions", "DeleteMarkers") for row in page.get(kind, []) if row["Key"] == key]
        for start in range(0, len(objects), 1000):
            result = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects[start:start + 1000]})
            if result.get("Errors"):
                raise RuntimeError("Quarantine cleanup failed")


def copy_once(bucket, key, job):
    fingerprint = hashlib.sha256((job["video_key"] + ":" + job["upload_version"]).encode()).hexdigest()
    try:
        existing = s3.head_object(Bucket=bucket, Key=key)
        if existing.get("Metadata", {}).get("safety-source") == fingerprint:
            return
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            raise
    s3.copy_object(Bucket=bucket, Key=key, CopySource={
        "Bucket": os.environ["QUARANTINE"], "Key": job["video_key"], "VersionId": job["upload_version"]},
        Metadata={"safety-source": fingerprint}, MetadataDirective="REPLACE", TaggingDirective="REPLACE")


def cleanup(job, identity, outcome):
    bucket = os.environ["QUARANTINE"]
    transcribe = boto3.client("transcribe")
    for prefix in ("moderation-", "language-"):
        try:
            state = transcribe.get_transcription_job(TranscriptionJobName=prefix + identity["job_id"])
            if state["TranscriptionJob"]["TranscriptionJobStatus"] in ("QUEUED", "IN_PROGRESS"):
                raise RuntimeError("Managed scan must finish before terminal erasure")
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("BadRequestException", "NotFoundException"):
                raise
    if outcome == "BLOCK" and int(os.getenv("BLOCK_RETENTION_DAYS", "0")) > 0:
        copy_once(bucket, f"hold/{identity['tenant_id']}/{job['user_id']}/{identity['job_id']}.mp4", job)
    delete_versions(bucket, job["video_key"])
    delete_versions(bucket, f"checked/{identity['tenant_id']}/{job['user_id']}/{identity['job_id']}.mp4")
    delete_versions(bucket, f"toxicity/moderation-{identity['job_id']}.json")
    delete_versions(bucket, f"toxicity/language-{identity['job_id']}.json")
    for prefix in ("moderation-", "language-"):
        try:
            transcribe.delete_transcription_job(TranscriptionJobName=prefix + identity["job_id"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("BadRequestException", "NotFoundException"):
                raise


def finish(event, _):
    identity = {key: event[key] for key in ("tenant_id", "job_id")}
    job = fetch("JOBS", **identity)
    if not job or job["status"] not in ("PENDING_MODERATION", "MODERATION_COMMITTING"):
        return
    report = event.get("assessment", event.get("report", {}))
    if job["status"] == "MODERATION_COMMITTING":
        outcome, reason = job["pending_decision"], job["pending_code"]
        report = json.loads(job["pending_report"])
    else:
        outcome, reason = decision(report, review_level(), block_level())
    if not commit_decision(identity, job, outcome, reason, report, "automated"):
        return
    print(json.dumps({"event_type": "moderation_decision", "decision": outcome, "reason_code": reason,
        "ModerationBlockRate": int(outcome == "BLOCK"), "_aws": {"Timestamp": int(time.time() * 1000),
        "CloudWatchMetrics": [{"Namespace": "VideoSentiment/Moderation", "Dimensions": [[]],
                               "Metrics": [{"Name": "ModerationBlockRate", "Unit": "None"}]}]}}), flush=True)


def review(event, _):
    """Reviewer decisions can reject or retain items for investigation."""
    params = event.get("pathParameters") or {}
    identity = {"tenant_id": params.get("tenant"), "job_id": params.get("jobId")}
    from data_jobs import reply

    tenant = identity["tenant_id"]
    if not isinstance(tenant, str) or not tenant:
        return reply(400, code="INVALID_REVIEW_KEY")
    if identity["job_id"] is None and event["httpMethod"] == "GET":
        options = {"Limit": 25}
        after = (event.get("queryStringParameters") or {}).get("after")
        if after:
            options["ExclusiveStartKey"] = item({"tenant_id": tenant, "review_tenant": tenant, "job_id": after})
        page = ddb.query(TableName=os.environ["JOBS"], IndexName="ByReviewTenant",
                         KeyConditionExpression="review_tenant = :tenant",
                         ExpressionAttributeValues=item({":tenant": tenant}), **options)
        return reply(200, jobs=[row["job_id"]["S"] for row in page["Items"]],
                     next_cursor=page.get("LastEvaluatedKey", {}).get("job_id", {}).get("S"))
    job = fetch("JOBS", **identity)
    if not job or job["status"] not in ("REVIEW", "REVIEW_COMMITTING"):
        return reply(404, code="REVIEW_NOT_FOUND")
    if event["httpMethod"] == "GET":
        preview = s3.generate_presigned_url("get_object", Params={
            "Bucket": os.environ["QUARANTINE"], "Key": job["video_key"],
            "VersionId": job["upload_version"]}, ExpiresIn=300)
        return reply(200, status=job["status"], code=job["moderation_code"], review_url=preview)
    try:
        verdict = json.loads(event.get("body") or "{}")
    except ValueError:
        return reply(400, code="INVALID_REVIEW_DECISION")
    if not isinstance(verdict, dict) or verdict not in ({"decision": "BLOCK"}, {"decision": "PASS"}):
        return reply(400, code="INVALID_REVIEW_DECISION")
    approved = verdict["decision"] == "PASS"
    if approved:
        report = json.loads(job["moderation_report"])
        outcome, reason = decision(report, review_level(), block_level())
        if outcome != "REVIEW" or reason != "CONTENT_REVIEW_REQUIRED":
            return reply(409, code="SCAN_REQUIRED_BEFORE_APPROVAL")
        if job["status"] == "REVIEW":
            try:
                s3.head_object(Bucket=os.environ["QUARANTINE"], Key=job["video_key"], VersionId=job["upload_version"])
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NoSuchVersion", "NotFound"):
                    return reply(410, code="REVIEW_MEDIA_EXPIRED")
                raise
    action = "PASS" if approved else "BLOCK"
    code = "HUMAN_REVIEW_PASS" if approved else "HUMAN_REVIEW_BLOCK"
    if not commit_decision(identity, job, action, code, json.loads(job["moderation_report"]), "reviewer"):
        return reply(409, code="REVIEW_ALREADY_RESOLVED")
    print(json.dumps({"event_type": "moderation_review", "decision": action}), flush=True)
    return reply(200, status="QUEUED" if approved else "BLOCKED", code=code)


def interrupted(event, _):
    execution = boto3.client("stepfunctions").describe_execution(executionArn=event["detail"]["executionArn"])
    identity = json.loads(execution["input"])
    finish({**identity, "assessment": {}}, None)
