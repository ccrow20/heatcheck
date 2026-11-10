"""Data sources. Add a provider by writing a class with ``name`` and ``fetch_day``."""

from __future__ import annotations

import logging
from typing import Any

from .apinba import HOST as APINBA_HOST
from .apinba import ApiNbaSource
from .base import AllSourcesFailed, DataSource, SourceError, fetch_with_fallback, validate_day
from .espn import HOST as ESPN_HOST
from .espn import EspnSource

log = logging.getLogger(__name__)

# Seconds to wait between requests to each host. API-NBA's free plan allows
# 10 requests per minute; ESPN publishes no limit, so be polite anyway.
REQUEST_INTERVALS = {
    APINBA_HOST: 6.5,
    ESPN_HOST: 0.75,
}

AVAILABLE_SOURCES = ("espn", "apinba")


def build_sources(names: list[str] | tuple[str, ...], http: Any, *, apinba_key: str | None = None) -> list[DataSource]:
    """Instantiate sources in priority order, skipping any that are not configured."""
    sources: list[DataSource] = []
    for raw_name in names:
        name = raw_name.strip().lower()
        if not name:
            continue
        if name == "espn":
            sources.append(EspnSource(http))
        elif name == "apinba":
            if apinba_key:
                sources.append(ApiNbaSource(http, apinba_key))
            else:
                log.info("skipping apinba source: no API key configured")
        else:
            raise ValueError(f"unknown data source {raw_name!r}; choose from {AVAILABLE_SOURCES}")
    if not sources:
        raise ValueError("no usable data sources configured")
    return sources


__all__ = [
    "AVAILABLE_SOURCES",
    "REQUEST_INTERVALS",
    "AllSourcesFailed",
    "ApiNbaSource",
    "DataSource",
    "EspnSource",
    "SourceError",
    "build_sources",
    "fetch_with_fallback",
    "validate_day",
]
