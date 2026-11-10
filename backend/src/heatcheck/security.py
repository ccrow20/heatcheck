"""Log filter that redacts the API key."""

from __future__ import annotations

import logging
from collections.abc import Iterable

REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 6  # don't "redact" short strings that would mangle normal text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str | None]):
        super().__init__()
        self._secrets = sorted({s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH}, key=len, reverse=True)

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self._secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, REDACTED)
        if record.exc_info and record.exc_info[1] is not None:
            # Exception text is formatted later by the handler; scrub it too.
            text = logging.Formatter().formatException(record.exc_info)
            scrubbed = text
            for secret in self._secrets:
                scrubbed = scrubbed.replace(secret, REDACTED)
            if scrubbed != text:
                redacted = f"{redacted}\n{scrubbed}"
                record.exc_info = None
                record.exc_text = None
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_redaction(*secrets: str | None, logger: logging.Logger | None = None) -> RedactingFilter:
    """Attach a redacting filter to every handler of ``logger`` (default: root).

    Handler filters (not logger filters) so propagated records get caught too.
    """
    target = logger or logging.getLogger()
    redactor = RedactingFilter(secrets)
    if not redactor.secrets:
        return redactor
    for handler in target.handlers:
        # Warm Lambda containers reuse handlers, so don't stack duplicate filters.
        if not any(isinstance(f, RedactingFilter) and f.secrets == redactor.secrets for f in handler.filters):
            handler.addFilter(redactor)
    return redactor
