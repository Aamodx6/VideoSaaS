"""Aurora moderation audit and strike enforcement through the RDS Data API."""

import os
import time

import boto3


def parameters(**values):
    return [{"name": key, "value": {"stringValue": str(value)}} for key, value in values.items()]


def execute(sql, **values):
    return boto3.client("rds-data").execute_statement(
        resourceArn=os.environ["SAFETY_CLUSTER_ARN"],
        secretArn=os.environ["SAFETY_SECRET_ARN"], database="aspect_sentiment",
        sql=sql, parameters=parameters(**values))


def disposition(count, throttle=3, suspend=5):
    if not 1 <= throttle < suspend:
        raise ValueError("Invalid strike thresholds")
    return "suspend" if count >= suspend else "throttle" if count >= throttle else "warn" if count else "active"


def account_policy(tenant, user):
    result = execute(
        "SELECT count(*) FROM aspect.strikes WHERE tenant_id=:tenant AND user_id=:subject AND expires_at>now()",
        tenant=tenant, subject=user)
    count = result["records"][0][0]["longValue"]
    return disposition(count, int(os.getenv("STRIKE_THROTTLE_COUNT", "3")),
                       int(os.getenv("STRIKE_SUSPEND_COUNT", "5")))


def audit(tenant, user, job_id, outcome, reason, source="automated"):
    # The primary key makes service retries and repeated reviewer calls idempotent.
    execute("""INSERT INTO aspect.moderation_events
        (tenant_id,user_id,job_id,source,decision,reason_code,expires_at)
        VALUES (:tenant,:subject,:job,:source,:outcome,:reason,
                to_timestamp(CAST(:expires AS bigint)))
        ON CONFLICT (tenant_id,user_id,job_id,source) DO NOTHING""",
        tenant=tenant, subject=user, job=job_id, source=source, outcome=outcome,
        reason=reason or "APPROVED", expires=int(time.time()) + 365 * 86400)
    if outcome == "BLOCK" and reason in ("CONTENT_POLICY", "MALWARE_DETECTED", "HUMAN_REVIEW_BLOCK"):
        execute("""INSERT INTO aspect.strikes(tenant_id,user_id,job_id,reason_code,expires_at)
            VALUES (:tenant,:subject,:job,:reason,to_timestamp(CAST(:expires AS bigint)))
            ON CONFLICT (tenant_id,user_id,job_id) DO NOTHING""",
            tenant=tenant, subject=user, job=job_id, reason=reason,
            expires=int(time.time()) + int(os.getenv("STRIKE_RETENTION_DAYS", "90")) * 86400)


def expire(_event, _context):
    execute("DELETE FROM aspect.strikes WHERE expires_at <= now()")
    execute("DELETE FROM aspect.moderation_events WHERE expires_at <= now()")
