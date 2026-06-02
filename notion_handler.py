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

from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class RenderedBlock:
    text: str
    block_type: str
    annotations: list[dict[str, Any]]
    code_language: str | None = None
    checked: bool | None = None


def _rich_text_to_plain(rich_texts: list[dict[str, Any]]) -> str:
    """Collapse a Notion rich-text array into a single plain string."""
    return "".join(rt.get("plain_text", "") for rt in rich_texts)


def _process_rich_text(rich_texts: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """
    Concatenates the plain text from Notion's rich text array, and returns:
      1. The concatenated plain text string.
      2. A list of annotation dicts relative to the concatenated text.
    """
    text = ""
    annotations = []
    for rt in rich_texts:
        pt = rt.get("plain_text", "")
        if not pt:
            continue
        start = len(text)
        text += pt
        end = len(text)
        
        ann = rt.get("annotations", {})
        has_ann = (
            ann.get("bold") or 
            ann.get("italic") or 
            ann.get("underline") or 
            ann.get("strikethrough") or 
            ann.get("code") or 
            (ann.get("color") and ann.get("color") != "default")
        )
        if has_ann:
            annotations.append({
                "start": start,
                "end": end,
                "bold": ann.get("bold", False),
                "italic": ann.get("italic", False),
                "underline": ann.get("underline", False),
                "strikethrough": ann.get("strikethrough", False),
                "code": ann.get("code", False),
                "color": ann.get("color", "default"),
            })
    return text, annotations


def _render_block(block: dict[str, Any]) -> RenderedBlock | None:
    """Return a RenderedBlock for a supported block type, or *None*."""
    btype = block.get("type", "")
    data = block.get(btype, {})

    if btype in ("paragraph", "quote", "callout", "toggle"):
        text, annotations = _process_rich_text(data.get("rich_text", []))
        return RenderedBlock(text=text, block_type=btype, annotations=annotations)

    if btype in ("heading_1", "heading_2", "heading_3"):
        text, annotations = _process_rich_text(data.get("rich_text", []))
        return RenderedBlock(text=text, block_type=btype, annotations=annotations)

    if btype == "bulleted_list_item":
        text, annotations = _process_rich_text(data.get("rich_text", []))
        return RenderedBlock(text=text, block_type=btype, annotations=annotations)

    if btype == "numbered_list_item":
        text, annotations = _process_rich_text(data.get("rich_text", []))
        return RenderedBlock(text=text, block_type=btype, annotations=annotations)

    if btype == "to_do":
        text, annotations = _process_rich_text(data.get("rich_text", []))
        checked = data.get("checked", False)
        prefix = "[x] " if checked else "[ ] "
        for ann in annotations:
            ann["start"] += len(prefix)
            ann["end"] += len(prefix)
        return RenderedBlock(text=prefix + text, block_type=btype, annotations=annotations, checked=checked)

    if btype == "code":
        lang = data.get("language", "")
        text, annotations = _process_rich_text(data.get("rich_text", []))
        return RenderedBlock(text=text, block_type=btype, annotations=annotations, code_language=lang)

    if btype == "divider":
        return RenderedBlock(text="---", block_type=btype, annotations=[])

    # Unsupported block — skip silently.
    logger.debug("Skipping unsupported block type: %s", btype)
    return None


def _extract_properties(page: dict[str, Any]) -> list[dict[str, str]]:
    """Extract properties of the Notion page into a list of key-value dicts."""
    properties = page.get("properties", {})
    extracted = []
    
    for name, prop in properties.items():
        ptype = prop.get("type")
        if not ptype:
            continue
        
        if ptype == "title":
            continue
            
        value_str = ""
        data = prop.get(ptype)
        if data is None:
            continue
            
        if ptype == "rich_text":
            value_str = "".join(rt.get("plain_text", "") for rt in data)
        elif ptype == "number":
            value_str = str(data)
        elif ptype == "checkbox":
            value_str = "Yes" if data else "No"
        elif ptype in ("url", "email", "phone_number"):
            value_str = str(data)
        elif ptype == "select":
            value_str = data.get("name", "")
        elif ptype == "multi_select":
            value_str = ", ".join(item.get("name", "") for item in data)
        elif ptype == "date":
            start = data.get("start", "")
            end = data.get("end")
            if end:
                value_str = f"{start} to {end}"
            else:
                value_str = start
        elif ptype == "people":
            value_str = ", ".join(person.get("name", "") for person in data if person.get("name"))
        elif ptype in ("created_by", "last_edited_by"):
            value_str = data.get("name", "")
        elif ptype in ("created_time", "last_edited_time"):
            value_str = str(data)
        else:
            continue
            
        if value_str.strip():
            extracted.append({
                "name": name,
                "value": value_str.strip()
            })
            
    extracted.sort(key=lambda x: x["name"])
    return extracted


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


def extract_page_text(token: str, page: dict[str, Any]) -> dict[str, Any]:
    """
    Download all supported blocks for a single page and return::

        {
            "title": "...",
            "body": "...",
            "last_edited": "...",
            "blocks": [...],
            "properties": [...],
            "page_id": "..."
        }
    """
    client = Client(auth=token, notion_version="2022-06-28")
    page_id = page["id"]
    title = _get_page_title(page)
    last_edited = page.get("last_edited_time", "")

    logger.debug("Extracting blocks from page '%s' (%s)", title, page_id)

    blocks: list[RenderedBlock] = []
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
                    blocks.append(rendered)

            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")

    except (APIResponseError, HTTPResponseError) as exc:
        logger.error("Notion API error reading blocks for page %s: %s", page_id, exc)
        raise

    properties = _extract_properties(page)

    return {
        "title": title,
        "body": "\n".join(b.text for b in blocks),
        "last_edited": last_edited,
        "blocks": blocks,
        "properties": properties,
        "page_id": page_id,
    }
