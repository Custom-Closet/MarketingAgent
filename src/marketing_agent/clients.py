"""Construct API clients from settings. Each is None when its credentials are not configured."""

from __future__ import annotations

from typing import Any

from .config import Settings


def gmail_client(settings: Settings, *, interactive: bool = True):
    from .channels.gmail import GmailChannel, authorize, credentials_from_json
    if settings.gmail_token_json:
        creds = credentials_from_json(settings.gmail_token_json)
    elif interactive or settings.gmail_token_file.exists():
        creds = authorize(settings.gmail_credentials_file, settings.gmail_token_file)
    else:
        return None
    return GmailChannel(creds, settings.sender_email)


def hunter_client(settings: Settings):
    from .sources.hunter import HunterClient
    return HunterClient(settings.hunter_api_key) if settings.hunter_api_key else None


def instagram_client(settings: Settings):
    from .sources.instagram import InstagramClient
    if not settings.ig_configured:
        return None
    return InstagramClient(settings.ig_access_token, settings.ig_user_id, page_id=settings.ig_page_id,
                           api_version=settings.graph_api_version)


def llm_configured(settings: Settings) -> bool:
    """True when the configured model backend has credentials (see llm.py)."""
    import os
    if settings.llm_provider == "bedrock":
        aws_keys = os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
        return bool(settings.bedrock_model_id and settings.bedrock_region
                    and (settings.bedrock_api_key or aws_keys or os.environ.get("AWS_PROFILE")))
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip())


def llm(settings: Settings):
    from .llm import build_llm
    return build_llm(provider=settings.llm_provider, anthropic_model=settings.anthropic_model,
                     effort=settings.anthropic_effort, bedrock_model=settings.bedrock_model_id,
                     bedrock_region=settings.bedrock_region, bedrock_api_key=settings.bedrock_api_key)


def describe_llm(settings: Settings) -> str:
    if settings.llm_provider == "bedrock":
        return f"Bedrock {settings.bedrock_model_id} ({settings.bedrock_region})"
    return f"Claude API {settings.anthropic_model}"


def writer(settings: Settings):
    from .personalize import Personalizer
    return Personalizer(settings.brand_file, llm=llm(settings))


def build_clients(settings: Settings, *, interactive: bool) -> dict[str, Any]:
    return {
        "gmail": gmail_client(settings, interactive=interactive),
        "hunter": hunter_client(settings),
        "ig": instagram_client(settings),
        "writer": writer(settings),
    }
