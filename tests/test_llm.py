"""Model backends: Claude (API or Bedrock) and any Bedrock model via Converse."""

import json

import pytest
from pydantic import BaseModel

from marketing_agent.llm import ClaudeLLM, ConverseLLM, LLMError, build_llm, is_claude_model


class Draft(BaseModel):
    message: str


def _claude_client(captured):
    import anthropic
    import httpx2

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
            "content": [{"type": "text", "text": json.dumps({"message": "Hi!"})}],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    return anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def test_claude_on_bedrock_omits_claude_api_only_fields():
    captured = {}
    llm = ClaudeLLM(_claude_client(captured), "anthropic.claude-opus-5-5", "medium", bedrock=True)
    assert llm.parse(system="brand", prompt="reply", schema=Draft).message == "Hi!"
    body = captured["body"]
    assert body["model"] == "anthropic.claude-opus-5-5" and "fallbacks" not in body
    assert "server-side-fallback" not in (captured["beta"] or "")
    assert body["output_config"]["format"]["type"] == "json_schema" and body["output_config"]["effort"] == "medium"
    assert llm.price() is None                                  # billed by AWS


def test_claude_api_keeps_fallbacks_and_price():
    captured = {}
    llm = ClaudeLLM(_claude_client(captured), "claude-opus-5-5", "medium")
    llm.parse(system="brand", prompt="reply", schema=Draft)
    assert captured["body"]["fallbacks"] == "default" and llm.price() == (4.0, 20.0)


class FakeBedrock:
    """boto3 bedrock-runtime stand-in: returns queued replies and records requests."""

    def __init__(self, *replies, stop="end_turn"):
        self.replies, self.stop, self.requests = list(replies), stop, []

    def converse(self, **kwargs):
        self.requests.append(kwargs)
        return {"output": {"message": {"role": "assistant", "content": [{"text": self.replies.pop(0)}]}},
                "stopReason": self.stop}


def test_converse_parse_validates_json_and_maps_messages():
    bedrock = FakeBedrock('```json\n{"message": "Hello from GPT"}\n```')
    llm = ConverseLLM("openai.gpt-5.5", region="ap-south-1", client=bedrock)
    assert llm.parse(system="brand", prompt="reply to them", schema=Draft).message == "Hello from GPT"
    [req] = bedrock.requests
    assert req["modelId"] == "openai.gpt-5.5" and req["system"] == [{"text": "brand"}]
    content = req["messages"][0]["content"][0]["text"]
    assert content.startswith("reply to them") and '"message"' in content     # schema included


def test_converse_parse_retries_once_with_the_validation_error():
    bedrock = FakeBedrock('{"msg": "wrong key"}', 'Sure: {"message": "fixed"}')
    llm = ConverseLLM("openai.gpt-5.5", region="ap-south-1", client=bedrock)
    assert llm.parse(system="s", prompt="p", schema=Draft).message == "fixed"
    retry = bedrock.requests[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"] and "didn't match" in retry[2]["content"][0]["text"]

    with pytest.raises(LLMError, match="expected format"):
        ConverseLLM("m", region="r", client=FakeBedrock("nope", "still nope")).parse(system="s", prompt="p", schema=Draft)


def test_converse_text_and_stop_reasons():
    llm = ConverseLLM("openai.gpt-5.5", region="ap-south-1", client=FakeBedrock("## Advice"))
    assert llm.text(system="s", messages=[{"role": "user", "content": "q"}]) == "## Advice"
    with pytest.raises(LLMError, match="declined"):
        ConverseLLM("m", region="r", client=FakeBedrock("x", stop="guardrail_intervened")).text(
            system="s", messages=[{"role": "user", "content": "q"}])
    with pytest.raises(LLMError, match="cut off"):
        ConverseLLM("m", region="r", client=FakeBedrock("x", stop="max_tokens")).text(
            system="s", messages=[{"role": "user", "content": "q"}])
    assert llm.count_tokens(system="ab", prompt="cdef") == 1 and llm.price() is None


def test_build_llm_picks_backend_by_model(tmp_path, monkeypatch):
    # Keep boto3 away from the developer's own ~/.aws files.
    for var in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"):
        empty = tmp_path / var.lower()
        empty.write_text("")
        monkeypatch.setenv(var, str(empty))
    assert is_claude_model("anthropic.claude-opus-5-5") and is_claude_model("apac.anthropic.claude-sonnet-5-5")
    assert not is_claude_model("openai.gpt-5.5")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    claude = build_llm(provider="bedrock", anthropic_model="", effort="medium", bedrock_model="anthropic.claude-opus-5-5",
                       bedrock_region="ap-south-1", bedrock_api_key="bedrock-key")
    assert isinstance(claude, ClaudeLLM) and claude.bedrock
    assert "bedrock-mantle.ap-south-1" in str(claude.client.base_url)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")                 # API key only: Claude goes through Converse
    claude_converse = build_llm(provider="bedrock", anthropic_model="", effort="medium",
                                bedrock_model="global.anthropic.claude-opus-5-5", bedrock_region="ap-south-1",
                                bedrock_api_key="bedrock-key")
    assert isinstance(claude_converse, ConverseLLM)
    other = build_llm(provider="bedrock", anthropic_model="", effort="medium", bedrock_model="openai.gpt-5.5",
                      bedrock_region="ap-south-1", bedrock_api_key="bedrock-key")
    assert isinstance(other, ConverseLLM) and other.client.meta.region_name == "ap-south-1"
    with pytest.raises(LLMError):
        build_llm(provider="bedrock", anthropic_model="", effort="m", bedrock_model="", bedrock_region="x",
                  bedrock_api_key="")
