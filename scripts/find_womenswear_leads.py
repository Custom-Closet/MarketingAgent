#!/usr/bin/env python3
"""Find emerging women's clothing brand leads (USA + India) via the Instagram Graph API.

Pipeline:
  1. Hashtag search -> recent/top media captions -> @mentioned handles become candidates.
     (The Graph API does not say who posted hashtag media, so @mentions are the only
     handle signal it offers. Extra handles can be supplied with --handles.)
  2. Business Discovery -> profile fields + the latest media, in a single call.
  3. Filters: follower cap, media count, activity window, apparel/exclusion keywords, region.
  4. Dedupe against seen_brands.csv, write usa_leads.csv / india_leads.csv.

Run:  uv run --with requests scripts/find_womenswear_leads.py --region both --max-results 25
Needs: IG_ACCESS_TOKEN and IG_USER_ID in the environment (see SETUP_NOTE below).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# =============================================================================
# CONFIG
# =============================================================================
ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")  # your own IG professional account id; Business Discovery runs "as" it
API_VERSION = "v23.0"
GRAPH_URL = f"https://graph.facebook.com/{API_VERSION}"

# Meta allows 30 unique hashtags per IG account per rolling 7 days - keep this list short.
HASHTAGS = [
    "emergingdesigner", "smallfashionbrand", "slowfashionindia", "desiboutique",
    "handmadewomenswear", "lowmoq", "madeinindia", "shopsmallusa",
]
HASHTAG_MEDIA_LIMIT = 10   # posts per page; Meta rejects larger pages with code 1 "reduce the amount of data"
HASHTAG_PAGES = 5          # pages read per hashtag edge (recent_media and top_media)

FOLLOWER_CAP = 10_000      # hard cap (exclusive)
IDEAL_CAP = 5_000          # tagged "ideal" below this
MIN_MEDIA_COUNT = 3
ACTIVITY_WINDOW_DAYS = 60
RECENT_MEDIA_LIMIT = 5

# Region inference: matched as whole words/phrases (case-insensitive) against name + bio + website.
USA_KEYWORDS = [
    "usa", "u.s.a", "united states", "america", "🇺🇸", "us based", "us-based",
    "nyc", "new york", "brooklyn", "los angeles", "san francisco", "bay area", "chicago", "houston",
    "dallas", "austin", "seattle", "boston", "atlanta", "miami", "new jersey", "nj", "philadelphia",
    "washington dc", "dc", "portland", "denver", "san diego", "california", "texas", "florida",
    "georgia", "illinois", "massachusetts", "virginia", "maryland", "north carolina", "ohio",
    "michigan", "colorado", "arizona", "oregon", "pennsylvania", "minnesota",
]
INDIA_KEYWORDS = [
    "india", "🇮🇳", "bharat", "mumbai", "bombay", "delhi", "new delhi", "ncr", "gurgaon", "gurugram",
    "noida", "bengaluru", "bangalore", "chennai", "hyderabad", "kolkata", "pune", "jaipur",
    "ahmedabad", "surat", "lucknow", "chandigarh", "kochi", "goa", "indore", "tiruppur",
]
INDIA_WEBSITE_TLDS = (".in", ".co.in")
# Phrases like "ships to USA" or "worldwide shipping" say nothing about where a brand is based.
SHIPPING_PHRASE_RE = re.compile(r"\b(?:ships?|shipping|delivery|delivering)\s+(?:to|in|across)?\s*[\w\s,&/]{0,40}",
                                re.IGNORECASE)

# A candidate must look like a clothing brand at all (mentions also surface photographers, models...).
APPAREL_KEYWORDS = [
    "clothing", "apparel", "womenswear", "women's wear", "womens wear", "fashion", "label", "dresses",
    "co-ords", "coords", "tops", "kurtas", "wear", "garments", "collection", "designer", "studio",
    "atelier", "couture", "ready to wear", "rtw", "prêt", "pret",
]
# Always excluded: resellers / curated / non-production businesses.
EXCLUDE_KEYWORDS = [
    "reseller", "resellers", "curated boutique", "multi-designer", "multi designer", "multidesigner",
    "dropship", "dropshipping", "preloved", "thrift", "consignment", "wholesale only", "rental",
]
# Excluded only when the bio shows no womenswear signal (i.e. bridal-only / kids-only / menswear-only).
EXCLUDE_UNLESS_WOMENSWEAR = [
    "bridal", "bridalwear", "lehenga", "wedding wear", "kidswear", "kids wear", "kids", "baby",
    "menswear", "men's wear", "for men",
]
WOMENSWEAR_KEYWORDS = [
    "women", "womens", "women's", "womenswear", "her", "she", "dresses", "co-ords", "western",
    "contemporary", "everyday wear", "workwear", "resort", "tops",
]

# Fit signals: brands likely to need sampling / low-MOQ production / fabric sourcing.
FIT_KEYWORDS = [
    "small batch", "small-batch", "handmade", "hand made", "made to order", "made-to-order",
    "slow fashion", "limited edition", "limited drops", "sustainable", "ethically made",
    "independent", "indie", "new launch", "launching", "just launched", "est. 2024", "est. 2025",
    "est 2025", "est. 2026", "est 2026", "since 2025", "since 2026",
]
# US preference: Indian / South Asian founders or Indian textiles & craft.
INDIAN_CONNECTION_KEYWORDS = [
    "indian", "south asian", "desi", "made in india", "handcrafted in india", "indian-owned",
    "indian owned", "block print", "block-printed", "hand block", "ajrakh", "chikankari", "khadi",
    "handloom", "ikat", "kantha", "bandhani", "zari", "kalamkari", "jamdani", "banarasi",
    "mulmul", "dabu", "artisans in india", "jaipur", "kutch",
]

BATCH_SIZE = 20                    # profile lookups per batch
BATCH_SLEEP = (1.0, 2.0)           # random sleep range between batches (seconds)
CALL_SLEEP = 0.25                  # small gap between individual calls
MAX_RETRIES = 5
BACKOFF_BASE = 2.0                 # seconds; doubles each retry, plus jitter
BACKOFF_MAX = 60.0

OUTPUT_DIR = Path("data/leads")
SEEN_BRANDS_PATH = OUTPUT_DIR / "seen_brands.csv"
OUTPUT_PATHS = {"usa": OUTPUT_DIR / "usa_leads.csv", "india": OUTPUT_DIR / "india_leads.csv"}
LEAD_COLUMNS = ["brand_name", "instagram_url", "followers", "founder", "website", "bio_snippet",
                "last_post_date", "tier", "signals"]

SETUP_NOTE = """\
SETUP
  1. Convert the Instagram account you will query from into a Business or Creator account and
     link it to a Facebook Page.
  2. Meta for Developers -> create an app (Business type) -> add "Instagram API with Facebook Login".
     Hashtag search and Business Discovery are NOT available with Instagram Login ("IG..." tokens).
  3. Generate a user token (Graph API Explorer works) with: instagram_basic, pages_show_list,
     pages_read_engagement. Hashtag search also needs the "Instagram Public Content Access" feature
     (dev mode works for app admins/testers; production needs App Review).
     Note: instagram_graph_user_profile / instagram_graph_user_media were Basic Display API scopes;
     that API was shut down in Dec 2024 and cannot call these endpoints.
  4. export IG_ACCESS_TOKEN=EAA...   and   export IG_USER_ID=<your IG professional account id>
     (GET /me/accounts?fields=instagram_business_account returns the id.)
  Business Discovery only works for business/creator accounts - personal accounts are skipped and logged.
  Hashtag search is limited to 30 unique hashtags per 7 days per account.
"""

log = logging.getLogger("leads")
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}
TRANSIENT_CODES = {-2, 1, 2}
NOT_FOUND_CODES = {100, 110}  # Business Discovery: personal account, renamed or nonexistent handle
MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]{1,30})")


# =============================================================================
# Graph API client
# =============================================================================
class GraphError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class RateLimitExhausted(GraphError):
    pass


class Graph:
    def __init__(self, token: str, ig_user_id: str):
        self.token = token
        self.ig_user_id = ig_user_id
        self.session = requests.Session()

    def get(self, path: str, params: dict) -> dict:
        """GET with exponential backoff + jitter on 429 / Graph rate-limit codes / 5xx."""
        params = {**params, "access_token": self.token}
        for attempt in range(MAX_RETRIES + 1):
            time.sleep(CALL_SLEEP)
            try:
                resp = self.session.get(f"{GRAPH_URL}{path}", params=params, timeout=30)
            except requests.RequestException as exc:
                # Never log the exception text verbatim: it can contain the URL (and thus the token).
                retryable, err = True, GraphError(f"network error ({type(exc).__name__})")
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                if resp.ok and "error" not in body:
                    return body
                e = body.get("error", {})
                code = e.get("code")
                err = GraphError(f"{path}: {e.get('message', f'HTTP {resp.status_code}')} (code {code})",
                                 code, e.get("error_subcode"))
                # Code 1 also means "reduce the amount of data" - retrying the same request never helps.
                too_big = "reduce the amount of data" in e.get("message", "")
                retryable = not too_big and (resp.status_code == 429 or resp.status_code >= 500
                                             or code in RATE_LIMIT_CODES or code in TRANSIENT_CODES)
            if not retryable:
                raise err
            if attempt == MAX_RETRIES:
                raise RateLimitExhausted(f"gave up after {MAX_RETRIES} retries: {err}", err.code)
            delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** attempt)
            delay += random.uniform(0, delay / 2)
            log.warning("Retryable error (%s) - backing off %.1fs [attempt %d/%d]",
                        err, delay, attempt + 1, MAX_RETRIES)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def hashtag_candidates(self, tag: str) -> set[str]:
        tag = tag.lstrip("#")
        found = self.get("/ig_hashtag_search", {"user_id": self.ig_user_id, "q": tag}).get("data", [])
        if not found:
            log.info("Hashtag #%s: not found", tag)
            return set()
        handles: set[str] = set()
        for edge in ("recent_media", "top_media"):
            params = {"user_id": self.ig_user_id, "fields": "caption", "limit": HASHTAG_MEDIA_LIMIT}
            for _ in range(HASHTAG_PAGES):
                page = self.get(f"/{found[0]['id']}/{edge}", params)
                for item in page.get("data", []):
                    caption = normalize(item.get("caption") or "")
                    handles |= {m.lower().rstrip(".") for m in MENTION_RE.findall(caption)}
                after = page.get("paging", {}).get("cursors", {}).get("after")
                if not after or "next" not in page.get("paging", {}):
                    break
                params = {**params, "after": after}
        log.info("Hashtag #%s: %d candidate handles", tag, len(handles))
        return handles

    def business_discovery(self, username: str) -> dict:
        fields = (f"business_discovery.username({username})"
                  "{username,name,biography,followers_count,follows_count,media_count,website,"
                  f"media.limit({RECENT_MEDIA_LIMIT}){{timestamp,caption}}}}")
        return self.get(f"/{self.ig_user_id}", {"fields": fields})["business_discovery"]


# =============================================================================
# Classification helpers
# =============================================================================
def _phrase_re(phrases: list[str]) -> re.Pattern:
    # Whole-word match; emoji / punctuation-bounded phrases fall back to plain substring matching.
    parts = [rf"(?<!\w){re.escape(p)}(?!\w)" for p in phrases]
    return re.compile("|".join(parts), re.IGNORECASE)


USA_RE, INDIA_RE = _phrase_re(USA_KEYWORDS), _phrase_re(INDIA_KEYWORDS)
APPAREL_RE, EXCLUDE_RE = _phrase_re(APPAREL_KEYWORDS), _phrase_re(EXCLUDE_KEYWORDS)
EXCLUDE_SOFT_RE, WOMENSWEAR_RE = _phrase_re(EXCLUDE_UNLESS_WOMENSWEAR), _phrase_re(WOMENSWEAR_KEYWORDS)
FIT_RE, INDIAN_CONN_RE = _phrase_re(FIT_KEYWORDS), _phrase_re(INDIAN_CONNECTION_KEYWORDS)

FOUNDER_PATTERNS = [
    re.compile(r"(?i:co-?founder|founder|founded by|ceo|creative director|designer)\s*[:\-–|,]?\s*"
               r"(@[A-Za-z0-9_.]+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"),
    re.compile(r"\bby\s+(@[A-Za-z0-9_.]+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"),  # "by Priya Shah" (capitalised)
    re.compile(r"(@[A-Za-z0-9_.]+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*[,(|\-–]\s*(?i:co-?founder|founder|ceo)"),
]
_NOT_NAMES = {"the", "our", "and", "for", "you", "women", "her", "hand", "love", "design", "india", "usa"}


def normalize(text: str) -> str:
    """Fold stylised Unicode letters (𝘉𝘺 𝘚𝘩𝘢𝘺𝘢, 𝐁𝐲 ...) to plain text so keyword/founder regexes match."""
    return unicodedata.normalize("NFKC", text)


PERSON_NAME_RE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}$")
SELF_DESIGNER_RE = re.compile(r"(?i)\b(?:i design|my designs|designer|founder|designed by me|by me)\b")


def extract_founder(bio: str, name: str = "") -> str:
    for pattern in FOUNDER_PATTERNS:
        for m in pattern.finditer(bio):
            candidate = m.group(1).strip()
            if candidate.split()[0].lower() not in _NOT_NAMES:
                return candidate
    # Designer-run accounts ("Shaya Nadaf": "...& designer 'By Shaya'") - the account name is the founder.
    if PERSON_NAME_RE.match(name.strip()) and SELF_DESIGNER_RE.search(bio):
        return name.strip()
    return ""


def infer_region(name: str, bio: str, website: str) -> str | None:
    """'usa', 'india', or None when unverifiable / ambiguous."""
    text = SHIPPING_PHRASE_RE.sub(" ", f"{name} {bio}")
    host = re.sub(r"^https?://", "", website.lower()).split("/")[0]
    usa = len(USA_RE.findall(text)) + (1 if host.endswith(".us") else 0)
    india = len(INDIA_RE.findall(text)) + (2 if host.endswith(INDIA_WEBSITE_TLDS) else 0)
    if usa == india:
        return None
    return "usa" if usa > india else "india"


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")


@dataclass
class Lead:
    region: str
    username: str
    name: str
    followers: int
    founder: str
    website: str
    bio: str
    last_post: datetime
    signals: list[str] = field(default_factory=list)

    @property
    def tier(self) -> str:
        return "ideal" if self.followers < IDEAL_CAP else "ok"

    @property
    def score(self) -> int:
        # Sort key: fit signals and (US) Indian connection first, then smaller accounts.
        return len(self.signals) * 10 + (5 if self.tier == "ideal" else 0)

    def row(self) -> dict:
        snippet = " ".join(self.bio.split())
        return {
            "brand_name": self.name or self.username,
            "instagram_url": f"https://www.instagram.com/{self.username}/",
            "followers": self.followers,
            "founder": self.founder,
            "website": self.website,
            "bio_snippet": snippet[:150] + ("…" if len(snippet) > 150 else ""),
            "last_post_date": self.last_post.date().isoformat(),
            "tier": self.tier,
            "signals": "; ".join(self.signals),
        }


def evaluate(profile: dict, wanted: set[str]) -> tuple[Lead | None, str]:
    """Apply all filters. Returns (lead, "") or (None, skip_reason)."""
    username = profile.get("username", "")
    name = normalize(profile.get("name") or "")
    bio = normalize(profile.get("biography") or "")
    website = profile.get("website") or ""
    followers = profile.get("followers_count")
    media_count = profile.get("media_count") or 0
    text = f"{name} {bio}"

    if followers is None:
        return None, "followers_count hidden"
    if followers >= FOLLOWER_CAP:
        return None, f"{followers} followers >= cap {FOLLOWER_CAP}"
    if media_count < MIN_MEDIA_COUNT:
        return None, f"only {media_count} posts"
    if m := EXCLUDE_RE.search(text):
        return None, f"excluded keyword '{m.group(0)}'"
    if (m := EXCLUDE_SOFT_RE.search(text)) and not WOMENSWEAR_RE.search(text):
        return None, f"'{m.group(0)}' without womenswear signal"
    if not APPAREL_RE.search(text):
        return None, "bio does not look like a clothing brand"

    media = (profile.get("media") or {}).get("data", [])
    if not media:
        return None, "no media returned"
    last_post = max(parse_ts(m["timestamp"]) for m in media if m.get("timestamp"))
    if datetime.now(timezone.utc) - last_post > timedelta(days=ACTIVITY_WINDOW_DAYS):
        return None, f"inactive (last post {last_post.date()})"

    region = infer_region(name, bio, website)
    if region is None:
        return None, "region unverifiable or ambiguous"
    if region not in wanted:
        return None, f"region {region} not requested"

    signals = sorted({m.lower() for m in FIT_RE.findall(text)})
    if region == "usa":
        captions = normalize(" ".join(m.get("caption") or "" for m in media))
        signals += sorted({f"indian:{m.lower()}" for m in INDIAN_CONN_RE.findall(f"{text} {captions}")})
    return Lead(region, username, name, followers, extract_founder(bio, name), website, bio, last_post, signals), ""


# =============================================================================
# Persistence
# =============================================================================
def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {row["username"].lower() for row in csv.DictReader(f) if row.get("username")}


def append_seen(path: Path, usernames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["username", "date_found"])
        today = date.today().isoformat()
        w.writerows([u, today] for u in usernames)


def write_leads(path: Path, leads: list[Lead]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEAD_COLUMNS)
        w.writeheader()
        w.writerows(lead.row() for lead in sorted(leads, key=lambda l: -l.score))


# =============================================================================
# Main
# =============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0], epilog=SETUP_NOTE,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", choices=["usa", "india", "both"], default="both")
    p.add_argument("--max-results", type=int, default=25, help="max new leads per region (default 25)")
    p.add_argument("--hashtags", help="comma-separated override for HASHTAGS")
    p.add_argument("--handles", help="extra candidate handles: comma-separated, or a path to a file with one per line")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def collect_candidates(graph: Graph, hashtags: list[str], extra: list[str]) -> list[str]:
    seen_order: dict[str, None] = dict.fromkeys(h.lstrip("@").lower() for h in extra if h.strip())
    for tag in hashtags:
        try:
            seen_order.update(dict.fromkeys(sorted(graph.hashtag_candidates(tag))))
        except RateLimitExhausted:
            raise
        except GraphError as exc:
            log.warning("Hashtag #%s skipped: %s", tag, exc)
    return list(seen_order)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    print(SETUP_NOTE)
    if not (ACCESS_TOKEN and IG_USER_ID):
        log.error("IG_ACCESS_TOKEN and IG_USER_ID must both be set - see SETUP above.")
        return 1
    if ACCESS_TOKEN.startswith("IG"):
        log.error("This is an Instagram Login token; hashtag search and Business Discovery need a "
                  "Facebook Login token (EAA...).")
        return 1

    wanted = {"usa", "india"} if args.region == "both" else {args.region}
    hashtags = [t.strip() for t in args.hashtags.split(",")] if args.hashtags else HASHTAGS
    extra: list[str] = []
    if args.handles:
        src = Path(args.handles)
        extra = src.read_text().split() if src.is_file() else args.handles.split(",")

    graph = Graph(ACCESS_TOKEN, IG_USER_ID)
    seen = load_seen(SEEN_BRANDS_PATH)
    leads: dict[str, list[Lead]] = {r: [] for r in wanted}

    def done() -> bool:
        return all(len(leads[r]) >= args.max_results for r in wanted)

    try:
        candidates = collect_candidates(graph, hashtags, extra)
        fresh = [c for c in candidates if c not in seen]
        log.info("%d candidates, %d already in %s", len(candidates), len(candidates) - len(fresh),
                 SEEN_BRANDS_PATH)
        for c in candidates:
            if c in seen:
                log.debug("SKIP @%s: already in seen_brands.csv", c)

        for start in range(0, len(fresh), BATCH_SIZE):
            if done():
                break
            batch = fresh[start:start + BATCH_SIZE]
            log.info("Batch %d: looking up %d profiles", start // BATCH_SIZE + 1, len(batch))
            for username in batch:
                if done():
                    break
                try:
                    profile = graph.business_discovery(username)
                except RateLimitExhausted:
                    raise
                except GraphError as exc:
                    reason = ("not a business/creator account (or handle not found)"
                              if exc.code in NOT_FOUND_CODES else str(exc))
                    log.info("SKIP @%s: %s", username, reason)
                    continue
                lead, reason = evaluate(profile, wanted)
                if lead is None:
                    log.info("SKIP @%s: %s", username, reason)
                elif len(leads[lead.region]) >= args.max_results:
                    log.info("SKIP @%s: %s already has %d leads", username, lead.region, args.max_results)
                else:
                    log.info("LEAD @%s (%s, %d followers, %s)", username, lead.region, lead.followers, lead.tier)
                    leads[lead.region].append(lead)
            time.sleep(random.uniform(*BATCH_SLEEP))
    except RateLimitExhausted as exc:
        log.error("Stopping early - rate limit not recovering: %s", exc)
    except KeyboardInterrupt:
        log.warning("Interrupted - saving what was found so far.")

    for region in sorted(wanted):
        write_leads(OUTPUT_PATHS[region], leads[region])
    append_seen(SEEN_BRANDS_PATH, [l.username for region in sorted(wanted) for l in leads[region]])

    print("\nRESULTS")
    for region in sorted(wanted):
        n = len(leads[region])
        ideal = sum(1 for l in leads[region] if l.tier == "ideal")
        line = f"  {region.upper():5} {n} new verified leads ({ideal} ideal <{IDEAL_CAP}) -> {OUTPUT_PATHS[region]}"
        if n < args.max_results:
            line += f"\n        Only {n} of the requested {args.max_results} qualified; nothing was padded."
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
