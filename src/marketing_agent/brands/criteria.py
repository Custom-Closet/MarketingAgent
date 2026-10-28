"""Who qualifies: emerging, active, contemporary womenswear labels in the countries being searched.

A lead's `region` is its country: a lower-case ISO code ("us", "in", "gb"; see geo.py).

The numbers are checked in code from Business Discovery data (never guessed). Whether a bio reads
like a contemporary womenswear label is judged by keywords here, or by the model in judge.py
when one is configured.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..sources.instagram import IGProfile, parse_time
from .geo import infer_country

FOLLOWER_CAP = 10_000       # hard cap (exclusive)
IDEAL_CAP = 5_000           # "ideal" below this
MIN_MEDIA_COUNT = 3
MAX_MEDIA_COUNT = 800       # more posts than this reads as an established brand
ACTIVITY_WINDOW_DAYS = 60
RECENT_MEDIA_LIMIT = 5

APPAREL_KEYWORDS = [
    "clothing", "apparel", "womenswear", "women's wear", "womens wear", "fashion", "label", "dresses",
    "co-ords", "coords", "co-ord", "tops", "wear", "garments", "collection", "designer", "studio",
    "atelier", "couture", "ready to wear", "rtw", "prêt", "pret", "linen", "sets", "separates",
]
# Never a fit: resellers / curated / non-production businesses.
EXCLUDE_KEYWORDS = [
    "reseller", "resellers", "curated boutique", "multi-designer", "multi designer", "multidesigner",
    "dropship", "dropshipping", "preloved", "thrift", "consignment", "wholesale only", "rental",
]
# Not a fit unless the bio also shows contemporary womenswear (bridal-only / kids-only / menswear-only).
EXCLUDE_UNLESS_WOMENSWEAR = [
    "bridal", "bridalwear", "wedding wear", "kidswear", "kids wear", "kids", "baby",
    "menswear", "men's wear", "for men",
]
WOMENSWEAR_KEYWORDS = [
    "women", "womens", "women's", "womenswear", "her", "she", "dresses", "co-ords", "western",
    "contemporary", "everyday wear", "workwear", "resort", "tops",
]
# Traditional ethnicwear: not a fit unless the bio also shows western / contemporary pieces.
ETHNIC_KEYWORDS = [
    "kurta", "kurtas", "kurti", "kurtis", "saree", "sarees", "sari", "lehenga", "lehengas", "salwar",
    "anarkali", "ethnic wear", "ethnicwear", "dupatta", "ethnic",
]
WESTERN_KEYWORDS = [
    "western", "contemporary", "dresses", "co-ords", "co-ord", "tops", "fusion", "indo-western",
    "indowestern", "modern", "resort", "workwear", "everyday",
]
# Brands likely to need sampling / low-MOQ production / fabric sourcing.
FIT_KEYWORDS = [
    "small batch", "small-batch", "handmade", "hand made", "made to order", "made-to-order",
    "slow fashion", "limited edition", "limited drops", "sustainable", "ethically made",
    "independent", "indie", "new launch", "launching", "just launched", "est. 2024", "est. 2025",
    "est 2025", "est. 2026", "est 2026", "since 2025", "since 2026", "pre-order", "preorder",
]
# Preferred outside India: Indian / South Asian founders or Indian textiles and craft.
INDIAN_CONNECTION_KEYWORDS = [
    "indian", "south asian", "desi", "made in india", "handcrafted in india", "indian-owned",
    "indian owned", "block print", "block-printed", "hand block", "ajrakh", "chikankari", "khadi",
    "handloom", "ikat", "kantha", "bandhani", "zari", "kalamkari", "jamdani", "banarasi",
    "mulmul", "dabu", "artisans in india", "jaipur", "kutch",
]


def _phrase_re(phrases: list[str]) -> re.Pattern:
    # Whole-word match; emoji / punctuation-bounded phrases fall back to plain substring matching.
    return re.compile("|".join(rf"(?<!\w){re.escape(p)}(?!\w)" for p in phrases), re.IGNORECASE)


APPAREL_RE, EXCLUDE_RE = _phrase_re(APPAREL_KEYWORDS), _phrase_re(EXCLUDE_KEYWORDS)
EXCLUDE_SOFT_RE, WOMENSWEAR_RE = _phrase_re(EXCLUDE_UNLESS_WOMENSWEAR), _phrase_re(WOMENSWEAR_KEYWORDS)
ETHNIC_RE, WESTERN_RE = _phrase_re(ETHNIC_KEYWORDS), _phrase_re(WESTERN_KEYWORDS)
FIT_RE, INDIAN_CONN_RE = _phrase_re(FIT_KEYWORDS), _phrase_re(INDIAN_CONNECTION_KEYWORDS)

FOUNDER_PATTERNS = [
    re.compile(r"(?i:co-?founder|founder|founded by|ceo|creative director|designer)\s*[:\-–|,]?\s*"
               r"(@[A-Za-z0-9_.]+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"),
    re.compile(r"\bby\s+(@[A-Za-z0-9_.]+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"),  # "by Priya Shah" (capitalised)
    re.compile(r"(@[A-Za-z0-9_.]+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*[,(|\-–]\s*(?i:co-?founder|founder|ceo)"),
]
_NOT_NAMES = {"the", "our", "and", "for", "you", "women", "her", "hand", "love", "design", "india", "usa"}
PERSON_NAME_RE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}$")
SELF_DESIGNER_RE = re.compile(r"(?i)\b(?:i design|my designs|designer|founder|designed by me|by me)\b")


def normalize(text: str | None) -> str:
    """Fold stylised Unicode letters (𝘉𝘺 𝘚𝘩𝘢𝘺𝘢, 𝐁𝐲 ...) to plain text so the regexes match."""
    return unicodedata.normalize("NFKC", text or "")


def extract_founder(bio: str, name: str = "") -> str:
    for pattern in FOUNDER_PATTERNS:
        for m in pattern.finditer(bio):
            candidate = m.group(1).strip()
            if candidate.split()[0].lower() not in _NOT_NAMES:
                return candidate
    # Designer-run accounts ("Shaya Nadaf": "...& designer") - the account name is the founder.
    if PERSON_NAME_RE.match(name.strip()) and SELF_DESIGNER_RE.search(bio):
        return name.strip()
    return ""


def infer_region(name: str, bio: str, website: str, captions: str = "",
                 places: dict[str, str] | None = None) -> str | None:
    """Country code ('us', 'in', 'gb'...) the brand says it is based in, or None (see geo.infer_country)."""
    return infer_country(name, bio, website, captions, places)


@dataclass
class BrandLead:
    region: str
    username: str
    name: str
    followers: int
    founder: str
    website: str
    bio: str
    last_post: datetime
    media_count: int = 0
    signals: list[str] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)

    @property
    def tier(self) -> str:
        return "ideal" if self.followers < IDEAL_CAP else "ok"

    @property
    def link(self) -> str:
        return f"https://www.instagram.com/{self.username}/"

    @property
    def score(self) -> int:
        # Fit signals and (US) Indian connection first, then smaller accounts.
        return len(self.signals) * 10 + (5 if self.tier == "ideal" else 0)


@dataclass
class Screened:
    """A profile that passed the numeric checks; `lead` is set when the keyword rules accept it."""
    profile: IGProfile
    last_post: datetime
    captions: str
    region: str | None
    signals: list[str]
    founder: str
    lead: BrandLead | None = None
    reason: str = ""      # keyword verdict when rejected

    def to_lead(self, region: str, *, founder: str = "") -> BrandLead:
        p = self.profile
        signals = list(self.signals)
        if region != "in":      # outside India, Indian founders / textiles are the preferred fit
            text = normalize(f"{p.name} {p.biography} {self.captions}")
            signals += sorted({f"indian:{m.lower()}" for m in INDIAN_CONN_RE.findall(text)})
        return BrandLead(region, p.username.lower(), normalize(p.name) or p.username, p.followers_count or 0,
                         self.founder or founder, p.website or "", normalize(p.biography), self.last_post,
                         p.media_count or 0, signals)


def hard_filters(profile: IGProfile, *, now: datetime | None = None,
                 places: dict[str, str] | None = None) -> tuple[Screened | None, str]:
    """Numbers and exclusions that never need judgement. Returns (screened, "") or (None, reason)."""
    now = now or datetime.now(timezone.utc)
    name, bio = normalize(profile.name), normalize(profile.biography)
    text = f"{name} {bio}"
    followers, media_count = profile.followers_count, profile.media_count or 0
    if followers is None:
        return None, "followers_count hidden"
    if followers >= FOLLOWER_CAP:
        return None, f"{followers} followers >= {FOLLOWER_CAP}"
    if media_count < MIN_MEDIA_COUNT:
        return None, f"only {media_count} posts"
    if media_count > MAX_MEDIA_COUNT:
        return None, f"{media_count} posts - established"
    if m := EXCLUDE_RE.search(text):
        return None, f"excluded keyword '{m.group(0)}'"
    stamps = [parse_time(m["timestamp"]) for m in profile.recent_media if m.get("timestamp")]
    if not stamps:
        return None, "no posts returned"
    last_post = max(stamps)
    if now - last_post > timedelta(days=ACTIVITY_WINDOW_DAYS):
        return None, f"inactive (last post {last_post.date()})"
    captions = normalize(" ".join(m.get("caption") or "" for m in profile.recent_media))
    return Screened(profile, last_post, captions, infer_region(name, bio, profile.website or "", captions, places),
                    sorted({m.lower() for m in FIT_RE.findall(text)}), extract_founder(bio, name)), ""


def keyword_verdict(screened: Screened, wanted: set[str]) -> Screened:
    """Accept or reject on keywords alone (used when no model is configured)."""
    p = screened.profile
    text = normalize(f"{p.name} {p.biography}")
    if (m := EXCLUDE_SOFT_RE.search(text)) and not WOMENSWEAR_RE.search(text):
        screened.reason = f"'{m.group(0)}' without womenswear signal"
    elif (m := ETHNIC_RE.search(text)) and not WESTERN_RE.search(text):
        screened.reason = f"traditional ethnicwear ('{m.group(0)}')"
    elif not APPAREL_RE.search(text):
        screened.reason = "bio does not look like a clothing brand"
    elif screened.region is None:
        screened.reason = "country unverifiable or ambiguous"
    elif screened.region not in wanted:
        screened.reason = f"based in {screened.region.upper()}, not a searched country"
    else:
        screened.lead = screened.to_lead(screened.region)
    return screened
