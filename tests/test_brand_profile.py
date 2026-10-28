"""Settings > Brand profile: edit brand.md with versions, tune it with the model, founder voice in drafts."""

import pytest
from conftest import US
from fastapi.testclient import TestClient

from marketing_agent.brand_profile import BrandProfileService, parse_tune_reply
from marketing_agent.db import Database
from marketing_agent.inbox.api import create_app
from marketing_agent.inbox.repository import InboxRepository
from marketing_agent.inbox.service import InboxService
from marketing_agent.personalize import FOUNDER_VOICE, Personalizer

PROFILE = "# Brand profile\n\n## Who we are\nCustom Closet, a one-person studio in Jaipur.\n"


class FakeLLM:
    model = "fake"

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def price(self):
        return (1.0, 5.0)

    def text(self, *, system, messages, max_tokens=16000, long=False):
        self.calls.append((system, messages))
        return self.reply


@pytest.fixture
def brand(tmp_path):
    path = tmp_path / "brand.md"
    path.write_text("# Brand profile\n<Brand name, what you sell, price range>\n")
    (tmp_path / "brand.draft.md").write_text(PROFILE)
    reloads = []
    llm = FakeLLM("<reply>Made it humbler.</reply>\n<changes>\n- Removed 'premium'\n</changes>\n"
                  "<profile>\n# Brand profile\n\nSimple, honest stitching by Tanya.\n</profile>")
    service = BrandProfileService(Database(":memory:", check_same_thread=False), path, llm_getter=lambda: llm,
                                  on_change=lambda: reloads.append(path.read_text()))
    return service, path, llm, reloads


def test_template_draft_save_versions_and_restore(brand):
    service, path, _, reloads = brand
    state = service.state()
    assert state["is_template"] and state["draft_available"] and state["versions"] == []
    first = service.save(service.draft(), note="from chats")
    assert path.read_text() == PROFILE and reloads == [PROFILE]          # drafting reloads at once
    versions = service.state()["versions"]
    assert [v["created_by"] for v in versions] == ["user", "file"]       # the template was kept too
    service.save(PROFILE.replace("Jaipur", "Jaipur, India"))
    service.restore(first)
    assert path.read_text() == PROFILE and not service.state()["is_template"]
    with pytest.raises(ValueError):
        service.save("  ")
    with pytest.raises(ValueError):
        service.save("x" * 70_000)


def test_tuning_proposes_a_profile_and_saves_nothing(brand):
    service, path, llm, _ = brand
    before = path.read_text()
    with pytest.raises(ValueError):
        service.tune(PROFILE, "make it humbler", [], confirm=False)
    assert service.tune_estimate(PROFILE, "make it humbler", [])["usd"] > 0
    out = service.tune(PROFILE, "make it humbler", [], confirm=True)
    assert out == {"reply": "Made it humbler.", "changes": ["Removed 'premium'"],
                   "profile": "# Brand profile\n\nSimple, honest stitching by Tanya."}
    assert path.read_text() == before
    system, messages = llm.calls[0]
    assert "one person" in system and "<current_profile>" in messages[0]["content"]


def test_parse_tune_reply_question_and_cut_off():
    assert parse_tune_reply("<reply>Your MOQ is 1.</reply><changes></changes>") == \
        {"reply": "Your MOQ is 1.", "changes": [], "profile": None}
    with pytest.raises(ValueError, match="cut off"):
        parse_tune_reply("<reply>ok</reply><profile># Brand profile\n## Who")


def test_drafts_always_carry_the_founder_voice(tmp_path):
    path = tmp_path / "brand.md"
    path.write_text(PROFILE)
    system = Personalizer(path, llm=None).system
    assert FOUNDER_VOICE in system and "one-person business" in system and "Let me check" in system
    assert system.index(FOUNDER_VOICE) < system.index("<brand>")


def test_brand_api(brand):
    service, path, _, _ = brand
    inbox = InboxService(InboxRepository(Database(":memory:", check_same_thread=False)), our_id=US)
    http = TestClient(create_app(inbox, brand=service))
    assert http.get("/static/brand.js").status_code == 200
    assert http.get("/api/brand").json()["is_template"]
    assert http.get("/api/brand/draft").json()["content"] == PROFILE
    version = http.post("/api/brand", json={"content": PROFILE, "note": "test"}).json()["version"]
    assert http.get(f"/api/brand/versions/{version}").json()["content"] == PROFILE
    assert http.get("/api/brand/versions/999").status_code == 404
    assert http.post("/api/brand", json={"content": ""}).status_code == 400
    assert http.post("/api/brand/tune", json={"content": PROFILE, "message": "x"}).status_code == 400
    r = http.post("/api/brand/tune", json={"content": PROFILE, "message": "humbler", "confirm": True}).json()
    assert r["profile"].startswith("# Brand profile")
    assert TestClient(create_app(inbox)).get("/api/brand").status_code == 503
