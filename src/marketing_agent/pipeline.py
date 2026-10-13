"""Pipeline steps: discover -> enrich -> draft -> (human review) -> send -> sync."""

from __future__ import annotations

import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable

from .channels.gmail import GmailChannel, build_email
from .config import Settings
from .db import Database
from .personalize import Personalizer
from .sources.hunter import HunterClient, HunterError
from .sources.instagram import REPLY_WINDOW, InstagramClient, InstagramError

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


def send_ig_replies(db: Database, ig: InstagramClient, *, log: Log = print) -> int:
    """Send approved replies to inbound DMs (allowed by Meta within the 24h window)."""
    sent = 0
    for row in db.messages(status="approved", channel="instagram"):
        if row["kind"] != "reply":
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


def sync_instagram(db: Database, ig: InstagramClient, writer: Personalizer, *, log: Log = print) -> int:
    """Draft replies for inbound DMs still inside the 24h reply window."""
    drafted = 0
    for convo in ig.conversations():
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
        reply = writer.draft_dm_reply(convo.username, convo.last_inbound_text)
        db.add_message(lead_id, "instagram", reply.message, kind="reply", thread_id=convo.recipient_id)
        hours_left = (REPLY_WINDOW - (datetime.now(timezone.utc) - convo.last_inbound_at)).total_seconds() / 3600
        log(f"  @{convo.username}: \"{convo.last_inbound_text[:60]}\" -> reply drafted ({hours_left:.0f}h left)")
        drafted += 1
    return drafted
