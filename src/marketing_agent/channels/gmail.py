"""Gmail API channel: send cold emails, thread follow-ups, and detect replies / bounces."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

UNSUBSCRIBE_RE = re.compile(r"\b(unsubscribe|remove me|opt[ -]?out|stop emailing|not interested)\b", re.I)
BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")


@dataclass
class ThreadStatus:
    replied: bool = False
    bounced: bool = False
    unsubscribe: bool = False
    reply_snippet: str = ""


def authorize(credentials_file: Path, token_file: Path) -> Credentials:
    """Run (or refresh) the OAuth installed-app flow and cache the token."""
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not credentials_file.exists():
            raise FileNotFoundError(
                f"{credentials_file} not found. Download an OAuth 'Desktop app' client from Google Cloud Console."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        creds = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    token_file.chmod(0o600)
    return creds


def credentials_from_json(token_json: str) -> Credentials:
    """Non-interactive credentials (Lambda): the authorized-user JSON holds a long-lived refresh token."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return creds


def compliance_footer(physical_address: str) -> str:
    return (
        "\n\n--\n"
        f"{physical_address}\n"
        "Not interested? Reply \"unsubscribe\" and you won't hear from us again."
    )


def build_notification(*, sender_email: str, to: str, subject: str, body: str) -> EmailMessage:
    """Internal email to the team (digests); not outreach, so no compliance footer."""
    msg = EmailMessage()
    msg["From"] = formataddr(("Marketing Agent", sender_email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1])
    msg.set_content(body)
    return msg


def build_email(*, sender_name: str, sender_email: str, to: str, subject: str, body: str,
                physical_address: str, in_reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{sender_email}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body.rstrip() + compliance_footer(physical_address))
    return msg


class GmailChannel:
    def __init__(self, creds: Credentials, sender_email: str):
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.sender_email = sender_email.lower()

    def send(self, msg: EmailMessage, *, thread_id: str | None = None) -> tuple[str, str]:
        """Send a message. Returns (gmail_thread_id, rfc_message_id)."""
        payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            payload["threadId"] = thread_id
        sent = self.service.users().messages().send(userId="me", body=payload).execute()
        return sent["threadId"], msg["Message-ID"]

    def thread_status(self, thread_id: str) -> ThreadStatus:
        thread = self.service.users().threads().get(
            userId="me", id=thread_id, format="metadata", metadataHeaders=["From"]
        ).execute()
        status = ThreadStatus()
        for message in thread.get("messages", []):
            headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            sender = parseaddr(headers.get("from", ""))[1].lower()
            if not sender or sender == self.sender_email:
                continue
            snippet = message.get("snippet", "")
            if sender.startswith(BOUNCE_SENDERS):
                status.bounced = True
                continue
            status.replied = True
            status.reply_snippet = snippet
            if UNSUBSCRIBE_RE.search(snippet):
                status.unsubscribe = True
        return status

    def bounced_recipients(self, newer_than_days: int = 14) -> list[str]:
        """Bounce notices that land outside the original thread. Returns their snippets."""
        query = f"from:mailer-daemon newer_than:{newer_than_days}d"
        listing = self.service.users().messages().list(userId="me", q=query, maxResults=100).execute()
        snippets = []
        for ref in listing.get("messages", []):
            msg = self.service.users().messages().get(userId="me", id=ref["id"], format="minimal").execute()
            snippets.append(msg.get("snippet", ""))
        return snippets
