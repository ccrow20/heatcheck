"""Runtime config from env vars.

The API-NBA key is read from the SSM parameter named in APINBA_KEY_PARAM, or
from APINBA_KEY for local runs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Values that mean "no key has been set yet".
_UNSET_VALUES = {"", "unset", "none", "changeme", "placeholder"}


@dataclass(frozen=True)
class Config:
    data_bucket: str
    site_bucket: str
    sources: tuple[str, ...] = ("espn", "apinba")
    lookback_days: int = 2
    project: str = "heatcheck"
    apinba_key: str | None = field(default=None, repr=False)  # keep it out of logs
    apinba_key_param: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        try:
            data_bucket = env["DATA_BUCKET"]
            site_bucket = env["SITE_BUCKET"]
        except KeyError as err:
            raise RuntimeError(f"missing required environment variable {err.args[0]}") from None
        sources = tuple(s.strip() for s in env.get("DATA_SOURCES", "espn,apinba").split(",") if s.strip())
        return cls(
            data_bucket=data_bucket,
            site_bucket=site_bucket,
            sources=sources,
            lookback_days=int(env.get("LOOKBACK_DAYS", "2")),
            project=env.get("PROJECT_NAME", "heatcheck"),
            apinba_key=env.get("APINBA_KEY") or None,
            apinba_key_param=env.get("APINBA_KEY_PARAM") or None,
        )


def clean_secret(value: str | None) -> str | None:
    if value is None or value.strip().lower() in _UNSET_VALUES:
        return None
    return value.strip()


def read_secret_parameter(name: str, ssm_client: Any = None) -> str | None:
    """Decrypt one SSM SecureString. Returns None (and logs why) if it can't.

    The error message is logged, never the value.
    """
    try:
        if ssm_client is None:
            import boto3

            ssm_client = boto3.client("ssm")
        resp = ssm_client.get_parameter(Name=name, WithDecryption=True)
        return clean_secret(resp["Parameter"]["Value"])
    except Exception as err:  # noqa: BLE001 - a missing key just disables that source
        code = getattr(err, "response", {}).get("Error", {}).get("Code", type(err).__name__)
        log.warning("could not read secret parameter %s (%s); continuing without it", name, code)
        return None


def resolve_apinba_key(config: Config, ssm_client: Any = None) -> str | None:
    """API key from the APINBA_KEY variable, else from SSM Parameter Store."""
    if config.apinba_key:
        return clean_secret(config.apinba_key)
    if not config.apinba_key_param:
        return None
    return read_secret_parameter(config.apinba_key_param, ssm_client)
