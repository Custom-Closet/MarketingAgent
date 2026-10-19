"""Pipeline steps: discover -> enrich -> draft -> (human review) -> send -> sync."""

from __future__ import annotations

import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .channels.gmail import GmailChannel, build_email
from .config import Settings
from .context import DEFAULT_WINDOW_DAYS, build_context, format_messages
from .db import Database
from .personalize import Personalizer
from .sources.hunter import HunterClient, HunterError
from .sources.instagram import REPLY_WINDOW, InstagramClient, InstagramError, InstagramRateLimited, parse_time

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SENDABLE_EMAIL_STATUSES = {"valid", "accept_all"}

Log = Callable[[str], None]


# ---- discovery -----------------------------------------------------------

def discover_domain(db: Database, hunter: HunterClient, domain: str, *, limit: int = 10,
                    seniority: str | None = None, department: str | None = None, verify: bool = True,
                    log: Log = print) -> int:
    added = 0
    for contact in hunter.domain_search(domain, limit=limit, seniority=seniority, department=department):
        status = hunter.verify(contact.email) if verify else None
        db.upsert_lead(name=contact.name, email=contact.email, email_status=status,
                       company=contact.organization, domain=contact.domain, position=contact.position,
                       source="hunter")
        log(f"  {contact.email:<40} {contact.position or '':<30} {status or ''}")
        added += 1
    return added


def discover_instagram(db: Database, ig: InstagramClient, usernames: set[str], *, min_followers: int = 0,
                       log: Log = print) -> int:
    added = 0
    for username in sorted(usernames):
        if db.is_suppressed([username]):
            continue
        try:
            profile = ig.business_discovery(username)
        except InstagramError as exc:
            log(f"  skip @{username}: {exc}")  # usually a personal (non-business) account
            continue
        if (profile.followers_count or 0) < min_followers:
            continue
        db.upsert_lead(name=profile.name, ig_username=profile.username, ig_bio=profile.biography,
                       ig_followers=profile.followers_count, website=profile.website, domain=profile.domain,
                       company=profile.name, source="instagram")
        log(f"  @{profile.username:<28} {profile.followers_count or 0:>8} followers  {profile.domain or '-'}")
        added += 1
    return added


def enrich_emails(db: Database, hunter: HunterClient, *, verify: bool = True, log: Log = print) -> int:
    """Find an email for leads that only have a domain (typically from their Instagram website)."""
    found = 0
    for lead in db.leads_needing_email():
        try:
            contacts = hunter.domain_search(lead["domain"], limit=5)
        except HunterError as exc:
            log(f"  {lead['domain']}: {exc}")
            continue
        if not contacts:
            continue
        best = max(contacts, key=lambda c: c.confidence or 0)
        status = hunter.verify(best.email) if verify else None
        try:
            db.update_lead(lead["id"], email=best.email.lower(), email_status=status,
                           position=lead["position"] or best.position)
        except Exception:  # email already belongs to another lead
            continue
        log(f"  @{lead['ig_username'] or lead['id']}: {best.email} ({status or 'unverified'})")
        found += 1
    return found


def process_targets(db: Database, settings: Settings, *, limit: int, hunter: HunterClient | None,
                    ig: InstagramClient | None, log: Log = print) -> int:
    """Run discovery for queued targets (`agent targets add ...`)."""
    kinds = (["domain"] if hunter else []) + (["ig_user", "hashtag"] if ig else [])
    if not kinds:
        return 0
    processed = 0
    for target in db.pending_targets(limit, kinds):
        try:
            if target["kind"] == "domain":
                added = discover_domain(db, hunter, target["value"], log=log)
            elif target["kind"] == "ig_user":
                added = discover_instagram(db, ig, {target["value"]}, log=log)
            else:
                added = discover_instagram(db, ig, ig.hashtag_mentions(target["value"]), log=log)
            db.finish_target(target["id"], "done", f"{added} leads")
        except (HunterError, InstagramError) as exc:
            db.finish_target(target["id"], "failed", str(exc))
            log(f"  {target['kind']} {target['value']}: {exc}")
        processed += 1
    return processed


# ---- drafting ------------------------------------------------------------

def _run_parallel(items: list, fn: Callable, *, deadline: float | None, workers: int = 4):
    """Yield (item, result, error) as Claude calls finish; stop starting new work after `deadline`."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for future in as_completed(futures):
            if deadline and time.monotonic() > deadline:
                for pending in futures:
                    pending.cancel()
            if future.cancelled():
                continue
            try:
                yield futures[future], future.result(), None
            except Exception as exc:
                yield futures[future], None, exc


def draft_initial(db: Database, writer: Personalizer, channel: str, *, limit: int,
                  deadline: float | None = None, log: Log = print) -> int:
    leads = db.leads_without_draft(channel, limit)
    fn = writer.draft_email if channel == "email" else writer.draft_dm
    drafted = 0
    for lead, draft, error in _run_parallel(leads, fn, deadline=deadline):
        if error:
            log(f"  #{lead['id']} failed: {error}")
        elif channel == "email":
            db.add_message(lead["id"], "email", draft.body, subject=draft.subject)
            log(f"  #{lead['id']} {lead['email']}: {draft.subject}")
            drafted += 1
        else:
            db.add_message(lead["id"], "instagram", draft.message)
            log(f"  #{lead['id']} @{lead['ig_username']}")
            drafted += 1
    return drafted


def draft_followups(db: Database, writer: Personalizer, settings: Settings, *,
                    deadline: float | None = None, log: Log = print) -> int:
    due = db.followups_due(settings.followup_after_days, settings.max_followups)
    jobs = [(last, db.get_lead(last["lead_id"]), db.thread_history(last["lead_id"], "email")) for last in due]
    drafted = 0
    for (last, lead, _), draft, error in _run_parallel(
        jobs, lambda job: writer.draft_followup(job[1], job[2]), deadline=deadline
    ):
        if error:
            log(f"  #{lead['id']} failed: {error}")
            continue
        db.add_message(lead["id"], "email", draft.body, kind="followup", step=last["step"] + 1,
                       subject=draft.subject, thread_id=last["thread_id"], rfc_message_id=last["rfc_message_id"])
        log(f"  #{lead['id']} {lead['email']}: follow-up {last['step'] + 1}")
        drafted += 1
    return drafted


# ---- sending -------------------------------------------------------------

def remaining_daily_quota(db: Database, cap: int) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    return max(0, cap - db.emails_sent_since(since))


def check_sendable(settings: Settings) -> None:
    missing = [name for name, value in (("SENDER_EMAIL", settings.sender_email),
                                        ("SENDER_NAME", settings.sender_name),
                                        ("PHYSICAL_ADDRESS", settings.physical_address)) if not value]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} in .env before sending (required for CAN-SPAM compliance)")


def send_approved_emails(db: Database, gmail: GmailChannel, settings: Settings, *, dry_run: bool = False,
                         deadline: float | None = None, sleep: Callable[[float], None] = time.sleep,
                         log: Log = print) -> int:
    """Send approved emails. With a `deadline` (time.monotonic()), spacing shrinks to fit the time left."""
    check_sendable(settings)
    quota = remaining_daily_quota(db, settings.daily_email_cap)
    queue = db.messages(status="approved", channel="email", limit=quota) if quota else []
    if not queue:
        log(f"Nothing to send (rolling 24h quota left: {quota}).")
        return 0

    sent = 0
    for i, row in enumerate(queue):
        if deadline and time.monotonic() > deadline:
            log(f"  time budget reached; {len(queue) - i} approved emails left for the next run")
            break
        lead = db.get_lead(row["lead_id"])
        if db.is_suppressed([lead["email"]]) or lead["status"] in ("replied", "bounced", "unsubscribed"):
            db.update_message(row["id"], status="rejected", error=f"lead {lead['status']}")
            continue
        if row["step"] == 0 and lead["email_status"] and lead["email_status"] not in SENDABLE_EMAIL_STATUSES:
            db.update_message(row["id"], status="rejected", error=f"email {lead['email_status']}")
            continue

        msg = build_email(sender_name=settings.sender_name, sender_email=settings.sender_email,
                          to=lead["email"], subject=row["subject"], body=row["body"],
                          physical_address=settings.physical_address, in_reply_to=row["rfc_message_id"])
        if dry_run:
            log(f"[dry-run] would send #{row['id']} to {lead['email']}: {row['subject']}")
            continue
        try:
            thread_id, rfc_id = gmail.send(msg, thread_id=row["thread_id"])
        except Exception as exc:
            db.update_message(row["id"], status="failed", error=str(exc))
            log(f"  failed #{row['id']} {lead['email']}: {exc}")
            continue
        db.mark_sent(row["id"], thread_id=thread_id, rfc_message_id=rfc_id)
        sent += 1
        log(f"  sent #{row['id']} -> {lead['email']}")
        if i < len(queue) - 1:
            # Human-like spacing protects sender reputation.
            delay = random.uniform(settings.min_delay_seconds, settings.max_delay_seconds)
            if deadline:
                fair_share = (deadline - time.monotonic()) / (len(queue) - i - 1)
                delay = max(0.0, min(delay, fair_share - 2))
            sleep(delay)
    return sent


IG_MAX_MESSAGE_CHARS = 1000


def ig_send_blocker(db: Database, row) -> tuple[str, str] | None:
    """Gates an approved Instagram reply must pass before sending; returns (status, reason) if blocked."""
    if not row["approved_at"]:
        return "pending", "approval was not recorded - review it again"
    if not row["reply_deadline"] or datetime.fromisoformat(row["reply_deadline"]) <= datetime.now(timezone.utc):
        return "expired", "reply window closed"
    if db.is_suppressed([row["ig_username"]]):
        return "rejected", "recipient is on the suppression list"
    if db.replied_since(row["lead_id"], row["created_at"]):
        return "rejected", "another reply was already sent for this message"
    if not row["body"].strip() or len(row["body"]) > IG_MAX_MESSAGE_CHARS:
        return "failed", f"message must be 1-{IG_MAX_MESSAGE_CHARS} characters"
    return None


def send_ig_replies(db: Database, ig: InstagramClient, *, confirm: Callable[[Any, float], bool],
                    log: Log = print) -> int:
    """Send approved replies to inbound DMs, after the send-time gates and a per-message confirmation."""
    sent = 0
    for row in db.messages(status="approved", channel="instagram"):
        if row["kind"] != "reply":
            continue
        if blocked := ig_send_blocker(db, row):
            status, reason = blocked
            db.update_message(row["id"], status=status, error=reason)
            log(f"  not sent to @{row['ig_username']}: {reason}")
            continue
        hours_left = (datetime.fromisoformat(row["reply_deadline"]) - datetime.now(timezone.utc)).total_seconds() / 3600
        if not confirm(row, hours_left):
            log(f"  skipped @{row['ig_username']} (left approved)")
            continue
        try:
            ig.send_reply(row["thread_id"], row["body"])
        except InstagramError as exc:
            db.update_message(row["id"], status="failed", error=str(exc))
            log(f"  failed @{row['ig_username']}: {exc}")
            continue
        db.mark_sent(row["id"])
        sent += 1
        log(f"  replied to @{row['ig_username']}")
    return sent


# ---- syncing -------------------------------------------------------------

def sync_gmail(db: Database, gmail: GmailChannel, *, replies: list[tuple[str, str]] | None = None,
               log: Log = print) -> dict[str, int]:
    """Update lead statuses from Gmail threads. New replies are appended to `replies` as (email, snippet)."""
    stats = {"replied": 0, "bounced": 0, "unsubscribed": 0}
    for row in db.sent_email_threads():
        status = gmail.thread_status(row["thread_id"])
        if status.unsubscribe:
            db.suppress(row["email"], "replied unsubscribe")
            stats["unsubscribed"] += 1
            log(f"  unsubscribed {row['email']}")
        elif status.replied:
            db.update_lead(row["lead_id"], status="replied")
            stats["replied"] += 1
            if replies is not None:
                replies.append((row["email"], status.reply_snippet))
            log(f"  REPLY from {row['email']}: {status.reply_snippet[:100]}")
        elif status.bounced:
            db.update_lead(row["lead_id"], status="bounced")
            db.suppress(row["email"], "bounced")
            stats["bounced"] += 1
            log(f"  bounced {row['email']}")

    # Catch bounce notices Gmail didn't thread with the original.
    known = {row["email"]: row["lead_id"] for row in db.sent_email_threads()}
    for snippet in gmail.bounced_recipients():
        for email in EMAIL_RE.findall(snippet):
            if email.lower() in known:
                db.update_lead(known[email.lower()], status="bounced")
                db.suppress(email, "bounced")
                stats["bounced"] += 1
    return stats


ARCHIVE_WINDOW = timedelta(days=90)  # how far back a conversation's history is fetched


def archive_conversation(db: Database, ig: InstagramClient, conversation_id: str, *, since: datetime,
                         igsid: str | None, username: str | None, updated_time: str | None) -> int:
    """Fetch and store messages newer than what's already archived (back to `since`). Returns new messages.

    The conversation's updated_time is recorded only after its messages are saved, so an
    interrupted run fetches it again next time.
    """
    db.save_ig_conversation(conversation_id, igsid=igsid, username=username, updated_time=None)
    latest = db.ig_latest_message_time(conversation_id)
    stop_before = max(since, datetime.fromisoformat(latest)) if latest else since
    added = db.add_ig_messages(conversation_id, ig.messages(conversation_id, stop_before=stop_before))
    db.save_ig_conversation(conversation_id, igsid=igsid, username=username, updated_time=updated_time)
    return added


def archive_instagram(db: Database, ig: InstagramClient, *, since: datetime, stats: dict[str, int] | None = None,
                      log: Log = print) -> dict[str, int]:
    """Store every DM conversation active since `since`. Resumable: unchanged conversations are skipped.

    `stats` is updated as it goes, so callers can report progress if the run stops early.
    """
    stats = stats if stats is not None else {}
    for key in ("conversations", "skipped", "failed", "messages"):
        stats.setdefault(key, 0)
    for convo in ig.iter_conversations(since):
        stats["conversations"] += 1
        other = ig.other_participant(convo) or {}
        updated = parse_time(convo["updated_time"]).astimezone(timezone.utc).isoformat(timespec="seconds")
        known = db.ig_conversation(convo["id"])
        if known and known["updated_time"] == updated:
            stats["skipped"] += 1
            continue
        try:
            added = archive_conversation(db, ig, convo["id"], since=since, igsid=other.get("id"),
                                         username=other.get("username"), updated_time=updated)
        except InstagramRateLimited:
            raise
        except InstagramError as exc:  # one unreadable chat shouldn't stop the archive; retried next run
            stats["failed"] += 1
            log(f"  @{other.get('username', '?')}: skipped ({exc})")
            continue
        stats["messages"] += added
        log(f"  @{other.get('username', '?')}: {added} new messages")
    return stats


def transcript_text(db: Database, conversation_id: str, ig_user_id: str, limit: int | None = None) -> str:
    """Stored conversation as 'them:' / 'us:' lines, oldest first."""
    return format_messages(db.ig_transcript(conversation_id, limit=limit), ig_user_id)


def sync_instagram(db: Database, ig: InstagramClient, writer: Personalizer, *, log: Log = print) -> int:
    """Archive recent DMs and draft replies for those still inside the reply window."""
    drafted = 0
    for convo in ig.conversations():
        if convo.conversation_id:
            # Continue from the last archived message so the stored history has no gaps.
            archive_conversation(db, ig, convo.conversation_id, since=datetime.now(timezone.utc) - ARCHIVE_WINDOW,
                                 igsid=convo.recipient_id, username=convo.username, updated_time=None)
        if not convo.can_reply or not convo.last_inbound_text or not convo.username:
            continue
        if db.is_suppressed([convo.username]):
            continue
        lead_id = db.upsert_lead(ig_username=convo.username, source="instagram")
        lead = db.get_lead(lead_id)
        if lead["status"] in ("new", "contacted"):
            db.update_lead(lead_id, status="replied")
        already = db.conn.execute(
            "SELECT 1 FROM messages WHERE lead_id = ? AND kind = 'reply' AND created_at >= ?",
            (lead_id, convo.last_inbound_at.isoformat(timespec="seconds")),
        ).fetchone()
        if already:
            continue
        ctx = (build_context(db, convo.conversation_id, ig.ig_user_id,
                             summarizer=getattr(writer, "summarize_conversation", None),
                             window_days=int(db.setting("context_window_days", str(DEFAULT_WINDOW_DAYS))))
               if convo.conversation_id else None)
        reply = writer.draft_dm_reply(convo.username, convo.last_inbound_text,
                                      history=ctx.recent if ctx else "", summary=ctx.summary if ctx else "")
        db.add_message(lead_id, "instagram", reply.message, kind="reply", thread_id=convo.recipient_id,
                       reply_deadline=(convo.last_inbound_at + REPLY_WINDOW).isoformat(timespec="seconds"))
        hours_left = (REPLY_WINDOW - (datetime.now(timezone.utc) - convo.last_inbound_at)).total_seconds() / 3600
        log(f"  @{convo.username}: \"{convo.last_inbound_text[:60]}\" -> reply drafted ({hours_left:.0f}h left)")
        drafted += 1
    return drafted
