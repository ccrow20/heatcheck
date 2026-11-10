"""The whole app as one CloudFormation stack.

    Scheduler -> Lambda -> S3 (data, site) -> CloudFront
                        -> CloudWatch metrics -> alarms -> SNS
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from .settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
FRONTEND = REPO_ROOT / "frontend"

METRICS_NAMESPACE = "HeatCheck"  # must match backend/src/heatcheck/telemetry.py
GITHUB_OIDC_URL = "token.actions.githubusercontent.com"

# The site loads nothing from other origins (system fonts, no CDNs, no
# analytics), so the policy can allow only its own files. Trusted Types are
# enforced too: the code never assigns HTML strings, so any attempt to is blocked.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "upgrade-insecure-requests",
        "require-trusted-types-for 'script'",
        "trusted-types 'none'",
    ]
)
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"
)

RETENTION = {
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    90: logs.RetentionDays.THREE_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
}


class HeatCheckStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, settings: Settings, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.settings = settings

        self.site_bucket = self._private_bucket("SiteBucket", noncurrent_days=14)
        self.data_bucket = self._private_bucket("DataBucket", noncurrent_days=30)
        self.distribution = self._distribution()
        self._deploy_site()
        self.function = self._pipeline_function()
        self._schedule()
        self._monitoring()
        self.github_role = self._github_deploy_role() if settings.github_repo else None
        self._outputs()

    # storage

    def _private_bucket(self, construct_id: str, *, noncurrent_days: int) -> s3.Bucket:
        """Private, encrypted, TLS-only, versioned. Kept if the stack is deleted."""
        return s3.Bucket(
            self,
            construct_id,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            versioned=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-versions",
                    noncurrent_version_expiration=Duration.days(noncurrent_days),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

    # CDN + headers

    def _security_headers(self) -> cloudfront.ResponseHeadersPolicy:
        return cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            response_headers_policy_name=f"{self.settings.project}-security-headers",
            comment=f"Strict CSP and browser hardening for the {self.settings.project} site",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=CONTENT_SECURITY_POLICY, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(730), include_subdomains=True, preload=False, override=True
                ),
            ),
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                custom_headers=[
                    cloudfront.ResponseCustomHeader(
                        header="Permissions-Policy", value=PERMISSIONS_POLICY, override=True
                    ),
                    cloudfront.ResponseCustomHeader(
                        header="Cross-Origin-Opener-Policy", value="same-origin", override=True
                    ),
                    cloudfront.ResponseCustomHeader(
                        header="Cross-Origin-Resource-Policy", value="same-origin", override=True
                    ),
                ]
            ),
        )

    def _distribution(self) -> cloudfront.Distribution:
        settings = self.settings
        certificate = None
        zone = None
        if settings.domain_name:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                self, "Zone", hosted_zone_id=settings.hosted_zone_id, zone_name=settings.hosted_zone_name
            )
            certificate = acm.Certificate(
                self,
                "Certificate",
                domain_name=settings.domain_name,
                validation=acm.CertificateValidation.from_dns(zone),
            )

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"{settings.project} website",
            default_root_object="index.html",
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            enable_ipv6=True,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,  # North America and Europe: the cheapest class
            domain_names=[settings.domain_name] if settings.domain_name else None,
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                # Origin Access Control: CloudFront signs its requests, and the bucket
                # policy CDK adds lets only this distribution read the bucket.
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                # Caches by path only and honors the Cache-Control the Lambda sets on streaks.json.
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=self._security_headers(),
                compress=True,
            ),
        )

        if zone is not None:
            target = route53.RecordTarget.from_alias(targets.CloudFrontTarget(distribution))
            route53.ARecord(self, "AliasRecord", zone=zone, record_name=settings.domain_name, target=target)
            route53.AaaaRecord(self, "AliasRecordIpv6", zone=zone, record_name=settings.domain_name, target=target)
        return distribution

    def _deploy_site(self) -> None:
        """Upload the website on every `cdk deploy` (CI also uploads it on every push).

        data/ is excluded both from the upload and from pruning: data/streaks.json
        belongs to the Lambda, and the local sample file must never be published.
        """
        source = s3deploy.Source.asset(str(FRONTEND), exclude=["data", "data/*"])
        assets = s3deploy.BucketDeployment(
            self,
            "DeploySiteAssets",
            sources=[source],
            destination_bucket=self.site_bucket,
            exclude=["data/*", "*.html"],
            prune=True,
            cache_control=[s3deploy.CacheControl.from_string("public, max-age=3600")],
        )
        pages = s3deploy.BucketDeployment(
            self,
            "DeploySitePages",
            sources=[source],
            destination_bucket=self.site_bucket,
            exclude=["*"],
            include=["*.html"],
            prune=True,
            cache_control=[s3deploy.CacheControl.no_cache()],
            distribution=self.distribution,
            distribution_paths=["/*"],
        )
        pages.node.add_dependency(assets)  # refresh the CDN only after every file is in place

    # the pipeline

    def _pipeline_function(self) -> lambda_.Function:
        settings = self.settings
        log_group = logs.LogGroup(
            self,
            "PipelineLogs",
            log_group_name=f"/aws/lambda/{settings.function_name}",
            retention=RETENTION.get(settings.log_retention_days, logs.RetentionDays.ONE_MONTH),
            removal_policy=RemovalPolicy.DESTROY,
        )

        role = iam.Role(
            self,
            "PipelineRole",
            role_name=f"{settings.project}-lambda",
            description="Heat Check pipeline: game logs, streaks.json, one SSM parameter",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        log_group.grant_write(role)
        statements = [
            iam.PolicyStatement(sid="ListGameLogs", actions=["s3:ListBucket"], resources=[self.data_bucket.bucket_arn]),
            iam.PolicyStatement(
                sid="ReadWriteGameLogs",
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[self.data_bucket.arn_for_objects("gamelogs/*")],
            ),
            iam.PolicyStatement(
                sid="PublishStreaks", actions=["s3:PutObject"], resources=[self.site_bucket.arn_for_objects("data/*")]
            ),
            iam.PolicyStatement(
                sid="ReadApiKey",
                actions=["ssm:GetParameter"],
                resources=[
                    self.format_arn(
                        service="ssm", resource="parameter", resource_name=settings.apinba_key_parameter.lstrip("/")
                    )
                ],
            ),
        ]
        for statement in statements:
            role.add_to_policy(statement)

        return lambda_.Function(
            self,
            "Pipeline",
            function_name=settings.function_name,
            description="Ingests last night's NBA box scores and publishes hot/cold streaks",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="heatcheck.handler.lambda_handler",
            # No third-party dependencies, so the package is just the source folder.
            code=lambda_.Code.from_asset(str(BACKEND_SRC), exclude=["**/__pycache__", "**/*.pyc"]),
            memory_size=settings.lambda_memory_mb,
            timeout=Duration.seconds(settings.lambda_timeout_seconds),
            role=role,
            log_group=log_group,
            environment={
                "DATA_BUCKET": self.data_bucket.bucket_name,
                "SITE_BUCKET": self.site_bucket.bucket_name,
                "DATA_SOURCES": settings.data_sources,
                "LOOKBACK_DAYS": str(settings.lookback_days),
                "PROJECT_NAME": settings.project,
                # The parameter's name only. The key itself is decrypted at runtime.
                "APINBA_KEY_PARAM": settings.apinba_key_parameter,
                "LOG_LEVEL": "INFO",
            },
            # The scheduler invokes asynchronously. One retry covers a blip; the
            # second daily run is the real retry.
            retry_attempts=1,
            max_event_age=Duration.hours(1),
        )

    def _schedule(self) -> None:
        settings = self.settings
        role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{settings.project}-scheduler",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}}
            ),
        )
        role.add_to_policy(
            iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[self.function.function_arn])
        )
        scheduler.CfnSchedule(
            self,
            "Schedule",
            name=f"{settings.project}-nightly",
            description="Runs the Heat Check pipeline after the night's games",
            schedule_expression=settings.schedule_expression,
            schedule_expression_timezone=settings.schedule_timezone,
            state="ENABLED",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.function.function_arn,
                role_arn=role.role_arn,
                input="{}",
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=3600, maximum_retry_attempts=3
                ),
            ),
        )

    # monitoring

    def _monitoring(self) -> None:
        settings = self.settings
        topic = sns.Topic(self, "Alerts", topic_name=f"{settings.project}-alerts")
        if settings.alert_email:
            topic.add_subscription(subscriptions.EmailSubscription(settings.alert_email))
        notify = cw_actions.SnsAction(topic)

        def pipeline_metric(name: str) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name=name,
                dimensions_map={"Project": settings.project},
                statistic="Sum",
                period=Duration.hours(1),
            )

        errors = cloudwatch.Alarm(
            self,
            "PipelineErrors",
            alarm_name=f"{settings.project}-pipeline-errors",
            alarm_description=f"The Heat Check pipeline failed. Check the /aws/lambda/{settings.function_name} logs.",
            metric=self.function.metric_errors(period=Duration.hours(1), statistic="Sum"),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        errors.add_alarm_action(notify)

        # A dead man's switch: missing data counts as breaching, so this fires when
        # 24 straight hours pass without a successful run, even if the schedule
        # itself broke. The schedule runs twice a day, so one failed run never trips it.
        stale = cloudwatch.Alarm(
            self,
            "DataStale",
            alarm_name=f"{settings.project}-data-stale",
            alarm_description="No successful Heat Check run in 24 hours, so the site is showing old numbers.",
            metric=pipeline_metric("PipelineSuccess"),
            threshold=1,
            evaluation_periods=24,
            datapoints_to_alarm=24,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        stale.add_alarm_action(notify)
        stale.add_ok_action(notify)

        fallback = cloudwatch.Alarm(
            self,
            "PrimarySourceFailing",
            alarm_name=f"{settings.project}-primary-source-failing",
            alarm_description="The primary data source failed and the backup was used. The site is fine; "
            "check whether the primary changed its format.",
            metric=pipeline_metric("FallbackDays"),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        fallback.add_alarm_action(notify)

        if settings.alert_email and settings.create_budget:
            subscriber = budgets.CfnBudget.SubscriberProperty(address=settings.alert_email, subscription_type="EMAIL")
            budgets.CfnBudget(
                self,
                "MonthlyBudget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_name=f"{settings.project}-monthly",
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(amount=settings.monthly_budget_usd, unit="USD"),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type=kind,
                            threshold=threshold,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[subscriber],
                    )
                    for kind, threshold in (("ACTUAL", 80), ("FORECASTED", 100))
                ],
            )

    # GitHub Actions

    def _github_deploy_role(self) -> iam.Role:
        """Lets GitHub Actions deploy without stored AWS keys.

        Only pushes to main. Can update the site, Lambda code and CDN cache.
        """
        settings = self.settings
        if settings.create_github_oidc_provider:
            provider_arn = iam.CfnOIDCProvider(
                self,
                "GithubOidc",
                url=f"https://{GITHUB_OIDC_URL}",
                client_id_list=["sts.amazonaws.com"],
                thumbprint_list=[
                    "6938fd4d98bab03faadb97b34396831e3780aea1",
                    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
                ],
            ).attr_arn
        else:
            provider_arn = f"arn:{Aws.PARTITION}:iam::{Aws.ACCOUNT_ID}:oidc-provider/{GITHUB_OIDC_URL}"

        role = iam.Role(
            self,
            "GithubDeployRole",
            role_name=f"{settings.project}-github-deploy",
            description=f"GitHub Actions deploys for {settings.github_repo} (main branch only)",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_URL}:aud": "sts.amazonaws.com",
                        f"{GITHUB_OIDC_URL}:sub": f"repo:{settings.github_repo}:ref:refs/heads/main",
                    }
                },
            ),
        )
        distribution_arn = self.format_arn(
            service="cloudfront",
            region="",
            resource="distribution",
            resource_name=self.distribution.distribution_id,
        )
        statements = [
            iam.PolicyStatement(sid="ListSite", actions=["s3:ListBucket"], resources=[self.site_bucket.bucket_arn]),
            iam.PolicyStatement(
                sid="SyncSite",
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[self.site_bucket.arn_for_objects("*")],
            ),
            # streaks.json belongs to the Lambda; CI must never delete or overwrite it.
            iam.PolicyStatement(
                sid="KeepPipelineOutput",
                effect=iam.Effect.DENY,
                actions=["s3:PutObject", "s3:DeleteObject"],
                resources=[self.site_bucket.arn_for_objects("data/*")],
            ),
            iam.PolicyStatement(
                sid="InvalidateCdn",
                actions=["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"],
                resources=[distribution_arn],
            ),
            iam.PolicyStatement(
                sid="UpdateLambdaCode",
                actions=["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration"],
                resources=[self.function.function_arn],
            ),
        ]
        for statement in statements:
            role.add_to_policy(statement)
        return role

    # outputs

    def _outputs(self) -> None:
        settings = self.settings
        url = (
            f"https://{settings.domain_name}"
            if settings.domain_name
            else f"https://{self.distribution.distribution_domain_name}"
        )
        # Output keys are construct IDs too, so they must not reuse a resource's
        # ID (the buckets are "SiteBucket" and "DataBucket").
        values = {
            "SiteUrl": url,
            "SiteBucketName": self.site_bucket.bucket_name,
            "DataBucketName": self.data_bucket.bucket_name,
            "DistributionId": self.distribution.distribution_id,
            "FunctionName": self.function.function_name,
            "ApiKeyParameter": settings.apinba_key_parameter,
            "AwsRegion": Aws.REGION,
        }
        if self.github_role is not None:
            values["GithubDeployRoleArn"] = self.github_role.role_arn
        for key, value in values.items():
            CfnOutput(self, key, value=value)
