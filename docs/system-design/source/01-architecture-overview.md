# Architecture Overview
_What the Custom Closet marketing agent does, how it is put together, and the rules it never breaks_

## 1. Purpose

The marketing agent runs sales and outreach for an Instagram-first custom womenswear manufacturer (@customcloset.in). It does four jobs:

- **Find leads.** Hunter.io domain search, Instagram Business Discovery and hashtag @-mentions, or a CSV import.
- **Draft messages.** An LLM (the Claude API, or any model on Amazon Bedrock) writes personalised cold emails, Instagram DMs and replies to inbound DMs. The brand profile in `brand.md` is its system prompt.
- **Get a human to approve, then send.** Email goes through the Gmail API. Instagram replies go through the Graph API inside Meta's reply window. Cold Instagram DMs are copied to the clipboard and sent by hand.
- **Measure.** The DM archive feeds a sales funnel, lost-lead reasons, outreach template performance, and an LLM "strategist" that suggests improvements.

## 2. System context

```
                         +--------------------------------------------+
   Customer (Instagram) <-|  Meta Graph API (graph.facebook.com /       |
                         |  graph.instagram.com)  - DMs, discovery     |
                         +-------------------^------------------------+
                                             |  read chats / send replies (1 call/s)
+-----------+   browser   +------------------+-------------------+    Hunter.io API
|  Sales    |<----------->|  Local app: `agent serve`            |--> (lead discovery)
|  person   |  localhost  |  FastAPI + static JS, SQLite file    |
+-----------+    :8000    |  background jobs: sync, auto-send    |--> Claude API / Bedrock
      |                   +------------------+-------------------+    (drafts, strategist)
      | terminal                             |
      v                                      | same SQLite schema
+-----------+                  +-------------+-------------------+
| `agent`   |  S3 + lock obj   |  AWS: Lambda `marketing-agent-   |--> Gmail API (send, sync,
|  CLI      |<---------------->|  daily` 08:00 IST (EventBridge)  |    daily digest)
+-----------+                  +---------------------------------+
```

## 3. Building blocks

| Block | Where | Responsibility |
|---|---|---|
| CLI (`agent`) | `src/marketing_agent/cli.py` | Typer commands for discovery, drafting, review, sending, archiving, brand building, the daily run and `serve` |
| Pipeline | `pipeline.py` | Discovery, enrichment, drafting, send gates, Gmail/Instagram sync, DM archive |
| Daily job | `daily.py`, `lambda_handler.py` | Ordered, isolated steps run once a day; emails a digest |
| Storage | `db.py`, `storage.py` | SQLite schema and queries; S3 download, lock and upload for the shared database |
| LLM layer | `llm.py`, `models.py`, `personalize.py`, `context.py`, `brand_builder.py` | Provider abstraction, model picker, drafting prompts, context compression, brand.md builder |
| Integrations | `sources/instagram.py`, `sources/hunter.py`, `channels/gmail.py` | Rate-limited API clients |
| Inbox (Sales app) | `inbox/` | Reply queue, FAQ library, priority scoring, auto-send, background sync, HTTP API, static web UI |
| Insights | `insights/` | Rule-based funnel analysis, marketing report, strategist |
| Infrastructure | `infra/app.py`, `scripts/` | CDK stack (S3, Secrets Manager, Lambda, Scheduler, alarm) and build/deploy scripts |

## 4. Two runtimes, one database schema

| | Local app (`agent serve`) | Daily job (Lambda) |
|---|---|---|
| Trigger | A person starts it; runs until stopped | EventBridge Scheduler, 08:00 Asia/Kolkata |
| Database | Local file `AGENT_DB_PATH` (refuses to run if `AGENT_DB_S3_URI` is set) | `s3://marketing-agent-data-<account>/agent.db`, downloaded under an S3 lock, uploaded at the end |
| Instagram | Syncs chats at startup and every 15 minutes; sends replies after approval; optional auto-send of trusted FAQ answers | Drafts DM replies only; never sends Instagram messages |
| Email | Not used | Gmail sync, sending approved emails, digest |
| UI | Web app at `http://localhost:8000` | Digest email; review happens in the CLI |

The same CLI can work against either database. `agent review`, `agent send` and the other commands take the S3 lock when `AGENT_DB_S3_URI` is set.

## 5. Design principles

1. **Official APIs only.** No unofficial Instagram libraries or browser automation. Meta doesn't allow business-initiated DMs, so cold DMs go through an assisted manual queue.
2. **A human approves outbound messages.** Every draft starts `pending`. Sending needs `approved` plus an explicit confirmation (`confirm=true` from the UI dialog, or typing `y` in the CLI).
   - **The one exception (opt-in, off by default):** an unchanged *trusted* FAQ answer can go out on its own after a 60-second window in which anyone can step in. A person still approved the exact text, either in Settings or by hand three times. See the Backend Design doc, section 5.
3. **Send-time gates re-check everything.** The reply window must be open; the recipient must not be suppressed, must not have had a duplicate reply, and must not have been answered in the Instagram app; the length must be valid. The daily email cap and the CAN-SPAM fields must be set.
4. **Respect rate limits.** At most 1 Graph API call per second. Stop at 80% of Meta's usage headers. Retry transient errors only. Archive work is resumable.
5. **Local-first and cheap.** One SQLite file, rule-based analytics with no API cost, prompt caching on the brand prompt, and older chat history compressed into a summary.
6. **Privacy by default.** Business files (`brand.md`, `faqs.json`, tokens) are gitignored. Secrets live in Secrets Manager. The strategist sees anonymised, masked chats.

## 6. Technology stack

| Layer | Choice |
|---|---|
| Language / packaging | Python 3.11+, `uv`, `uv_build`; console script `agent` |
| Web | FastAPI + Uvicorn; static HTML/CSS/vanilla JS (no build step) |
| Data | SQLite (`sqlite3`), optional S3 copy with conditional-write lock |
| LLM | `anthropic` SDK (Claude API, prompt caching, structured outputs) or `boto3` Bedrock Converse |
| Integrations | `httpx` (Graph API, Hunter), `google-api-python-client` (Gmail) |
| Infra | AWS CDK (Python): S3, Secrets Manager, Lambda (Python 3.12, arm64), EventBridge Scheduler, CloudWatch + SNS |
| Tests | `pytest`, offline: mocked HTTP, fake Instagram client, in-memory SQLite |

## 7. Document map

| Document | Contents |
|---|---|
| 02 Backend Design | Modules, the inbox service, auto-send and background jobs, LLM layer, integrations |
| 03 Frontend Design | Web app modules, state, refresh model, file-by-file guide |
| 04 API Contract | Every HTTP endpoint with request, response and errors |
| 05 Data Contract and Schema | SQLite tables, lifecycles, settings, JSON shapes, files |
| 06 End-to-End Workflows | Step-by-step flows for each user and system journey |
| 07 Operations, Security and Deployment | Config, deployment, monitoring, security, known issues |
