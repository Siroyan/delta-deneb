"""CloudFront, private S3, and an IAM-authenticated Lambda health endpoint."""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as deployment
from constructs import Construct


ROOT = Path(__file__).resolve().parents[1]


class FoundationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

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
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                str(ROOT / "backend" / "health"),
                exclude=["__pycache__", "**/__pycache__", "*.pyc", "**/*.pyc", "*.pyo", "**/*.pyo"],
            ),
            memory_size=128,
            timeout=Duration.seconds(5),
            environment={"STAGE": stage},
            log_group=self.api_logs,
            description="Step 1 health endpoint; no telemetry or DynamoDB access",
        )
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
            header_behavior=cloudfront.OriginRequestHeaderBehavior.none(),
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

        site_url = f"https://{self.distribution.distribution_domain_name}"
        CfnOutput(self, "SiteUrl", value=f"{site_url}/")
        CfnOutput(self, "HealthUrl", value=f"{site_url}/api/health")
        CfnOutput(self, "StaticBucketName", value=self.site_bucket.bucket_name)
        CfnOutput(self, "ApiFunctionName", value=self.api.function_name)
        CfnOutput(self, "ApiLogGroupName", value=self.api_logs.log_group_name)
        CfnOutput(self, "FunctionUrl", value=self.function_url.url)
