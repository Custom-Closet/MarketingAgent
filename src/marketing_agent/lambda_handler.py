"""AWS Lambda entry point for the daily run (invoked by EventBridge Scheduler)."""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta

# Keys allowed to come from the Secrets Manager secret (see `agent push-secrets`).
SECRET_KEYS = ("ANTHROPIC_API_KEY", "HUNTER_API_KEY", "IG_ACCESS_TOKEN", "GMAIL_TOKEN_JSON")

# Leave room after the deadline for the database upload and the digest.
SAFETY_MARGIN_SECONDS = 90


def load_secrets() -> None:
    secret_id = os.environ.get("AGENT_SECRET_ID")
    if not secret_id:
        return
    import boto3
    raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
    for key, value in json.loads(raw or "{}").items():
        if key in SECRET_KEYS and value:
            os.environ[key] = value


def handler(event, context):
    load_secrets()

    # Imported after secrets are in the environment, since settings read it at load time.
    from . import daily
    from .clients import build_clients
    from .config import load_settings
    from .storage import open_database

    settings = load_settings()
    deadline = time.monotonic() + context.get_remaining_time_in_millis() / 1000 - SAFETY_MARGIN_SECONDS
    with open_database(settings, owner="lambda", lock_wait_seconds=5 * 60,
                       lock_ttl=timedelta(minutes=20)) as db:
        report = daily.run(settings, db, deadline=deadline, **build_clients(settings, interactive=False))

    if report.errors:
        # Fail the invocation so the CloudWatch alarm emails the team.
        raise RuntimeError("Daily run finished with errors: " + "; ".join(report.errors))
    return {"stats": report.stats, "replies": len(report.replies)}
