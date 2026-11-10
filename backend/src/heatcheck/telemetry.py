"""CloudWatch metrics via EMF (one JSON line on stdout). Alarms are in infra/heatcheck_infra/stack.py."""

from __future__ import annotations

import json
import sys
import time
from typing import IO

NAMESPACE = "HeatCheck"


def emit_metrics(
    metrics: dict[str, float],
    *,
    project: str,
    namespace: str = NAMESPACE,
    stream: IO[str] | None = None,
) -> str:
    """Print an EMF record and return it (handy for tests)."""
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [["Project"]],
                    "Metrics": [{"Name": name, "Unit": "Count"} for name in metrics],
                }
            ],
        },
        "Project": project,
        **metrics,
    }
    line = json.dumps(record)
    print(line, file=stream or sys.stdout, flush=True)
    return line
