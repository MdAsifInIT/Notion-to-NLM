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

TOKEN_PATH = Path("token.json")


def _get_credentials(credentials_file: str) -> Credentials:
    """
    Return valid Google OAuth 2.0 credentials.

    On first run this opens a browser-based consent flow and persists the
    refresh token to ``token.json``.  Subsequent runs reuse the cached token,
    refreshing it transparently when it expires.
    """
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

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
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)

        # Persist for next run.
        TOKEN_PATH.write_text(creds.to_json())
        logger.info("Google credentials saved to %s", TOKEN_PATH)

    return creds


def _get_doc_length(service: Any, doc_id: str) -> int:
    """Return the current end-of-body index of the document."""
    doc = service.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {})
    content = body.get("content", [])
    if content:
        return content[-1].get("endIndex", 1)
    return 1


def append_to_doc(
    credentials_file: str,
    doc_id: str,
    entries: list[dict[str, str]],
) -> None:
    """
    Append one or more text entries to the end of a Google Doc.

    Each entry is a dict with keys ``title``, ``body``, and ``last_edited``.
    A divider header is prepended before each entry for readability.
    """
    if not entries:
        logger.info("Nothing to append — entry list is empty.")
        return

    creds = _get_credentials(credentials_file)
    service = build("docs", "v1", credentials=creds)

    # Build the full text payload ─────────────────────────────────────────
    sections: list[str] = []
    for entry in entries:
        title = entry.get("title", "Untitled")
        edited = entry.get("last_edited", "N/A")
        body = entry.get("body", "").strip()

        divider = f"\n\n{'━' * 60}\n📝  {title}  |  {edited}\n{'━' * 60}\n\n"
        sections.append(divider + body + "\n")

    full_text = "".join(sections)

    # Determine insertion index (end of document) ─────────────────────────
    try:
        end_index = _get_doc_length(service, doc_id)

        # Google Docs indexes are 1-based; content always ends with a
        # trailing newline whose index == endIndex.  Insert just before it.
        insert_index = max(end_index - 1, 1)

        requests = [
            {
                "insertText": {
                    "location": {"index": insert_index},
                    "text": full_text,
                }
            }
        ]

        service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests},
        ).execute()

        logger.info(
            "Successfully appended %d entry/entries (%d chars) to Google Doc %s.",
            len(entries),
            len(full_text),
            doc_id,
        )

    except HttpError as exc:
        logger.error("Google Docs API error: %s", exc)
        raise
