"""Inbox rules: which chats need a reply, what to suggest, how urgent, and what may be sent.

Every send still passes the pipeline's send-time gates (approval recorded, reply window open,
not suppressed, no duplicate, length) plus one more: the customer must not already have been
answered from the Instagram app. Sends also require an explicit confirmation from the caller.

Auto-send (off unless enabled in Settings): an open draft whose text is an unchanged *trusted*
FAQ answer (see faq.py) is scheduled AUTO_SEND_DELAY after it becomes eligible. If nobody edits,
holds, dismisses or sends it by then, the background job approves it as "auto" and sends it
through the same gates. Any failed attempt holds the draft for a person to handle.
"""

from __future__ import annotations

import logging
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
AUTO_SEND_DELAY = timedelta(minutes=1)  # time a person has to step in before a trusted FAQ answer goes out
SYNC_EVERY = timedelta(minutes=15)      # background Instagram check while `agent serve` runs
JOB_TICK_SECONDS = 5                    # how often the background job looks for due auto-sends

log = logging.getLogger(__name__)

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
    auto_send_at: str | None            # when this draft sends itself unless someone steps in
    auto_send_held: bool                # a person held it (or an auto-send failed): never auto-sent
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
        self.sync_state = {"running": False, "last_finished": None, "last_error": None, "conversations": 0,
                           "next_run": None}
        self._sending: set[int] = set()     # drafts being sent right now (manual send vs. auto-send race)
        self._stop = threading.Event()

    # ---- settings -----------------------------------------------------------------

    def window_days(self) -> int:
        """Days of recent messages Claude sees verbatim; older ones are summarised."""
        return int(self.repo.db.setting("context_window_days", str(DEFAULT_WINDOW_DAYS)))

    def auto_send_enabled(self) -> bool:
        return self.repo.db.setting("auto_send_faq", "0") == "1"

    def set_auto_send(self, enabled: bool) -> None:
        """Turning it on starts a fresh one-minute countdown on every eligible draft; off cancels them."""
        with self.lock:
            self.repo.db.set_setting("auto_send_faq", "1" if enabled else "0")
            self.schedule_auto_sends()

    # ---- FAQ library ----------------------------------------------------------

    def faqs(self) -> list[FAQ]:
        with self.lock:
            return self.repo.faqs()

    def approve_faq(self, key: str, approved: bool = True) -> None:
        with self.lock:
            self._require_faq(key)
            self.repo.update_faq(key, approved=approved)
            self.schedule_auto_sends()

    def edit_faq(self, key: str, answer: str, keywords: list[str] | None = None) -> None:
        """Editing an answer withdraws its approval, so it must be approved again."""
        if not answer.strip():
            raise ValueError("FAQ answer can't be empty")
        with self.lock:
            self._require_faq(key)
            self.repo.update_faq(key, answer=answer.strip(), keywords=keywords, approved=False)
            self.schedule_auto_sends()

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
            self.schedule_auto_sends()
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
            auto_send_at=d["auto_send_at"], auto_send_held=bool(d["auto_send_hold"]),
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
            self.repo.update_draft(draft_id, body=body.strip(), status="pending", approved_at=None, auto_send_at=None)
            self.schedule_auto_sends()      # editing back to the exact trusted answer restarts the countdown

    def apply_faq(self, draft_ids: list[int], key: str) -> int:
        with self.lock:
            faq = self._require_faq(key)
            for draft_id in draft_ids:
                self._open_draft(draft_id)
                self.repo.update_draft(draft_id, body=faq.answer, faq_key=key, status="pending", approved_at=None,
                                       auto_send_at=None)
            self.schedule_auto_sends()
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

    def hold(self, draft_ids: list[int]) -> None:
        """Stop these drafts from ever being auto-sent; they wait for a person like any other draft."""
        with self.lock:
            for draft_id in draft_ids:
                self._open_draft(draft_id)
                self.repo.update_draft(draft_id, auto_send_hold=1, auto_send_at=None)

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
                reason = "already being sent" if draft_id in self._sending else self._send_blocker(draft)
                if reason:
                    result.blocked[draft_id] = reason
                    if draft and draft["status"] == "approved" and reason in ("reply window closed", "answered in Instagram"):
                        self.repo.update_draft(draft_id, status="expired" if "window" in reason else "rejected",
                                               error=reason)
                    continue
                self._sending.add(draft_id)
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
            else:
                with self.lock:
                    self.repo.db.mark_sent(draft_id)
                result.sent.append(draft_id)
            finally:
                with self.lock:
                    self._sending.discard(draft_id)
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

    # ---- auto-send ----------------------------------------------------------------------

    def _auto_sendable(self, draft, faqs: dict[str, FAQ]) -> bool:
        faq = faqs.get(draft["faq_key"]) if draft["faq_key"] else None
        return bool(faq and faq.trusted and not draft["auto_send_hold"] and draft["body"].strip()
                    and draft["body"].strip() == faq.answer.strip())

    def schedule_auto_sends(self) -> None:
        """Give newly eligible drafts a send time AUTO_SEND_DELAY from now; clear it on drafts no longer eligible."""
        with self.lock:
            enabled = self.ig is not None and self.auto_send_enabled()   # nothing could send it otherwise
            faqs = {f.key: f for f in self.repo.faqs()}
            due_at = (self.clock() + AUTO_SEND_DELAY).astimezone(timezone.utc).isoformat(timespec="seconds")
            for d in self.repo.open_drafts():
                eligible = enabled and self._auto_sendable(d, faqs)
                if eligible and not d["auto_send_at"]:
                    self.repo.update_draft(d["id"], auto_send_at=due_at)
                elif not eligible and d["auto_send_at"]:
                    self.repo.update_draft(d["id"], auto_send_at=None)

    def run_auto_sends(self) -> SendResult:
        """Send every draft whose auto-send time has passed and that is still eligible."""
        result = SendResult()
        if self.ig is None:
            return result
        with self.lock:
            self.schedule_auto_sends()
            due = self.repo.due_auto_sends(self.clock())[:MAX_SEND_BATCH]
        due = [i for i in due if self._caught_up(i)]
        ids = []
        with self.lock:
            faqs = {f.key: f for f in self.repo.faqs()}
            for draft_id in due:
                draft = self.repo.draft(draft_id)
                if not self._auto_sendable(draft, faqs):   # edited or held while we were fetching
                    continue
                if draft["status"] == "pending":
                    self.repo.approve(draft_id, by="auto")
                ids.append(draft_id)
        if not ids:
            return result
        # confirm=True: a person approved this exact text (in Settings or 3+ times) and had a minute to step in.
        result = self.send(ids, confirm=True)
        with self.lock:
            for draft_id in ids:
                if draft_id not in result.sent:     # blocked, failed or not reached: leave it to a person
                    self.repo.update_draft(draft_id, auto_send_at=None, auto_send_hold=1)
        for draft_id in result.sent:
            log.info("auto-sent FAQ reply %s", draft_id)
        return result

    def _caught_up(self, draft_id: int) -> bool:
        """Fetch this chat's newest messages so a reply typed in the Instagram app since the last sync
        blocks the auto-send. False (and the draft is held) if Instagram can't be read right now."""
        with self.lock:
            draft = self.repo.draft(draft_id)
            cid = draft["conversation_id"]
            latest = self.repo.db.ig_latest_message_time(cid) if cid else None
        if not cid or not latest:
            return True
        try:
            fetched = self.ig.messages(cid, stop_before=datetime.fromisoformat(latest))
        except InstagramError as exc:
            with self.lock:
                self.repo.update_draft(draft_id, auto_send_at=None, auto_send_hold=1, error=f"auto-send held: {exc}")
            return False
        with self.lock:
            self.repo.db.add_ig_messages(cid, fetched)
        return True

    # ---- background jobs ----------------------------------------------------------------

    def start_background_jobs(self, *, sync_every: timedelta = SYNC_EVERY, tick: float = JOB_TICK_SECONDS) -> None:
        """While the app runs: check Instagram now and every `sync_every`, and send due auto-replies."""
        threading.Thread(target=self._run_jobs, args=(sync_every, tick), daemon=True).start()

    def stop_background_jobs(self) -> None:
        self._stop.set()

    def _run_jobs(self, sync_every: timedelta, tick: float) -> None:
        next_sync = self.clock()
        while not self._stop.is_set():
            now = self.clock()
            if self.ig is not None and now >= next_sync:
                self.start_background_sync()
                next_sync = now + sync_every
                self.sync_state["next_run"] = next_sync.isoformat(timespec="seconds")
            try:
                self.run_auto_sends()
            except Exception:
                log.exception("auto-send failed")
            self._stop.wait(tick)

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
            "auto_sent_24h": self.repo.sent_replies_since(self.clock() - timedelta(hours=24), approved_by="auto"),
            "auto_send": {"enabled": self.auto_send_enabled(),
                          "scheduled": sum(bool(i.auto_send_at) for i in items)},
            "sync": dict(self.sync_state),
        }

    def transcript(self, conversation_id: str) -> list[dict]:
        with self.lock:
            rows = self.repo.db.ig_transcript(conversation_id)
        return [{"from": "us" if r["from_id"] == self.our_id else "them", "text": r["text"],
                 "attachment": bool(r["extras_json"]), "at": r["created_time"]} for r in rows]
