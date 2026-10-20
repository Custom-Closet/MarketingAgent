# Data Contract and Schema
_SQLite tables, lifecycles, settings, file formats and the invariants every writer must keep_

## 1. Storage model

- **One SQLite file** holds all state (`data/agent.db` by default, `AGENT_DB_PATH`). The schema is created with `CREATE TABLE IF NOT EXISTS`. `Database._migrate()` adds columns introduced later with `ALTER TABLE ... ADD COLUMN`, so older files keep working.
- **Shared mode:** with `AGENT_DB_S3_URI` set, commands download the file under an S3 lock object (`<key>.lock`, created with `IfNoneMatch="*"`), work locally, and upload in a `finally` block. Concurrency is whole-file last-writer-wins, guarded by that lock. S3 versioning keeps 90 days of history.
- **Times** are stored as ISO-8601 UTC text with second precision. ISO strings sort correctly, so `<` and `>=` comparisons in SQL work on text.
- `PRAGMA foreign_keys = ON`.

## 2. Entity relationships

```
leads 1───* messages            (lead_id)        outbound drafts/sends, all channels
ig_conversations 1───* ig_messages (conversation_id)  archived DMs, both directions
ig_conversations 1───0..1 ig_summaries           compressed older history
ig_conversations 1───0..1 funnel_overrides       manual funnel stage
messages.thread_id  ──(instagram reply)──> ig_conversations.igsid   (logical join, not an FK)
messages.faq_key    ──> faqs.key                                     (logical join)
leads.ig_username / email ──> suppression.value (lower-cased)        (logical join)
```

## 3. Tables

### 3.1 `leads`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name`, `company`, `domain`, `position`, `website`, `notes` | TEXT | Merged on upsert: blanks are filled, values never overwritten |
| `email` | TEXT UNIQUE | Lower-cased |
| `email_status` | TEXT | Hunter verifier result: `valid`, `accept_all`, `invalid`, `webmail`, `disposable`, `unknown` |
| `ig_username` | TEXT UNIQUE | Lower-cased, no `@` |
| `ig_bio`, `ig_followers` | TEXT, INTEGER | From Business Discovery |
| `source` | TEXT NOT NULL | `hunter`, `instagram`, `csv`, `manual` |
| `status` | TEXT NOT NULL | `new` -> `contacted` -> `replied` / `bounced` / `unsubscribed` |
| `created_at` | TEXT NOT NULL | |

### 3.2 `messages` (outbound drafts and sends)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | The `draft_id` in the API |
| `lead_id` | INTEGER FK -> leads | |
| `channel` | TEXT | `email`, `instagram` |
| `kind` | TEXT | `initial`, `followup`, `reply` (inbox drafts are `instagram` + `reply`) |
| `step` | INTEGER | Follow-up number (0 = first email) |
| `subject` | TEXT | Email only |
| `body` | TEXT NOT NULL | |
| `status` | TEXT | Section 4.1 |
| `thread_id` | TEXT | Gmail thread id, or **the customer's IGSID** for Instagram replies |
| `rfc_message_id` | TEXT | Email `Message-ID`, used to thread follow-ups |
| `error` | TEXT | Last block or failure reason, shown in the UI |
| `created_at`, `approved_at`, `sent_at` | TEXT | `approved_at` is required by the send gate |
| `reply_deadline` | TEXT | Instagram reply: last inbound + 23 h |
| `faq_key` | TEXT | FAQ the draft started from |
| `approved_by` | TEXT | **new**: `user` or `auto`; NULL on older rows means user |
| `auto_send_at` | TEXT | **new**: when an eligible draft sends itself; NULL = not scheduled |
| `auto_send_hold` | INTEGER NOT NULL DEFAULT 0 | **new**: 1 = never auto-send (held by a person, or a failed attempt) |

### 3.3 Instagram archive

| Table | Columns | Notes |
|---|---|---|
| `ig_conversations` | `id` PK (Graph conversation id), `igsid`, `username`, `updated_time`, `synced_at` | `updated_time` is written **after** the messages are saved, so an interrupted sync fetches the chat again |
| `ig_messages` | `id` PK (Graph message id), `conversation_id` FK, `from_id`, `from_username`, `text`, `extras_json`, `created_time` | `INSERT OR IGNORE` dedupes; `extras_json` holds attachments, shares or story (paging links removed); index on `(conversation_id, created_time)` |
| `ig_summaries` | `conversation_id` PK/FK, `summary`, `summarized_through`, `updated_at` | Running summary of messages older than the context window |

### 3.4 Inbox, sales and settings

| Table | Columns | Notes |
|---|---|---|
| `faqs` | `key` PK, `question`, `answer`, `keywords_json` (lower-case list), `approved` (0/1), `updated_at` | Seeded from `faqs.json` and never overwritten; `approvals` and `trusted` are **derived**, not stored |
| `funnel_overrides` | `conversation_id` PK/FK, `stage`, `updated_at` | Manual stage from the Sales board |
| `app_settings` | `key` PK, `value`, `updated_at` | Section 5 |
| `targets` | `id`, `kind` (`domain`, `ig_user`, `hashtag`), `value`, `status` (`pending`, `done`, `failed`), `result`, `created_at`, `processed_at`; UNIQUE(kind, value) | Discovery queue for the daily run |
| `suppression` | `value` PK (email or username, lower-cased), `reason`, `created_at` | Do-not-contact list |

## 4. Lifecycles

### 4.1 Message status

```
pending ──approve──> approved ──send──> sent
   │                    │ ├─ window closed ──> expired
   │                    │ ├─ answered in IG / suppressed / duplicate ──> rejected
   │                    │ └─ API error / bad length ──> failed (or stays approved with error)
   ├──edit / apply FAQ──> pending (approved_at cleared, auto_send_at cleared)
   └──dismiss / suppress──> rejected
(sent emails later move the *lead* to replied / bounced / unsubscribed via Gmail sync)
```

Auto-send (Instagram replies only): `pending/approved` with `auto_send_at <= now` and eligible. A pending draft becomes `approved` with `approved_by=auto`, then `sent`. If it isn't sent, `auto_send_hold` becomes 1.

### 4.2 Funnel stage (derived, `insights/analysis.py`)

| Stage | Reached when (later rules win) |
|---|---|
| `new` | Default |
| `engaged` | The customer typed a real message, or there are 2+ customer messages and a human reply from us |
| `qualified` | Intent words, brand or personal signals, or a reference image |
| `quoted` | One of our human messages contains a price (`₹`, `rs`, "price estimate", "cut to pack"...) |
| `order` | Payment, QR code, address or measurements requested, or the customer says they paid |
| `won` | Tracking, shipped, "your order is ready" |
| `lost` | Not won, and declined or silent for more than `lost_after_days` |

Lost reasons, first match wins: `price`, `declined`, `unanswered`, `out_of_scope`, `fabric`, `no_reference`, `went_quiet`. Lead sources: `outreach`, `ad_price_tap`, `brand_keyword`, `faq_button`, `direct_message`. Segments: `brand`, `personal`, `unknown`.

## 5. Settings (`app_settings`)

| Key | Default | Range | Used by |
|---|---|---|---|
| `context_window_days` | `7` | 1–60 | Days of chat the LLM reads word for word |
| `lost_after_days` | `14` | 3–120 | Silence before a lead counts as lost |
| `auto_send_faq` | `0` | `0` / `1` | **new**: auto-send trusted FAQ answers |
| `bedrock_model_id` | `BEDROCK_MODEL_ID` | Any listed model | Model picked in Settings (`agent serve` only) |

Constants that are not settings: `AUTO_SEND_DELAY = 60 s`, `AUTO_SEND_AFTER_APPROVALS = 3`, `SYNC_EVERY = 15 min`, `REPLY_WINDOW = 23 h`, `MAX_SEND_BATCH = 25`, `IG_MAX_MESSAGE_CHARS = 1000`.

## 6. Invariants

1. **At most one open draft per customer:** an `instagram`/`reply` row in `pending` or `approved` per `thread_id`. `refresh_drafts` checks `open_draft(igsid)` first.
2. **`approved_at` is set** for every row that may be sent; edits clear it.
3. **`auto_send_at` is set only while the draft is eligible.** The scheduler clears it otherwise, and `hold`, `edit` and `apply_faq` clear it directly.
4. **Trust is derived from history:** `approvals = COUNT(messages WHERE faq_key = key AND approved_at IS NOT NULL AND COALESCE(approved_by,'user') = 'user' AND TRIM(body) = TRIM(faqs.answer))`. Editing an answer therefore resets trust without extra bookkeeping.
5. **Suppression is final:** `suppress()` sets matching leads to `unsubscribed` and rejects their open messages. Every discovery, drafting and send path checks the list.
6. **The archive is append-only:** message ids are Graph ids, stored with `INSERT OR IGNORE`.

## 7. Files and external data contracts

| File | Format | Contract |
|---|---|---|
| `brand.md` (gitignored) | Markdown | Brand profile appended to every drafting and strategist system prompt; baked into the Lambda bundle |
| `brand.draft.md` | Markdown | Output of `agent build-brand`; never overwrites `brand.md`; includes `## Needs confirmation` |
| `faqs.json` (gitignored; see `faqs.example.json`) | `[{key, question, answer, keywords: [str]}]` | Seeded once per key; edits are made in the app |
| `.env` | `KEY=value` | See Operations, section 2 |
| `credentials.json`, `data/gmail_token.json` | Google OAuth JSON | Token file `chmod 600`; Lambda reads `GMAIL_TOKEN_JSON` from Secrets Manager |
| Secrets Manager `marketing-agent/secrets` | JSON object | Only `ANTHROPIC_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`, `HUNTER_API_KEY`, `IG_ACCESS_TOKEN`, `GMAIL_TOKEN_JSON` are loaded |

### 7.1 Graph API message mapping

| Graph field | Column |
|---|---|
| `id` | `ig_messages.id` |
| `from.id` / `from.username` | `from_id` / `from_username` (our id = `IG_USER_ID`) |
| `message` | `text` (empty string becomes NULL) |
| `attachments`, `shares`, `story` | `extras_json` (paging removed) |
| `created_time` (`%Y-%m-%dT%H:%M:%S%z`) | `created_time` (UTC ISO) |

## 8. LLM structured outputs

| Model | Fields |
|---|---|
| `EmailDraft` | `subject`, `body` |
| `DMDraft` | `message` |
| `ConversationSummary` | `summary` (about 120 words, `- ` bullets, no personal contact details) |
| `BatchFindings` (brand builder) | `faqs[{question, answer_given, chats}]`, `facts[{topic, statement, chats}]`, `objections[{objection, response, outcome}]`, `conversion_signals`, `winning_replies`, `tone_notes` |
