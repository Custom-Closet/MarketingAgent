"""Repository layer: the SQL behind the inbox."""

import sqlite3

from conftest import FAQS, US, ago, seed_chat

from marketing_agent.db import Database
from marketing_agent.inbox.faq import FAQ, load_faq_file, match_faq
from marketing_agent.inbox.repository import InboxRepository


def test_threads_awaiting_reply_only_lists_customer_last_within_window(inbox):
    db, repo, *_ = inbox
    seed_chat(db, "c-waiting", "1", "fan", [("us", "hello!", 5), ("them", "where are you based?", 2)])
    seed_chat(db, "c-answered", "2", "done", [("them", "price?", 3), ("us", "please share the image", 1)])
    seed_chat(db, "c-old", "3", "stale", [("them", "hi", 30)])
    threads = repo.threads_awaiting_reply(US, since=ago(23))
    assert [t["id"] for t in threads] == ["c-waiting"]
    assert threads[0]["username"] == "fan" and threads[0]["igsid"] == "1"


def test_unanswered_messages_are_those_after_our_last_reply(inbox):
    db, repo, *_ = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "hi", 5), ("us", "Hi!", 4), ("them", "what's the MOQ?", 2),
                                     ("them", "and pricing?", 1)])
    assert [m["text"] for m in repo.unanswered_messages("c1", US)] == ["what's the MOQ?", "and pricing?"]
    seed_chat(db, "c2", "2", "new", [("them", "hello", 1)])
    assert [m["text"] for m in repo.unanswered_messages("c2", US)] == ["hello"]
    assert repo.customer_text("c1", US) == "hi what's the MOQ? and pricing?"


def test_create_and_read_draft_joins_lead_and_conversation(inbox):
    db, repo, *_ = inbox
    seed_chat(db, "c1", "1", "fan", [("them", "price?", 1)])
    draft_id = repo.create_draft(username="fan", igsid="1", body="Please share the image",
                                 reply_deadline=ago(-22), faq_key="pricing")
    draft = repo.draft(draft_id)
    assert (draft["ig_username"], draft["conversation_id"], draft["faq_key"], draft["status"]) == \
        ("fan", "c1", "pricing", "pending")
    assert repo.open_draft("1")["id"] == draft_id
    repo.update_draft(draft_id, status="rejected")
    assert repo.open_draft("1") is None and repo.open_drafts() == []


def test_seed_faqs_never_overwrites_edits(inbox):
    _, repo, *_ = inbox
    repo.update_faq("location", answer="Jaipur, Rajasthan", approved=True)
    assert repo.seed_faqs(FAQS + [FAQ("moq", "MOQ?", "Just 1", ("moq",))]) == 1
    location = repo.faq("location")
    assert location.answer == "Jaipur, Rajasthan" and location.approved
    assert repo.faq("moq").keywords == ("moq",)


def test_sent_replies_since_counts_only_sent_instagram_replies(inbox):
    db, repo, *_ = inbox
    a = repo.create_draft(username="fan", igsid="1", body="hi", reply_deadline=ago(-5), faq_key=None)
    repo.create_draft(username="other", igsid="2", body="hi", reply_deadline=ago(-5), faq_key=None)
    db.mark_sent(a)
    assert repo.sent_replies_since(ago(1)) == 1


def test_migration_adds_faq_key_and_faqs_table(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, lead_id INTEGER, channel TEXT, kind TEXT, step INTEGER,"
                " subject TEXT, body TEXT, status TEXT, thread_id TEXT, rfc_message_id TEXT, error TEXT,"
                " created_at TEXT, sent_at TEXT)")
    old.commit()
    old.close()
    db = Database(path)
    assert "faq_key" in {r["name"] for r in db.conn.execute("PRAGMA table_info(messages)")}
    assert InboxRepository(db).faqs() == []


# ---- FAQ matching (pure functions) -----------------------------------------------

def test_match_faq_prefers_more_and_longer_keyword_hits():
    assert match_faq("Hi, where are you based?", FAQS).key == "location"
    assert match_faq("What's the sample cost?", FAQS).key == "sample"     # tie on hits -> longer keyword
    assert match_faq("how much for this kurta, price?", FAQS).key == "pricing"
    assert match_faq("lovely work!", FAQS) is None
    assert match_faq("this is a debased question", FAQS) is None          # word boundaries


def test_load_faq_file(tmp_path):
    path = tmp_path / "faqs.json"
    path.write_text('[{"key": "moq", "question": "MOQ?", "answer": "Just 1", "keywords": ["MOQ", "Minimum order"]}]')
    [faq] = load_faq_file(path)
    assert faq.keywords == ("moq", "minimum order") and not faq.approved
    assert load_faq_file(tmp_path / "missing.json") == []
