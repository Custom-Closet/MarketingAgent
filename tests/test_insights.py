"""Funnel rules, reports, stage overrides and the strategist."""

from types import SimpleNamespace as NS

import pytest
from conftest import US, seed_chat

from marketing_agent.db import Database
from marketing_agent.insights.analysis import analyze, funnel_report, outreach_report
from marketing_agent.insights.service import InsightsService
from marketing_agent.insights.strategist import Strategist, mask

CANNED = "So our prices differ from product to product, please share the reference image"


def _rows(db):
    from datetime import datetime, timedelta, timezone
    return db.ig_messages_since(datetime.now(timezone.utc) - timedelta(days=90))


def _leads(db, **kw):
    return {l.username: l for l in analyze(_rows(db), US, **kw).leads}


@pytest.fixture
def db():
    db = Database(":memory:", check_same_thread=False)
    # canned auto-reply used 4+ times so it counts as canned, not a human quote
    for i in range(4):
        seed_chat(db, f"tap{i}", f"t{i}", f"tapper{i}", [("them", "What's the pricing for bulk orders?", 400 - i),
                                                         ("us", CANNED, 399.99 - i)])
    seed_chat(db, "q", "1", "quoted", [("them", "price for my brand kurta set?", 50), ("them", None, 49.9),
                                      ("us", "Sample - 1600/-, Bulk (5MOQ any size) - 880/-", 48)], attachment_at={1})
    seed_chat(db, "o", "2", "ordered", [("them", "I want this top", 30), ("us", "It will be ₹1,599/-", 29),
                                       ("them", "ok", 28), ("us", "You can make the payment here", 27)])
    seed_chat(db, "w", "3", "delivered", [("them", "for my birthday dress", 100), ("us", "₹1599/- incl shipping", 99),
                                         ("us", "Please find the tracking link https://www.delhivery.com/track-v2/x", 2)])
    seed_chat(db, "u", "4", "ignored", [("them", "Can you make lehengas for my sister's wedding?", 24 * 20)])
    seed_chat(db, "f", "5", "fabricless", [("them", "do you provide fabric? for my label", 24 * 30),
                                          ("us", "Please note the brand has to provide the fabric and trims", 24 * 29)])
    seed_chat(db, "p", "6", "pricey", [("them", "how much for this?", 10), ("us", "That would be 2500/-", 9),
                                      ("them", "too expensive for me", 8)])
    seed_chat(db, "spam", "7", "creator", [("them", "Hi! Interested in a paid collab / UGC?", 5)])
    seed_chat(db, "out1", "8", "outbrand", [("us", "Hi! We're Custom Closet, a Jaipur studio...", 72),
                                           ("them", "Thanks for writing to us! Pick a topic below.", 71.999)])
    seed_chat(db, "out2", "9", "realbrand", [("us", "Hi! We're Custom Closet, a Jaipur studio...", 72),
                                            ("them", "Hi! We're launching in Nov, can you do 20 dresses?", 60)])
    return db


def test_stage_rules(db):
    leads = _leads(db)
    assert leads["quoted"].reached == "quoted" and leads["quoted"].segment == "brand"
    assert leads["ordered"].reached == "order"
    assert leads["delivered"].reached == "won" and leads["delivered"].stage == "won"
    assert leads["tapper0"].reached == "new"            # canned reply is not a quote, button tap is not engagement
    assert "creator" not in leads                        # collab pitch is not a lead
    assert leads["realbrand"].source == "outreach" and "outbrand" not in leads   # instant auto-reply isn't a lead


def test_lost_reasons(db):
    leads = _leads(db, lost_after_days=14)
    assert (leads["ignored"].stage, leads["ignored"].reason) == ("lost", "unanswered")
    assert (leads["fabricless"].stage, leads["fabricless"].reason) == ("lost", "fabric")
    assert (leads["pricey"].stage, leads["pricey"].reason) == ("lost", "price")   # declined, recent
    assert leads["tapper0"].reason == "no_reference"
    assert leads["quoted"].stage == "quoted"                                   # recent, still open


def test_funnel_report_counts_and_conversion(db):
    leads = list(_leads(db).values())
    report = funnel_report(leads)
    steps = {s["stage"]: s for s in report["steps"]}
    assert steps["new"]["reached"] == len(leads)
    assert steps["won"]["reached"] == 1 and steps["order"]["reached"] == 2
    assert steps["order"]["conversion"] == round(100 * 2 / steps["quoted"]["reached"], 1)
    assert {d["reason"] for d in report["dropoffs"]} >= {"unanswered", "fabric", "price"}
    assert report["response"]["awaiting_reply"] >= 1


def test_outreach_report_separates_auto_replies(db):
    analysis = analyze(_rows(db), US)
    report = outreach_report(analysis.outreach, analysis.leads)
    assert report["sent"] == 2 and report["real_replies"] == 1 and report["reply_rate"] == 50.0
    [template] = report["templates"]
    assert template["sent"] == 2 and template["real_replies"] == 1


def test_overrides_and_settings(db):
    service = InsightsService(db, our_id=US)
    service.set_stage("u", "quoted")
    lead = next(l for l in service.leads(stage="quoted") if l["username"] == "ignored")
    assert lead["overridden"] and lead["reached"] == "qualified"     # typed request + personal occasion
    service.set_stage("u", None)
    assert all(l["username"] != "ignored" for l in service.leads(stage="quoted"))
    with pytest.raises(ValueError):
        service.set_stage("u", "maybe")
    with pytest.raises(KeyError):
        service.set_stage("nope", "won")
    db.set_setting("lost_after_days", "60")
    assert next(l for l in service.leads() if l["username"] == "ignored")["stage"] != "lost"


# ---- strategist -----------------------------------------------------------------------

class FakeClaude:
    def __init__(self):
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return NS(stop_reason="end_turn", content=[NS(type="thinking"), NS(type="text", text="## Fix replies\n- Answer within 1h")])


def test_strategist_requires_confirmation_and_sends_briefing_first(db):
    claude = FakeClaude()
    service = InsightsService(db, our_id=US, strategist=Strategist("# Brand", model="claude-opus-5-5",
                                                                    effort="medium", client=claude))
    est = service.strategy_estimate("Biggest leaks?", [])
    assert est["input_tokens"] > 0 and est["usd"] > 0
    with pytest.raises(ValueError):
        service.strategy("Biggest leaks?", [], confirm=False)
    assert service.strategy("Biggest leaks?", [], confirm=True).startswith("## Fix replies")

    history = [{"role": "user", "content": "Biggest leaks?"}, {"role": "assistant", "content": "## Fix replies"}]
    service.strategy("And for brands?", history, confirm=True)
    messages = claude.calls[-1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"].startswith("<funnel_data>") and messages[-1]["content"] == "And for brands?"
    assert "@" not in messages[0]["content"].split("</funnel_data>")[1]   # no usernames in examples


def test_strategist_not_configured(db):
    with pytest.raises(RuntimeError):
        InsightsService(db, our_id=US).strategy_estimate("q", [])


def test_mask_hides_phones_and_emails():
    assert mask("call 70142 38228 or mail a.b@shop.in") == "call <phone> or mail <email>"
