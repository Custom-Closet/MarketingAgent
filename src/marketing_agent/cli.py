"""Command-line interface: `agent --help`."""

from __future__ import annotations

import csv
import os
import re
from contextlib import ExitStack
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import clients, pipeline
from .config import Settings, load_settings
from .db import Database
from .storage import LockBusy, open_database

app = typer.Typer(help="Lead discovery and approved-outreach agent for Gmail and Instagram.", no_args_is_help=True)
find_app = typer.Typer(help="Discover new leads.", no_args_is_help=True)
targets_app = typer.Typer(help="Queue discovery targets for the daily run.", no_args_is_help=True)
brands_app = typer.Typer(help="Daily list of emerging womenswear brands (USA + India).", no_args_is_help=True)
app.add_typer(find_app, name="find")
app.add_typer(targets_app, name="targets")
app.add_typer(brands_app, name="brands")
console = Console()

# Resources (the database) opened by a command; closed - and uploaded to S3 - when the command ends.
_resources = ExitStack()


@app.callback()
def _main(ctx: typer.Context) -> None:
    ctx.call_on_close(_resources.close)

BRAND_TEMPLATE = """\
# Brand profile

Claude reads this file to write every message. Be specific - vague input gives generic output.

## Who we are
<Brand name, what you sell, price range, what makes you different.>

## Ideal customer
<Who you're reaching out to: e.g. boutique owners, fitness creators with 10k-100k followers,
DTC founders. What problem do you solve for them?>

## Offer / call to action
<What you want them to do: reply for a wholesale catalog, try a free sample, book a 15-min call.>

## Voice
<e.g. warm, concise, a little playful. Words/phrases to avoid.>

## Proof points
<Customers, press, numbers you're allowed to mention.>
"""


def _ctx() -> tuple[Settings, Database]:
    """Settings plus the database, opened for this command and saved (to S3 if configured) when it exits."""
    settings = load_settings()
    try:
        db = _resources.enter_context(open_database(settings))
    except LockBusy as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    return settings, db


def _log(message: str) -> None:
    console.print(message, highlight=False)


def _require(client, what: str):
    if client is None:
        raise typer.BadParameter(f"{what} is not configured - see .env.example")
    return client


def _hunter(settings: Settings):
    return _require(clients.hunter_client(settings), "HUNTER_API_KEY")


def _instagram(settings: Settings):
    return _require(clients.instagram_client(settings), "IG_ACCESS_TOKEN / IG_USER_ID")


def _gmail(settings: Settings):
    return clients.gmail_client(settings, interactive=True)


def _writer(settings: Settings):
    return clients.writer(settings)


# ---- setup ---------------------------------------------------------------

@app.command()
def init() -> None:
    """Create the database and a brand.md template."""
    settings, _ = _ctx()
    if not settings.brand_file.exists():
        settings.brand_file.write_text(BRAND_TEMPLATE)
        _log(f"Created {settings.brand_file} - fill it in before drafting.")
    if not Path(".env").exists() and Path(".env.example").exists():
        shutil.copy(".env.example", ".env")
        _log("Created .env from .env.example - add your API keys.")
    _log(f"Database ready at {settings.db_path}")


@app.command("auth-gmail")
def auth_gmail() -> None:
    """Authorize Gmail access in the browser (one time)."""
    settings = load_settings()
    _gmail(settings)
    _log(f"Gmail authorized. Token saved to {settings.gmail_token_file}")


@app.command("push-secrets")
def push_secrets(secret_id: str = typer.Option("marketing-agent/secrets", help="Secrets Manager secret name")) -> None:
    """Upload API keys from .env and the Gmail token to AWS Secrets Manager for the Lambda run."""
    import json

    import boto3

    from .lambda_handler import SECRET_KEYS

    settings = load_settings()
    if not settings.gmail_token_file.exists():
        raise typer.BadParameter("Run `agent auth-gmail` first")
    values = {"ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
              "AWS_BEARER_TOKEN_BEDROCK": settings.bedrock_api_key,
              "HUNTER_API_KEY": settings.hunter_api_key,
              "IG_ACCESS_TOKEN": settings.ig_access_token,
              "GMAIL_TOKEN_JSON": settings.gmail_token_file.read_text()}
    values = {k: v for k, v in values.items() if k in SECRET_KEYS and v}
    boto3.client("secretsmanager").put_secret_value(SecretId=secret_id, SecretString=json.dumps(values))
    _log(f"Stored {', '.join(sorted(values))} in {secret_id}.")


# ---- discovery -----------------------------------------------------------

@find_app.command("domain")
def find_domain(
    domains: list[str] = typer.Argument(..., help="Company domains, e.g. acme.com"),
    limit: int = typer.Option(10, help="Max contacts per domain"),
    seniority: Optional[str] = typer.Option(None, help="junior, senior or executive"),
    department: Optional[str] = typer.Option(None, help="e.g. marketing, sales, executive"),
    verify: bool = typer.Option(True, help="Verify each email with Hunter (uses verification credits)"),
) -> None:
    """Find people and emails at company domains via Hunter.io."""
    settings, db = _ctx()
    hunter = _hunter(settings)
    total = 0
    for domain in domains:
        _log(f"[bold]{domain}[/bold]")
        total += pipeline.discover_domain(db, hunter, domain, limit=limit, seniority=seniority,
                                          department=department, verify=verify, log=_log)
    _log(f"{total} leads saved.")


@find_app.command("instagram")
def find_instagram(
    user: list[str] = typer.Option([], "--user", "-u", help="Instagram username (repeatable)"),
    hashtag: list[str] = typer.Option([], "--hashtag", "-t", help="Hashtag to mine for @mentions (repeatable)"),
    file: Optional[Path] = typer.Option(None, help="Text file with one username per line"),
    min_followers: int = typer.Option(0, help="Skip accounts below this follower count"),
) -> None:
    """Enrich Instagram business/creator accounts into leads."""
    settings, db = _ctx()
    ig = _instagram(settings)
    usernames = {u.lower().lstrip("@") for u in user}
    if file:
        usernames |= {line.strip().lower().lstrip("@") for line in file.read_text().splitlines() if line.strip()}
    for tag in hashtag:
        mentions = ig.hashtag_mentions(tag)
        _log(f"#{tag}: {len(mentions)} accounts mentioned")
        usernames |= mentions
    if not usernames:
        raise typer.BadParameter("Pass --user, --hashtag or --file")
    added = pipeline.discover_instagram(db, ig, usernames, min_followers=min_followers, log=_log)
    _log(f"{added} Instagram leads saved. Run `agent enrich` to find emails for their websites.")


@find_app.command("csv")
def find_csv(path: Path) -> None:
    """Import leads from a CSV with any of: name,email,company,domain,position,ig_username,website,notes."""
    _, db = _ctx()
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        fields = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        fields["source"] = "csv"
        db.upsert_lead(**fields)
    _log(f"Imported {len(rows)} rows.")


@targets_app.command("add")
def targets_add(
    kind: str = typer.Argument(..., help="domain, instagram or hashtag"),
    values: list[str] = typer.Argument(..., help="e.g. acme.com / cafeluna / specialtycoffee"),
) -> None:
    """Queue targets; the daily run discovers leads from them (DAILY_DISCOVERY_LIMIT per day)."""
    kinds = {"domain": "domain", "instagram": "ig_user", "hashtag": "hashtag"}
    if kind not in kinds:
        raise typer.BadParameter("kind must be domain, instagram or hashtag")
    _, db = _ctx()
    added = sum(db.add_target(kinds[kind], v) for v in values)
    _log(f"Queued {added} new {kind} targets ({len(values) - added} already queued).")


@targets_app.command("list")
def targets_list() -> None:
    """Show the most recent targets and their results."""
    _, db = _ctx()
    table = Table("Kind", "Value", "Status", "Result")
    for t in db.targets():
        table.add_row(t["kind"], t["value"], t["status"], t["result"] or "")
    console.print(table)


# ---- brand finder --------------------------------------------------------

@brands_app.command("run")
def brands_run(
    country: Optional[str] = typer.Option(None, help="Country codes, e.g. us,in,gb (default: the app's search setup)"),
    per_country: Optional[int] = typer.Option(None, help="Brands per country (default: the search setup)"),
    minutes: float = typer.Option(14, help="Time budget for the run"),
    email: bool = typer.Option(True, help="Email the list to DIGEST_EMAIL"),
    model: bool = typer.Option(True, help="Let the model judge fit and region (else keywords only)"),
) -> None:
    """Find today's brands: search + snowball + hashtag sweeps, verified with Business Discovery."""
    import time

    from . import brands

    countries = tuple(c.strip().lower() for c in country.split(",") if c.strip()) if country else None
    if countries and any(len(c) != 2 for c in countries):
        raise typer.BadParameter("use 2-letter country codes, e.g. us,in,gb")
    settings, db = _ctx()
    search = brands.search_client(settings)
    if search is None:
        _log("[yellow]No BRAVE_SEARCH_API_KEY / SERPER_API_KEY: skipping web search sweeps.[/yellow]")
    llm = clients.llm(settings) if model and clients.llm_configured(settings) else None
    run = brands.run_job(settings, db, ig=_instagram(settings), search=search, llm=llm,
                         gmail=_gmail(settings) if email else None, per_country=per_country, countries=countries,
                         deadline=time.monotonic() + minutes * 60, log=_log)
    console.print(brands.render(run)[1], highlight=False)


def _seed_entries(path: Path) -> list[tuple[str | None, str]]:
    """(handle or None, name) per line of a list from another tool: IG links, @handles, or brand names."""
    from .sources.web_search import handle_from_instagram_url

    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("-*0123456789.) ").strip()
        if not line or line.startswith("#"):
            continue
        handle = handle_from_instagram_url(line)
        if not handle and (m := re.search(r"(?<![\w.])@([A-Za-z0-9_.]{1,30})", line)):
            handle = m.group(1).lower().rstrip(".")
        name = re.split(r"\s+[—–|-]\s+|\t|,", line)[0].strip(" *_")
        entries.append((handle, name))
    return entries


@brands_app.command("seed")
def brands_seed(
    path: Path = typer.Argument(..., help="Text/Markdown/CSV list: Instagram links, @handles or brand names"),
    source: str = typer.Option("", help="Label stored with them (default: the file name)"),
) -> None:
    """Import brands found elsewhere: never delivered again, and mined for similar brands (snowball)."""
    from . import brands
    from .sources.web_search import handle_from_instagram_url

    settings, db = _ctx()
    search = brands.search_client(settings)
    added, unresolved = 0, []
    for handle, name in _seed_entries(path):
        if not handle and search and name:
            hits = search.search(f'site:instagram.com "{name}"', count=5)
            handle = next((h for h in (handle_from_instagram_url(r.url) for r in hits) if h), None)
        if not handle:
            unresolved.append(name)
            continue
        if not db.brand_candidate(handle):
            db.save_brand_candidate(handle, "known", name=name or None, reason=f"seed: {source or path.name}",
                                    sources="seed")
            added += 1
    _log(f"{added} brands added to the ledger.")
    if unresolved:
        hint = "" if search else " (set BRAVE_SEARCH_API_KEY or SERPER_API_KEY to look names up)"
        _log(f"[yellow]No Instagram handle for {len(unresolved)}{hint}:[/yellow] " + ", ".join(unresolved[:20]))


@brands_app.command("list")
def brands_list(day: Optional[str] = typer.Option(None, "--date", help="Run date YYYY-MM-DD (default: recent)")) -> None:
    """Show delivered brands."""
    _, db = _ctx()
    table = Table("Date", "Region", "Brand", "Instagram", "Followers", "Founder", "Website")
    for r in db.brand_leads(delivered_on=day):
        table.add_row(r["delivered_on"], r["region"] or "", r["name"] or "", f"instagram.com/{r['username']}",
                      str(r["followers"] or ""), r["founder"] or "", r["website"] or "")
    console.print(table)


@app.command()
def enrich(verify: bool = typer.Option(True, help="Verify found emails with Hunter")) -> None:
    """Use Hunter.io to find emails for leads that only have a website/domain."""
    settings, db = _ctx()
    found = pipeline.enrich_emails(db, _hunter(settings), verify=verify, log=_log)
    _log(f"Found {found} emails.")


# ---- drafting & review ---------------------------------------------------

@app.command()
def draft(
    channel: str = typer.Option("email", help="email or instagram"),
    limit: int = typer.Option(20, help="Max new drafts"),
    followups: bool = typer.Option(False, help="Also draft follow-ups for unanswered emails"),
) -> None:
    """Have Claude write personalized drafts. Drafts wait for `agent review`."""
    if channel not in ("email", "instagram"):
        raise typer.BadParameter("channel must be email or instagram")
    settings, db = _ctx()
    writer = _writer(settings)
    n = pipeline.draft_initial(db, writer, channel, limit=limit, log=_log)
    if followups and channel == "email":
        n += pipeline.draft_followups(db, writer, settings, log=_log)
    _log(f"{n} drafts created. Next: `agent review`")


@app.command()
def review(channel: Optional[str] = typer.Option(None, help="email or instagram")) -> None:
    """Approve, edit or reject pending drafts one by one."""
    _, db = _ctx()
    queue = db.messages(status="pending", channel=channel)
    if not queue:
        _log("No pending drafts.")
        return
    approved = 0
    for i, row in enumerate(queue, 1):
        to = row["email"] if row["channel"] == "email" else f"@{row['ig_username']}"
        title = f"[{i}/{len(queue)}] {row['channel']} {row['kind']} -> {to}"
        if row["company"]:
            title += f"  ({row['company']})"
        body = f"[bold]{row['subject']}[/bold]\n\n{row['body']}" if row["subject"] else row["body"]
        console.print(Panel(body, title=title, title_align="left"))
        choice = typer.prompt("[a]pprove  [e]dit  [r]eject  [s]kip  [q]uit", default="s").lower()[:1]
        if choice == "q":
            break
        if choice == "e":
            text = f"{row['subject']}\n---\n{row['body']}" if row["subject"] else row["body"]
            edited = typer.edit(text)
            if edited:
                if row["subject"] and "\n---\n" in edited:
                    subject, new_body = edited.split("\n---\n", 1)
                    db.update_message(row["id"], subject=subject.strip(), body=new_body.strip())
                else:
                    db.update_message(row["id"], body=edited.strip())
            choice = "a"
        if choice == "a":
            db.approve_message(row["id"])
            approved += 1
        elif choice == "r":
            db.update_message(row["id"], status="rejected")
    _log(f"{approved} approved. Next: `agent send` (email) or `agent ig-queue` (Instagram).")


# ---- sending -------------------------------------------------------------

@app.command()
def send(dry_run: bool = typer.Option(False, help="Show what would be sent without sending")) -> None:
    """Send approved emails via Gmail, within the daily cap, spaced out over time."""
    settings, db = _ctx()
    gmail = None if dry_run else _gmail(settings)
    n = pipeline.send_approved_emails(db, gmail, settings, dry_run=dry_run, log=_log)
    _log(f"{n} emails sent.")


@app.command("ig-queue")
def ig_queue() -> None:
    """Assisted Instagram DMs: copies each approved message and opens the profile for you to send."""
    _, db = _ctx()
    queue = [m for m in db.messages(status="approved", channel="instagram") if m["kind"] == "initial"]
    if not queue:
        _log("No approved Instagram DMs.")
        return
    has_pbcopy = shutil.which("pbcopy") is not None
    for i, row in enumerate(queue, 1):
        console.print(Panel(row["body"], title=f"[{i}/{len(queue)}] @{row['ig_username']}", title_align="left"))
        if has_pbcopy:
            subprocess.run(["pbcopy"], input=row["body"].encode(), check=False)
            _log("Copied to clipboard.")
        webbrowser.open(f"https://ig.me/m/{row['ig_username']}")
        choice = typer.prompt("Sent? [y]es  [n]o/skip  [r]eject  [q]uit", default="y").lower()[:1]
        if choice == "q":
            break
        if choice == "y":
            db.mark_sent(row["id"])
        elif choice == "r":
            db.update_message(row["id"], status="rejected")


def _confirm_ig_send(row, hours_left: float) -> bool:
    """Final human gate: show exactly what will be sent and require an explicit yes."""
    console.print(Panel(row["body"], title=f"Send to @{row['ig_username']}?  ({hours_left:.1f}h left in reply window)",
                        title_align="left"))
    return typer.prompt("Type y to send, anything else to skip", default="n").strip().lower() == "y"


@app.command("ig-inbox")
def ig_inbox(send_approved: bool = typer.Option(False, "--send", help="Send approved replies, confirming each")) -> None:
    """Draft replies to inbound DMs (24h window), or send the approved ones with --send."""
    settings, db = _ctx()
    ig = _instagram(settings)
    if send_approved:
        n = pipeline.send_ig_replies(db, ig, confirm=_confirm_ig_send, log=_log)
        _log(f"{n} replies sent.")
    else:
        n = pipeline.sync_instagram(db, ig, _writer(settings), log=_log)
        _log(f"{n} replies drafted. Review with `agent review --channel instagram`, then `agent ig-inbox --send`.")


@app.command("ig-archive")
def ig_archive(days: int = typer.Option(90, help="Store conversations active in the last N days")) -> None:
    """Save Instagram DM history to the local database (read-only on Instagram; resumable)."""
    from datetime import datetime, timedelta, timezone

    from .sources.instagram import InstagramRateLimited

    settings, db = _ctx()
    ig = _instagram(settings)
    stats: dict[str, int] = {}
    try:
        pipeline.archive_instagram(db, ig, since=datetime.now(timezone.utc) - timedelta(days=days), stats=stats, log=_log)
    except (InstagramRateLimited, KeyboardInterrupt) as exc:
        _log(f"[yellow]Stopped early ({exc or 'interrupted'}). Progress is saved - run again to continue.[/yellow]")
    _log(f"{stats.get('conversations', 0)} conversations checked, {stats.get('skipped', 0)} unchanged, "
         f"{stats.get('failed', 0)} failed, {stats.get('messages', 0)} new messages stored.")


@app.command("build-brand")
def build_brand(
    days: int = typer.Option(90, help="Learn from conversations active in the last N days"),
    out: Optional[Path] = typer.Option(None, help="Draft file to write (default: brand.draft.md next to brand.md)"),
    yes: bool = typer.Option(False, "--yes", help="Skip the cost confirmation"),
) -> None:
    """Draft a brand profile (FAQ, policies, sales playbook) from archived DMs. Run `agent ig-archive` first."""
    from datetime import datetime, timedelta, timezone

    from .brand_builder import BrandBuilder

    settings, db = _ctx()
    out = out or settings.brand_file.with_name("brand.draft.md")
    if out.resolve() == settings.brand_file.resolve():
        raise typer.BadParameter("--out must not be brand.md itself; review the draft and rename it yourself")
    if not clients.llm_configured(settings):
        raise typer.BadParameter("No model configured - set ANTHROPIC_API_KEY, or LLM_PROVIDER=bedrock with BEDROCK_* in .env")
    builder = BrandBuilder(settings.brand_file, llm=clients.llm(settings))
    plan = builder.plan(db, settings.ig_user_id, datetime.now(timezone.utc) - timedelta(days=days))
    if not plan.chats:
        _log("No archived conversations in that period. Run `agent ig-archive` first.")
        raise typer.Exit(1)
    cost = f"~${plan.est_cost_usd:.2f}" if plan.est_cost_usd is not None else "billed by AWS (see Bedrock pricing)"
    _log(f"{plan.chats} conversations in {len(plan.batches)} batches, {plan.input_tokens:,} transcript tokens, "
         f"{clients.describe_llm(settings)}: estimated cost {cost}.")
    if not yes and not typer.confirm("Send these conversations to the model and draft the profile?", default=False):
        raise typer.Exit()
    out.write_text(builder.build(plan, log=_log))
    _log(f"Draft written to {out}. Review it - especially '## Needs confirmation' - then replace {settings.brand_file}.")


@app.command()
def serve(
    port: int = typer.Option(8000, help="Port for the local inbox"),
    host: str = typer.Option("127.0.0.1", help="Use 0.0.0.0 to open it from a phone on the same Wi-Fi"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the inbox in the browser"),
) -> None:
    """Open the local inbox: review, bulk-approve and send Instagram replies."""
    import threading

    import uvicorn

    from .db import Database
    from .inbox.api import create_app
    from .inbox.faq import load_faq_file
    from .inbox.repository import InboxRepository
    from dataclasses import replace

    from .inbox.service import SYNC_EVERY, InboxService
    from .insights.service import InsightsService
    from .insights.strategist import Strategist
    from .models import SETTING_KEY as MODEL_SETTING
    from .models import ModelManager, list_bedrock_text_models
    from .personalize import Personalizer

    settings = load_settings()
    if settings.db_s3_uri:
        raise typer.BadParameter("`agent serve` uses the local database; unset AGENT_DB_S3_URI to run it")
    db = Database(settings.db_path, check_same_thread=False)
    repo = InboxRepository(db)
    added = repo.seed_faqs(load_faq_file(settings.faq_file))
    # The model picked in Settings (stored in the database) wins over BEDROCK_MODEL_ID.
    if settings.llm_provider == "bedrock":
        settings = replace(settings, bedrock_model_id=db.setting(MODEL_SETTING) or settings.bedrock_model_id)
    writer = None
    if not clients.llm_configured(settings):
        missing = ("BEDROCK_MODEL_ID / BEDROCK_REGION / AWS_BEARER_TOKEN_BEDROCK" if settings.llm_provider == "bedrock"
                   else "ANTHROPIC_API_KEY")
        _log(f"[yellow]{missing} not set - 'Draft with Claude' and the strategist are off "
             "(on Bedrock you can pick a model in Settings).[/yellow]")
    else:
        try:
            writer = clients.writer(settings)
            _log(f"Drafting with {clients.describe_llm(settings)}")
        except FileNotFoundError:
            _log("[yellow]brand.md not found - 'Draft with Claude' and the strategist are off.[/yellow]")
    service = InboxService(repo, our_id=settings.ig_user_id, ig=clients.instagram_client(settings), writer=writer)
    service.refresh_drafts()
    strategist = Strategist(settings.brand_file.read_text(), llm=writer.llm) if writer else None
    insights = InsightsService(db, our_id=settings.ig_user_id, lock=service.lock, strategist=strategist)
    from . import brands
    from .prospecting.service import ProspectingService
    prospecting = ProspectingService(
        db, lock=service.lock, ig_factory=lambda: clients.instagram_client(settings),
        search_factory=lambda: brands.search_client(settings),
        llm_getter=lambda: service.writer.llm if service.writer else None,   # follows the model picked in Settings
        run_db_factory=lambda: Database(settings.db_path, check_same_thread=False))

    def use_model(llm) -> None:
        if not settings.brand_file.exists():
            raise ValueError("brand.md not found - create it before drafting")
        service.writer = Personalizer(settings.brand_file, llm=llm)
        insights.strategist = Strategist(settings.brand_file.read_text(), llm=llm)

    from .brand_profile import BrandProfileService

    def current_llm():
        if service.writer:
            return service.writer.llm                  # the model picked in Settings
        return clients.llm(settings) if clients.llm_configured(settings) else None

    def reload_brand() -> None:
        """A saved brand.md takes effect for the next draft and strategist answer."""
        if (llm := current_llm()) is not None:
            use_model(llm)

    brand = BrandProfileService(db, settings.brand_file, lock=service.lock, llm_getter=current_llm,
                                on_change=reload_brand)

    models = ModelManager(
        db, provider=settings.llm_provider, default_model=settings.bedrock_model_id,
        factory=lambda model_id: clients.llm(replace(settings, llm_provider="bedrock", bedrock_model_id=model_id)),
        lister=lambda: list_bedrock_text_models(settings.bedrock_region), on_switch=use_model)
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    _log(f"Inbox at {url}  ({added} new FAQs loaded from {settings.faq_file}). Ctrl+C to stop.")
    if service.ig is not None:
        service.start_background_jobs()
        _log(f"Checking Instagram now and every {int(SYNC_EVERY.total_seconds() // 60)} minutes. "
             f"Auto-send of trusted FAQ answers is {'on' if service.auto_send_enabled() else 'off'} (Settings).")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(service, insights, models, prospecting, brand), host=host, port=port, log_level="warning")


# ---- tracking ------------------------------------------------------------

@app.command()
def sync() -> None:
    """Check Gmail threads for replies, bounces and unsubscribes."""
    settings, db = _ctx()
    stats = pipeline.sync_gmail(db, _gmail(settings), log=_log)
    _log(", ".join(f"{v} {k}" for k, v in stats.items()))


@app.command()
def daily() -> None:
    """Run the daily job locally (the same steps Lambda runs each morning)."""
    import time

    from . import daily as daily_job

    settings, db = _ctx()
    report = daily_job.run(settings, db, deadline=time.monotonic() + 30 * 60,
                           **clients.build_clients(settings, interactive=True))
    if report.errors:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show pipeline counts."""
    _, db = _ctx()
    counts = db.counts()
    table = Table("Group", "Status", "Count")
    for group, by_status in counts.items():
        for state, n in sorted(by_status.items()):
            table.add_row(group, state, str(n))
    console.print(table)
    settings = load_settings()
    if settings.db_s3_uri:
        _log(f"Database: {settings.db_s3_uri}")
    _log(f"Email quota left (rolling 24h): {pipeline.remaining_daily_quota(db, settings.daily_email_cap)}"
         f"/{settings.daily_email_cap}")


@app.command()
def unsubscribe(contact: str = typer.Argument(..., help="Email address or Instagram username")) -> None:
    """Permanently stop contacting someone."""
    _, db = _ctx()
    db.suppress(contact, "manual")
    _log(f"{contact} added to the suppression list.")


if __name__ == "__main__":
    app()
