"""
state_manager.py
────────────────
JSON-backed state persistence for idempotent runs.

Provides atomic writes, corruption backups, stable page ID tracking, and
cross-process locks for both state files and whole pipeline runs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineState:
    """Validated pipeline checkpoint state."""

    last_processed_time: datetime
    processed_page_ids: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "last_processed_time": self.last_processed_time.astimezone(timezone.utc).isoformat(),
            "processed_page_ids": _stable_page_ids(self.processed_page_ids),
        }


@contextlib.contextmanager
def file_lock(lock_file: str | Path) -> Iterator[None]:
    """Cross-platform exclusive file lock that leaves the lock file in place."""
    path = Path(lock_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write("0")
        handle.flush()
        handle.seek(0)

        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

        try:
            yield
        finally:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                logger.debug("Failed to unlock %s cleanly.", path, exc_info=True)


@contextlib.contextmanager
def run_lock(lock_file: str | Path) -> Iterator[None]:
    """Acquire the whole-pipeline run lock."""
    logger.debug("Acquiring pipeline run lock: %s", lock_file)
    with file_lock(lock_file):
        yield


def _state_lock_path(path: Path) -> Path:
    suffix = path.suffix or ".json"
    return path.with_suffix(suffix + ".lock")


def _stable_page_ids(page_ids: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    return sorted({page_id for page_id in page_ids if isinstance(page_id, str) and page_id.strip()})


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("last_processed_time must be a non-empty ISO datetime string")

    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_state(data: object) -> PipelineState:
    if not isinstance(data, dict):
        raise ValueError("state root must be an object")

    timestamp = _parse_datetime(data.get("last_processed_time"))
    raw_ids = data.get("processed_page_ids", [])
    if raw_ids is None:
        raw_ids = []
    if not isinstance(raw_ids, list):
        raise ValueError("processed_page_ids must be a list")

    return PipelineState(
        last_processed_time=timestamp,
        processed_page_ids=_stable_page_ids(raw_ids),
    )


def _backup_corrupt_state(path: Path) -> Path | None:
    if not path.exists():
        return None

    backup = path.with_name(
        f"{path.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    try:
        shutil.copy2(path, backup)
        logger.warning("Backed up corrupt state file %s to %s.", path, backup)
        return backup
    except OSError:
        logger.warning("Failed to back up corrupt state file %s.", path, exc_info=True)
        return None


def load_pipeline_state(
    state_file: str,
    default_lookback_hours: int = 24,
) -> PipelineState:
    """Read and validate the persisted pipeline state."""
    path = Path(state_file)

    if path.exists():
        with file_lock(_state_lock_path(path)):
            try:
                state = _parse_state(json.loads(path.read_text(encoding="utf-8")))
                logger.info(
                    "Loaded state: last_processed_time=%s, %d page ID(s) tracked.",
                    state.last_processed_time.isoformat(),
                    len(state.processed_page_ids),
                )
                return state
            except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
                logger.warning("Could not parse state file %s (%s). Using default lookback.", path, exc)
                _backup_corrupt_state(path)

    fallback = datetime.now(timezone.utc) - timedelta(hours=default_lookback_hours)
    logger.info(
        "No valid state found; defaulting to %d-hour lookback (%s).",
        default_lookback_hours,
        fallback.isoformat(),
    )
    return PipelineState(last_processed_time=fallback, processed_page_ids=[])


def load_state(
    state_file: str,
    default_lookback_hours: int = 24,
) -> tuple[datetime, list[str]]:
    """
    Read the state dict from *state_file*.

    Returns a tuple of (last_processed_time, list_of_processed_page_ids).
    If the file is missing or corrupt, returns default lookback and empty list.
    """
    state = load_pipeline_state(state_file, default_lookback_hours)
    return state.last_processed_time, state.processed_page_ids


def load_last_processed(
    state_file: str,
    default_lookback_hours: int = 24,
) -> datetime:
    """Backward-compatible wrapper returning only the last_processed_time."""
    state, _ = load_state(state_file, default_lookback_hours)
    return state


def save_pipeline_state(state_file: str, state: PipelineState) -> None:
    """Atomically persist validated state in *state_file*."""
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_json_dict(), indent=2, sort_keys=True)

    with file_lock(_state_lock_path(path)):
        temp_fd, temp_path_str = tempfile.mkstemp(dir=str(path.parent), prefix=".state-tmp-")
        temp_path = Path(temp_path_str)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                temp_file.write(payload)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, path)
        except OSError as exc:
            logger.warning(
                "Atomic replace failed for %s (%s). Falling back to direct write.",
                path,
                exc,
            )
            if temp_path.exists():
                temp_path.unlink()
            with path.open("w", encoding="utf-8") as direct_file:
                direct_file.write(payload)
                direct_file.write("\n")
                direct_file.flush()
                os.fsync(direct_file.fileno())
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise

    logger.info(
        "State saved: last_processed_time=%s, %d page ID(s).",
        state.last_processed_time.astimezone(timezone.utc).isoformat(),
        len(state.processed_page_ids),
    )


def save_state(
    state_file: str,
    timestamp: datetime,
    processed_page_ids: list[str] | None = None,
) -> None:
    """Backward-compatible state save wrapper."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    save_pipeline_state(
        state_file,
        PipelineState(
            last_processed_time=timestamp.astimezone(timezone.utc),
            processed_page_ids=_stable_page_ids(processed_page_ids or []),
        ),
    )


def save_last_processed(state_file: str, timestamp: datetime) -> None:
    """Backward-compatible wrapper to save timestamp without page IDs."""
    save_state(state_file, timestamp, None)
