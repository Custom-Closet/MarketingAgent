"""Draft context: recent window verbatim, older messages folded into an incremental summary."""

from conftest import US, seed_chat

from marketing_agent.context import MIN_RECENT, build_context
from marketing_agent.db import Database


class FakeSummarizer:
    def __init__(self):
        self.calls = []

    def __call__(self, previous, transcript):
        self.calls.append((previous, transcript))
        return f"{previous} | {transcript.count(chr(10)) + 1} msgs".strip(" |")


def _chat(db):
    # 6 old messages (10-20 days ago) and 6 recent ones (within 2 days)
    old = [("them" if i % 2 == 0 else "us", f"old {i}", 24 * (20 - i)) for i in range(6)]
    recent = [("them" if i % 2 == 0 else "us", f"new {i}", 48 - 8 * i) for i in range(6)]
    seed_chat(db, "c1", "1", "fan", old + recent)


def test_recent_window_verbatim_and_older_messages_summarised_once():
    db = Database(":memory:")
    _chat(db)
    summarize = FakeSummarizer()
    ctx = build_context(db, "c1", US, summarizer=summarize, window_days=7)
    assert ctx.recent.count("\n") + 1 == 6 and "new 0" in ctx.recent and "old" not in ctx.recent
    assert ctx.summary == "6 msgs" and ctx.summarized_count == 6
    assert "them: old 0" in summarize.calls[0][1]

    again = build_context(db, "c1", US, summarizer=summarize, window_days=7)
    assert len(summarize.calls) == 1 and again.summary == "6 msgs"        # nothing new aged out


def test_only_newly_aged_messages_are_folded_in():
    db = Database(":memory:")
    _chat(db)
    summarize = FakeSummarizer()
    build_context(db, "c1", US, summarizer=summarize, window_days=7)
    # 1-day window holds only 2 messages, so MIN_RECENT keeps 5 recent and just "new 0" ages out
    ctx = build_context(db, "c1", US, summarizer=summarize, window_days=1)
    previous, transcript = summarize.calls[1]
    assert previous == "6 msgs" and transcript.endswith("them: new 0") and transcript.count("\n") == 0
    assert ctx.summary == "6 msgs | 1 msgs" and ctx.recent_count == MIN_RECENT


def test_minimum_recent_messages_and_no_summarizer():
    db = Database(":memory:")
    seed_chat(db, "c1", "1", "fan", [("them", f"m{i}", 24 * (30 - i)) for i in range(8)])   # all older than 7 days
    ctx = build_context(db, "c1", US, window_days=7)
    assert ctx.recent_count == MIN_RECENT and ctx.summary == "" and db.ig_summary("c1") is None
