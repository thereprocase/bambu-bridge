"""Remove credentials before stdlib and structured records reach any log sink."""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any
from urllib.parse import quote, quote_plus

_QUERY = re.compile(r'''(?i)([?&](?:token|api_key|access_token|access_code|key)=)[^&\s\#'"<>]*''')
_BEARER = re.compile(r'''(?i)(\bBearer\s+)[^\s'"<>]+''')
_SECRET_FIELDS = {"token", "api_key", "bridge_api_key", "bridge_viz_token", "access_code"}
_secrets: frozenset[str] = frozenset()
_original_factory = logging.getLogRecordFactory()


def redact(value: str) -> str:
    for secret in _secrets:
        value = value.replace(secret, "[REDACTED]")
    value = _QUERY.sub(r"\1[REDACTED]", value)
    return _BEARER.sub(r"\1[REDACTED]", value)


def _record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _original_factory(*args, **kwargs)
    # AccessFormatter unpacks the five HTTP access arguments itself. Preserve
    # their types and shape while redacting the request target before any sink.
    if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
        record.args = tuple(redact(value) if isinstance(value, str) else value
                            for value in record.args)
    else:
        # WebSocket messages use the regular formatter. A handler/root-logger
        # filter alone misses non-propagating Uvicorn loggers.
        record.msg = redact(record.getMessage())
        record.args = ()
    if record.exc_info:
        record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
        record.exc_info = None
    if record.stack_info:
        record.stack_info = redact(record.stack_info)
    return record


def install(*secrets: str | None) -> None:
    global _secrets
    _secrets = _secrets | frozenset(
        encoded for secret in secrets if secret
        for encoded in (secret, quote(secret, safe=""), quote_plus(secret))
    )
    logging.setLogRecordFactory(_record_factory)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if str(k).lower() in _SECRET_FIELDS else _redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


def redact_event(_logger: Any, _method: str, event: Any) -> Any:
    """Also covers structlog's PrintLogger and serialized exception details."""
    return _redact_value(event)
