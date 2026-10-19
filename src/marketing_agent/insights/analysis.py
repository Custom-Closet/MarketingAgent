"""Funnel and outreach analysis over the archived DMs. Pure functions, no API calls.

A conversation's stage is inferred from what was said (rules below) and can be overridden by hand.
"Canned" messages - texts we sent 4+ times, i.e. auto-replies and templates - don't count as a
human quote or reply, since e.g. the ad auto-reply contains a product price.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

FUNNEL = ["new", "engaged", "qualified", "quoted", "order", "won"]
STAGES = FUNNEL + ["lost"]
STAGE_LABELS = {"new": "New", "engaged": "Engaged", "qualified": "Qualified", "quoted": "Quoted",
                "order": "Sample / Order", "won": "Won", "lost": "Lost"}
REASON_LABELS = {
    "unanswered": "We didn't reply", "out_of_scope": "We can't make it", "price": "Price",
    "fabric": "Fabric must be provided", "no_reference": "Never sent a reference",
    "declined": "Said no / not now", "went_quiet": "Went quiet",
}
SOURCE_LABELS = {"ad_price_tap": "Ad / post price tap", "brand_keyword": "BRAND keyword",
                 "faq_button": "FAQ button", "direct_message": "Typed message", "outreach": "Our outreach"}

CANNED_MIN_COUNT = 4
AUTO_REPLY_SECONDS = 90           # a reply this fast is an auto-response, not a person

BUTTON_TEXTS = {
    "what is the price?", "what's the pricing for bulk orders?", "what fabrics do you offer?",
    "how to place an order?", "how long does shipping take?", "how long does delivery take?", "'brand'",
    "brand", "brands", "can i get a sample first?", "what's the pricing for 5 pieces?",
    "what's the minimum order quantity?", "what's the price range?", "what's the pricing range?",
    "how do you select the fabric?", "how do you select the fabrics?", "what's the pricing?",
    "what's the sample cost?", "can you check the price of a product?", "can i get a discount?",
    "what's the shipping cost?", "how do you handle sizing?", "how do you handle shipping?",
    "how do you handle payment?", "do you offer custom designs?", "can i get a custom quote?",
    "what's the pricing process?", "how long does production take?", "single sample", "location",
}
GREETINGS = {"hi", "hii", "hiii", "hello", "hey", "heyy", "hai", "hie", "good morning", "👋", "ok", "okay", "yes"}

QUOTE_RE = re.compile(r"cut to pack|price estimate|₹\s?\d|\b\d{3,5}\s*/-|\b\d{3,5}\s*(rs|inr)\b|"
                      r"(sample|bulk|making|stitching)\D{0,25}\d{3,5}", re.I)
ORDER_RE = re.compile(r"make the payment|\bscanner\b|\bqr\b|you can (ship|send) (it |the fabric )?to|"
                      r"gurunanak|received,? thanks|payment (is )?(done|received)|sourcing (fee )?now|"
                      r"share (your|the) (address|measurements)", re.I)
CUSTOMER_ORDER_RE = re.compile(r"\bpaid\b|payment (done|sent)|sent the payment|transferred|placing the order", re.I)
WON_RE = re.compile(r"tracking|track-v2|thank you for placing|is ready,? (can|please)|"
                    r"(has been|got) (shipped|dispatched)|your (shirt|top|dress|outfit|order|lehenga) is ready", re.I)
INTENT_RE = re.compile(r"price|pricing|cost|rate|quote|quotation|sample|order|bulk|moq|pieces|pcs|"
                       r"how much|kitna|tech ?pack|for my brand|clothing brand|personal use", re.I)
BRAND_RE = re.compile(r"\bbrand\b|\blabel\b|manufactur|launching|boutique|\bbulk\b|production|startup|"
                      r"business|wholesale|resell", re.I)
PERSONAL_RE = re.compile(r"personal|for me\b|myself|my (wedding|sister|mom|daughter|birthday)|"
                         r"festive|navratri|haldi|mehendi|sangeet|farewell", re.I)
NOT_A_LEAD_RE = re.compile(r"collab|barter|\bugc\b|influencer|paid partnership|website design|"
                           r"video edit|digital marketing|seo\b", re.I)
DECLINE_RE = re.compile(r"no thanks|not (right )?now|not interested|too expensive|very expensive|"
                        r"out of (my|our) budget|beyond my budget|won'?t fit|not poss|will let you know|"
                        r"^no\b|moved ahead|found (another|someone)", re.I)
PRICE_OBJECTION_RE = re.compile(r"expensive|costly|budget|too high|overpriced|margin|cheaper|half the price|"
                                r"price is (more|bothering)|discount", re.I)
OUT_OF_SCOPE_RE = re.compile(r"only (work|deal) (in|with) (non.?stretch|woven)|only (do |make )?womenswear|"
                             r"won'?t be possible|not be possible|we don'?t (make|do)", re.I)
FABRIC_POLICY_RE = re.compile(r"brand has to provide|provide (us )?(the )?fabric", re.I)
ASKED_REFERENCE_RE = re.compile(r"reference image", re.I)


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class Lead:
    conversation_id: str
    username: str | None
    stage: str                  # current: a FUNNEL stage or "lost" (override wins)
    reached: str                # furthest FUNNEL stage reached (inferred)
    overridden: bool
    segment: str                # brand / personal / unknown
    source: str
    reason: str | None          # drop-off reason when lost
    first_at: str
    last_at: str
    last_from: str              # "us" / "them"
    last_text: str
    customer_messages: int
    first_response_minutes: float | None   # first human reply after the customer's first message
    awaiting_reply: bool        # customer wrote last and we haven't answered
    template: str | None = None  # opening outreach message, when we wrote first

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OutreachConversation:
    template: str
    replied: bool
    human_replied: bool
    reached: str


@dataclass
class Analysis:
    leads: list[Lead] = field(default_factory=list)
    outreach: list[OutreachConversation] = field(default_factory=list)
    excluded: Counter = field(default_factory=Counter)


def canned_texts(rows, our_id: str) -> set[str]:
    counts = Counter(_norm(r["text"]) for r in rows if r["from_id"] == our_id and r["text"])
    return {t for t, n in counts.items() if n >= CANNED_MIN_COUNT}


def _source(first, our_id: str) -> str:
    if first["from_id"] == our_id:
        return "outreach"
    text = _norm(first["text"])
    if text == "what is the price?":
        return "ad_price_tap"
    if text in ("'brand'", "brand", "brands"):
        return "brand_keyword"
    if text in BUTTON_TEXTS:
        return "faq_button"
    return "direct_message"


def classify(msgs: list, our_id: str, canned: set[str], *, now: datetime, lost_after_days: int,
             override: str | None = None) -> Lead | OutreachConversation | str:
    """One conversation -> Lead, OutreachConversation (we wrote first, no real reply) or an exclusion reason."""
    cust = [m for m in msgs if m["from_id"] != our_id]
    ours = [m for m in msgs if m["from_id"] == our_id]
    cust_text = " ".join(m["text"] for m in cust if m["text"])
    human_ours = [m for m in ours if m["text"] and _norm(m["text"]) not in canned]
    first = msgs[0]
    source = _source(first, our_id)

    if not cust:
        return OutreachConversation(template=_norm(first["text"])[:80], replied=False, human_replied=False,
                                    reached="new") if source == "outreach" else "no_customer_messages"
    if NOT_A_LEAD_RE.search(cust_text):
        return "not_a_lead"

    sent_media = any(m["extras_json"] and not m["text"] for m in cust)
    typed = [m for m in cust if m["text"] and _norm(m["text"]) not in BUTTON_TEXTS
             and _norm(m["text"]).rstrip("!.?") not in GREETINGS]

    reached = "new"
    if typed or (len(cust) >= 2 and human_ours):
        reached = "engaged"
    if sent_media or INTENT_RE.search(" ".join(m["text"] for m in typed)) or BRAND_RE.search(cust_text) \
            or PERSONAL_RE.search(cust_text):
        reached = "qualified" if (typed or sent_media) else reached
    if any(QUOTE_RE.search(m["text"]) for m in human_ours):
        reached = "quoted"
    if any(ORDER_RE.search(m["text"]) for m in human_ours) or CUSTOMER_ORDER_RE.search(cust_text):
        reached = "order"
    if any(WON_RE.search(m["text"]) for m in human_ours):
        reached = "won"

    if source == "outreach":
        reply = cust[0]
        delay = (_dt(reply["created_time"]) - _dt(first["created_time"])).total_seconds()
        human = delay > AUTO_REPLY_SECONDS and bool(typed)
        if not human and reached in ("new", "engaged"):
            return OutreachConversation(template=_norm(first["text"])[:80], replied=True, human_replied=False,
                                        reached=reached)

    last = msgs[-1]
    last_from = "us" if last["from_id"] == our_id else "them"
    silent_days = (now - _dt(last["created_time"])).total_seconds() / 86400
    declined = last_from == "them" and bool(last["text"]) and bool(DECLINE_RE.search(_norm(last["text"])))
    stage = reached
    reason = None
    if reached != "won" and (declined or silent_days > lost_after_days):
        stage = "lost"
        if declined and PRICE_OBJECTION_RE.search(last["text"]):
            reason = "price"
        elif declined:
            reason = "declined"
        elif last_from == "them":
            reason = "unanswered"
        elif any(OUT_OF_SCOPE_RE.search(m["text"] or "") for m in ours):
            reason = "out_of_scope"
        elif PRICE_OBJECTION_RE.search(cust_text):
            reason = "price"
        elif any(FABRIC_POLICY_RE.search(m["text"] or "") for m in ours) and reached in ("new", "engaged", "qualified"):
            reason = "fabric"
        elif not sent_media and any(ASKED_REFERENCE_RE.search(m["text"] or "") for m in ours):
            reason = "no_reference"
        else:
            reason = "went_quiet"

    first_response = None
    first_cust = cust[0]
    for m in human_ours:
        if m["created_time"] > first_cust["created_time"]:
            first_response = round((_dt(m["created_time"]) - _dt(first_cust["created_time"])).total_seconds() / 60, 1)
            break

    if BRAND_RE.search(cust_text):
        segment = "brand"
    elif PERSONAL_RE.search(cust_text) or source == "ad_price_tap":   # ad taps are shoppers unless they say otherwise
        segment = "personal"
    else:
        segment = "unknown"
    if override in STAGES:
        stage = override
    return Lead(
        conversation_id=first["conversation_id"], username=first["username"], stage=stage, reached=reached,
        overridden=override in STAGES, segment=segment, source=source, reason=reason if stage == "lost" else None,
        first_at=first["created_time"], last_at=last["created_time"], last_from=last_from,
        last_text=(last["text"] or ("<attachment>" if last["extras_json"] else ""))[:200],
        customer_messages=len(cust), first_response_minutes=first_response,
        awaiting_reply=last_from == "them" and stage not in ("won", "lost"),
        template=_norm(first["text"])[:80] if source == "outreach" else None)


def analyze(rows, our_id: str, *, now: datetime | None = None, lost_after_days: int = 14,
            overrides: dict[str, str] | None = None) -> Analysis:
    """Rows: archived messages ordered by conversation then time (Database.ig_messages_since)."""
    now = now or datetime.now(timezone.utc)
    overrides = overrides or {}
    canned = canned_texts(rows, our_id)
    by_convo: dict[str, list] = defaultdict(list)
    for r in rows:
        by_convo[r["conversation_id"]].append(r)
    result = Analysis()
    for cid, msgs in by_convo.items():
        item = classify(msgs, our_id, canned, now=now, lost_after_days=lost_after_days, override=overrides.get(cid))
        if isinstance(item, Lead):
            result.leads.append(item)
        elif isinstance(item, OutreachConversation):
            result.outreach.append(item)
        else:
            result.excluded[item] += 1
    return result


def _rate(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def funnel_report(leads: list[Lead]) -> dict:
    """Stage counts, step conversion, drop-offs, sources, segments, weekly volume and response times."""
    index = {s: i for i, s in enumerate(FUNNEL)}
    reached = [sum(1 for l in leads if index[l.reached] >= i) for i in range(len(FUNNEL))]
    steps = [{"stage": s, "label": STAGE_LABELS[s], "reached": reached[i],
              "conversion": _rate(reached[i], reached[i - 1]) if i else None} for i, s in enumerate(FUNNEL)]
    lost = [l for l in leads if l.stage == "lost"]

    def breakdown(key: str, labels: dict | None = None) -> list[dict]:
        groups: dict[str, list[Lead]] = defaultdict(list)
        for l in leads:
            groups[getattr(l, key)].append(l)
        out = []
        for name, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            quoted = sum(index[l.reached] >= index["quoted"] for l in group)
            ordered = sum(index[l.reached] >= index["order"] for l in group)
            out.append({"key": name, "label": (labels or {}).get(name, name.title()), "leads": len(group),
                        "quoted": quoted, "order": ordered, "won": sum(l.reached == "won" for l in group),
                        "quote_rate": _rate(quoted, len(group)), "order_rate": _rate(ordered, len(group))})
        return out

    weekly = Counter()
    for l in leads:
        d = _dt(l.first_at).date()
        weekly[(d - timedelta(days=d.weekday())).isoformat()] += 1
    responses = [l.first_response_minutes for l in leads if l.first_response_minutes is not None]
    return {
        "total": len(leads),
        "steps": steps,
        "board": {s: sum(l.stage == s for l in leads) for s in STAGES},
        "won": sum(l.stage == "won" for l in leads),
        "lost": len(lost),
        "open": sum(l.stage not in ("won", "lost") for l in leads),
        "dropoffs": [{"reason": r, "label": REASON_LABELS[r], "count": n}
                     for r, n in Counter(l.reason for l in lost).most_common()],
        "lost_after_stage": dict(Counter(l.reached for l in lost)),
        "by_source": breakdown("source", SOURCE_LABELS),
        "by_segment": breakdown("segment"),
        "weekly": [{"week": w, "leads": n} for w, n in sorted(weekly.items())],
        "response": {
            "median_first_reply_hours": round(statistics.median(responses) / 60, 1) if responses else None,
            "within_1h": _rate(sum(r <= 60 for r in responses), len(responses)),
            "within_24h": _rate(sum(r <= 1440 for r in responses), len(responses)),
            "never_answered": sum(l.first_response_minutes is None for l in leads),
            "awaiting_reply": sum(l.awaiting_reply for l in leads),
        },
    }


def outreach_report(items: list[OutreachConversation], leads: list[Lead]) -> dict:
    """Per-template outbound performance (a real reply = a person typed back) plus lead-source quality."""
    index = {s: i for i, s in enumerate(FUNNEL)}
    outreach_leads = [l for l in leads if l.source == "outreach"]
    groups: dict[str, dict] = defaultdict(lambda: {"sent": 0, "auto_replies": 0, "real_replies": 0,
                                                    "quoted": 0, "orders": 0})
    for o in items:
        g = groups[o.template]
        g["sent"] += 1
        g["auto_replies"] += o.replied
    for l in outreach_leads:
        g = groups[l.template or ""]
        g["sent"] += 1
        g["real_replies"] += 1
        g["quoted"] += index[l.reached] >= index["quoted"]
        g["orders"] += index[l.reached] >= index["order"]
    # Messages sent fewer than 3 times are one-off follow-ups, not templates: group them.
    merged: dict[str, dict] = defaultdict(lambda: {"sent": 0, "auto_replies": 0, "real_replies": 0,
                                                    "quoted": 0, "orders": 0})
    for t, g in groups.items():
        name = (t or "(image or reel)") if g["sent"] >= 3 else "Other one-off messages"
        for k, v in g.items():
            merged[name][k] += v
    templates = [{"template": t, **g, "reply_rate": _rate(g["real_replies"], g["sent"])} for t, g in merged.items()]
    templates.sort(key=lambda t: (t["template"] == "Other one-off messages", -t["sent"]))
    sent = len(items) + len(outreach_leads)
    return {
        "sent": sent,
        "real_replies": len(outreach_leads),
        "reply_rate": _rate(len(outreach_leads), sent),
        "quoted": sum(index[l.reached] >= index["quoted"] for l in outreach_leads),
        "orders": sum(index[l.reached] >= index["order"] for l in outreach_leads),
        "templates": templates[:15],
        "sources": funnel_report(leads)["by_source"],
    }


def lost_examples(leads: list[Lead], limit: int = 8) -> list[Lead]:
    """A spread of recent lost leads, one or two per drop-off reason, for the strategist."""
    lost = sorted((l for l in leads if l.stage == "lost" and l.reached != "new"), key=lambda l: l.last_at, reverse=True)
    picked, per_reason = [], Counter()
    for l in lost:
        if per_reason[l.reason] < 2:
            picked.append(l)
            per_reason[l.reason] += 1
        if len(picked) >= limit:
            break
    return picked

