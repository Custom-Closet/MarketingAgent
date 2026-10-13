"""SQLite persistence for leads, outreach messages, and the suppression list.

Lifecycle of a message row:

    pending  -> approved -> sent
             -> rejected
    (sent emails are later marked replied / bounced by `sync`)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY,
    name            TEXT,
    email           TEXT UNIQUE,
    email_status    TEXT,              -- Hunter verifier result: valid / accept_all / ...
    company         TEXT,
    domain          TEXT,
    position        TEXT,
    ig_username     TEXT UNIQUE,
    ig_bio          TEXT,
    ig_followers    INTEGER,
    website         TEXT,
    source          TEXT NOT NULL,     -- hunter / instagram / csv
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'new',  -- new / contacted / replied / bounced / unsubscribed
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    lead_id         INTEGER NOT NULL REFERENCES leads(id),
    channel         TEXT NOT NULL,     -- email / instagram
    kind            TEXT NOT NULL,     -- initial / followup / reply
    step            INTEGER NOT NULL DEFAULT 0,
    subject         TEXT,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / sent / failed
    thread_id       TEXT,              -- Gmail thread, or IG conversation recipient id
    rfc_message_id  TEXT,              -- Gmail Message-ID header, used to thread follow-ups
    error           TEXT,
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);

CREATE TABLE IF NOT EXISTS targets (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,     -- domain / ig_user / hashtag
    value           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / done / failed
    result          TEXT,
    created_at      TEXT NOT NULL,
    processed_at    TEXT,
    UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS suppression (
    value           TEXT PRIMARY KEY,  -- email address or IG username, lower-cased
    reason          TEXT,
    created_at      TEXT NOT NULL
);
"""

LEAD_FIELDS = {
    "name", "email", "email_status", "company", "domain", "position", "ig_username",
    "ig_bio", "ig_followers", "website", "source", "notes", "status",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    # ---- leads -----------------------------------------------------------

    def upsert_lead(self, **fields: Any) -> int:
        """Insert a lead, or merge non-empty fields into an existing one matched by email / IG username."""
        fields = {k: v for k, v in fields.items() if k in LEAD_FIELDS and v not in (None, "")}
        if fields.get("email"):
            fields["email"] = fields["email"].lower()
        if fields.get("ig_username"):
            fields["ig_username"] = fields["ig_username"].lower().lstrip("@")

        existing = None
        for key in ("email", "ig_username"):
            if fields.get(key):
                existing = self.conn.execute(
                    f"SELECT id FROM leads WHERE {key} = ?", (fields[key],)
                ).fetchone()
                if existing:
                    break

        if existing:
            updates = {k: v for k, v in fields.items() if k not in ("source", "status")}
            if updates:
                assignments = ", ".join(f"{k} = COALESCE({k}, ?)" for k in updates)
                self.conn.execute(
                    f"UPDATE leads SET {assignments} WHERE id = ?", (*updates.values(), existing["id"])
                )
                self.conn.commit()
            return existing["id"]

        fields.setdefault("source", "manual")
        fields["created_at"] = now_iso()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(f"INSERT INTO leads ({cols}) VALUES ({marks})", tuple(fields.values()))
        self.conn.commit()
        return cur.lastrowid

    def get_lead(self, lead_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()

    def update_lead(self, lead_id: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in LEAD_FIELDS}
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE leads SET {assignments} WHERE id = ?", (*fields.values(), lead_id))
        self.conn.commit()

    def leads_needing_email(self) -> list[sqlite3.Row]:
        """Leads with a known domain but no email yet (e.g. Instagram profiles with a website)."""
        return self.conn.execute(
            "SELECT * FROM leads WHERE email IS NULL AND domain IS NOT NULL AND status = 'new'"
        ).fetchall()

    def leads_without_draft(self, channel: str, limit: int) -> list[sqlite3.Row]:
        """New, contactable leads that have no message on `channel` yet."""
        contact_col = "email" if channel == "email" else "ig_username"
        extra = "AND (l.email_status IS NULL OR l.email_status IN ('valid', 'accept_all'))" if channel == "email" else ""
        return self.conn.execute(
            f"""
            SELECT l.* FROM leads l
            WHERE l.status = 'new' AND l.{contact_col} IS NOT NULL {extra}
              AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.lead_id = l.id AND m.channel = ?)
              AND lower(l.{contact_col}) NOT IN (SELECT value FROM suppression)
            ORDER BY l.id LIMIT ?
            """,
            (channel, limit),
        ).fetchall()

    def counts(self) -> dict[str, dict[str, int]]:
        def group(sql: str) -> dict[str, int]:
            return {row[0]: row[1] for row in self.conn.execute(sql)}

        return {
            "leads": group("SELECT status, COUNT(*) FROM leads GROUP BY status"),
            "email": group("SELECT status, COUNT(*) FROM messages WHERE channel='email' GROUP BY status"),
            "instagram": group("SELECT status, COUNT(*) FROM messages WHERE channel='instagram' GROUP BY status"),
            "targets": group("SELECT status, COUNT(*) FROM targets GROUP BY status"),
        }

    # ---- messages --------------------------------------------------------

    def add_message(self, lead_id: int, channel: str, body: str, *, kind: str = "initial",
                    step: int = 0, subject: str | None = None, thread_id: str | None = None,
                    rfc_message_id: str | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO messages (lead_id, channel, kind, step, subject, body, thread_id,
                                     rfc_message_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, channel, kind, step, subject, body, thread_id, rfc_message_id, now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def messages(self, *, status: str, channel: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        sql = """SELECT m.*, l.name, l.email, l.company, l.ig_username
                 FROM messages m JOIN leads l ON l.id = m.lead_id WHERE m.status = ?"""
        params: list[Any] = [status]
        if channel:
            sql += " AND m.channel = ?"
            params.append(channel)
        sql += " ORDER BY m.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def update_message(self, message_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE messages SET {assignments} WHERE id = ?", (*fields.values(), message_id))
        self.conn.commit()

    def mark_sent(self, message_id: int, *, thread_id: str | None = None,
                  rfc_message_id: str | None = None) -> None:
        row = self.conn.execute("SELECT lead_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        self.conn.execute(
            """UPDATE messages SET status = 'sent', sent_at = ?, error = NULL,
                   thread_id = COALESCE(?, thread_id), rfc_message_id = COALESCE(?, rfc_message_id)
               WHERE id = ?""",
            (now_iso(), thread_id, rfc_message_id, message_id),
        )
        self.conn.execute("UPDATE leads SET status = 'contacted' WHERE id = ? AND status = 'new'", (row["lead_id"],))
        self.conn.commit()

    def emails_sent_since(self, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel = 'email' AND status = 'sent' AND sent_at >= ?",
            (since.isoformat(timespec="seconds"),),
        ).fetchone()[0]

    def sent_email_threads(self) -> list[sqlite3.Row]:
        """Latest sent email per lead that is still awaiting a response."""
        return self.conn.execute(
            """
            SELECT m.*, l.email, l.name, l.company FROM messages m JOIN leads l ON l.id = m.lead_id
            WHERE m.channel = 'email' AND m.status = 'sent' AND l.status = 'contacted'
              AND m.step = (SELECT MAX(step) FROM messages x
                            WHERE x.lead_id = m.lead_id AND x.channel = 'email' AND x.status = 'sent')
            """
        ).fetchall()

    def followups_due(self, after_days: int, max_followups: int) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=after_days)).isoformat(timespec="seconds")
        return [
            row for row in self.sent_email_threads()
            if row["sent_at"] <= cutoff and row["step"] < max_followups
            and not self.conn.execute(
                "SELECT 1 FROM messages WHERE lead_id = ? AND channel = 'email' AND step > ?",
                (row["lead_id"], row["step"]),
            ).fetchone()
        ]

    def thread_history(self, lead_id: int, channel: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE lead_id = ? AND channel = ? AND status = 'sent' ORDER BY step",
            (lead_id, channel),
        ).fetchall()

    # ---- discovery targets (processed by the daily job) ---------------------

    def add_target(self, kind: str, value: str) -> bool:
        value = value.strip().lower().lstrip("@#")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO targets (kind, value, created_at) VALUES (?, ?, ?)", (kind, value, now_iso())
        )
        self.conn.commit()
        return cur.rowcount == 1

    def pending_targets(self, limit: int, kinds: Iterable[str]) -> list[sqlite3.Row]:
        kinds = list(kinds)
        marks = ", ".join("?" for _ in kinds)
        return self.conn.execute(
            f"SELECT * FROM targets WHERE status = 'pending' AND kind IN ({marks}) ORDER BY id LIMIT ?",
            (*kinds, limit),
        ).fetchall()

    def finish_target(self, target_id: int, status: str, result: str) -> None:
        self.conn.execute(
            "UPDATE targets SET status = ?, result = ?, processed_at = ? WHERE id = ?",
            (status, result, now_iso(), target_id),
        )
        self.conn.commit()

    def targets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM targets ORDER BY id DESC LIMIT 100").fetchall()

    # ---- suppression -----------------------------------------------------

    def suppress(self, value: str, reason: str) -> None:
        value = value.lower().lstrip("@")
        self.conn.execute(
            "INSERT OR IGNORE INTO suppression (value, reason, created_at) VALUES (?, ?, ?)",
            (value, reason, now_iso()),
        )
        self.conn.execute(
            "UPDATE leads SET status = 'unsubscribed' WHERE lower(email) = ? OR ig_username = ?", (value, value)
        )
        # Drop anything queued for this contact.
        self.conn.execute(
            """UPDATE messages SET status = 'rejected', error = 'suppressed'
               WHERE status IN ('pending', 'approved') AND lead_id IN
                 (SELECT id FROM leads WHERE lower(email) = ? OR ig_username = ?)""",
            (value, value),
        )
        self.conn.commit()

    def is_suppressed(self, values: Iterable[str | None]) -> bool:
        for value in values:
            if value and self.conn.execute(
                "SELECT 1 FROM suppression WHERE value = ?", (value.lower().lstrip("@"),)
            ).fetchone():
                return True
        return False
