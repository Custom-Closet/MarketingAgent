"""Brand finder: a daily, verified list of emerging womenswear brands (see finder.py)."""

from __future__ import annotations

from typing import Callable

from ..config import Settings
from ..db import Database
from .finder import BrandRun, add_to_pipeline, find_brands, month_start, render, save_run_file


def search_client(settings: Settings):
    from ..sources.web_search import build_search
    return build_search(brave_api_key=settings.brave_search_api_key, serper_api_key=settings.serper_api_key)


def current_config(settings: Settings, db: Database):
    """(search setup, its version id): the one saved in the app, else defaults from .env."""
    from .config import SearchConfig
    saved = db.search_configs(1)
    if saved:
        return SearchConfig.from_json(saved[0]["config_json"]), saved[0]["id"]
    return SearchConfig(max_per_country=settings.brand_leads_per_region,
                        leads_per_run=settings.brand_leads_per_region * 2,
                        queries_per_run=settings.brand_search_queries, lookup_limit=settings.brand_lookup_limit), None


def search_usage(db: Database, config) -> dict:
    """Web searches used (what the search API bills for) and their cost at the setup's price."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    month = db.searches_since(month_start(now))
    rate = config.cost_per_1000 / 1000
    budget = config.monthly_search_budget
    return {"month": month, "today": db.searches_since(now.replace(hour=0, minute=0, second=0, microsecond=0)),
            "last_30_days": db.searches_since(now - timedelta(days=30)), "cost_per_1000": config.cost_per_1000,
            "month_cost": round(month * rate, 2), "per_run": config.queries_per_run,
            "per_run_cost": round(config.queries_per_run * rate, 2),
            "budget": budget, "budget_left": max(0, budget - month) if budget else None}


def describe(config, usage: dict) -> list[str]:
    """What a run will look for and what it may cost, for the terminal."""
    c = config.criteria
    rules = (f"followers {c.min_followers:,}-{c.max_followers:,} (ideal under {c.ideal_followers:,}), "
             f"posts {c.min_posts}-{c.max_posts}, posted in the last {c.active_within_days} days, "
             f"{' / '.join(c.segments)}{', ethnicwear-only accepted' if c.allow_ethnic_only else ''}")
    where = ", ".join(f"{t.country.upper()} ({len(t.places) or 'country name'})" for t in config.targets)
    left = f", {usage['budget_left']:,} of {usage['budget']:,} left" if usage["budget"] else ""
    return [f"Who qualifies: {rules}",
            f"Where: {where}",
            f"Cost: up to {config.queries_per_run} searches this run (~${usage['per_run_cost']:.2f}); "
            f"this month {usage['month']:,} searches (~${usage['month_cost']:.2f}){left}"]


def run_job(settings: Settings, db: Database, *, ig, deadline: float, gmail=None, search=None, llm=None,
            countries: tuple[str, ...] | None = None, per_country: int | None = None,
            log: Callable[[str], None] = print) -> BrandRun:
    """Find, save the dated run file, email the list, and (optionally) queue the brands for outreach.

    Uses the search setup saved in the app (Marketing > Prospecting) and records the run there.
    """
    config, config_id = current_config(settings, db)
    search_id = db.start_brand_search(config_id)
    try:
        run = find_brands(db, ig=ig, search=search, llm=llm, countries=countries,
                          max_per_country=per_country or config.max_per_country,
                          leads_per_run=None if per_country else config.leads_per_run,
                          search_queries_limit=config.queries_per_run, lookup_limit=config.lookup_limit,
                          hashtag_budget=settings.brand_hashtag_budget, deadline=deadline, log=log,
                          config=config, search_id=search_id)
    except Exception as exc:
        db.finish_brand_search(search_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    db.finish_brand_search(search_id, status="done", stats=run.stats,
                           leads={r: len(v) for r, v in run.leads.items()}, stopped=run.stopped)
    # The file is written before the email, so a failed delivery never loses the list.
    path = save_run_file(run, settings.data_dir, db_s3_uri=settings.db_s3_uri)
    log(f"Saved {path}")
    if settings.brand_leads_to_pipeline:
        run.stats["queued_for_outreach"] = add_to_pipeline(db, run)
    if gmail and settings.digest_email:
        from ..channels.gmail import build_notification
        subject, body = render(run)
        gmail.send(build_notification(sender_email=settings.sender_email, to=settings.digest_email,
                                      subject=subject, body=body))
        log(f"Emailed the list to {settings.digest_email}")
    return run
