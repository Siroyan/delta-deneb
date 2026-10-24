"""CloudFront, private S3, and an IAM-authenticated Lambda health endpoint."""

from pathlib import Path

from aws_cdk import ArnFormat, CfnOutput, DefaultStackSynthesizer, Duration, RemovalPolicy, Stack, Stage, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as deployment
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iot as iot
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infra.deployment_settings import deployment_settings
from scripts.package_api import package_api


ROOT = Path(__file__).resolve().parents[1]


class FoundationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kwargs):
        settings = deployment_settings(stage)
        kwargs.setdefault("synthesizer", DefaultStackSynthesizer(qualifier=settings["qualifier"]))
        super().__init__(scope, construct_id, **kwargs)

        boundary = iam.ManagedPolicy.from_managed_policy_arn(
            self, "RuntimeBoundary",
            self.format_arn(service="iam", region="", resource="policy", resource_name=settings["runtime_boundary"]),
        )
        iam.PermissionsBoundary.of(self).apply(boundary)
        Tags.of(self).add("Project", "delta-deneb")
        Tags.of(self).add("Environment", stage)

        self.telemetry_table = dynamodb.Table(
            self, "TelemetryTable", table_name=settings["telemetry_table"],
            partition_key=dynamodb.Attribute(name="machine_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sample_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.DEFAULT,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.telemetry_table.add_global_secondary_index(
            index_name=settings["telemetry_index"],
            partition_key=dynamodb.Attribute(name="machine_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="received_at_ms", type=dynamodb.AttributeType.NUMBER),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.viewer_secret = secretsmanager.Secret(
            self, "ViewerToken", secret_name=settings["viewer_secret"],
            generate_secret_string=secretsmanager.SecretStringGenerator(password_length=48, exclude_punctuation=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
        rule_arn = self.format_arn(service="iot", resource="rule", resource_name=settings["iot_rule"], arn_format=ArnFormat.SLASH_RESOURCE_NAME)
        ingest = iam.Role(
            self, "TelemetryIngestRole", role_name=f"{settings['stack_name']}-telemetry-ingest",
            assumed_by=iam.ServicePrincipal("iot.amazonaws.com", conditions={
                "StringEquals": {"aws:SourceAccount": self.account}, "ArnEquals": {"aws:SourceArn": rule_arn},
            }),
            inline_policies={"StoreTelemetry": iam.PolicyDocument(statements=[iam.PolicyStatement(
                actions=["dynamodb:PutItem"], resources=[self.telemetry_table.table_arn],
                conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [settings["machine_id"]]}},
            )])},
        )
        self.telemetry_rule = iot.CfnTopicRule(
            self, "TelemetryRule", rule_name=settings["iot_rule"],
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                aws_iot_sql_version="2016-03-23", rule_disabled=False,
                description=f"delta-deneb {stage}: copy telemetry to the dedicated table",
                sql=("SELECT speed, average_speed, lap_number, total_time_ms, lap_time_ms, "
                     "latitude, longitude, timestamp_ms, completed_lap_times_ms, "
                     f"'{settings['machine_id']}' AS machine_id, "
                     "timestamp() AS received_at_ms, newuuid() AS sample_id "
                     f"FROM '{settings['mqtt_topic']}'"),
                actions=[iot.CfnTopicRule.ActionProperty(dynamo_d_bv2=iot.CfnTopicRule.DynamoDBv2ActionProperty(
                    role_arn=ingest.role_arn,
                    put_item=iot.CfnTopicRule.PutItemInputProperty(table_name=self.telemetry_table.table_name),
                ))],
            ),
        )

        self.site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.api_logs = logs.LogGroup(
            self,
            "ApiLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.api = lambda_.Function(
            self,
            "HealthApi",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="backend.telemetry.handler.handler",
            code=lambda_.Code.from_asset(str(package_api(Path(Stage.of(self).outdir) / f"{self.artifact_id}.api-package"))),
            memory_size=256,
            timeout=Duration.seconds(30),
            environment={"STAGE": stage, "TELEMETRY_TABLE": self.telemetry_table.table_name,
                         "TELEMETRY_INDEX": settings["telemetry_index"], "MACHINE_ID": settings["machine_id"],
                         "VIEWER_SECRET_ARN": self.viewer_secret.secret_arn},
            log_group=self.api_logs,
            description="Token-protected, read-only telemetry API and health endpoint",
        )
        self.api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:Query"],
            resources=[self.telemetry_table.table_arn, f"{self.telemetry_table.table_arn}/index/{settings['telemetry_index']}"],
            conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [settings["machine_id"]]}},
        ))
        self.viewer_secret.grant_read(self.api)
        self.function_url = self.api.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
        )

        static_cache = cloudfront.CachePolicy(
            self,
            "StaticCache",
            min_ttl=Duration.seconds(0),
            default_ttl=Duration.minutes(5),
            max_ttl=Duration.hours(1),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )
        api_requests = cloudfront.OriginRequestPolicy(
            self,
            "ApiRequests",
            query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all(),
            header_behavior=cloudfront.OriginRequestHeaderBehavior.allow_list("X-Deneb-Token"),
            cookie_behavior=cloudfront.OriginRequestCookieBehavior.none(),
        )
        api_behavior = cloudfront.BehaviorOptions(
            origin=origins.FunctionUrlOrigin.with_origin_access_control(self.function_url),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=api_requests,
            response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
        )
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cache_policy=static_cache,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
                compress=True,
            ),
            additional_behaviors={"/api/*": api_behavior, "/api": api_behavior},
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
            error_responses=[
                cloudfront.ErrorResponse(http_status=status, ttl=Duration.seconds(0))
                for status in (403, 404, 500, 502, 503, 504)
            ],
            comment=f"delta-deneb {stage} - static demo and health API",
        )

        self.site_deployment = deployment.BucketDeployment(
            self,
            "DeploySite",
            sources=[deployment.Source.asset(str(ROOT / "web"), exclude=[".DS_Store", "**/.DS_Store"])],
            destination_bucket=self.site_bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
            cache_control=[
                deployment.CacheControl.from_string(
                    "public,max-age=0,s-maxage=300,must-revalidate"
                )
            ],
            prune=True,
            retain_on_delete=True,
        )
        # BucketDeployment does not expose a layer name. Give its generated
        # layer a stable name so the bootstrap can grant permissions to it only.
        cli_layer = self.site_deployment.node.find_child("AwsCliLayer").node.default_child
        if not isinstance(cli_layer, lambda_.CfnLayerVersion):
            raise TypeError("Review BucketDeployment's AWS CLI layer before updating CDK")
        cli_layer.layer_name = settings["deployment_layer_name"]

        site_url = f"https://{self.distribution.distribution_domain_name}"
        CfnOutput(self, "SiteUrl", value=f"{site_url}/")
        CfnOutput(self, "HealthUrl", value=f"{site_url}/api/health")
        CfnOutput(self, "StaticBucketName", value=self.site_bucket.bucket_name)
        CfnOutput(self, "ApiFunctionName", value=self.api.function_name)
        CfnOutput(self, "ApiLogGroupName", value=self.api_logs.log_group_name)
        CfnOutput(self, "FunctionUrl", value=self.function_url.url)
        CfnOutput(self, "TelemetryTableName", value=self.telemetry_table.table_name)
        CfnOutput(self, "TelemetryRuleName", value=self.telemetry_rule.ref)
        CfnOutput(self, "ViewerSecretArn", value=self.viewer_secret.secret_arn)
        CfnOutput(self, "TelemetryLatestUrl", value=f"{site_url}/api/telemetry/latest")
