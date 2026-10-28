"""Prospecting: the search setup, search runs (in the background), and prospects moving towards a sale.

    found -> shortlisted -> contacted  (set in the app; contacted is also set once a DM exists)
    -> the DM conversation joins the Sales funnel: engaged -> qualified -> quoted -> order -> won / lost
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from typing import Callable

from ..brands import geo
from ..brands.config import SearchConfig
from ..brands.finder import find_brands
from ..db import Database
from . import tuner

PROSPECT_STAGES = ["found", "shortlisted", "contacted", "dismissed"]
LOG_LINES = 200


class ProspectingService:
    def __init__(self, db: Database, *, lock: threading.RLock | None = None,
                 ig_factory: Callable[[], object | None] = lambda: None,
                 search_factory: Callable[[], object | None] = lambda: None,
                 llm_getter: Callable[[], object | None] = lambda: None,
                 run_db_factory: Callable[[], Database] | None = None):
        """`run_db_factory` opens the connection a background search writes through (default: this one)."""
        self.db = db
        self.lock = lock or threading.RLock()
        self.ig_factory, self.search_factory, self.llm_getter = ig_factory, search_factory, llm_getter
        self.run_db_factory = run_db_factory
        self._thread: threading.Thread | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self.current_search: int | None = None
        with self.lock:
            db.fail_unfinished_searches()

    # ---- search setup -----------------------------------------------------------------

    def config(self) -> tuple[SearchConfig, int | None]:
        with self.lock:
            rows = self.db.search_configs(1)
        return (SearchConfig.from_json(rows[0]["config_json"]), rows[0]["id"]) if rows else (SearchConfig(), None)

    def save_config(self, config: SearchConfig, *, note: str = "", created_by: str = "user") -> int:
        config = config.clean()
        if not (config.keywords or config.extra_queries):
            raise ValueError("add at least one keyword or extra query")
        if not (config.targets or config.extra_queries):
            raise ValueError("add at least one country or extra query")
        unknown = [t.country for t in config.targets if geo.country_name(t.country).lower() == t.country]
        if unknown:
            raise ValueError(f"unknown country code: {', '.join(unknown)}")
        with self.lock:
            return self.db.save_search_config(config.model_dump_json(), note=note or "edited", created_by=created_by)

    def config_history(self) -> list[dict]:
        with self.lock:
            return [{"id": r["id"], "note": r["note"], "created_by": r["created_by"], "created_at": r["created_at"]}
                    for r in self.db.search_configs(20)]

    def revert(self, config_id: int) -> int:
        with self.lock:
            row = self.db.search_config(config_id)
        if row is None:
            raise KeyError(f"unknown setup version {config_id}")
        return self.save_config(SearchConfig.from_json(row["config_json"]), note=f"restored version {config_id}")

    # ---- search runs --------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {"running": self.running, "search_id": self.current_search, "log": list(self._log)[-60:]}

    def start_search(self) -> int:
        if self.running:
            raise ValueError("a search is already running")
        ig, search = self.ig_factory(), self.search_factory()
        if ig is None:
            raise RuntimeError("Instagram isn't configured (IG_ACCESS_TOKEN / IG_USER_ID)")
        if search is None:
            raise RuntimeError("No search API key: set SERPER_API_KEY or BRAVE_SEARCH_API_KEY in .env")
        config, config_id = self.config()
        with self.lock:
            search_id = self.db.start_brand_search(config_id)
        self.current_search = search_id
        self._log.clear()
        self._thread = threading.Thread(target=self._run, args=(search_id, config, ig, search, self.llm_getter()),
                                        name=f"brand-search-{search_id}", daemon=True)
        self._thread.start()
        return search_id

    def _run(self, search_id: int, config: SearchConfig, ig, search, llm) -> None:
        db = self.run_db_factory() if self.run_db_factory else self.db
        try:
            run = find_brands(db, ig=ig, search=search, llm=llm, leads_per_run=config.leads_per_run,
                              max_per_country=config.max_per_country, search_queries_limit=config.queries_per_run, lookup_limit=config.lookup_limit,
                              deadline=time.monotonic() + config.minutes * 60, log=self._log.append,
                              config=config, search_id=search_id)
            leads = {r: len(v) for r, v in run.leads.items()}
            db.finish_brand_search(search_id, status="done", stats=run.stats, leads=leads, stopped=run.stopped)
            found = ", ".join(f"{n} {c.upper()}" for c, n in leads.items() if n) or "none"
            self._log.append(f"Done: {sum(leads.values())} prospects ({found}).")
        except Exception as exc:   # keep the app alive; the run shows as failed with the reason
            db.finish_brand_search(search_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            self._log.append(f"Failed: {exc}")
        finally:
            if db is not self.db:
                db.conn.close()

    def searches(self) -> list[dict]:
        with self.lock:
            rows = self.db.brand_searches()
        return [{**dict(r), "stats": json.loads(r["stats_json"] or "{}"), "leads": json.loads(r["leads_json"] or "{}")}
                for r in rows]

    def query_stats(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.query_yield()]

    # ---- prospects ----------------------------------------------------------------------

    def prospects(self, *, stage: str | None = None, region: str | None = None, search: str | None = None,
                  search_id: int | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.prospects(stage=stage, region=region, search=search, search_id=search_id)
            out = []
            for r in rows:
                item = dict(r)
                # A DM exists, so they were contacted (or wrote to us) even if nobody clicked it.
                if r["conversation_id"] and r["stage"] in ("found", "shortlisted"):
                    self.db.set_prospect_stage(r["username"], "contacted")
                    item["stage"] = "contacted"
                out.append(item)
        return out

    def set_stage(self, username: str, stage: str) -> None:
        if stage not in PROSPECT_STAGES:
            raise ValueError(f"stage must be one of {', '.join(PROSPECT_STAGES)}")
        with self.lock:
            self.db.set_prospect_stage(username, stage)

    def stage_counts(self) -> dict[str, int]:
        with self.lock:
            return dict(Counter(self.db.prospect_usernames().values()))

    # ---- tuning with the model ------------------------------------------------------------

    def _llm(self):
        llm = self.llm_getter()
        if llm is None:
            raise RuntimeError("No model configured - pick one in Settings")
        return llm

    def _brief(self) -> str:
        config, _ = self.config()
        with self.lock:
            rejections = [{"why": r["why"], "count": r["n"]} for r in self.db.rejection_reasons()]
        return tuner.briefing(config, self.query_stats(), rejections, self.stage_counts())

    def tune_estimate(self, message: str, history: list[dict]) -> dict:
        return tuner.estimate(self._llm(), self._brief(), message, history)

    def tune(self, message: str, history: list[dict], *, confirm: bool) -> dict:
        """Ask the model; `confirm` must be True (the person saw the cost). Returns its proposal, unsaved."""
        if not confirm:
            raise ValueError("confirm the estimated cost first (confirm=true)")
        if not message.strip():
            raise ValueError("say what you'd like to change")
        reply = tuner.tune(self._llm(), self._brief(), message.strip(), history)
        return {"reply": reply.reply, "changes": reply.changes,
                "config": reply.config.model_dump() if reply.config else None}
