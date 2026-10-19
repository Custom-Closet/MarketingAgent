"""Funnel strategist: Claude reads the funnel numbers and a few lost chats and suggests what to change.

Runs only when asked (a button in the Sales view); the estimated cost is shown first.
Customer usernames are never sent, and phone numbers/emails are masked in the examples.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic

PRICE_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0)}   # USD (input, output)
EST_OUTPUT_TOKENS = 2_500

SYSTEM = """\
You are the sales strategist for the small business described below. You get its Instagram DM \
sales funnel (stage counts, conversion between stages, drop-off reasons, lead sources, segments, \
response times, outreach results) and a few anonymised lost conversations.

Give specific, practical advice the founder can act on this week:
- Lead with the 2-3 biggest leaks in the funnel, using the numbers.
- For each: why it happens (cite the evidence), what to change, and an exact message/template or \
process tweak where useful. Respect the business's real policies; flag a policy change as a choice.
- Suggest how to measure whether it worked (which number should move).
- Keep it under ~400 words unless asked for more. Markdown with short headings and bullets.
If the data can't support a conclusion, say so instead of guessing.

<business>
{brand}
</business>"""

_MASK = [(re.compile(r"\+?\d[\d\s-]{8,}\d"), "<phone>"), (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>")]


def mask(text: str) -> str:
    for pattern, repl in _MASK:
        text = pattern.sub(repl, text)
    return text


@dataclass
class Estimate:
    input_tokens: int
    output_tokens: int
    usd: float | None


class Strategist:
    def __init__(self, brand_text: str, *, model: str, effort: str, client: anthropic.Anthropic | None = None):
        self.system = SYSTEM.format(brand=brand_text.strip() or "(no brand profile yet)")
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    @staticmethod
    def briefing(report: dict, outreach: dict, examples: list[dict]) -> str:
        """The data block sent with every question."""
        data = {"funnel": report, "outreach": {k: v for k, v in outreach.items() if k != "sources"}}
        convos = "\n\n".join(f"<lost_conversation reason=\"{e['reason']}\" reached=\"{e['reached']}\">\n"
                             f"{mask(e['transcript'])}\n</lost_conversation>" for e in examples)
        return f"<funnel_data>\n{json.dumps(data, indent=1)}\n</funnel_data>\n\n{convos}"

    def estimate(self, briefing: str, question: str, history: list[dict]) -> Estimate:
        chars = len(self.system) + len(briefing) + len(question) + sum(len(m["content"]) for m in history)
        tokens = chars // 4
        price = PRICE_PER_MTOK.get(self.model)
        usd = round((tokens * price[0] + EST_OUTPUT_TOKENS * price[1]) / 1_000_000, 3) if price else None
        return Estimate(input_tokens=tokens, output_tokens=EST_OUTPUT_TOKENS, usd=usd)

    def advise(self, briefing: str, question: str, history: list[dict]) -> str:
        """`history`: earlier turns [{"role": "user"|"assistant", "content": str}] of this strategy chat."""
        messages = [*history, {"role": "user", "content": question}]
        messages[0] = {"role": "user", "content": f"{briefing}\n\n{messages[0]['content']}"}   # data goes first
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to answer this question")
        return "".join(b.text for b in response.content if b.type == "text").strip()
