"""Model picker: listing (cheap first), testing access, switching drafting + strategist."""

import pytest

from marketing_agent.db import Database
from marketing_agent.llm import LLMError
from marketing_agent.models import ModelManager, list_bedrock_text_models


class FakeBedrockControl:
    def list_foundation_models(self, byOutputModality):
        return {"modelSummaries": [
            {"modelId": "anthropic.claude-opus-5-5", "providerName": "Anthropic", "inferenceTypesSupported": ["ON_DEMAND"]},
            {"modelId": "amazon.nova-micro-v1:0", "providerName": "Amazon", "inferenceTypesSupported": ["ON_DEMAND"]},
            {"modelId": "openai.gpt-oss-20b-1:0", "providerName": "OpenAI", "inferenceTypesSupported": ["ON_DEMAND"]},
            {"modelId": "meta.llama3-70b", "providerName": "Meta", "inferenceTypesSupported": ["PROVISIONED"]},
            {"modelId": "old.model", "providerName": "X", "inferenceTypesSupported": ["ON_DEMAND"],
             "modelLifecycle": {"status": "LEGACY"}}]}

    def list_inference_profiles(self):
        return {"inferenceProfileSummaries": [{"inferenceProfileId": "global.anthropic.claude-haiku-4-5"},
                                              {"inferenceProfileId": "amazon.nova-micro-v1:0"}]}


def test_listing_puts_low_cost_models_first_and_skips_unusable():
    models = list_bedrock_text_models("ap-south-1", client=FakeBedrockControl())
    ids = [m["id"] for m in models]
    assert ids[:3] == ["amazon.nova-micro-v1:0", "openai.gpt-oss-20b-1:0", "global.anthropic.claude-haiku-4-5"]
    assert "meta.llama3-70b" not in ids and "old.model" not in ids     # provisioned-only / legacy
    assert ids.count("amazon.nova-micro-v1:0") == 1 and ids[-1] == "anthropic.claude-opus-5-5"
    assert models[0]["low_cost"] and not models[-1]["low_cost"]


class FakeLLM:
    def __init__(self, model, fail=None):
        self.model, self.fail = model, fail

    def text(self, **_):
        if self.fail:
            raise self.fail
        return "OK"


def _manager(db, switched, fail_models=()):
    def factory(model_id):
        err = Exception("openai.gpt-5.5 is not available for this account") if model_id in fail_models else None
        return FakeLLM(model_id, err)
    return ModelManager(db, provider="bedrock", default_model="env.model", factory=factory,
                        lister=lambda: [{"id": "amazon.nova-micro-v1:0"}], on_switch=switched.append)


def test_select_tests_first_then_switches_and_persists():
    db, switched = Database(":memory:"), []
    manager = _manager(db, switched, fail_models={"openai.gpt-5.5"})
    assert manager.current() == "env.model"

    bad = manager.select("openai.gpt-5.5")
    assert not bad["ok"] and "doesn't have access" in bad["error"] and switched == [] and manager.current() == "env.model"

    good = manager.select("amazon.nova-micro-v1:0")
    assert good["ok"] and good["reply"] == "OK"
    assert [l.model for l in switched] == ["amazon.nova-micro-v1:0"] and manager.current() == "amazon.nova-micro-v1:0"
    with pytest.raises(ValueError):
        manager.select("  ")


def test_model_switching_is_bedrock_only():
    manager = ModelManager(Database(":memory:"), provider="anthropic", default_model="", factory=FakeLLM,
                           lister=lambda: 1 / 0, on_switch=lambda llm: None)
    assert manager.available() == []
    with pytest.raises(ValueError):
        manager.select("amazon.nova-micro-v1:0")


def test_llm_errors_are_reported_verbatim():
    db = Database(":memory:")
    manager = ModelManager(db, provider="bedrock", default_model="m",
                           factory=lambda mid: FakeLLM(mid, LLMError("The model declined this request")),
                           lister=list, on_switch=lambda llm: None)
    assert manager.test("m") == {"ok": False, "model": "m", "error": "The model declined this request"}


def test_use_case_form_error_is_explained():
    from marketing_agent.models import _short_error
    msg = _short_error(Exception("ResourceNotFoundException: Model use case details have not been submitted for this account."))
    assert "use-case form" in msg
