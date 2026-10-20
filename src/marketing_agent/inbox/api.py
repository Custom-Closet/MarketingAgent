"""HTTP API and static page for the inbox. Routes only translate HTTP <-> InboxService."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..insights.service import InsightsService
from ..models import ModelManager
from .faq import AUTO_SEND_AFTER_APPROVALS
from .service import AUTO_SEND_DELAY, InboxService, QueueFilters, SendNotConfirmed

STATIC = Path(__file__).parent / "static"


class Ids(BaseModel):
    ids: list[int]


class SendRequest(BaseModel):
    ids: list[int]
    confirm: bool = False


class EditRequest(BaseModel):
    body: str


class ApplyFaqRequest(BaseModel):
    ids: list[int]
    faq_key: str


class FaqUpdate(BaseModel):
    answer: str | None = None
    keywords: list[str] | None = None
    approved: bool | None = None


class StageUpdate(BaseModel):
    stage: str | None = None          # None = back to the inferred stage


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class StrategyRequest(BaseModel):
    question: str
    history: list[Turn] = []
    days: int = Field(90, ge=7, le=365)
    confirm: bool = False


class ModelChoice(BaseModel):
    model_id: str


class SettingsUpdate(BaseModel):
    context_window_days: int | None = Field(None, ge=1, le=60)
    lost_after_days: int | None = Field(None, ge=3, le=120)
    auto_send_faq: bool | None = None


def _filters(priority: str | None, category: str | None, customer_type: str | None, status: str | None,
             max_hours_left: float | None, q: str | None) -> QueueFilters:
    return QueueFilters(priority=set(priority.split(",")) if priority else None, category=category or None,
                        customer_type=customer_type or None, status=status or None,
                        max_hours_left=max_hours_left, search=q or None)


def create_app(service: InboxService, insights: InsightsService | None = None,
               models: ModelManager | None = None) -> FastAPI:
    app = FastAPI(title="Custom Closet", docs_url="/api/docs")
    insights = insights or InsightsService(service.repo.db, our_id=service.our_id, lock=service.lock)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\""))
        except (ValueError, SendNotConfirmed) as exc:
            raise HTTPException(400, str(exc))
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/queue")
    def queue(priority: str | None = None, category: str | None = None, customer_type: str | None = None,
              status: str | None = None, max_hours_left: float | None = Query(None, ge=0), q: str | None = None):
        items = service.queue(_filters(priority, category, customer_type, status, max_hours_left, q))
        return [i.to_dict() for i in items]

    @app.get("/api/stats")
    def stats():
        return service.stats()

    @app.post("/api/sync")
    def sync():
        if service.ig is None:
            raise HTTPException(503, "Instagram is not configured")
        return {"started": service.start_background_sync()}

    @app.post("/api/drafts/approve")
    def approve(req: Ids):
        return {"refused": guard(service.approve, req.ids)}

    @app.post("/api/drafts/approve-faq")
    def approve_faq_replies(priority: str | None = None, category: str | None = None,
                            customer_type: str | None = None, max_hours_left: float | None = None,
                            q: str | None = None):
        ids = service.approve_faq_replies(_filters(priority, category, customer_type, None, max_hours_left, q))
        return {"approved": ids}

    @app.post("/api/drafts/hold")
    def hold(req: Ids):
        guard(service.hold, req.ids)
        return {"ok": True}

    @app.post("/api/drafts/reject")
    def reject(req: Ids):
        guard(service.reject, req.ids)
        return {"ok": True}

    @app.post("/api/drafts/apply-faq")
    def apply_faq(req: ApplyFaqRequest):
        return {"updated": guard(service.apply_faq, req.ids, req.faq_key)}

    @app.post("/api/drafts/send")
    def send(req: SendRequest):
        result = guard(service.send, req.ids, confirm=req.confirm)
        return {"sent": result.sent, "blocked": result.blocked, "failed": result.failed}

    @app.post("/api/drafts/{draft_id}")
    def edit(draft_id: int, req: EditRequest):
        guard(service.edit, draft_id, req.body)
        return {"ok": True}

    @app.post("/api/drafts/{draft_id}/ai")
    def ai_draft(draft_id: int):
        return {"body": guard(service.ai_draft, draft_id)}

    @app.get("/api/conversations/{conversation_id}")
    def transcript(conversation_id: str):
        return service.transcript(conversation_id)

    @app.get("/api/faqs")
    def faqs():
        return [{"key": f.key, "question": f.question, "answer": f.answer, "keywords": list(f.keywords),
                 "approved": f.approved, "approvals": f.approvals, "trusted": f.trusted} for f in service.faqs()]

    @app.post("/api/faqs/{key}")
    def update_faq(key: str, req: FaqUpdate):
        if req.answer is not None:
            guard(service.edit_faq, key, req.answer, req.keywords)
        if req.approved is not None:
            guard(service.approve_faq, key, req.approved)
        return {"ok": True}

    # ---- sales ------------------------------------------------------------------------

    @app.get("/api/funnel")
    def funnel(days: int = Query(90, ge=7, le=365), segment: str | None = None, source: str | None = None):
        return insights.funnel(days=days, segment=segment or None, source=source or None)

    @app.get("/api/funnel/leads")
    def funnel_leads(days: int = Query(90, ge=7, le=365), stage: str | None = None, reason: str | None = None,
                     segment: str | None = None, source: str | None = None, q: str | None = None):
        return insights.leads(days=days, stage=stage or None, reason=reason or None, segment=segment or None,
                              source=source or None, search=q or None)

    @app.post("/api/funnel/{conversation_id}/stage")
    def set_stage(conversation_id: str, req: StageUpdate):
        guard(insights.set_stage, conversation_id, req.stage)
        return {"ok": True}

    @app.post("/api/strategy/estimate")
    def strategy_estimate(req: StrategyRequest):
        return guard(insights.strategy_estimate, req.question, [t.model_dump() for t in req.history], days=req.days)

    @app.post("/api/strategy")
    def strategy(req: StrategyRequest):
        answer = guard(insights.strategy, req.question, [t.model_dump() for t in req.history],
                       confirm=req.confirm, days=req.days)
        return {"answer": answer}

    # ---- marketing ----------------------------------------------------------------------

    @app.get("/api/marketing")
    def marketing(days: int = Query(90, ge=7, le=365)):
        return insights.marketing(days=days)

    # ---- settings -------------------------------------------------------------------------

    @app.get("/api/settings")
    def settings():
        db = service.repo.db
        counts = db.conn.execute("SELECT (SELECT COUNT(*) FROM ig_conversations), (SELECT COUNT(*) FROM ig_messages), "
                                 "(SELECT MAX(created_time) FROM ig_messages), (SELECT COUNT(*) FROM ig_summaries)").fetchone()
        return {
            "context_window_days": service.window_days(),
            "lost_after_days": insights.lost_after_days(),
            "auto_send_faq": service.auto_send_enabled(),
            "auto_send": {"delay_seconds": int(AUTO_SEND_DELAY.total_seconds()),
                          "after_approvals": AUTO_SEND_AFTER_APPROVALS},
            "status": {"instagram": service.ig is not None, "claude_drafts": service.writer is not None,
                       "strategist": insights.strategist is not None, "sync": dict(service.sync_state)},
            "archive": {"conversations": counts[0], "messages": counts[1], "newest_message": counts[2],
                        "summaries": counts[3]},
        }

    # ---- model (Bedrock) ---------------------------------------------------------------

    @app.get("/api/models")
    def list_models(refresh: bool = False):
        if models is None:
            return {"provider": None, "current": None, "models": []}
        try:
            available = models.available(refresh=refresh)
            error = None
        except Exception as exc:   # e.g. AWS credentials file unreadable
            available, error = [], str(exc)[:300]
        return {"provider": models.provider, "current": models.current(), "models": available, "error": error}

    @app.post("/api/models/test")
    def test_model(req: ModelChoice):
        if models is None:
            raise HTTPException(503, "Model switching isn't available")
        return models.test(req.model_id)

    @app.post("/api/models/select")
    def select_model(req: ModelChoice):
        if models is None:
            raise HTTPException(503, "Model switching isn't available")
        return guard(models.select, req.model_id)

    @app.post("/api/settings")
    def update_settings(req: SettingsUpdate):
        with service.lock:
            if req.context_window_days is not None:
                service.repo.db.set_setting("context_window_days", str(req.context_window_days))
            if req.lost_after_days is not None:
                service.repo.db.set_setting("lost_after_days", str(req.lost_after_days))
            if req.auto_send_faq is not None:
                service.set_auto_send(req.auto_send_faq)
        return {"ok": True}

    return app
