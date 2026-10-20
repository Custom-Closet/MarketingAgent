# Backend Design
_Python package `marketing_agent`: modules, the inbox service, auto-send, background jobs, LLM layer and integrations_

## 1. Package layout

```
src/marketing_agent/
  cli.py              Typer app `agent` (all commands, incl. `serve`)
  config.py           Settings dataclass loaded from env / .env
  clients.py          Factories: gmail_client, hunter_client, instagram_client, llm, writer, build_clients
  db.py               SQLite schema, migrations, all non-inbox queries
  storage.py          open_database(): local file, or S3 download + S3Lock + upload
  pipeline.py         discovery, enrichment, drafting, send gates, sync, DM archive
  daily.py            daily.run(): ordered, isolated steps + digest
  lambda_handler.py   AWS entry point: secrets -> daily.run -> upload
  llm.py              LLM protocol; ClaudeLLM (Anthropic SDK) and ConverseLLM (Bedrock)
  models.py           ModelManager: list / test / switch Bedrock models at runtime
  personalize.py      Personalizer: drafting prompts and Pydantic output models
  context.py          build_context(): recent window verbatim + incremental summary
  brand_builder.py    BrandBuilder: archived DMs -> brand.draft.md
  sources/instagram.py  InstagramClient (Graph API)
  sources/hunter.py     HunterClient (Hunter.io v2)
  channels/gmail.py     GmailChannel, email building, OAuth
  inbox/              Sales app backend + static frontend
    faq.py            FAQ dataclass, trust rule, keyword matcher
    repository.py     InboxRepository: inbox SQL
    service.py        InboxService: rules, queue, approvals, sending, auto-send, jobs
    api.py            create_app(): FastAPI routes
    static/           index.html, app.css, common/chat/sales/marketing/settings.js
  insights/
    analysis.py       pure funnel classification and reports
    service.py        InsightsService: funnel, leads, marketing, stage overrides, strategist
    strategist.py     Strategist: briefing + LLM advice with masking
```

Layering inside the inbox follows **routes -> service -> repository -> db**. `api.py` only translates HTTP to and from service calls. Every rule lives in `service.py`. All SQL lives in `repository.py` or `db.py`.

## 2. The inbox service (`inbox/service.py`)

`InboxService(repo, *, our_id, ig=None, writer=None, clock=utcnow)` owns everything about replying to DMs.

### 2.1 Concurrency model

- `agent serve` shares **one SQLite connection** (`check_same_thread=False`) across Uvicorn worker threads, the background sync thread and the job thread.
- `InboxService.lock` (a re-entrant `threading.RLock`) serialises every database access. `InsightsService` takes the same lock.
- Network calls (Graph API fetches, `send_reply`) run **outside** the lock so the UI stays responsive.
- `_sending: set[int]` marks drafts that are being sent right now. A second send of the same draft, such as a manual Send racing an auto-send, is blocked with `"already being sent"`.

### 2.2 Drafting rules: `refresh_drafts()`

1. Find threads awaiting a reply: the newest stored message is from the customer and inside the 23-hour reply window.
2. Close open drafts that no longer apply. If we answered from the Instagram app, the draft becomes `rejected` / "answered in Instagram". Otherwise it becomes `expired` / "reply window closed".
3. For each waiting thread with no open draft (and not suppressed): if the customer sent an image without text, leave the body empty, since a quote is needed. Otherwise use `match_faq()` on the unanswered text to pre-fill an FAQ answer. Then create a `pending` draft with `reply_deadline = last_inbound + 23h` and `faq_key`.
4. Call `schedule_auto_sends()`.

### 2.3 Priority scoring: `score_thread()`

| Signal | Points |
|---|---|
| Reply window closes in 4 hours or less (`URGENT_HOURS`) | +3 (+1 if 12 hours or less) |
| Unanswered text asks about price, samples or ordering (`INTENT_WORDS`) | +3 (+1 if only mentioned earlier) |
| Sent an image or attachment | +3 |
| Looks like a clothing brand (`BRAND_WORDS`) | +2 |
| Only greetings | -1 |

5 or more points is `high`, 2 or more is `medium`, anything else is `low`. The queue is sorted by score, then by hours left. The category is the FAQ key, `reference_image`, `greeting` or `other`. The customer type is `brand`, `personal` or `unknown`.

### 2.4 Approvals and edits

- `approve(ids)` refuses empty bodies and records `approved_at` and `approved_by='user'`.
- `edit(id, body)` sends the draft back to `pending`, clears `approved_at` and the auto-send time, then reschedules. Editing back to the exact trusted answer restarts the countdown.
- `apply_faq(ids, key)` fills in an FAQ answer and resets the draft to `pending`.
- `approve_faq_replies(filters)` is the one-click approval of pending drafts that use an *approved* FAQ answer unchanged.
- `hold(ids)` sets `auto_send_hold=1`, so the draft is never auto-sent.
- `reject(ids)` sets `rejected` with the reason "rejected in inbox".
- `ai_draft(id)` builds the context window and summary, calls `writer.draft_dm_reply`, then calls `edit()`.

### 2.5 Sending: `send(ids, *, confirm)`

`confirm` must be `True`, a batch holds at most 25 drafts (`MAX_SEND_BATCH`), and Instagram must be configured. For each draft:

1. Under the lock, run `_send_blocker()`. It requires `status == approved`, then applies `pipeline.ig_send_blocker` (approval recorded, window open, not suppressed, no reply already sent, 1–1000 characters), then the **answered-in-Instagram** check. A blocked draft whose window closed or that was answered elsewhere is closed (`expired` or `rejected`).
2. Mark the draft as being sent and call `ig.send_reply(igsid, body)` outside the lock.
3. `InstagramRateLimited` stops the batch; the remaining drafts stay approved. Any other `InstagramError` records the error and moves on. On success the draft is marked `sent`.

## 3. Background jobs (Task: refresh on boot and every 15 minutes)

`start_background_jobs(sync_every=SYNC_EVERY, tick=JOB_TICK_SECONDS)` starts one daemon thread. `agent serve` calls it whenever Instagram is configured.

```
next_sync = now                      # boot: sync immediately
loop until stop_background_jobs():
    if ig and now >= next_sync:
        start_background_sync()      # separate thread; no-op if one is running
        next_sync = now + 15 min     # exposed as stats.sync.next_run
    run_auto_sends()                 # errors logged, loop continues
    wait 5 s
```

| Constant | Value | Meaning |
|---|---|---|
| `SYNC_EVERY` | 15 minutes | Instagram check interval while the app runs |
| `JOB_TICK_SECONDS` | 5 | How often due auto-sends are checked |
| `AUTO_SEND_DELAY` | 1 minute | How long a person has to step in |

`sync()` archives the conversations active inside the reply window. It skips chats whose `updated_time` hasn't changed and pages messages back only to the newest stored one. Then it calls `refresh_drafts()`. Progress and errors go into `sync_state = {running, last_finished, last_error, conversations, next_run}`, which `/api/stats` and `/api/settings` return.

The **frontend** reloads the queue every 20 seconds while Chat is visible. It also reloads when a countdown passes zero. It skips the reload while a person is typing, a dialog is open, or a draft has unsaved edits.

## 4. FAQ library (`inbox/faq.py`)

- FAQs are seeded from `faqs.json` with `INSERT OR IGNORE`, so edits made in the app are never overwritten.
- `match_faq(text, faqs)` picks the FAQ with the most keyword hits on word boundaries, breaking ties by the longest keyword.
- `FAQ.approvals` is computed live: the number of Instagram reply drafts with `faq_key = key`, `approved_at` set, `approved_by` = user (or NULL on older rows), and a body equal to the **current** answer, ignoring surrounding whitespace.
- `FAQ.trusted = approved or approvals >= AUTO_SEND_AFTER_APPROVALS (3)`.
- `edit_faq()` withdraws approval. Because the count only matches the current text, editing an answer also resets its count.

## 5. Auto-send of trusted FAQ answers

**Goal:** answers the business has already vetted shouldn't wait for a person, and a person can still always step in.

### 5.1 Eligibility (checked on every tick and again right before sending)

All of these must hold:

1. The `auto_send_faq` setting is on (`app_settings`, default `"0"`), and Instagram is configured.
2. The draft is open (`pending` or `approved`) and `auto_send_hold = 0`.
3. The draft has a `faq_key` whose FAQ is **trusted**.
4. The draft body is non-empty and equals the FAQ's current answer, ignoring surrounding whitespace.

### 5.2 State machine

```
  eligible & no auto_send_at  -> auto_send_at = now + 60 s       (countdown shown in Chat)
  becomes ineligible          -> auto_send_at = NULL             (edit, FAQ edited/unapproved, setting off)
  Hold / click into the reply -> auto_send_hold = 1, auto_send_at = NULL   (permanent for this draft)
  manual Send / Dismiss       -> draft leaves the open set; nothing to do
  now >= auto_send_at         -> re-fetch chat from Instagram; if pending: approve(by='auto');
                                 send([...], confirm=True)
       sent                   -> status sent, approved_by 'auto'
       blocked/failed/limited -> auto_send_hold = 1 (left for a person; never retried)
```

### 5.3 Safeguards

- Auto-approvals (`approved_by='auto'`) **don't count** towards the 3-approval trust, so trust can't build on itself.
- Right before sending, the job **re-fetches that conversation from Instagram** (`_caught_up`, one Graph API call). A reply typed in the Instagram app since the last sync then trips the "answered in Instagram" gate. If Instagram can't be read, the draft is held instead of sent.
- Auto-sends use the same `send()` path and every send-time gate.
- A failed attempt holds the draft. It never loops.
- Customers who sent an image never get an FAQ draft, so they are never auto-answered.
- Enabling the setting starts a **fresh** one-minute countdown on existing drafts; nothing goes out the moment it's switched on.
- It only runs while `agent serve` is running. The Lambda daily job never auto-sends.

## 6. Insights (`insights/`)

- `analysis.analyze(rows, our_id, now, lost_after_days, overrides)` is a pure function over archived messages. It detects canned texts (our texts sent 4+ times), the lead source, exclusions, the furthest stage reached (`new -> engaged -> qualified -> quoted -> order -> won`, regex rules), lost status and the reason for it. See the Data Contract doc for the stage and reason lists.
- `InsightsService` wraps it with filters, overrides from `funnel_overrides`, the `lost_after_days` setting and the strategist. It shares the inbox lock.
- `Strategist.advise()` sends funnel JSON plus at most 8 anonymised lost chats (phone numbers and emails masked, stored summaries preferred). It requires a cost estimate step and `confirm=true` first.

## 7. LLM layer

| Path | Class | Notes |
|---|---|---|
| Claude API (default) | `ClaudeLLM(anthropic.Anthropic())` | Structured outputs via `messages.parse`; `cache_control: ephemeral` on the system prompt (brand.md); server-side fallback beta; `effort` from `ANTHROPIC_EFFORT`; priced by `PRICE_PER_MTOK` |
| Bedrock (default) | `ConverseLLM` | Any text model; JSON-in-prompt validated by Pydantic with one corrective retry; learns the model's output cap; price unknown (shows tokens) |
| Bedrock + Anthropic SDK | `ClaudeLLM(AnthropicBedrockMantle)` | Opt-in with `BEDROCK_USE_ANTHROPIC_SDK=1` + SigV4 keys |

- **Model picker:** `ModelManager` lists on-demand Bedrock text models, cheap ones first. `test()` makes one tiny call. `select()` tests, then swaps the writer and strategist at runtime and persists `bedrock_model_id`.
- **Context compression:** `build_context()` sends the last N days verbatim (`context_window_days`, default 7, 5–30 messages). Messages that age out are folded incrementally into a summary of about 120 words, stored in `ig_summaries`. Personal details are left out of the summary.
- **Refusals and truncation** raise `LLMError`; partial output is never used.

## 8. Integrations

| Client | Endpoints | Protections |
|---|---|---|
| `InstagramClient` | `/{node}/conversations` (1 per page), `/{conversation}`, `/{conversation}/messages` (25 per page, shrinks on "too much data"), `POST /{node}/messages`, Business Discovery, hashtag search | 1 call/s; stop at 80% of `x-business-use-case-usage` / `x-app-usage`; retry codes -2/1/2 after 3 s and 9 s; rate-limit codes raise `InstagramRateLimited`; paging URLs (which contain the token) scrubbed |
| `HunterClient` | `GET /domain-search`, `/email-verifier`, `/email-finder` | 429 maps to a quota error; per-target failure |
| `GmailChannel` | `users.messages.send`, `users.threads.get`, `users.messages.list` | Daily cap, random 45–180 s gaps, CAN-SPAM footer, `List-Unsubscribe`, reply/bounce/unsubscribe detection |

## 9. Daily job (Lambda)

`lambda_handler.handler` loads secrets, opens the S3 database (it waits up to 5 minutes for the lock, which has a 20-minute TTL) and runs `daily.run`. The steps are isolated, so one failure doesn't stop the rest:

| # | Step | Needs |
|---|---|---|
| 1 | `sync`: Gmail replies, bounces, unsubscribes | Gmail |
| 2 | `sent`: approved emails, at most a 6-minute budget | Gmail |
| 3 | `targets`: queued discovery targets | Hunter or Instagram |
| 4 | `enriched`: emails for leads with a website | Hunter |
| 5–7 | `email_drafts`, `followup_drafts`, `dm_drafts` | LLM (+ Instagram) |
| 8 | `dm_reply_drafts`: `pipeline.sync_instagram` | LLM + Instagram |
| 9 | `digest`: summary email | Gmail |

Any step error makes the invocation fail, which fires a CloudWatch alarm and an SNS email. Lambda and the scheduler both have retries set to 0, so a partial send is never repeated.

## 10. Testing

The tests run offline (`PYTHONPATH=src uv run --no-sync pytest -q`). `tests/conftest.py` provides an in-memory database, seeded FAQs, a `FakeIG` that records sends, and `seed_chat()`. The auto-send tests drive a hand-controlled `Clock`. Coverage includes the trust rules, the one-minute window, hold and edit cancelling, the manual-vs-auto race, failure holding, and the background boot sync and 15-minute schedule.
