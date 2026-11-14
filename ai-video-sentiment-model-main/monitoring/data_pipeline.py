import os

import aws_cdk as cdk
from aws_cdk import (
    aws_apigateway as api, aws_applicationautoscaling as scaling, aws_cloudwatch as cw,
    aws_events as events, aws_events_targets as targets, aws_iam as iam,
    aws_lambda as lam, aws_lambda_destinations as destinations, aws_lambda_event_sources as sources,
    aws_sagemaker as sm, aws_sns as sns, aws_sns_subscriptions as subscriptions, aws_sqs as sqs,
)
from constructs import Construct


class DataPipeline(Construct):
    def __init__(self, scope, name, data):
        super().__init__(scope, name)
        stack = data.stack
        endpoint_name = os.getenv("ASYNC_ENDPOINT_NAME", stack.endpoint + "-async")
        endpoint_arn = stack.format_arn(service="sagemaker", resource="endpoint", resource_name=endpoint_name)
        artifact = os.environ["ASYNC_MODEL_ARTIFACT_KEY"]
        image = os.environ["ASYNC_MODEL_IMAGE_URI"]
        env = {
            "JOBS": data.jobs.table_name, "KEYS": data.api_keys.table_name,
            "QUOTAS": data.quotas.table_name, "VIDEOS": data.videos.bucket_name,
            "RESULTS": data.results.bucket_name, "ENDPOINT": endpoint_name,
            "CACHE_HOST": data.valkey.attr_endpoint_address,
        }
        quarantine = None
        if os.getenv("ENABLE_MODERATION", "1") == "1":
            from moderation import workflow
            from aws_cdk import aws_s3 as s3
            quarantine = data.bucket("Quarantine", None, cors=True, post=True)
            retention = int(os.getenv("QUARANTINE_RETENTION_DAYS", "2"))
            hold = int(os.getenv("BLOCK_RETENTION_DAYS", "0"))
            if not 1 <= retention <= 30 or not 0 <= hold <= 365:
                raise ValueError("Invalid quarantine or blocked-content retention")
            for prefix in ("uploads/", "checked/", "toxicity/"):
                quarantine.add_lifecycle_rule(prefix=prefix, expiration=cdk.Duration.days(retention),
                    noncurrent_version_expiration=cdk.Duration.days(1))
            quarantine.add_lifecycle_rule(prefix="hold/", expiration=cdk.Duration.days(max(1, hold)),
                noncurrent_version_expiration=cdk.Duration.days(1))
            env["QUARANTINE"] = quarantine.bucket_name
            env["BLOCK_RETENTION_DAYS"] = str(hold)
            env["MAX_VIDEO_BYTES"] = os.getenv("MAX_VIDEO_BYTES", str(128 * 1024**2))
            env["SAFETY_CLUSTER_ARN"] = data.safety_cluster.cluster_arn
            env["SAFETY_SECRET_ARN"] = data.safety_secret.secret_arn
            env["STRIKE_THROTTLE_COUNT"] = os.getenv("STRIKE_THROTTLE_COUNT", "3")
            env["STRIKE_SUSPEND_COUNT"] = os.getenv("STRIKE_SUSPEND_COUNT", "5")
            moderation = workflow(self, data, quarantine, env)
            env["MODERATION_WORKFLOW"] = moderation.state_machine_arn
        queue_key = data.key("JobQueueKey")
        dlq = sqs.Queue(self, "DeadLetters", encryption=sqs.QueueEncryption.KMS,
                        encryption_master_key=queue_key, retention_period=cdk.Duration.days(14))
        queue = sqs.Queue(self, "SubmitQueue", encryption=sqs.QueueEncryption.KMS,
                          encryption_master_key=queue_key, visibility_timeout=cdk.Duration.minutes(6),
                          dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=5))
        notifications = sns.Topic(self, "CompletionNotifications", master_key=data.key("NotificationKey"))
        result_bus = events.EventBus(self, "ModelResults")
        env.update({"QUEUE": queue.queue_url, "NOTIFICATIONS": notifications.topic_arn,
                    "RESULT_BUS": result_bus.event_bus_name})
        handler = stack.function("DataJobsApi", "data_jobs.api", env, timeout=15)
        if quarantine:
            data.grant_safety_access(handler)
            self.vpc_data_api(data)
        # The public API validates an opaque product key. Rate limits apply at
        # the gateway and, after authentication, per tenant in the handler.
        handler.add_to_role_policy(iam.PolicyStatement(actions=[
            "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces",
            "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses",
            "ec2:UnassignPrivateIpAddresses", "ec2:DescribeSubnets",
        ], resources=["*"]))
        handler.node.default_child.vpc_config = lam.CfnFunction.VpcConfigProperty(
            security_group_ids=[data.app_sg.security_group_id],
            subnet_ids=[subnet.subnet_id for subnet in data.vpc.isolated_subnets])
        data.api_keys.grant_read_data(handler)
        data.jobs.grant(handler, "dynamodb:GetItem", "dynamodb:PutItem")
        data.quotas.grant(handler, "dynamodb:GetItem", "dynamodb:UpdateItem")
        upload_bucket = quarantine or data.videos
        upload_bucket.grant_put(handler, "uploads/*")
        upload_bucket.grant_read(handler, "uploads/*")
        data.results.grant_read(handler, "output/*")
        rest = api.RestApi(self, "JobsApi", deploy_options=api.StageOptions(
            throttling_rate_limit=100, throttling_burst_limit=200, data_trace_enabled=False))
        jobs = rest.root.add_resource("jobs")
        for path, method in ((rest.root.add_resource("uploads"), "POST"), (jobs, "POST"),
                             (rest.root.add_resource("quota"), "GET"),
                             (jobs.add_resource("{jobId}"), "GET")):
            path.add_method(method, api.LambdaIntegration(handler), authorization_type=api.AuthorizationType.IAM)
        if quarantine:
            reviewer = stack.function("ModerationReviewer", "moderate.review", {
                **env, "MODERATION_REVIEW_SCORE": os.getenv("MODERATION_REVIEW_SCORE", "65"),
                "MODERATION_BLOCK_SCORE": os.getenv("MODERATION_BLOCK_SCORE", "90")})
            data.jobs.grant(reviewer, "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query")
            data.grant_safety_access(reviewer)
            quarantine.grant_read(reviewer, "uploads/*")
            data.videos.grant_put(reviewer, "uploads/*")
            data.videos.grant_read(reviewer, "uploads/*")
            quarantine.grant_read_write(reviewer, "hold/*")
            quarantine.grant_delete(reviewer)
            reviewer.add_to_role_policy(iam.PolicyStatement(
                actions=["s3:ListBucketVersions"], resources=[quarantine.bucket_arn]))
            reviewer.add_to_role_policy(iam.PolicyStatement(
                actions=["transcribe:GetTranscriptionJob", "transcribe:DeleteTranscriptionJob"],
                resources=[stack.format_arn(service="transcribe", resource="transcription-job", resource_name=name + "*")
                           for name in ("language-", "moderation-")]))
            review_queue = rest.root.add_resource("review").add_resource("{tenant}")
            review_queue.add_method("GET", api.LambdaIntegration(reviewer), authorization_type=api.AuthorizationType.IAM)
            review = review_queue.add_resource("{jobId}")
            for verb in ("GET", "POST"):
                review.add_method(verb, api.LambdaIntegration(reviewer), authorization_type=api.AuthorizationType.IAM)
            reviewer_arn = os.environ["MODERATION_REVIEW_ROLE_ARN"]
            if reviewer_arn == os.environ["SAAS_ROLE_ARN"]:
                raise ValueError("Reviewer and application roles must be different")
            review_role = iam.Role.from_role_arn(self, "ReviewCaller", reviewer_arn, mutable=True)
            review_role.add_to_principal_policy(iam.PolicyStatement(
                actions=["execute-api:Invoke"], resources=[rest.arn_for_execute_api(method="*", path="/review/*")]))
            rest.add_to_resource_policy(iam.PolicyStatement(
                effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()], actions=["execute-api:Invoke"],
                resources=["execute-api:/*/*/review/*"],
                conditions={"ArnNotEquals": {"aws:PrincipalArn": reviewer_arn}}))
        data.output("JobsApiUrl", rest.url)
        data.output("NotificationTopic", notifications.topic_arn)
        saas = iam.Role.from_role_arn(self, "SaasCaller", os.environ["SAAS_ROLE_ARN"], mutable=True)
        for method, path in (("POST", "/uploads"), ("POST", "/jobs"), ("GET", "/jobs/*"), ("GET", "/quota")):
            saas.add_to_principal_policy(iam.PolicyStatement(
                actions=["execute-api:Invoke"], resources=[rest.arn_for_execute_api(method=method, path=path)]))
        data.api_keys.grant(saas, "dynamodb:PutItem", "dynamodb:GetItem")

        dispatch = stack.function("DataDispatch", "data_jobs.dispatch", env)
        dispatch.add_event_source(sources.DynamoEventSource(
            data.jobs, starting_position=lam.StartingPosition.TRIM_HORIZON,
            batch_size=10, retry_attempts=5, bisect_batch_on_error=True,
            on_failure=sources.SqsDlq(dlq),
            filters=[lam.FilterCriteria.filter({"eventName": ["INSERT", "MODIFY"], "dynamodb": {
                "NewImage": {"status": {"S": ["QUEUED", "PENDING_MODERATION"] if quarantine else ["QUEUED"]}}}})],
        ))
        queue.grant_send_messages(dispatch)
        if quarantine:
            moderation.grant_start_execution(dispatch)
        worker = stack.function("DataWorker", "data_jobs.submit", env, timeout=60)
        if quarantine:
            data.grant_safety_access(worker)
        worker.add_event_source(sources.SqsEventSource(queue, batch_size=1))
        data.jobs.grant(worker, "dynamodb:GetItem", "dynamodb:UpdateItem")
        data.videos.grant_put(worker, "requests/*")
        worker.add_to_role_policy(iam.PolicyStatement(actions=["sagemaker:InvokeEndpointAsync"],
                                                      resources=[endpoint_arn]))

        finalizer = stack.function("DataCompletion", "data_jobs.complete", env)
        data.jobs.grant(finalizer, "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:GetItem")
        notifications.grant_publish(finalizer)
        events.Rule(self, "ResultWritten", event_bus=result_bus, event_pattern=events.EventPattern(
            source=["aspect.sagemaker"], detail_type=["Inference result"]),
            targets=[targets.LambdaFunction(finalizer, dead_letter_queue=dlq, retry_attempts=10)])
        model_events = sns.Topic(self, "ModelEvents", enforce_ssl=True)
        bridge = stack.function("DataResultBridge", "data_jobs.result_event", env)
        result_bus.grant_put_events_to(bridge)
        model_events.add_subscription(subscriptions.LambdaSubscription(bridge, dead_letter_queue=dlq))
        bridge.configure_async_invoke(on_failure=destinations.SqsDestination(dlq), retry_attempts=2)
        finalizer.configure_async_invoke(on_failure=destinations.SqsDestination(dlq), retry_attempts=2)

        execution = iam.Role(self, "ModelExecution", assumed_by=iam.ServicePrincipal(
            "sagemaker.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": stack.account}}))
        data.artifacts.grant_read(execution, artifact)
        data.videos.grant_read(execution, "uploads/*")
        data.videos.grant_read(execution, "requests/*")
        data.results.grant_put(execution, "output/*")
        data.results.grant_put(execution, "failure/*")
        model_events.grant_publish(execution)
        execution.add_to_policy(iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        repository = image.split("/", 1)[1].split("@")[0].split(":")[0]
        execution.add_to_policy(iam.PolicyStatement(
            actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
            resources=[stack.format_arn(service="ecr", account=image.split(".", 1)[0],
                                        resource="repository", resource_name=repository)]))
        log_arn = stack.format_arn(service="logs", resource="log-group",
                                   resource_name=f"/aws/sagemaker/Endpoints/{endpoint_name}",
                                   arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)
        execution.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
            resources=[log_arn, log_arn + ":*"]))
        data.output("AsyncModelExecutionRoleArn", execution.role_arn)
        # Stage the artifact into the generated bucket before enabling creation.
        if os.getenv("CREATE_ASYNC_ENDPOINT") == "1":
            if os.getenv("REQUIRE_QUALITY_POLICY", "1") == "1" and not os.getenv("QUALITY_POLICY_PATH"):
                raise ValueError("Async serving requires a calibrated QUALITY_POLICY_PATH")
            model = sm.CfnModel(self, "Model", execution_role_arn=execution.role_arn,
                primary_container=sm.CfnModel.ContainerDefinitionProperty(
                    image=image, model_data_url=data.artifacts.s3_url_for_object(artifact),
                    environment={"INFERENCE_BUCKET": data.videos.bucket_name,
                                 "SAGEMAKER_PROGRAM": "inference.py",
                                 "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/model/code",
                                 "PIP_CONSTRAINT": "/opt/ml/model/code/build-constraints.txt",
                                 "PIP_BUILD_CONSTRAINT": "/opt/ml/model/code/build-constraints.txt",
                                 "INFERENCE_INPUT_PREFIX": "uploads/",
                                 "QUALITY_POLICY_PATH": os.getenv("QUALITY_POLICY_PATH", ""),
                                 "REQUIRE_QUALITY_POLICY": os.getenv("REQUIRE_QUALITY_POLICY", "1"),
                                 "ENDPOINT_NAME": endpoint_name, "MODEL_VERSION": stack.version}))
            config = sm.CfnEndpointConfig(self, "Config",
                production_variants=[sm.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic", model_name=model.attr_model_name, initial_instance_count=1,
                    instance_type=os.getenv("ASYNC_INSTANCE_TYPE", "ml.g5.xlarge"), initial_variant_weight=1)],
                async_inference_config=sm.CfnEndpointConfig.AsyncInferenceConfigProperty(
                    client_config=sm.CfnEndpointConfig.AsyncInferenceClientConfigProperty(
                        max_concurrent_invocations_per_instance=1),
                    output_config=sm.CfnEndpointConfig.AsyncInferenceOutputConfigProperty(
                        s3_output_path=data.results.s3_url_for_object("output"),
                        s3_failure_path=data.results.s3_url_for_object("failure"),
                        notification_config=sm.CfnEndpointConfig.AsyncInferenceNotificationConfigProperty(
                            success_topic=model_events.topic_arn, error_topic=model_events.topic_arn),
                        kms_key_id=data.results.encryption_key.key_arn)))
            endpoint = sm.CfnEndpoint(self, "Endpoint", endpoint_name=endpoint_name,
                                      endpoint_config_name=config.attr_endpoint_config_name)
            min_instances = int(os.getenv("ASYNC_MIN_INSTANCES", "0"))
            max_instances = int(os.getenv("ASYNC_MAX_INSTANCES", "1"))
            if not 0 <= min_instances <= max_instances or max_instances < 1:
                raise ValueError("Async scaling requires 0 <= min <= max and max >= 1")
            target = scaling.ScalableTarget(
                self, "AsyncCapacity", service_namespace=scaling.ServiceNamespace.SAGEMAKER,
                scalable_dimension="sagemaker:variant:DesiredInstanceCount",
                resource_id=f"endpoint/{endpoint_name}/variant/AllTraffic",
                min_capacity=min_instances, max_capacity=max_instances)
            target.node.add_dependency(endpoint)
            target.scale_to_track_metric(
                "BacklogPerInstance", target_value=5,
                custom_metric=cw.Metric(namespace="AWS/SageMaker", metric_name="ApproximateBacklogSizePerInstance",
                                        dimensions_map={"EndpointName": endpoint_name}),
                scale_in_cooldown=cdk.Duration.minutes(10), scale_out_cooldown=cdk.Duration.minutes(2))
            if min_instances == 0:
                target.scale_on_metric(
                    "WakeFromZero",
                    metric=cw.Metric(namespace="AWS/SageMaker", metric_name="HasBacklogWithoutCapacity",
                                     dimensions_map={"EndpointName": endpoint_name},
                                     statistic="Average", period=cdk.Duration.minutes(1)),
                    adjustment_type=scaling.AdjustmentType.CHANGE_IN_CAPACITY,
                    scaling_steps=[scaling.ScalingInterval(upper=1, change=0),
                                   scaling.ScalingInterval(lower=1, change=1)],
                    evaluation_periods=2, datapoints_to_alarm=2,
                    cooldown=cdk.Duration.minutes(5))
        cw.Alarm(self, "QueueDeadLetters", metric=dlq.metric_approximate_number_of_messages_visible(),
                 threshold=1, evaluation_periods=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                 alarm_description=f"Async queue dead letters. Runbook {stack.runbook}#async-recovery")

    def vpc_data_api(self, data):
        from aws_cdk import aws_ec2 as ec2
        endpoint_sg = ec2.SecurityGroup(self, "DataApiEndpointSecurityGroup", vpc=data.vpc)
        endpoint_sg.add_ingress_rule(data.app_sg, ec2.Port.tcp(443))
        data.vpc.add_interface_endpoint("SafetyDataApi", service=ec2.InterfaceVpcEndpointAwsService.RDS_DATA,
                                         security_groups=[endpoint_sg])
