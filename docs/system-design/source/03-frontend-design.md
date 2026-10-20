# Frontend Design
_The local Sales studio web app: modules, files, state, refresh model and UI rules_

## 1. Approach

The frontend is a **no-build** single page served by FastAPI from `src/marketing_agent/inbox/static/`: one HTML file, one stylesheet, and one plain JavaScript file per module, loaded as classic scripts that share globals. There is no framework, bundler or npm step. That keeps the app installable with `uv` alone and lets it run on a laptop or a phone on the same Wi-Fi (`agent serve --host 0.0.0.0`).

| Principle | How it shows up |
|---|---|
| Server owns the rules | The UI never decides what may be sent. It renders `/api/queue` and calls the action endpoints; the gates run on the server. |
| Explicit confirmation | Sending opens a dialog that lists every message and recipient. Only its button posts `confirm: true`. |
| Costs shown first | The strategist calls `/api/strategy/estimate` and shows tokens and USD before asking. |
| Never clobber the person | Timed refreshes are skipped while someone is typing, has unsaved edits, or has a dialog open. |
| Escaped output | Every server string goes through `esc()` before `innerHTML`. Strategist Markdown is escaped, then given a small safe subset (headings, bullets, bold). |

## 2. Files

| File | Role |
|---|---|
| `index.html` | Shell: sidebar navigation, the four `<section data-title>` modules, the send and cost dialogs, the tooltip; loads the scripts in order and calls `go(location.hash)` |
| `app.css` | Design tokens (`:root` colours), layout, cards, pills, charts, kanban, dialogs; responsive below 760 px (bottom tab bar) |
| `common.js` | `$`, `esc`, `fmt` (en-IN numbers), `pct`, `api()` fetch wrapper (JSON, raises the server's `detail`), `toast()`, hover tooltips for `[data-tip]`, module router `go()` and `loaders`, `renderMarkdown()` |
| `chat.js` | Reply inbox: filters, queue cards, bulk bar, FAQ banner, send dialog, transcripts, **auto-send countdown and Hold**, **timed refresh** |
| `sales.js` | Funnel tiles and bars, lost reasons, weekly SVG column chart, source table, kanban board (drag to change stage), lead list, strategist chat |
| `marketing.js` | Outreach template table and lead-source quality |
| `settings.js` | Status list, drafting and funnel settings, **auto-send toggle**, Bedrock model picker, FAQ editor with approval count and trust |

## 3. Routing

`go(name)` shows one `<section>`, highlights its nav button, sets `document.title`, writes `#name` to the URL with `history.replaceState`, and calls `loaders[name]()`. Each module script registers its loader (`loaders.chat = loadChat`, and so on). Opening `/#sales` goes straight to Sales.

## 4. Chat module

### 4.1 State

```js
state = {
  items: [],          // QueueItem[] from GET /api/queue (see API Contract)
  faqs: [],           // from GET /api/faqs
  selected: Set,      // draft ids ticked for bulk actions
  priority: Set,      // active priority chips
  openChat: Set,      // draft ids whose transcript is expanded
  transcripts: {},    // draft id -> cached transcript, so refreshes keep it open
}
```

### 4.2 Card anatomy

| Element | Source field(s) |
|---|---|
| `@username` link to `ig.me/m/<user>` | `username` |
| Priority pill, topic pill, customer-type pill, "approved" pill | `priority`, `category` / `faq_question`, `customer_type`, `status` |
| **"Auto-sends in 42s" pill + Hold** | `auto_send_at` (live countdown from the browser clock) |
| "auto-send held" pill | `auto_send_held` |
| Hours left (red when 4 hours or less) | `hours_left` |
| What they said (+ attachment marker) | `customer_messages`, `sent_attachment` |
| Why (score reasons), error | `reasons`, `error` |
| Reply textarea | `body` |
| Actions: Approve / Save changes, Draft with Claude, Show chat, Dismiss | `status`, `draft_id`, `conversation_id` |

### 4.3 Actions mapped to endpoints

| UI action | Calls |
|---|---|
| Filter chips, selects, search (250 ms debounce) | `GET /api/queue?...` |
| Approve (card) | `POST /api/drafts/{id}` if the text changed, then `POST /api/drafts/approve` |
| Save changes (approved card) | `POST /api/drafts/{id}`, which sends it back to review |
| Draft with Claude | `POST /api/drafts/{id}/ai` |
| Show chat | `GET /api/conversations/{conversation_id}` |
| Dismiss / bulk Dismiss | `POST /api/drafts/reject` |
| Bulk "Use FAQ answer..." | `POST /api/drafts/apply-faq` |
| Banner "Approve all" | `POST /api/drafts/approve-faq?<filters>` |
| **Hold** / clicking into a counting-down reply | `POST /api/drafts/hold` |
| Send approved, then confirm in the dialog | `POST /api/drafts/send {ids, confirm: true}` (at most 25) |
| Check for new messages | `POST /api/sync`, then polls `/api/stats` every 4 s while syncing |

### 4.4 Auto-send UX

- A card whose draft is scheduled shows an amber **Auto-sends in Ns** pill. A one-second ticker updates every `[data-countdown]` element from `auto_send_at`, with no network calls.
- **Focusing the reply textarea holds the draft at once.** The pill turns into "auto-send held" without a re-render, so focus and cursor stay put, and `POST /api/drafts/hold` is sent. This closes the "it went out while I was typing" race.
- The **Hold** button does the same and then reloads.
- When a countdown passes zero by more than 3 seconds, the page reloads the queue (at most once every 5 seconds) so the sent card disappears. The server's job checks every 5 seconds.
- The stats line shows `auto-send on (N today)` when the setting is enabled.

### 4.5 Refresh model (Task: auto refresh)

| Trigger | What happens |
|---|---|
| Page load / switching to Chat | `loadChat()`: queue + FAQs + stats |
| Server boot | The server syncs Instagram right away; the page shows "syncing..." and polls stats every 4 s until it finishes |
| Every 15 minutes (server) | The background job syncs Instagram and creates drafts; `stats.sync.next_run` gives the next time |
| Every 20 s (browser, `REFRESH_MS`) | `loadChat()` if Chat is visible, the tab is visible and `isBusy()` is false |
| Countdown expired | `loadChat()` (throttled) |

`isBusy()` is true when the send or cost dialog is open, a queue textarea has focus, or any textarea differs from its saved `body`. Then the refresh is skipped and retried on the next tick, so work in progress is never thrown away. The stats line reads `checked 7:28 pm, next 7:43 pm`.

## 5. Sales module

- **Tiles:** leads, quoted, sample/order, median first reply, replied within 1 h, waiting for us.
- **Funnel bars:** click one to list its leads. **Lost reasons:** click one to list them.
- **Weekly columns:** an inline SVG with a nice-rounded axis and hover tooltips.
- **Open pipeline board:** five lanes, at most 40 cards each. Drag a card, or use its select, to change the stage (`POST /api/funnel/{conversation_id}/stage`). "Auto" clears the override.
- **Strategist:** preset questions, `estimate` then a cost dialog, then `POST /api/strategy` with `confirm: true`. The history is kept in memory (`sales.history`) and sent with each question. "New conversation" clears it.

## 6. Marketing and Settings modules

- **Marketing:** tiles for outreach sent, real replies, quoted and orders. A template table (real replies exclude instant auto-responses) and a source table.
- **Settings:**
  - Status: Instagram, AI drafts, strategist, archive size, summaries, last sync.
  - Context window days, lost-after days, and the **Auto-send trusted FAQ answers** checkbox (help text: "Sends an unchanged trusted answer 60s after it's drafted...").
  - Bedrock model picker (only when `provider == "bedrock"`).
  - FAQ editor: each FAQ shows "approved unchanged in Chat N times" and a **trusted - can auto-send** pill. Saving an answer withdraws approval.

## 7. Styling tokens

| Token | Value | Use |
|---|---|---|
| `--accent` | `#7a4b2a` | Primary buttons, brand |
| `--data` | `#b5562a` | Single-series chart marks |
| `--high` / `--high-bg` | `#b42318` / `#fde7e4` | High priority, errors, badges |
| `--med` / `--med-bg` | `#9a6700` / `#fff4d6` | Medium priority, **auto-send countdown** |
| `--ok` / `--ok-bg` | `#1a7f37` / `#dcf5e3` | Approved, trusted, connected |

## 8. Extending the UI

1. Add a `<section id="x" data-title="X" hidden>` and a nav button to `index.html`.
2. Create `static/x.js` that defines `loadX()` and sets `loaders.x = loadX`. Include it after `common.js`.
3. Add the endpoint in `inbox/api.py`, with its logic in a service and a test in `tests/test_inbox_api.py`. `test_index_page_is_served` lists the static assets; add the new file there.
