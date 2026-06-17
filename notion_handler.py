"""
notion_handler.py
─────────────────
Queries a Notion database and extracts formatted plain-text content from pages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from retry_utils import RetryConfig, retry_call

logger = logging.getLogger(__name__)


@dataclass
class RenderedBlock:
    text: str
    block_type: str
    annotations: list[dict[str, Any]]
    code_language: str | None = None
    checked: bool | None = None
    depth: int = 0


def _get_client(token: str, retry_config: RetryConfig | None = None) -> Any:
    """Create a Notion client lazily so unit tests do not require the SDK."""
    try:
        from notion_client import Client
    except ImportError as exc:
        raise RuntimeError(
            "notion-client is required for Notion API calls. Install requirements.txt first."
        ) from exc

    config = retry_config or RetryConfig.from_env()
    try:
        return Client(
            auth=token,
            notion_version="2022-06-28",
            timeout_ms=int(config.timeout_seconds * 1000),
        )
    except TypeError:
        return Client(auth=token, notion_version="2022-06-28")


def _rich_text_to_plain(rich_texts: list[dict[str, Any]]) -> str:
    """Collapse a Notion rich-text array into a single plain string."""
    return "".join(str(rt.get("plain_text", "")) for rt in rich_texts if isinstance(rt, dict))


def _process_rich_text(rich_texts: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """
    Concatenate Notion rich text and return text plus annotation ranges relative
    to the concatenated string.
    """
    text = ""
    annotations: list[dict[str, Any]] = []
    for rich_text in rich_texts or []:
        if not isinstance(rich_text, dict):
            continue

        plain_text = str(rich_text.get("plain_text", ""))
        if not plain_text:
            continue

        start = len(text)
        text += plain_text
        end = len(text)

        ann = rich_text.get("annotations") or {}
        if not isinstance(ann, dict):
            ann = {}
        has_ann = (
            ann.get("bold")
            or ann.get("italic")
            or ann.get("underline")
            or ann.get("strikethrough")
            or ann.get("code")
            or (ann.get("color") and ann.get("color") != "default")
        )
        if has_ann:
            annotations.append(
                {
                    "start": start,
                    "end": end,
                    "bold": bool(ann.get("bold", False)),
                    "italic": bool(ann.get("italic", False)),
                    "underline": bool(ann.get("underline", False)),
                    "strikethrough": bool(ann.get("strikethrough", False)),
                    "code": bool(ann.get("code", False)),
                    "color": ann.get("color", "default"),
                }
            )
    return text, annotations


def _render_block(block: dict[str, Any], depth: int = 0) -> RenderedBlock | None:
    """Return a RenderedBlock for a supported block type, or None."""
    btype = block.get("type", "")
    data = block.get(btype, {})
    if not isinstance(data, dict):
        logger.debug("Skipping malformed block %s: payload is not an object.", block.get("id"))
        return None

    rich_text_types = {
        "paragraph",
        "quote",
        "callout",
        "toggle",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
    }
    if btype in rich_text_types:
        text, annotations = _process_rich_text(data.get("rich_text", []))
        return RenderedBlock(text=text, block_type=btype, annotations=annotations, depth=depth)

    if btype == "to_do":
        text, annotations = _process_rich_text(data.get("rich_text", []))
        checked = bool(data.get("checked", False))
        prefix = "[x] " if checked else "[ ] "
        for ann in annotations:
            ann["start"] += len(prefix)
            ann["end"] += len(prefix)
        return RenderedBlock(
            text=prefix + text,
            block_type=btype,
            annotations=annotations,
            checked=checked,
            depth=depth,
        )

    if btype == "code":
        language = data.get("language") or None
        text, annotations = _process_rich_text(data.get("rich_text", []))
        return RenderedBlock(
            text=text,
            block_type=btype,
            annotations=annotations,
            code_language=language,
            depth=depth,
        )

    if btype == "divider":
        return RenderedBlock(text="---", block_type=btype, annotations=[], depth=depth)

    if btype == "child_page":
        title = data.get("title")
        if title:
            return RenderedBlock(text=str(title), block_type=btype, annotations=[], depth=depth)

    logger.debug("Skipping unsupported block type: %s", btype)
    return None


def _formula_to_text(data: dict[str, Any]) -> str:
    ftype = data.get("type")
    if ftype in {"string", "number", "boolean"}:
        value = data.get(ftype)
        return "" if value is None else str(value)
    if ftype == "date":
        date_value = data.get("date") or {}
        return _date_to_text(date_value) if isinstance(date_value, dict) else ""
    return ""


def _date_to_text(data: dict[str, Any]) -> str:
    start = data.get("start", "")
    end = data.get("end")
    return f"{start} to {end}" if end else str(start)


def _rollup_to_text(data: dict[str, Any]) -> str:
    rtype = data.get("type")
    if rtype == "array":
        values: list[str] = []
        for item in data.get("array", []):
            if isinstance(item, dict):
                item_type = item.get("type")
                item_data = item.get(item_type)
                values.append(_property_value_to_text(item_type, item_data))
        return ", ".join(value for value in values if value)
    if rtype in {"number", "date"}:
        return _property_value_to_text(rtype, data.get(rtype))
    return ""


def _property_value_to_text(ptype: str | None, data: Any) -> str:
    if data is None or not ptype:
        return ""

    if ptype in {"title", "rich_text"}:
        return _rich_text_to_plain(data if isinstance(data, list) else [])
    if ptype == "number":
        return str(data)
    if ptype == "checkbox":
        return "Yes" if data else "No"
    if ptype in {"url", "email", "phone_number", "created_time", "last_edited_time"}:
        return str(data)
    if ptype in {"select", "status", "created_by", "last_edited_by"} and isinstance(data, dict):
        return str(data.get("name", ""))
    if ptype == "multi_select" and isinstance(data, list):
        return ", ".join(str(item.get("name", "")) for item in data if isinstance(item, dict))
    if ptype == "date" and isinstance(data, dict):
        return _date_to_text(data)
    if ptype == "people" and isinstance(data, list):
        return ", ".join(
            str(person.get("name", ""))
            for person in data
            if isinstance(person, dict) and person.get("name")
        )
    if ptype == "files" and isinstance(data, list):
        names: list[str] = []
        for file_obj in data:
            if isinstance(file_obj, dict):
                names.append(str(file_obj.get("name") or file_obj.get("type") or "file"))
        return ", ".join(names)
    if ptype == "relation" and isinstance(data, list):
        return ", ".join(str(item.get("id", "")) for item in data if isinstance(item, dict) and item.get("id"))
    if ptype == "formula" and isinstance(data, dict):
        return _formula_to_text(data)
    if ptype == "rollup" and isinstance(data, dict):
        return _rollup_to_text(data)
    if ptype == "unique_id" and isinstance(data, dict):
        prefix = data.get("prefix") or ""
        number = data.get("number")
        return f"{prefix}-{number}" if prefix and number is not None else str(number or "")

    return ""


def _extract_properties(page: dict[str, Any]) -> list[dict[str, str]]:
    """Extract user-visible Notion properties into sorted key/value rows."""
    properties = page.get("properties", {})
    if not isinstance(properties, dict):
        return []

    extracted: list[dict[str, str]] = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue

        ptype = prop.get("type")
        if ptype == "title":
            continue

        value = _property_value_to_text(ptype, prop.get(ptype))
        if value.strip():
            extracted.append({"name": str(name), "value": value.strip()})

    extracted.sort(key=lambda item: item["name"].casefold())
    return extracted


def _format_since(since: datetime) -> str:
    if since.tzinfo is None:
        logger.warning("Received naive since timestamp; assuming UTC.")
        since = since.replace(tzinfo=timezone.utc)
    since_utc = since.astimezone(timezone.utc)
    return since_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def query_recent_pages(
    token: str,
    database_id: str,
    since: datetime,
) -> list[dict[str, Any]]:
    """
    Return Notion pages from *database_id* last edited on or after *since*.

    Handles pagination automatically and retries transient failures.
    """
    retry_config = RetryConfig.from_env()
    client = _get_client(token, retry_config)
    since_iso = _format_since(since)
    logger.info("Querying Notion DB %s for pages edited since %s", database_id, since_iso)

    body: dict[str, Any] = {
        "filter": {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": since_iso},
        },
        "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}],
    }

    pages: list[dict[str, Any]] = []
    next_cursor: str | None = None

    while True:
        request_body = dict(body)
        if next_cursor:
            request_body["start_cursor"] = next_cursor

        response = retry_call(
            lambda: client.request(
                path=f"databases/{database_id}/query",
                method="POST",
                body=request_body,
            ),
            config=retry_config,
            logger=logger,
            operation_name="Notion database query",
        )
        pages.extend(response.get("results", []))
        if not response.get("has_more", False):
            break
        next_cursor = response.get("next_cursor")
        if not next_cursor:
            logger.warning("Notion response had has_more=true but no next_cursor; stopping pagination.")
            break

    logger.info("Found %d page(s) to process.", len(pages))
    return pages


def _get_page_title(page: dict[str, Any]) -> str:
    """Best-effort extraction of a page title from its properties."""
    for prop in page.get("properties", {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title = _rich_text_to_plain(prop.get("title", []))
            if title.strip():
                return title.strip()
    return "Untitled"


def _iter_child_blocks(
    client: Any,
    block_id: str,
    retry_config: RetryConfig,
    *,
    depth: int = 0,
) -> list[RenderedBlock]:
    blocks: list[RenderedBlock] = []
    next_cursor: str | None = None

    while True:
        kwargs: dict[str, Any] = {"block_id": block_id}
        if next_cursor:
            kwargs["start_cursor"] = next_cursor

        response = retry_call(
            lambda: client.blocks.children.list(**kwargs),
            config=retry_config,
            logger=logger,
            operation_name="Notion block children list",
        )

        for block in response.get("results", []):
            if not isinstance(block, dict):
                continue
            rendered = _render_block(block, depth=depth)
            if rendered is not None:
                blocks.append(rendered)
            if block.get("has_children") and block.get("id"):
                blocks.extend(
                    _iter_child_blocks(
                        client,
                        str(block["id"]),
                        retry_config,
                        depth=depth + 1,
                    )
                )

        if not response.get("has_more", False):
            break
        next_cursor = response.get("next_cursor")
        if not next_cursor:
            logger.warning("Notion block response had has_more=true but no next_cursor.")
            break

    return blocks


def extract_page_text(token: str, page: dict[str, Any]) -> dict[str, Any]:
    """
    Download all supported blocks for a single page and return the sync entry.
    """
    retry_config = RetryConfig.from_env()
    client = _get_client(token, retry_config)
    page_id = page["id"]
    title = _get_page_title(page)
    last_edited = page.get("last_edited_time", "")

    logger.debug("Extracting blocks from page '%s' (%s)", title, page_id)
    blocks = _iter_child_blocks(client, page_id, retry_config)
    properties = _extract_properties(page)

    return {
        "title": title,
        "body": "\n".join(block.text for block in blocks),
        "last_edited": last_edited,
        "blocks": blocks,
        "properties": properties,
        "page_id": page_id,
    }
