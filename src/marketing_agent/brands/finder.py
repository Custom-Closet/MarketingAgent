"""Daily list of emerging womenswear brands (USA + India), verified through official APIs.

    sweeps (search, site, snowball, hashtag)  ->  candidate pool, minus the seen-ledger
    -> Business Discovery per candidate (profile + last 5 posts, verbatim)
    -> numeric checks (followers, posts, activity)  ->  fit + region (model, or keywords)
    -> ledger (brand_candidates)  ->  dated run file, email, and optionally the outreach pipeline

Leads beyond today's quota stay in the ledger as a backlog and are delivered first next run.
Nothing is padded: when fewer qualify, the run says so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..db import Database
from ..llm import LLMError
from ..sources.instagram import InstagramError, InstagramRateLimited, domain_from_url, extract_mentions
from ..sources.web_search import WebSearch
from . import judge as judge_mod
from .criteria import RECENT_MEDIA_LIMIT, BrandLead, Screened, hard_filters, keyword_verdict
from .config import SearchConfig
from .geo import country_name
from .sweeps import Candidate, CandidatePool, hashtag_sweep, hashtags_for_run, rotate, search_queries, search_sweep, snowball_sweep

Log = Callable[[str], None]

RECHECK_AFTER = timedelta(days=45)     # rejected / unavailable handles are looked at again after this
BACKLOG_MAX_AGE = timedelta(days=14)   # undelivered leads older than this get re-verified instead
SEARCH_SHARE = 0.3                     # of the run's time spent on web search before verifying
SNOWBALL_SEEDS = 20
DEFAULT_COUNTRIES = ("us", "in")


@dataclass
class BrandRun:
    run_date: str
    wanted: list[str]                 # country codes, in the order of the search setup
    leads_per_run: int
    max_per_country: int
    leads: dict[str, list[BrandLead]] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    stopped: str = ""     # why verification ended before the quota was met


def _lead_from_row(row) -> BrandLead:
    return BrandLead(row["region"], row["username"], row["name"] or row["username"], row["followers"] or 0,
                     row["founder"] or "", row["website"] or "", row["bio"] or "",
                     datetime.fromisoformat(row["last_post"]),
                     signals=[s for s in (row["signals"] or "").split("; ") if s],
                     sources=set(filter(None, (row["sources"] or "").split(","))))


def _save_lead(db: Database, lead: BrandLead, **found: Any) -> None:
    db.save_brand_candidate(lead.username, "lead", region=lead.region, name=lead.name, followers=lead.followers,
                            founder=lead.founder, website=lead.website, bio=lead.bio,
                            last_post=lead.last_post.isoformat(), signals="; ".join(lead.signals),
                            sources=",".join(sorted(lead.sources)), **found)


def _found(candidate: Candidate, search_id: int | None) -> dict[str, Any]:
    """Where a candidate came from, saved with every outcome (kept if already set)."""
    return {k: v for k, v in {"search_id": search_id, "query": candidate.query,
                              "sources": ",".join(sorted(candidate.sources))}.items() if v}


def backlog(db: Database, wanted: set[str]) -> list[BrandLead]:
    since = (datetime.now(timezone.utc) - BACKLOG_MAX_AGE).isoformat(timespec="seconds")
    rows = db.conn.execute(
        "SELECT * FROM brand_candidates WHERE status = 'lead' AND delivered_on IS NULL AND checked_at >= ?",
        (since,)).fetchall()
    return [_lead_from_row(r) for r in rows if r["region"] in wanted and r["last_post"]]


def find_brands(db: Database, *, ig, search: WebSearch | None = None, llm=None,
                countries: tuple[str, ...] | None = None, leads_per_run: int | None = None,
                max_per_country: int = 20, search_queries_limit: int = 100, lookup_limit: int = 300,
                hashtag_budget: int = 0, deadline: float, log: Log = print,
                config: SearchConfig | None = None, search_id: int | None = None) -> BrandRun:
    """Find up to `leads_per_run` prospects, at most `max_per_country` from any one country.

    `config` (the app's search setup) decides what is searched and, unless `countries` is given, where;
    `search_id` tags what this run finds.
    """
    ordered = list(countries or (config.countries if config else DEFAULT_COUNTRIES))
    wanted = set(ordered)
    total = leads_per_run or max_per_country * len(ordered)
    places = config.place_index() if config else None
    run = BrandRun(date.today().isoformat(), ordered, total, max_per_country, {c: [] for c in ordered})
    stats = run.stats
    for lead in backlog(db, wanted):
        run.leads[lead.region].append(lead)
    stats["backlog"] = sum(len(v) for v in run.leads.values())

    def full_regions() -> set[str]:
        """Countries that can't take more this run (all of them once the run's total is reached)."""
        if sum(len(v) for v in run.leads.values()) >= total:
            return set(wanted)
        return {c for c in wanted if len(run.leads[c]) >= max_per_country}

    pool = CandidatePool(db.brand_usernames_checked_since(datetime.now(timezone.utc) - RECHECK_AFTER))
    for row in db.queued_brand_candidates():
        for source in filter(None, (row["sources"] or "").split(",")):
            pool.add(row["username"], source, followers=row["followers"], region=row["region"], query=row["query"])
    stats["carried_over"] = len(pool)
    lookups = 0
    try:
        if search and full_regions() != wanted:
            log("== brand search sweeps")
            cursor = int(db.setting("brand_search_cursor", "0"))
            queries = search_queries(wanted, config)
            extra = [q for q in queries if not q[2]]            # hand-written queries run every time
            rotating = queries[len(extra):]
            todays = extra + rotate(rotating, cursor, max(0, search_queries_limit - len(extra)))
            search_deadline = time.monotonic() + max(0.0, deadline - time.monotonic()) * SEARCH_SHARE
            record = (lambda q, kind, region, results, new: db.add_search_query(search_id, q, kind, region, results, new)
                      ) if search_id else None
            stats.update(search_sweep(search, todays, pool, deadline=search_deadline, log=log, on_query=record))
            # Only move past queries that ran, so a failed day (bad key, plan limit) doesn't skip them.
            ran = max(0, stats["queries"] - len(extra))
            db.set_setting("brand_search_cursor", str((cursor + ran) % max(1, len(rotating))))
            log(f"  {stats.get('queries', 0)} queries, {stats.get('sites', 0)} brand sites -> {len(pool)} candidates")

        log("== snowball from known brands")
        lookups += snowball_sweep(ig, db, pool, limit=SNOWBALL_SEEDS, deadline=deadline, log=log)

        if hashtag_budget > 0:
            log("== hashtags")
            cursor = int(db.setting("brand_hashtag_cursor", "0"))
            tags = hashtags_for_run(db, hashtag_budget, cursor)
            db.set_setting("brand_hashtag_cursor", str(cursor + len(tags)))
            stats["hashtags"] = hashtag_sweep(ig, db, tags, pool, deadline=deadline, log=log)
        stats["candidates"] = len(pool)
        stats["dropped_by_snippet"] = pool.dropped
        log(f"== verifying ({len(pool)} candidates)")
        lookups += _verify(db, ig, llm, pool, run, wanted, full_regions, lookup_limit - lookups, deadline, log,
                           search_id, places)
    except InstagramRateLimited as exc:
        run.stopped = f"Instagram rate limit: {exc}"
        log(f"  stopping: {exc}")
    stats["lookups"] = lookups
    # Whatever wasn't checked waits for the next run instead of being lost.
    for c in pool.items.values():
        db.save_brand_candidate(c.username, "queued", region=c.region_hint, followers=c.followers_hint,
                                **_found(c, search_id))
    stats["queued"] = len(pool)
    if not run.stopped and full_regions() != wanted:
        run.stopped = ("ran out of candidates" if not pool else
                       "ran out of time" if time.monotonic() > deadline else "lookup limit reached")

    # Best first within each country's cap, then the run's total across countries.
    capped = [l for c in run.leads for l in sorted(run.leads[c], key=lambda l: -l.score)[:max_per_country]]
    keep = {l.username for l in sorted(capped, key=lambda l: -l.score)[:total]}
    for c in run.leads:
        run.leads[c] = [l for l in sorted(run.leads[c], key=lambda l: -l.score) if l.username in keep]
    db.mark_delivered([l.username for leads in run.leads.values() for l in leads], run.run_date)
    return run


def _verify(db: Database, ig, llm, pool: CandidatePool, run: BrandRun, wanted: set[str],
            full_regions: Callable[[], set[str]], budget: int, deadline: float, log: Log,
            search_id: int | None = None, places: dict[str, str] | None = None) -> int:
    stats = run.stats
    pending: list[tuple[Screened, Candidate]] = []
    lookups = 0

    def settle(batch: list[tuple[Screened, Candidate]]) -> None:
        screened = [s for s, _ in batch]
        if llm is not None:
            try:
                judge_mod.judge(llm, screened, wanted)
            except (LLMError, ValueError) as exc:     # ValueError covers replies that don't match the schema
                log(f"  model check failed ({str(exc)[:120]}); using keywords for this batch")
                for s in screened:
                    s.lead, s.reason = None, ""
                    keyword_verdict(s, wanted)
        else:
            for s in screened:
                keyword_verdict(s, wanted)
        for s, cand in batch:
            if s.lead:
                s.lead.sources = cand.sources
                _save_lead(db, s.lead, **{k: v for k, v in _found(cand, search_id).items() if k != "sources"})
                run.leads[s.lead.region].append(s.lead)
                log(f"  LEAD @{s.lead.username} ({s.lead.region}, {s.lead.followers} followers)")
            else:
                db.save_brand_candidate(s.profile.username, "rejected", reason=s.reason,
                                        followers=s.profile.followers_count, **_found(cand, search_id))
                log(f"  skip @{s.profile.username}: {s.reason}")

    while lookups < budget and time.monotonic() < deadline and full_regions() != wanted:
        candidate = pool.take(full_regions())
        if candidate is None:
            break
        lookups += 1
        try:
            profile = ig.business_discovery(candidate.username, media_limit=RECENT_MEDIA_LIMIT)
        except InstagramRateLimited:
            if pending:
                settle(pending)
            raise
        except InstagramError:
            # Personal account, renamed or gone: Business Discovery can't see it.
            db.save_brand_candidate(candidate.username, "unavailable", reason="not a business/creator account",
                                    **_found(candidate, search_id))
            stats["unavailable"] = stats.get("unavailable", 0) + 1
            continue
        screened, reason = hard_filters(profile, places=places)
        if screened is None:
            db.save_brand_candidate(profile.username, "rejected", reason=reason,
                                    followers=profile.followers_count, **_found(candidate, search_id))
            log(f"  skip @{profile.username}: {reason}")
            continue
        # Active small accounts in this space tag their collaborators: free candidates.
        for handle in extract_mentions(f"{profile.biography} {screened.captions}") - {profile.username.lower()}:
            pool.add(handle, "snowball", region=screened.region)
        pending.append((screened, candidate))
        if llm is None or len(pending) >= judge_mod.BATCH_SIZE:
            settle(pending)
            pending = []
    if pending:
        settle(pending)
    return lookups


# ---- delivery --------------------------------------------------------------------

def _line(i: int, lead: BrandLead) -> str:
    parts = [f"{i}. {lead.name}", lead.link, f"{lead.followers:,} followers"]
    if lead.founder:
        parts.append(f"Founder: {lead.founder}")
    if lead.website:
        parts.append(lead.website)
    return " — ".join(parts)


def render(run: BrandRun) -> tuple[str, str]:
    """(subject, body): fields only - name, Instagram link, followers, founder, website."""
    found = sum(len(v) for v in run.leads.values())
    counts = " + ".join(f"{len(run.leads[c])} {c.upper()}" for c in run.wanted if run.leads[c]) or "none"
    subject = f"[Brand leads] {run.run_date}: {counts}"
    lines = [f"Brand leads - {run.run_date}", ""]
    for country in run.wanted:
        leads = run.leads[country]
        if not leads:
            continue
        lines.append(f"{country_name(country)} ({len(leads)})")
        lines += [_line(i, lead) for i, lead in enumerate(leads, 1)]
        lines.append("")
    if found < run.leads_per_run:
        lines.append(f"Only {found} of {run.leads_per_run} qualified today; nothing was padded.")
    if run.stopped:
        lines.append(f"Stopped early: {run.stopped}")
    s = run.stats
    lines.append(f"Run: {s.get('queries', 0)} searches, {s.get('hashtags', 0)} hashtags, "
                 f"{s.get('candidates', 0)} candidates, {s.get('lookups', 0)} profiles checked, "
                 f"{s.get('backlog', 0)} from backlog, {s.get('queued', 0)} queued for next run.")
    return subject, "\n".join(lines)


def save_run_file(run: BrandRun, data_dir: Path, *, s3=None, db_s3_uri: str = "") -> Path:
    """Dated copy of the run (data/brand_runs/run-YYYY-MM-DD.md, and next to the database on S3)."""
    subject, body = render(run)
    path = data_dir / "brand_runs" / f"run-{run.run_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {subject}\n\n{body}\n", encoding="utf-8")
    if db_s3_uri:
        from ..storage import parse_s3_uri
        if s3 is None:
            import boto3
            s3 = boto3.client("s3")
        bucket, key = parse_s3_uri(db_s3_uri)
        prefix = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
        s3.upload_file(str(path), bucket, f"{prefix}brand-runs/{path.name}")
    return path


def add_to_pipeline(db: Database, run: BrandRun) -> int:
    """Delivered brands become Instagram leads, so DMs get drafted (and wait for approval)."""
    added = 0
    for leads in run.leads.values():
        for lead in leads:
            if db.is_suppressed([lead.username]):
                continue
            notes = f"brand finder {run.run_date}: {lead.region}" + (f"; {'; '.join(lead.signals)}" if lead.signals else "")
            db.upsert_lead(name=lead.founder or lead.name, company=lead.name, ig_username=lead.username,
                           ig_bio=lead.bio, ig_followers=lead.followers, website=lead.website,
                           domain=domain_from_url(lead.website), source="brand_finder", notes=notes)
            added += 1
    return added
