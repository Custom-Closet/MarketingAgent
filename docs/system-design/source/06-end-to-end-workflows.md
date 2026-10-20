# End-to-End Workflows
_Step-by-step flows for every user and system journey, with the components and tables each step touches_

## W1. Starting the local app (boot refresh)

1. The operator runs `uv run agent serve`.
2. The CLI loads `.env`. It refuses to start if `AGENT_DB_S3_URI` is set, then opens the local SQLite file with `check_same_thread=False`.
3. New FAQs from `faqs.json` are seeded (`INSERT OR IGNORE`). The stored Bedrock model choice is applied, and the LLM writer is built if it is configured.
4. `InboxService.refresh_drafts()` creates drafts from what is already archived.
5. If Instagram is configured, `start_background_jobs()` starts. **Its first tick runs `sync()` at once** (the boot refresh). After that it syncs every 15 minutes, and checks for due auto-sends every 5 seconds.
6. Uvicorn serves the API and the static UI, and the browser opens `http://localhost:8000/#chat`.
7. The page loads the queue, sees `sync.running` and polls stats every 4 seconds until the sync finishes. The new drafts then appear.

## W2. Inbound DM to reply (manual path)

```
Customer DM ──> Meta ──(sync every 15 min / button)──> ig_conversations + ig_messages
   ──> refresh_drafts: FAQ match? ──> messages(pending, faq_key, reply_deadline)
   ──> Chat card (priority, reasons) ──> person edits / Draft with Claude / applies FAQ
   ──> Approve (approved_at, approved_by=user) ──> Send approved ──> confirm dialog
   ──> POST /api/drafts/send {confirm:true} ──> gates ──> Graph POST /{node}/messages
   ──> messages.status = sent; lead contacted
```

1. **Sync:** `iter_conversations(since = now - 23h)` returns one conversation per page, newest first. Chats whose `updated_time` hasn't changed are skipped. Messages are paged back to the newest stored one and saved.
2. **Draft:** for each thread where the customer spoke last:
   - If they only sent an image, the body is left empty (the category is `reference_image`; a quote is needed).
   - Otherwise the best keyword-matching FAQ answer is filled in.
3. **Triage:** the queue is scored (`score_thread`) and sorted. Filters cover priority, topic, customer type, status, closing soon and search.
4. **Compose:** the person keeps the FAQ text, edits it, or presses **Draft with Claude**. That builds the context (summary plus the last 7 days word for word) and makes one LLM call.
5. **Approve:** a single card, the bulk bar, or **Approve all** for approved-FAQ drafts.
6. **Send:** the dialog lists recipients, hours left and the exact text; at most 25 per batch. Each draft re-runs the gates. Any that are blocked or failed are reported in a toast.

## W3. Auto-send of a trusted FAQ answer (new)

**Precondition:** Settings has **Auto-send trusted FAQ answers** ticked, and the FAQ is trusted: approved in Settings, or approved unchanged in Chat at least 3 times.

| t | Event | System behaviour |
|---|---|---|
| 0 s | Sync creates a draft whose text is the trusted answer | `schedule_auto_sends()` sets `auto_send_at = now + 60 s`; the card shows **Auto-sends in 60s** |
| 0–60 s | The person **edits** | Body differs, so `auto_send_at` is cleared and nothing is sent |
| 0–60 s | The person **clicks into the reply** or presses **Hold** | `auto_send_hold = 1`; the card shows "auto-send held"; the draft waits like any other |
| 0–60 s | The person **approves and sends**, or **dismisses** | The draft is closed; the auto-send has nothing to do (`_sending` guards a simultaneous send) |
| 0–60 s | The person **replies in the Instagram app** | Caught by the live re-fetch at send time: blocked as "answered in Instagram" |
| 60 s+ | Next job tick (within 5 s) | Re-fetch the chat from Instagram, check eligibility again, `approve(by='auto')`, `send(confirm=True)` |
| | Sent | `status = sent`, `approved_by = auto`; Chat stats show "auto-send on (N today)" |
| | Blocked or failed | `auto_send_hold = 1`, error recorded; a person handles it; never retried |

Trust building: when the person approves "Where are you based?" drafts by hand, unchanged, three times, the FAQ becomes trusted, and the fourth such draft gets a countdown. If the FAQ answer is edited in Settings, the count goes back to 0.

## W4. Periodic refresh while the app is open (new)

1. **Server:** every 15 minutes `start_background_sync()` runs in its own thread. A sync already running is not doubled. `next_run` is published in stats.
2. **Browser:** every 20 seconds, if Chat is visible, the tab is in the foreground and `isBusy()` is false, the page reloads the queue, FAQs and stats. Selection, open transcripts and filters are kept.
3. **Browser:** a one-second ticker updates the countdown pills and reloads soon after one expires.
4. **Manual:** **Check for new messages** starts the same sync at once.

## W5. Daily job (AWS)

1. EventBridge Scheduler `marketing-agent-daily` fires at **08:00 Asia/Kolkata** and invokes the Lambda (no retries).
2. `load_secrets()` copies the whitelisted secrets from Secrets Manager into the environment.
3. `open_database(owner="lambda", lock_wait=5 min, ttl=20 min)`: acquire the S3 lock (stale locks are taken over), then download `agent.db`.
4. `daily.run` steps: Gmail sync, send approved emails (6-minute budget, cap and spacing), discovery targets, Hunter enrichment, email, follow-up and DM drafts, then DM reply drafts (`sync_instagram`) and the digest email.
5. On exit: close, **upload**, release the lock. If any step failed, the invocation fails, which fires a CloudWatch alarm and an SNS email.
6. The operator reviews: `agent review` (CLI against the S3 database), `agent send`, `agent ig-queue`, `agent ig-inbox --send`.

## W6. Cold outreach by email

1. **Leads:** `agent find domain` (Hunter domain search plus verification), `agent find instagram` (Business Discovery; hashtag @-mentions), or `agent find csv`. `agent targets add` queues work for the daily run.
2. **Enrich:** leads with a website but no email get Hunter's highest-confidence contact.
3. **Draft:** `agent draft --channel email` (LLM, brand system prompt cached), then `agent review` to approve, edit or reject.
4. **Send:** `agent send`, or the daily step. Each email gets the CAN-SPAM checks, the rolling 24-hour cap (30), random 45–180 s gaps, the compliance footer and the `List-Unsubscribe` header.
5. **Track:** Gmail sync marks leads `replied`, `bounced` or `unsubscribed`. Any reply stops the sequence. Follow-ups go after 4 days, at most 2, threaded with `In-Reply-To`.

## W7. Cold outreach by Instagram (assisted)

1. `agent draft --channel instagram`, then `agent review`.
2. `agent ig-queue` copies the approved text to the clipboard and opens `ig.me/m/<user>`. **The person sends it in Instagram** and confirms with `y`, and the message is marked `sent`. The API is never used for a first contact.

## W8. Funnel review and strategist

1. Sales calls `GET /api/funnel` and `/api/funnel/leads`. The rule-based analysis of `ig_messages` over the last N days costs nothing in API calls.
2. The person drags a card to correct its stage (`funnel_overrides`).
3. **Ask the strategist:** `estimate` shows tokens and USD, the person confirms, then `POST /api/strategy` sends the funnel JSON plus at most 8 lost chats (anonymised, phone numbers and emails masked) and the result is rendered as Markdown.

## W9. Building brand.md from chats

1. `agent ig-archive --days 90` (resumable; stops cleanly at rate limits).
2. `agent build-brand --days 90`: batches of at most 40 chats (about 60k characters). It shows the token and cost estimate and asks to confirm; then runs extract per batch and merge.
3. The result is written to `brand.draft.md`. A person checks `## Needs confirmation` and renames the file to `brand.md`. For Lambda, redeploy afterwards.

## W10. Switching the model (Bedrock)

Settings shows the model list (cheap ones first). **Test** makes one tiny call. **Use this model** tests, then swaps the writer and strategist in the running server and saves `bedrock_model_id`. Lambda keeps using `BEDROCK_MODEL_ID` from the environment.

## W11. Unsubscribe and suppression

`agent unsubscribe <email|username>`, an "unsubscribe" reply, or a bounce calls `suppress()`. The lead becomes `unsubscribed` and its open messages are `rejected`. Every later discovery, drafting, auto-send and send step skips that contact.
