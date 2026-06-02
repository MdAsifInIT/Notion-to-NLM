"""
state_manager.py
────────────────
Lightweight JSON-backed state persistence for idempotent runs.
Supports atomic writes, file locking, and page ID tracking.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _file_lock(file_path: Path):
    """Cross-platform file lock using fcntl (Unix) or msvcrt (Windows)."""
    lock_file = file_path.with_suffix(file_path.suffix + ".lock")
    # Open lock file in write mode, ensuring it exists
    f = open(lock_file, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass


def load_state(
    state_file: str,
    default_lookback_hours: int = 24,
) -> tuple[datetime, list[str]]:
    """
    Read the state dict from *state_file*.
    Returns a tuple of (last_processed_time, list_of_processed_page_ids).
    If the file is missing or corrupt, returns default lookback and empty list.
    """
    path = Path(state_file)

    if path.exists():
        with _file_lock(path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                ts = data.get("last_processed_time")
                processed_page_ids = data.get("processed_page_ids", [])
                
                if ts:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    logger.info("Loaded state: last_processed_time=%s, %d page ID(s) tracked.", dt.isoformat(), len(processed_page_ids))
                    return dt, processed_page_ids
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                logger.warning("Could not parse state file %s (%s). Using default lookback.", state_file, exc)

    fallback = datetime.now(timezone.utc) - timedelta(hours=default_lookback_hours)
    logger.info(
        "No valid state found — defaulting to %d-hour lookback (%s).",
        default_lookback_hours,
        fallback.isoformat(),
    )
    return fallback, []


def load_last_processed(
    state_file: str,
    default_lookback_hours: int = 24,
) -> datetime:
    """Backward-compatible wrapper returning only the last_processed_time."""
    dt, _ = load_state(state_file, default_lookback_hours)
    return dt


def save_state(
    state_file: str,
    timestamp: datetime,
    processed_page_ids: list[str] | None = None,
) -> None:
    """
    Atomically persist state in *state_file* inside a file lock.
    """
    path = Path(state_file)
    ts_utc = timestamp.astimezone(timezone.utc)
    
    if processed_page_ids is None:
        processed_page_ids = []

    data = {
        "last_processed_time": ts_utc.isoformat(),
        "processed_page_ids": processed_page_ids
    }
    
    # Ensure directory exists
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with _file_lock(path):
        # Atomic write: write to temp file in same directory, then rename
        temp_fd, temp_path_str = tempfile.mkstemp(dir=str(path.parent), prefix=".state-tmp-")
        temp_path = Path(temp_path_str)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                json.dump(data, temp_file, indent=2)
            os.replace(temp_path, path)
        except OSError as exc:
            logger.warning("Atomic replace failed (%s). Falling back to direct write for docker compatibility.", exc)
            if temp_path.exists():
                temp_path.unlink()
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise

    logger.info("State atomically saved — last_processed_time = %s", ts_utc.isoformat())


def save_last_processed(state_file: str, timestamp: datetime) -> None:
    """Backward-compatible wrapper to save timestamp without page IDs."""
    save_state(state_file, timestamp, None)
