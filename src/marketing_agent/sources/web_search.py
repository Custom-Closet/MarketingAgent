"""Web search APIs used to find Instagram brand profiles (keyword and city sweeps).

Search engines index public Instagram profile pages, so a query like
`site:instagram.com "womenswear label" Brooklyn` returns profile URLs whose snippet usually reads
"2,345 Followers, 512 Following, 128 Posts - See Instagram photos and videos from Brand (@handle)".
That gives a handle plus a follower estimate before any Graph API call is spent on it.

Two interchangeable providers (set one key):

* Brave Search API  - BRAVE_SEARCH_API_KEY   (https://api-dashboard.search.brave.com)
* Serper (Google)   - SERPER_API_KEY         (https://serper.dev)

Only the search APIs are called; Instagram itself is never scraped. Profiles are verified
through the official Business Discovery endpoint afterwards.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

# First path segments of instagram.com URLs that are not usernames.
_RESERVED = {"p", "reel", "reels", "tv", "explore", "stories", "accounts", "about", "developer", "legal",
             "directory", "web", "tags", "locations", "challenge", "privacy", "terms", "direct", "emails",
             "session", "oauth", "share", "s", "invites", "help", "press", "api", "static", "ar"}
_IG_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{1,30})(?=[/?#\"'\s]|$)", re.IGNORECASE)
_HANDLE_IN_TEXT_RE = re.compile(r"\(@([A-Za-z0-9_.]{1,30})\)")
_COUNT = r"(\d[\d,.]*\s*[KkMm]?)"
_FOLLOWERS_RE = re.compile(_COUNT + r"\s+Followers", re.IGNORECASE)
_POSTS_RE = re.compile(_COUNT + r"\s+Posts", re.IGNORECASE)


class SearchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


class WebSearch(Protocol):
    name: str

    def search(self, query: str, *, count: int = 20) -> list[SearchResult]: ...


def _get_json(http: httpx.Client, method: str, url: str, retries: int = 2, **kwargs) -> dict:
    for attempt in range(retries + 1):
        resp = http.request(method, url, **kwargs)
        if resp.status_code == 429 and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue
        if resp.is_error:
            raise SearchError(f"{url}: HTTP {resp.status_code} {resp.text[:200]}", resp.status_code)
        return resp.json()
    raise AssertionError("unreachable")


class BraveSearch:
    name = "brave"
    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=30, headers={"X-Subscription-Token": api_key,
                                                                "Accept": "application/json"})

    def search(self, query: str, *, count: int = 20) -> list[SearchResult]:
        body = _get_json(self.http, "GET", self.URL, params={"q": query, "count": count})
        return [SearchResult(r.get("url", ""), r.get("title", ""), r.get("description", ""))
                for r in body.get("web", {}).get("results", [])]


class SerperSearch:
    name = "serper"
    URL = "https://google.serper.dev/search"

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=30, headers={"X-API-KEY": api_key})

    MAX_FREE_RESULTS = 10   # free accounts reject num > 10 ("Query pattern not allowed for free accounts")

    def search(self, query: str, *, count: int = 20) -> list[SearchResult]:
        body = _get_json(self.http, "POST", self.URL, json={"q": query, "num": min(count, self.MAX_FREE_RESULTS)})
        return [SearchResult(r.get("link", ""), r.get("title", ""), r.get("snippet", ""))
                for r in body.get("organic", [])]


# ---- parsing ---------------------------------------------------------------

def handle_from_instagram_url(url: str) -> str | None:
    """`https://www.instagram.com/brand.name/` (or /brand.name/p/xyz/) -> 'brand.name'."""
    match = _IG_URL_RE.search(url)
    if not match:
        return None
    handle = match.group(1).lower().rstrip(".")
    return None if handle in _RESERVED else handle


def handles_in_text(text: str) -> set[str]:
    """Handles written as 'Brand (@handle)' in Instagram page titles and snippets."""
    return {h.lower().rstrip(".") for h in _HANDLE_IN_TEXT_RE.findall(text or "")}


def instagram_handles_in_html(html: str) -> set[str]:
    """Instagram profile links on a brand's website (footer / header social icons)."""
    found = {handle_from_instagram_url(m.group(0)) for m in _IG_URL_RE.finditer(html or "")}
    return {h for h in found if h}


def parse_count(text: str) -> int:
    """'2,345' -> 2345, '1.2K' -> 1200, '3M' -> 3000000."""
    text = text.replace(",", "").replace(" ", "")
    scale = {"k": 1_000, "m": 1_000_000}.get(text[-1].lower(), 1)
    return int(float(text.rstrip("KkMm")) * scale)


def snippet_stats(text: str) -> tuple[int | None, int | None]:
    """(followers, posts) quoted in an Instagram search snippet, when present."""
    followers, posts = _FOLLOWERS_RE.search(text or ""), _POSTS_RE.search(text or "")
    try:
        return (parse_count(followers.group(1)) if followers else None,
                parse_count(posts.group(1)) if posts else None)
    except ValueError:
        return None, None


def build_search(*, brave_api_key: str = "", serper_api_key: str = "") -> WebSearch | None:
    if brave_api_key:
        return BraveSearch(brave_api_key)
    if serper_api_key:
        return SerperSearch(serper_api_key)
    return None
