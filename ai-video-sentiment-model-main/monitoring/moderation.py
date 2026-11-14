import os

import aws_cdk as cdk
from aws_cdk import (aws_events as events, aws_events_targets as targets, aws_guardduty as gd,
                     aws_iam as iam, aws_lambda as lam, aws_stepfunctions as sfn,
                     aws_sqs as sqs, aws_cloudwatch as cw)


def workflow(scope, data, bucket, environment):
    review = float(os.getenv("MODERATION_REVIEW_SCORE", "65"))
    block = float(os.getenv("MODERATION_BLOCK_SCORE", "90"))
    if not 0 <= review < block <= 100:
        raise ValueError("Moderation scores require 0 <= review < block <= 100")
    layer_arn = os.environ["MODERATION_MEDIA_LAYER_ARN"]
    if layer_arn.split(":")[3] != data.stack.region:
        raise ValueError("Moderation media layer must be in the selected Region")
    scan_role = iam.Role(scope, "MalwareScanRole", assumed_by=iam.ServicePrincipal(
        "malware-protection-plan.guardduty.amazonaws.com", conditions={
            "StringEquals": {"aws:SourceAccount": data.stack.account},
            "ArnLike": {"aws:SourceArn": data.stack.format_arn(
                service="guardduty", resource="malware-protection-plan", resource_name="*")},
        }))
    bucket.grant_read(scan_role, "uploads/*")
    scan_role.add_to_policy(iam.PolicyStatement(
        actions=["s3:PutObjectTagging", "s3:PutObjectVersionTagging",
                 "s3:GetObjectTagging", "s3:GetObjectVersionTagging"],
        resources=[bucket.arn_for_objects("uploads/*")]))
    scan_role.add_to_policy(iam.PolicyStatement(
        actions=["s3:PutBucketNotification", "s3:GetBucketNotification", "s3:ListBucket"],
        resources=[bucket.bucket_arn]))
    bucket.grant_put(scan_role, "malware-protection-resource-validation-object")
    rule = data.stack.format_arn(service="events", resource="rule",
                                 resource_name="DO-NOT-DELETE-AmazonGuardDutyMalwareProtectionS3*")
    scan_role.add_to_policy(iam.PolicyStatement(
        actions=["events:PutRule", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets"],
        resources=[rule], conditions={"StringLike": {"events:ManagedBy": "malware-protection-plan.guardduty.amazonaws.com"}}))
    scan_role.add_to_policy(iam.PolicyStatement(
        actions=["events:DescribeRule", "events:ListTargetsByRule"], resources=[rule]))
    plan = gd.CfnMalwareProtectionPlan(scope, "MalwareProtection",
        role=scan_role.role_arn,
        actions=gd.CfnMalwareProtectionPlan.CFNActionsProperty(
            tagging=gd.CfnMalwareProtectionPlan.CFNTaggingProperty(status="ENABLED")),
        protected_resource=gd.CfnMalwareProtectionPlan.CFNProtectedResourceProperty(
            s3_bucket=gd.CfnMalwareProtectionPlan.S3BucketProperty(
                bucket_name=bucket.bucket_name, object_prefixes=["uploads/"])))
    plan.node.add_dependency(scan_role)
    env = {**environment, "QUARANTINE_KEY": bucket.encryption_key.key_arn,
           "MAX_VIDEO_SECONDS": os.getenv("MAX_VIDEO_SECONDS", "120"),
           "MODERATION_REVIEW_SCORE": os.getenv("MODERATION_REVIEW_SCORE", "65"),
           "MODERATION_BLOCK_SCORE": os.getenv("MODERATION_BLOCK_SCORE", "90"),
           "BLOCK_RETENTION_DAYS": os.getenv("BLOCK_RETENTION_DAYS", "0")}
    handlers = {}
    for name in ("scan", "start", "poll", "finish"):
        handler = data.stack.function("Moderation" + name.title(), "moderate." + name, env,
                                      timeout=180 if name == "scan" else 60, memory=1536 if name == "scan" else 256)
        handlers[name] = handler
    handlers["scan"].add_layers(lam.LayerVersion.from_layer_version_arn(
        scope, "MediaTools", layer_arn))
    data.jobs.grant(handlers["scan"], "dynamodb:GetItem")
    data.jobs.grant(handlers["finish"], "dynamodb:GetItem", "dynamodb:UpdateItem")
    data.grant_safety_access(handlers["finish"])
    for name in ("scan", "finish"):
        bucket.grant_read(handlers[name], "uploads/*")
    bucket.grant_read(handlers["finish"], "checked/*")
    bucket.grant_read(handlers["finish"], "toxicity/*")
    bucket.grant_delete(handlers["finish"])
    handlers["finish"].add_to_role_policy(iam.PolicyStatement(
        actions=["s3:ListBucketVersions"], resources=[bucket.bucket_arn]))
    handlers["finish"].add_to_role_policy(iam.PolicyStatement(
        actions=["transcribe:GetTranscriptionJob", "transcribe:DeleteTranscriptionJob"],
        resources=[data.stack.format_arn(service="transcribe", resource="transcription-job", resource_name=name + "*")
                   for name in ("language-", "moderation-")]))
    bucket.grant_read_write(handlers["finish"], "hold/*")
    handlers["scan"].add_to_role_policy(iam.PolicyStatement(
        actions=["s3:GetObjectVersionTagging"], resources=[bucket.arn_for_objects("uploads/*")]))
    bucket.grant_put(handlers["scan"], "checked/*")
    bucket.grant_read(handlers["start"], "checked/*")
    bucket.grant_put(handlers["start"], "toxicity/*")
    bucket.grant_read(handlers["poll"], "toxicity/*")
    data.videos.grant_put(handlers["finish"], "uploads/*")
    data.videos.grant_read(handlers["finish"], "uploads/*")
    for name, actions in {
        "scan": ["rekognition:DetectModerationLabels"],
        "start": ["rekognition:StartContentModeration", "transcribe:StartTranscriptionJob"],
        "poll": ["rekognition:GetContentModeration", "transcribe:GetTranscriptionJob"],
    }.items():
        handlers[name].add_to_role_policy(iam.PolicyStatement(
            actions=[action for action in actions if action.startswith("rekognition:")],
            resources=["*"], conditions={"StringEquals": {"aws:RequestedRegion": data.stack.region}}))
        transcribe_actions = [action for action in actions if action.startswith("transcribe:")]
        if transcribe_actions:
            handlers[name].add_to_role_policy(iam.PolicyStatement(
                actions=transcribe_actions,
                resources=[data.stack.format_arn(service="transcribe", resource="transcription-job", resource_name=prefix + "*")
                           for prefix in ("language-", "moderation-")]))

    from runtime.moderation_states import definition
    states = definition({name: handler.function_arn for name, handler in handlers.items()})
    machine = sfn.StateMachine(scope, "ModerationWorkflow",
                               definition_body=sfn.DefinitionBody.from_string(data.stack.to_json_string(states)))
    for handler in handlers.values():
        handler.grant_invoke(machine)
    recovery = data.stack.function("ModerationRecovery", "moderate.interrupted", env, timeout=60)
    machine.grant_read(recovery)
    recovery.add_to_role_policy(iam.PolicyStatement(actions=["states:DescribeExecution"],
        resources=[data.stack.format_arn(service="states", resource="execution",
                    resource_name=machine.state_machine_name + ":*",
                    arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)]))
    data.jobs.grant(recovery, "dynamodb:GetItem", "dynamodb:UpdateItem")
    data.grant_safety_access(recovery)
    dead = sqs.Queue(scope, "ModerationRecoveryDlq", encryption=sqs.QueueEncryption.KMS,
                      encryption_master_key=data.key("ModerationRecoveryKey"),
                      retention_period=cdk.Duration.days(14))
    events.Rule(scope, "ModerationInterrupted", event_pattern=events.EventPattern(
        source=["aws.states"], detail_type=["Step Functions Execution Status Change"],
        detail={"stateMachineArn": [machine.state_machine_arn], "status": ["FAILED", "TIMED_OUT", "ABORTED"]}),
        targets=[targets.LambdaFunction(recovery, dead_letter_queue=dead, retry_attempts=10)])
    from aws_cdk import aws_lambda_destinations as destinations
    recovery.configure_async_invoke(on_failure=destinations.SqsDestination(dead), retry_attempts=2)
    recovery_alarm = cw.Alarm(scope, "ModerationRecoveryFailed", metric=dead.metric_approximate_number_of_messages_visible(),
             threshold=1, evaluation_periods=1, treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
             alarm_description=f"Moderation recovery failed. Runbook {data.stack.runbook}#input-quality")
    failed = cw.Alarm(scope, "ModerationExecutionFailed",
                       metric=machine.metric_failed(period=cdk.Duration.minutes(5)),
                       threshold=1, evaluation_periods=1,
                       alarm_description=f"Moderation failed. Runbook {data.stack.runbook}#input-quality")
    from aws_cdk import aws_cloudwatch_actions as actions
    incident = cw.CompositeAlarm(scope, "ModerationIncident", alarm_rule=cw.AlarmRule.any_of(recovery_alarm, failed),
                                  alarm_description=f"Moderation incident. Runbook {data.stack.runbook}#input-quality")
    incident.add_alarm_action(actions.SnsAction(data.stack.alert_topic))
    purge = data.stack.function("SafetyRetention", "safety_store.expire", env, timeout=60)
    data.grant_safety_access(purge)
    events.Rule(scope, "SafetyRetentionSchedule", schedule=events.Schedule.rate(cdk.Duration.days(1)),
                 targets=[targets.LambdaFunction(purge, dead_letter_queue=dead, retry_attempts=2)])
    purge.configure_async_invoke(on_failure=destinations.SqsDestination(dead), retry_attempts=2)
    return machine
