#!/usr/bin/env python3
"""
main.py — Notion → Google Docs Pipeline
────────────────────────────────────────
Pulls recently edited pages from a Notion database and upserts their content
into a single Google Doc.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from gdocs_handler import SyncError, sync_to_doc
from notion_handler import extract_page_text, query_recent_pages
from state_manager import file_lock, load_state, run_lock, save_state

load_dotenv()


@dataclass(frozen=True)
class PipelineConfig:
    notion_token: str
    notion_database_id: str
    google_doc_id: str
    google_credentials_file: str
    state_file: str
    default_lookback_hours: int
    state_safety_overlap_seconds: int
    run_lock_file: str
    metrics_file: str


@dataclass(frozen=True)
class PipelineResult:
    pages_synced: int
    pages_failed: int
    checkpoint_advanced: bool
    duration_seconds: float


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Missing required environment variable: {key}")
    return value


def _env_int(key: str, default: int, *, minimum: int = 0) -> int:
    value = os.getenv(key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {value!r}") from exc
    if parsed < minimum:
        raise ValueError(f"{key} must be >= {minimum}, got {parsed}")
    return parsed


def _load_config() -> PipelineConfig:
    return PipelineConfig(
        notion_token=_require_env("NOTION_TOKEN"),
        notion_database_id=_require_env("NOTION_DATABASE_ID"),
        google_doc_id=_require_env("GOOGLE_DOC_ID"),
        google_credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        state_file=os.getenv("STATE_FILE", "state.json"),
        default_lookback_hours=_env_int("DEFAULT_LOOKBACK_HOURS", 24, minimum=1),
        state_safety_overlap_seconds=_env_int("STATE_SAFETY_OVERLAP_SECONDS", 300, minimum=0),
        run_lock_file=os.getenv("RUN_LOCK_FILE", ".pipeline.lock"),
        metrics_file=os.getenv("METRICS_FILE", "metrics.json"),
    )


def _touch_healthy() -> None:
    try:
        Path(os.getenv("HEALTH_FILE", "/tmp/healthy")).touch()
    except Exception:
        logging.getLogger("pipeline").debug("Failed to touch health file.", exc_info=True)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True)
    temp_fd, temp_path_str = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-tmp-")
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(payload)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        logging.getLogger("pipeline").warning(
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


def _write_metrics(
    *,
    metrics_file: str | None = None,
    pages_synced: int = 0,
    pages_failed: int = 0,
    duration: float = 0.0,
    error_msg: str | None = None,
) -> None:
    """Atomically update file-based metrics."""
    path = Path(metrics_file or os.getenv("METRICS_FILE", "metrics.json"))
    lock_path = path.with_suffix((path.suffix or ".json") + ".lock")

    with file_lock(lock_path):
        data: dict[str, Any] = {
            "last_sync_timestamp": None,
            "last_sync_duration_seconds": 0.0,
            "last_sync_pages_synced": 0,
            "last_sync_pages_failed": 0,
            "total_pages_synced": 0,
            "total_sync_runs": 0,
            "total_errors": 0,
            "last_error": None,
            "last_error_timestamp": None,
        }

        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
            except Exception:
                logging.getLogger("pipeline").warning("Could not read metrics file %s.", path, exc_info=True)

        now_iso = datetime.now(timezone.utc).isoformat()
        data["last_sync_timestamp"] = now_iso
        data["last_sync_duration_seconds"] = round(duration, 3)
        data["last_sync_pages_synced"] = pages_synced
        data["last_sync_pages_failed"] = pages_failed
        data["total_pages_synced"] = int(data.get("total_pages_synced", 0)) + pages_synced
        data["total_sync_runs"] = int(data.get("total_sync_runs", 0)) + 1

        if error_msg:
            data["total_errors"] = int(data.get("total_errors", 0)) + 1
            data["last_error"] = error_msg
            data["last_error_timestamp"] = now_iso

        _write_json_atomic(path, data)


def _entry_has_syncable_content(entry: dict[str, Any]) -> bool:
    if str(entry.get("body", "")).strip():
        return True
    if entry.get("blocks"):
        return True
    return bool(entry.get("properties"))


def _checkpoint_since(last_processed: datetime, overlap_seconds: int) -> datetime:
    if last_processed.tzinfo is None:
        last_processed = last_processed.replace(tzinfo=timezone.utc)
    return last_processed.astimezone(timezone.utc) - timedelta(seconds=overlap_seconds)


def run_once() -> PipelineResult:
    start_time = time.perf_counter()
    config = _load_config()
    logger = logging.getLogger("pipeline")
    pages_synced = 0
    pages_failed = 0

    try:
        with run_lock(config.run_lock_file):
            last_processed, processed_ids = load_state(
                config.state_file,
                config.default_lookback_hours,
            )
            query_since = _checkpoint_since(last_processed, config.state_safety_overlap_seconds)
            run_start = datetime.now(timezone.utc)

            pages = query_recent_pages(
                config.notion_token,
                config.notion_database_id,
                query_since,
            )

            if not pages:
                logger.info("No new or modified pages found. Nothing to do.")
                save_state(config.state_file, run_start, processed_ids)
                duration = time.perf_counter() - start_time
                _write_metrics(
                    metrics_file=config.metrics_file,
                    pages_synced=0,
                    pages_failed=0,
                    duration=duration,
                )
                _touch_healthy()
                return PipelineResult(0, 0, True, duration)

            entries: list[dict[str, Any]] = []
            for page in pages:
                page_id = page.get("id", "<missing-page-id>")
                try:
                    entry = extract_page_text(config.notion_token, page)
                    if _entry_has_syncable_content(entry):
                        entries.append(entry)
                    else:
                        logger.debug(
                            "Page '%s' has no syncable content; skipping.",
                            entry.get("title", page_id),
                        )
                except Exception:
                    logger.exception("Failed to extract page %s; checkpoint will not advance.", page_id)
                    pages_failed += 1

            if entries:
                try:
                    sync_result = sync_to_doc(
                        config.google_credentials_file,
                        config.google_doc_id,
                        entries,
                    )
                    pages_synced = sync_result.synced
                    pages_failed += sync_result.failed
                except SyncError as exc:
                    pages_synced = exc.result.synced
                    pages_failed += exc.result.failed
                    raise
            else:
                logger.info("No pages produced syncable entries.")

            checkpoint_advanced = pages_failed == 0
            if checkpoint_advanced:
                new_processed_ids = sorted({*processed_ids, *(str(e["page_id"]) for e in entries if e.get("page_id"))})
                save_state(config.state_file, run_start, new_processed_ids)
            else:
                logger.warning(
                    "Leaving checkpoint unchanged because %d page(s) failed.",
                    pages_failed,
                )

            duration = time.perf_counter() - start_time
            _write_metrics(
                metrics_file=config.metrics_file,
                pages_synced=pages_synced,
                pages_failed=pages_failed,
                duration=duration,
            )
            logger.info("Pipeline complete; %d entry/entries synced.", pages_synced)
            _touch_healthy()
            return PipelineResult(pages_synced, pages_failed, checkpoint_advanced, duration)

    except Exception as exc:
        duration = time.perf_counter() - start_time
        _write_metrics(
            metrics_file=config.metrics_file if "config" in locals() else None,
            pages_synced=pages_synced,
            pages_failed=pages_failed,
            duration=duration,
            error_msg=str(exc),
        )
        raise


def _parse_interval_hours() -> float | None:
    interval_env = os.getenv("SYNC_INTERVAL_HOURS")
    if not interval_env:
        return None
    try:
        interval_hours = float(interval_env)
    except ValueError:
        logging.getLogger("pipeline").error(
            "Invalid SYNC_INTERVAL_HOURS: %s. Must be a number. Falling back to run-once mode.",
            interval_env,
        )
        return None
    if interval_hours <= 0:
        logging.getLogger("pipeline").error(
            "Invalid SYNC_INTERVAL_HOURS: %s. Must be greater than zero. Falling back to run-once mode.",
            interval_env,
        )
        return None
    return interval_hours


def run() -> None:
    _configure_logging()
    logger = logging.getLogger("pipeline")
    interval_hours = _parse_interval_hours()

    if interval_hours is None:
        logger.info("Notion to Google Docs pipeline starting (run-once mode).")
        run_once()
        return

    logger.info(
        "Notion to Google Docs pipeline running in daemon mode (interval: %s hours).",
        interval_hours,
    )
    interval_seconds = interval_hours * 3600
    while True:
        logger.info("Triggering scheduled sync run.")
        try:
            run_once()
        except Exception:
            logger.exception("Pipeline run failed inside daemon loop. Will retry on next interval.")

        next_sync = datetime.now().replace(microsecond=0) + timedelta(seconds=interval_seconds)
        logger.info("Sleeping for %s hours (next sync at %s).", interval_hours, next_sync)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        logging.exception("Pipeline failed with an unhandled exception.")
        sys.exit(1)
