"""Sales and marketing views: funnel, leads by stage, stage overrides, outreach results, strategist."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..context import format_messages
from ..db import Database
from .analysis import FUNNEL, STAGES, Analysis, Lead, analyze, funnel_report, lost_examples, outreach_report
from .strategist import Strategist

DEFAULT_LOST_AFTER_DAYS = 14
DEFAULT_DAYS = 90


class InsightsService:
    def __init__(self, db: Database, *, our_id: str, lock: threading.RLock | None = None,
                 strategist: Strategist | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.db = db
        self.our_id = our_id
        self.lock = lock or threading.RLock()
        self.strategist = strategist
        self.clock = clock

    def lost_after_days(self) -> int:
        return int(self.db.setting("lost_after_days", str(DEFAULT_LOST_AFTER_DAYS)))

    def _analysis(self, days: int) -> Analysis:
        now = self.clock()
        with self.lock:
            rows = self.db.ig_messages_since(now - timedelta(days=days))
            overrides = self.db.funnel_overrides()
            prospects = self.db.prospect_usernames()
        analysis = analyze(rows, self.our_id, now=now, lost_after_days=self.lost_after_days(), overrides=overrides)
        # Conversations with brands the brand finder found count as their own lead source.
        for lead in analysis.leads:
            if (lead.username or "").lower() in prospects:
                lead.source = "brand_finder"
        return analysis

    @staticmethod
    def _filter(leads: list[Lead], segment: str | None, source: str | None) -> list[Lead]:
        return [l for l in leads if (not segment or l.segment == segment) and (not source or l.source == source)]

    def funnel(self, *, days: int = DEFAULT_DAYS, segment: str | None = None, source: str | None = None) -> dict:
        leads = self._filter(self._analysis(days).leads, segment, source)
        return {"days": days, "lost_after_days": self.lost_after_days(), **funnel_report(leads)}

    def leads(self, *, days: int = DEFAULT_DAYS, stage: str | None = None, reason: str | None = None,
              segment: str | None = None, source: str | None = None, search: str | None = None,
              limit: int = 200) -> list[dict]:
        leads = self._filter(self._analysis(days).leads, segment, source)
        if stage:
            leads = [l for l in leads if l.stage == stage]
        if reason:
            leads = [l for l in leads if l.reason == reason]
        if search:
            needle = search.lower()
            leads = [l for l in leads if needle in (l.username or "").lower() or needle in l.last_text.lower()]
        leads.sort(key=lambda l: l.last_at, reverse=True)
        return [l.to_dict() for l in leads[:limit]]

    def set_stage(self, conversation_id: str, stage: str | None) -> None:
        """Manual stage (or None to go back to the inferred one)."""
        if stage is not None and stage not in STAGES:
            raise ValueError(f"stage must be one of {', '.join(STAGES)}")
        with self.lock:
            if not self.db.ig_conversation(conversation_id):
                raise KeyError(f"unknown conversation {conversation_id}")
            self.db.set_funnel_override(conversation_id, stage)

    # ---- prospecting -> sales ---------------------------------------------------------

    def prospect_conversations(self, *, days: int = 365) -> dict[str, dict]:
        """Prospect username -> its conversation's funnel stage, for prospects who replied to us."""
        return {l.username.lower(): {"conversation_id": l.conversation_id, "stage": l.stage, "reached": l.reached}
                for l in self._analysis(days).leads if l.source == "brand_finder" and l.username}

    def prospect_funnel(self, *, days: int = 365) -> dict:
        """Found -> Shortlisted -> Contacted -> Replied -> ... -> Won, for brands the brand finder found."""
        with self.lock:
            stages = self.db.prospect_usernames()
            with_chat = {r[0] for r in self.db.conn.execute(
                "SELECT lower(username) FROM ig_conversations WHERE username IS NOT NULL").fetchall()}
        convos = self.prospect_conversations(days=days)
        contacted = {u for u, s in stages.items() if s == "contacted" or u in with_chat}
        counts = [
            ("found", "Found", len(stages)),
            ("shortlisted", "Shortlisted", len({u for u, s in stages.items() if s == "shortlisted"} | contacted)),
            ("contacted", "Contacted", len(contacted)),
            ("replied", "Replied", len(convos)),
        ]
        for stage in FUNNEL[2:]:                       # qualified, quoted, order, won
            reached = sum(FUNNEL.index(c["reached"]) >= FUNNEL.index(stage) or c["stage"] == stage
                          for c in convos.values())
            counts.append((stage, {"qualified": "Qualified", "quoted": "Quoted", "order": "Sample / Order",
                                   "won": "Won"}[stage], reached))
        steps, prev = [], None
        for key, label, n in counts:
            steps.append({"stage": key, "label": label, "reached": n,
                          "conversion": round(100 * n / prev, 1) if prev else None})
            prev = n
        return {"steps": steps, "dismissed": sum(s == "dismissed" for s in stages.values()),
                "lost": sum(c["stage"] == "lost" for c in convos.values())}

    def marketing(self, *, days: int = DEFAULT_DAYS) -> dict:
        analysis = self._analysis(days)
        return {"days": days, **outreach_report(analysis.outreach, analysis.leads)}

    # ---- strategist -----------------------------------------------------------------

    def _briefing(self, days: int) -> str:
        analysis = self._analysis(days)
        examples = []
        with self.lock:
            for lead in lost_examples(analysis.leads):
                summary = self.db.ig_summary(lead.conversation_id)
                transcript = summary["summary"] if summary else format_messages(
                    self.db.ig_transcript(lead.conversation_id, limit=14), self.our_id)
                examples.append({"reason": lead.reason, "reached": lead.reached, "transcript": transcript})
        return Strategist.briefing(funnel_report(analysis.leads), outreach_report(analysis.outreach, analysis.leads),
                                   examples)

    def _require_strategist(self) -> Strategist:
        if self.strategist is None:
            raise RuntimeError("Claude is not configured (ANTHROPIC_API_KEY)")
        return self.strategist

    def strategy_estimate(self, question: str, history: list[dict], *, days: int = DEFAULT_DAYS) -> dict:
        est = self._require_strategist().estimate(self._briefing(days), question, history)
        return {"input_tokens": est.input_tokens, "output_tokens": est.output_tokens, "usd": est.usd}

    def strategy(self, question: str, history: list[dict], *, confirm: bool, days: int = DEFAULT_DAYS) -> str:
        """Ask the strategist. `confirm` must be True: the person saw the cost estimate."""
        if not confirm:
            raise ValueError("confirm the estimated cost first (confirm=true)")
        if not question.strip():
            raise ValueError("ask a question")
        return self._require_strategist().advise(self._briefing(days), question.strip(), history)
