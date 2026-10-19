"""Inbox rules: which chats need a reply, what to suggest, how urgent, and what may be sent.

Every send still passes the pipeline's send-time gates (approval recorded, reply window open,
not suppressed, no duplicate, length) plus one more: the customer must not already have been
answered from the Instagram app. Sends also require an explicit confirmation from the caller.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import pipeline
from ..context import DEFAULT_WINDOW_DAYS, build_context
from ..sources.instagram import REPLY_WINDOW, InstagramError, InstagramRateLimited, parse_time
from .faq import FAQ, match_faq
from .repository import InboxRepository

MAX_SEND_BATCH = 25                     # replies per send request; the client throttles to 1 call/second
URGENT_HOURS = 4                        # reply window closing soon

INTENT_WORDS = ("price", "pricing", "cost", "rate", "quote", "quotation", "sample", "order", "bulk", "moq",
                "pieces", "charge", "how much", "kitna", "tech pack", "techpack", "export")
BRAND_WORDS = ("brand", "label", "manufacturer", "manufacturing", "launching", "bulk", "production", "boutique")
GREETINGS = {"hi", "hii", "hiii", "hello", "hey", "heyy", "hai", "good morning", "hie"}


class SendNotConfirmed(Exception):
    pass


@dataclass
class QueueItem:
    draft_id: int
    status: str                         # pending / approved
    username: str | None
    igsid: str
    conversation_id: str | None
    customer_messages: list[str]        # unanswered customer messages, oldest first
    sent_attachment: bool               # e.g. a reference image - usually needs a quote
    last_inbound_at: str | None
    hours_left: float
    category: str                       # FAQ key, "reference_image", "greeting" or "other"
    faq_question: str | None
    faq_approved: bool                  # draft uses an approved FAQ answer unchanged -> one-click approvable
    customer_type: str                  # brand / personal / unknown
    priority: str                       # high / medium / low
    score: int
    reasons: list[str] = field(default_factory=list)
    body: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SendResult:
    sent: list[int] = field(default_factory=list)
    blocked: dict[int, str] = field(default_factory=dict)   # draft id -> reason
    failed: dict[int, str] = field(default_factory=dict)


@dataclass
class QueueFilters:
    priority: set[str] | None = None
    category: str | None = None
    customer_type: str | None = None
    status: str | None = None
    max_hours_left: float | None = None
    search: str | None = None


def _hours_left(deadline: str | None, now: datetime) -> float:
    if not deadline:
        return 0.0
    return round((datetime.fromisoformat(deadline) - now).total_seconds() / 3600, 1)


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    text = f" {re.sub(r'[^a-z0-9 ]', ' ', text.lower())} "
    return any(f" {w} " in text for w in words)


def score_thread(customer_messages: list[str], sent_attachment: bool, all_customer_text: str,
                 hours_left: float) -> tuple[int, str, list[str]]:
    """Priority score, label and the reasons behind it."""
    unanswered = " ".join(customer_messages)
    score, reasons = 0, []
    if hours_left <= URGENT_HOURS:
        score += 3
        reasons.append(f"reply window closes in {max(hours_left, 0):.0f}h")
    elif hours_left <= 12:
        score += 1
    if _has_any(unanswered, INTENT_WORDS):
        score += 3
        reasons.append("asking about price, samples or ordering")
    elif _has_any(all_customer_text, INTENT_WORDS):
        score += 1
        reasons.append("talked about price, samples or ordering earlier")
    if sent_attachment:
        score += 3
        reasons.append("sent an image or reference")
    if _has_any(all_customer_text, BRAND_WORDS):
        score += 2
        reasons.append("clothing brand")
    if customer_messages and all(m.strip().lower().rstrip("!.") in GREETINGS for m in customer_messages):
        score -= 1
    label = "high" if score >= 5 else "medium" if score >= 2 else "low"
    return score, label, reasons


class InboxService:
    def __init__(self, repo: InboxRepository, *, our_id: str, ig=None, writer=None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.repo = repo
        self.our_id = our_id
        self.ig = ig
        self.writer = writer
        self.clock = clock
        self.lock = threading.RLock()       # one SQLite connection shared by the web server's threads
        self.sync_state = {"running": False, "last_finished": None, "last_error": None, "conversations": 0}

    # ---- settings -----------------------------------------------------------------

    def window_days(self) -> int:
        """Days of recent messages Claude sees verbatim; older ones are summarised."""
        return int(self.repo.db.setting("context_window_days", str(DEFAULT_WINDOW_DAYS)))

    # ---- FAQ library ----------------------------------------------------------

    def faqs(self) -> list[FAQ]:
        with self.lock:
            return self.repo.faqs()

    def approve_faq(self, key: str, approved: bool = True) -> None:
        with self.lock:
            self._require_faq(key)
            self.repo.update_faq(key, approved=approved)

    def edit_faq(self, key: str, answer: str, keywords: list[str] | None = None) -> None:
        """Editing an answer withdraws its approval, so it must be approved again."""
        if not answer.strip():
            raise ValueError("FAQ answer can't be empty")
        with self.lock:
            self._require_faq(key)
            self.repo.update_faq(key, answer=answer.strip(), keywords=keywords, approved=False)

    def _require_faq(self, key: str) -> FAQ:
        faq = self.repo.faq(key)
        if not faq:
            raise KeyError(f"unknown FAQ {key!r}")
        return faq

    # ---- sync -------------------------------------------------------------------

    def sync(self, *, log: Callable[[str], None] = lambda m: None) -> dict[str, int]:
        """Archive conversations active within the reply window, then refresh drafts.

        Network calls happen outside the lock so the inbox stays usable while syncing.
        """
        if self.ig is None:
            raise RuntimeError("Instagram is not configured")
        since = self.clock() - REPLY_WINDOW
        stats = {"conversations": 0, "messages": 0, "failed": 0}
        self.sync_state.update(running=True, last_error=None, conversations=0)
        try:
            for convo in self.ig.iter_conversations(since):
                other = self.ig.other_participant(convo) or {}
                updated = parse_time(convo["updated_time"]).astimezone(timezone.utc).isoformat(timespec="seconds")
                with self.lock:
                    known = self.repo.db.ig_conversation(convo["id"])
                    if known and known["updated_time"] == updated:
                        continue
                    self.repo.db.save_ig_conversation(convo["id"], igsid=other.get("id"),
                                                      username=other.get("username"), updated_time=None)
                    latest = self.repo.db.ig_latest_message_time(convo["id"])
                stop_before = max(since, datetime.fromisoformat(latest)) if latest else since
                try:
                    fetched = self.ig.messages(convo["id"], stop_before=stop_before)
                except InstagramRateLimited:
                    raise
                except InstagramError as exc:
                    stats["failed"] += 1
                    log(f"@{other.get('username')}: {exc}")
                    continue
                with self.lock:
                    stats["messages"] += self.repo.db.add_ig_messages(convo["id"], fetched)
                    self.repo.db.save_ig_conversation(convo["id"], igsid=other.get("id"),
                                                      username=other.get("username"), updated_time=updated)
                stats["conversations"] += 1
                self.sync_state["conversations"] = stats["conversations"]
            stats["drafts"] = self.refresh_drafts()
            return stats
        except Exception as exc:
            self.sync_state["last_error"] = str(exc)
            raise
        finally:
            self.sync_state.update(running=False, last_finished=self.clock().isoformat(timespec="seconds"))

    def start_background_sync(self) -> bool:
        """Run sync in a thread; False if one is already running."""
        if self.sync_state["running"]:
            return False
        self.sync_state["running"] = True
        threading.Thread(target=self._sync_quietly, daemon=True).start()
        return True

    def _sync_quietly(self) -> None:
        try:
            self.sync()
        except Exception:
            pass  # recorded in sync_state["last_error"]

    # ---- drafts -------------------------------------------------------------------

    def refresh_drafts(self) -> int:
        """Create a draft for every chat awaiting a reply, and close drafts that no longer apply."""
        now = self.clock()
        created = 0
        with self.lock:
            faqs = self.repo.faqs()
            awaiting = {t["igsid"]: t for t in self.repo.threads_awaiting_reply(self.our_id, now - REPLY_WINDOW)}
            for draft in self.repo.open_drafts():
                if draft["thread_id"] not in awaiting:
                    answered = draft["conversation_id"] and self._answered_elsewhere(draft)
                    self.repo.update_draft(draft["id"], status="rejected" if answered else "expired",
                                           error="answered in Instagram" if answered else "reply window closed")
            for igsid, thread in awaiting.items():
                if not igsid or not thread["username"] or self.repo.db.is_suppressed([thread["username"]]):
                    continue
                if self.repo.open_draft(igsid):
                    continue
                unanswered = self.repo.unanswered_messages(thread["id"], self.our_id)
                texts = [m["text"] for m in unanswered if m["text"]]
                # A customer who just sent an image (usually the reference) needs a quote, not an FAQ answer.
                sent_image = any(m["extras_json"] and not m["text"] for m in unanswered)
                faq = match_faq(" ".join(texts), faqs) if texts and not sent_image else None
                self.repo.create_draft(username=thread["username"], igsid=igsid, body=faq.answer if faq else "",
                                       reply_deadline=datetime.fromisoformat(thread["last_inbound_at"]) + REPLY_WINDOW,
                                       faq_key=faq.key if faq else None)
                created += 1
        return created

    def _answered_elsewhere(self, draft) -> bool:
        latest = self.repo.latest_message(draft["conversation_id"])
        return bool(latest and latest["from_id"] == self.our_id
                    and latest["created_time"] >= draft["created_at"])

    def queue(self, filters: QueueFilters | None = None) -> list[QueueItem]:
        """Open drafts, highest priority (then most urgent) first."""
        filters = filters or QueueFilters()
        now = self.clock()
        items = []
        with self.lock:
            faqs = {f.key: f for f in self.repo.faqs()}
            for d in self.repo.open_drafts():
                items.append(self._item(d, faqs, now))
        items = [i for i in items if self._matches(i, filters)]
        return sorted(items, key=lambda i: (-i.score, i.hours_left))

    def _item(self, d, faqs: dict[str, FAQ], now: datetime) -> QueueItem:
        unanswered = self.repo.unanswered_messages(d["conversation_id"], self.our_id) if d["conversation_id"] else []
        texts = [m["text"] for m in unanswered if m["text"]]
        attachment = any(m["extras_json"] and not m["text"] for m in unanswered)
        all_text = self.repo.customer_text(d["conversation_id"], self.our_id) if d["conversation_id"] else ""
        hours = _hours_left(d["reply_deadline"], now)
        score, label, reasons = score_thread(texts, attachment, all_text, hours)
        faq = faqs.get(d["faq_key"]) if d["faq_key"] else None
        if faq:
            category = faq.key
        elif attachment:
            category = "reference_image"
        elif texts and all(t.strip().lower().rstrip("!.") in GREETINGS for t in texts):
            category = "greeting"
        else:
            category = "other"
        customer_type = ("brand" if _has_any(all_text, BRAND_WORDS)
                         else "personal" if _has_any(all_text, ("personal", "for me", "myself", "my wedding"))
                         else "unknown")
        return QueueItem(
            draft_id=d["id"], status=d["status"], username=d["ig_username"], igsid=d["thread_id"],
            conversation_id=d["conversation_id"], customer_messages=texts, sent_attachment=attachment,
            last_inbound_at=unanswered[-1]["created_time"] if unanswered else None, hours_left=hours,
            category=category, faq_question=faq.question if faq else None,
            faq_approved=bool(faq and faq.approved and d["body"].strip() == faq.answer.strip()),
            customer_type=customer_type, priority=label, score=score, reasons=reasons, body=d["body"],
            error=d["error"])

    @staticmethod
    def _matches(item: QueueItem, f: QueueFilters) -> bool:
        if f.priority and item.priority not in f.priority:
            return False
        if f.category and item.category != f.category:
            return False
        if f.customer_type and item.customer_type != f.customer_type:
            return False
        if f.status and item.status != f.status:
            return False
        if f.max_hours_left is not None and item.hours_left > f.max_hours_left:
            return False
        if f.search:
            needle = f.search.lower()
            if needle not in (item.username or "").lower() and not any(needle in m.lower() for m in item.customer_messages):
                return False
        return True

    def _open_draft(self, draft_id: int):
        draft = self.repo.draft(draft_id)
        if not draft or draft["status"] not in ("pending", "approved"):
            raise KeyError(f"draft {draft_id} is not open")
        return draft

    def edit(self, draft_id: int, body: str) -> None:
        """Change a draft's text. An approved draft goes back to pending so it's reviewed again."""
        with self.lock:
            self._open_draft(draft_id)
            self.repo.update_draft(draft_id, body=body.strip(), status="pending", approved_at=None)

    def apply_faq(self, draft_ids: list[int], key: str) -> int:
        with self.lock:
            faq = self._require_faq(key)
            for draft_id in draft_ids:
                self._open_draft(draft_id)
                self.repo.update_draft(draft_id, body=faq.answer, faq_key=key, status="pending", approved_at=None)
        return len(draft_ids)

    def approve(self, draft_ids: list[int]) -> dict[int, str]:
        """Approve drafts; returns {draft id: reason} for any that can't be approved (e.g. empty)."""
        refused = {}
        with self.lock:
            for draft_id in draft_ids:
                draft = self._open_draft(draft_id)
                if not draft["body"].strip():
                    refused[draft_id] = "write a reply first"
                    continue
                self.repo.approve(draft_id)
        return refused

    def approve_faq_replies(self, filters: QueueFilters | None = None) -> list[int]:
        """One click: approve every pending draft that uses an approved FAQ answer unchanged."""
        ids = [i.draft_id for i in self.queue(filters) if i.status == "pending" and i.faq_approved]
        self.approve(ids)
        return ids

    def reject(self, draft_ids: list[int]) -> None:
        with self.lock:
            for draft_id in draft_ids:
                self._open_draft(draft_id)
                self.repo.update_draft(draft_id, status="rejected", error="rejected in inbox")

    def ai_draft(self, draft_id: int) -> str:
        """Ask Claude for a reply using the conversation so far (costs an API call)."""
        if self.writer is None:
            raise RuntimeError("Claude is not configured (ANTHROPIC_API_KEY)")
        with self.lock:
            draft = self._open_draft(draft_id)
            cid = draft["conversation_id"]
            texts = [m["text"] for m in self.repo.unanswered_messages(cid, self.our_id) if m["text"]] if cid else []
            ctx = build_context(self.repo.db, cid, self.our_id, window_days=self.window_days(),
                                summarizer=getattr(self.writer, "summarize_conversation", None)) if cid else None
        reply = self.writer.draft_dm_reply(draft["ig_username"], " ".join(texts) or "(sent an attachment)",
                                           history=ctx.recent if ctx else "", summary=ctx.summary if ctx else "")
        self.edit(draft_id, reply.message)
        return reply.message

    # ---- sending --------------------------------------------------------------------

    def send(self, draft_ids: list[int], *, confirm: bool) -> SendResult:
        """Send approved drafts. `confirm` must be True: the caller showed the person exactly what goes out."""
        if not confirm:
            raise SendNotConfirmed("sending requires confirm=true")
        if len(draft_ids) > MAX_SEND_BATCH:
            raise ValueError(f"send at most {MAX_SEND_BATCH} replies at a time")
        if self.ig is None:
            raise RuntimeError("Instagram is not configured")
        result = SendResult()
        for draft_id in draft_ids:
            with self.lock:
                draft = self.repo.draft(draft_id)
                reason = self._send_blocker(draft)
                if reason:
                    result.blocked[draft_id] = reason
                    if draft and draft["status"] == "approved" and reason in ("reply window closed", "answered in Instagram"):
                        self.repo.update_draft(draft_id, status="expired" if "window" in reason else "rejected",
                                               error=reason)
                    continue
            try:
                self.ig.send_reply(draft["thread_id"], draft["body"])
            except InstagramRateLimited as exc:
                result.failed[draft_id] = str(exc)
                break   # stop the batch; the rest stay approved for later
            except InstagramError as exc:
                result.failed[draft_id] = str(exc)
                with self.lock:
                    self.repo.update_draft(draft_id, error=str(exc))
                continue
            with self.lock:
                self.repo.db.mark_sent(draft_id)
            result.sent.append(draft_id)
        return result

    def _send_blocker(self, draft) -> str | None:
        if not draft:
            return "draft not found"
        if draft["status"] != "approved":
            return "not approved"
        if blocked := pipeline.ig_send_blocker(self.repo.db, draft):
            return blocked[1]
        if draft["conversation_id"] and self._answered_elsewhere(draft):
            return "answered in Instagram"
        return None

    # ---- overview ---------------------------------------------------------------------

    def stats(self) -> dict:
        items = self.queue()
        return {
            "open": len(items),
            "approved": sum(i.status == "approved" for i in items),
            "high": sum(i.priority == "high" for i in items),
            "closing_soon": sum(i.hours_left <= URGENT_HOURS for i in items),
            "faq_ready": sum(i.status == "pending" and i.faq_approved for i in items),
            "sent_24h": self.repo.sent_replies_since(self.clock() - timedelta(hours=24)),
            "sync": dict(self.sync_state),
        }

    def transcript(self, conversation_id: str) -> list[dict]:
        with self.lock:
            rows = self.repo.db.ig_transcript(conversation_id)
        return [{"from": "us" if r["from_id"] == self.our_id else "them", "text": r["text"],
                 "attachment": bool(r["extras_json"]), "at": r["created_time"]} for r in rows]
