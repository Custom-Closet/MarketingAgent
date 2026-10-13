"""The once-a-day job (run by Lambda every morning, or locally with `agent daily`).

1. Sync Gmail: replies, bounces, unsubscribes      (stops sequences before anything is sent)
2. Send emails approved since the last run          (spacing fitted into the time budget)
3. Discover leads from queued targets               (`agent targets add ...`)
4. Find emails for Instagram leads via Hunter
5. Draft new emails, follow-ups and Instagram DMs   (wait for `agent review`)
6. Draft replies to inbound Instagram DMs           (24h window)
7. Email a digest to the team

Each step is isolated: a failing step is reported in the digest and the job still finishes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import pipeline
from .channels.gmail import build_notification
from .config import Settings
from .db import Database

# Max time spent sending, so drafting still gets a share of the 15-minute Lambda run.
SEND_BUDGET_SECONDS = 6 * 60


@dataclass
class DailyReport:
    stats: dict[str, Any] = field(default_factory=dict)
    replies: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        print(message, flush=True)
        self.log_lines.append(message)


def _step(report: DailyReport, name: str, fn: Callable[[], Any]) -> None:
    report.log(f"== {name}")
    try:
        report.stats[name] = fn()
    except Exception as exc:  # keep going; surface it in the digest and the Lambda error metric
        report.errors.append(f"{name}: {type(exc).__name__}: {exc}")
        report.log(f"  ERROR {exc}")


def run(settings: Settings, db: Database, *, deadline: float, gmail=None, hunter=None, ig=None,
        writer=None) -> DailyReport:
    report = DailyReport()
    log = report.log

    if gmail:
        _step(report, "sync", lambda: pipeline.sync_gmail(db, gmail, replies=report.replies, log=log))
        send_deadline = min(deadline, time.monotonic() + SEND_BUDGET_SECONDS)
        _step(report, "sent", lambda: pipeline.send_approved_emails(db, gmail, settings, deadline=send_deadline,
                                                                    log=log))
    if hunter or ig:
        _step(report, "targets", lambda: pipeline.process_targets(
            db, settings, limit=settings.daily_discovery_limit, hunter=hunter, ig=ig, log=log))
    if hunter:
        _step(report, "enriched", lambda: pipeline.enrich_emails(db, hunter, log=log))
    if writer:
        _step(report, "email_drafts", lambda: pipeline.draft_initial(
            db, writer, "email", limit=settings.daily_draft_limit, deadline=deadline, log=log))
        _step(report, "followup_drafts", lambda: pipeline.draft_followups(
            db, writer, settings, deadline=deadline, log=log))
        if ig:
            _step(report, "dm_drafts", lambda: pipeline.draft_initial(
                db, writer, "instagram", limit=settings.daily_draft_limit, deadline=deadline, log=log))
            _step(report, "dm_reply_drafts", lambda: pipeline.sync_instagram(db, ig, writer, log=log))

    if gmail and settings.digest_email:
        _step(report, "digest", lambda: send_digest(db, gmail, settings, report))
    return report


def digest_text(db: Database, settings: Settings, report: DailyReport) -> tuple[str, str]:
    counts = db.counts()
    pending_email = counts["email"].get("pending", 0)
    pending_ig = counts["instagram"].get("pending", 0)
    approved_ig = counts["instagram"].get("approved", 0)
    quota = pipeline.remaining_daily_quota(db, settings.daily_email_cap)
    s = report.stats

    lines = [f"Daily run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    if report.replies:
        lines.append(f"NEW REPLIES ({len(report.replies)}) - answer these in Gmail:")
        lines += [f"  - {email}: {snippet[:160]}" for email, snippet in report.replies]
        lines.append("")
    lines += [
        "Waiting for you:",
        f"  - {pending_email} email drafts and {pending_ig} Instagram drafts to review  ->  agent review",
        f"  - {approved_ig} approved Instagram DMs to send by hand  ->  agent ig-queue",
    ]
    if s.get("dm_reply_drafts"):
        lines.append(f"  - {s['dm_reply_drafts']} replies to inbound DMs; the 24h window closes soon  ->  "
                     "agent review --channel instagram, then agent ig-inbox --send")
    sync = s.get("sync") or {}
    lines += [
        "",
        "Today:",
        f"  - sent {s.get('sent', 0)} emails (quota left in the rolling 24h: {quota}/{settings.daily_email_cap})",
        f"  - {sync.get('replied', 0)} replied, {sync.get('bounced', 0)} bounced, "
        f"{sync.get('unsubscribed', 0)} unsubscribed",
        f"  - {s.get('targets', 0)} targets processed, {s.get('enriched', 0)} emails found",
        f"  - drafted {s.get('email_drafts', 0)} emails, {s.get('followup_drafts', 0)} follow-ups, "
        f"{s.get('dm_drafts', 0)} DMs",
    ]
    if report.errors:
        lines += ["", "ERRORS:"] + [f"  - {e}" for e in report.errors]

    total = pending_email + pending_ig
    subject = f"[Marketing Agent] {total} drafts to review"
    if report.replies:
        subject += f", {len(report.replies)} new replies"
    if report.errors:
        subject += f" ({len(report.errors)} errors)"
    return subject, "\n".join(lines)


def send_digest(db: Database, gmail, settings: Settings, report: DailyReport) -> str:
    subject, body = digest_text(db, settings, report)
    gmail.send(build_notification(sender_email=settings.sender_email, to=settings.digest_email,
                                  subject=subject, body=body))
    return settings.digest_email
