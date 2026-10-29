"""The editable search setup: what the brand finder searches for.

Saved as versions in `brand_search_configs` (edited in the app, by hand or with the model's help);
the newest version is used. The defaults below are the starting point.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

DEFAULT_KEYWORDS = [
    "womenswear label", "women's clothing brand", "independent fashion label", "small batch clothing",
    "slow fashion brand", "made to order", "handmade clothing", "co-ord sets", "linen clothing",
    "contemporary womenswear", "emerging designer", "new clothing brand", "emerging women's clothing brand",
    "new boutique launch", "boutique",
]
DEFAULT_USA_CITIES = [
    "New York", "Brooklyn", "Los Angeles", "San Francisco", "Chicago", "Houston", "Dallas", "Austin",
    "Seattle", "Boston", "Atlanta", "Miami", "New Jersey", "Philadelphia", "Washington DC", "Portland",
    "Denver", "San Diego", "Nashville", "Charlotte",
]
DEFAULT_INDIA_CITIES = [
    "Mumbai", "Delhi", "Bangalore", "Chennai", "Hyderabad", "Kolkata", "Pune", "Jaipur", "Ahmedabad",
    "Gurgaon", "Noida", "Chandigarh", "Kochi", "Goa",
]
DEFAULT_US_INDIAN_TERMS = [
    "block print", "chikankari", "handloom", "ikat", "south asian owned", "indian owned", "desi",
]


SEGMENTS = ("womenswear", "kidswear", "menswear")


class Criteria(BaseModel):
    """Who counts as a prospect. Checked on every profile; the follower / post limits also drop big
    accounts straight from search snippets, before any Instagram lookup."""
    min_followers: int = Field(0, ge=0, description="Fewer followers than this is too early")
    max_followers: int = Field(10_000, ge=100, description="This many followers or more is too big")
    ideal_followers: int = Field(5_000, ge=0, description="Under this ranks first ('ideal')")
    min_posts: int = Field(3, ge=0)
    max_posts: int = Field(800, ge=1, description="More posts than this reads as an established brand")
    active_within_days: int = Field(60, ge=7, le=365, description="Must have posted within this many days")
    segments: list[str] = Field(default_factory=lambda: ["womenswear"],
                                description="Who the brand dresses: womenswear / kidswear / menswear")
    allow_ethnic_only: bool = Field(False, description="Also accept brands selling only traditional ethnicwear")
    fit_notes: str = Field("", description="Extra rules for the model's fit check, in plain words")


class Target(BaseModel):
    country: str = Field(description="ISO 3166 alpha-2 code, lower case, e.g. 'us', 'in', 'gb'")
    places: list[str] = Field(default_factory=list,
                              description="States or cities searched with every keyword; empty = the country name")


def default_targets() -> list[Target]:
    return [Target(country="us", places=list(DEFAULT_USA_CITIES)),
            Target(country="in", places=list(DEFAULT_INDIA_CITIES))]


class SearchConfig(BaseModel):
    keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_KEYWORDS),
                                description="Phrases combined with every place, e.g. 'womenswear label'")
    targets: list[Target] = Field(default_factory=default_targets,
                                  description="Countries to search, each with its states or cities")
    craft_terms: list[str] = Field(default_factory=lambda: list(DEFAULT_US_INDIAN_TERMS),
                                   description="Indian craft / founder terms, searched with places outside India")
    extra_queries: list[str] = Field(default_factory=list,
                                     description="Complete queries run as written, e.g. 'site:instagram.com ...'")
    exclude_terms: list[str] = Field(default_factory=list,
                                     description="Words added as -word to every query, e.g. 'swimwear'")
    criteria: Criteria = Field(default_factory=Criteria)
    leads_per_run: int = Field(40, ge=1, le=500, description="Prospects to find per run, all countries together")
    max_per_country: int = Field(20, ge=1, le=500, description="At most this many from one country per run")
    queries_per_run: int = Field(100, ge=1, le=500)
    lookup_limit: int = Field(300, ge=10, le=1000, description="Instagram profile checks per run")
    minutes: int = Field(12, ge=2, le=60, description="Time budget for one run")
    # ---- web search cost ----
    results_per_query: int = Field(10, ge=1, le=100, description="Results asked for per search (Serper free: 10)")
    site_queries: bool = Field(True, description="Also search brand websites (one extra search per place)")
    snowball: bool = Field(True, description="Mine known brands' captions for more brands (Instagram calls, no search cost)")
    monthly_search_budget: int = Field(0, ge=0, description="Stop searching after this many searches a month; 0 = no limit")
    cost_per_1000: float = Field(1.0, ge=0, description="What the search provider charges per 1,000 searches, in USD")

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data):
        """Setups saved before countries were configurable (usa_cities / india_cities / us_indian_terms)."""
        if isinstance(data, dict) and "targets" not in data and ("usa_cities" in data or "india_cities" in data):
            data = dict(data)
            data["targets"] = [{"country": "us", "places": data.pop("usa_cities", [])},
                               {"country": "in", "places": data.pop("india_cities", [])}]
            data["targets"] = [t for t in data["targets"] if t["places"]]
            if "us_indian_terms" in data:
                data["craft_terms"] = data.pop("us_indian_terms")
            if "leads_per_region" in data:
                per = data.pop("leads_per_region")
                data.setdefault("max_per_country", per)
                data.setdefault("leads_per_run", per * max(1, len(data["targets"])))
        return data

    @property
    def countries(self) -> list[str]:
        return [t.country for t in self.targets]

    def place_index(self) -> dict[str, str]:
        """Place name -> country code, so a bio naming a searched place counts towards that country."""
        return {p: t.country for t in self.targets for p in t.places if len(p) >= 4}

    @classmethod
    def from_json(cls, raw: str | None) -> "SearchConfig":
        return cls.model_validate_json(raw) if raw else cls()

    def clean(self) -> "SearchConfig":
        """Trim, drop blanks and duplicates (case-insensitive), keep order; merge repeated countries."""
        def dedupe(items: list[str]) -> list[str]:
            seen, out = set(), []
            for item in (i.strip() for i in items):
                if item and item.lower() not in seen:
                    seen.add(item.lower())
                    out.append(item)
            return out
        data = self.model_dump()
        for key, value in data.items():
            if isinstance(value, list) and key != "targets":
                data[key] = dedupe(value)
        merged: dict[str, list[str]] = {}
        for t in data["targets"]:
            code = t["country"].strip().lower()
            if len(code) == 2:
                merged[code] = dedupe(merged.get(code, []) + t["places"])
        data["targets"] = [{"country": c, "places": p} for c, p in merged.items()]
        data["max_per_country"] = min(data["max_per_country"], data["leads_per_run"])
        c = data["criteria"]
        c["segments"] = [s for s in dedupe([s.lower() for s in c["segments"]]) if s in SEGMENTS] or ["womenswear"]
        c["max_followers"] = max(c["max_followers"], c["min_followers"] + 1)
        c["ideal_followers"] = min(c["ideal_followers"], c["max_followers"])
        c["max_posts"] = max(c["max_posts"], c["min_posts"])
        c["fit_notes"] = c["fit_notes"].strip()[:1000]
        return SearchConfig(**data)
