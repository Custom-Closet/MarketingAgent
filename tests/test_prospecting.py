"""Marketing > Prospecting: search setup, background runs, tuning, prospects joining the sales funnel."""

import time

import pytest
from conftest import US, seed_chat
from fastapi.testclient import TestClient
from test_brands import FakeIG, FakeLLM as _FakeJudge, FakeSearch, ig_result, profile

from marketing_agent.brands import finder, sweeps
from marketing_agent.brands.config import SearchConfig, Target
from marketing_agent.inbox.api import create_app
from marketing_agent.insights.service import InsightsService
from marketing_agent.prospecting.service import ProspectingService


class FakeTuner:
    """Stands in for the model: proposes adding a city."""
    model = "fake"

    def __init__(self):
        self.prompts = []

    def price(self):
        return (1.0, 5.0)

    def parse(self, *, system, prompt, schema, max_tokens=16000):
        if schema.__name__ == "Verdicts":                 # the brand check during a search: keywords decide
            raise ValueError("not a tuning request")
        self.prompts.append(prompt)
        config = SearchConfig(targets=[{"country": "us", "places": ["Houston", "Edison"]}], keywords=["small batch"])
        return schema(reply="Edison NJ has many South Asian-owned labels.", changes=["Add Edison"], config=config)


def wait_for(service, seconds=5):
    end = time.monotonic() + seconds
    while service.running and time.monotonic() < end:
        time.sleep(0.02)


def test_search_queries_follow_the_setup():
    config = SearchConfig(keywords=["small batch"], targets=[{"country": "us", "places": ["Austin"]},
                          {"country": "in", "places": ["Pune"]}, {"country": "gb", "places": []}],
                          craft_terms=["ikat"], extra_queries=["site:instagram.com desi label nyc"],
                          exclude_terms=["swimwear", "kids wear"])
    qs = sweeps.search_queries({"us", "in", "gb"}, config)
    assert qs[0] == ("site:instagram.com desi label nyc", "search", "")
    texts = [q for q, _, _ in qs]
    assert 'site:instagram.com "small batch" "Austin" -swimwear -"kids wear"' in texts
    assert any('"ikat" womenswear "Austin"' in q for q in texts)
    assert any('"Pune"' in q for q in texts) and not any('"ikat"' in q and "Pune" in q for q in texts)
    assert 'site:instagram.com "small batch" "United Kingdom" -swimwear -"kids wear"' in texts   # no places: the country
    assert {c for _, _, c in qs[1:4]} == {"us", "in", "gb"}                                    # interleaved


def test_runs_record_queries_and_where_each_prospect_came_from(db):
    search = FakeSearch({'"Brooklyn"': [ig_result("brooklynlinen")]})
    search_id = db.start_brand_search(None)
    finder.find_brands(db, ig=FakeIG({"brooklynlinen": profile("brooklynlinen")}), search=search, countries=("us",),
                       max_per_country=5, deadline=time.monotonic() + 30, log=lambda _: None,
                       config=SearchConfig(keywords=["womenswear label"], targets=[{"country": "us", "places": ["Brooklyn"]}]),
                       search_id=search_id)
    row = db.brand_candidate("brooklynlinen")
    assert (row["search_id"], row["stage"]) == (search_id, "found")
    assert row["query"] == 'site:instagram.com "womenswear label" "Brooklyn"'
    by_query = {r["query"]: dict(r) for r in db.query_yield()}
    q = by_query['site:instagram.com "womenswear label" "Brooklyn"']
    assert (q["runs"], q["candidates"], q["leads"]) == (1, 1, 1)


@pytest.fixture
def db():
    from marketing_agent.db import Database
    return Database(":memory:", check_same_thread=False)


@pytest.fixture
def prospecting(db):
    ig = FakeIG({"brooklynlinen": profile("brooklynlinen"), "jaipurdresses": profile(
        "jaipurdresses", bio="Small batch dresses, handmade in Jaipur")})
    search = FakeSearch({'"Brooklyn"': [ig_result("brooklynlinen")], '"Jaipur"': [ig_result("jaipurdresses")]})
    llm = FakeTuner()
    service = ProspectingService(db, ig_factory=lambda: ig, search_factory=lambda: search, llm_getter=lambda: llm)
    service.save_config(SearchConfig(keywords=["womenswear label"], minutes=2,
                                     targets=[{"country": "us", "places": ["Brooklyn"]},
                                              {"country": "in", "places": ["Jaipur"]}]), note="test")
    return service, llm


def test_background_search_finds_prospects(prospecting):
    service, _ = prospecting
    search_id = service.start_search()
    wait_for(service)
    [run] = service.searches()
    assert (run["id"], run["status"], run["leads"]) == (search_id, "done", {"us": 1, "in": 1})
    assert {p["username"] for p in service.prospects()} == {"brooklynlinen", "jaipurdresses"}
    assert any("Done:" in line for line in service.status()["log"])
    with pytest.raises(ValueError):
        service.set_stage("brooklynlinen", "married")


def test_search_needs_instagram_and_a_search_key(db):
    with pytest.raises(RuntimeError, match="Instagram"):
        ProspectingService(db).start_search()
    with pytest.raises(RuntimeError, match="SERPER_API_KEY"):
        ProspectingService(db, ig_factory=lambda: FakeIG({})).start_search()


def test_setup_versions_and_restore(prospecting):
    service, _ = prospecting
    first = service.config()[1]
    service.save_config(SearchConfig(keywords=["boutique", " boutique ", "Boutique"],
                                     targets=[{"country": "US", "places": ["Austin"]}, {"country": "us", "places": ["Austin", "Dallas"]}]))
    assert service.config()[0].targets == [Target(country="us", places=["Austin", "Dallas"])]   # merged
    assert service.config()[0].keywords == ["boutique"]            # cleaned
    service.revert(first)
    assert service.config()[0].targets[0].places == ["Brooklyn"]
    assert service.config_history()[0]["note"] == f"restored version {first}"
    with pytest.raises(ValueError):
        service.save_config(SearchConfig(keywords=[], extra_queries=[]))


def test_tuning_proposes_but_never_saves(prospecting):
    service, llm = prospecting
    with pytest.raises(ValueError):
        service.tune("more NJ brands", [], confirm=False)
    assert service.tune_estimate("more NJ brands", [])["usd"] > 0
    before = service.config()
    out = service.tune("more NJ brands", [], confirm=True)
    assert out["changes"] == ["Add Edison"] and "Edison" in out["config"]["targets"][0]["places"]
    assert service.config() == before
    assert "current_setup" in llm.prompts[0] and "more NJ brands" in llm.prompts[0]


def test_prospects_join_the_sales_funnel(db, prospecting):
    service, _ = prospecting
    service.start_search()
    wait_for(service)
    service.set_stage("jaipurdresses", "shortlisted")
    # We DM'd brooklynlinen; they replied asking for a quote, and we quoted.
    seed_chat(db, "c9", "9", "brooklynlinen", [("us", "Hi! We do small-batch production for new labels", 30),
                                              ("them", "Hi, what's your price for 20 pieces for my brand?", 20),
                                              ("us", "Sample 1800 rs, bulk 1200 rs per piece", 10)])
    insights = InsightsService(db, our_id=US)
    rows = {p["username"]: p for p in service.prospects()}
    assert rows["brooklynlinen"]["stage"] == "contacted"            # a DM exists
    assert rows["jaipurdresses"]["stage"] == "shortlisted"

    steps = {s["stage"]: s["reached"] for s in insights.prospect_funnel()["steps"]}
    assert steps["found"] == 2 and steps["shortlisted"] == 2 and steps["contacted"] == 1
    assert steps["replied"] == 1 and steps["quoted"] == 1 and steps["won"] == 0
    funnel = insights.funnel()
    assert [s["key"] for s in funnel["by_source"]] == ["brand_finder"]


def test_prospecting_api(db, prospecting, inbox):
    _, repo, inbox_service, _ = inbox
    service, _ = prospecting
    from marketing_agent.inbox.repository import InboxRepository
    from marketing_agent.inbox.service import InboxService
    inbox_service = InboxService(InboxRepository(db), our_id=US)
    http = TestClient(create_app(inbox_service, prospecting=service))

    assert http.get("/static/prospecting.js").status_code == 200
    cfg = http.get("/api/prospecting/config").json()
    assert cfg["config"]["targets"][0] == {"country": "us", "places": ["Brooklyn"]} and cfg["history"]
    assert len(http.get("/api/prospecting/countries").json()) > 240
    assert "California" in http.get("/api/prospecting/countries/us/states").json()["states"]
    assert http.post("/api/prospecting/search").status_code == 200
    wait_for(service)
    assert http.post("/api/prospecting/search").status_code == 200   # finished, so a new one may start
    wait_for(service)
    rows = http.get("/api/prospects", params={"region": "in"}).json()
    assert [r["username"] for r in rows] == ["jaipurdresses"] and rows[0]["funnel"] is None
    assert http.post("/api/prospects/jaipurdresses/stage", json={"stage": "dismissed"}).status_code == 200
    assert http.post("/api/prospects/nobody/stage", json={"stage": "found"}).status_code == 404
    assert http.get("/api/prospects/funnel").json()["dismissed"] == 1
    assert http.post("/api/prospecting/tune", json={"message": "x"}).status_code == 400   # cost not confirmed
    assert http.post("/api/prospecting/tune", json={"message": "x", "confirm": True}).json()["changes"]
    bad = {"config": {**cfg["config"], "keywords": [], "extra_queries": []}}
    assert http.post("/api/prospecting/config", json=bad).status_code == 400


def test_old_setups_and_regions_are_upgraded(db):
    old = '{"keywords": ["a"], "usa_cities": ["Austin"], "india_cities": ["Pune"], "us_indian_terms": ["ikat"], "leads_per_region": 15}'
    config = SearchConfig.from_json(old)
    assert [t.country for t in config.targets] == ["us", "in"] and config.craft_terms == ["ikat"]
    assert (config.leads_per_run, config.max_per_country) == (30, 15)
    db.conn.execute("INSERT INTO brand_candidates (username, status, region, checked_at) VALUES ('x', 'lead', 'usa', '2026-01-01')")
    db._migrate()
    assert db.brand_candidate("x")["region"] == "us"


def test_total_quota_and_cap_per_country(db):
    profiles = {f"ny{i}": profile(f"ny{i}") for i in range(4)}
    profiles |= {f"ldn{i}": profile(f"ldn{i}", bio="Womenswear label, small batch. London") for i in range(4)}
    search = FakeSearch({'"Brooklyn"': [ig_result(h) for h in profiles if h.startswith("ny")],
                         '"London"': [ig_result(h) for h in profiles if h.startswith("ldn")]})
    config = SearchConfig(keywords=["label"], targets=[{"country": "us", "places": ["Brooklyn"]},
                                                       {"country": "gb", "places": ["London"]}])
    run = finder.find_brands(db, ig=FakeIG(profiles), search=search, config=config, leads_per_run=5,
                             max_per_country=3, deadline=time.monotonic() + 30, log=lambda _: None)
    assert sum(len(v) for v in run.leads.values()) == 5
    assert max(len(v) for v in run.leads.values()) == 3
    assert {"us", "gb"} == {c for c, v in run.leads.items() if v}
