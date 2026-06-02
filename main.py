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

import logging
import os
import sys
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

from gdocs_handler import append_to_doc
from notion_handler import extract_page_text, query_recent_pages
from state_manager import load_last_processed, save_last_processed

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


def run_once() -> None:
    # 1. Read configuration ──────────────────────────────────────────────
    notion_token = _require_env("NOTION_TOKEN")
    database_id = _require_env("NOTION_DATABASE_ID")
    doc_id = _require_env("GOOGLE_DOC_ID")
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    state_file = os.getenv("STATE_FILE", "state.json")
    lookback_hours = int(os.getenv("DEFAULT_LOOKBACK_HOURS", "24"))

    # 2. Determine the "since" timestamp ─────────────────────────────────
    since = load_last_processed(state_file, lookback_hours)
    run_start = datetime.now(timezone.utc)

    # 3. Query Notion ────────────────────────────────────────────────────
    pages = query_recent_pages(notion_token, database_id, since)

    if not pages:
        logging.getLogger("pipeline").info("No new or modified pages found. Nothing to do.")
        # Still update the timestamp so the next run doesn't re-scan.
        save_last_processed(state_file, run_start)
        return

    # 4. Extract text from each page ─────────────────────────────────────
    entries: list[dict[str, str]] = []
    for page in pages:
        try:
            entry = extract_page_text(notion_token, page)
            if entry["body"].strip():
                entries.append(entry)
            else:
                logging.getLogger("pipeline").debug("Page '%s' has no text content — skipping.", entry["title"])
        except Exception:
            logging.getLogger("pipeline").exception("Failed to extract page %s — skipping.", page.get("id"))

    if not entries:
        logging.getLogger("pipeline").info("All pages were empty or failed extraction. Nothing to append.")
        save_last_processed(state_file, run_start)
        return

    # 5. Append to Google Doc ────────────────────────────────────────────
    append_to_doc(credentials_file, doc_id, entries)

    # 6. Persist state ───────────────────────────────────────────────────
    save_last_processed(state_file, run_start)

    logging.getLogger("pipeline").info(
        "═══ Pipeline complete — %d entry/entries synced ═══",
        len(entries),
    )


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
