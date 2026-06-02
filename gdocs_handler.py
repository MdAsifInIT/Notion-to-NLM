"""
gdocs_handler.py
────────────────
Authenticates with Google APIs and appends formatted text to a Google Doc.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# If modifying scopes, delete the existing token.json so a new one is issued.
SCOPES = ["https://www.googleapis.com/auth/documents"]


def _get_credentials(credentials_file: str) -> Credentials:
    """
    Return valid Google OAuth 2.0 credentials.
    On first run, this handles either local server flow, console flow,
    or auto fallback depending on GOOGLE_AUTH_MODE environment variable.
    Persists token to TOKEN_FILE (default: token.json).
    """
    creds: Credentials | None = None
    token_path_str = os.getenv("TOKEN_FILE", "token.json")
    token_path = Path(token_path_str)

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:
            logger.warning("Failed to load token from %s: %s", token_path_str, exc)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Google token …")
            try:
                creds.refresh(Request())
            except Exception:
                logger.warning("Token refresh failed — re-authenticating.")
                creds = None

        if creds is None:
            if not os.path.exists(credentials_file):
                raise FileNotFoundError(
                    f"Google credentials file not found: {credentials_file}. "
                    "Download it from the Google Cloud Console."
                )

            auth_mode = os.getenv("GOOGLE_AUTH_MODE", "auto").lower()
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)

            def run_server():
                logger.info("Starting local server flow...")
                return flow.run_local_server(port=0)

            def run_console_flow():
                logger.info("Starting manual console authorization flow...")
                flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
                auth_url, _ = flow.authorization_url(prompt="consent")
                print("\n" + "="*80)
                print("GOOGLE OAUTH AUTHORIZATION REQUIRED")
                print("="*80)
                print("1. Go to the following URL in your browser:")
                print(f"\n   {auth_url}\n")
                print("2. Authorize the application and copy the code provided.")
                print("3. Paste the authorization code below:")
                print("="*80 + "\n")
                
                try:
                    code = input("Enter authorization code: ").strip()
                except KeyboardInterrupt:
                    print("\nAuthorization cancelled by user.")
                    raise
                
                flow.fetch_token(code=code)
                return flow.credentials

            if auth_mode == "console":
                creds = run_console_flow()
            elif auth_mode == "local_server":
                creds = run_server()
            else:  # "auto"
                try:
                    creds = run_server()
                except Exception as exc:
                    logger.warning("Local server flow failed: %s. Falling back to console flow.", exc)
                    creds = run_console_flow()

        # Persist for next run.
        token_path.write_text(creds.to_json())
        logger.info("Google credentials saved to %s", token_path_str)

    return creds


def _get_doc_length(service: Any, doc_id: str) -> int:
    """Return the current end-of-body index of the document."""
    doc = service.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {})
    content = body.get("content", [])
    if content:
        return content[-1].get("endIndex", 1)
    return 1


def _build_entry_requests(entry: dict[str, Any], start_index: int) -> tuple[list[dict[str, Any]], int]:
    """
    Build a list of Google Docs batchUpdate requests for a single entry.
    Returns the list of requests and the new end index.
    """
    requests = []
    current_index = start_index

    title = entry.get("title", "Untitled")
    edited = entry.get("last_edited", "N/A")

    # 1. Title/Header (styled as HEADING_2)
    title_text = f"\n\n📝  {title}  |  {edited}\n"
    requests.append({
        "insertText": {
            "location": {"index": current_index},
            "text": title_text
        }
    })
    requests.append({
        "updateParagraphStyle": {
            "range": {
                "startIndex": current_index + 2,
                "endIndex": current_index + len(title_text)
            },
            "paragraphStyle": {
                "namedStyleType": "HEADING_2"
            },
            "fields": "namedStyleType"
        }
    })
    current_index += len(title_text)

    # 2. Properties Block (Phase 1B)
    properties = entry.get("properties", [])
    if properties:
        header_line = "── Properties ──────────────────────────────────────────\n"
        requests.append({
            "insertText": {
                "location": {"index": current_index},
                "text": header_line
            }
        })
        requests.append({
            "updateTextStyle": {
                "range": {
                    "startIndex": current_index,
                    "endIndex": current_index + len(header_line)
                },
                "textStyle": {"italic": True},
                "fields": "italic"
            }
        })
        current_index += len(header_line)

        for prop in properties:
            name = prop.get("name", "")
            value = prop.get("value", "")
            prop_line = f"  •  {name}: {value}\n"
            
            requests.append({
                "insertText": {
                    "location": {"index": current_index},
                    "text": prop_line
                }
            })
            
            # Style the label "name:" as bold
            name_start = current_index + 5  # after "  •  "
            name_end = name_start + len(name) + 1  # include colon
            requests.append({
                "updateTextStyle": {
                    "range": {
                        "startIndex": name_start,
                        "endIndex": name_end
                    },
                    "textStyle": {"bold": True},
                    "fields": "bold"
                }
            })
            current_index += len(prop_line)

        footer_line = "────────────────────────────────────────────────────────\n\n"
        requests.append({
            "insertText": {
                "location": {"index": current_index},
                "text": footer_line
            }
        })
        requests.append({
            "updateTextStyle": {
                "range": {
                    "startIndex": current_index,
                    "endIndex": current_index + len(footer_line)
                },
                "textStyle": {"italic": True},
                "fields": "italic"
            }
        })
        current_index += len(footer_line)

    # 3. Content Blocks (Phase 1A)
    blocks = entry.get("blocks", [])
    if blocks:
        for block in blocks:
            if hasattr(block, "text"):
                b_text = block.text
                b_type = block.block_type
                b_annotations = block.annotations
            else:
                b_text = block.get("text", "")
                b_type = block.get("block_type", "")
                b_annotations = block.get("annotations", [])

            if not b_text.strip() and b_type != "divider":
                continue

            text_to_insert = b_text + "\n"
            block_start = current_index
            requests.append({
                "insertText": {
                    "location": {"index": current_index},
                    "text": text_to_insert
                }
            })
            block_end = current_index + len(text_to_insert)

            if b_type in ("heading_1", "heading_2", "heading_3"):
                style_name = b_type.upper()
                requests.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": block_start, "endIndex": block_end},
                        "paragraphStyle": {"namedStyleType": style_name},
                        "fields": "namedStyleType"
                    }
                })
            elif b_type == "bulleted_list_item":
                requests.append({
                    "createParagraphBullets": {
                        "range": {"startIndex": block_start, "endIndex": block_end},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"
                    }
                })
            elif b_type == "numbered_list_item":
                requests.append({
                    "createParagraphBullets": {
                        "range": {"startIndex": block_start, "endIndex": block_end},
                        "bulletPreset": "NUMBERED_DECIMAL_NESTED"
                    }
                })

            for ann in b_annotations:
                ann_start = block_start + ann["start"]
                ann_end = block_start + ann["end"]

                if ann_start < block_start or ann_end > block_end:
                    continue

                text_style = {}
                fields = []
                if ann.get("bold"):
                    text_style["bold"] = True
                    fields.append("bold")
                if ann.get("italic"):
                    text_style["italic"] = True
                    fields.append("italic")
                if ann.get("underline"):
                    text_style["underline"] = True
                    fields.append("underline")
                if ann.get("strikethrough"):
                    text_style["strikethrough"] = True
                    fields.append("strikethrough")
                if ann.get("code"):
                    text_style["weightedFontFamily"] = {"fontFamily": "Courier New"}
                    fields.append("weightedFontFamily")

                if text_style:
                    requests.append({
                        "updateTextStyle": {
                            "range": {"startIndex": ann_start, "endIndex": ann_end},
                            "textStyle": text_style,
                            "fields": ",".join(fields)
                        }
                    })

            current_index = block_end
    else:
        body = entry.get("body", "").strip()
        if body:
            text_to_insert = body + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": current_index},
                    "text": text_to_insert
                }
            })
            current_index += len(text_to_insert)

    return requests, current_index


def sync_to_doc(
    credentials_file: str,
    doc_id: str,
    entries: list[dict[str, Any]],
) -> None:
    """
    Sync one or more Notion entries into a Google Doc.
    Uses upsert logic with Named Ranges to avoid duplicating content.
    """
    if not entries:
        logger.info("Nothing to sync — entry list is empty.")
        return

    creds = _get_credentials(credentials_file)
    service = build("docs", "v1", credentials=creds)

    import sync_engine

    try:
        for entry in entries:
            page_id = entry.get("page_id")
            if not page_id:
                end_index = _get_doc_length(service, doc_id)
                insert_index = max(end_index - 1, 1)
                reqs, _ = _build_entry_requests(entry, insert_index)
                if reqs:
                    service.documents().batchUpdate(
                        documentId=doc_id,
                        body={"requests": reqs},
                    ).execute()
                continue

            def build_reqs_fn(start_idx):
                return _build_entry_requests(entry, start_idx)

            requests = sync_engine.build_upsert_requests(
                service=service,
                doc_id=doc_id,
                page_id=page_id,
                build_content_reqs_fn=build_reqs_fn,
                doc_length_fn=_get_doc_length,
            )

            if requests:
                service.documents().batchUpdate(
                    documentId=doc_id,
                    body={"requests": requests},
                ).execute()

        logger.info(
            "Successfully synced %d entry/entries to Google Doc %s.",
            len(entries),
            doc_id,
        )

    except HttpError as exc:
        logger.error("Google Docs API error: %s", exc)
        raise


def append_to_doc(
    credentials_file: str,
    doc_id: str,
    entries: list[dict[str, Any]],
) -> None:
    """Backward-compatible wrapper that delegates to sync_to_doc."""
    sync_to_doc(credentials_file, doc_id, entries)
