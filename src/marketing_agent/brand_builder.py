"""Draft brand.md from archived Instagram DMs: what customers ask and how the business answers.

Two steps:
1. Extract - chats are split into batches; Claude pulls FAQs, policies, objections and tone from each.
2. Merge   - one call combines every batch's findings with the current brand.md into a new draft.

The result is written to a separate draft file for a human to review; brand.md is never overwritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from .db import Database
from .llm import LLM
from .pipeline import transcript_text

BATCH_CHARS = 60_000          # ~15k tokens of transcript per extraction call
BATCH_MAX_CHATS = 40
EST_OUTPUT_TOKENS_PER_BATCH = 4_000
EST_MERGE_OUTPUT_TOKENS = 12_000

EXTRACT_PROMPT = """\
Below are real Instagram DM conversations between a business and its customers ("us" is the \
business, "them" is the customer). Extract what a new sales assistant would need to learn to \
reply like the business does. Only report what the conversations actually show - never invent \
prices, policies or facts. Count how many separate conversations support each item.

<current_brand_profile>
{brand}
</current_brand_profile>

<conversations>
{transcripts}
</conversations>"""

MERGE_PROMPT = """\
Rewrite the business's brand profile (used as the system prompt for an AI that drafts DM and \
email replies, every one reviewed by a human) using the findings extracted from {chats} real \
customer conversations below.

Output the complete new profile as Markdown, and nothing else. Keep the existing sections \
(# Brand profile, ## Who we are, ## Ideal customer, ## Offer / call to action, ## Voice, \
## Proof points) - fill them from the evidence, keeping any real content already there - and add:
- ## FAQ - the most common customer questions, each with the answer the business actually gives.
- ## Sales playbook - how to engage, qualify (what to ask for: reference image, quantity, \
measurements, deadline), quote, follow up and close, based on what worked in these chats.
- ## Policies - MOQ, samples, pricing approach, shipping/export, timelines, payment.
- ## Do / Don't - tone and behaviour rules for replies.
- ## Needs confirmation - anything uncertain, inconsistent between chats, or only seen once. \
Prices and policies that conflict go here, not in the sections above.

After each fact add how many conversations support it, e.g. "(seen in 7 chats)". Be concrete \
and concise; this text is sent with every draft.

<current_brand_profile>
{brand}
</current_brand_profile>

<findings>
{findings}
</findings>"""


class FAQ(BaseModel):
    question: str = Field(description="Customer question, generalised")
    answer_given: str = Field(description="How the business answered, or empty if never answered")
    chats: int = Field(description="Number of conversations where this came up")


class Fact(BaseModel):
    topic: str = Field(description="e.g. MOQ, samples, pricing, shipping, fabrics, timelines, location")
    statement: str = Field(description="What the business said, as specifically as the chats show")
    chats: int


class Objection(BaseModel):
    objection: str
    response: str = Field(description="How the business handled it")
    outcome: str = Field(description="What happened next, if visible")


class BatchFindings(BaseModel):
    faqs: list[FAQ]
    facts: list[Fact] = Field(description="Products, fabrics, pricing, MOQ, samples, shipping, policies")
    objections: list[Objection]
    conversion_signals: list[str] = Field(description="Signs a chat was turning into an order")
    winning_replies: list[str] = Field(description="Verbatim replies from the business that moved a chat forward")
    tone_notes: list[str] = Field(description="How the business writes: language, length, emojis, greetings")


@dataclass
class Plan:
    batches: list[str]                # transcript text per extraction call
    chats: int
    input_tokens: int
    est_cost_usd: float | None


def _transcripts(db: Database, ig_user_id: str, since: datetime) -> list[str]:
    chats = []
    for convo in db.ig_conversations_since(since):
        text = transcript_text(db, convo["id"], ig_user_id)
        if "them:" in text:  # skip chats with no customer messages
            chats.append(f"<conversation customer=\"@{convo['username'] or 'unknown'}\">\n{text}\n</conversation>")
    return chats


def _batch(chats: list[str]) -> list[str]:
    batches: list[list[str]] = [[]]
    size = 0
    for chat in chats:
        if batches[-1] and (size + len(chat) > BATCH_CHARS or len(batches[-1]) >= BATCH_MAX_CHATS):
            batches.append([])
            size = 0
        batches[-1].append(chat)
        size += len(chat)
    return ["\n\n".join(b) for b in batches if b]


class BrandBuilder:
    SYSTEM = "You analyse a small business's customer conversations to document how it sells."

    def __init__(self, brand_file: Path, *, llm: LLM):
        self.brand = brand_file.read_text().strip() if brand_file.exists() else ""
        self.llm = llm

    def plan(self, db: Database, ig_user_id: str, since: datetime) -> Plan:
        chats = _transcripts(db, ig_user_id, since)
        batches = _batch(chats)
        tokens = sum(self.llm.count_tokens(system=self.SYSTEM, prompt=self._extract_prompt(b)) for b in batches)
        cost = None
        if price := self.llm.price():
            price_in, price_out = price
            out = EST_OUTPUT_TOKENS_PER_BATCH * len(batches) + EST_MERGE_OUTPUT_TOKENS
            merge_in = EST_OUTPUT_TOKENS_PER_BATCH * len(batches) + len(self.brand) // 4
            cost = ((tokens + merge_in) * price_in + out * price_out) / 1_000_000
        return Plan(batches=batches, chats=len(chats), input_tokens=tokens, est_cost_usd=cost)

    def _extract_prompt(self, transcripts: str) -> str:
        return EXTRACT_PROMPT.format(brand=self.brand or "(empty)", transcripts=transcripts)

    def extract(self, transcripts: str) -> BatchFindings:
        return self.llm.parse(system=self.SYSTEM, prompt=self._extract_prompt(transcripts), schema=BatchFindings)

    def merge(self, findings: list[BatchFindings], chats: int) -> str:
        prompt = MERGE_PROMPT.format(
            chats=chats, brand=self.brand or "(empty)",
            findings=json.dumps([f.model_dump() for f in findings], indent=1, ensure_ascii=False))
        text = self.llm.text(system=self.SYSTEM, messages=[{"role": "user", "content": prompt}], long=True)
        if not text.startswith("#"):
            raise RuntimeError("Unexpected output: brand profile should start with a Markdown heading")
        return text + "\n"

    def build(self, plan: Plan, *, log: Callable[[str], None] = print) -> str:
        findings = []
        for i, batch in enumerate(plan.batches, 1):
            log(f"  analysing batch {i}/{len(plan.batches)}")
            findings.append(self.extract(batch))
        log("  merging findings into a brand profile")
        return self.merge(findings, plan.chats)
