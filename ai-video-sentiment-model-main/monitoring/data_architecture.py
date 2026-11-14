import os
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_athena as athena, aws_dynamodb as ddb, aws_ec2 as ec2,
    aws_elasticache as cache, aws_glue as glue, aws_iam as iam,
    aws_kms as kms, aws_rds as rds, aws_s3 as s3,
)
from constructs import Construct

from data_pipeline import DataPipeline


class DataArchitecture(Construct):
    def __init__(self, stack, name):
        super().__init__(stack, name)
        self.stack = stack
        if stack.region != "ap-southeast-2":
            raise ValueError("The data architecture must use ap-southeast-2")
        self.vpc = ec2.Vpc(self, "Vpc", availability_zones=["ap-southeast-2a", "ap-southeast-2b"],
                           nat_gateways=0, subnet_configuration=[
            ec2.SubnetConfiguration(name="Data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        ])
        self.app_sg = ec2.SecurityGroup(self, "ApplicationSecurityGroup", vpc=self.vpc)
        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
        self.vpc.add_gateway_endpoint("DynamoEndpoint", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB)
        endpoint_sg = ec2.SecurityGroup(self, "EndpointSecurityGroup", vpc=self.vpc)
        endpoint_sg.add_ingress_rule(self.app_sg, ec2.Port.tcp(443))
        for name, service in (
            ("Secrets", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("Sqs", ec2.InterfaceVpcEndpointAwsService.SQS),
            ("SageMakerRuntime", ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME),
        ):
            self.vpc.add_interface_endpoint(name, service=service, security_groups=[endpoint_sg])
        self.videos = self.bucket("Videos", 7, cors=True)
        self.artifacts = self.bucket("Artifacts", None)
        self.capture = self.bucket("Capture", 7)
        self.lake = self.bucket("Lake", 365)
        self.results = self.bucket("InferenceResults", 30, event_bridge=True)
        self.api_keys = self.table("ApiKeys", "key_hash", indexes=[("ByUser", "user_id", "key_hash")])
        self.quotas = self.table("QuotaCounters", "tenant_id", "period")
        self.jobs = self.table("InferenceJobs", "tenant_id", "job_id", [
            ("ByJob", "job_id", None), ("ByUser", "user_id", "job_id"),
        ], stream=True)
        if os.getenv("ENABLE_MODERATION") == "1":
            self.jobs.add_global_secondary_index(
                index_name="ByReviewTenant",
                partition_key=ddb.Attribute(name="review_tenant", type=ddb.AttributeType.STRING),
                sort_key=ddb.Attribute(name="job_id", type=ddb.AttributeType.STRING),
                projection_type=ddb.ProjectionType.KEYS_ONLY)
        self.database()
        self.analytics()
        DataPipeline(self, "Inference", self)

    def output(self, name, value):
        cdk.CfnOutput(self, name, value=value)

    def key(self, name):
        return kms.Key(self, name, enable_key_rotation=True, removal_policy=cdk.RemovalPolicy.RETAIN)

    def bucket(self, name, retention, cors=False, event_bridge=False, post=False):
        rule = s3.LifecycleRule(
            expiration=cdk.Duration.days(retention) if retention else None,
            noncurrent_version_expiration=cdk.Duration.days(7) if retention else None,
            abort_incomplete_multipart_upload_after=cdk.Duration.days(3),
        )
        bucket = s3.Bucket(
            self, name, encryption=s3.BucketEncryption.KMS, encryption_key=self.key(name + "Key"),
            bucket_key_enabled=True, versioned=True, enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=cdk.RemovalPolicy.RETAIN, lifecycle_rules=[rule],
            event_bridge_enabled=event_bridge,
            cors=[s3.CorsRule(allowed_origins=[os.environ["APP_ORIGIN"]],
                             allowed_methods=[s3.HttpMethods.POST if post else s3.HttpMethods.PUT],
                             allowed_headers=["content-type", "if-none-match"])]
            if cors else None,
        )
        self.output(name + "BucketName", bucket.bucket_name)
        return bucket

    def table(self, name, partition, sort=None, indexes=(), stream=False):
        def attribute(key):
            return ddb.Attribute(name=key, type=ddb.AttributeType.STRING) if key else None

        table = ddb.Table(
            self, name, partition_key=attribute(partition), sort_key=attribute(sort),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED, encryption_key=self.key(name + "Key"),
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            time_to_live_attribute="expires_at", deletion_protection=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            stream=ddb.StreamViewType.NEW_IMAGE if stream else None,
        )
        for index, pk, sk in indexes:
            table.add_global_secondary_index(index_name=index, partition_key=attribute(pk),
                                             sort_key=attribute(sk), projection_type=ddb.ProjectionType.KEYS_ONLY)
        self.output(name + "TableName", table.table_name)
        return table

    def database(self):
        proxy_sg = ec2.SecurityGroup(self, "ProxySecurityGroup", vpc=self.vpc)
        db_sg = ec2.SecurityGroup(self, "DatabaseSecurityGroup", vpc=self.vpc)
        proxy_sg.add_ingress_rule(self.app_sg, ec2.Port.tcp(5432))
        db_sg.add_ingress_rule(proxy_sg, ec2.Port.tcp(5432))
        subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)
        cluster = rds.DatabaseCluster(
            self, "Postgres", engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.of(
                    os.getenv("AURORA_POSTGRES_VERSION", "16.15"), "16")),
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            readers=[rds.ClusterInstance.serverless_v2("FailoverReader", scale_with_writer=True)],
            serverless_v2_min_capacity=0.5, serverless_v2_max_capacity=16,
            credentials=rds.Credentials.from_generated_secret("dbadmin", encryption_key=self.key("SecretsKey")),
            default_database_name="aspect_sentiment", vpc=self.vpc, security_groups=[db_sg],
            vpc_subnets=subnets,
            storage_encrypted=True, storage_encryption_key=self.key("PostgresKey"),
            backup=rds.BackupProps(retention=cdk.Duration.days(35)),
            deletion_protection=True, removal_policy=cdk.RemovalPolicy.SNAPSHOT,
            parameters={"rds.force_ssl": "1"},
            enable_data_api=True,
        )
        self.safety_cluster = cluster
        self.safety_secret = rds.DatabaseSecret(self, "SafetySecret", username="aspect_safety",
                                                encryption_key=self.key("SafetySecretKey"))
        rotation_sg = ec2.SecurityGroup(self, "RotationSecurityGroup", vpc=self.vpc)
        db_sg.add_ingress_rule(rotation_sg, ec2.Port.tcp(5432))
        self.vpc.node.find_child("Secrets").connections.allow_from(rotation_sg, ec2.Port.tcp(443))
        cluster.add_rotation_single_user(
            automatically_after=cdk.Duration.days(30),
            security_group=rotation_sg, vpc_subnets=subnets,
        )
        app_secret = rds.DatabaseSecret(self, "ApplicationSecret", username="aspect_app",
                                        encryption_key=self.key("ApplicationSecretKey"))
        proxy = cluster.add_proxy("Proxy", secrets=[app_secret], vpc=self.vpc,
                                  vpc_subnets=subnets, security_groups=[proxy_sg], require_tls=True)
        if os.getenv("ENABLE_DATABASE_ROTATION") == "1":
            cluster.add_rotation_multi_user("RotateApplicationSecret", secret=app_secret,
                                            automatically_after=cdk.Duration.days(30),
                                            security_group=rotation_sg, vpc_subnets=subnets)
            cluster.add_rotation_multi_user("RotateSafetySecret", secret=self.safety_secret,
                                            automatically_after=cdk.Duration.days(30),
                                            security_group=rotation_sg, vpc_subnets=subnets)
        self.output("SafetySecretArn", self.safety_secret.secret_arn)
        self.output("ProxyEndpoint", proxy.endpoint)
        self.output("ApplicationSecretArn", app_secret.secret_arn)
        self.output("DatabaseAdminSecretArn", cluster.secret.secret_arn)
        self.output("VpcId", self.vpc.vpc_id)
        self.output("ApplicationSecurityGroupId", self.app_sg.security_group_id)

        cache_sg = ec2.SecurityGroup(self, "ValkeySecurityGroup", vpc=self.vpc)
        cache_sg.add_ingress_rule(self.app_sg, ec2.Port.tcp(6379))
        self.valkey = cache.CfnServerlessCache(
            self, "Valkey", engine="valkey", serverless_cache_name=self.stack.stack_name + "-valkey",
            kms_key_id=self.key("ValkeyKey").key_arn,
            subnet_ids=[s.subnet_id for s in self.vpc.isolated_subnets],
            security_group_ids=[cache_sg.security_group_id],
        )
        self.output("ValkeyEndpoint", self.valkey.attr_endpoint_address)

    def grant_safety_access(self, handler):
        handler.add_to_role_policy(iam.PolicyStatement(
            actions=["rds-data:ExecuteStatement"], resources=[self.safety_cluster.cluster_arn]))
        self.safety_secret.grant_read(handler)

    def analytics(self):
        database = glue.CfnDatabase(self, "LakeDatabase", catalog_id=self.stack.account,
                                    database_input=glue.CfnDatabase.DatabaseInputProperty(
                                        name=os.getenv("DATA_LAKE_DATABASE", "aspect_sentiment_lake")))
        for name, columns in {
            "usage": {"requests": "bigint", "succeeded": "bigint", "priced_requests": "bigint", "cost_usd": "double"},
            "accuracy": {"labeled": "bigint", "correct": "bigint"},
            "drift": {"model_version": "string", "drift_score": "double", "confidence": "double"},
        }.items():
            glue.CfnTable(self, name.title() + "Catalog", catalog_id=self.stack.account, database_name=database.ref,
                          table_input=glue.CfnTable.TableInputProperty(
                name=name, table_type="EXTERNAL_TABLE",
                parameters={
                    "classification": "parquet", "projection.enabled": "true",
                    "projection.dt.type": "date", "projection.dt.format": "yyyy-MM-dd",
                    "projection.dt.range": "2026-01-01,NOW", "projection.dt.interval": "1",
                    "projection.dt.interval.unit": "DAYS", "projection.tenant_id.type": "injected",
                    "storage.location.template": self.lake.s3_url_for_object(
                        name + "/dt=${dt}/tenant_id=${tenant_id}/"),
                },
                partition_keys=[glue.CfnTable.ColumnProperty(name=x, type="string") for x in ("dt", "tenant_id")],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=self.lake.s3_url_for_object(name + "/"),
                    columns=[glue.CfnTable.ColumnProperty(name=k, type=v) for k, v in columns.items()],
                    input_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"))))
        for name, limit in (("Operations", 10), ("Reporting", 25)):
            group = athena.CfnWorkGroup(self, name + "Workgroup", name=f"{self.stack.stack_name}-{name.lower()}",
                                work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True, publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=limit * 1024**3,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=self.lake.s3_url_for_object("query-results/" + name.lower() + "/"),
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS", kms_key=self.lake.encryption_key.key_arn))))
            if name == "Operations":
                statements = (Path(__file__).parent / "sql" / "lake_views.sql").read_text(encoding="utf-8")
                for index, statement in enumerate(statements.split(";")):
                    if "CREATE OR REPLACE VIEW" in statement:
                        athena.CfnNamedQuery(self, f"LakeView{index}", database=database.ref,
                                             work_group=group.ref, query_string=statement.strip())
