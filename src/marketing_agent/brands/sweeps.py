"""Where candidate handles come from. Every sweep only proposes handles; finder.py verifies them.

* search    - web search for Instagram profiles: keyword x city queries (site:instagram.com ...).
              Snippets carry follower / post counts, so large accounts are dropped for free.
* site      - web search for brand websites, whose header/footer links to their Instagram.
* snowball  - @mentions in the bio and captions of brands we already know (collabs, sister labels).
* hashtag   - @mentions in captions of hashtag posts (Graph API; 30 new hashtags / 7 days).
* seed      - handles imported by hand (`agent brands seed`), e.g. lists from other tools.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from typing import Callable

import httpx

from ..db import Database
from ..sources.instagram import InstagramError, InstagramRateLimited, extract_mentions
from ..sources.web_search import (SearchError, WebSearch, handle_from_instagram_url, handles_in_text,
                                  instagram_handles_in_html, snippet_stats)
from .config import Criteria, SearchConfig
from .geo import country_name
from .criteria import FOLLOWER_CAP

Log = Callable[[str], None]

HASHTAG_POOL = [
    "emergingdesigner", "smallfashionbrand", "slowfashionindia", "handmadewomenswear", "madeinindia",
    "shopsmallusa", "womenownedbrand", "smallbatchclothing", "slowfashionbrand", "independentlabel",
    "newclothingbrand", "indianfashionlabel", "sustainablefashionindia", "blockprintdresses",
    "handblockprint", "coordsets", "linenclothing", "madetoorderclothing", "nycdesigner", "ladesigner",
    "brooklyndesigner", "southasianowned", "desidesigner", "mumbaidesigner", "delhidesigner",
    "bangaloredesigner", "jaipurblockprint", "indianslowfashion", "smallbusinessfashion",
    "supportsmallbrands",
]
HASHTAG_WINDOW = timedelta(days=7)
HASHTAGS_PER_RUN = 12
SITE_FETCH_LIMIT = 40        # brand homepages fetched per run
SNOWBALL_MEDIA = 12          # captions read per snowball seed
STALE_HINT_MARGIN = 1.5
MAX_REJECTED_SEARCHES = 3    # consecutive 4xx answers before the search sweep gives up      # snippet counts can be months old; only skip clearly-too-big accounts


@dataclass
class Candidate:
    username: str
    sources: set[str] = field(default_factory=set)
    followers_hint: int | None = None
    region_hint: str | None = None
    query: str | None = None       # the web search that first found it

    def priority(self, max_followers: int = FOLLOWER_CAP) -> int:
        """Lower is looked up first: snippet says small > known neighbours > plain search > hashtag mentions."""
        if self.followers_hint is not None and self.followers_hint < max_followers:
            return 0
        if self.sources & {"snowball", "site", "seed"}:
            return 1
        return 2 if "search" in self.sources else 3


class CandidatePool:
    def __init__(self, skip: set[str], criteria: Criteria | None = None):
        self.skip = set(skip)
        self.items: dict[str, Candidate] = {}
        self.dropped = 0
        self.criteria = criteria or Criteria()

    def add(self, username: str, source: str, *, followers: int | None = None, posts: int | None = None,
            region: str | None = None, query: str | None = None) -> bool:
        """Returns True when the handle is new to this pool."""
        username = username.lower().lstrip("@").rstrip(".")
        if not username or username in self.skip:
            return False
        c = self.criteria
        # Search snippets can be months old, so only clearly-too-big accounts are dropped here.
        if (followers is not None and followers >= c.max_followers * STALE_HINT_MARGIN) or (
                posts is not None and posts > c.max_posts * STALE_HINT_MARGIN):
            self.dropped += 1
            return False
        is_new = username not in self.items
        cand = self.items.setdefault(username, Candidate(username))
        cand.sources.add(source)
        if followers is not None:
            cand.followers_hint = followers
        cand.region_hint = cand.region_hint or region
        cand.query = cand.query or query
        return is_new

    def take(self, full_regions: set[str]) -> Candidate | None:
        """Next candidate to verify; skips those hinted at a region that already has enough leads."""
        options = [c for c in self.items.values() if c.region_hint not in full_regions or c.region_hint is None]
        if not options:
            return None
        cap = self.criteria.max_followers
        best = min(options, key=lambda c: (c.priority(cap), c.followers_hint or cap))
        del self.items[best.username]
        self.skip.add(best.username)
        return best

    def __len__(self) -> int:
        return len(self.items)


# ---- web search --------------------------------------------------------------

def search_queries(countries: set[str], config: SearchConfig | None = None) -> list[tuple[str, str, str]]:
    """(query, kind, country) for every sweep, interleaved across countries so any slice covers them all."""
    config = config or SearchConfig()
    keywords = config.keywords or ["womenswear label"]
    exclude = "".join(f" -{t}" if " " not in t else f' -"{t}"' for t in config.exclude_terms)
    per_country: dict[str, list[tuple[str, str, str]]] = {}
    for target in config.targets:
        if target.country not in countries:
            continue
        places = target.places or [country_name(target.country)]
        qs: list[tuple[str, str, str]] = []
        for i, place in enumerate(places):
            for kw in keywords:
                qs.append((f'site:instagram.com "{kw}" "{place}"{exclude}', "search", target.country))
            if target.country != "in":      # Indian founders / craft are the preferred fit outside India
                qs += [(f'site:instagram.com "{term}" womenswear "{place}"{exclude}', "search", target.country)
                       for term in config.craft_terms]
            if config.site_queries:
                kw = keywords[i % len(keywords)]
                qs.append((f'"{kw}" "{place}" shop -site:instagram.com -site:pinterest.com{exclude}', "site",
                           target.country))
        # Spread places through the list so a short slice isn't all one place.
        per_country[target.country] = qs[::3] + qs[1::3] + qs[2::3]
    mixed = [q for group in zip_longest(*per_country.values()) for q in group if q]
    # Hand-written queries run first, every time.
    return [(q, "site" if "-site:instagram.com" in q else "search", "") for q in config.extra_queries] + mixed


def rotate(items: list, start: int, count: int) -> list:
    if not items:
        return []
    return [items[(start + i) % len(items)] for i in range(min(count, len(items)))]


def search_sweep(search: WebSearch, queries: list[tuple[str, str, str]], pool: CandidatePool, *,
                 deadline: float, log: Log, http: httpx.Client | None = None,
                 on_query: Callable[[str, str, str, int, int], None] | None = None,
                 results_per_query: int = 10) -> dict[str, int]:
    """`on_query(query, kind, region, results, new_candidates)` is called after every search."""
    stats = {"queries": 0, "search_errors": 0, "sites": 0}
    site_urls: dict[str, tuple[str | None, str]] = {}
    rejected_in_a_row = 0
    for query, kind, region in queries:
        if time.monotonic() > deadline:
            log("  search: out of time")
            break
        try:
            results = search.search(query, count=results_per_query)
        except SearchError as exc:
            stats["search_errors"] += 1
            log(f"  search failed: {exc}")
            # The same 4xx for every query means a bad key or plan limit: stop instead of burning the run.
            rejected_in_a_row += 1 if exc.status and 400 <= exc.status < 500 and exc.status != 429 else 0
            if rejected_in_a_row >= MAX_REJECTED_SEARCHES:
                log("  search: stopping, the search API keeps rejecting requests")
                break
            continue
        rejected_in_a_row = 0
        stats["queries"] += 1
        before = len(pool)
        region = region or None
        for r in results:
            handle = handle_from_instagram_url(r.url)
            if kind == "search":
                is_profile = handle and r.url.rstrip("/").lower().endswith(handle)
                followers, posts = snippet_stats(r.snippet) if is_profile else (None, None)
                # Post and reel results carry no username in the link; the brand is usually @mentioned in the text.
                found = ({handle} if handle else set()) | handles_in_text(r.title)
                if not is_profile:
                    found |= extract_mentions(f"{r.title} {r.snippet}")
                for h in found:
                    pool.add(h, "search", followers=followers if h == handle else None,
                             posts=posts if h == handle else None, region=region, query=query)
            elif not handle and r.url.startswith("http"):
                site_urls.setdefault(r.url, (region, query))
        if on_query:
            on_query(query, kind, region or "", len(results), len(pool) - before)
    if site_urls:
        found = fetch_site_handles(list(site_urls)[:SITE_FETCH_LIMIT], http=http)
        for url, handles in found.items():
            stats["sites"] += 1
            region, query = site_urls[url]
            for h in handles:
                pool.add(h, "site", region=region, query=query)
    return stats


def _fetch_handles(http: httpx.Client, url: str) -> set[str]:
    try:
        resp = http.get(url)
    except httpx.HTTPError:
        return set()
    if resp.is_error or "html" not in resp.headers.get("content-type", ""):
        return set()
    handles = instagram_handles_in_html(resp.text[:500_000])
    # A site linking many accounts is a directory or a magazine, not one brand.
    return handles if len(handles) <= 2 else set()


def fetch_site_handles(urls: list[str], *, http: httpx.Client | None = None) -> dict[str, set[str]]:
    http = http or httpx.Client(timeout=8, follow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; MarketingAgent/0.1)"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda u: (u, _fetch_handles(http, u)), urls))
    return {u: h for u, h in results if h}


# ---- Instagram -----------------------------------------------------------------

def hashtags_for_run(db: Database, budget: int, cursor: int, *, now: datetime | None = None) -> list[str]:
    """Up to HASHTAGS_PER_RUN tags: new ones (rotating through the pool) while the weekly budget allows,
    topped up with tags whose 7-day window is already open (free to query again)."""
    now = now or datetime.now(timezone.utc)
    open_tags = db.hashtags_used_since(now - HASHTAG_WINDOW)
    slots = max(0, budget - len(open_tags))
    fresh = [t for t in rotate(HASHTAG_POOL, cursor, len(HASHTAG_POOL)) if t not in open_tags][:slots]
    reuse = [t for t in HASHTAG_POOL if t in open_tags]
    return (fresh + reuse)[:HASHTAGS_PER_RUN]


def hashtag_sweep(ig, db: Database, tags: list[str], pool: CandidatePool, *, deadline: float,
                  log: Log) -> int:
    done = 0
    for tag in tags:
        if time.monotonic() > deadline:
            break
        db.record_hashtag(tag, datetime.now(timezone.utc) - HASHTAG_WINDOW)
        try:
            captions = ig.hashtag_captions(tag)
        except InstagramRateLimited:
            raise
        except InstagramError as exc:
            log(f"  #{tag}: {exc}")
            continue
        before = len(pool)
        for caption in captions:
            for handle in extract_mentions(caption):
                pool.add(handle, "hashtag")
        log(f"  #{tag}: {len(captions)} posts, {len(pool) - before} new handles")
        done += 1
    return done


def snowball_sweep(ig, db: Database, pool: CandidatePool, *, limit: int, deadline: float, log: Log) -> int:
    """Mine mentions from leads and known brands not mined in the last 30 days. Returns lookups used."""
    used = 0
    for seed in db.snowball_seeds(limit, datetime.now(timezone.utc) - timedelta(days=30)):
        if time.monotonic() > deadline:
            break
        used += 1
        try:
            profile = ig.business_discovery(seed["username"], media_limit=SNOWBALL_MEDIA)
        except InstagramRateLimited:
            raise
        except InstagramError:
            db.mark_snowballed(seed["username"])
            continue
        text = " ".join([profile.biography or ""] + [m.get("caption") or "" for m in profile.recent_media])
        for handle in extract_mentions(text) - {seed["username"]}:
            pool.add(handle, "snowball", region=seed["region"])
        db.mark_snowballed(seed["username"])
    return used
