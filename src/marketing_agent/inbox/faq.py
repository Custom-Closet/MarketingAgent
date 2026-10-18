"""FAQ answers and a keyword matcher for incoming DMs (no API calls).

FAQs are seeded from a JSON file (faqs.json, gitignored - see faqs.example.json) into the
database, where the inbox can edit and approve them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FAQ:
    key: str
    question: str
    answer: str
    keywords: tuple[str, ...]
    approved: bool = False


def load_faq_file(path: Path) -> list[FAQ]:
    """Read FAQs from JSON: [{"key", "question", "answer", "keywords": [...]}, ...]."""
    if not path.exists():
        return []
    items = json.loads(path.read_text())
    return [FAQ(key=i["key"], question=i["question"], answer=i["answer"],
                keywords=tuple(k.lower() for k in i["keywords"])) for i in items]


def _normalise(text: str) -> str:
    return " " + re.sub(r"[^\w\s']", " ", text.lower()).replace("'", "") + " "


def match_faq(text: str, faqs: list[FAQ]) -> FAQ | None:
    """The FAQ whose keywords best match `text` (most keyword hits, then longest keyword), or None.

    Keywords match on word boundaries, so "moq" matches "what is the MOQ?" but not "smoqy".
    """
    haystack = _normalise(text)
    best, best_score = None, (0, 0)
    for faq in faqs:
        hits = [k for k in faq.keywords if f" {_normalise(k).strip()} " in haystack]
        score = (len(hits), max((len(k) for k in hits), default=0))
        if score > best_score:
            best, best_score = faq, score
    return best
