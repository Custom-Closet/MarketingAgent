"""The language model behind drafting, brand building and the strategist.

Two backends behind one small interface:

* ClaudeLLM   - Claude through the Anthropic SDK: the Claude API, or Claude on Amazon Bedrock
                (AnthropicBedrockMantle, SigV4-signed with AWS access keys). Structured outputs
                are enforced by the API.
* ConverseLLM - any Bedrock model (e.g. openai.gpt-5.5, or Claude when only a Bedrock API key is
                available) through boto3's Converse API.
                The reply must be JSON matching the schema; it is validated here and retried
                once with the validation error.

Pick with LLM_PROVIDER=anthropic|bedrock and BEDROCK_MODEL_ID (see config.py).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output) on the Claude API. Bedrock is billed by AWS at its own
# rates, so costs there are shown as unknown.
PRICE_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0)}

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    model: str

    def parse(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T: ...

    def text(self, *, system: str, messages: list[dict], max_tokens: int = 16000, long: bool = False) -> str: ...

    def count_tokens(self, *, system: str, prompt: str) -> int: ...

    def price(self) -> tuple[float, float] | None: ...


def is_claude_model(model_id: str) -> bool:
    """Claude models on Bedrock look like anthropic.claude-…, or apac./global.anthropic.claude-…"""
    return model_id.startswith("claude") or "anthropic." in model_id


class ClaudeLLM:
    def __init__(self, client, model: str, effort: str, *, bedrock: bool = False):
        self.client = client
        self.model = model
        self.effort = effort
        self.bedrock = bedrock

    def _extra(self) -> dict[str, Any]:
        # Server-side refusal fallback exists on the Claude API only, not on Bedrock.
        return {} if self.bedrock else {"betas": [_FALLBACK_BETA], "fallbacks": "default"}

    def _system(self, system: str) -> list[dict]:
        # The system prompt (brand profile) is identical across calls, so cache it.
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    @staticmethod
    def _check(response) -> None:
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("The reply was cut off (max_tokens)")

    def parse(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T:
        response = self.client.beta.messages.parse(
            model=self.model, max_tokens=max_tokens, system=self._system(system),
            messages=[{"role": "user", "content": prompt}], output_config={"effort": self.effort},
            output_format=schema, **self._extra())
        self._check(response)
        if response.parsed_output is None:
            raise LLMError(f"Could not parse the reply (stop_reason={response.stop_reason})")
        return response.parsed_output

    def text(self, *, system: str, messages: list[dict], max_tokens: int = 16000, long: bool = False) -> str:
        kwargs = dict(model=self.model, system=self._system(system), messages=messages,
                      output_config={"effort": self.effort}, **self._extra())
        if long:
            # Long output with thinking on: stream so the request can't hit an HTTP timeout.
            with self.client.beta.messages.stream(max_tokens=64000, **kwargs) as stream:
                response = stream.get_final_message()
        else:
            response = self.client.beta.messages.create(max_tokens=max_tokens, **kwargs)
        self._check(response)
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def count_tokens(self, *, system: str, prompt: str) -> int:
        return self.client.messages.count_tokens(
            model=self.model, system=system, messages=[{"role": "user", "content": prompt}]).input_tokens

    def price(self) -> tuple[float, float] | None:
        return None if self.bedrock else PRICE_PER_MTOK.get(self.model)


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    match = _JSON_BLOCK.search(text)
    return match.group(0) if match else text


class ConverseLLM:
    """Any Bedrock model via the Converse API (boto3 picks up AWS_BEARER_TOKEN_BEDROCK itself)."""

    def __init__(self, model: str, *, region: str, client=None):
        if client is None:
            import boto3
            client = boto3.client("bedrock-runtime", region_name=region)
        self.client = client
        self.model = model

    def _converse(self, system: str, messages: list[dict], max_tokens: int) -> str:
        response = self.client.converse(
            modelId=self.model,
            system=[{"text": system}],
            messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
            inferenceConfig={"maxTokens": max_tokens},
        )
        stop = response.get("stopReason")
        if stop in ("guardrail_intervened", "content_filtered"):
            raise LLMError(f"The model declined this request ({stop})")
        if stop == "max_tokens":
            raise LLMError("The reply was cut off (max_tokens)")
        blocks = response["output"]["message"]["content"]
        return "".join(b.get("text", "") for b in blocks).strip()

    def parse(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T:
        instruction = (f"{prompt}\n\nReply with only a JSON object (no prose, no code fences) that matches "
                       f"this JSON Schema:\n{json.dumps(schema.model_json_schema())}")
        messages = [{"role": "user", "content": instruction}]
        reply = self._converse(system, messages, max_tokens)
        try:
            return schema.model_validate_json(_extract_json(reply))
        except ValidationError as exc:
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": f"That didn't match the schema: {exc.errors()[:3]}. "
                                                     "Reply again with only the corrected JSON object."}]
            reply = self._converse(system, messages, max_tokens)
            try:
                return schema.model_validate_json(_extract_json(reply))
            except ValidationError as again:
                raise LLMError(f"The model's reply didn't match the expected format: {again.errors()[:2]}")

    def text(self, *, system: str, messages: list[dict], max_tokens: int = 16000, long: bool = False) -> str:
        return self._converse(system, messages, 32000 if long else max_tokens)

    def count_tokens(self, *, system: str, prompt: str) -> int:
        return (len(system) + len(prompt)) // 4     # estimate; Converse has no token-count endpoint

    def price(self) -> tuple[float, float] | None:
        return None


def build_llm(*, provider: str, anthropic_model: str, effort: str, bedrock_model: str, bedrock_region: str,
              bedrock_api_key: str) -> LLM:
    """Construct the configured backend."""
    if provider == "bedrock":
        if not bedrock_model:
            raise LLMError("BEDROCK_MODEL_ID must be set when LLM_PROVIDER=bedrock")
        if not bedrock_region:
            raise LLMError("BEDROCK_REGION (or AWS_REGION) must be set when LLM_PROVIDER=bedrock")
        access_key, secret_key = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
        if is_claude_model(bedrock_model) and access_key and secret_key:
            # The Anthropic-on-Bedrock endpoint needs SigV4-signed requests; it rejects Bedrock API keys.
            from anthropic import AnthropicBedrockMantle
            client = AnthropicBedrockMantle(aws_region=bedrock_region, aws_access_key=access_key,
                                            aws_secret_key=secret_key,
                                            aws_session_token=os.environ.get("AWS_SESSION_TOKEN") or None)
            return ClaudeLLM(client, bedrock_model, effort, bedrock=True)
        # Converse accepts the Bedrock API key (AWS_BEARER_TOKEN_BEDROCK) for any model, Claude included.
        return ConverseLLM(bedrock_model, region=bedrock_region)
    import anthropic
    return ClaudeLLM(anthropic.Anthropic(), anthropic_model, effort)
