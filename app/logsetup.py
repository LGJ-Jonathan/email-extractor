"""Logging for the API and the worker. One place, so both get the same protections.

httpx logs every request URL at INFO, and Jina's wallet endpoint takes the API key in
the query string -- so the key went into the logs on every balance check. httpx and
httpcore are held at WARNING, and a filter masks anything that looks like a credential
in any log line, as a backstop for the next library that does the same.
"""

import logging
import re

_SECRET = re.compile(
    r"(?i)(api[_-]?key=|access[_-]?token=|token=|key=)[^&\s\"']+"
    r"|(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
)


def redact(text: str) -> str:
    return _SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "[redacted]", text)


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record: leave it alone
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def configure(level: str) -> None:
    logging.basicConfig(level=level)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactSecrets) for f in handler.filters):
            handler.addFilter(RedactSecrets())
