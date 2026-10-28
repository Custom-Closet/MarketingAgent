import time
from datetime import datetime, timedelta, timezone

import pytest

from marketing_agent.brands import criteria, finder, judge, sweeps
from marketing_agent.db import Database
from marketing_agent.sources.instagram import IGProfile, InstagramError, InstagramRateLimited
from marketing_agent.sources.web_search import (SearchResult, handle_from_instagram_url, instagram_handles_in_html,
                                                snippet_stats)


def graph_time(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def profile(username, *, bio="Contemporary womenswear label, small batch. Brooklyn, NY", followers=2400,
            media_count=80, days_ago=3, captions=("New drop",), name=None, website="") -> IGProfile:
    return IGProfile(username=username, name=name or username.title(), biography=bio, website=website,
                     followers_count=followers, media_count=media_count,
                     recent_media=[{"timestamp": graph_time(days_ago + i), "caption": c}
                                   for i, c in enumerate(captions)])


class FakeIG:
    def __init__(self, profiles: dict[str, IGProfile], hashtags: dict[str, list[str]] | None = None,
                 rate_limit_after: int | None = None):
        self.profiles, self.hashtags = profiles, hashtags or {}
        self.lookups: list[str] = []
        self.rate_limit_after = rate_limit_after

    def business_discovery(self, username, *, media_limit=0):
        if self.rate_limit_after is not None and len(self.lookups) >= self.rate_limit_after:
            raise InstagramRateLimited("slow down")
        self.lookups.append(username)
        if username not in self.profiles:
            raise InstagramError("not a business account", 100)
        return self.profiles[username]

    def hashtag_captions(self, tag, **_):
        return self.hashtags.get(tag, [])


class FakeSearch:
    name = "fake"

    def __init__(self, results: dict[str, list[SearchResult]]):
        self.results, self.queries = results, []

    def search(self, query, *, count=20):
        self.queries.append(query)
        return [r for key, rs in self.results.items() if key in query for r in rs]


def ig_result(handle, followers="2,100", posts="85"):
    return SearchResult(f"https://www.instagram.com/{handle}/", f"{handle.title()} (@{handle}) • Instagram",
                        f"{followers} Followers, 300 Following, {posts} Posts - See Instagram photos and videos")


@pytest.fixture
def db():
    return Database(":memory:")


# ---- parsing ----------------------------------------------------------------

def test_instagram_url_and_snippet_parsing():
    assert handle_from_instagram_url("https://www.instagram.com/label.studio/") == "label.studio"
    assert handle_from_instagram_url("https://instagram.com/label.studio/p/C1x2/") == "label.studio"
    assert handle_from_instagram_url("https://www.instagram.com/p/C1x2/") is None
    assert handle_from_instagram_url("https://www.instagram.com/reel/abc") is None
    assert snippet_stats("1.2K Followers, 10 Following, 45 Posts - See") == (1200, 45)
    assert snippet_stats("12,345 Followers") == (12345, None)
    html = '<a href="https://www.instagram.com/thelinenlabel/">IG</a><a href="https://instagram.com/p/xyz">'
    assert instagram_handles_in_html(html) == {"thelinenlabel"}


# ---- criteria -----------------------------------------------------------------

@pytest.mark.parametrize("kwargs, reason", [
    ({"followers": 12_000}, "followers"),
    ({"media_count": 2}, "only 2 posts"),
    ({"media_count": 1500}, "established"),
    ({"days_ago": 90}, "inactive"),
    ({"bio": "Curated boutique, multi-designer store"}, "excluded keyword"),
])
def test_hard_filters_reject(kwargs, reason):
    screened, why = criteria.hard_filters(profile("x", **kwargs))
    assert screened is None and reason in why


def test_keyword_verdict_accepts_and_rejects():
    ok, _ = criteria.hard_filters(profile("a", bio="Small batch womenswear label. Handmade in Jaipur"))
    lead = criteria.keyword_verdict(ok, {"us", "in"}).lead
    assert lead.region == "in" and "small batch" in lead.signals and lead.tier == "ideal"

    ethnic, _ = criteria.hard_filters(profile("b", bio="Handcrafted kurta sets and sarees. Mumbai"))
    assert "ethnicwear" in criteria.keyword_verdict(ethnic, {"in"}).reason

    fusion, _ = criteria.hard_filters(profile("c", bio="Indo-western kurta co-ords and dresses. Delhi"))
    assert criteria.keyword_verdict(fusion, {"in"}).lead


def test_region_from_captions_and_us_indian_signals():
    s, _ = criteria.hard_filters(profile("d", bio="Clothing label for her, small batch",
                                         captions=["Pop-up in Austin this weekend", "Hand block print cotton"]))
    lead = criteria.keyword_verdict(s, {"us"}).lead
    assert lead.region == "us"
    assert "indian:hand block" in lead.signals


def test_region_ignores_shipping_phrases():
    assert criteria.infer_region("Label", "Womenswear. Ships to USA and Canada", "") is None
    assert criteria.infer_region("Label", "Womenswear. Ships to USA", "https://label.co.in") == "in"
    assert criteria.infer_region("Label", "Designed in London 🇬🇧", "") == "gb"
    assert criteria.infer_region("Label", "Toronto based label", "") == "ca"
    assert criteria.infer_region("Label", "Georgia based label", "") is None     # the US state or the country


# ---- judge --------------------------------------------------------------------

class FakeLLM:
    model = "fake"

    def __init__(self, verdicts):
        self.verdicts, self.calls = verdicts, 0

    def parse(self, *, system, prompt, schema, max_tokens=16000):
        self.calls += 1
        return schema(verdicts=[v for v in self.verdicts if v["username"] in prompt])


def test_judge_uses_code_region_first_and_model_otherwise():
    a, _ = criteria.hard_filters(profile("a", bio="Linen sets for slow summers"))            # no region in text
    b, _ = criteria.hard_filters(profile("b", bio="Saree drapes. Mumbai"))
    c, _ = criteria.hard_filters(profile("c", bio="Everyday dresses. Brooklyn"))
    llm = FakeLLM([
        {"username": "a", "contemporary_womenswear": True, "country": "in", "founder": "Asha Rao", "reason": "own label"},
        {"username": "b", "contemporary_womenswear": False, "country": "in", "founder": "", "reason": "ethnicwear"},
        {"username": "c", "contemporary_womenswear": True, "country": "in", "founder": "", "reason": "own label"},
    ])
    judge.judge(llm, [a, b, c], {"us", "in"})
    assert (a.lead.region, a.lead.founder) == ("in", "Asha Rao")
    assert b.lead is None and "ethnicwear" in b.reason
    assert c.lead.region == "us"     # Brooklyn in the bio beats the model


# ---- sweeps -------------------------------------------------------------------

def test_pool_drops_big_snippets_and_orders_by_promise():
    pool = sweeps.CandidatePool(skip={"seen"})
    pool.add("seen", "search")
    pool.add("huge", "search", followers=50_000)
    pool.add("tagged", "hashtag")
    pool.add("small", "search", followers=900)
    pool.add("neighbour", "snowball")
    assert pool.dropped == 1
    assert [pool.take(set()).username for _ in range(3)] == ["small", "neighbour", "tagged"]
    assert pool.take(set()) is None


def test_search_queries_are_balanced_and_cover_us_indian_terms():
    qs = sweeps.search_queries({"us", "in"})
    first = qs[:20]
    assert {r for _, _, r in first} == {"us", "in"}
    assert any('"chikankari"' in q for q, _, r in qs if r == "us")
    assert any(kind == "site" for _, kind, _ in qs)


def test_hashtag_budget(db):
    now = datetime.now(timezone.utc)
    for tag in sweeps.HASHTAG_POOL[:24]:
        db.record_hashtag(tag, now - sweeps.HASHTAG_WINDOW)
    tags = sweeps.hashtags_for_run(db, 25, cursor=0, now=now)
    new = [t for t in tags if t not in sweeps.HASHTAG_POOL[:24]]
    assert len(new) == 1 and len(tags) == sweeps.HASHTAGS_PER_RUN


# ---- end to end ----------------------------------------------------------------

def test_find_brands_end_to_end(db):
    search = FakeSearch({
        '"Brooklyn"': [ig_result("brooklynlinen"), ig_result("bigbrand", followers="40.2K")],
        '"Jaipur"': [ig_result("jaipurdresses")],
    })
    profiles = {
        "brooklynlinen": profile("brooklynlinen", captions=["Shot with @sisterlabel.nyc"]),
        "jaipurdresses": profile("jaipurdresses", bio="Small batch dresses, handmade in Jaipur"),
        "sisterlabel.nyc": profile("sisterlabel.nyc", bio="Womenswear label, NYC. Made to order"),
        "tagonly": profile("tagonly", bio="Photographer in Brooklyn"),
    }
    ig = FakeIG(profiles, hashtags={"emergingdesigner": ["Wearing @tagonly"]})
    run = finder.find_brands(db, ig=ig, search=search, max_per_country=2, search_queries_limit=500, hashtag_budget=25,
                             deadline=time.monotonic() + 60, log=lambda _: None)

    assert {l.username for l in run.leads["us"]} == {"brooklynlinen", "sisterlabel.nyc"}
    assert [l.username for l in run.leads["in"]] == ["jaipurdresses"]
    assert "bigbrand" not in ig.lookups                  # dropped from its snippet, no API call spent
    assert "snowball" in next(l for l in run.leads["us"] if l.username == "sisterlabel.nyc").sources
    assert db.brand_candidate("tagonly")["status"] == "rejected"

    subject, body = finder.render(run)
    assert "2 US + 1 IN" in subject
    assert "https://www.instagram.com/jaipurdresses/" in body
    assert "Only 3 of 4 qualified today; nothing was padded." in body
    assert "United States (2)" in body and "India (1)" in body

    # Next day: nothing is delivered or verified again; yesterday's leads are only mined once for mentions.
    ig.lookups.clear()
    again = finder.find_brands(db, ig=ig, search=search, max_per_country=2, search_queries_limit=500, hashtag_budget=25,
                               deadline=time.monotonic() + 60, log=lambda _: None)
    assert not any(again.leads.values())
    assert sorted(ig.lookups) == ["brooklynlinen", "jaipurdresses", "sisterlabel.nyc"]


def test_extra_leads_wait_in_backlog(db):
    profiles = {f"label{i}": profile(f"label{i}") for i in range(3)}
    search = FakeSearch({'"Brooklyn"': [ig_result(h) for h in profiles]})
    llm = FakeLLM([{"username": h, "contemporary_womenswear": True, "country": "us", "founder": "", "reason": "ok"}
                   for h in profiles])
    first = finder.find_brands(db, ig=FakeIG(profiles), search=search, llm=llm, countries=("us",), max_per_country=2,
                               search_queries_limit=500, deadline=time.monotonic() + 60, log=lambda _: None)
    assert len(first.leads["us"]) == 2              # the batch judged 3; one waits
    second = finder.find_brands(db, ig=FakeIG({}), countries=("us",), max_per_country=2,
                                deadline=time.monotonic() + 60, log=lambda _: None)
    assert len(second.leads["us"]) == 1 and second.stats["backlog"] == 1


def test_rate_limit_stops_and_keeps_what_was_found(db, tmp_path):
    profiles = {f"label{i}": profile(f"label{i}") for i in range(4)}
    search = FakeSearch({'"Brooklyn"': [ig_result(h) for h in profiles]})
    run = finder.find_brands(db, ig=FakeIG(profiles, rate_limit_after=2), search=search, countries=("us",),
                             max_per_country=20, search_queries_limit=500, deadline=time.monotonic() + 60,
                             log=lambda _: None)
    assert len(run.leads["us"]) == 2 and "rate limit" in run.stopped
    path = finder.save_run_file(run, tmp_path)
    assert path.name == f"run-{run.run_date}.md" and "label0" in path.read_text()
    assert finder.add_to_pipeline(db, run) == 2
    assert db.conn.execute("SELECT source FROM leads").fetchone()[0] == "brand_finder"


def test_seed_entries(tmp_path):
    from marketing_agent.cli import _seed_entries
    f = tmp_path / "muse.md"
    f.write_text("1. Kora Studio — https://www.instagram.com/korastudio/ — 2.1K\n"
                 "- Ama Label — @ama.label — 900\n"
                 "Linen Days | 3K | lindays.com\n")
    assert _seed_entries(f) == [("korastudio", "Kora Studio"), ("ama.label", "Ama Label"), (None, "Linen Days")]


def test_search_sweep_reads_mentions_from_post_results_and_stops_on_rejections():
    from marketing_agent.sources.web_search import SearchError

    post = SearchResult("https://www.instagram.com/p/CHvxCriA2aP/", "Photo by Behind the Pattern",
                        "@ajaie_alaie is a Brooklyn-based womenswear label")
    pool = sweeps.CandidatePool(skip=set())
    sweeps.search_sweep(FakeSearch({'"Brooklyn"': [post]}), [('x "Brooklyn"', "search", "us")], pool,
                        deadline=time.monotonic() + 10, log=lambda _: None)
    assert "ajaie_alaie" in pool.items

    class Rejecting:
        calls = 0

        def search(self, query, *, count=20):
            Rejecting.calls += 1
            raise SearchError("HTTP 400", 400)

    stats = sweeps.search_sweep(Rejecting(), [("q", "search", "us")] * 50, sweeps.CandidatePool(set()),
                                deadline=time.monotonic() + 10, log=lambda _: None)
    assert Rejecting.calls == sweeps.MAX_REJECTED_SEARCHES and stats["search_errors"] == 3


def test_unchecked_candidates_carry_over_to_next_run(db):
    profiles = {f"label{i}": profile(f"label{i}") for i in range(4)}
    search = FakeSearch({'"Brooklyn"': [ig_result(h) for h in profiles]})
    first = finder.find_brands(db, ig=FakeIG(profiles), search=search, countries=("us",), max_per_country=20,
                               search_queries_limit=500, lookup_limit=1, deadline=time.monotonic() + 60,
                               log=lambda _: None)
    assert len(first.leads["us"]) == 1 and first.stats["queued"] == 3

    ig = FakeIG(profiles)
    second = finder.find_brands(db, ig=ig, countries=("us",), max_per_country=20, deadline=time.monotonic() + 60,
                                log=lambda _: None)
    assert len(second.leads["us"]) == 3 and second.stats["queued"] == 0
    assert "label0" not in {l.username for l in second.leads["us"]}   # only mined for mentions, not re-delivered
