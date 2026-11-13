import os
import re
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_applicationautoscaling as scaling, aws_athena as athena, aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions, aws_dynamodb as ddb, aws_events as events,
    aws_events_targets as targets, aws_glue as glue, aws_iam as iam, aws_lambda as lam,
    aws_kinesisfirehose as firehose, aws_logs as logs, aws_s3 as s3, aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
)


def integer(name, default):
    return int(os.getenv(name, str(default)))


def numeric(name, default):
    return float(os.getenv(name, str(default)))


class Monitoring(cdk.Stack):
    def __init__(self, scope, name, **kwargs):
        super().__init__(scope, name, **kwargs)
        self.endpoint = os.environ["ENDPOINT_NAME"]
        self.version = os.environ["MODEL_VERSION"]
        self.runbook = os.environ["RUNBOOK_URL"].rstrip("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", self.version):
            raise ValueError("MODEL_VERSION must be a short alphanumeric identifier")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", self.endpoint):
            raise ValueError("ENDPOINT_NAME must be a SageMaker endpoint name")
        if not self.runbook.startswith("https://"):
            raise ValueError("RUNBOOK_URL must be an HTTPS link to the published runbook")
        self.bucket = s3.Bucket(
            self, "MonitoringData", encryption=s3.BucketEncryption.S3_MANAGED, versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(prefix=prefix, expiration=cdk.Duration.days(days),
                                 noncurrent_version_expiration=cdk.Duration.days(7),
                                 abort_incomplete_multipart_upload_after=cdk.Duration.days(7))
                for prefix, days in (("events/", integer("EVENT_RETENTION_DAYS", 30)),
                                     ("capture/", integer("CAPTURE_RETENTION_DAYS", 7)),
                                     ("firehose-errors/", 14), ("athena-results/", 7))
            ],
        )
        self.inference_bucket = os.environ["INFERENCE_BUCKET"]
        self.input_prefix = os.getenv("INFERENCE_INPUT_PREFIX", "inference/uploads/")
        self.model_prefix = os.getenv("MODEL_ARTIFACT_PREFIX", "inference/artifacts/")
        for prefix in (self.input_prefix, self.model_prefix):
            if not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)+", prefix):
                raise ValueError("S3 prefixes must be relative and end with a slash")
        if self.input_prefix.startswith(self.model_prefix) or self.model_prefix.startswith(self.input_prefix):
            raise ValueError("Uploads and model artifacts must have disjoint S3 prefixes")
        self.execution_role()
        self.delivery()
        self.analytics()
        self.jobs()
        self.scaling()
        self.alarms()
        self.dashboard()
        if os.getenv("ENABLE_DATA_ARCHITECTURE") == "1":
            from data_architecture import DataArchitecture
            DataArchitecture(self, "DataArchitecture")
        cdk.CfnOutput(self, "MonitoringBucket", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "CaptureUri", value=self.bucket.s3_url_for_object("capture"))

    def execution_role(self):
        data = s3.Bucket.from_bucket_name(self, "InferenceData", self.inference_bucket)
        role = iam.Role(self, "EndpointRole", assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com",
            conditions={"StringEquals": {"aws:SourceAccount": self.account}}))
        role.add_to_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[
            data.arn_for_objects(self.input_prefix + "*"), data.arn_for_objects(self.model_prefix + "*")]))
        role.add_to_policy(iam.PolicyStatement(actions=["s3:PutObject", "s3:AbortMultipartUpload"],
                                               resources=[self.bucket.arn_for_objects("capture/*")]))
        role.add_to_policy(iam.PolicyStatement(actions=["s3:GetBucketLocation"],
                                               resources=[data.bucket_arn, self.bucket.bucket_arn]))
        role.add_to_policy(iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        image_owner = os.getenv("DLC_ACCOUNT", "763104351884")
        role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
            resources=[self.format_arn(service="ecr", account=image_owner, resource="repository",
                                       resource_name="pytorch-inference")]))
        log_group = f"/aws/sagemaker/Endpoints/{self.endpoint}"
        log_arn = self.format_arn(service="logs", resource="log-group", resource_name=log_group,
                                  arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)
        role.add_to_policy(iam.PolicyStatement(actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"],
                                               resources=[log_arn, log_arn + ":*"]))
        role.add_to_policy(iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                               resources=[log_arn + ":log-stream:*"]))
        cdk.CfnOutput(self, "ExecutionRoleArn", value=role.role_arn)

    def function(self, name, handler, environment=None, timeout=60, memory=256):
        retention = integer("LOG_RETENTION_DAYS", 30)
        log_group = logs.CfnLogGroup(self, f"{name}Logs", retention_in_days=retention)
        log_group.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
        role = iam.Role(self, f"{name}Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        group_arn = self.format_arn(service="logs", resource="log-group", resource_name=log_group.ref,
                                    arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)
        role.add_to_policy(iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                               resources=[group_arn + ":log-stream:*"]))
        function = lam.Function(
            self, name, runtime=lam.Runtime.PYTHON_3_12, handler=handler,
            code=lam.Code.from_asset(str(Path(__file__).parent / "runtime"), exclude=["__pycache__"]),
            environment=environment or {}, timeout=cdk.Duration.seconds(timeout), memory_size=memory, role=role,
            log_group=logs.LogGroup.from_log_group_name(self, f"{name}LogRef", log_group.ref),
        )
        function.node.add_dependency(role)
        default_policy = role.node.try_find_child("DefaultPolicy")
        if default_policy:
            function.node.add_dependency(default_policy)
        return function

    def delivery(self):
        normalizer = self.function("Normalize", "delivery.handler", timeout=60, memory=512)
        self.delivery_function = normalizer
        role = iam.Role(self, "FirehoseRole", assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"))
        role.add_to_policy(iam.PolicyStatement(actions=["s3:GetBucketLocation", "s3:ListBucket",
            "s3:ListBucketMultipartUploads"], resources=[self.bucket.bucket_arn]))
        role.add_to_policy(iam.PolicyStatement(actions=["s3:AbortMultipartUpload", "s3:GetObject", "s3:PutObject"],
            resources=[self.bucket.arn_for_objects("events/*"), self.bucket.arn_for_objects("firehose-errors/*")]))
        normalizer.grant_invoke(role)
        role.add_to_policy(iam.PolicyStatement(actions=["lambda:GetFunctionConfiguration"],
                                                resources=[normalizer.function_arn, f"{normalizer.function_arn}:*"]))
        delivery_logs = logs.LogGroup(self, "FirehoseLogs", retention=logs.RetentionDays.ONE_MONTH)
        stream_logs = logs.LogStream(self, "FirehoseLogStream", log_group=delivery_logs)
        role.add_to_policy(iam.PolicyStatement(actions=["logs:PutLogEvents"], resources=[
            self.format_arn(service="logs", resource="log-group", resource_name=
                            f"{delivery_logs.log_group_name}:log-stream:{stream_logs.log_stream_name}",
                            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)]))
        stream = firehose.CfnDeliveryStream(self, "InferenceEvents", delivery_stream_type="DirectPut",
            extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=self.bucket.bucket_arn, role_arn=role.role_arn, compression_format="GZIP",
                prefix="events/dt=!{timestamp:yyyy-MM-dd}/hour=!{timestamp:HH}/",
                error_output_prefix="firehose-errors/!{firehose:error-output-type}/!{timestamp:yyyy/MM/dd}/",
                buffering_hints={"intervalInSeconds": 60, "sizeInMBs": 16},
                cloud_watch_logging_options={"enabled": True, "logGroupName": delivery_logs.log_group_name,
                                             "logStreamName": stream_logs.log_stream_name},
                processing_configuration=firehose.CfnDeliveryStream.ProcessingConfigurationProperty(
                    enabled=True, processors=[firehose.CfnDeliveryStream.ProcessorProperty(type="Lambda",
                        parameters=[firehose.CfnDeliveryStream.ProcessorParameterProperty(parameter_name=key,
                                    parameter_value=value) for key, value in (
                            ("LambdaArn", normalizer.function_arn), ("BufferSizeInMBs", "0.2"),
                            ("BufferIntervalInSeconds", "60"), ("NumberOfRetries", "3"))])]),
            ))
        stream.node.add_dependency(role, role.node.find_child("DefaultPolicy"))
        self.delivery_stream = stream
        source_name = f"/aws/sagemaker/Endpoints/{self.endpoint}"
        source_arn = self.format_arn(service="logs", resource="log-group", resource_name=source_name + ":*",
                                     arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)
        sender = iam.Role(self, "LogsDeliveryRole", assumed_by=iam.ServicePrincipal("logs.amazonaws.com",
            conditions={"StringEquals": {"aws:SourceAccount": self.account},
                        "ArnLike": {"aws:SourceArn": source_arn}}))
        sender.add_to_policy(iam.PolicyStatement(actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                                                 resources=[stream.attr_arn]))
        subscription = logs.CfnSubscriptionFilter(self, "InferenceSubscription", log_group_name=source_name,
            destination_arn=stream.attr_arn, role_arn=sender.role_arn, filter_pattern='"sentiment_inference_v1"')
        subscription.node.add_dependency(sender, sender.node.find_child("DefaultPolicy"))
        # The endpoint log group already exists. Set retention without importing its ownership.
        from aws_cdk import custom_resources as cr
        call = cr.AwsSdkCall(service="CloudWatchLogs", action="putRetentionPolicy",
                            parameters={"logGroupName": source_name, "retentionInDays": integer("LOG_RETENTION_DAYS", 30)},
                            physical_resource_id=cr.PhysicalResourceId.of(source_name))
        cr.AwsCustomResource(self, "EndpointLogRetention", on_create=call, on_update=call,
                             install_latest_aws_sdk=False, policy=cr.AwsCustomResourcePolicy.from_statements([
                                 iam.PolicyStatement(actions=["logs:PutRetentionPolicy"], resources=[source_arn])]))

    def analytics(self):
        name = os.getenv("ATHENA_DATABASE", "video_sentiment_monitoring")
        database = glue.CfnDatabase(self, "Database", catalog_id=self.account, database_input={"name": name})
        fields = {
            **dict.fromkeys(("event_type", "endpoint", "model_version", "user_id", "tenant_id", "request_id",
                             "event_id", "emotion_class", "sentiment_class", "error_type"), "string"),
            **dict.fromkeys(("timestamp_ms", "frame_count", "utterance_count", "segment_errors", "errors"), "bigint"),
            **dict.fromkeys(("video_duration", "audio_loudness", "emotion_confidence", "sentiment_confidence",
                             "requestlatency", "frameextractionlatency", "audioextractionlatency",
                             "transcriptiontextlatency", "fusionlatency"), "double"),
            "has_speech": "boolean", "drift_sample": "boolean",
        }
        table = glue.CfnTable(self, "InferenceTable", catalog_id=self.account, database_name=name, table_input={
            "name": "inference_events", "tableType": "EXTERNAL_TABLE",
            "partitionKeys": [{"name": "dt", "type": "string"}, {"name": "hour", "type": "string"}],
            "parameters": {"EXTERNAL": "TRUE", "projection.enabled": "true", "projection.dt.type": "date",
                           "projection.dt.format": "yyyy-MM-dd", "projection.dt.range":
                           f"NOW-{integer('EVENT_RETENTION_DAYS', 30) + 5}DAYS,NOW",
                           "projection.dt.interval": "1", "projection.dt.interval.unit": "DAYS",
                           "projection.hour.type": "integer", "projection.hour.range": "0,23",
                           "projection.hour.digits": "2", "storage.location.template":
                           self.bucket.s3_url_for_object("events/dt=${dt}/hour=${hour}/")},
            "storageDescriptor": {"location": self.bucket.s3_url_for_object("events/"),
                "columns": [{"name": column, "type": kind} for column, kind in fields.items()],
                "inputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                "outputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                "serdeInfo": {"serializationLibrary": "org.openx.data.jsonserde.JsonSerDe"}},
        })
        table.add_resource_dependency(database)
        athena.CfnWorkGroup(self, "AthenaWorkgroup", name=os.getenv("ATHENA_WORKGROUP", "video-sentiment-monitoring"),
            work_group_configuration={"enforceWorkGroupConfiguration": True,
                "bytesScannedCutoffPerQuery": integer("ATHENA_MAX_SCAN_BYTES", 10_000_000_000),
                "resultConfiguration": {"outputLocation": self.bucket.s3_url_for_object("athena-results/"),
                                        "encryptionConfiguration": {"encryptionOption": "SSE_S3"}}})

    def jobs(self):
        common = {"ENDPOINT": self.endpoint, "MODEL_VERSION": self.version}
        drift = self.function("Drift", "drift.handler", {**common, "BUCKET": self.bucket.bucket_name,
            "MIN_SAMPLES": str(integer("DRIFT_MIN_SAMPLES", 100)),
            "PSI_THRESHOLD": str(numeric("PSI_THRESHOLD", .25)),
            "EMBEDDING_SHIFT_THRESHOLD": str(numeric("EMBEDDING_SHIFT_THRESHOLD", .5))},
            timeout=600, memory=1024)
        drift.add_to_role_policy(iam.PolicyStatement(actions=["s3:ListBucket"], resources=[self.bucket.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["events/*"]}}))
        drift.add_to_role_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[
            self.bucket.arn_for_objects("events/*"), self.bucket.arn_for_objects(f"baselines/{self.version}.json")]))
        feedback_name = os.getenv("FEEDBACK_TABLE_NAME", f"{self.stack_name}-feedback")
        feedback = ddb.Table(self, "Feedback", table_name=feedback_name,
            partition_key=ddb.Attribute(name="id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST, time_to_live_attribute="expires",
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            removal_policy=cdk.RemovalPolicy.RETAIN)
        saas_role = iam.Role.from_role_arn(self, "SaasRole", os.environ["SAAS_ROLE_ARN"], mutable=True)
        saas_role.add_to_principal_policy(iam.PolicyStatement(actions=["dynamodb:PutItem"],
            resources=[feedback.table_arn], conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["feedback#*"]}}))
        saas_role.add_to_principal_policy(iam.PolicyStatement(actions=["dynamodb:UpdateItem"],
            resources=[feedback.table_arn], conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["acc#*"]}}))
        saas_role.add_to_principal_policy(iam.PolicyStatement(actions=["sagemaker:InvokeEndpoint"],
            resources=[self.format_arn(service="sagemaker", resource="endpoint", resource_name=self.endpoint)]))
        data = s3.Bucket.from_bucket_name(self, "SaasInputData", self.inference_bucket)
        saas_role.add_to_principal_policy(iam.PolicyStatement(actions=["s3:PutObject"],
            resources=[data.arn_for_objects(self.input_prefix + "*")]))
        accuracy = self.function("Accuracy", "accuracy.handler", {**common, "FEEDBACK_TABLE": feedback.table_name,
            "ACCURACY_WINDOW_HOURS": str(integer("ACCURACY_WINDOW_HOURS", 168))}, timeout=600)
        accuracy.add_to_role_policy(iam.PolicyStatement(actions=["dynamodb:BatchGetItem"],
            resources=[feedback.table_arn], conditions={"ForAllValues:StringLike": {
                "dynamodb:LeadingKeys": [f"acc#{self.version}#{self.endpoint}#*"]}}))
        for label, function, minute in (("Drift", drift, "12"), ("Accuracy", accuracy, "20")):
            events.Rule(self, f"{label}Schedule", schedule=events.Schedule.cron(minute=minute, hour="*"),
                        enabled=os.getenv("ENABLE_MONITORING_JOBS", "false").lower() == "true",
                        targets=[targets.LambdaFunction(function, retry_attempts=2)])
        self.jobs_to_watch = (drift, accuracy)
        cdk.CfnOutput(self, "FeedbackTable", value=feedback.table_name)

    def scaling(self):
        if os.getenv("ENABLE_AUTOSCALING", "false").lower() != "true":
            return
        minimum, maximum = integer("MIN_CAPACITY", 1), integer("MAX_CAPACITY", 2)
        rate = numeric("INVOCATIONS_PER_INSTANCE", 4)
        if not 1 <= minimum <= maximum or rate <= 0:
            raise ValueError("Autoscaling requires positive capacity bounds and invocation target")
        target = scaling.ScalableTarget(self, "Capacity", service_namespace=scaling.ServiceNamespace.SAGEMAKER,
            scalable_dimension="sagemaker:variant:DesiredInstanceCount",
            resource_id=f"endpoint/{self.endpoint}/variant/AllTraffic",
            min_capacity=minimum, max_capacity=maximum)
        target.scale_to_track_metric("InvocationsPerInstance", target_value=rate,
            predefined_metric=scaling.PredefinedMetric.SAGEMAKER_VARIANT_INVOCATIONS_PER_INSTANCE,
            scale_in_cooldown=cdk.Duration.seconds(integer("SCALE_IN_COOLDOWN", 600)),
            scale_out_cooldown=cdk.Duration.seconds(integer("SCALE_OUT_COOLDOWN", 120)))

    def aws_metric(self, name, statistic="Sum", period=5, namespace="AWS/SageMaker"):
        return cw.Metric(namespace=namespace, metric_name=name, statistic=statistic, period=cdk.Duration.minutes(period),
                         dimensions_map={"EndpointName": self.endpoint, "VariantName": "AllTraffic"})

    def custom(self, name, namespace="VideoSentiment/Monitoring", statistic="Average", period=60, extra=None):
        return cw.Metric(namespace=namespace, metric_name=name, statistic=statistic, period=cdk.Duration.minutes(period),
                         dimensions_map={"endpoint": self.endpoint, "model_version": self.version, **(extra or {})})

    def alarms(self):
        topic = sns.Topic(self, "Alerts")
        if os.getenv("ALERT_EMAIL"):
            topic.add_subscription(subscriptions.EmailSubscription(os.environ["ALERT_EMAIL"]))
        action = cw_actions.SnsAction(topic)
        self.error_rate = cw.MathExpression(expression="IF(m1>=10,100*FILL(m2,0)/m1,0)",
            using_metrics={"m1": self.aws_metric("Invocations"), "m2": self.aws_metric("Invocation5XXErrors")},
            period=cdk.Duration.minutes(5), label="5XX rate (%)")
        self.throttles = self.custom("ThrottledRequests", "VideoSentiment/SaaS", "Sum", 5)
        self.latencies = [self.aws_metric("ModelLatency", p) for p in ("p95", "p99")]
        self.gpu = self.aws_metric("GPUMemoryUtilization", "Maximum", namespace="/aws/sagemaker/Endpoints")
        self.gpu_compute = self.aws_metric("GPUUtilization", "Maximum", namespace="/aws/sagemaker/Endpoints")
        self.quality = [self.custom(n) for n in ("DriftScore", "ConfidenceMean", "NoSpeechRate", "Accuracy")]

        def alarm(label, metric, threshold, section):
            return cw.Alarm(self, label, metric=metric, threshold=threshold,
                alarm_description=f"{label}. Runbook {self.runbook}#{section}",
                evaluation_periods=3, datapoints_to_alarm=2,
                evaluate_low_sample_count_percentile="ignore" if label in ("P95", "P99") else None,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING)

        errors = alarm("Errors5xx", self.error_rate, numeric("ERROR_5XX_PERCENT", 2), "serving")
        throttles = alarm("Throttles", self.throttles, numeric("THROTTLES_PER_5M", 5), "serving")
        p95 = alarm("P95", self.latencies[0], numeric("P95_LATENCY_US", 20_000_000), "serving")
        p99 = alarm("P99", self.latencies[1], numeric("P99_LATENCY_US", 40_000_000), "serving")
        gpu = alarm("GpuMemory", self.gpu, numeric("GPU_MEMORY_PERCENT", 85), "serving")
        drift = alarm("DriftScore", self.quality[0], 1, "drift")
        confidence = alarm("ConfidenceDrop", self.custom("ConfidenceDrop"), numeric("CONFIDENCE_DROP", .08), "drift")
        no_speech = alarm("NoSpeechRate", self.custom("NoSpeechIncrease"), numeric("NO_SPEECH_INCREASE", .20), "drift")
        job_errors = [alarm(f"{label}JobErrors", function.metric_errors(period=cdk.Duration.hours(1)), 1, "pipeline")
                      for label, function in zip(("Drift", "Accuracy"), self.jobs_to_watch)]
        delivery_errors = alarm("NormalizeErrors", self.delivery_function.metric_errors(
            period=cdk.Duration.minutes(5)), 1, "pipeline")
        freshness = alarm("FirehoseFreshness", cw.Metric(namespace="AWS/Firehose",
            metric_name="DeliveryToS3.DataFreshness", statistic="Maximum", period=cdk.Duration.minutes(5),
            dimensions_map={"DeliveryStreamName": self.delivery_stream.ref}), 600, "pipeline")
        for label, section, rule in (
            ("Serving", "serving", cw.AlarmRule.any_of(errors, throttles, cw.AlarmRule.all_of(p95, p99),
                                                       cw.AlarmRule.all_of(gpu, p95))),
            ("Quality", "drift", cw.AlarmRule.any_of(drift, confidence, no_speech)),
            ("Pipeline", "pipeline", cw.AlarmRule.any_of(*job_errors, delivery_errors, freshness)),
        ):
            composite = cw.CompositeAlarm(self, label, alarm_rule=rule,
                alarm_description=f"{label} incident. Runbook {self.runbook}#{section}")
            composite.add_alarm_action(action)

    def dashboard(self):
        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name=f"{self.endpoint}-monitoring")
        dashboard.add_widgets(
            cw.GraphWidget(title="Traffic", left=[self.aws_metric("Invocations"), self.throttles]),
            cw.GraphWidget(title="Latency (microseconds)", left=self.latencies),
            cw.GraphWidget(title="Errors and GPU usage (%)", left=[self.error_rate], right=[self.gpu, self.gpu_compute]),
        )
        for label, classes in (("Emotion", ("anger", "disgust", "fear", "joy", "neutral", "sadness", "surprise")),
                               ("Sentiment", ("negative", "neutral", "positive"))):
            dashboard.add_widgets(cw.GraphWidget(title=f"{label} distribution", width=12,
                left=[self.custom(f"{label}ClassCount", "VideoSentiment/Inference", "Sum", 5,
                                  {f"{label.lower()}_class": value}).with_(label=value) for value in classes]))
        dashboard.add_widgets(cw.GraphWidget(title="Confidence, drift and accuracy", width=18,
            left=[*self.quality, self.custom("SentimentAccuracy"), self.custom("EmotionAccuracy")],
            right=[self.custom("DriftSampleCount"), self.custom("FeedbackCount")]))
        dashboard.add_widgets(cw.GraphWidget(title="Inference stage p95 (milliseconds)", width=18,
            left=[self.custom(metric, "VideoSentiment/Inference", "p95", 5) for metric in (
                "FrameExtractionLatency", "AudioExtractionLatency", "TranscriptionTextLatency", "FusionLatency")]))


app = cdk.App()
Monitoring(app, os.getenv("STACK_NAME", "VideoSentimentMonitoring"),
           env=cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"),
                               region=os.getenv("AWS_REGION", "ap-southeast-2")))
app.synth()
