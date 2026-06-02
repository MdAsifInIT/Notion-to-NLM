"""
state_manager.py
────────────────
Lightweight JSON-backed state persistence for idempotent runs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def load_last_processed(
    state_file: str,
    default_lookback_hours: int = 24,
) -> datetime:
    """
    Read the ``last_processed_time`` from *state_file*.

    If the file is missing or corrupt, fall back to ``now - default_lookback_hours``.
    The returned datetime is always timezone-aware (UTC).
    """
    path = Path(state_file)

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ts = data.get("last_processed_time")
            if ts:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                logger.info("Loaded last_processed_time from state: %s", dt.isoformat())
                return dt
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning("Could not parse state file %s (%s). Using default lookback.", state_file, exc)

    fallback = datetime.now(timezone.utc) - timedelta(hours=default_lookback_hours)
    logger.info(
        "No valid state found — defaulting to %d-hour lookback (%s).",
        default_lookback_hours,
        fallback.isoformat(),
    )
    return fallback


def save_last_processed(state_file: str, timestamp: datetime) -> None:
    """
    Persist *timestamp* as ``last_processed_time`` in *state_file*.

    The timestamp is stored in ISO-8601 UTC format.
    """
    path = Path(state_file)
    ts_utc = timestamp.astimezone(timezone.utc)

    data = {"last_processed_time": ts_utc.isoformat()}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("State saved — last_processed_time = %s", ts_utc.isoformat())
