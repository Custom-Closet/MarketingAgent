"""Inbox queries over the agent database. SQL lives here; rules live in service.py."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..db import Database, now_iso
from .faq import FAQ

OPEN_DRAFT_STATUSES = ("pending", "approved")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class InboxRepository:
    def __init__(self, db: Database):
        self.db = db
        self.conn = db.conn

    # ---- threads ------------------------------------------------------------

    def threads_awaiting_reply(self, our_id: str, since: datetime) -> list[sqlite3.Row]:
        """Conversations whose newest stored message is from the customer and at/after `since`."""
        return self.conn.execute(
            """SELECT c.id, c.igsid, c.username, last.created_time AS last_inbound_at
               FROM ig_conversations c
               JOIN ig_messages last ON last.conversation_id = c.id
               WHERE last.created_time = (SELECT MAX(created_time) FROM ig_messages WHERE conversation_id = c.id)
                 AND (last.from_id IS NULL OR last.from_id != ?) AND last.created_time >= ?
               GROUP BY c.id ORDER BY last.created_time DESC""",
            (our_id, _iso(since))).fetchall()

    def unanswered_messages(self, conversation_id: str, our_id: str) -> list[sqlite3.Row]:
        """Customer messages after our most recent message, oldest first."""
        return self.conn.execute(
            """SELECT * FROM ig_messages WHERE conversation_id = ? AND created_time >
                   COALESCE((SELECT MAX(created_time) FROM ig_messages WHERE conversation_id = ? AND from_id = ?), '')
               ORDER BY created_time""",
            (conversation_id, conversation_id, our_id)).fetchall()

    def customer_text(self, conversation_id: str, our_id: str) -> str:
        """Everything the customer has written in this conversation, for intent signals."""
        rows = self.conn.execute(
            "SELECT text FROM ig_messages WHERE conversation_id = ? AND from_id != ? AND text IS NOT NULL",
            (conversation_id, our_id)).fetchall()
        return " ".join(r["text"] for r in rows)

    def latest_message(self, conversation_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ig_messages WHERE conversation_id = ? ORDER BY created_time DESC LIMIT 1",
            (conversation_id,)).fetchone()

    def conversation_by_igsid(self, igsid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ig_conversations WHERE igsid = ?", (igsid,)).fetchone()

    # ---- drafts -------------------------------------------------------------

    def open_draft(self, igsid: str) -> sqlite3.Row | None:
        """The pending/approved Instagram reply draft for this customer, if any."""
        return self.conn.execute(
            f"""SELECT * FROM messages WHERE channel = 'instagram' AND kind = 'reply' AND thread_id = ?
                    AND status IN ({",".join("?" * len(OPEN_DRAFT_STATUSES))}) ORDER BY id DESC LIMIT 1""",
            (igsid, *OPEN_DRAFT_STATUSES)).fetchone()

    def open_drafts(self) -> list[sqlite3.Row]:
        """Open Instagram reply drafts joined with the lead and conversation."""
        return self.conn.execute(
            f"""SELECT m.*, l.ig_username, c.id AS conversation_id
                FROM messages m JOIN leads l ON l.id = m.lead_id
                LEFT JOIN ig_conversations c ON c.igsid = m.thread_id
                WHERE m.channel = 'instagram' AND m.kind = 'reply'
                  AND m.status IN ({",".join("?" * len(OPEN_DRAFT_STATUSES))})
                ORDER BY m.id""", OPEN_DRAFT_STATUSES).fetchall()

    def draft(self, message_id: int) -> sqlite3.Row | None:
        """One Instagram reply draft with the fields the send gates need."""
        return self.conn.execute(
            """SELECT m.*, l.ig_username, l.name, l.email, l.company, c.id AS conversation_id
               FROM messages m JOIN leads l ON l.id = m.lead_id
               LEFT JOIN ig_conversations c ON c.igsid = m.thread_id
               WHERE m.id = ? AND m.channel = 'instagram' AND m.kind = 'reply'""", (message_id,)).fetchone()

    def create_draft(self, *, username: str, igsid: str, body: str, reply_deadline: datetime,
                     faq_key: str | None) -> int:
        lead_id = self.db.upsert_lead(ig_username=username, source="instagram")
        return self.db.add_message(lead_id, "instagram", body, kind="reply", thread_id=igsid,
                                   reply_deadline=_iso(reply_deadline), faq_key=faq_key)

    def update_draft(self, message_id: int, **fields) -> None:
        self.db.update_message(message_id, **fields)

    def approve(self, message_id: int) -> None:
        self.db.approve_message(message_id)

    # ---- FAQs -----------------------------------------------------------------

    def seed_faqs(self, faqs: list[FAQ]) -> int:
        """Insert FAQs that aren't stored yet; never overwrites edits made in the inbox."""
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO faqs (key, question, answer, keywords_json, approved, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            [(f.key, f.question, f.answer, json.dumps(list(f.keywords)), now_iso()) for f in faqs])
        self.conn.commit()
        return self.conn.total_changes - before

    def faqs(self) -> list[FAQ]:
        rows = self.conn.execute("SELECT * FROM faqs ORDER BY key").fetchall()
        return [FAQ(key=r["key"], question=r["question"], answer=r["answer"],
                    keywords=tuple(json.loads(r["keywords_json"])), approved=bool(r["approved"])) for r in rows]

    def faq(self, key: str) -> FAQ | None:
        return next((f for f in self.faqs() if f.key == key), None)

    def update_faq(self, key: str, *, answer: str | None = None, approved: bool | None = None,
                   keywords: list[str] | None = None) -> None:
        fields: dict = {"updated_at": now_iso()}
        if answer is not None:
            fields["answer"] = answer
        if approved is not None:
            fields["approved"] = int(approved)
        if keywords is not None:
            fields["keywords_json"] = json.dumps([k.lower() for k in keywords])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE faqs SET {assignments} WHERE key = ?", (*fields.values(), key))
        self.conn.commit()

    # ---- stats ----------------------------------------------------------------

    def sent_replies_since(self, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel = 'instagram' AND kind = 'reply' AND status = 'sent' "
            "AND sent_at >= ?", (_iso(since),)).fetchone()[0]
