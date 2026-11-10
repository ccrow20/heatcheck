"""Deploy settings from infra/config.json, overridable with `cdk deploy -c key=value`.

Keys starting with _ are ignored. Doesn't import aws_cdk so it can be tested
without it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

VALID_SOURCES = {"espn", "apinba"}


@dataclass(frozen=True)
class Settings:
    project: str = "heatcheck"
    aws_region: str = "us-east-1"
    alert_email: str = ""
    monthly_budget_usd: float = 5.0
    create_budget: bool = True
    data_sources: str = "espn,apinba"
    lookback_days: int = 2
    schedule_expression: str = "cron(30 5,12 * * ? *)"
    schedule_timezone: str = "America/New_York"
    lambda_memory_mb: int = 512
    lambda_timeout_seconds: int = 600
    log_retention_days: int = 30
    github_repo: str = ""
    create_github_oidc_provider: bool = True
    domain_name: str = ""
    hosted_zone_id: str = ""
    hosted_zone_name: str = ""

    def __post_init__(self) -> None:
        # It becomes the stack name, which CloudFormation requires to start with a letter.
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", self.project):
            raise ValueError("project: 2-31 lowercase letters, numbers or hyphens, starting with a letter")
        sources = [s.strip() for s in self.data_sources.split(",") if s.strip()]
        if not sources or any(s not in VALID_SOURCES for s in sources):
            raise ValueError(f"data_sources: comma-separated list of {sorted(VALID_SOURCES)}")
        if not 1 <= self.lookback_days <= 14:
            raise ValueError("lookback_days: between 1 and 14")
        if self.alert_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.alert_email):
            raise ValueError("alert_email: not an email address")
        if self.github_repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repo):
            raise ValueError("github_repo: use the form owner/name")
        if not 60 <= self.lambda_timeout_seconds <= 900:
            raise ValueError("lambda_timeout_seconds: between 60 and 900")
        if self.domain_name:
            if not (self.hosted_zone_id and self.hosted_zone_name):
                raise ValueError("domain_name needs hosted_zone_id and hosted_zone_name too")
            if self.aws_region != "us-east-1":
                raise ValueError("a custom domain needs aws_region us-east-1 (CloudFront certificates live there)")

    # Names other parts of the project rely on (the Lambda, the CLI, the Makefile).
    @property
    def function_name(self) -> str:
        return f"{self.project}-nightly"

    @property
    def apinba_key_parameter(self) -> str:
        return f"/{self.project}/apinba-api-key"

    @classmethod
    def load(cls, path: Path, context: Callable[[str], Any] | None = None) -> Settings:
        values: dict[str, Any] = {}
        if path.exists():
            values.update({k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")})
        known = {f.name: f for f in fields(cls)}
        unknown = sorted(set(values) - set(known))
        if unknown:
            raise ValueError(f"unknown setting(s) in {path.name}: {', '.join(unknown)}")
        if context is not None:
            for name in known:
                override = context(name)
                if override is not None:
                    values[name] = override
        return cls(**{name: _coerce(value, getattr(cls, name)) for name, value in values.items()})


def _coerce(value: Any, default: Any) -> Any:
    """CDK context from the command line arrives as strings."""
    if not isinstance(value, str) or isinstance(default, str):
        return value
    if isinstance(default, bool):
        if value.lower() in {"true", "1", "yes"}:
            return True
        if value.lower() in {"false", "0", "no"}:
            return False
        raise ValueError(f"expected true or false, got {value!r}")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value
