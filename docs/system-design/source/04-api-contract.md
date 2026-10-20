# API Contract
_HTTP API served by `agent serve` (FastAPI, `inbox/api.py`). Base URL `http://localhost:8000`. Interactive docs at `/api/docs`._

## 1. Conventions

- **Transport:** JSON over HTTP on localhost. There is **no authentication**: the app binds to `127.0.0.1` by default and is meant for one trusted operator. With `--host 0.0.0.0` anyone on the LAN can reach it (see Operations).
- **Times** are ISO-8601 UTC strings with second precision, e.g. `2026-10-20T02:29:38+00:00`.
- **Ids:** `draft_id` is the `messages.id` integer; `conversation_id` is the Graph API conversation id (string); `igsid` is the customer's Instagram-scoped id.
- **Errors** return `{"detail": "<message>"}` with:

| Status | Raised by | Example |
|---|---|---|
| 400 | `ValueError`, `SendNotConfirmed` | "sending requires confirm=true", "FAQ answer can't be empty", "send at most 25 replies at a time" |
| 404 | `KeyError` | "draft 999 is not open", "unknown FAQ 'nope'" |
| 422 | Request validation (Pydantic / query bounds) | `max_hours_left=-1`, `context_window_days=0` |
| 503 | `RuntimeError` / not configured | "Instagram is not configured", "Claude is not configured (ANTHROPIC_API_KEY)" |

## 2. Shared shapes

### 2.1 QueueItem

```json
{
  "draft_id": 12,
  "status": "pending",                 // pending | approved
  "username": "fan",
  "igsid": "1784...",
  "conversation_id": "aWdf...",
  "customer_messages": ["Hi! Where are you based?"],   // unanswered texts, oldest first
  "sent_attachment": false,
  "last_inbound_at": "2026-10-20T02:16:38+00:00",
  "hours_left": 22.7,                  // until the reply window closes
  "category": "location",              // FAQ key | reference_image | greeting | other
  "faq_question": "Where are you based?",
  "faq_approved": true,                // uses an approved FAQ answer unchanged
  "auto_send_at": "2026-10-20T02:29:38+00:00",  // null when not scheduled
  "auto_send_held": false,             // true: never auto-sent
  "customer_type": "brand",            // brand | personal | unknown
  "priority": "high",                  // high | medium | low
  "score": 6,
  "reasons": ["asking about price, samples or ordering"],
  "body": "We are based in Jaipur, we deliver pan India",
  "error": null
}
```

### 2.2 FAQ

```json
{"key": "location", "question": "Where are you based?", "answer": "...", "keywords": ["based", "located"],
 "approved": true, "approvals": 2, "trusted": true}
```

`approvals` counts hand-approved drafts that used the current answer unchanged. `trusted = approved || approvals >= 3`.

### 2.3 SyncState

```json
{"running": false, "last_finished": "2026-10-20T02:28:38+00:00", "last_error": null,
 "conversations": 3, "next_run": "2026-10-20T02:43:38+00:00"}
```

## 3. Inbox (Chat) endpoints

| Method and path | Request | Response | Notes |
|---|---|---|---|
| `GET /` | - | `index.html` | The web app; static files under `/static/*` |
| `GET /api/queue` | Query: `priority` (comma list), `category`, `customer_type`, `status`, `max_hours_left` (>= 0), `q` | `QueueItem[]` | Sorted by score desc, then hours left asc |
| `GET /api/stats` | - | `{open, approved, high, closing_soon, faq_ready, sent_24h, auto_sent_24h, auto_send: {enabled, scheduled}, sync: SyncState}` | Counts over the open queue; `*_24h` are rolling |
| `POST /api/sync` | - | `{started: bool}` | Starts a background Instagram sync; `false` if one is running; 503 without Instagram |
| `POST /api/drafts/approve` | `{ids: int[]}` | `{refused: {id: reason}}` | Empty drafts are refused ("write a reply first"); 404 if a draft isn't open |
| `POST /api/drafts/approve-faq` | Query: the same filters as the queue (no `status`) | `{approved: int[]}` | Approves pending drafts using an *approved* FAQ answer unchanged |
| `POST /api/drafts/hold` | `{ids: int[]}` | `{ok: true}` | Stops auto-send for these drafts for good; 404 if not open |
| `POST /api/drafts/reject` | `{ids: int[]}` | `{ok: true}` | Dismisses the drafts (`rejected`) |
| `POST /api/drafts/apply-faq` | `{ids: int[], faq_key: str}` | `{updated: int}` | Fills in the answer; drafts go back to `pending` |
| `POST /api/drafts/send` | `{ids: int[], confirm: bool}` | `{sent: int[], blocked: {id: reason}, failed: {id: error}}` | **`confirm` must be `true`**, at most 25 ids. Reasons: "not approved", "reply window closed", "answered in Instagram", "recipient is on the suppression list", "another reply was already sent for this message", "already being sent", length |
| `POST /api/drafts/{id}` | `{body: str}` | `{ok: true}` | Edit; an approved draft goes back to `pending`; resets the auto-send countdown |
| `POST /api/drafts/{id}/ai` | - | `{body: str}` | LLM draft from the chat context (costs one LLM call); 503 if no LLM |
| `GET /api/conversations/{conversation_id}` | - | `[{from: "us"\|"them", text, attachment: bool, at}]` | Full archived transcript, oldest first |
| `GET /api/faqs` | - | `FAQ[]` | |
| `POST /api/faqs/{key}` | `{answer?: str, keywords?: str[], approved?: bool}` | `{ok: true}` | Setting `answer` withdraws approval (and the count resets); 400 on an empty answer |

### 3.1 Send semantics

`send` is **at-most-once per draft**. A draft that is sent is marked `sent` before the response. On `InstagramRateLimited` the batch stops: that draft goes in `failed` and the rest stay `approved`, not listed. The client should show `blocked` and `failed` reasons to the person.

## 4. Sales and Marketing endpoints

| Method and path | Request | Response |
|---|---|---|
| `GET /api/funnel` | Query: `days` (7–365, default 90), `segment`, `source` | Funnel report (section 4.1) |
| `GET /api/funnel/leads` | Query: `days`, `stage`, `reason`, `segment`, `source`, `q` | `Lead[]`, newest first, at most 200 |
| `POST /api/funnel/{conversation_id}/stage` | `{stage: str \| null}` | `{ok: true}`; `null` returns to the inferred stage; 400 on an unknown stage, 404 on an unknown conversation |
| `POST /api/strategy/estimate` | `{question, history: Turn[], days}` | `{input_tokens, output_tokens: 2500, usd: float \| null}`; 503 without an LLM |
| `POST /api/strategy` | `{question, history: Turn[], days, confirm: true}` | `{answer: markdown}`; 400 if `confirm` is false or the question is empty |
| `GET /api/marketing` | Query: `days` | Marketing report (section 4.2) |

`Turn = {role: "user" | "assistant", content: str}`.

### 4.1 Funnel report

```json
{
  "days": 90, "lost_after_days": 14, "total": 120, "won": 4, "lost": 51, "open": 65,
  "steps": [{"stage": "new", "label": "New", "reached": 120, "conversion": null},
            {"stage": "engaged", "label": "Engaged", "reached": 88, "conversion": 73.3}],
  "board": {"new": 20, "engaged": 18, "qualified": 12, "quoted": 9, "order": 6, "won": 4, "lost": 51},
  "dropoffs": [{"reason": "went_quiet", "label": "Went quiet", "count": 19}],
  "lost_after_stage": {"engaged": 12},
  "by_source": [{"key": "direct_message", "label": "Typed message", "leads": 70, "quoted": 12,
                 "order": 5, "won": 2, "quote_rate": 17.1, "order_rate": 7.1}],
  "by_segment": [{"key": "brand", "label": "Brand", "...": "same keys as by_source"}],
  "weekly": [{"week": "2026-10-12", "leads": 14}],
  "response": {"median_first_reply_hours": 2.4, "within_1h": 41.0, "within_24h": 88.5,
               "never_answered": 7, "awaiting_reply": 5}
}
```

### 4.2 Lead and marketing report

```json
// Lead
{"conversation_id": "...", "username": "fan", "stage": "quoted", "reached": "quoted", "overridden": false,
 "segment": "brand", "source": "direct_message", "reason": null, "first_at": "...", "last_at": "...",
 "last_from": "us", "last_text": "...", "customer_messages": 6, "first_response_minutes": 12.5,
 "awaiting_reply": false, "template": null}

// Marketing
{"days": 90, "sent": 40, "real_replies": 9, "reply_rate": 22.5, "quoted": 3, "orders": 1,
 "templates": [{"template": "Hi! We manufacture...", "sent": 25, "auto_replies": 4, "real_replies": 6,
                "quoted": 2, "orders": 1, "reply_rate": 24.0}],
 "sources": ["same items as funnel.by_source"]}
```

## 5. Settings and model endpoints

| Method and path | Request | Response |
|---|---|---|
| `GET /api/settings` | - | `{context_window_days, lost_after_days, auto_send_faq: bool, auto_send: {delay_seconds: 60, after_approvals: 3}, status: {instagram, claude_drafts, strategist, sync: SyncState}, archive: {conversations, messages, newest_message, summaries}}` |
| `POST /api/settings` | `{context_window_days?: 1-60, lost_after_days?: 3-120, auto_send_faq?: bool}` | `{ok: true}`. Turning `auto_send_faq` on schedules a fresh 60 s countdown on eligible drafts; off clears them all |
| `GET /api/models?refresh=bool` | - | `{provider: "bedrock"\|"anthropic"\|null, current, models: [{id, name, provider, low_cost}], error}` |
| `POST /api/models/test` | `{model_id}` | `{ok: true, model, reply, seconds}` or `{ok: false, model, error}`; 503 if switching is unavailable |
| `POST /api/models/select` | `{model_id}` | Same as test; on success the writer and strategist switch and the choice is saved; 400 if not on Bedrock or the id is empty |

## 6. Compatibility rules

- New response fields are **additive**. Clients ignore keys they don't know. `auto_send_at`, `auto_send_held`, `auto_sent_24h`, `auto_send`, `approvals`, `trusted`, `next_run` and `auto_send_faq` were added in this way.
- Request bodies reject wrong types (422) but allow optional fields to be left out.
- `/api/drafts/send` will never accept `confirm` defaulting to true. Auto-send is a server-side job, not an API flag.
