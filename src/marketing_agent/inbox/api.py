"""HTTP API and static page for the inbox. Routes only translate HTTP <-> InboxService."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .service import InboxService, QueueFilters, SendNotConfirmed

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


def _filters(priority: str | None, category: str | None, customer_type: str | None, status: str | None,
             max_hours_left: float | None, q: str | None) -> QueueFilters:
    return QueueFilters(priority=set(priority.split(",")) if priority else None, category=category or None,
                        customer_type=customer_type or None, status=status or None,
                        max_hours_left=max_hours_left, search=q or None)


def create_app(service: InboxService) -> FastAPI:
    app = FastAPI(title="Custom Closet inbox", docs_url="/api/docs")

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
                 "approved": f.approved} for f in service.faqs()]

    @app.post("/api/faqs/{key}")
    def update_faq(key: str, req: FaqUpdate):
        if req.answer is not None:
            guard(service.edit_faq, key, req.answer, req.keywords)
        if req.approved is not None:
            guard(service.approve_faq, key, req.approved)
        return {"ok": True}

    return app
