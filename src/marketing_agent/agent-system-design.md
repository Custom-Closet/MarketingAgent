# Plan: Learn from DMs → brand.md, local-first Sales app, business ops roadmap

## Context
@customcloset.in (custom womenswear manufacturer) runs mostly on Instagram DMs. Live tests showed we can read real customer chats (10+ per day, all inside the reply window) and full histories (26 messages paged fine). But:
- `brand.md` is still an empty template, so every Claude draft is generic. Customers keep asking the same things (fabrics, MOQ, samples, location, bulk pricing), and someone on the account already replies with a set answer.
- Inbound DMs aren't stored. `sync_instagram` only sees the last message, and `draft_dm_reply` gets no chat history.
- There's no UI. Review and sending happen in the terminal.

Decisions made: mine chats from the **last 90 days**; send transcripts to Claude **as-is**; **build our own** app on the existing Python backend (no Chatwoot or CRM); **no phone calls** in this phase. The rules stay the same: official API only, a human approves every message, rate limits are respected.

---

## Phase 1: Archive chats and generate brand.md (branch `feature/instagram-messaging`)

### 1a. Store every chat locally
- **`src/marketing_agent/db.py`: two new tables** (created with `CREATE TABLE IF NOT EXISTS` and checked in `_migrate`):
  - `ig_conversations(id PK, igsid, username, updated_time, synced_at)`
  - `ig_messages(id PK, conversation_id, from_id, from_username, text, attachments_json, created_time)`
  - Helpers: `upsert_ig_conversation`, `add_ig_messages` (`INSERT OR IGNORE` by message id), `ig_transcript(conversation_id)`, `ig_conversations_since(dt)`.
- **`src/marketing_agent/sources/instagram.py`:**
  - `messages()`: drop the wrong 20-message cap. Use a `max_pages=50` safety limit, add `attachments{image_data{url},file_url}` to the fields, and add a `stop_before: datetime | None` that stops paging once messages get older than a date.
  - New `iter_conversations(since)`: a generator using the existing one-conversation-per-call paging. It yields `(conversation_id, updated_time, participants)` and stops at the first chat older than `since`. Then refactor `conversations()` to use it.
  - Throttling, retries, usage-header checks and token scrubbing already live in `_request` and are reused unchanged.
- **New `pipeline.archive_instagram(db, ig, since, log)`:**
  - For each chat, skip it if the stored `updated_time` hasn't changed.
  - Otherwise page messages back until we reach messages we already have or the date cutoff, and save them.
  - It's **resumable**: state lives in the database, so a re-run after `InstagramRateLimited` or Ctrl-C only fetches what's missing.
- **CLI: `agent ig-archive --days 90`**, with progress output and API calls used.
- **`sync_instagram`** also stores the messages it reads, so the archive keeps growing from the daily runs.

### 1b. Build brand.md from the archive
- **New module `src/marketing_agent/brand_builder.py`**, a two-step summarise-then-merge:
  1. **Batch step:** chunk transcripts into about 40 chats per batch and send each to Claude (`client.beta.messages.parse` with a Pydantic model, as in `Personalizer._parse`). For each batch, extract:
     - FAQs with the answers the business actually gave
     - products and fabrics mentioned
     - pricing, MOQ, sample and shipping statements
     - objections and how they were handled
     - signs a chat turned into an order (measurements, address or payment shared)
     - examples of tone
  2. **Merge step:** one Claude call merges all batch results into the existing brand.md sections, plus new ones: `## FAQ`, `## Sales playbook` (how to engage, qualify, quote, follow up, close), `## Policies` (MOQ, samples, shipping, export) and `## Do / Don't`.
     - Every fact gets a "(seen in N chats)" note. Prices or policies that contradict each other are listed under `## Needs confirmation` rather than guessed.
- **CLI: `agent build-brand --days 90`** writes **`brand.draft.md`**. It never overwrites `brand.md`; you review and rename it yourself.
  - It reuses the Anthropic client and model settings from `config.py` / `clients.writer`.
  - It prints the token and cost estimate before running and asks for confirmation.
- **Better drafts:** `Personalizer.draft_dm_reply` takes the full stored transcript (newest 30 messages), not just the last message, and `pipeline.sync_instagram` passes it. The system prompt is unchanged, and caching still applies to the brand prompt.

## Phase 2: Local sales app (new branch `feature/sales-app`, based on `feature/instagram-messaging`)

### Backend: FastAPI in the same package
- **New `src/marketing_agent/api/`:** `app.py` plus routers. It reuses `Database`, `pipeline`, `clients` and `config`; no business logic is duplicated.
- **Endpoints:**
  - `GET /leads`, filterable by stage or channel
  - `PATCH /leads/{id}` to change stage or notes
  - `GET /conversations`
  - `GET /conversations/{id}`, the transcript from `ig_messages`
  - `GET /drafts`
  - `POST /drafts/{id}/approve | reject | edit`
  - `POST /drafts/{id}/send`, which runs `pipeline.ig_send_blocker` and the same gates, and needs an explicit `confirm=true` that the UI sets only from a per-message confirm dialog
  - `POST /sync`, which triggers `sync_instagram` and `archive_instagram` as a background task
  - `GET /stats`
- **Funnel stage:** a new `leads.stage` column (`new → engaged → quoted → sample → ordered → lost`), separate from the existing outreach `status`. Claude suggests a stage while drafting (stored as `suggested_stage`), and you accept it in the UI.
- **Next action:** each draft also gets a short "next best action" (e.g. "ask for reference image", "send fabric swatches"), stored with the message. Personalizer output gains a `next_action` field.
- **`agent serve`** starts uvicorn and serves the built frontend from `web/dist`, at `localhost:8000` by default.
- **Local polling:** a background job runs `sync_instagram` every 15 minutes (configurable), with the existing throttling. Webhooks need a public URL, so they come in Phase 3.

### Frontend: `web/` (React + Vite + TypeScript, TanStack Query)
- **Kanban funnel:** one column per stage. Each card shows the lead, last message, hours left in the reply window and whether a draft is waiting.
- **Conversation view:** full transcript and images on the left; on the right, the AI draft (editable), suggested stage, next action, and **Approve → Send** with a confirm dialog showing the recipient and time left.
- **Approval queue:** all pending drafts across channels, with the most urgent reply windows first.
- **Dashboard:** new chats per day, reply rate, average response time, chats per stage, and drafts approved vs rejected.
- **Build:** `npm run build` outputs to `web/dist`, which FastAPI serves.

## Phase 3: AWS mode (later; same code, different config)
- **Run it on one small server.** Deploy the same Docker image (API + UI + scheduler) to a small Lightsail or EC2 instance, with SQLite on its disk and a nightly backup to the existing S3 bucket.
  - The current Lambda + S3-lock design is fine for one daily job, but not for a UI and webhooks writing at the same time.
- **Webhooks:** add a Meta webhook endpoint (`/webhooks/meta`, with signature verification) for instant DMs, replacing polling there.
- **Login:** CloudFront plus authentication (Cognito, or a simple password-protected login to start).
- **Infra:** the CDK stack in `infra/app.py` gains the instance, its security group and DNS. The daily job moves to the instance's own scheduler.
- **Switching:** `AGENT_MODE=local|aws` decides only where things run. Application code stays identical.

## Phase 4: Other things to handle for an Instagram-first business (suggested order)
1. **Facebook Messenger.** Nearly free: the same Page token, with `platform=messenger` on the same endpoints. Add a `channel` value and you get one unified inbox.
2. **WhatsApp Business (Cloud API).** The main channel for Indian D2C after Instagram.
   - Use it to move a customer from Instagram to order updates.
   - Replies inside 24h are free; outside that, use approved templates (about ₹0.11–0.86 each).
3. **Instagram comments → private reply DM.** Comment keywords like "'BRAND'" look like this is already happening. Covered by the official Private Replies API.
4. **Quotes, orders and payments.** Quote/order records on the lead, Razorpay payment links, GST invoice PDFs, and order status updates. This is what turns the funnel into revenue tracking.
5. **The existing auto-reply.** Find out what's sending the 2-second replies (Instagram saved replies/FAQ, or a third-party tool) so the agent doesn't double-answer.
6. **Compliance:**
   - India's **DPDP Act 2023**: a privacy notice, a way to delete a customer's data, and keeping chat archives private.
   - Meta's 24h window, and the `HUMAN_AGENT` tag (7 days, human-sent only), which our approval step satisfies.
   - TRAI rules if phone or SMS is added later.
7. **Phone (deferred).** Exotel click-to-call plus call logs via webhook. An AI voice agent (Bolna) only later.

---

## Verification
- **Unit tests** (`tests/test_agent.py`, using the `_ig` / `MockTransport` pattern already there):
  - the archive resumes and skips unchanged chats
  - `INSERT OR IGNORE` prevents duplicates
  - `messages(stop_before=…)` stops paging
  - the brand builder with a fake Anthropic client (the same pattern as the `writer` fakes) produces every section and never writes to `brand.md`
  - API tests via FastAPI `TestClient`: approve then send runs the gates and refuses without `confirm=true`
- **Live, read-only:**
  - `agent ig-archive --days 7` first: check the call count and the usage header, then `--days 90`
  - interrupt it and re-run to confirm it resumes
- **brand.md:** run `agent build-brand --days 90` and review `brand.draft.md` by hand for wrong prices or policies before renaming it to `brand.md`.
- **App:** run `agent serve`, open `localhost:8000`, move a card between stages, and open the karthik_rten chat. Approve and send one reply to karthik_rten only (a tester account); the confirm dialog must appear.
- **Run tests with** `PYTHONPATH=src uv run --no-sync pytest -q`, which works around the hidden `.pth` file issue.
