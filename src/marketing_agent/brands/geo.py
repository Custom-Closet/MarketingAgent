"""Countries and states (ISO 3166, via pycountry), and where a brand says it is based.

Countries are lower-case ISO alpha-2 codes everywhere ("us", "in", "gb"). A brand's country is read
from its name, bio and website: country names, flag emoji, state / province names that belong to
one country only, well-known cities, and the website's country domain (.in, .co.uk, .com.au).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from functools import lru_cache

import pycountry

# Names people write that ISO doesn't list.
ALIASES = {
    "us": ["usa", "u.s.a", "u.s.", "united states", "america", "us based", "us-based", "ny", "tx", "nc", "nyc",
           "la based"],
    "gb": ["uk", "u.k.", "britain", "great britain", "united kingdom", "england", "scotland", "wales"],
    "in": ["bharat"],
    "ae": ["uae", "u.a.e"],
    "kr": ["south korea", "korea"],
    "ru": ["russia"],
    "vn": ["vietnam"],
    "tw": ["taiwan"],
    "ir": ["iran"],
    "tz": ["tanzania"],
    "nl": ["holland", "the netherlands"],
}
# Cities brands put in their bios. Larger fashion hubs only; states and countries are matched from ISO.
CITIES = {
    "us": ["new york", "brooklyn", "manhattan", "los angeles", "san francisco", "bay area", "chicago", "houston",
           "dallas", "austin", "seattle", "boston", "atlanta", "miami", "philadelphia", "washington dc",
           "portland", "denver", "san diego", "nashville", "charlotte", "raleigh", "phoenix", "detroit", "atx"],
    "in": ["mumbai", "bombay", "delhi", "new delhi", "ncr", "gurgaon", "gurugram", "noida", "bengaluru",
           "bangalore", "chennai", "hyderabad", "kolkata", "pune", "jaipur", "ahmedabad", "surat", "lucknow",
           "chandigarh", "kochi", "indore", "tiruppur", "coimbatore"],
    "gb": ["london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh", "bristol"],
    "ca": ["toronto", "vancouver", "montreal", "calgary", "ottawa", "mississauga", "brampton"],
    "au": ["sydney", "melbourne", "brisbane", "perth", "adelaide"],
    "ae": ["dubai", "abu dhabi", "sharjah"],
    "fr": ["paris", "lyon", "marseille"],
    "it": ["milan", "milano", "rome", "florence"],
    "de": ["berlin", "munich", "hamburg"],
    "es": ["madrid", "barcelona"],
    "nl": ["amsterdam", "rotterdam"],
    "sg": ["singapore"],
    "pk": ["karachi", "lahore", "islamabad"],
    "bd": ["dhaka"],
    "lk": ["colombo"],
    "np": ["kathmandu"],
    "ng": ["lagos", "abuja"],
    "za": ["johannesburg", "cape town", "durban"],
    "ke": ["nairobi"],
    "jp": ["tokyo", "osaka"],
    "kr": ["seoul"],
    "mx": ["mexico city", "guadalajara"],
    "br": ["sao paulo", "são paulo", "rio de janeiro"],
}
# Country domains that are mostly used generically, so they say nothing about location.
GENERIC_TLDS = {"co", "io", "me", "ai", "tv", "fm", "ly", "to", "gg", "so", "sh", "ws", "cc", "la", "st", "is",
                "it", "am", "im", "id", "us", "nu", "ac", "bz", "vc", "ag", "ms", "pw", "tk", "ml", "ga", "cf"}
MIN_STATE_NAME = 5          # shorter subdivision names are too often ordinary words
SHIPPING_PHRASE_RE = re.compile(r"\b(?:ships?|shipping|delivery|delivering)\s+(?:to|in|across)?\s*[\w\s,&/]{0,40}",
                                re.IGNORECASE)


def plain(name: str) -> str:
    """'Bihār' -> 'Bihar', 'Wales [Cymru GB-CYM]' -> 'Wales': the way people write place names."""
    name = re.sub(r"\s*\[.*?\]", "", name)
    return "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch)).strip()


def country_name(code: str | None) -> str:
    if not code:
        return ""
    c = pycountry.countries.get(alpha_2=code.upper())
    return (getattr(c, "common_name", None) or c.name) if c else code.upper()


def flag(code: str) -> str:
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code.upper()) if len(code) == 2 else ""


@lru_cache(maxsize=1)
def countries() -> list[dict]:
    """Every country: code, name and flag, sorted by name."""
    out = [{"code": c.alpha_2.lower(), "name": getattr(c, "common_name", None) or c.name, "flag": flag(c.alpha_2)}
           for c in pycountry.countries]
    return sorted(out, key=lambda c: c["name"])


@lru_cache(maxsize=None)
def states(code: str) -> list[str]:
    """Top-level states / provinces / regions of a country (not districts or boroughs)."""
    subs = pycountry.subdivisions.get(country_code=code.upper()) or []
    return sorted({plain(s.name) for s in subs if not getattr(s, "parent_code", None)})


@lru_cache(maxsize=1)
def _index() -> tuple[re.Pattern, dict[str, str]]:
    """One regex over every place phrase, and phrase -> country code."""
    phrases: dict[str, str] = {}
    ambiguous: set[str] = set()

    def add(phrase: str, code: str) -> None:
        phrase = phrase.lower().strip()
        if not phrase:
            return
        if phrase in phrases and phrases[phrase] != code:
            ambiguous.add(phrase)
        phrases.setdefault(phrase, code)

    country_names: set[str] = set()
    for c in pycountry.countries:
        code = c.alpha_2.lower()
        for attr in ("name", "common_name", "official_name"):
            if getattr(c, attr, None):
                add(c.name if attr == "name" else getattr(c, attr), code)
                country_names.add(getattr(c, attr).lower())
        add(flag(c.alpha_2), code)
    for code, names in ALIASES.items():
        for n in names:
            add(n, code)
    for code, names in CITIES.items():
        for n in names:
            add(n, code)
    for s in pycountry.subdivisions:
        name = plain(s.name)
        if getattr(s, "parent_code", None) or len(name) < MIN_STATE_NAME:
            continue
        if name.lower() in country_names:       # e.g. Georgia the state vs Georgia the country
            ambiguous.add(name.lower())
            continue
        add(name, s.country_code.lower())
        if name != s.name:
            add(s.name, s.country_code.lower())
    for phrase in ambiguous:
        phrases.pop(phrase, None)
    ordered = sorted(phrases, key=len, reverse=True)   # longest first: "new delhi" before "delhi"
    pattern = re.compile("|".join(rf"(?<!\w){re.escape(p)}(?!\w)" for p in ordered), re.IGNORECASE)
    return pattern, phrases


def _votes(text: str) -> Counter:
    pattern, phrases = _index()
    text = SHIPPING_PHRASE_RE.sub(" ", text or "")
    return Counter(phrases[m.group(0).lower()] for m in pattern.finditer(text) if m.group(0).lower() in phrases)


def website_country(website: str | None) -> str | None:
    host = re.sub(r"^https?://", "", (website or "").lower()).split("/")[0].split(":")[0]
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    tld = "gb" if tld == "uk" else tld
    if len(tld) != 2 or tld in GENERIC_TLDS or not pycountry.countries.get(alpha_2=tld.upper()):
        return None
    return tld


def infer_country(name: str, bio: str, website: str = "", captions: str = "",
                  places: dict[str, str] | None = None) -> str | None:
    """Lower-case country code, or None when unstated or ambiguous. Captions only break a tie.

    `places` adds the search setup's own places (place name -> country code).
    """
    votes = _votes(f"{name} {bio}")
    if places:
        text = SHIPPING_PHRASE_RE.sub(" ", f"{name} {bio}").lower()
        for place, code in places.items():
            if re.search(rf"(?<!\w){re.escape(place.lower())}(?!\w)", text):
                votes[code] += 1
    if site := website_country(website):
        votes[site] += 2
    top = votes.most_common(2)
    if not top or (len(top) == 2 and top[0][1] == top[1][1]):
        cap = _votes(captions)
        votes.update(cap)
        top = votes.most_common(2)
    if not top or (len(top) == 2 and top[0][1] == top[1][1]):
        return None
    return top[0][0]
