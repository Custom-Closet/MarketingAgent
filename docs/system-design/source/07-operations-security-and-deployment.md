# Operations, Security and Deployment
_Configuration, deployment, monitoring, security posture, limits and known issues_

## 1. Environments

| Environment | How to run | Database |
|---|---|---|
| Local CLI | `uv run agent <command>` | `data/agent.db`, or the S3 copy when `AGENT_DB_S3_URI` is set |
| Local app | `uv run agent serve [--host 0.0.0.0] [--port 8000] [--no-open]` | Local file only |
| AWS daily job | Lambda `marketing-agent-daily` (EventBridge, 08:00 IST) | `s3://marketing-agent-data-<account>/agent.db` |
| Tests | `PYTHONPATH=src uv run --no-sync pytest -q` | In-memory |

## 2. Configuration (`.env`)

| Group | Variables (defaults) |
|---|---|
| Storage | `AGENT_DATA_DIR` (`data`), `AGENT_DB_PATH` (`data/agent.db`), `AGENT_DB_S3_URI`, `BRAND_FILE` (`brand.md`), `FAQ_FILE` (`faqs.json`) |
| LLM | `LLM_PROVIDER` (auto), `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (`claude-opus-5-5`), `ANTHROPIC_EFFORT` (`medium`), `BEDROCK_MODEL_ID`, `BEDROCK_REGION`, `AWS_BEARER_TOKEN_BEDROCK`, `BEDROCK_USE_ANTHROPIC_SDK` |
| Instagram | `IG_ACCESS_TOKEN`, `IG_USER_ID`, `IG_PAGE_ID` (required with a Page token), `GRAPH_API_VERSION` (`v23.0`) |
| Email | `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE`, `SENDER_NAME`, `SENDER_EMAIL`, `PHYSICAL_ADDRESS`, `DAILY_EMAIL_CAP` (30), `MIN_DELAY_SECONDS` (45), `MAX_DELAY_SECONDS` (180), `FOLLOWUP_AFTER_DAYS` (4), `MAX_FOLLOWUPS` (2), `DIGEST_EMAIL` |
| Discovery | `HUNTER_API_KEY`, `DAILY_DRAFT_LIMIT` (20), `DAILY_DISCOVERY_LIMIT` (10) |
| Infra only | `ALERT_EMAIL`; Lambda sets `AGENT_SECRET_ID` |

In-app settings (stored in the database): context window days, lost-after days, **auto-send trusted FAQ answers (off)**, Bedrock model.

## 3. Deployment

### 3.1 Local app

1. `uv sync`, then copy `.env.example` to `.env` and fill it in. Copy `faqs.example.json` to `faqs.json` and write the real answers. Create `brand.md` (or build it with `agent build-brand`).
2. `uv run agent serve`. It syncs at once, then every 15 minutes, while it is running.
3. Optional: approve FAQ answers in Settings, then tick **Auto-send trusted FAQ answers**.

### 3.2 AWS

1. One-time setup: `npx aws-cdk@latest bootstrap aws://<ACCOUNT_ID>/us-east-1`.
2. `scripts/deploy.sh`. It runs `build_lambda.sh` (arm64 wheels for Python 3.12 into `build/lambda`, copying in `brand.md`), then `cdk deploy MarketingAgent --require-approval broadening`.
3. `agent push-secrets` (after `agent auth-gmail`) and set `AGENT_DB_S3_URI` in the local `.env` from the stack output.
4. Redeploy after changing `brand.md` or any non-secret `.env` value; they are baked in.

| CDK resource | Configuration |
|---|---|
| S3 bucket `marketing-agent-data-<account>` | Versioned, all public access blocked, SSE-S3, SSL enforced, retained, noncurrent versions expire after 90 days |
| Secret `marketing-agent/secrets` | JSON object of whitelisted keys |
| Lambda `marketing-agent-daily` | Python 3.12, arm64, 1024 MB, 15-minute timeout, 0 retries, logs kept for 3 months; read/write/delete on the bucket, read on the secret |
| Scheduler `marketing-agent-daily` | `cron(0 8 * * ? *)` Asia/Kolkata, 0 retries |
| Alarm `DailyErrors` and SNS `Alerts` | Lambda Errors of 1 or more per hour, email subscription |

## 4. Monitoring and runbook

| Signal | Where | Action |
|---|---|---|
| The daily run failed | SNS email; CloudWatch `/aws/lambda/marketing-agent-daily`; the digest's ERRORS section | Read the step error. Steps are isolated; re-run locally with `agent daily` if needed |
| "Database is in use by ..." | CLI | Wait for the Lambda run; locks expire (20 minutes for Lambda, 2 hours for the CLI) |
| Sync failed or stalled | Chat stats line, Settings status (`last_error`) | Rate limit: wait, since a sync stops at 80% usage. Token expired: renew `IG_ACCESS_TOKEN` |
| Auto-send held a draft | Card shows "auto-send held" plus an error | Send or edit it by hand. Held drafts are never retried on purpose |
| Graph `(#10) outside of allowed window` | Draft error | Window passed (23 hours), or the Page is not subscribed to `messages` |
| Gmail 4xx / quota | Digest, `agent status` | Check the cap and the OAuth token (`agent auth-gmail`) |

Useful commands: `agent status`, `aws lambda invoke --function-name marketing-agent-daily --invocation-type Event /dev/null`.

## 5. Security and privacy

| Area | Control |
|---|---|
| Secrets | Never committed (`.gitignore`); in AWS only in Secrets Manager, and `load_secrets` copies only whitelisted keys; Lambda environment carries no secrets; Gmail token `chmod 600` |
| Tokens in logs | Graph paging URLs (which contain the token) are scrubbed from responses; the leads script never logs raw network errors |
| Web app exposure | No authentication; binds to `127.0.0.1` by default. `--host 0.0.0.0` exposes sending to the LAN, so use it only on a trusted network. A login is planned before any hosted deployment (system design plan, Phase 3) |
| Outbound safety | Mandatory approval and confirmation; send-time gates; suppression list; one open draft per customer; at-most-once send guard; auto-send is opt-in, limited to trusted unchanged answers, has a 60-second veto, re-checks Instagram live, and holds on failure |
| Data minimisation | The strategist gets no usernames, and phone numbers and emails are masked; summaries leave out contact details; business files are gitignored |
| Compliance | CAN-SPAM footer, physical address and `List-Unsubscribe`; Meta 24-hour window enforced with a 1-hour margin; no unofficial Instagram automation; India DPDP Act items (privacy notice, deletion) are on the roadmap |
| Supply chain | Locked dependencies (`uv.lock`), binary-only wheels for Lambda |

## 6. Limits and capacity

| Limit | Value | Reason |
|---|---|---|
| Graph API pace | 1 call/s; stop at 80% usage | Protect the account |
| Instagram reply window | 23 h (Meta allows 24 h) | Safety margin |
| Send batch | 25 replies | Bounded confirm dialog and run time |
| Auto-send delay | 60 s | Time for a person to step in |
| Trust threshold | 3 hand approvals | Proven answer |
| Background sync | Every 15 minutes | Fresh enough without heavy API use; webhooks are planned |
| Email cap | 30 per rolling 24 h, 45–180 s gaps | Deliverability; ramp a new Workspace inbox at 10–20 a day |
| SQLite | One writer process at a time | The S3 lock serialises CLI and Lambda; `serve` is local only |

## 7. Known issues and follow-ups

1. **Moving a lead to "Lost" by hand breaks the funnel.** If the rules hadn't already marked it lost, its `reason` is `None`, and `funnel_report` raises `KeyError` while building drop-offs. That breaks `/api/funnel`, `/api/marketing` and the strategist. It was reproduced while writing these docs and is not yet fixed.
2. **No Bedrock IAM permission on the Lambda.** Bedrock there works only through `AWS_BEARER_TOKEN_BEDROCK` in the secret. `BEDROCK_USE_ANTHROPIC_SDK` is not passed to the Lambda.
3. **A missing `brand.md` stops the daily run early.** `build_clients` always builds the writer, so the run fails before step isolation begins.
4. The strategist's "not configured" message mentions `ANTHROPIC_API_KEY` even on Bedrock.
5. **Auto-send and the 15-minute sync only run while `agent serve` is running.** A hosted single server with Meta webhooks (Phase 3) would make replies instant and keep them going while the laptop is off.
6. The README's project-structure section leaves out the newer modules (`models.py`, `llm.py`, `context.py`, `brand_builder.py`, `insights/`, `inbox/`).
