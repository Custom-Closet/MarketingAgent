"""What Claude sees of a conversation when drafting: a rolling summary plus the recent window.

Raw messages are all kept in the database. For drafting, messages older than the window are
folded into a short per-conversation summary (incrementally - only messages not yet summarised
are sent), so the prompt stays small and predictable however long the chat gets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .db import Database

DEFAULT_WINDOW_DAYS = 7
MAX_RECENT = 30          # newest messages in the window, at most
MIN_RECENT = 5           # always show at least this many, even if they're older than the window

# (previous summary, transcript of newly aged messages) -> new summary
Summarizer = Callable[[str, str], str]


def format_messages(rows: Iterable, ig_user_id: str) -> str:
    """Stored messages as '[time] them:' / 'us:' lines, oldest first."""
    lines = []
    for m in rows:
        who = "us" if m["from_id"] == ig_user_id else "them"
        text = m["text"] or ("<sent an image/attachment>" if m["extras_json"] else "<no text>")
        lines.append(f"[{m['created_time'][:16]}] {who}: {text}")
    return "\n".join(lines)


@dataclass
class DraftContext:
    summary: str          # compressed history of everything before the recent window ("" if none)
    recent: str           # recent messages, formatted
    recent_count: int
    summarized_count: int  # older messages covered by the summary in this call or before


def build_context(db: Database, conversation_id: str, ig_user_id: str, *, summarizer: Summarizer | None = None,
                  window_days: int = DEFAULT_WINDOW_DAYS, now: datetime | None = None) -> DraftContext:
    """Summary + recent window for one conversation, updating the stored summary if messages aged out.

    Without a summarizer, the existing summary (possibly stale) is used and nothing is written.
    """
    now = now or datetime.now(timezone.utc)
    rows = db.ig_transcript(conversation_id)
    cutoff = (now - timedelta(days=window_days)).isoformat(timespec="seconds")
    in_window = [r for r in rows if r["created_time"] >= cutoff][-MAX_RECENT:]
    if len(in_window) < MIN_RECENT:
        in_window = rows[-MIN_RECENT:]
    older = rows[:len(rows) - len(in_window)]

    stored = db.ig_summary(conversation_id)
    summary = stored["summary"] if stored else ""
    through = stored["summarized_through"] if stored else ""
    unsummarized = [r for r in older if r["created_time"] > through]
    if unsummarized and summarizer:
        summary = summarizer(summary, format_messages(unsummarized, ig_user_id)).strip()
        db.save_ig_summary(conversation_id, summary, unsummarized[-1]["created_time"])
    return DraftContext(summary=summary, recent=format_messages(in_window, ig_user_id),
                        recent_count=len(in_window), summarized_count=len(older))
