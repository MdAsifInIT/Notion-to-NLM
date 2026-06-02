#!/usr/bin/env python3
"""
main.py — Notion → Google Docs Pipeline
────────────────────────────────────────
Pulls recently edited pages from a Notion database and appends their plain-
text content to a single, continuous Google Doc.

Environment variables (see .env.example):
    NOTION_TOKEN, NOTION_DATABASE_ID, GOOGLE_DOC_ID,
    GOOGLE_CREDENTIALS_FILE, STATE_FILE,
    DEFAULT_LOOKBACK_HOURS, LOG_LEVEL
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

from gdocs_handler import sync_to_doc
from notion_handler import extract_page_text, query_recent_pages
from state_manager import load_state, save_state

# ── Bootstrap ───────────────────────────────────────────────────────────────

load_dotenv()


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


# ── Main pipeline ──────────────────────────────────────────────────────────


def _write_metrics(
    pages_synced: int = 0,
    pages_failed: int = 0,
    duration: float = 0.0,
    error_msg: str | None = None,
) -> None:
    """
    Atomically update file-based metrics.
    """
    metrics_file = os.getenv("METRICS_FILE", "metrics.json")
    path = Path(metrics_file)

    data = {
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
            data.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass

    now_iso = datetime.now(timezone.utc).isoformat()
    data["last_sync_timestamp"] = now_iso
    data["last_sync_duration_seconds"] = round(duration, 3)
    data["last_sync_pages_synced"] = pages_synced
    data["last_sync_pages_failed"] = pages_failed
    data["total_pages_synced"] += pages_synced
    data["total_sync_runs"] += 1

    if error_msg:
        data["total_errors"] += 1
        data["last_error"] = error_msg
        data["last_error_timestamp"] = now_iso

    path_parent = path.parent
    path_parent.mkdir(parents=True, exist_ok=True)

    try:
        temp_fd, temp_path_str = tempfile.mkstemp(dir=str(path_parent), prefix=".metrics-tmp-")
        temp_path = Path(temp_path_str)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
            json.dump(data, temp_file, indent=2)
        os.replace(temp_path, path)
    except OSError as exc:
        logging.getLogger("pipeline").warning("Atomic replace failed for metrics (%s). Falling back to direct write.", exc)
        if 'temp_path' in locals() and temp_path.exists():
            temp_path.unlink()
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logging.getLogger("pipeline").warning("Failed to direct-write metrics to %s: %s", metrics_file, e)
    except Exception as exc:
        if 'temp_path' in locals() and temp_path.exists():
            temp_path.unlink()
        logging.getLogger("pipeline").warning("Failed to write metrics to %s: %s", metrics_file, exc)


def run_once() -> None:
    start_time = time.perf_counter()
    pages_synced = 0
    pages_failed = 0

    try:
        # 1. Read configuration ──────────────────────────────────────────────
        notion_token = _require_env("NOTION_TOKEN")
        database_id = _require_env("NOTION_DATABASE_ID")
        doc_id = _require_env("GOOGLE_DOC_ID")
        credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        state_file = os.getenv("STATE_FILE", "state.json")
        lookback_hours = int(os.getenv("DEFAULT_LOOKBACK_HOURS", "24"))

        # 2. Determine the "since" timestamp ─────────────────────────────────
        since, processed_ids = load_state(state_file, lookback_hours)
        run_start = datetime.now(timezone.utc)

        # 3. Query Notion ────────────────────────────────────────────────────
        pages = query_recent_pages(notion_token, database_id, since)

        if not pages:
            logging.getLogger("pipeline").info("No new or modified pages found. Nothing to do.")
            save_state(state_file, run_start, processed_ids)
            _write_metrics(pages_synced=0, pages_failed=0, duration=time.perf_counter() - start_time)
            # Touch healthy file (for Docker Healthcheck)
            try:
                Path("/tmp/healthy").touch()
            except Exception:
                pass
            return

        # 4. Extract text from each page ─────────────────────────────────────
        entries: list[dict[str, Any]] = []
        for page in pages:
            try:
                entry = extract_page_text(notion_token, page)
                if entry["body"].strip():
                    entries.append(entry)
                else:
                    logging.getLogger("pipeline").debug("Page '%s' has no text content — skipping.", entry["title"])
            except Exception:
                logging.getLogger("pipeline").exception("Failed to extract page %s — skipping.", page.get("id"))
                pages_failed += 1

        if not entries:
            logging.getLogger("pipeline").info("All pages were empty or failed extraction. Nothing to append.")
            save_state(state_file, run_start, processed_ids)
            _write_metrics(pages_synced=0, pages_failed=pages_failed, duration=time.perf_counter() - start_time)
            # Touch healthy file (for Docker Healthcheck)
            try:
                Path("/tmp/healthy").touch()
            except Exception:
                pass
            return

        # 5. Sync to Google Doc ──────────────────────────────────────────────
        sync_to_doc(credentials_file, doc_id, entries)

        # 6. Persist state ───────────────────────────────────────────────────
        new_processed_ids = list(set(processed_ids + [e["page_id"] for e in entries]))
        save_state(state_file, run_start, new_processed_ids)

        pages_synced = len(entries)
        duration = time.perf_counter() - start_time
        _write_metrics(pages_synced=pages_synced, pages_failed=pages_failed, duration=duration)

        logging.getLogger("pipeline").info(
            "═══ Pipeline complete — %d entry/entries synced ═══",
            pages_synced,
        )

        # Touch healthy file (for Docker Healthcheck)
        try:
            Path("/tmp/healthy").touch()
        except Exception:
            pass

    except Exception as exc:
        duration = time.perf_counter() - start_time
        _write_metrics(pages_synced=0, pages_failed=0, duration=duration, error_msg=str(exc))
        raise


def run() -> None:
    import time
    _configure_logging()
    logger = logging.getLogger("pipeline")

    interval_env = os.getenv("SYNC_INTERVAL_HOURS")
    
    if interval_env:
        try:
            interval_hours = float(interval_env)
        except ValueError:
            logger.error("Invalid SYNC_INTERVAL_HOURS: %s. Must be a number. Falling back to run-once mode.", interval_env)
            interval_hours = None
    else:
        interval_hours = None

    if interval_hours is not None:
        logger.info("═══ Notion → Google Docs pipeline running in daemon mode (Interval: %s hours) ═══", interval_hours)
        interval_seconds = interval_hours * 3600
        while True:
            logger.info("Triggering scheduled sync run...")
            try:
                run_once()
            except Exception:
                logger.exception("Pipeline run failed inside daemon loop. Will retry on next scheduled interval.")
            
            logger.info("Sleeping for %s hours (next sync at %s)...", 
                        interval_hours, 
                        datetime.now().replace(microsecond=0) + timedelta(seconds=interval_seconds))
            time.sleep(interval_seconds)
    else:
        logger.info("═══ Notion → Google Docs pipeline starting (run-once mode) ═══")
        run_once()


# ── Entrypoint ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        logging.exception("Pipeline failed with an unhandled exception.")
        sys.exit(1)
