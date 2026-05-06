"""
Thin Gmail API wrapper for Ari.

Handles OAuth refresh transparently and exposes the four operations Ari needs:
  - list_new_threads(after_iso)         → [thread_summary, ...]
  - get_thread(thread_id)               → full thread with messages + attachments
  - create_draft(thread_id, to, subject, body)
                                        → draft_id
  - send_email(to, subject, body)       → message_id

Tokens are persisted to secrets/token.json. credentials.json (the OAuth client
config from Google Cloud) is read from secrets/credentials.json.

Both files live under ari/secrets/ which is gitignored.
"""

from __future__ import annotations
import base64
import html
import json
import os
import pathlib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


SCOPES = [
    # gmail.modify is a superset that covers read, compose, and label modification.
    # We need label modification to mark processed threads as read.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",      # send cover notes to Shaw
]

ARI_ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRETS_DIR = ARI_ROOT / "secrets"
CRED_PATH = SECRETS_DIR / "credentials.json"
TOKEN_PATH = SECRETS_DIR / "token.json"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _load_credentials() -> Credentials:
    """Load a refreshed Credentials object. Raises if no token yet (run auth_setup.py)."""
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"No token at {TOKEN_PATH}. Run `python3 scripts/auth_setup.py` first."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    if not creds or not creds.valid:
        raise RuntimeError(
            "Token is invalid and could not be refreshed. Re-run `python3 scripts/auth_setup.py`."
        )
    return creds


def run_oauth_flow() -> None:
    """One-time interactive flow. Opens a browser, saves the refresh token."""
    if not CRED_PATH.exists():
        raise FileNotFoundError(
            f"OAuth client config missing at {CRED_PATH}.\n"
            "Follow docs/gmail_api_setup.md to download it from Google Cloud."
        )
    SECRETS_DIR.mkdir(exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(CRED_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"Token saved to {TOKEN_PATH}")


def _service():
    creds = _load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def list_new_threads(query: str = "is:unread newer_than:1d -from:me") -> list[dict]:
    """List threads matching the query. Returns the API thread summaries."""
    svc = _service()
    out, page_token = [], None
    while True:
        resp = svc.users().threads().list(
            userId="me", q=query, pageToken=page_token, maxResults=50
        ).execute()
        out.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def get_thread(thread_id: str) -> dict:
    """Fetch full thread including all messages and attachment metadata."""
    svc = _service()
    return svc.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()


def latest_message(thread: dict) -> dict:
    """Return the latest message in a thread."""
    return thread["messages"][-1]


def parse_message(msg: dict) -> dict:
    """Convert a Gmail API message into the email JSON shape ari_pipeline.py expects."""
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    body = _extract_body(msg["payload"])
    attachments = _list_attachments(msg["payload"])
    return {
        "thread_id": msg["threadId"],
        "message_id": msg["id"],
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "date": headers.get("date", ""),
        "subject": headers.get("subject", ""),
        "body": body,
        "attachments": attachments,
    }


def _extract_body(payload: dict) -> str:
    """Walk the MIME tree, prefer text/plain, fall back to text/html stripped of tags."""
    if payload.get("mimeType", "").startswith("text/plain") and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    # Fallback: any text/html or recurse
    for part in payload.get("parts", []) or []:
        if part.get("mimeType", "").startswith("text/html") and part.get("body", {}).get("data"):
            html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            import re
            return re.sub(r"<[^>]+>", "", html)
        nested = _extract_body(part)
        if nested:
            return nested
    return ""


def _list_attachments(payload: dict) -> list[dict]:
    out = []
    for part in payload.get("parts", []) or []:
        filename = part.get("filename")
        mime = part.get("mimeType", "")
        if filename:
            out.append({"filename": filename, "mime_type": mime})
        if part.get("parts"):
            out.extend(_list_attachments(part))
    return out


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def _build_message(to: str, subject: str, body: str, in_reply_to: str | None = None,
                   plain_only: bool = False) -> MIMEMultipart | MIMEText:
    """Build a multipart message with plain + HTML versions, so Gmail wraps it naturally.

    plain_only=True for cover notes to Shaw (we want the monospace structure preserved).
    """
    if plain_only:
        msg = MIMEText(body, "plain")
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        # HTML version: escape, then convert paragraph breaks to <p>, single linebreaks to <br>
        html_body = html.escape(body)
        paragraphs = html_body.split("\n\n")
        html_body = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs if p.strip())
        msg.attach(MIMEText(html_body, "html"))
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    return msg


def create_draft(thread_id: str, to: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
    """Create a Gmail draft on the given thread. Returns draft id. NEVER sends."""
    svc = _service()
    msg = _build_message(to, subject, body, in_reply_to=in_reply_to)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    body_arg: dict[str, Any] = {"message": {"raw": raw, "threadId": thread_id}}
    draft = svc.users().drafts().create(userId="me", body=body_arg).execute()
    return draft["id"]


def send_email(to: str, subject: str, body: str, plain_only: bool = True) -> str:
    """Send a fresh email (used for cover notes to Shaw). Returns message id.

    Cover notes are plain-only by default — they have a monospace structure
    (separators, indented bullets) that's worth preserving as-is.
    """
    svc = _service()
    msg = _build_message(to, subject, body, plain_only=plain_only)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent["id"]


def draft_exists(draft_id: str) -> bool:
    """True if a Gmail draft with this ID is still pending (not sent or deleted)."""
    svc = _service()
    try:
        svc.users().drafts().get(userId="me", id=draft_id).execute()
        return True
    except HttpError as e:
        if e.resp.status == 404:
            return False
        raise


def list_thread_messages(thread_id: str) -> list[dict]:
    """Return all messages in a thread, parsed."""
    svc = _service()
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    return [parse_message(m) for m in thread.get("messages", [])]


# ---------------------------------------------------------------------------
# Quick CLI for verification
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "whoami"
    if cmd == "whoami":
        svc = _service()
        prof = svc.users().getProfile(userId="me").execute()
        print(json.dumps(prof, indent=2))
    elif cmd == "list":
        for t in list_new_threads():
            print(t["id"], t.get("snippet", "")[:80])
    elif cmd == "thread" and len(sys.argv) > 2:
        t = get_thread(sys.argv[2])
        msg = parse_message(latest_message(t))
        print(json.dumps(msg, indent=2))
    else:
        print("usage: gmail_client.py [whoami|list|thread <id>]")
