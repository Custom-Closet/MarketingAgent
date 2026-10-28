"""Edit and tune brand.md from Settings: the profile the model reads before every draft.

brand.md stays the source of truth (the CLI and Lambda read it); every save is also kept as a
version in the database so it can be restored. Tuning with the model proposes a revised profile;
it is only saved when the founder applies it.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable

from .db import Database
from .llm import LLM

MAX_CHARS = 60_000                 # sent with every draft; keep it a profile, not a manual
TEMPLATE_MARKER = "<Brand name, what you sell"
EST_OUTPUT_TOKENS = 6_000

TUNE_SYSTEM = """\
You help a solo founder edit their brand profile. The profile is the system prompt an AI reads \
before drafting every Instagram DM and email reply; the founder reviews each draft.

The business is run by one person, the founder. Keep the profile true to that:
- The voice is the founder's own: simple, humble, warm and honest. No hype or superlatives.
- Only facts the founder gave or the profile already states. Never add prices, policies, \
customers, numbers or claims. If something is unclear, put it under "## Needs confirmation" \
instead of deciding it.
- Don't describe a team, departments, a factory or a scale that isn't there.
- Keep it concise and well organised: it is sent with every draft, so every line should help \
write a better reply. Prefer short bullets.

When the founder asks for a change, answer in exactly this format:
<reply>One short paragraph: what you changed and why, or your answer if they only asked a question.</reply>
<changes>
- one line per change (leave empty if nothing changed)
</changes>
<profile>
The complete revised profile in Markdown - omit this block entirely if nothing should change.
</profile>"""

_BLOCK = {name: re.compile(rf"<{name}>\s*(.*?)\s*</{name}>", re.S) for name in ("reply", "changes", "profile")}


def parse_tune_reply(text: str) -> dict:
    found = {name: (m.group(1).strip() if (m := rx.search(text)) else "") for name, rx in _BLOCK.items()}
    profile = found["profile"]
    if not profile and "<profile>" in text:
        raise ValueError("The model's reply was cut off before the end of the profile. Try again, "
                         "or ask for one smaller change at a time.")
    changes = [line.lstrip("-•* ").strip() for line in found["changes"].splitlines() if line.strip("-•* ").strip()]
    reply = found["reply"] or (text.strip() if not any(found.values()) else "")
    return {"reply": reply, "changes": changes if profile else [], "profile": profile or None}


class BrandProfileService:
    def __init__(self, db: Database, path: Path, *, lock: threading.RLock | None = None,
                 llm_getter: Callable[[], LLM | None] = lambda: None, on_change: Callable[[], None] = lambda: None):
        """`on_change` runs after a save so the drafting model and strategist pick up the new profile."""
        self.db, self.path = db, path
        self.lock = lock or threading.RLock()
        self.llm_getter, self.on_change = llm_getter, on_change

    @property
    def draft_path(self) -> Path:
        return self.path.with_name("brand.draft.md")

    def content(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def state(self) -> dict:
        text = self.content()
        with self.lock:
            versions = [dict(r) for r in self.db.brand_versions()]
        return {"content": text, "path": str(self.path), "exists": self.path.exists(),
                "is_template": not text.strip() or TEMPLATE_MARKER in text,
                "est_tokens": len(text) // 4, "draft_available": self.draft_path.exists(), "versions": versions}

    def draft(self) -> str:
        if not self.draft_path.exists():
            raise KeyError("no brand.draft.md - run `agent build-brand` to make one from your chats")
        return self.draft_path.read_text(encoding="utf-8")

    def save(self, content: str, *, note: str = "", created_by: str = "user") -> int:
        content = content.replace("\r\n", "\n").strip() + "\n"
        if len(content.strip()) < 20:
            raise ValueError("the profile is empty")
        if len(content) > MAX_CHARS:
            raise ValueError(f"the profile is {len(content):,} characters; keep it under {MAX_CHARS:,} "
                             "(it is sent with every draft)")
        with self.lock:
            previous = self.content()
            if previous and not self.db.brand_versions(1):
                self.db.add_brand_version(previous, note="brand.md before the first edit here", created_by="file")
            tmp = self.path.with_suffix(".md.tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(self.path)                          # atomic: drafting never reads half a file
            version = self.db.add_brand_version(content, note=note or "edited", created_by=created_by)
        self.on_change()
        return version

    def restore(self, version_id: int) -> int:
        with self.lock:
            row = self.db.brand_version(version_id)
        if row is None:
            raise KeyError(f"unknown version {version_id}")
        return self.save(row["content"], note=f"restored version {version_id}")

    def version(self, version_id: int) -> str:
        with self.lock:
            row = self.db.brand_version(version_id)
        if row is None:
            raise KeyError(f"unknown version {version_id}")
        return row["content"]

    # ---- tuning with the model -------------------------------------------------------

    def _llm(self) -> LLM:
        llm = self.llm_getter()
        if llm is None:
            raise RuntimeError("No model configured - pick one under Model")
        return llm

    @staticmethod
    def _messages(content: str, message: str, history: list[dict]) -> list[dict]:
        first = f"<current_profile>\n{content}\n</current_profile>\n\n"
        turns = [*history, {"role": "user", "content": message}]
        turns[0] = {"role": "user", "content": first + turns[0]["content"]}   # the profile goes first
        return turns

    def tune_estimate(self, content: str, message: str, history: list[dict]) -> dict:
        llm = self._llm()
        chars = len(TUNE_SYSTEM) + sum(len(m["content"]) for m in self._messages(content, message, history))
        tokens, price = chars // 4, llm.price()
        usd = round((tokens * price[0] + EST_OUTPUT_TOKENS * price[1]) / 1_000_000, 3) if price else None
        return {"input_tokens": tokens, "output_tokens": EST_OUTPUT_TOKENS, "usd": usd}

    def tune(self, content: str, message: str, history: list[dict], *, confirm: bool) -> dict:
        """Ask the model to revise `content` (the editor's text, saved or not). Returns a proposal; saves nothing."""
        if not confirm:
            raise ValueError("confirm the estimated cost first (confirm=true)")
        if not message.strip():
            raise ValueError("say what you'd like to change")
        text = self._llm().text(system=TUNE_SYSTEM, messages=self._messages(content, message.strip(), history),
                                long=True)
        return parse_tune_reply(text)
