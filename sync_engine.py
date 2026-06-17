"""
sync_engine.py
──────────────
Builds idempotent Google Docs upsert requests using Named Ranges as anchors.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from retry_utils import RetryConfig, retry_call

logger = logging.getLogger(__name__)

NOTION_RANGE_PREFIX = "notion_page_"


@dataclass(frozen=True)
class NamedRangeInfo:
    name: str
    start: int | None
    end: int | None
    ids: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.start is not None and self.end is not None and self.start < self.end


@dataclass(frozen=True)
class UpsertPlan:
    requests: list[dict[str, Any]]
    range_name: str
    action: str
    insert_index: int
    repaired_named_range_ids: list[str] = field(default_factory=list)


def range_name_for_page(page_id: str) -> str:
    """Return a stable Google Docs named range name for a Notion page ID."""
    normalized = re.sub(r"[^A-Za-z0-9_]", "", page_id.replace("-", ""))
    if not normalized:
        normalized = hashlib.sha256(page_id.encode("utf-8")).hexdigest()[:32]
    return f"{NOTION_RANGE_PREFIX}{normalized}"


def _document_length(doc: dict[str, Any]) -> int:
    content = doc.get("body", {}).get("content", [])
    if not content:
        return 1
    return int(content[-1].get("endIndex", 1))


def _collect_named_ranges(doc: dict[str, Any]) -> dict[str, NamedRangeInfo]:
    result: dict[str, NamedRangeInfo] = {}
    named_ranges_map = doc.get("namedRanges", {})
    if not isinstance(named_ranges_map, dict):
        return result

    for name, named_ranges_obj in named_ranges_map.items():
        if not isinstance(name, str) or not name.startswith(NOTION_RANGE_PREFIX):
            continue

        nr_list: list[dict[str, Any]]
        if isinstance(named_ranges_obj, dict):
            raw_list = named_ranges_obj.get("namedRanges", [])
            if isinstance(raw_list, list):
                nr_list = [item for item in raw_list if isinstance(item, dict)]
            else:
                nr_list = [named_ranges_obj]
        elif isinstance(named_ranges_obj, list):
            nr_list = [item for item in named_ranges_obj if isinstance(item, dict)]
        else:
            nr_list = []

        min_start: int | None = None
        max_end: int | None = None
        ids: list[str] = []

        for nr in nr_list:
            nr_id = nr.get("namedRangeId")
            if isinstance(nr_id, str):
                ids.append(nr_id)

            ranges = nr.get("ranges", [])
            if not isinstance(ranges, list):
                continue
            for range_obj in ranges:
                if not isinstance(range_obj, dict):
                    continue
                start = range_obj.get("startIndex")
                end = range_obj.get("endIndex")
                if isinstance(start, int) and isinstance(end, int):
                    min_start = start if min_start is None else min(min_start, start)
                    max_end = end if max_end is None else max(max_end, end)

        result[name] = NamedRangeInfo(name=name, start=min_start, end=max_end, ids=ids)

    return result


def get_named_ranges(service: Any, doc_id: str) -> dict[str, dict[str, Any]]:
    """
    Fetch the document and extract valid notion_page_* named ranges.

    Returns a backward-compatible mapping of name to start/end/ids.
    """
    retry_config = RetryConfig.from_env()
    doc = retry_call(
        lambda: service.documents().get(documentId=doc_id).execute(),
        config=retry_config,
        logger=logger,
        operation_name="Google Docs get named ranges",
    )

    result: dict[str, dict[str, Any]] = {}
    doc_length = _document_length(doc)
    for name, info in _collect_named_ranges(doc).items():
        if info.is_valid and info.end is not None and info.end <= doc_length:
            result[name] = {"start": info.start, "end": info.end, "ids": info.ids}
    return result


def _delete_named_range_requests(ids: list[str]) -> list[dict[str, Any]]:
    return [{"deleteNamedRange": {"namedRangeId": nr_id}} for nr_id in ids]


def build_upsert_plan(
    service: Any,
    doc_id: str,
    page_id: str,
    build_content_reqs_fn: Callable[[int], tuple[list[dict[str, Any]], int]],
    doc_length_fn: Callable[[Any, str], int],
) -> UpsertPlan:
    """
    Build requests to replace an existing page section or append a new one.
    """
    range_name = range_name_for_page(page_id)
    retry_config = RetryConfig.from_env()
    doc = retry_call(
        lambda: service.documents().get(documentId=doc_id).execute(),
        config=retry_config,
        logger=logger,
        operation_name="Google Docs get document for upsert",
    )
    doc_length = _document_length(doc)
    insert_at_end = max(doc_length - 1, 1)

    named_ranges = _collect_named_ranges(doc)
    existing = named_ranges.get(range_name)
    requests: list[dict[str, Any]] = []
    action = "append"
    repaired_ids: list[str] = []

    if existing:
        if existing.ids:
            requests.extend(_delete_named_range_requests(existing.ids))

        if existing.is_valid and existing.end is not None and existing.end <= doc_length:
            start = int(existing.start)
            end = int(existing.end)
            logger.info(
                "Found existing section for page %s at range [%d, %d]. Replacing.",
                page_id,
                start,
                end,
            )
            requests.append(
                {"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}}
            )
            insert_index = start
            action = "replace"
        else:
            repaired_ids = existing.ids
            logger.warning(
                "Named range for page %s is stale or malformed. Deleting anchor and appending.",
                page_id,
            )
            insert_index = insert_at_end
            action = "repair_append"
    else:
        # Keep the injected doc_length_fn for compatibility and existing tests.
        insert_index = max(doc_length_fn(service, doc_id) - 1, 1)
        logger.info("Page %s not found in document. Appending to end at index %d.", page_id, insert_index)

    content_reqs, end_index = build_content_reqs_fn(insert_index)
    requests.extend(content_reqs)

    if end_index > insert_index:
        requests.append(
            {
                "createNamedRange": {
                    "name": range_name,
                    "range": {"startIndex": insert_index, "endIndex": end_index},
                }
            }
        )

    return UpsertPlan(
        requests=requests,
        range_name=range_name,
        action=action,
        insert_index=insert_index,
        repaired_named_range_ids=repaired_ids,
    )


def build_upsert_requests(
    service: Any,
    doc_id: str,
    page_id: str,
    build_content_reqs_fn: Callable[[int], tuple[list[dict[str, Any]], int]],
    doc_length_fn: Callable[[Any, str], int],
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper returning only Google Docs requests."""
    return build_upsert_plan(
        service=service,
        doc_id=doc_id,
        page_id=page_id,
        build_content_reqs_fn=build_content_reqs_fn,
        doc_length_fn=doc_length_fn,
    ).requests
