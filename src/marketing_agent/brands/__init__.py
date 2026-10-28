"""Brand finder: a daily, verified list of emerging womenswear brands (see finder.py)."""

from __future__ import annotations

from typing import Callable

from ..config import Settings
from ..db import Database
from .finder import BrandRun, add_to_pipeline, find_brands, render, save_run_file


def search_client(settings: Settings):
    from ..sources.web_search import build_search
    return build_search(brave_api_key=settings.brave_search_api_key, serper_api_key=settings.serper_api_key)


def run_job(settings: Settings, db: Database, *, ig, deadline: float, gmail=None, search=None, llm=None,
            countries: tuple[str, ...] | None = None, per_country: int | None = None,
            log: Callable[[str], None] = print) -> BrandRun:
    """Find, save the dated run file, email the list, and (optionally) queue the brands for outreach.

    Uses the search setup saved in the app (Marketing > Prospecting) and records the run there.
    """
    from .config import SearchConfig
    saved = db.search_configs(1)
    config = (SearchConfig.from_json(saved[0]["config_json"]) if saved else
              SearchConfig(max_per_country=settings.brand_leads_per_region,
                           leads_per_run=settings.brand_leads_per_region * 2,
                           queries_per_run=settings.brand_search_queries, lookup_limit=settings.brand_lookup_limit))
    search_id = db.start_brand_search(saved[0]["id"] if saved else None)
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
