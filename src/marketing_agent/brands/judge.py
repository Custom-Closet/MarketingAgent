"""Model check for what keywords judge badly: is this a contemporary womenswear label, and where is it based?

Profiles that already passed the numeric checks (criteria.hard_filters) are sent in small batches.
The model only classifies the text it is given; follower counts and dates never come from it.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from ..llm import LLM
from .config import Criteria
from .criteria import Screened
from .geo import country_name

BATCH_SIZE = 8

SEGMENT_WORDS = {"womenswear": "womenswear", "kidswear": "kidswear (children's clothing)", "menswear": "menswear"}

SYSTEM_PROMPT = """\
You screen Instagram accounts for a made-to-order desi clothing brand that also produces for \
new clothing brands: sampling, small-batch / low-MOQ production, garment development and fabric sourcing.

A fit is an emerging brand selling its OWN {segments}{styles}.
Not a fit: {not_fit}jewellery / bags / shoes / beauty, resellers, multi-brand boutiques, thrift or \
rental, stylists, photographers, models, influencers and fabric shops.{notes}

Country is where the brand is BASED, as an ISO 3166 alpha-2 code (us, in, gb, ca, ae...). Use only \
what the bio, website or captions say; shipping destinations don't count. Answer "unknown" when it \
isn't stated - never guess from names or language.
"""


def system_prompt(criteria: Criteria) -> str:
    """The fit rules, written from the search setup's criteria."""
    wanted = [SEGMENT_WORDS[s] for s in criteria.segments]
    others = [s for s in SEGMENT_WORDS if s not in criteria.segments]
    styles = (" (any style: contemporary, western, fusion or traditional ethnicwear)" if criteria.allow_ethnic_only
              else ": contemporary or western pieces (dresses, co-ords, tops, separates, resortwear, everyday "
                   "or workwear). Fusion / indo-western labels are a fit")
    not_fit = "".join([
        "" if criteria.allow_ethnic_only else "traditional ethnicwear only (sarees, lehengas, salwar suits, kurta sets), ",
        "bridal-only, ",
        "".join(f"{s}-only or mostly {s}, " for s in others),
    ])
    notes = f"\n\nAlso, from the founder:\n{criteria.fit_notes}" if criteria.fit_notes else ""
    return SYSTEM_PROMPT.format(segments=" or ".join(wanted), styles=styles, not_fit=not_fit, notes=notes)


class Verdict(BaseModel):
    username: str
    fits: bool = Field(description="Sells its own clothing that matches the fit rules")
    country: str = Field(description="ISO 3166 alpha-2 code where the brand is based, or 'unknown'")
    founder: str = Field(description="Founder's name if the text states it, else empty")
    reason: str = Field(description="Under 12 words")


class Verdicts(BaseModel):
    verdicts: list[Verdict]


def _profile_block(s: Screened) -> dict:
    p = s.profile
    return {"username": p.username, "name": p.name or "", "bio": p.biography or "", "website": p.website or "",
            "recent_captions": [(m.get("caption") or "")[:300] for m in p.recent_media]}


def judge(llm: LLM, batch: list[Screened], wanted: set[str]) -> None:
    """Fill `lead` or `reason` on every screened profile in the batch."""
    prompt = ("Classify each account.\n\n"
              + json.dumps([_profile_block(s) for s in batch], ensure_ascii=False, indent=1))
    result = llm.parse(system=system_prompt(batch[0].criteria), prompt=prompt, schema=Verdicts, max_tokens=4000)
    by_name = {v.username.lower().lstrip("@"): v for v in result.verdicts}
    for s in batch:
        v = by_name.get(s.profile.username.lower())
        if v is None:
            s.reason = "model gave no verdict"
            continue
        # A country stated in the bio or website (found by code) wins over the model's reading.
        said = v.country.strip().lower()
        region = s.region or (said if len(said) == 2 and country_name(said).lower() != said else None)
        if not v.fits:
            s.reason = f"model: {v.reason}"
        elif region is None:
            s.reason = "country unverifiable"
        elif region not in wanted:
            s.reason = f"based in {region.upper()}, not a searched country"
        else:
            s.lead = s.to_lead(region, founder=v.founder.strip())
