"""Service layer: drafting rules, priority, bulk actions and the send gates."""

import pytest
from conftest import US, ago, seed_chat

from marketing_agent.inbox.service import (
    MAX_SEND_BATCH, InboxService, QueueFilters, SendNotConfirmed, score_thread,
)
from marketing_agent.sources.instagram import InstagramError, InstagramRateLimited


def _queue_by_user(service, filters=None):
    return {i.username: i for i in service.queue(filters)}


# ---- drafting ---------------------------------------------------------------------

def test_refresh_creates_one_draft_per_waiting_chat_with_faq_suggestion(inbox):
    db, repo, service, _ = inbox
    seed_chat(db, "c1", "1", "asker", [("them", "Hi! Where are you based?", 2)])
    seed_chat(db, "c2", "2", "chatty", [("them", "love your work", 1)])
    seed_chat(db, "c3", "3", "answered", [("them", "price?", 3), ("us", "share the image", 1)])
    assert service.refresh_drafts() == 2
    assert service.refresh_drafts() == 0                            # idempotent
    q = _queue_by_user(service)
    assert set(q) == {"asker", "chatty"}
    assert q["asker"].category == "location" and q["asker"].body.startswith("We are based in Jaipur")
    assert q["chatty"].category == "other" and q["chatty"].body == ""


def test_refresh_skips_suppressed_customers(inbox):
    db, _, service, _ = inbox
    seed_chat(db, "c1", "1", "optout", [("them", "price?", 1)])
    db.suppress("optout", "manual")
    assert service.refresh_drafts() == 0


def test_refresh_closes_drafts_answered_in_instagram_or_expired(inbox):
    db, repo, service, _ = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "price?", 2)])
    seed_chat(db, "c2", "2", "late", [("them", "price?", 22.9)])
    service.refresh_drafts()
    drafts = {d["thread_id"]: d["id"] for d in repo.open_drafts()}
    db.add_ig_messages("c1", [{"id": "c1-reply", "message": "Sent from the Instagram app",
                               "from": {"id": US}, "created_time": ago(-0.01).strftime("%Y-%m-%dT%H:%M:%S+0000")}])
    db.conn.execute("DELETE FROM ig_messages WHERE conversation_id = 'c2'")
    seed_chat(db, "c2", "2", "late", [("them", "price?", 24)])         # now outside the window
    service.refresh_drafts()
    assert repo.draft(drafts["1"])["status"] == "rejected" and repo.draft(drafts["1"])["error"] == "answered in Instagram"
    assert repo.draft(drafts["2"])["status"] == "expired"


# ---- priority and filters ------------------------------------------------------------

def test_score_thread_rewards_intent_reference_images_brands_and_urgency():
    assert score_thread(["price for 50 pieces?"], True, "launching my brand", 3)[1] == "high"
    assert score_thread(["where are you based"], False, "where are you based", 20)[1] == "low"
    assert score_thread(["hi"], False, "hi", 20)[0] < 0
    _, _, reasons = score_thread(["sample cost?"], False, "", 2)
    assert "reply window closes in 2h" in reasons


def test_queue_sorts_by_priority_and_filters(inbox):
    db, _, service, _ = inbox
    seed_chat(db, "c1", "1", "hot", [("them", "We are launching our brand, price for 50 pieces?", 21),
                                     ("them", None, 21)], attachment_at={1})
    seed_chat(db, "c2", "2", "casual", [("them", "where are you based", 1)])
    seed_chat(db, "c3", "3", "personal", [("them", "how much for my wedding lehenga, for personal use", 2)])
    service.refresh_drafts()
    order = [i.username for i in service.queue()]
    assert order[0] == "hot" and order[-1] == "casual"
    hot = service.queue(QueueFilters(search="hot"))[0]
    assert hot.priority == "high" and hot.sent_attachment and hot.customer_type == "brand" and hot.hours_left < 4
    assert [i.username for i in service.queue(QueueFilters(priority={"high"}))] == ["hot"]
    assert [i.username for i in service.queue(QueueFilters(category="location"))] == ["casual"]
    assert [i.username for i in service.queue(QueueFilters(max_hours_left=4))] == ["hot"]
    assert [i.username for i in service.queue(QueueFilters(customer_type="personal"))] == ["personal"]
    assert [i.username for i in service.queue(QueueFilters(search="lehenga"))] == ["personal"]


# ---- approving and editing ---------------------------------------------------------

def test_approve_refuses_empty_drafts(inbox):
    db, repo, service, _ = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "love it", 1)])
    service.refresh_drafts()
    [item] = service.queue()
    assert service.approve([item.draft_id]) == {item.draft_id: "write a reply first"}
    service.edit(item.draft_id, "Thank you so much!")
    assert service.approve([item.draft_id]) == {}
    assert repo.draft(item.draft_id)["approved_at"]


def test_one_click_faq_approval_only_takes_approved_unchanged_answers(inbox):
    db, repo, service, _ = inbox
    seed_chat(db, "c1", "1", "a", [("them", "where are you based?", 1)])
    seed_chat(db, "c2", "2", "b", [("them", "where are you located?", 1)])
    seed_chat(db, "c3", "3", "c", [("them", "price?", 1)])
    service.refresh_drafts()
    q = _queue_by_user(service)
    assert not any(i.faq_approved for i in q.values())              # nothing approved yet
    service.approve_faq("location")
    service.edit(q["b"].draft_id, "We're in Jaipur! Shipping pan India.")   # edited -> needs its own review
    approved = service.approve_faq_replies()
    assert approved == [q["a"].draft_id]
    assert repo.draft(q["c"].draft_id)["status"] == "pending"


def test_editing_an_approved_draft_sends_it_back_for_review(inbox):
    db, repo, service, _ = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "where are you based?", 1)])
    service.refresh_drafts()
    [item] = service.queue()
    service.approve([item.draft_id])
    service.edit(item.draft_id, "Jaipur!")
    draft = repo.draft(item.draft_id)
    assert draft["status"] == "pending" and draft["approved_at"] is None


def test_apply_faq_and_edit_faq_withdraws_its_approval(inbox):
    db, repo, service, _ = inbox
    seed_chat(db, "c1", "1", "a", [("them", "hello", 1)])
    seed_chat(db, "c2", "2", "b", [("them", "hey", 1)])
    service.refresh_drafts()
    ids = [i.draft_id for i in service.queue()]
    assert service.apply_faq(ids, "pricing") == 2
    assert {repo.draft(i)["faq_key"] for i in ids} == {"pricing"}
    service.approve_faq("pricing")
    service.edit_faq("pricing", "Share a picture and I'll quote!")
    assert not repo.faq("pricing").approved
    with pytest.raises(KeyError):
        service.apply_faq(ids, "nope")
    with pytest.raises(ValueError):
        service.edit_faq("pricing", "   ")


def test_reject_closes_draft(inbox):
    db, repo, service, _ = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "hi", 1)])
    service.refresh_drafts()
    [item] = service.queue()
    service.reject([item.draft_id])
    assert service.queue() == [] and repo.draft(item.draft_id)["status"] == "rejected"
    with pytest.raises(KeyError):
        service.approve([item.draft_id])


# ---- sending ------------------------------------------------------------------------

def _approved_draft(db, service, username="fan", igsid="1", text="where are you based?", hours=1):
    seed_chat(db, f"c-{username}", igsid, username, [("them", text, hours)])
    service.refresh_drafts()
    item = next(i for i in service.queue() if i.username == username)
    service.approve([item.draft_id])
    return item.draft_id


def test_send_requires_confirmation_and_approval(inbox):
    db, repo, service, ig = inbox
    draft_id = _approved_draft(db, service)
    with pytest.raises(SendNotConfirmed):
        service.send([draft_id], confirm=False)
    seed_chat(db, "c-p", "9", "pending", [("them", "where are you based?", 1)])
    service.refresh_drafts()
    pending_id = next(i.draft_id for i in service.queue() if i.username == "pending")
    result = service.send([draft_id, pending_id], confirm=True)
    assert result.sent == [draft_id] and result.blocked == {pending_id: "not approved"}
    assert ig.sent == [("1", "We are based in Jaipur, we deliver pan India")]
    assert repo.draft(draft_id)["status"] == "sent"


def test_send_blocks_when_customer_was_answered_in_instagram(inbox):
    db, repo, service, ig = inbox
    draft_id = _approved_draft(db, service)
    db.add_ig_messages("c-fan", [{"id": "manual", "message": "Jaipur!", "from": {"id": US},
                                  "created_time": ago(-0.01).strftime("%Y-%m-%dT%H:%M:%S+0000")}])
    result = service.send([draft_id], confirm=True)
    assert result.blocked == {draft_id: "answered in Instagram"} and ig.sent == []
    assert repo.draft(draft_id)["status"] == "rejected"


def test_send_blocks_expired_suppressed_and_duplicate(inbox):
    db, repo, service, ig = inbox
    late = _approved_draft(db, service, "late", "2", hours=1)
    repo.update_draft(late, reply_deadline=ago(0.1).isoformat(timespec="seconds"))
    optout = _approved_draft(db, service, "optout", "3")
    db.suppress("optout", "manual")                                 # suppress() also rejects queued drafts
    result = service.send([late, optout], confirm=True)
    assert result.blocked[late] == "reply window closed" and result.blocked[optout] == "not approved"
    assert ig.sent == []


def test_send_stops_batch_on_rate_limit_and_keeps_rest_approved(inbox):
    db, repo, service, ig = inbox
    first = _approved_draft(db, service, "a", "1")
    second = _approved_draft(db, service, "b", "2")
    ig.fail_with = InstagramRateLimited("slow down", 613)
    result = service.send([first, second], confirm=True)
    assert list(result.failed) == [first] and result.sent == []
    assert repo.draft(second)["status"] == "approved"


def test_send_records_other_errors_and_continues(inbox):
    db, repo, service, ig = inbox
    first = _approved_draft(db, service, "a", "1")
    ig.fail_with = InstagramError("outside of allowed window", 10)
    result = service.send([first], confirm=True)
    assert result.failed == {first: "outside of allowed window"}
    assert repo.draft(first)["error"] == "outside of allowed window"


def test_send_limits_batch_size(inbox):
    _, _, service, _ = inbox
    with pytest.raises(ValueError):
        service.send(list(range(MAX_SEND_BATCH + 1)), confirm=True)


# ---- sync, Claude drafts and stats ------------------------------------------------------

def test_sync_archives_window_and_creates_drafts(inbox):
    db, repo, service, ig = inbox
    ig.add_chat("c1", "1", "fan", [("us", "Hello!", 3), ("them", "can I get a sample first?", 1)])
    ig.add_chat("c2", "2", "old", [("them", "price?", 40)])           # outside the window: not fetched
    stats = service.sync()
    assert stats == {"conversations": 1, "messages": 2, "failed": 0, "drafts": 1}
    [item] = service.queue()
    assert item.username == "fan" and item.category == "sample"
    assert service.sync()["conversations"] == 0                       # unchanged chats are skipped
    assert service.sync_state["running"] is False and service.sync_state["last_finished"]


def test_ai_draft_uses_conversation_history(inbox):
    db, repo, _, ig = inbox

    class Writer:
        def draft_dm_reply(self, username, inbound_text, *, history="", summary=""):
            self.args = (username, inbound_text, history)
            from marketing_agent.personalize import DMDraft
            return DMDraft(message="Lovely! Could you share the reference image?")

    writer = Writer()
    service = InboxService(repo, our_id=US, ig=ig, writer=writer)
    seed_chat(db, "c1", "1", "fan", [("them", "we're launching a brand", 3), ("us", "Congrats!", 2),
                                     ("them", "can you make kurta sets?", 1)])
    service.refresh_drafts()
    [item] = service.queue()
    assert service.ai_draft(item.draft_id).startswith("Lovely!")
    username, inbound, history = writer.args
    assert inbound == "can you make kurta sets?" and "them: we're launching a brand" in history
    assert repo.draft(item.draft_id)["body"].startswith("Lovely!")


def test_ai_draft_without_claude_configured(inbox):
    db, _, service, _ = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "hi", 1)])
    service.refresh_drafts()
    with pytest.raises(RuntimeError):
        service.ai_draft(service.queue()[0].draft_id)


def test_stats(inbox):
    db, _, service, _ = inbox
    seed_chat(db, "c1", "1", "a", [("them", "where are you based?", 1)])
    seed_chat(db, "c2", "2", "b", [("them", "price for my brand, 20 pieces?", 21)], attachment_at=set())
    service.refresh_drafts()
    service.approve_faq("location")
    s = service.stats()
    assert (s["open"], s["faq_ready"], s["closing_soon"], s["sent_24h"]) == (2, 1, 1, 0)


def test_no_faq_suggestion_after_customer_sent_an_image(inbox):
    db, _, service, _ = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "how much?", 2), ("us", "please share the reference image", 1.5),
                                     ("them", None, 1), ("them", "let me know the price", 1)], attachment_at={2})
    service.refresh_drafts()
    [item] = service.queue()
    assert item.body == "" and item.category == "reference_image" and item.sent_attachment


def test_earlier_buying_intent_still_raises_priority():
    low = score_thread(["where are you located?"], False, "where are you located?", 20)[0]
    warmer = score_thread(["where are you located?"], False, "I have the tech pack, just need a sample", 20)[0]
    assert warmer == low + 1
