"""HTTP layer: routes map to the service and keep the send confirmation mandatory."""

import pytest
from conftest import seed_chat
from fastapi.testclient import TestClient

from marketing_agent.inbox.api import create_app


@pytest.fixture
def client(inbox):
    db, repo, service, ig = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "Hi, where are you based?", 1)])
    seed_chat(db, "c2", "2", "buyer", [("them", "price for 20 pieces for my brand?", 2)])
    service.refresh_drafts()
    return TestClient(create_app(service)), inbox


def test_index_page_is_served(client):
    http, _ = client
    res = http.get("/")
    assert res.status_code == 200 and "Custom Closet" in res.text
    for asset in ("app.css", "common.js", "chat.js", "sales.js", "marketing.js", "settings.js"):
        assert http.get(f"/static/{asset}").status_code == 200


def test_queue_and_filters(client):
    http, _ = client
    items = http.get("/api/queue").json()
    assert {i["username"] for i in items} == {"fan", "buyer"}
    assert [i["username"] for i in http.get("/api/queue", params={"category": "location"}).json()] == ["fan"]
    assert [i["username"] for i in http.get("/api/queue", params={"q": "pieces"}).json()] == ["buyer"]
    assert http.get("/api/queue", params={"max_hours_left": -1}).status_code == 422


def test_faq_one_click_approve_then_bulk_approve_and_send(client):
    http, (_, repo, _, ig) = client
    assert http.post("/api/faqs/location", json={"approved": True}).status_code == 200
    assert next(f for f in http.get("/api/faqs").json() if f["key"] == "location")["approved"]
    approved = http.post("/api/drafts/approve-faq").json()["approved"]
    assert len(approved) == 1

    res = http.post("/api/drafts/send", json={"ids": approved})           # confirm missing
    assert res.status_code == 400 and ig.sent == []
    res = http.post("/api/drafts/send", json={"ids": approved, "confirm": True}).json()
    assert res["sent"] == approved and ig.sent[0][0] == "1"


def test_edit_approve_reject_and_apply_faq(client):
    http, (_, repo, _, _) = client
    buyer = next(i for i in http.get("/api/queue").json() if i["username"] == "buyer")
    assert http.post(f"/api/drafts/{buyer['draft_id']}", json={"body": "Please share the reference image!"}).status_code == 200
    assert http.post("/api/drafts/approve", json={"ids": [buyer["draft_id"]]}).json() == {"refused": {}}
    assert repo.draft(buyer["draft_id"])["status"] == "approved"
    assert http.post("/api/drafts/apply-faq", json={"ids": [buyer["draft_id"]], "faq_key": "pricing"}).json() == {"updated": 1}
    assert repo.draft(buyer["draft_id"])["status"] == "pending"
    assert http.post("/api/drafts/reject", json={"ids": [buyer["draft_id"]]}).status_code == 200
    assert http.post("/api/drafts/approve", json={"ids": [buyer["draft_id"]]}).status_code == 404


def test_errors_map_to_http_codes(client):
    http, _ = client
    assert http.post("/api/drafts/999", json={"body": "x"}).status_code == 404
    assert http.post("/api/faqs/nope", json={"approved": True}).status_code == 404
    assert http.post("/api/faqs/location", json={"answer": " "}).status_code == 400
    draft_id = http.get("/api/queue").json()[0]["draft_id"]
    assert http.post(f"/api/drafts/{draft_id}/ai").status_code == 503          # Claude not configured


def test_transcript_and_stats(client):
    http, _ = client
    assert http.get("/api/conversations/c1").json()[0] == {
        "from": "them", "text": "Hi, where are you based?", "attachment": False,
        "at": http.get("/api/conversations/c1").json()[0]["at"]}
    stats = http.get("/api/stats").json()
    assert stats["open"] == 2 and stats["sync"]["running"] is False


def test_sales_marketing_and_settings_routes(client):
    http, (db, *_rest) = client
    funnel = http.get("/api/funnel", params={"days": 30}).json()
    assert funnel["total"] == 2 and funnel["steps"][0]["stage"] == "new"
    leads = http.get("/api/funnel/leads").json()
    assert {l["username"] for l in leads} == {"fan", "buyer"}
    assert http.post("/api/funnel/c1/stage", json={"stage": "quoted"}).status_code == 200
    assert [l["username"] for l in http.get("/api/funnel/leads", params={"stage": "quoted"}).json()] == ["fan"]
    assert http.post("/api/funnel/c1/stage", json={"stage": "bogus"}).status_code == 400
    assert http.get("/api/funnel", params={"days": 1}).status_code == 422
    assert http.get("/api/marketing").json()["sent"] == 0

    assert http.post("/api/settings", json={"context_window_days": 10}).status_code == 200
    settings = http.get("/api/settings").json()
    assert settings["context_window_days"] == 10 and settings["archive"]["conversations"] == 2
    assert http.post("/api/settings", json={"context_window_days": 0}).status_code == 422
    assert http.post("/api/strategy/estimate", json={"question": "leaks?"}).status_code == 503   # no Claude
