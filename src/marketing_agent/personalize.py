"""Draft personalized outreach with Claude, grounded in the brand profile (brand.md)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from .llm import LLM

# A one-person business: every message is the founder talking. Applies whatever brand.md says.
FOUNDER_VOICE = """\
This is a one-person business: the founder runs it and personally writes and reviews every \
message. Write as the founder would:
- Simple and humble. Short, plain sentences; no sales language, superlatives or hype \
("best quality", "leading", "premium", "world-class").
- Only the truth. Use facts, prices and policies from the brand profile; never invent them. \
If the answer isn't there, say so plainly ("Let me check and get back to you") instead of guessing.
- Never imply a team, departments, a factory or a scale the profile doesn't describe.
- It's fine to say what we can't do, and to offer the nearest thing we can."""

SYSTEM_PROMPT = """\
You write messages on behalf of the business described below. Every message goes to a real \
person, and the founder reviews each draft before it is sent.

{founder_voice}

Write like a thoughtful person, not a marketing template:
- Open with something specific to this lead (their company, role, bio, or content). Never \
invent facts about them; if you know little, keep the personal touch modest and honest.
- One clear, low-friction ask (a short reply, a quick call, a sample). No pressure tactics, \
fake urgency, or misleading subject lines.
- Plain text. No emojis in email. Do not add a signature block, unsubscribe line, or \
physical address - those are appended automatically.
- Emails: subject under 8 words, body 60-120 words. Instagram DMs: 2-4 short sentences, \
conversational, at most one emoji.
- Follow-ups: shorter than the original, add a new angle or bit of value, never guilt-trip.

<brand>
{brand}
</brand>
"""


class EmailDraft(BaseModel):
    subject: str = Field(description="Email subject line")
    body: str = Field(description="Email body, plain text, starting with a greeting")


class DMDraft(BaseModel):
    message: str = Field(description="Instagram direct message text")


class ConversationSummary(BaseModel):
    summary: str = Field(description="Updated running summary, at most ~120 words, as short '- ' bullet lines")


SUMMARY_PROMPT = """\
Update the running summary of this Instagram conversation between the brand ("us") and a customer \
("them"), folding in the newer messages below. Keep only what matters for replying later:
- who they are (clothing brand or personal use, city/country if mentioned)
- what they want (products, quantity, sizes, deadline, budget) and what they've sent (reference images, fabric)
- what we told them (prices quoted with numbers, policies, timelines) and any objections
- current status and the open next step
Use at most ~120 words of short '- ' bullets. Leave out phone numbers, emails and street addresses."""


def lead_context(lead: sqlite3.Row) -> str:
    keys = ("name", "position", "company", "domain", "website", "ig_username", "ig_bio", "ig_followers", "notes")
    return json.dumps({k: lead[k] for k in keys if lead[k]}, indent=2)


class Personalizer:
    def __init__(self, brand_file: Path, *, llm: LLM):
        if not brand_file.exists():
            raise FileNotFoundError(f"{brand_file} not found. Run `agent init` and describe your brand there.")
        self.system = SYSTEM_PROMPT.format(founder_voice=FOUNDER_VOICE, brand=brand_file.read_text().strip())
        self.llm = llm

    def _parse(self, prompt: str, output_format: type[BaseModel]) -> BaseModel:
        return self.llm.parse(system=self.system, prompt=prompt, schema=output_format)

    def draft_email(self, lead: sqlite3.Row) -> EmailDraft:
        prompt = f"Write a first cold email to this lead.\n\n<lead>\n{lead_context(lead)}\n</lead>"
        return self._parse(prompt, EmailDraft)

    def draft_followup(self, lead: sqlite3.Row, history: list[sqlite3.Row]) -> EmailDraft:
        previous = "\n\n".join(f"Subject: {m['subject']}\n{m['body']}" for m in history)
        prompt = (
            f"The lead has not replied. Write follow-up #{len(history)} in the same thread "
            "(reuse the subject with 'Re: ' prefix).\n\n"
            f"<lead>\n{lead_context(lead)}\n</lead>\n\n<previous_emails>\n{previous}\n</previous_emails>"
        )
        return self._parse(prompt, EmailDraft)

    def draft_dm(self, lead: sqlite3.Row) -> DMDraft:
        prompt = f"Write a first Instagram DM to this account.\n\n<lead>\n{lead_context(lead)}\n</lead>"
        return self._parse(prompt, DMDraft)

    def summarize_conversation(self, previous_summary: str, transcript: str) -> str:
        prompt = (f"{SUMMARY_PROMPT}\n\n<previous_summary>\n{previous_summary or '(none yet)'}\n</previous_summary>"
                  f"\n\n<newer_messages>\n{transcript}\n</newer_messages>")
        return self._parse(prompt, ConversationSummary).summary

    def draft_dm_reply(self, username: str | None, inbound_text: str, *, history: str = "",
                       summary: str = "") -> DMDraft:
        earlier = f"<earlier_in_this_chat>\n{summary}\n</earlier_in_this_chat>\n\n" if summary else ""
        context = (f"{earlier}<recent_messages>\n{history}\n</recent_messages>\n\n"
                   "Don't repeat questions the conversation already answered.\n\n") if history or summary else ""
        prompt = (
            f"@{username or 'someone'} messaged the brand on Instagram. Write a helpful reply that "
            "answers them and, if natural, moves toward a sale or next step.\n\n"
            f"{context}<their_message>\n{inbound_text}\n</their_message>"
        )
        return self._parse(prompt, DMDraft)
