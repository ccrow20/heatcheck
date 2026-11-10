#!/usr/bin/env python3
"""CDK entry point: `cdk synth`, `cdk diff` and `cdk deploy` run this file."""

from __future__ import annotations

import os
from pathlib import Path

import aws_cdk as cdk

from heatcheck_infra.settings import Settings
from heatcheck_infra.stack import HeatCheckStack

app = cdk.App()
settings = Settings.load(Path(__file__).parent / "config.json", app.node.try_get_context)

HeatCheckStack(
    app,
    settings.project,  # the CloudFormation stack name
    settings=settings,
    description="Heat Check: nightly NBA hot/cold streaks (Lambda, S3, CloudFront)",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=settings.aws_region),
)
cdk.Tags.of(app).add("Project", settings.project)
cdk.Tags.of(app).add("ManagedBy", "cdk")

app.synth()
