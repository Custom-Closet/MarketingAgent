"""Draft personalized outreach with Claude, grounded in the brand profile (brand.md)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

SYSTEM_PROMPT = """\
You write outreach on behalf of the brand described below. Every message goes to a real \
person, and a human on the brand's team reviews each draft before it is sent.

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


def lead_context(lead: sqlite3.Row) -> str:
    keys = ("name", "position", "company", "domain", "website", "ig_username", "ig_bio", "ig_followers", "notes")
    return json.dumps({k: lead[k] for k in keys if lead[k]}, indent=2)


class Personalizer:
    def __init__(self, brand_file: Path, *, model: str, effort: str, client: anthropic.Anthropic | None = None):
        if not brand_file.exists():
            raise FileNotFoundError(f"{brand_file} not found. Run `agent init` and describe your brand there.")
        self.system = SYSTEM_PROMPT.format(brand=brand_file.read_text().strip())
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    def _parse(self, prompt: str, output_format: type[BaseModel]) -> BaseModel:
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            # Brand system prompt is identical across every lead, so cache it.
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.effort},
            output_format=output_format,
            # Re-run on a fallback model if a safety classifier declines the request.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to draft this message")
        if response.parsed_output is None:
            raise RuntimeError(f"Could not parse draft (stop_reason={response.stop_reason})")
        return response.parsed_output

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

    def draft_dm_reply(self, username: str | None, inbound_text: str, *, history: str = "") -> DMDraft:
        context = (f"<conversation_so_far>\n{history}\n</conversation_so_far>\n\n"
                   "Don't repeat questions the conversation already answered.\n\n") if history else ""
        prompt = (
            f"@{username or 'someone'} messaged the brand on Instagram. Write a helpful reply that "
            "answers them and, if natural, moves toward a sale or next step.\n\n"
            f"{context}<their_message>\n{inbound_text}\n</their_message>"
        )
        return self._parse(prompt, DMDraft)
