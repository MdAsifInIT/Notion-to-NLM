"""
retry_utils.py
──────────────
Small, dependency-free retry helpers for external API calls.
"""

from __future__ import annotations

import email.utils
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypeVar

T = TypeVar("T")

RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
RETRYABLE_EXCEPTION_NAMES = {
    "ConnectionError",
    "ConnectError",
    "HTTPError",
    "ReadTimeout",
    "RemoteDisconnected",
    "ServiceUnavailable",
    "Timeout",
    "TimeoutError",
    "TooManyRequests",
    "TransportError",
}


@dataclass(frozen=True)
class RetryConfig:
    """Runtime retry settings for outbound API calls."""

    timeout_seconds: float = 30.0
    max_retries: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "RetryConfig":
        return cls(
            timeout_seconds=_env_float("API_TIMEOUT_SECONDS", 30.0, minimum=1.0),
            max_retries=_env_int("API_MAX_RETRIES", 5, minimum=0),
            base_delay_seconds=_env_float("API_RETRY_BASE_SECONDS", 1.0, minimum=0.1),
            max_delay_seconds=_env_float("API_RETRY_MAX_SECONDS", 60.0, minimum=0.1),
        )


def _env_int(name: str, default: int, minimum: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logging.getLogger(__name__).warning("Invalid %s=%r; using %s.", name, value, default)
        return default
    return max(parsed, minimum)


def _env_float(name: str, default: float, minimum: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        logging.getLogger(__name__).warning("Invalid %s=%r; using %s.", name, value, default)
        return default
    return max(parsed, minimum)


def get_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction across Google, Notion, and transports."""
    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value

    resp = getattr(exc, "resp", None)
    if resp is not None:
        for attr in ("status", "status_code"):
            value = getattr(resp, attr, None)
            if isinstance(value, int):
                return value

    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value

    return None


def get_retry_after_seconds(exc: BaseException) -> float | None:
    """Parse Retry-After from an exception response if one is present."""
    headers = None
    resp = getattr(exc, "resp", None) or getattr(exc, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", None)

    if not headers:
        return None

    value = None
    if hasattr(headers, "get"):
        value = headers.get("retry-after") or headers.get("Retry-After")
    if not value:
        return None

    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max((parsed - datetime.now(timezone.utc)).total_seconds(), 0.0)


def is_retryable_exception(exc: BaseException) -> bool:
    """Return True when an exception looks like a transient API/transport failure."""
    status = get_status_code(exc)
    if status in RETRYABLE_STATUS_CODES:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    exc_names = {type(exc).__name__}
    exc_names.update(type(parent).__name__ for parent in type(exc).__mro__)
    return bool(exc_names & RETRYABLE_EXCEPTION_NAMES)


def retry_call(
    operation: Callable[[], T],
    *,
    config: RetryConfig | None = None,
    logger: logging.Logger | None = None,
    operation_name: str = "API request",
    retryable: Callable[[BaseException], bool] = is_retryable_exception,
) -> T:
    """Run *operation* with bounded exponential backoff and jitter."""
    retry_config = config or RetryConfig.from_env()
    log = logger or logging.getLogger(__name__)
    attempt = 0

    while True:
        try:
            return operation()
        except Exception as exc:
            if attempt >= retry_config.max_retries or not retryable(exc):
                raise

            retry_after = get_retry_after_seconds(exc)
            if retry_after is None:
                delay = min(
                    retry_config.max_delay_seconds,
                    retry_config.base_delay_seconds * (2**attempt),
                )
                delay += random.uniform(0, min(delay * 0.25, 1.0))
            else:
                delay = min(retry_after, retry_config.max_delay_seconds)

            attempt += 1
            log.warning(
                "%s failed transiently (%s). Retrying in %.2fs (%d/%d).",
                operation_name,
                exc,
                delay,
                attempt,
                retry_config.max_retries,
            )
            time.sleep(delay)
