from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from marketing_agent import pipeline
from marketing_agent.channels.gmail import ThreadStatus, build_email
from marketing_agent.config import load_settings
from marketing_agent.db import Database
from marketing_agent.sources.hunter import HunterClient
from marketing_agent.sources.instagram import (
    IGConversation, InstagramClient, domain_from_url, extract_mentions,
)


@pytest.fixture
def db():
    return Database(":memory:")


@pytest.fixture
def settings(tmp_path):
    return replace(load_settings(), sender_name="Ada", sender_email="ada@brand.co",
                   physical_address="1 Main St, Boston MA", daily_email_cap=3,
                   min_delay_seconds=0, max_delay_seconds=0, db_path=tmp_path / "a.db")


class FakeGmail:
    def __init__(self, statuses=None):
        self.sent = []
        self.statuses = statuses or {}

    def send(self, msg, *, thread_id=None):
        self.sent.append((msg, thread_id))
        return thread_id or f"t{len(self.sent)}", msg["Message-ID"]

    def thread_status(self, thread_id):
        return self.statuses.get(thread_id, ThreadStatus())

    def bounced_recipients(self, newer_than_days=14):
        return []


def approved_email(db, email, **lead):
    lead_id = db.upsert_lead(email=email, source="test", **lead)
    mid = db.add_message(lead_id, "email", "Hi there", subject="Hello")
    db.update_message(mid, status="approved")
    return lead_id, mid


# ---- db ------------------------------------------------------------------

def test_upsert_merges_by_ig_username_and_fills_blanks(db):
    a = db.upsert_lead(ig_username="@Cafe.Luna", source="instagram")
    b = db.upsert_lead(ig_username="cafe.luna", email="Owner@CafeLuna.com", source="hunter")
    assert a == b
    lead = db.get_lead(a)
    assert lead["email"] == "owner@cafeluna.com"
    assert lead["source"] == "instagram"


def test_suppression_cancels_queued_messages(db):
    lead_id, mid = approved_email(db, "x@y.com")
    db.suppress("X@y.com", "manual")
    assert db.get_lead(lead_id)["status"] == "unsubscribed"
    assert db.messages(status="approved") == []
    assert db.leads_without_draft("email", 10) == []


# ---- sources -------------------------------------------------------------

def test_hunter_domain_search_parses_contacts():
    def handler(request: httpx.Request):
        assert request.url.params["api_key"] == "k"
        assert request.url.params["type"] == "personal"
        return httpx.Response(200, json={"data": {"domain": "acme.com", "organization": "Acme", "emails": [
            {"value": "jo@acme.com", "first_name": "Jo", "last_name": "Li", "position": "CMO", "confidence": 94}
        ]}})

    http = httpx.Client(base_url="https://api.hunter.io/v2", transport=httpx.MockTransport(handler))
    [contact] = HunterClient("k", client=http).domain_search("acme.com")
    assert (contact.email, contact.name, contact.organization) == ("jo@acme.com", "Jo Li", "Acme")


def test_instagram_business_discovery():
    def handler(request: httpx.Request):
        assert "business_discovery.username(cafeluna)" in request.url.params["fields"]
        return httpx.Response(200, json={"business_discovery": {
            "username": "cafeluna", "name": "Cafe Luna", "biography": "Oat lattes",
            "website": "https://www.cafeluna.com/menu", "followers_count": 12000, "media_count": 300}})

    http = httpx.Client(base_url="https://graph.facebook.com/v23.0", transport=httpx.MockTransport(handler))
    profile = InstagramClient("tok", "123", client=http).business_discovery("@cafeluna")
    assert profile.domain == "cafeluna.com"
    assert profile.followers_count == 12000


def test_domain_and_mention_helpers():
    assert domain_from_url("linktr.ee/someone") is None
    assert domain_from_url("shop.example.co.uk") == "shop.example.co.uk"
    assert extract_mentions("Shot by @studio.k with @Brand_X. email me@x.com") == {"studio.k", "brand_x"}


def test_ig_reply_window():
    recent = IGConversation("1", "a", "hi", datetime.now(timezone.utc) - timedelta(hours=2))
    stale = IGConversation("2", "b", "hi", datetime.now(timezone.utc) - timedelta(hours=30))
    assert recent.can_reply and not stale.can_reply


# ---- sending -------------------------------------------------------------

def test_email_includes_compliance_and_threading():
    msg = build_email(sender_name="Ada", sender_email="ada@brand.co", to="jo@acme.com", subject="Re: Hello",
                      body="Following up", physical_address="1 Main St", in_reply_to="<abc@brand.co>")
    text = msg.get_content()
    assert "1 Main St" in text and "unsubscribe" in text
    assert msg["List-Unsubscribe"] == "<mailto:ada@brand.co?subject=unsubscribe>"
    assert msg["In-Reply-To"] == "<abc@brand.co>"


def test_send_respects_daily_cap_and_skips_invalid(db, settings):
    for i in range(4):
        approved_email(db, f"p{i}@acme.com", email_status="valid")
    approved_email(db, "bad@acme.com", email_status="invalid")
    gmail = FakeGmail()

    sent = pipeline.send_approved_emails(db, gmail, settings, sleep=lambda s: None, log=lambda m: None)
    assert sent == 3  # cap
    assert pipeline.remaining_daily_quota(db, settings.daily_email_cap) == 0
    assert pipeline.send_approved_emails(db, gmail, settings, sleep=lambda s: None, log=lambda m: None) == 0


def test_send_requires_physical_address(db, settings):
    with pytest.raises(RuntimeError, match="PHYSICAL_ADDRESS"):
        pipeline.send_approved_emails(db, FakeGmail(), replace(settings, physical_address=""), log=print)


def test_sync_marks_replies_and_unsubscribes(db, settings):
    a, _ = approved_email(db, "a@acme.com")
    b, _ = approved_email(db, "b@acme.com")
    gmail = FakeGmail()
    pipeline.send_approved_emails(db, gmail, settings, sleep=lambda s: None, log=lambda m: None)
    gmail.statuses = {"t1": ThreadStatus(replied=True, reply_snippet="Sounds good"),
                      "t2": ThreadStatus(replied=True, unsubscribe=True)}

    stats = pipeline.sync_gmail(db, gmail, log=lambda m: None)
    assert stats == {"replied": 1, "bounced": 0, "unsubscribed": 1}
    assert db.get_lead(a)["status"] == "replied"
    assert db.get_lead(b)["status"] == "unsubscribed"
    assert db.followups_due(0, 2) == []


def test_followups_due_after_wait(db, settings):
    lead_id, mid = approved_email(db, "a@acme.com")
    pipeline.send_approved_emails(db, FakeGmail(), settings, sleep=lambda s: None, log=lambda m: None)
    assert db.followups_due(4, 2) == []
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
    db.update_message(mid, sent_at=old)
    [due] = db.followups_due(4, 2)
    assert due["lead_id"] == lead_id and due["thread_id"] == "t1"


# ---- personalization -----------------------------------------------------

def test_personalizer_request_shape(db, tmp_path):
    import json

    import anthropic
    import httpx2

    from marketing_agent.personalize import Personalizer

    captured = {}

    def handler(request: httpx2.Request):
        captured["body"] = json.loads(request.content)
        captured["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps({"subject": "Quick idea", "body": "Hi Jo, ..."})}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    brand = tmp_path / "brand.md"
    brand.write_text("We sell oat milk to cafes.")
    writer = Personalizer(brand, model="claude-opus-5-5", effort="medium", client=client)

    lead_id = db.upsert_lead(name="Jo Li", email="jo@acme.com", company="Acme", source="test")
    draft = writer.draft_email(db.get_lead(lead_id))

    assert draft.subject == "Quick idea"
    body = captured["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["beta"]
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "oat milk" in body["system"][0]["text"]
    assert "Acme" in body["messages"][0]["content"]


def test_ig_send_reply_routes_by_token_type():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.host, request.url.path))
        return httpx.Response(200, json={"message_id": "m1"})

    ig_login = InstagramClient("IGAAtok", "1784", page_id="555")
    ig_login.http = httpx.Client(base_url=ig_login.http.base_url, transport=httpx.MockTransport(handler))
    fb_page = InstagramClient("EAAtok", "1784", page_id="555")
    fb_page.http = httpx.Client(base_url=fb_page.http.base_url, transport=httpx.MockTransport(handler))
    assert ig_login.send_reply("42", "hi") == fb_page.send_reply("42", "hi") == "m1"
    assert seen == [("graph.instagram.com", "/v23.0/1784/messages"), ("graph.facebook.com", "/v23.0/555/messages")]


def _ig(handler, token="EAAtok"):
    ig = InstagramClient(token, "1784", page_id="555")
    ig.http = httpx.Client(base_url=ig.http.base_url, transport=httpx.MockTransport(handler))
    ig.min_interval, ig.retry_delays = 0, (0, 0)
    return ig


def test_ig_conversations_page_one_at_a_time_and_stop_at_window():
    now = datetime.now(timezone.utc)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S+0000")
    stubs = [{"id": "c1"}, {"id": "c2"}]
    updated = {"c1": fmt(now - timedelta(hours=1)), "c2": fmt(now - timedelta(hours=30))}
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path.endswith("/conversations"):
            assert request.url.params["limit"] == "1"
            i = 1 if request.url.params.get("after") else 0
            return httpx.Response(200, json={"data": [stubs[i]], "paging": {
                "cursors": {"after": "x"}, "next": "https://graph.facebook.com/...&access_token=SECRET"}})
        return httpx.Response(200, json={
            "updated_time": updated[request.url.path.rsplit("/", 1)[1]],
            "participants": {"data": [{"id": "1784", "username": "brand"}, {"id": "42", "username": "fan"}]},
            "messages": {"data": [{"message": "hello", "from": {"id": "42"}, "created_time": fmt(now - timedelta(hours=1))}]}})

    [convo] = _ig(handler).conversations()
    assert (convo.recipient_id, convo.username, convo.last_inbound_text, convo.can_reply) == ("42", "fan", "hello", True)
    assert calls == ["/v23.0/555/conversations", "/v23.0/c1", "/v23.0/555/conversations", "/v23.0/c2"]  # c2: past window, stop


def test_ig_scrubs_tokens_from_paging_links():
    def handler(request):
        return httpx.Response(200, json={"data": [], "paging": {"cursors": {"after": "a"},
                                                                "next": "https://x/?access_token=SECRET"}})
    body = _ig(handler)._request("GET", "/555/conversations")
    assert "SECRET" not in str(body) and body["paging"]["cursors"]["after"] == "a"


def test_ig_stops_when_usage_header_near_limit():
    from marketing_agent.sources.instagram import InstagramRateLimited

    def handler(request):
        usage = '{"555": [{"type": "messenger", "call_count": 85, "total_cputime": 1, "total_time": 1, "estimated_time_to_regain_access": 0}]}'
        return httpx.Response(200, json={"data": []}, headers={"x-business-use-case-usage": usage})
    with pytest.raises(InstagramRateLimited, match="85%"):
        _ig(handler).conversations()

    def throttled(request):
        return httpx.Response(400, json={"error": {"message": "Too many calls", "code": 613}})
    with pytest.raises(InstagramRateLimited):
        _ig(throttled).send_reply("42", "hi")


def test_ig_retries_transient_meta_errors():
    responses = [httpx.Response(500, json={"error": {"message": "Timeout", "code": -2}}),
                 httpx.Response(200, json={"message_id": "m1"})]
    assert _ig(lambda request: responses.pop(0)).send_reply("42", "hi") == "m1"


# ---- Instagram send-time approval gates -----------------------------------

class FakeIG:
    def __init__(self):
        self.sent = []

    def send_reply(self, recipient_id, text):
        self.sent.append((recipient_id, text))
        return "m1"


def _ig_reply(db, *, username="fan", hours_left=5.0, approve=True, body="Thanks for writing!"):
    lead = db.upsert_lead(ig_username=username, source="instagram")
    deadline = (datetime.now(timezone.utc) + timedelta(hours=hours_left)).isoformat(timespec="seconds")
    mid = db.add_message(lead, "instagram", body, kind="reply", thread_id="42", reply_deadline=deadline)
    if approve:
        db.approve_message(mid)
    return mid


def _status(db, mid):
    return db.conn.execute("SELECT status, error FROM messages WHERE id = ?", (mid,)).fetchone()


def test_ig_send_requires_final_confirmation(db):
    ok, skipped = _ig_reply(db, username="yes"), _ig_reply(db, username="no")
    ig = FakeIG()
    n = pipeline.send_ig_replies(db, ig, confirm=lambda row, hours: row["ig_username"] == "yes", log=lambda m: None)
    assert n == 1 and ig.sent == [("42", "Thanks for writing!")]
    assert _status(db, ok)["status"] == "sent" and _status(db, skipped)["status"] == "approved"


def test_ig_send_gates_block_before_asking(db):
    expired = _ig_reply(db, username="late", hours_left=-1)
    suppressed = _ig_reply(db, username="optout")
    db.suppress("optout", "manual")
    too_long = _ig_reply(db, username="wordy", body="x" * 1001)
    unrecorded = _ig_reply(db, username="legacy", approve=False)
    db.update_message(unrecorded, status="approved")  # approved without approved_at, e.g. an old row
    asked, ig = [], FakeIG()
    pipeline.send_ig_replies(db, ig, confirm=lambda row, hours: asked.append(row) or True, log=lambda m: None)
    assert ig.sent == [] and asked == []
    assert [_status(db, m)["status"] for m in (expired, suppressed, too_long, unrecorded)] == \
        ["expired", "rejected", "failed", "pending"]


def test_ig_send_skips_duplicate_reply(db):
    first, second = _ig_reply(db), _ig_reply(db)
    ig = FakeIG()
    pipeline.send_ig_replies(db, ig, confirm=lambda row, hours: True, log=lambda m: None)
    assert len(ig.sent) == 1 and _status(db, second)["status"] == "rejected"


def test_db_migration_adds_gate_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, lead_id INTEGER, channel TEXT, kind TEXT, step INTEGER,"
                " subject TEXT, body TEXT, status TEXT, thread_id TEXT, rfc_message_id TEXT, error TEXT,"
                " created_at TEXT, sent_at TEXT)")
    old.commit(); old.close()
    cols = {r["name"] for r in Database(path).conn.execute("PRAGMA table_info(messages)")}
    assert {"approved_at", "reply_deadline"} <= cols
