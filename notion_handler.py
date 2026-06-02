"""
notion_handler.py
─────────────────
Queries a Notion database and extracts plain-text content from its pages.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from notion_client import Client
from notion_client.errors import APIResponseError, HTTPResponseError

logger = logging.getLogger(__name__)

# ── Block types we know how to render ───────────────────────────────────────


def _rich_text_to_plain(rich_texts: list[dict[str, Any]]) -> str:
    """Collapse a Notion rich-text array into a single plain string."""
    return "".join(rt.get("plain_text", "") for rt in rich_texts)


def _render_block(block: dict[str, Any]) -> str | None:
    """Return a plain-text line for a supported block type, or *None*."""
    btype = block.get("type", "")
    data = block.get(btype, {})

    if btype in ("paragraph", "quote", "callout", "toggle"):
        return _rich_text_to_plain(data.get("rich_text", []))

    if btype in ("heading_1", "heading_2", "heading_3"):
        text = _rich_text_to_plain(data.get("rich_text", []))
        level = int(btype[-1])
        prefix = "#" * level
        return f"{prefix} {text}"

    if btype == "bulleted_list_item":
        return f"  • {_rich_text_to_plain(data.get('rich_text', []))}"

    if btype == "numbered_list_item":
        return f"  1. {_rich_text_to_plain(data.get('rich_text', []))}"

    if btype == "to_do":
        checked = "x" if data.get("checked") else " "
        return f"  [{checked}] {_rich_text_to_plain(data.get('rich_text', []))}"

    if btype == "code":
        lang = data.get("language", "")
        code = _rich_text_to_plain(data.get("rich_text", []))
        return f"```{lang}\n{code}\n```"

    if btype == "divider":
        return "---"

    # Unsupported block — skip silently.
    logger.debug("Skipping unsupported block type: %s", btype)
    return None


# ── Public API ──────────────────────────────────────────────────────────────


def query_recent_pages(
    token: str,
    database_id: str,
    since: datetime,
) -> list[dict[str, Any]]:
    """
    Return Notion pages from *database_id* that were **last edited** on or
    after *since* (a timezone-aware UTC datetime).

    Handles pagination automatically.

    Uses ``client.request()`` for compatibility with notion-client v3+,
    where ``databases.query()`` was removed.
    """
    client = Client(auth=token, notion_version="2022-06-28")

    # Notion requires simplified ISO-8601 with trailing Z, not +00:00
    since_utc = since.astimezone(timezone.utc)
    since_iso = since_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    logger.info("Querying Notion DB %s for pages edited since %s", database_id, since_iso)

    body: dict[str, Any] = {
        "filter": {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": since_iso},
        },
        "sorts": [
            {"timestamp": "last_edited_time", "direction": "ascending"},
        ],
    }

    pages: list[dict[str, Any]] = []
    has_more = True
    next_cursor: str | None = None

    try:
        while has_more:
            request_body = {**body}
            if next_cursor:
                request_body["start_cursor"] = next_cursor

            response = client.request(
                path=f"databases/{database_id}/query",
                method="POST",
                body=request_body,
            )
            pages.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")

    except (APIResponseError, HTTPResponseError) as exc:
        logger.error("Notion API error while querying database: %s", exc)
        raise

    logger.info("Found %d page(s) to process.", len(pages))
    return pages


def _get_page_title(page: dict[str, Any]) -> str:
    """Best-effort extraction of a page title from its properties."""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _rich_text_to_plain(prop.get("title", []))
    return "Untitled"


def extract_page_text(token: str, page: dict[str, Any]) -> dict[str, str]:
    """
    Download all supported blocks for a single page and return::

        {"title": "...", "body": "...", "last_edited": "..."}
    """
    client = Client(auth=token, notion_version="2022-06-28")
    page_id = page["id"]
    title = _get_page_title(page)
    last_edited = page.get("last_edited_time", "")

    logger.debug("Extracting blocks from page '%s' (%s)", title, page_id)

    lines: list[str] = []
    has_more = True
    next_cursor: str | None = None

    try:
        while has_more:
            kwargs: dict[str, Any] = {}
            if next_cursor:
                kwargs["start_cursor"] = next_cursor

            response = client.blocks.children.list(block_id=page_id, **kwargs)

            for block in response.get("results", []):
                rendered = _render_block(block)
                if rendered is not None:
                    lines.append(rendered)

            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")

    except (APIResponseError, HTTPResponseError) as exc:
        logger.error("Notion API error reading blocks for page %s: %s", page_id, exc)
        raise

    return {
        "title": title,
        "body": "\n".join(lines),
        "last_edited": last_edited,
    }
