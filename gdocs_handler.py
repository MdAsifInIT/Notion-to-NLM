"""
gdocs_handler.py
────────────────
Authenticates with Google APIs and syncs formatted entries to a Google Doc.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from retry_utils import RetryConfig, retry_call

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/documents"]
DEFAULT_BATCH_MAX_REQUESTS = 100


@dataclass(frozen=True)
class SyncResult:
    synced: int = 0
    failed: int = 0
    skipped: int = 0
    failed_page_ids: list[str] = field(default_factory=list)


class SyncError(RuntimeError):
    """Raised when one or more Google Docs entries fail to sync."""

    def __init__(self, result: SyncResult) -> None:
        super().__init__(
            f"Google Docs sync failed for {result.failed} entr"
            f"{'y' if result.failed == 1 else 'ies'}: {', '.join(result.failed_page_ids)}"
        )
        self.result = result


def _get_credentials(credentials_file: str) -> Any:
    """
    Return valid Google OAuth 2.0 credentials.

    Imports Google auth libraries lazily so pure unit tests do not require SDKs.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "Google API auth libraries are required for Docs sync. Install requirements.txt first."
        ) from exc

    creds = None
    token_path = Path(os.getenv("TOKEN_FILE", "token.json"))

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:
            logger.warning("Failed to load token from %s: %s", token_path, exc)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Google token.")
            try:
                retry_call(
                    lambda: creds.refresh(Request()),
                    config=RetryConfig.from_env(),
                    logger=logger,
                    operation_name="Google OAuth token refresh",
                )
            except Exception:
                logger.warning("Token refresh failed; re-authenticating.", exc_info=True)
                creds = None

        if creds is None:
            if not Path(credentials_file).exists():
                raise FileNotFoundError(
                    f"Google credentials file not found: {credentials_file}. "
                    "Download it from the Google Cloud Console."
                )

            auth_mode = os.getenv("GOOGLE_AUTH_MODE", "auto").lower()
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)

            def run_server() -> Any:
                logger.info("Starting local server OAuth flow.")
                return flow.run_local_server(port=0)

            def run_console_flow() -> Any:
                logger.info("Starting manual console OAuth flow.")
                flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
                auth_url, _ = flow.authorization_url(prompt="consent")
                print("\n" + "=" * 80)
                print("GOOGLE OAUTH AUTHORIZATION REQUIRED")
                print("=" * 80)
                print("1. Go to the following URL in your browser:")
                print(f"\n   {auth_url}\n")
                print("2. Authorize the application and copy the code provided.")
                print("3. Paste the authorization code below:")
                print("=" * 80 + "\n")

                try:
                    code = input("Enter authorization code: ").strip()
                except KeyboardInterrupt:
                    print("\nAuthorization cancelled by user.")
                    raise
                except EOFError as exc:
                    raise RuntimeError(
                        "Google OAuth re-authorization is required, but this environment is non-interactive. "
                        "Run once locally to generate token.json, mount a valid token, or publish the OAuth "
                        "consent app so refresh tokens do not expire during unattended Docker runs."
                    ) from exc

                flow.fetch_token(code=code)
                return flow.credentials

            if auth_mode == "console":
                creds = run_console_flow()
            elif auth_mode == "local_server":
                creds = run_server()
            else:
                try:
                    creds = run_server()
                except Exception as exc:
                    logger.warning("Local server flow failed: %s. Falling back to console flow.", exc)
                    creds = run_console_flow()

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info("Google credentials saved to %s", token_path)

    return creds


def _build_docs_service(credentials: Any) -> Any:
    try:
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "google-api-python-client and google-auth-httplib2 are required for Docs sync."
        ) from exc

    retry_config = RetryConfig.from_env()
    http = google_auth_httplib2.AuthorizedHttp(
        credentials,
        http=httplib2.Http(timeout=retry_config.timeout_seconds),
    )
    return build("docs", "v1", http=http, cache_discovery=False)


def _execute_google_request(request: Any, operation_name: str) -> Any:
    return retry_call(
        lambda: request.execute(),
        config=RetryConfig.from_env(),
        logger=logger,
        operation_name=operation_name,
    )


def _get_doc_length(service: Any, doc_id: str) -> int:
    """Return the current end-of-body index of the document."""
    doc = _execute_google_request(
        service.documents().get(documentId=doc_id),
        "Google Docs get document length",
    )
    content = doc.get("body", {}).get("content", [])
    if content:
        return int(content[-1].get("endIndex", 1))
    return 1


def _env_batch_limit() -> int:
    value = os.getenv("GOOGLE_BATCH_MAX_REQUESTS")
    if value is None:
        return DEFAULT_BATCH_MAX_REQUESTS
    try:
        return max(int(value), 1)
    except ValueError:
        logger.warning("Invalid GOOGLE_BATCH_MAX_REQUESTS=%r; using %d.", value, DEFAULT_BATCH_MAX_REQUESTS)
        return DEFAULT_BATCH_MAX_REQUESTS


def _entry_block_parts(block: Any) -> tuple[str, str, list[dict[str, Any]], int]:
    if hasattr(block, "text"):
        return block.text, block.block_type, block.annotations, getattr(block, "depth", 0)
    return (
        str(block.get("text", "")),
        str(block.get("block_type", "")),
        list(block.get("annotations", [])),
        int(block.get("depth", 0) or 0),
    )


def _append_segment(
    chunks: list[str],
    index: int,
    text: str,
) -> tuple[int, int, int]:
    start = index
    chunks.append(text)
    end = start + len(text)
    return start, end, end


def _annotation_style(annotation: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    text_style: dict[str, Any] = {}
    fields: list[str] = []
    if annotation.get("bold"):
        text_style["bold"] = True
        fields.append("bold")
    if annotation.get("italic"):
        text_style["italic"] = True
        fields.append("italic")
    if annotation.get("underline"):
        text_style["underline"] = True
        fields.append("underline")
    if annotation.get("strikethrough"):
        text_style["strikethrough"] = True
        fields.append("strikethrough")
    if annotation.get("code"):
        text_style["weightedFontFamily"] = {"fontFamily": "Courier New"}
        fields.append("weightedFontFamily")
    return text_style, fields


def _build_entry_requests(entry: dict[str, Any], start_index: int) -> tuple[list[dict[str, Any]], int]:
    """
    Build Google Docs batchUpdate requests for a single entry.

    Text is buffered into one insert request, followed by paragraph, bullet, and
    text-style operations using the final document indices.
    """
    title = str(entry.get("title") or "Untitled")
    edited = str(entry.get("last_edited") or "N/A")
    chunks: list[str] = []
    style_requests: list[dict[str, Any]] = []
    bullet_requests: list[dict[str, Any]] = []
    current_index = start_index

    title_text = f"\n\n📝  {title}  |  {edited}\n"
    title_start, title_end, current_index = _append_segment(chunks, current_index, title_text)
    style_requests.append(
        {
            "updateParagraphStyle": {
                "range": {"startIndex": title_start + 2, "endIndex": title_end},
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType",
            }
        }
    )

    properties = entry.get("properties", [])
    if properties:
        header_line = "── Properties ──────────────────────────────────────────\n"
        prop_header_start, prop_header_end, current_index = _append_segment(
            chunks, current_index, header_line
        )
        style_requests.append(
            {
                "updateTextStyle": {
                    "range": {"startIndex": prop_header_start, "endIndex": prop_header_end},
                    "textStyle": {"italic": True},
                    "fields": "italic",
                }
            }
        )

        for prop in properties:
            if not isinstance(prop, dict):
                continue
            name = str(prop.get("name", ""))
            value = str(prop.get("value", ""))
            if not name and not value:
                continue

            prop_line = f"  •  {name}: {value}\n"
            prop_start, _, current_index = _append_segment(chunks, current_index, prop_line)
            name_start = prop_start + 5
            name_end = name_start + len(name) + 1
            if name and name_end > name_start:
                style_requests.append(
                    {
                        "updateTextStyle": {
                            "range": {"startIndex": name_start, "endIndex": name_end},
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        }
                    }
                )

        footer_line = "────────────────────────────────────────────────────────\n\n"
        footer_start, footer_end, current_index = _append_segment(chunks, current_index, footer_line)
        style_requests.append(
            {
                "updateTextStyle": {
                    "range": {"startIndex": footer_start, "endIndex": footer_end},
                    "textStyle": {"italic": True},
                    "fields": "italic",
                }
            }
        )

    blocks = entry.get("blocks", [])
    if blocks:
        for block in blocks:
            block_text, block_type, annotations, depth = _entry_block_parts(block)
            if not block_text.strip() and block_type != "divider":
                continue

            prefix = "  " * max(depth, 0) if block_type in {"bulleted_list_item", "numbered_list_item"} else ""
            text_to_insert = f"{prefix}{block_text}\n"
            block_start, block_end, current_index = _append_segment(chunks, current_index, text_to_insert)
            annotation_offset = len(prefix)

            if block_type in {"heading_1", "heading_2", "heading_3"}:
                style_requests.append(
                    {
                        "updateParagraphStyle": {
                            "range": {"startIndex": block_start, "endIndex": block_end},
                            "paragraphStyle": {"namedStyleType": block_type.upper()},
                            "fields": "namedStyleType",
                        }
                    }
                )
            elif block_type == "bulleted_list_item":
                bullet_requests.append(
                    {
                        "createParagraphBullets": {
                            "range": {"startIndex": block_start, "endIndex": block_end},
                            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                        }
                    }
                )
            elif block_type == "numbered_list_item":
                bullet_requests.append(
                    {
                        "createParagraphBullets": {
                            "range": {"startIndex": block_start, "endIndex": block_end},
                            "bulletPreset": "NUMBERED_DECIMAL_NESTED",
                        }
                    }
                )

            for annotation in annotations:
                if not isinstance(annotation, dict):
                    continue
                ann_start = block_start + annotation_offset + int(annotation.get("start", 0))
                ann_end = block_start + annotation_offset + int(annotation.get("end", 0))
                if ann_start < block_start or ann_end <= ann_start or ann_end > block_end:
                    continue

                text_style, fields = _annotation_style(annotation)
                if text_style:
                    style_requests.append(
                        {
                            "updateTextStyle": {
                                "range": {"startIndex": ann_start, "endIndex": ann_end},
                                "textStyle": text_style,
                                "fields": ",".join(fields),
                            }
                        }
                    )
    else:
        body = str(entry.get("body", "")).strip()
        if body:
            _, _, current_index = _append_segment(chunks, current_index, body + "\n")

    full_text = "".join(chunks)
    if not full_text:
        return [], start_index

    requests: list[dict[str, Any]] = [
        {"insertText": {"location": {"index": start_index}, "text": full_text}}
    ]
    requests.extend(bullet_requests)
    requests.extend(style_requests)
    return requests, current_index


def _execute_batches(service: Any, doc_id: str, requests: list[dict[str, Any]]) -> None:
    if not requests:
        return

    batch_limit = _env_batch_limit()
    for offset in range(0, len(requests), batch_limit):
        batch = requests[offset : offset + batch_limit]
        _execute_google_request(
            service.documents().batchUpdate(documentId=doc_id, body={"requests": batch}),
            f"Google Docs batchUpdate ({offset + 1}-{offset + len(batch)})",
        )


def sync_to_doc(
    credentials_file: str,
    doc_id: str,
    entries: list[dict[str, Any]],
) -> SyncResult:
    """
    Sync one or more Notion entries into a Google Doc.

    Uses upsert logic with Named Ranges to avoid duplicating content.
    """
    if not entries:
        logger.info("Nothing to sync; entry list is empty.")
        return SyncResult(skipped=0)

    creds = _get_credentials(credentials_file)
    service = _build_docs_service(creds)

    import sync_engine

    synced = 0
    failed_page_ids: list[str] = []
    skipped = 0

    for entry in entries:
        page_id = str(entry.get("page_id") or "")
        try:
            if not page_id:
                end_index = _get_doc_length(service, doc_id)
                insert_index = max(end_index - 1, 1)
                requests, _ = _build_entry_requests(entry, insert_index)
                action = "append_without_page_id"
            else:
                plan = sync_engine.build_upsert_plan(
                    service=service,
                    doc_id=doc_id,
                    page_id=page_id,
                    build_content_reqs_fn=lambda start_idx, item=entry: _build_entry_requests(
                        item, start_idx
                    ),
                    doc_length_fn=_get_doc_length,
                )
                requests = plan.requests
                action = plan.action

            if not requests:
                skipped += 1
                logger.info("Skipping entry %s because it produced no Docs requests.", page_id or "<no page id>")
                continue

            _execute_batches(service, doc_id, requests)
            synced += 1
            logger.info("Synced entry %s using action=%s.", page_id or "<no page id>", action)
        except Exception:
            failed_page_ids.append(page_id or "<missing-page-id>")
            logger.exception("Failed to sync entry %s.", page_id or "<missing-page-id>")

    result = SyncResult(
        synced=synced,
        failed=len(failed_page_ids),
        skipped=skipped,
        failed_page_ids=failed_page_ids,
    )

    if result.failed:
        raise SyncError(result)

    logger.info("Successfully synced %d entry/entries to Google Doc %s.", synced, doc_id)
    return result


def append_to_doc(
    credentials_file: str,
    doc_id: str,
    entries: list[dict[str, Any]],
) -> SyncResult:
    """Backward-compatible wrapper that delegates to sync_to_doc."""
    return sync_to_doc(credentials_file, doc_id, entries)
