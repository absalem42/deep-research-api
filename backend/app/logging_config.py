"""Structured logging.

JSON in production so Cloud Logging / Datadog parse it without a grok rule;
human-readable lines everywhere else.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from .config import Settings

# These libraries log a line per HTTP call at INFO. Useful locally, noise in prod.
_NOISY = ("httpx", "httpcore", "urllib3", "openai._base_client", "anthropic._base_client")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=UTC
            ).isoformat(),
            # Cloud Logging keys off "severity", not "level"
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if request_id := getattr(record, "request_id", None):
            payload["request_id"] = request_id
        if job_id := getattr(record, "job_id", None):
            payload["job_id"] = job_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.is_production:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
