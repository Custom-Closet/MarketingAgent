"""Choose the Bedrock model from the app: list what the region offers (cheap first), test access, switch.

The chosen model is stored in app_settings ("bedrock_model_id") and overrides BEDROCK_MODEL_ID.
Switching swaps the model used by "Draft with Claude" and the strategist without a restart.
"""

from __future__ import annotations

import time
from typing import Callable

from .db import Database
from .llm import LLM, LLMError

SETTING_KEY = "bedrock_model_id"

# Substrings of model IDs from low-cost families, cheapest-looking first. Used for ordering and a
# "low cost" tag only - actual prices are on the Bedrock pricing page.
CHEAP_HINTS = ("nova-micro", "nova-lite", "gpt-oss-20b", "gpt-oss-120b", "haiku", "llama3-2-1b", "llama3-2-3b",
               "llama3-1-8b", "ministral", "mistral-small", "gemma", "nova-2-lite")


def _cheap_rank(model_id: str) -> int | None:
    return next((i for i, hint in enumerate(CHEAP_HINTS) if hint in model_id), None)


def list_bedrock_text_models(region: str, client=None) -> list[dict]:
    """Text models callable on demand in `region`, plus cross-region inference profiles."""
    if client is None:
        import boto3
        client = boto3.client("bedrock", region_name=region)
    models = []
    for m in client.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]:
        if "ON_DEMAND" in m.get("inferenceTypesSupported", []) and m.get("modelLifecycle", {}).get("status") != "LEGACY":
            models.append({"id": m["modelId"], "name": m.get("modelName", m["modelId"]), "provider": m.get("providerName", "")})
    for p in client.list_inference_profiles()["inferenceProfileSummaries"]:
        models.append({"id": p["inferenceProfileId"], "name": p.get("inferenceProfileName", p["inferenceProfileId"]),
                       "provider": p["inferenceProfileId"].split(".")[1] if p["inferenceProfileId"].count(".") >= 2 else ""})
    seen, unique = set(), []
    for m in models:
        if m["id"] not in seen:
            seen.add(m["id"])
            rank = _cheap_rank(m["id"])
            unique.append({**m, "low_cost": rank is not None, "_rank": rank if rank is not None else 99})
    unique.sort(key=lambda m: (m["_rank"], m["provider"], m["id"]))
    return [{k: v for k, v in m.items() if k != "_rank"} for m in unique]


class ModelManager:
    def __init__(self, db: Database, *, provider: str, default_model: str, factory: Callable[[str], LLM],
                 lister: Callable[[], list[dict]], on_switch: Callable[[LLM], None]):
        self.db = db
        self.provider = provider
        self.default_model = default_model
        self.factory = factory
        self.lister = lister
        self.on_switch = on_switch
        self._cache: list[dict] | None = None

    def current(self) -> str:
        return self.db.setting(SETTING_KEY) or self.default_model

    def available(self, *, refresh: bool = False) -> list[dict]:
        if self.provider != "bedrock":
            return []
        if self._cache is None or refresh:
            self._cache = self.lister()
        return self._cache

    def test(self, model_id: str) -> dict:
        """One tiny call to check this account can use the model."""
        started = time.monotonic()
        try:
            reply = self.factory(model_id).text(system="Reply with a single word.",
                                                messages=[{"role": "user", "content": "Say OK."}], max_tokens=300)
            return {"ok": True, "model": model_id, "reply": reply[:80], "seconds": round(time.monotonic() - started, 1)}
        except Exception as exc:  # access denied, unknown model, throttling: report, don't crash
            return {"ok": False, "model": model_id, "error": _short_error(exc)}

    def select(self, model_id: str) -> dict:
        """Test the model, then use it for drafting and the strategist if it works."""
        if self.provider != "bedrock":
            raise ValueError("Model switching is for Bedrock (LLM_PROVIDER=bedrock)")
        if not model_id.strip():
            raise ValueError("choose a model")
        result = self.test(model_id)
        if result["ok"]:
            self.on_switch(self.factory(model_id))
            self.db.set_setting(SETTING_KEY, model_id)
        return result


def _short_error(exc: Exception) -> str:
    text = str(exc)
    for marker in ("is not available for this account", "use case details have not been submitted", "does not exist",
                   "AccessDenied", "Throttling", "ValidationException", "on-demand throughput isn"):
        if marker in text:
            if marker == "is not available for this account":
                return "Your AWS account doesn't have access to this model yet (Bedrock console → Model access)."
            if marker == "use case details have not been submitted":
                return ("Anthropic models need a one-time use-case form: Bedrock console → Model catalog → "
                        "any Claude model → submit use case details.")
            if marker == "on-demand throughput isn":
                return "This model needs an inference-profile ID in this region (try the global./apac./in. version)."
            break
    return text[:240] if not isinstance(exc, LLMError) else str(exc)
