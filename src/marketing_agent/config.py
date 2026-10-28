"""Runtime settings, loaded from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    # Storage
    data_dir: Path
    db_path: Path
    db_s3_uri: str          # s3://bucket/key - share one database between Lambda and the CLI
    brand_file: Path
    faq_file: Path          # seed answers for the inbox (faqs.json, gitignored)

    # Language model (see llm.py): Claude API, or any model on Amazon Bedrock
    llm_provider: str       # "anthropic" or "bedrock"
    anthropic_model: str
    anthropic_effort: str
    bedrock_model_id: str   # e.g. anthropic.claude-opus-5-5 or openai.gpt-5.5
    bedrock_region: str
    bedrock_api_key: str    # AWS_BEARER_TOKEN_BEDROCK (Bedrock-scoped API key)

    # Hunter.io
    hunter_api_key: str

    # Gmail
    gmail_credentials_file: Path
    gmail_token_file: Path
    gmail_token_json: str   # authorized-user JSON; used on Lambda instead of the token file
    sender_name: str
    sender_email: str

    # Compliance (CAN-SPAM / GDPR basics)
    physical_address: str

    # Sending limits
    daily_email_cap: int
    min_delay_seconds: int
    max_delay_seconds: int
    followup_after_days: int
    max_followups: int

    # Daily job
    digest_email: str
    daily_draft_limit: int
    daily_discovery_limit: int

    # Instagram Graph API (Facebook Login for Business)
    ig_access_token: str
    ig_user_id: str
    ig_page_id: str
    graph_api_version: str

    # Brand finder (brands/): daily list of emerging womenswear brands
    brave_search_api_key: str   # web search for keyword / city sweeps (or SERPER_API_KEY)
    serper_api_key: str
    brand_leads_per_region: int
    brand_search_queries: int   # web searches per run
    brand_lookup_limit: int     # Business Discovery lookups per run
    brand_hashtag_budget: int   # of Meta's 30 unique hashtags / 7 days; 0 = skip (captions carry no @mentions)
    brand_leads_to_pipeline: bool  # also add delivered brands as leads, so DMs get drafted for review

    @property
    def ig_configured(self) -> bool:
        return bool(self.ig_access_token and self.ig_user_id)


def _llm_provider() -> str:
    """LLM_PROVIDER if set; otherwise Bedrock when a Bedrock model is configured and no Claude API key is."""
    explicit = _env("LLM_PROVIDER").lower()
    if explicit:
        if explicit not in ("anthropic", "bedrock"):
            raise ValueError("LLM_PROVIDER must be 'anthropic' or 'bedrock'")
        return explicit
    has_claude_key = bool(_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN"))
    return "bedrock" if _env("BEDROCK_MODEL_ID") and not has_claude_key else "anthropic"


def load_settings() -> Settings:
    data_dir = Path(_env("AGENT_DATA_DIR", "data"))
    return Settings(
        data_dir=data_dir,
        db_path=Path(_env("AGENT_DB_PATH", str(data_dir / "agent.db"))),
        db_s3_uri=_env("AGENT_DB_S3_URI"),
        brand_file=Path(_env("BRAND_FILE", "brand.md")),
        faq_file=Path(_env("FAQ_FILE", "faqs.json")),
        llm_provider=_llm_provider(),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=_env("ANTHROPIC_EFFORT", "medium"),
        bedrock_model_id=_env("BEDROCK_MODEL_ID"),
        bedrock_region=_env("BEDROCK_REGION") or _env("AWS_REGION") or _env("AWS_DEFAULT_REGION"),
        bedrock_api_key=_env("AWS_BEARER_TOKEN_BEDROCK"),
        hunter_api_key=_env("HUNTER_API_KEY"),
        gmail_credentials_file=Path(_env("GMAIL_CREDENTIALS_FILE", "credentials.json")),
        gmail_token_file=Path(_env("GMAIL_TOKEN_FILE", str(data_dir / "gmail_token.json"))),
        gmail_token_json=_env("GMAIL_TOKEN_JSON"),
        sender_name=_env("SENDER_NAME"),
        sender_email=_env("SENDER_EMAIL"),
        physical_address=_env("PHYSICAL_ADDRESS"),
        daily_email_cap=_env_int("DAILY_EMAIL_CAP", 30),
        min_delay_seconds=_env_int("MIN_DELAY_SECONDS", 45),
        max_delay_seconds=_env_int("MAX_DELAY_SECONDS", 180),
        followup_after_days=_env_int("FOLLOWUP_AFTER_DAYS", 4),
        max_followups=_env_int("MAX_FOLLOWUPS", 2),
        digest_email=_env("DIGEST_EMAIL") or _env("SENDER_EMAIL"),
        daily_draft_limit=_env_int("DAILY_DRAFT_LIMIT", 20),
        daily_discovery_limit=_env_int("DAILY_DISCOVERY_LIMIT", 10),
        ig_access_token=_env("IG_ACCESS_TOKEN"),
        ig_user_id=_env("IG_USER_ID"),
        ig_page_id=_env("IG_PAGE_ID"),
        graph_api_version=_env("GRAPH_API_VERSION", "v23.0"),
        brave_search_api_key=_env("BRAVE_SEARCH_API_KEY"),
        serper_api_key=_env("SERPER_API_KEY"),
        brand_leads_per_region=_env_int("BRAND_LEADS_PER_REGION", 20),
        brand_search_queries=_env_int("BRAND_SEARCH_QUERIES", 100),
        brand_lookup_limit=_env_int("BRAND_LOOKUP_LIMIT", 300),
        brand_hashtag_budget=_env_int("BRAND_HASHTAG_BUDGET", 0),
        brand_leads_to_pipeline=_env("BRAND_LEADS_TO_PIPELINE", "1").lower() in ("1", "true", "yes"),
    )
