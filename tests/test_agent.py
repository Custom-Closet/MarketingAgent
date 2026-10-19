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


# ---- Instagram DM archive ---------------------------------------------------

def _graph_time(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _msg(mid, hours_ago, text, sender="42"):
    return {"id": mid, "message": text, "from": {"id": sender, "username": "fan" if sender == "42" else "brand"},
            "created_time": _graph_time(datetime.now(timezone.utc) - timedelta(hours=hours_ago))}


def test_ig_messages_stop_before_cutoff_and_return_oldest_first():
    pages = [[_msg("m4", 1, "d"), _msg("m3", 2, "c")], [_msg("m2", 30, "b"), _msg("m1", 40, "a")]]
    calls = []

    def handler(request):
        calls.append(request.url.params.get("after"))
        i = 1 if request.url.params.get("after") else 0
        return httpx.Response(200, json={"data": pages[i], "paging": {"cursors": {"after": f"p{i + 1}"}}})

    ig = _ig(handler)
    got = ig.messages("c1", stop_before=datetime.now(timezone.utc) - timedelta(hours=35))
    assert [m["id"] for m in got] == ["m2", "m3", "m4"] and len(calls) == 2
    assert [m["id"] for m in ig.messages("c1", max_messages=1)] == ["m4"]


def test_add_ig_messages_ignores_duplicates(db):
    db.save_ig_conversation("c1", igsid="42", username="fan", updated_time=None)
    batch = [_msg("m1", 2, "hi"), {**_msg("m2", 1, ""), "shares": {"data": [{"link": "https://ig/reel"}], "paging": {}}}]
    assert db.add_ig_messages("c1", batch) == 2
    assert db.add_ig_messages("c1", batch) == 0
    first, second = db.ig_transcript("c1")
    assert first["text"] == "hi" and second["text"] is None and "paging" not in second["extras_json"]


class FakeArchiveIG:
    ig_user_id = "1784"

    def __init__(self, convos, history):
        self.convos, self.history, self.fetches = convos, history, []

    def iter_conversations(self, since, **_):
        yield from self.convos

    def other_participant(self, convo):
        return convo["participants"]["data"][1]

    def messages(self, conversation_id, *, stop_before=None, **_):
        self.fetches.append(conversation_id)
        from marketing_agent.sources.instagram import parse_time
        return [m for m in self.history[conversation_id] if not stop_before or parse_time(m["created_time"]) >= stop_before]


def _archive_convo(cid, updated_hours_ago):
    return {"id": cid, "updated_time": _graph_time(datetime.now(timezone.utc) - timedelta(hours=updated_hours_ago)),
            "participants": {"data": [{"id": "1784", "username": "brand"}, {"id": "42", "username": "fan"}]}}


def test_archive_skips_unchanged_and_fetches_only_new(db):
    since = datetime.now(timezone.utc) - timedelta(days=90)
    ig = FakeArchiveIG([_archive_convo("c1", 1)], {"c1": [_msg("m1", 5, "hi"), _msg("m2", 1, "price?")]})
    assert pipeline.archive_instagram(db, ig, since=since, log=lambda m: None)["messages"] == 2

    again = pipeline.archive_instagram(db, ig, since=since, log=lambda m: None)
    assert again == {"conversations": 1, "skipped": 1, "failed": 0, "messages": 0} and ig.fetches == ["c1"]

    ig.convos = [_archive_convo("c1", 0)]                        # new activity
    ig.history["c1"].append(_msg("m3", 0, "ok, ordering"))
    assert pipeline.archive_instagram(db, ig, since=since, log=lambda m: None)["messages"] == 1
    assert [m["id"] for m in db.ig_transcript("c1")] == ["m1", "m2", "m3"]


def test_archive_refetches_conversation_interrupted_midway(db):
    since = datetime.now(timezone.utc) - timedelta(days=90)

    class Boom(FakeArchiveIG):
        def messages(self, *a, **k):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        pipeline.archive_instagram(db, Boom([_archive_convo("c1", 1)], {"c1": []}), since=since, log=lambda m: None)
    assert db.ig_conversation("c1")["updated_time"] is None      # not marked done
    ig = FakeArchiveIG([_archive_convo("c1", 1)], {"c1": [_msg("m1", 1, "hi")]})
    assert pipeline.archive_instagram(db, ig, since=since, log=lambda m: None)["messages"] == 1


def test_sync_instagram_drafts_with_conversation_history(db):
    from marketing_agent.sources.instagram import IGConversation

    class SyncIG(FakeArchiveIG):
        def conversations(self):
            return [IGConversation("42", "fan", "price?", datetime.now(timezone.utc) - timedelta(hours=1),
                                   conversation_id="c1")]

    class Writer:
        def draft_dm_reply(self, username, inbound_text, *, history="", summary=""):
            self.history = history
            from marketing_agent.personalize import DMDraft
            return DMDraft(message="It depends on the fabric - could you share a reference image?")

    ig, writer = SyncIG([], {"c1": [_msg("m1", 3, "hi, do you do bulk?"), _msg("m2", 2, "yes!", sender="1784"),
                                    _msg("m3", 1, "price?")]}), Writer()
    assert pipeline.sync_instagram(db, ig, writer, log=lambda m: None) == 1
    assert "them: hi, do you do bulk?" in writer.history and "us: yes!" in writer.history
    [draft] = db.messages(status="pending", channel="instagram")
    assert draft["thread_id"] == "42" and draft["reply_deadline"]


def test_ig_messages_shrinks_page_when_meta_says_too_much_data():
    sizes = []

    def handler(request):
        sizes.append(int(request.url.params["limit"]))
        if sizes[-1] > 8:
            return httpx.Response(500, json={"error": {"message": "Fatal", "code": -1}})
        return httpx.Response(200, json={"data": [_msg("m1", 1, "hi")]})

    assert [m["id"] for m in _ig(handler).messages("c1")] == ["m1"]
    assert sizes == [25, 8]


def test_ig_messages_falls_back_to_basic_fields_for_unreadable_message():
    seen = []

    def handler(request):
        seen.append((int(request.url.params["limit"]), "attachments" in request.url.params["fields"]))
        if "attachments" in request.url.params["fields"]:
            return httpx.Response(500, json={"error": {"message": "Fatal", "code": -1}})
        return httpx.Response(200, json={"data": [_msg("m1", 1, "hi")]})

    assert [m["id"] for m in _ig(handler).messages("c1")] == ["m1"]
    assert seen == [(25, True), (8, True), (2, True), (1, True), (1, False)]


def test_archive_continues_past_an_unreadable_conversation(db):
    from marketing_agent.sources.instagram import InstagramError
    since = datetime.now(timezone.utc) - timedelta(days=90)

    class Flaky(FakeArchiveIG):
        def messages(self, conversation_id, **k):
            if conversation_id == "bad":
                raise InstagramError("Fatal", -1)
            return super().messages(conversation_id, **k)

    ig = Flaky([_archive_convo("bad", 1), _archive_convo("c1", 2)], {"c1": [_msg("m1", 2, "hi")]})
    stats = pipeline.archive_instagram(db, ig, since=since, log=lambda m: None)
    assert stats["failed"] == 1 and stats["messages"] == 1
    assert db.ig_conversation("bad")["updated_time"] is None


# ---- brand builder ------------------------------------------------------------

class FakeAnthropic:
    """Just enough of the Anthropic client for BrandBuilder."""

    def __init__(self, merge_text="# Brand profile\n\n## FAQ\n- MOQ? 30 pieces (seen in 2 chats)"):
        from types import SimpleNamespace as NS
        from marketing_agent.brand_builder import BatchFindings, FAQ
        self.prompts, outer = [], self

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return NS(stop_reason="end_turn", content=[NS(type="thinking"), NS(type="text", text=merge_text)])

        def parse(**kw):
            outer.prompts.append(kw["messages"][0]["content"])
            return NS(stop_reason="end_turn", parsed_output=BatchFindings(
                faqs=[FAQ(question="MOQ?", answer_given="30 pieces", chats=2)], facts=[], objections=[],
                conversion_signals=[], winning_replies=[], tone_notes=[]))

        def stream(**kw):
            outer.prompts.append(kw["messages"][0]["content"])
            return Stream()

        self.messages = NS(count_tokens=lambda **kw: NS(input_tokens=len(kw["messages"][0]["content"]) // 4))
        self.beta = NS(messages=NS(parse=parse, stream=stream))


def _seed_chat(db, cid, username, lines):
    db.save_ig_conversation(cid, igsid="42", username=username, updated_time=None)
    db.add_ig_messages(cid, [_msg(f"{cid}-{i}", 10 - i, text, sender=sender) for i, (sender, text) in enumerate(lines)])


def test_brand_builder_batches_chats_and_merges(db, tmp_path, monkeypatch):
    from marketing_agent import brand_builder
    from marketing_agent.brand_builder import BrandBuilder
    monkeypatch.setattr(brand_builder, "BATCH_MAX_CHATS", 2)
    for n in range(3):
        _seed_chat(db, f"c{n}", f"fan{n}", [("42", "what is the MOQ?"), ("1784", "30 pieces per design")])
    _seed_chat(db, "ours-only", "ghost", [("1784", "new collection is live!")])   # no customer messages
    brand = tmp_path / "brand.md"
    brand.write_text("# Brand profile\n\n## Who we are\nCustom Closet")

    client = FakeAnthropic()
    builder = BrandBuilder(brand, model="claude-opus-5-5", effort="medium", client=client)
    plan = builder.plan(db, "1784", datetime.now(timezone.utc) - timedelta(days=90))
    assert plan.chats == 3 and len(plan.batches) == 2 and plan.est_cost_usd > 0
    assert "ghost" not in "".join(plan.batches)

    text = builder.build(plan, log=lambda m: None)
    assert text.startswith("# Brand profile") and "## FAQ" in text
    extract_prompt, merge_prompt = client.prompts[0], client.prompts[-1]
    assert "them: what is the MOQ?" in extract_prompt and "us: 30 pieces per design" in extract_prompt
    assert "Custom Closet" in merge_prompt and '"answer_given": "30 pieces"' in merge_prompt
    assert brand.read_text() == "# Brand profile\n\n## Who we are\nCustom Closet"   # untouched


def test_brand_builder_rejects_non_markdown_merge(tmp_path):
    from marketing_agent.brand_builder import BrandBuilder
    builder = BrandBuilder(tmp_path / "missing.md", model="claude-opus-5-5", effort="medium",
                           client=FakeAnthropic(merge_text="Sure! Here is your profile..."))
    with pytest.raises(RuntimeError, match="Markdown heading"):
        builder.merge([], chats=1)


def test_build_brand_cli_refuses_to_overwrite_brand_md(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from marketing_agent import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_DB_S3_URI", "")
    monkeypatch.setenv("BRAND_FILE", str(tmp_path / "brand.md"))
    result = CliRunner().invoke(cli.app, ["build-brand", "--out", str(tmp_path / "brand.md")])
    assert result.exit_code != 0 and "must not be brand.md" in result.output


def test_claude_configured_reads_key_from_environment(monkeypatch):
    from marketing_agent import clients
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert not clients.claude_configured()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    assert not clients.claude_configured()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert clients.claude_configured()
