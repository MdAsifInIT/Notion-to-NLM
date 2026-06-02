"""
sync_engine.py
──────────────
Handles query, deletion, and insertion orchestration for idempotent upserts
in Google Docs using Named Ranges as anchors.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_named_ranges(service: Any, doc_id: str) -> dict[str, dict[str, Any]]:
    """
    Fetch the document and extract all notion_page_* named ranges.
    Returns a dict mapping name to range info:
        {
            "notion_page_<uuid>": {
                "start": int,
                "end": int,
                "ids": list[str]
            }
        }
    """
    doc = service.documents().get(documentId=doc_id).execute()
    named_ranges_map = doc.get("namedRanges", {})

    result = {}
    for name, named_ranges_obj in named_ranges_map.items():
        if not name.startswith("notion_page_"):
            continue

        nr_list = named_ranges_obj.get("namedRanges", [])
        if not nr_list:
            if isinstance(named_ranges_obj, list):
                nr_list = named_ranges_obj
            elif isinstance(named_ranges_obj, dict):
                nr_list = [named_ranges_obj]

        min_start = None
        max_end = None
        ids = []

        for nr in nr_list:
            nr_id = nr.get("namedRangeId")
            if nr_id:
                ids.append(nr_id)
            ranges = nr.get("ranges", [])
            for r in ranges:
                start = r.get("startIndex")
                end = r.get("endIndex")
                if start is not None and end is not None:
                    if min_start is None or start < min_start:
                        min_start = start
                    if max_end is None or end > max_end:
                        max_end = end

        if min_start is not None and max_end is not None:
            result[name] = {
                "start": min_start,
                "end": max_end,
                "ids": ids
            }

    return result


def build_upsert_requests(
    service: Any,
    doc_id: str,
    page_id: str,
    build_content_reqs_fn: Any,
    doc_length_fn: Any,
) -> list[dict[str, Any]]:
    """
    Determine if the page exists in the document.
    Generate a sequence of requests to:
      1. Delete existing content & named range (if it exists).
      2. Insert the new content.
      3. Create a new named range spanning the new content.
    """
    normalized_id = page_id.replace("-", "")
    range_name = f"notion_page_{normalized_id}"

    named_ranges = get_named_ranges(service, doc_id)
    existing = named_ranges.get(range_name)

    requests = []

    if existing:
        start = existing["start"]
        end = existing["end"]
        ids = existing["ids"]

        logger.info("Found existing section for page %s at range [%d, %d]. Replacing.", page_id, start, end)

        # Delete content range
        # Google Docs ranges are half-open: [start, end)
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": start,
                    "endIndex": end
                }
            }
        })

        # Delete old named ranges
        for nr_id in ids:
            requests.append({
                "deleteNamedRange": {
                    "namedRangeId": nr_id
                }
            })

        insert_index = start
    else:
        # Determine insertion index (end of document)
        end_index = doc_length_fn(service, doc_id)
        # Content always ends with a trailing newline. Insert just before it.
        insert_index = max(end_index - 1, 1)
        logger.info("Page %s not found in document. Appending to end at index %d.", page_id, insert_index)

    # Generate insertion and styling requests
    content_reqs, end_index = build_content_reqs_fn(insert_index)
    requests.extend(content_reqs)

    # Create the new named range spanning the entire inserted content
    # Note: the insert text starts at insert_index, and ends at end_index.
    # The range is half-open: [insert_index, end_index)
    if end_index > insert_index:
        requests.append({
            "createNamedRange": {
                "name": range_name,
                "range": {
                    "startIndex": insert_index,
                    "endIndex": end_index
                }
            }
        })

    return requests
