"""Checks on the synthesized template, mostly the security stuff: private buckets,
CSP, least-privilege roles, no secret in the Lambda. Skipped without aws-cdk-lib.
"""

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

from heatcheck_infra.settings import Settings  # noqa: E402
from heatcheck_infra.stack import HeatCheckStack  # noqa: E402

BASE = {"alert_email": "alerts@example.com", "github_repo": "someone/heatcheck"}
MAIN_ONLY = "repo:someone/heatcheck:ref:refs/heads/main"


def synth(**overrides) -> Template:
    app = cdk.App()
    stack = HeatCheckStack(app, "Test", settings=Settings(**{**BASE, **overrides}))
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return synth()


def resources(template: Template, kind: str, props: dict | None = None) -> dict:
    return template.find_resources(kind, {"Properties": props} if props else None)


def role_actions(template: Template, role_name: str) -> list[tuple[str, str]]:
    """(effect, action) pairs from every inline policy attached to the named role."""
    role_ids = list(resources(template, "AWS::IAM::Role", {"RoleName": role_name}))
    assert len(role_ids) == 1, f"expected one role named {role_name}"
    pairs = []
    for policy in resources(template, "AWS::IAM::Policy").values():
        if {"Ref": role_ids[0]} not in policy["Properties"]["Roles"]:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            pairs += [(statement["Effect"], action) for action in actions]
    return pairs


# ------------------------------------------------------------------ storage
def test_both_buckets_are_private_encrypted_versioned_and_kept(template):
    buckets = resources(template, "AWS::S3::Bucket")
    assert len(buckets) == 2
    for bucket in buckets.values():
        props = bucket["Properties"]
        assert props["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"] == {
            "SSEAlgorithm": "AES256"
        }
        assert props["VersioningConfiguration"] == {"Status": "Enabled"}
        assert props["OwnershipControls"]["Rules"] == [{"ObjectOwnership": "BucketOwnerEnforced"}]
        assert bucket["DeletionPolicy"] == "Retain"


def test_buckets_refuse_plain_http(template):
    policies = resources(template, "AWS::S3::BucketPolicy")
    assert len(policies) == 2
    for policy in policies.values():
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        assert any(
            s["Effect"] == "Deny" and s.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
            for s in statements
        )


# ------------------------------------------------------------------ CDN
def test_distribution_uses_https_and_origin_access_control(template):
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "DefaultRootObject": "index.html",
                    "HttpVersion": "http2and3",
                    "PriceClass": "PriceClass_100",
                    "DefaultCacheBehavior": Match.object_like(
                        {"ViewerProtocolPolicy": "redirect-to-https", "Compress": True}
                    ),
                }
            )
        },
    )


def test_security_headers(template):
    template.has_resource_properties(
        "AWS::CloudFront::ResponseHeadersPolicy",
        {
            "ResponseHeadersPolicyConfig": Match.object_like(
                {
                    "SecurityHeadersConfig": Match.object_like(
                        {
                            "ContentSecurityPolicy": {
                                "ContentSecurityPolicy": Match.string_like_regexp(
                                    "default-src 'none'.*require-trusted-types-for 'script'"
                                ),
                                "Override": True,
                            },
                            "FrameOptions": {"FrameOption": "DENY", "Override": True},
                            "StrictTransportSecurity": Match.object_like({"AccessControlMaxAgeSec": 63072000}),
                        }
                    )
                }
            )
        },
    )


def test_site_upload_never_touches_the_pipeline_output(template):
    deployments = resources(template, "Custom::CDKBucketDeployment")
    assert len(deployments) == 2
    for deployment in deployments.values():
        assert "data/*" in deployment["Properties"]["Exclude"] or deployment["Properties"]["Exclude"] == ["*"]


# ------------------------------------------------------------------ pipeline
def pipeline_function(template):
    found = resources(template, "AWS::Lambda::Function", {"FunctionName": "heatcheck-nightly"})
    assert len(found) == 1
    return next(iter(found.values()))["Properties"]


def test_pipeline_function_settings(template):
    props = pipeline_function(template)
    assert props["Runtime"] == "python3.13"
    assert props["Architectures"] == ["arm64"]
    assert props["Handler"] == "heatcheck.handler.lambda_handler"
    assert props["Timeout"] == 600
    env = props["Environment"]["Variables"]
    assert env["APINBA_KEY_PARAM"] == "/heatcheck/apinba-api-key"
    assert env["DATA_SOURCES"] == "espn,apinba"


def test_no_secret_in_the_lambda_environment(template):
    env = pipeline_function(template)["Environment"]["Variables"]
    assert "APINBA_KEY" not in env
    assert not any("SECRET" in k or k.endswith("_KEY") for k in env)


def test_pipeline_role_is_least_privilege(template):
    pairs = role_actions(template, "heatcheck-lambda")
    assert {action for _, action in pairs} == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "s3:ListBucket",
        "s3:GetObject",
        "s3:PutObject",
        "ssm:GetParameter",
    }
    assert all(effect == "Allow" and not action.endswith("*") for effect, action in pairs)


def test_schedule(template):
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "cron(30 5,12 * * ? *)",
            "ScheduleExpressionTimezone": "America/New_York",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "State": "ENABLED",
        },
    )
    assert {action for _, action in role_actions(template, "heatcheck-scheduler")} == {"lambda:InvokeFunction"}


# ------------------------------------------------------------------ monitoring
def test_alarms_and_dead_mans_switch(template):
    template.resource_count_is("AWS::CloudWatch::Alarm", 3)
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "HeatCheck",
            "MetricName": "PipelineSuccess",
            "Dimensions": [{"Name": "Project", "Value": "heatcheck"}],
            "EvaluationPeriods": 24,
            "DatapointsToAlarm": 24,
            "Period": 3600,
            "TreatMissingData": "breaching",
            "ComparisonOperator": "LessThanThreshold",
        },
    )
    template.resource_count_is("AWS::SNS::Subscription", 1)
    template.resource_count_is("AWS::Budgets::Budget", 1)


def test_no_email_means_no_subscription_or_budget():
    template = synth(alert_email="")
    template.resource_count_is("AWS::SNS::Subscription", 0)
    template.resource_count_is("AWS::Budgets::Budget", 0)


# ------------------------------------------------------------------ GitHub deploy role
def test_github_role_trusts_only_main_of_this_repo(template):
    template.resource_count_is("AWS::IAM::OIDCProvider", 1)
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": "heatcheck-github-deploy",
            "AssumeRolePolicyDocument": Match.object_like(
                {
                    "Statement": [
                        Match.object_like(
                            {
                                "Action": "sts:AssumeRoleWithWebIdentity",
                                "Condition": {
                                    "StringEquals": {
                                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                        "token.actions.githubusercontent.com:sub": MAIN_ONLY,
                                    }
                                },
                            }
                        )
                    ]
                }
            ),
        },
    )


def test_github_role_cannot_touch_pipeline_output_or_iam(template):
    pairs = role_actions(template, "heatcheck-github-deploy")
    assert ("Deny", "s3:PutObject") in pairs and ("Deny", "s3:DeleteObject") in pairs
    assert not any(action.startswith(("iam:", "cloudformation:", "ssm:")) or action == "*" for _, action in pairs)


def test_github_role_is_optional():
    template = synth(github_repo="")
    assert not resources(template, "AWS::IAM::Role", {"RoleName": "heatcheck-github-deploy"})
    template.resource_count_is("AWS::IAM::OIDCProvider", 0)


def test_existing_oidc_provider_can_be_reused():
    synth(create_github_oidc_provider=False).resource_count_is("AWS::IAM::OIDCProvider", 0)


# ------------------------------------------------------------------ custom domain
def test_custom_domain_adds_certificate_and_dns_records():
    template = synth(domain_name="hc.example.com", hosted_zone_id="Z0123456789", hosted_zone_name="example.com")
    template.resource_count_is("AWS::CertificateManager::Certificate", 1)
    template.resource_count_is("AWS::Route53::RecordSet", 2)
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {"DistributionConfig": Match.object_like({"Aliases": ["hc.example.com"]})},
    )
