"""Shared fixtures for the inbox tests: an in-memory database with seeded chats and FAQs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from marketing_agent.db import Database
from marketing_agent.inbox.faq import FAQ
from marketing_agent.inbox.repository import InboxRepository
from marketing_agent.inbox.service import InboxService

US = "1784"   # our Instagram account id


def graph_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


def ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


class FakeIG:
    """InstagramClient stand-in: serves stored conversations and records sends."""

    ig_user_id = US

    def __init__(self):
        self.convos: list[dict] = []
        self.history: dict[str, list[dict]] = {}
        self.sent: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None

    def add_chat(self, cid: str, igsid: str, username: str, messages: list[tuple[str, str, float]]):
        """messages: (sender "us"/"them", text, hours ago)."""
        msgs = [{"id": f"{cid}-{i}", "message": text,
                 "from": {"id": US if who == "us" else igsid, "username": "brand" if who == "us" else username},
                 "created_time": graph_time(ago(h))} for i, (who, text, h) in enumerate(messages)]
        self.history[cid] = msgs
        newest = min(h for _, _, h in messages)
        self.convos.append({"id": cid, "updated_time": graph_time(ago(newest)),
                            "participants": {"data": [{"id": US, "username": "brand"},
                                                      {"id": igsid, "username": username}]}})

    def iter_conversations(self, since, **_):
        from marketing_agent.sources.instagram import parse_time
        for c in sorted(self.convos, key=lambda c: c["updated_time"], reverse=True):
            if parse_time(c["updated_time"]) < since:
                return
            yield c

    def other_participant(self, convo):
        return convo["participants"]["data"][1]

    def messages(self, conversation_id, *, stop_before=None, **_):
        from marketing_agent.sources.instagram import parse_time
        return [m for m in self.history.get(conversation_id, [])
                if not stop_before or parse_time(m["created_time"]) >= stop_before]

    def send_reply(self, recipient_id, text):
        if self.fail_with:
            raise self.fail_with
        self.sent.append((recipient_id, text))
        return f"mid-{len(self.sent)}"


FAQS = [
    FAQ("location", "Where are you based?", "We are based in Jaipur, we deliver pan India",
        ("based", "located", "where are you")),
    FAQ("pricing", "What's the price?", "Prices differ by product - please share the reference image",
        ("price", "pricing", "cost", "how much")),
    FAQ("sample", "Can I get a sample first?", "Sure! Share the reference image; samples take 3-5 days",
        ("sample", "sampling")),
]


def seed_chat(db: Database, cid: str, igsid: str, username: str, messages: list[tuple[str, str | None, float]],
              attachment_at: set[int] = frozenset()):
    """Store a conversation directly. messages: (sender "us"/"them", text, hours ago)."""
    db.save_ig_conversation(cid, igsid=igsid, username=username, updated_time=None)
    rows = []
    for i, (who, text, hours) in enumerate(messages):
        m = {"id": f"{cid}-{i}", "message": text or "", "created_time": graph_time(ago(hours)),
             "from": {"id": US if who == "us" else igsid, "username": username}}
        if i in attachment_at:
            m["attachments"] = {"data": [{"image_data": {"url": "https://example.com/ref.jpg"}}]}
        rows.append(m)
    db.add_ig_messages(cid, rows)


@pytest.fixture
def inbox():
    db = Database(":memory:", check_same_thread=False)   # as in `agent serve`: shared by API threads
    repo = InboxRepository(db)
    repo.seed_faqs(FAQS)
    ig = FakeIG()
    service = InboxService(repo, our_id=US, ig=ig)
    return db, repo, service, ig
