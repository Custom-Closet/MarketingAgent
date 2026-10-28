"""SQLite persistence for leads, outreach messages, and the suppression list.

Lifecycle of a message row:

    pending  -> approved -> sent
             -> rejected        approved -> expired / failed  (Instagram send-time gates)
    (sent emails are later marked replied / bounced by `sync`)
"""

from __future__ import annotations

import json
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
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / sent / failed / expired
    thread_id       TEXT,              -- Gmail thread, or IG conversation recipient id
    rfc_message_id  TEXT,              -- Gmail Message-ID header, used to thread follow-ups
    error           TEXT,
    created_at      TEXT NOT NULL,
    approved_at     TEXT,
    reply_deadline  TEXT,              -- IG replies: last send time Meta allows (inbound + reply window)
    faq_key         TEXT,              -- IG replies: FAQ whose answer the draft started from
    approved_by     TEXT,              -- user / auto (auto-sent trusted FAQ answer); NULL on older rows = user
    auto_send_at    TEXT,              -- IG replies: when an unchanged trusted FAQ answer goes out on its own
    auto_send_hold  INTEGER NOT NULL DEFAULT 0,  -- 1 = never auto-send this draft (held in the inbox, or a failed try)
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

-- Local archive of Instagram DMs (both directions), filled by `ig-archive` and the daily sync.
CREATE TABLE IF NOT EXISTS ig_conversations (
    id              TEXT PRIMARY KEY,  -- Graph API conversation id
    igsid           TEXT,              -- customer's Instagram-scoped id (reply recipient)
    username        TEXT,
    updated_time    TEXT,              -- last activity per Meta; unchanged means nothing new to fetch
    synced_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ig_messages (
    id              TEXT PRIMARY KEY,  -- Graph API message id
    conversation_id TEXT NOT NULL REFERENCES ig_conversations(id),
    from_id         TEXT,
    from_username   TEXT,
    text            TEXT,
    extras_json     TEXT,              -- attachments / shares / story, when present
    created_time    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ig_messages_by_conversation ON ig_messages (conversation_id, created_time);

-- Reusable answers for common DM questions; only approved ones can be bulk-approved in the inbox.
CREATE TABLE IF NOT EXISTS faqs (
    key             TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    keywords_json   TEXT NOT NULL,     -- lower-case phrases that identify the question
    approved        INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

-- Compressed history of older messages, so Claude sees summary + recent window instead of the whole chat.
CREATE TABLE IF NOT EXISTS ig_summaries (
    conversation_id    TEXT PRIMARY KEY REFERENCES ig_conversations(id),
    summary            TEXT NOT NULL,
    summarized_through TEXT NOT NULL,  -- created_time of the newest message folded into the summary
    updated_at         TEXT NOT NULL
);

-- Funnel stage set by hand in the Sales view; overrides the stage inferred from messages.
CREATE TABLE IF NOT EXISTS funnel_overrides (
    conversation_id TEXT PRIMARY KEY REFERENCES ig_conversations(id),
    stage           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Small key/value settings edited in the app (e.g. context window days).
CREATE TABLE IF NOT EXISTS app_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Seen-ledger of the brand finder (brands/finder.py): every Instagram handle it has looked at.
CREATE TABLE IF NOT EXISTS brand_candidates (
    username        TEXT PRIMARY KEY,  -- lower-case Instagram handle
    status          TEXT NOT NULL,     -- lead / rejected / unavailable / known
    reason          TEXT,              -- why it was rejected, or where a known brand came from
    region          TEXT,              -- country, lower-case ISO code: us / in / gb ...
    name            TEXT,
    followers       INTEGER,
    founder         TEXT,
    website         TEXT,
    bio             TEXT,
    last_post       TEXT,
    signals         TEXT,
    sources         TEXT,              -- how it was found: search / site / hashtag / snowball / seed
    delivered_on    TEXT,              -- run date it was delivered as a lead (never repeated)
    snowballed_at   TEXT,              -- last time its captions were mined for more brands
    checked_at      TEXT NOT NULL,
    search_id       INTEGER,           -- the search run that found it
    query           TEXT,              -- the web search that found it
    stage           TEXT,              -- prospects (status lead): found / shortlisted / contacted / dismissed
    stage_at        TEXT
);

-- Prospecting (Marketing > Prospecting in the app): every search run, the queries it ran, and the
-- versions of the search setup. Candidates found live in brand_candidates (search_id / query / stage).
CREATE TABLE IF NOT EXISTS brand_searches (
    id              INTEGER PRIMARY KEY,
    config_id       INTEGER REFERENCES brand_search_configs(id),
    status          TEXT NOT NULL,     -- running / done / failed
    stats_json      TEXT,
    leads_usa       INTEGER NOT NULL DEFAULT 0,   -- before countries were configurable; see leads_json
    leads_india     INTEGER NOT NULL DEFAULT 0,
    leads_json      TEXT,              -- {"us": 12, "gb": 3, ...}
    stopped         TEXT,              -- why it ended before the quota was met
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS brand_search_queries (
    id              INTEGER PRIMARY KEY,
    search_id       INTEGER NOT NULL REFERENCES brand_searches(id),
    query           TEXT NOT NULL,
    kind            TEXT NOT NULL,     -- search (Instagram results) / site (brand websites)
    region          TEXT,
    results         INTEGER NOT NULL,
    new_candidates  INTEGER NOT NULL,
    ran_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS brand_search_queries_by_query ON brand_search_queries (query);

CREATE TABLE IF NOT EXISTS brand_search_configs (
    id              INTEGER PRIMARY KEY,
    config_json     TEXT NOT NULL,
    note            TEXT,              -- what changed
    created_by      TEXT NOT NULL,     -- user / ai
    created_at      TEXT NOT NULL
);

-- Versions of brand.md saved from Settings (the file itself stays the source of truth).
CREATE TABLE IF NOT EXISTS brand_profile_versions (
    id              INTEGER PRIMARY KEY,
    content         TEXT NOT NULL,
    note            TEXT,
    created_by      TEXT NOT NULL,     -- user / ai / file (what was on disk before the first edit)
    created_at      TEXT NOT NULL
);

-- First use of each hashtag in Meta's rolling 7-day window (max 30 unique hashtags per account).
CREATE TABLE IF NOT EXISTS hashtag_usage (
    tag             TEXT PRIMARY KEY,
    first_used_at   TEXT NOT NULL
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
    def __init__(self, path: Path | str, *, check_same_thread: bool = True):
        """`check_same_thread=False` lets a web server share the connection; callers must serialise access."""
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        # A background brand search writes through its own connection; wait for each other's short writes.
        self.conn = sqlite3.connect(str(path), check_same_thread=check_same_thread, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(messages)")}
        columns = {"approved_at": "TEXT", "reply_deadline": "TEXT", "faq_key": "TEXT", "approved_by": "TEXT",
                   "auto_send_at": "TEXT", "auto_send_hold": "INTEGER NOT NULL DEFAULT 0"}
        for column, kind in columns.items():
            if column not in have:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {kind}")
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(brand_candidates)")}
        for column in ("search_id", "query", "stage", "stage_at"):
            if column not in have:
                kind = "INTEGER" if column == "search_id" else "TEXT"
                self.conn.execute(f"ALTER TABLE brand_candidates ADD COLUMN {column} {kind}")
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(brand_searches)")}
        if "leads_json" not in have:
            self.conn.execute("ALTER TABLE brand_searches ADD COLUMN leads_json TEXT")
        # Regions became ISO country codes.
        self.conn.execute("UPDATE brand_candidates SET region = CASE region WHEN 'usa' THEN 'us' ELSE 'in' END "
                          "WHERE region IN ('usa', 'india')")
        self.conn.execute("""UPDATE brand_searches SET leads_json = json_object('us', leads_usa, 'in', leads_india)
                             WHERE leads_json IS NULL AND status != 'running'""")
        # Brands qualified before prospect stages existed start as "found".
        self.conn.execute("UPDATE brand_candidates SET stage = 'found', stage_at = checked_at "
                          "WHERE status = 'lead' AND stage IS NULL")
        self.conn.commit()

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
                    rfc_message_id: str | None = None, reply_deadline: str | None = None,
                    faq_key: str | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO messages (lead_id, channel, kind, step, subject, body, thread_id,
                                     rfc_message_id, reply_deadline, faq_key, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, channel, kind, step, subject, body, thread_id, rfc_message_id, reply_deadline, faq_key,
             now_iso()),
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

    def approve_message(self, message_id: int, *, by: str = "user") -> None:
        self.update_message(message_id, status="approved", approved_at=now_iso(), approved_by=by)

    def replied_since(self, lead_id: int, since: str) -> bool:
        """True if an Instagram reply was already sent to this lead at or after `since`."""
        return self.conn.execute(
            """SELECT 1 FROM messages WHERE lead_id = ? AND channel = 'instagram' AND kind = 'reply'
                   AND status = 'sent' AND sent_at >= ?""", (lead_id, since)).fetchone() is not None

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

    # ---- Instagram DM archive ----------------------------------------------

    def ig_conversation(self, conversation_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ig_conversations WHERE id = ?", (conversation_id,)).fetchone()

    def save_ig_conversation(self, conversation_id: str, *, igsid: str | None, username: str | None,
                             updated_time: str | None) -> None:
        self.conn.execute(
            """INSERT INTO ig_conversations (id, igsid, username, updated_time, synced_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET igsid = COALESCE(excluded.igsid, igsid),
                   username = COALESCE(excluded.username, username),
                   updated_time = COALESCE(excluded.updated_time, updated_time), synced_at = excluded.synced_at""",
            (conversation_id, igsid, username, updated_time, now_iso()),
        )
        self.conn.commit()

    def add_ig_messages(self, conversation_id: str, messages: Iterable[dict[str, Any]]) -> int:
        """Store Graph API message dicts; already-stored ids are ignored. Returns how many were new."""
        rows = []
        for m in messages:
            extras = {k: m[k] for k in ("attachments", "shares", "story") if m.get(k)}
            for value in extras.values():
                if isinstance(value, dict):
                    value.pop("paging", None)
            created = datetime.strptime(m["created_time"], "%Y-%m-%dT%H:%M:%S%z")
            rows.append((m["id"], conversation_id, m.get("from", {}).get("id"), m.get("from", {}).get("username"),
                         m.get("message") or None, json.dumps(extras) if extras else None,
                         created.astimezone(timezone.utc).isoformat(timespec="seconds")))
        before = self.conn.total_changes
        self.conn.executemany(
            """INSERT OR IGNORE INTO ig_messages (id, conversation_id, from_id, from_username, text, extras_json,
                                                  created_time) VALUES (?, ?, ?, ?, ?, ?, ?)""", rows)
        self.conn.commit()
        return self.conn.total_changes - before

    def ig_latest_message_time(self, conversation_id: str) -> str | None:
        row = self.conn.execute("SELECT MAX(created_time) AS t FROM ig_messages WHERE conversation_id = ?",
                                (conversation_id,)).fetchone()
        return row["t"]

    def ig_transcript(self, conversation_id: str, limit: int | None = None) -> list[sqlite3.Row]:
        """Messages of one conversation, oldest first (the newest `limit` if given)."""
        rows = self.conn.execute(
            "SELECT * FROM ig_messages WHERE conversation_id = ? ORDER BY created_time DESC LIMIT ?",
            (conversation_id, -1 if limit is None else limit)).fetchall()
        return list(reversed(rows))

    def ig_conversations_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT c.* FROM ig_conversations c WHERE EXISTS (SELECT 1 FROM ig_messages m
                   WHERE m.conversation_id = c.id AND m.created_time >= ?) ORDER BY c.updated_time DESC""",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),)).fetchall()

    # ---- summaries, funnel overrides, settings ----------------------------------

    def ig_summary(self, conversation_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ig_summaries WHERE conversation_id = ?",
                                 (conversation_id,)).fetchone()

    def save_ig_summary(self, conversation_id: str, summary: str, summarized_through: str) -> None:
        self.conn.execute(
            """INSERT INTO ig_summaries (conversation_id, summary, summarized_through, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (conversation_id) DO UPDATE SET summary = excluded.summary,
                   summarized_through = excluded.summarized_through, updated_at = excluded.updated_at""",
            (conversation_id, summary, summarized_through, now_iso()))
        self.conn.commit()

    def funnel_overrides(self) -> dict[str, str]:
        return {r["conversation_id"]: r["stage"] for r in self.conn.execute("SELECT * FROM funnel_overrides")}

    def set_funnel_override(self, conversation_id: str, stage: str | None) -> None:
        if stage is None:
            self.conn.execute("DELETE FROM funnel_overrides WHERE conversation_id = ?", (conversation_id,))
        else:
            self.conn.execute(
                """INSERT INTO funnel_overrides (conversation_id, stage, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT (conversation_id) DO UPDATE SET stage = excluded.stage, updated_at = excluded.updated_at""",
                (conversation_id, stage, now_iso()))
        self.conn.commit()

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, now_iso()))
        self.conn.commit()

    def ig_messages_since(self, since: datetime) -> list[sqlite3.Row]:
        """All archived messages of conversations active since `since`, grouped by conversation, oldest first."""
        return self.conn.execute(
            """SELECT m.*, c.username FROM ig_messages m JOIN ig_conversations c ON c.id = m.conversation_id
               WHERE m.conversation_id IN (SELECT conversation_id FROM ig_messages GROUP BY conversation_id
                                           HAVING MAX(created_time) >= ?)
               ORDER BY m.conversation_id, m.created_time""",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),)).fetchall()

    # ---- brand finder ----------------------------------------------------

    def brand_candidate(self, username: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM brand_candidates WHERE username = ?",
                                 (username.lower(),)).fetchone()

    def save_brand_candidate(self, username: str, status: str, **fields: Any) -> None:
        """Insert or replace what is known about a handle; keeps delivered_on / snowballed_at / sources."""
        username = username.lower().lstrip("@")
        old = self.brand_candidate(username)
        if old:
            for keep in ("delivered_on", "snowballed_at", "search_id", "query", "stage", "stage_at"):
                fields.setdefault(keep, old[keep])
            sources = set(filter(None, (old["sources"] or "").split(","))) | set(
                filter(None, (fields.get("sources") or "").split(",")))
            fields["sources"] = ",".join(sorted(sources))
        if status == "lead" and not fields.get("stage"):
            fields["stage"], fields["stage_at"] = "found", now_iso()
        row = {"username": username, "status": status, "checked_at": now_iso(), **fields}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT OR REPLACE INTO brand_candidates ({cols}) VALUES ({marks})", tuple(row.values()))
        self.conn.commit()

    def brand_usernames_checked_since(self, since: datetime) -> set[str]:
        """Handles not worth another lookup: delivered, known from elsewhere, already a lead, or checked after `since`."""
        rows = self.conn.execute(
            """SELECT username FROM brand_candidates
               WHERE status = 'known' OR delivered_on IS NOT NULL OR (checked_at >= ? AND status != 'queued')
               UNION SELECT ig_username FROM leads WHERE ig_username IS NOT NULL
               UNION SELECT value FROM suppression""",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),)).fetchall()
        return {r[0] for r in rows}

    def queued_brand_candidates(self) -> list[sqlite3.Row]:
        """Candidates found by an earlier run that ran out of time before checking them."""
        return self.conn.execute("SELECT * FROM brand_candidates WHERE status = 'queued' ORDER BY checked_at").fetchall()

    def snowball_seeds(self, limit: int, before: datetime) -> list[sqlite3.Row]:
        """Leads and known brands whose captions haven't been mined since `before`, newest first."""
        return self.conn.execute(
            """SELECT * FROM brand_candidates WHERE status IN ('lead', 'known')
               AND (snowballed_at IS NULL OR snowballed_at < ?) ORDER BY checked_at DESC LIMIT ?""",
            (before.astimezone(timezone.utc).isoformat(timespec="seconds"), limit)).fetchall()

    def mark_snowballed(self, username: str) -> None:
        self.conn.execute("UPDATE brand_candidates SET snowballed_at = ? WHERE username = ?",
                          (now_iso(), username.lower()))
        self.conn.commit()

    def mark_delivered(self, usernames: Iterable[str], run_date: str) -> None:
        self.conn.executemany("UPDATE brand_candidates SET delivered_on = ? WHERE username = ?",
                              [(run_date, u.lower()) for u in usernames])
        self.conn.commit()

    def brand_leads(self, *, delivered_on: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if delivered_on:
            return self.conn.execute("SELECT * FROM brand_candidates WHERE delivered_on = ? ORDER BY region, followers",
                                     (delivered_on,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM brand_candidates WHERE delivered_on IS NOT NULL ORDER BY delivered_on DESC, region LIMIT ?",
            (limit,)).fetchall()

    # ---- prospecting ------------------------------------------------------

    def save_search_config(self, config_json: str, *, note: str, created_by: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO brand_search_configs (config_json, note, created_by, created_at) VALUES (?, ?, ?, ?)",
            (config_json, note, created_by, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def search_configs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM brand_search_configs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def search_config(self, config_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM brand_search_configs WHERE id = ?", (config_id,)).fetchone()

    def start_brand_search(self, config_id: int | None) -> int:
        cur = self.conn.execute("INSERT INTO brand_searches (config_id, status, started_at) VALUES (?, 'running', ?)",
                                (config_id, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def finish_brand_search(self, search_id: int, *, status: str, stats: dict | None = None,
                            leads: dict[str, int] | None = None, stopped: str = "", error: str = "") -> None:
        leads = leads or {}
        self.conn.execute(
            """UPDATE brand_searches SET status = ?, stats_json = ?, leads_json = ?, leads_usa = ?, leads_india = ?,
               stopped = ?, error = ?, finished_at = ? WHERE id = ?""",
            (status, json.dumps(stats or {}), json.dumps(leads), leads.get("us", 0), leads.get("in", 0),
             stopped or None, error or None, now_iso(), search_id))
        self.conn.commit()

    def fail_unfinished_searches(self) -> None:
        """Runs left 'running' by a stopped app."""
        self.conn.execute("UPDATE brand_searches SET status = 'failed', error = 'interrupted', finished_at = ? "
                          "WHERE status = 'running'", (now_iso(),))
        self.conn.commit()

    def brand_searches(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM brand_searches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def add_search_query(self, search_id: int, query: str, kind: str, region: str, results: int,
                         new_candidates: int) -> None:
        self.conn.execute(
            """INSERT INTO brand_search_queries (search_id, query, kind, region, results, new_candidates, ran_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", (search_id, query, kind, region or None, results, new_candidates, now_iso()))
        self.conn.commit()

    def query_yield(self, limit: int = 400) -> list[sqlite3.Row]:
        """Per query: runs, candidates, qualified prospects and how far they got."""
        return self.conn.execute(
            """SELECT q.query, q.kind, MAX(q.region) AS region, COUNT(*) AS runs, SUM(q.results) AS results,
                      SUM(q.new_candidates) AS candidates,
                      (SELECT COUNT(*) FROM brand_candidates b WHERE b.query = q.query AND b.status = 'lead') AS leads,
                      (SELECT COUNT(*) FROM brand_candidates b WHERE b.query = q.query AND b.status = 'rejected') AS rejected,
                      (SELECT COUNT(*) FROM brand_candidates b WHERE b.query = q.query AND b.stage = 'contacted') AS contacted,
                      MAX(q.ran_at) AS last_run
               FROM brand_search_queries q GROUP BY q.query, q.kind ORDER BY leads DESC, candidates DESC LIMIT ?""",
            (limit,)).fetchall()

    def rejection_reasons(self, limit: int = 15) -> list[sqlite3.Row]:
        """Why candidates were turned down, most common first (numbers folded so '12034 followers' groups)."""
        return self.conn.execute(
            """SELECT CASE WHEN reason LIKE '%followers >=%' THEN 'too many followers'
                           WHEN reason LIKE 'inactive%' THEN 'inactive (no post in 60 days)'
                           WHEN reason LIKE '%established' THEN 'too many posts (established)'
                           WHEN reason LIKE 'model:%' THEN 'not a fit (model)'
                           ELSE reason END AS why, COUNT(*) AS n
               FROM brand_candidates WHERE status IN ('rejected', 'unavailable') GROUP BY why ORDER BY n DESC LIMIT ?""",
            (limit,)).fetchall()

    def prospects(self, *, stage: str | None = None, region: str | None = None, search: str | None = None,
                  search_id: int | None = None, limit: int = 500) -> list[sqlite3.Row]:
        """Qualified brands with their prospect stage and, when they've talked to us, their DM conversation."""
        where, args = ["b.status = 'lead'"], []
        if stage:
            where.append("b.stage = ?"); args.append(stage)
        if region:
            where.append("b.region = ?"); args.append(region)
        if search_id:
            where.append("b.search_id = ?"); args.append(search_id)
        if search:
            where.append("(b.username LIKE ? OR b.name LIKE ? OR b.bio LIKE ?)"); args += [f"%{search}%"] * 3
        return self.conn.execute(
            f"""SELECT b.*, c.id AS conversation_id FROM brand_candidates b
                LEFT JOIN ig_conversations c ON lower(c.username) = b.username
                WHERE {' AND '.join(where)} ORDER BY b.checked_at DESC LIMIT ?""", (*args, limit)).fetchall()

    def set_prospect_stage(self, username: str, stage: str) -> None:
        cur = self.conn.execute("UPDATE brand_candidates SET stage = ?, stage_at = ? WHERE username = ? AND status = 'lead'",
                                (stage, now_iso(), username.lower().lstrip("@")))
        self.conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"unknown prospect {username}")

    def prospect_usernames(self) -> dict[str, str]:
        """username -> prospect stage, for every qualified brand."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT username, stage FROM brand_candidates WHERE status = 'lead'").fetchall()}

    # ---- brand profile versions -------------------------------------------

    def add_brand_version(self, content: str, *, note: str, created_by: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO brand_profile_versions (content, note, created_by, created_at) VALUES (?, ?, ?, ?)",
            (content, note, created_by, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def brand_versions(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, note, created_by, created_at, length(content) AS chars "
                                 "FROM brand_profile_versions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def brand_version(self, version_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM brand_profile_versions WHERE id = ?", (version_id,)).fetchone()

    def hashtags_used_since(self, since: datetime) -> set[str]:
        """Hashtags whose 7-day window is still open (querying them again is free)."""
        rows = self.conn.execute("SELECT tag FROM hashtag_usage WHERE first_used_at >= ?",
                                 (since.astimezone(timezone.utc).isoformat(timespec="seconds"),)).fetchall()
        return {r["tag"] for r in rows}

    def record_hashtag(self, tag: str, window_start: datetime) -> None:
        """Start a new 7-day window for `tag` unless one is already open."""
        tag = tag.lower().lstrip("#")
        self.conn.execute(
            """INSERT INTO hashtag_usage (tag, first_used_at) VALUES (?, ?)
               ON CONFLICT (tag) DO UPDATE SET first_used_at = excluded.first_used_at
               WHERE hashtag_usage.first_used_at < ?""",
            (tag, now_iso(), window_start.astimezone(timezone.utc).isoformat(timespec="seconds")))
        self.conn.commit()

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
