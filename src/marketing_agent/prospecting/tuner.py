"""Tune the search setup with the model: it reads what each query actually found and proposes changes.

Nothing is saved until the person applies the proposal in the app. Runs only when asked; the
estimated cost is shown first.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from ..brands.config import SearchConfig
from ..llm import LLM

EST_OUTPUT_TOKENS = 3_000

SYSTEM = """\
You help tune the web searches that find prospects for a made-to-order desi clothing brand that also \
produces for new clothing brands (sampling, small-batch / low-MOQ production, fabric sourcing).

Prospects: emerging women's clothing brands in the target countries, under 10K Instagram followers \
(ideally under 5K), active in the last 60 days, contemporary / western womenswear (not traditional \
ethnicwear only). Outside India, brands with Indian / South Asian founders or Indian textiles and \
craft rank first.

How the search works: `targets` lists the countries searched (ISO 3166 alpha-2 codes, lower case: \
us, in, gb, ca, au, ae...), each with `places` - its states, provinces or cities (empty = the country \
name). Every keyword is combined with every place as `site:instagram.com "<keyword>" "<place>"`; \
places outside India are also searched with each craft term; one brand-website query per place; \
`extra_queries` run as written every time; `exclude_terms` are added as -word to every query. A run \
takes `queries_per_run` queries from this list (rotating day to day, interleaved across countries), \
then checks up to `lookup_limit` Instagram profiles within `minutes`, stopping at `leads_per_run` \
prospects with at most `max_per_country` from one country. Search snippets of big accounts are \
dropped before any check. Places: use the names people write in bios (cities and states), in English.

You get the current setup, how each query performed so far (candidates found, how many qualified, \
contacted) and why candidates were rejected. When the person asks for a change:
- Explain briefly what you'd change and why, citing the numbers when there are any.
- Return the FULL revised setup in `config`, changing only what is needed, and list each change in \
`changes` as a short sentence. If no change is needed (or they only asked a question), set `config` \
to null and `changes` to [].
- Adding a country means adding a target with its main fashion cities or states; more places means \
each one is searched less often, so prefer a focused list over every state.
- Keep lists focused: queries that keep finding only big or inactive accounts should go; phrases \
small labels actually use in bios ("small batch", "made to order", "DM to order") work better than \
generic fashion words.
- `criteria` decides who qualifies (follower and post limits, how recently they posted, segments, \
ethnicwear, extra fit notes). Don't loosen or tighten it unless the person asks; if the data suggests \
it, say so and let them decide.
- Cost: every search is billed (`cost_per_1000`). `queries_per_run`, `results_per_query`, \
`site_queries` and `monthly_search_budget` drive the cost. Don't raise them unless asked; when \
cutting waste, drop queries that keep finding nothing.
- Never claim results you weren't given.
"""


class TuneReply(BaseModel):
    reply: str = Field(description="Short answer to the person, plain text or simple Markdown")
    changes: list[str] = Field(description="Each proposed change as one short sentence; [] if none")
    config: SearchConfig | None = Field(description="The full revised setup, or null when nothing changes")


def briefing(config: SearchConfig, query_yield: list[dict], rejections: list[dict], stages: dict[str, int]) -> str:
    good = [q for q in query_yield if q["leads"]][:15]
    weak = sorted((q for q in query_yield if not q["leads"]), key=lambda q: -q["runs"])[:15]
    data = {
        "current_setup": config.model_dump(),
        "queries_that_found_prospects": good,
        "queries_with_no_prospects_yet": weak,
        "queries_run_so_far": len(query_yield),
        "why_candidates_were_rejected": rejections,
        "prospects_by_stage": stages,
    }
    return f"<search_data>\n{json.dumps(data, indent=1)}\n</search_data>"


def _prompt(brief: str, message: str, history: list[dict]) -> str:
    convo = "\n".join(f"<{t['role']}>{t['content']}</{t['role']}>" for t in history)
    return f"{brief}\n\n<conversation>\n{convo}\n<user>{message}</user>\n</conversation>"


def estimate(llm: LLM, brief: str, message: str, history: list[dict]) -> dict:
    tokens = (len(SYSTEM) + len(_prompt(brief, message, history))) // 4
    price = llm.price()
    usd = round((tokens * price[0] + EST_OUTPUT_TOKENS * price[1]) / 1_000_000, 3) if price else None
    return {"input_tokens": tokens, "output_tokens": EST_OUTPUT_TOKENS, "usd": usd}


def tune(llm: LLM, brief: str, message: str, history: list[dict]) -> TuneReply:
    reply = llm.parse(system=SYSTEM, prompt=_prompt(brief, message, history), schema=TuneReply, max_tokens=8000)
    if reply.config is not None:
        reply.config = reply.config.clean()
    return reply
