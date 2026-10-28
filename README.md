# Marketing Agent

A marketing and sales outreach agent for a brand. It finds leads through **Hunter.io** and
**Instagram**, has **Claude** write a personalized message for each one, and sends them through
**Gmail** and **Instagram**. Nothing is sent until a person approves it.

```
 Discover                Enrich              Draft              Review           Send / Track
┌───────────────┐      ┌───────────┐      ┌───────────┐      ┌──────────┐      ┌──────────────────────┐
│ Hunter domain │─┐    │ Hunter    │      │ Claude    │      │ You:     │      │ Gmail (capped, spaced│
│ Instagram     │ ├──▶ │ domain →  │ ──▶  │ email/DM  │ ──▶  │ approve  │ ──▶  │  follow-ups, sync)   │
│ CSV import    │─┘    │ email +   │      │ drafts    │      │ edit     │      │ Instagram (assisted  │
└───────────────┘      │ verify    │      └───────────┘      │ reject   │      │  DMs, inbox replies) │
                       └───────────┘                         └──────────┘      └──────────────────────┘
                                         all state in SQLite (data/agent.db)
```

## How each channel works

| Channel | Discovery | Outreach |
|---|---|---|
| **Hunter.io** | Domain search (personal emails, filter by seniority/department), email verification | — |
| **Gmail** | — | Sends approved emails through the Gmail API with daily caps, random gaps between sends, threaded follow-ups, and reply/bounce/unsubscribe detection |
| **Instagram** | Business Discovery API (bio, website, followers) for business/creator accounts; hashtag search to find @-mentioned accounts | **Cold DMs:** an assisted queue. The message is copied to your clipboard and the DM thread opens, so you paste and send. **Inbound DMs:** Claude drafts a reply and the API sends it after you approve |

### Why Instagram cold DMs aren't fully automated

Meta's Instagram Messaging API does **not** let a business send the first message. It can only reply
to someone who has messaged it, and only within 24 hours. Tools that automate cold DMs use private,
reverse-engineered APIs. That breaks Instagram's Terms of Service and gets accounts restricted
or banned. This project keeps the brand's account safe: Claude does the research and writing, and a
person presses send. Each message takes a few seconds.

Instagram leads that list a website get passed to Hunter (`agent enrich`), so many of them can also be
reached by email.

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run agent init        # creates brand.md, .env and the database
```

Then fill in `brand.md` (who you are, who you're targeting, the offer, your voice). Every draft is
based on it, so be specific. `brand.md` holds business details (prices, phone, addresses), so it is
gitignored; `brand.example.md` is the committed template. After that, add credentials to `.env`:

### 1. Claude
Create a key at [console.anthropic.com](https://console.anthropic.com) and set `ANTHROPIC_API_KEY`.
The default model is `claude-opus-5-5` at `medium` effort. Change it with `ANTHROPIC_MODEL` / `ANTHROPIC_EFFORT`.

**Or use Amazon Bedrock** (any model your AWS account has access to):

```bash
LLM_PROVIDER=bedrock                              # auto-selected if BEDROCK_MODEL_ID is set and ANTHROPIC_API_KEY isn't
BEDROCK_MODEL_ID=global.anthropic.claude-opus-5-5 # or e.g. global.openai.gpt-5.5; use an inference-profile ID if
                                                  # the plain model ID isn't offered in your region
BEDROCK_REGION=ap-south-1                         # defaults to AWS_REGION
AWS_BEARER_TOKEN_BEDROCK=...                      # Bedrock API key
```

- Every model goes through the Bedrock Converse API with the Bedrock API key; replies are requested as
  JSON and validated, with one retry. Pick a model in **Settings → Model**: low-cost families (Amazon Nova
  Micro/Lite, gpt-oss, Claude Haiku, Ministral, Gemma) are listed first, **Test** makes one tiny call, and
  **Use this model** switches drafting and the strategist without a restart.
- Optional: `BEDROCK_USE_ANTHROPIC_SDK=1` sends Claude models through the Anthropic SDK's Bedrock client
  (SigV4 with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`), where your region serves it.
- Claude models on Bedrock also need Anthropic's one-time use-case form (Bedrock console → Model catalog).
- Enable the model first under **Bedrock console → Model access** in that region. "is not available for
  this account" means access hasn't been granted yet. Costs are billed by AWS, so the app shows token
  estimates instead of dollar amounts.

### 2. Hunter.io
Copy your key from [hunter.io/api-keys](https://hunter.io/api-keys) into `HUNTER_API_KEY`.
Each domain search uses one search credit and each verification uses one verification credit.
Pass `--no-verify` to save credits.

### 3. Gmail (Google Workspace)
1. Sign in to [Google Cloud Console](https://console.cloud.google.com) **with the Workspace account
   that will send the emails**, and create a project (e.g. `marketing-agent`).
2. **APIs & Services → Library →** search for *Gmail API* → **Enable**.
3. **Google Auth Platform → Branding:** set the app name and support email. Under **Audience**, choose
   **Internal**. Internal apps need no Google verification, and their refresh tokens don't expire
   after 7 days, which the unattended daily run depends on.
4. **Data Access → Add or remove scopes:** add `.../auth/gmail.send` and `.../auth/gmail.readonly`.
5. **Clients → Create client →** type **Desktop app** → download the JSON and save it as
   `credentials.json` in the project root (it's gitignored).
6. Set `SENDER_NAME`, `SENDER_EMAIL` and `PHYSICAL_ADDRESS` in `.env`, then run
   `uv run agent auth-gmail` and sign in as the sending mailbox.

If a Workspace admin has restricted third-party API access, they need to mark this app as trusted
(Admin console → Security → API controls). `gmail.readonly` is used only to detect replies and bounces.

### 4. Instagram (optional)
1. Convert the brand's Instagram to a **Business** or **Creator** account and link it to a Facebook Page.
2. Create a Meta app at [developers.facebook.com](https://developers.facebook.com) and add the
   *Instagram Graph API* and *Messenger* products.
3. Generate a Page access token with `instagram_basic`, `instagram_manage_messages`, `pages_show_list`,
   `pages_read_engagement` and `business_management`. For accounts other than your own, these
   permissions need Meta App Review.
4. Set `IG_ACCESS_TOKEN`, `IG_USER_ID` (the Instagram business account ID) and `IG_PAGE_ID`.
5. Subscribe the Page to the app's `messages` webhook field (App Dashboard → Webhooks, or
   `POST /{page-id}/subscribed_apps?subscribed_fields=messages`). Without this, replies fail with
   "(#10) This message is sent outside of allowed window".

An Instagram Login token (`IG…`) also works for messaging; the client picks `graph.instagram.com`
for it automatically. Hashtag search and Business Discovery need the Facebook Page token.

## Usage

```bash
# 0. Teach the agent how the business talks (once, then refine)
uv run agent ig-archive --days 90                        # save DM history locally; resumable, rate-limited
uv run agent build-brand --days 90                       # Claude drafts brand.draft.md - review, then rename to brand.md

# 1. Find leads
uv run agent find domain acme.com glossier.com --department marketing
uv run agent find instagram -u cafeluna -u bluebottle --min-followers 5000
uv run agent find instagram -t specialtycoffee           # accounts @-mentioned under a hashtag
uv run agent find csv leads.csv                          # name,email,company,domain,ig_username,...
uv run agent enrich                                      # Instagram website -> Hunter email
uv run agent brands run                                  # today's emerging womenswear brands (USA + India)
uv run agent brands seed muse.md                         # import brands found elsewhere (links, @handles or names)
uv run agent brands list                                 # what has been delivered

# 2. Draft with Claude, then review each draft
uv run agent draft --channel email --limit 20
uv run agent draft --channel instagram --limit 20
uv run agent review                                      # [a]pprove [e]dit [r]eject [s]kip

# 3. Send
uv run agent send --dry-run
uv run agent send                                        # Gmail, respects DAILY_EMAIL_CAP
uv run agent ig-queue                                    # assisted Instagram DMs

# 4. Track and follow up (the Lambda daily run does all of this for you)
uv run agent sync                                        # replies, bounces, unsubscribes
uv run agent draft --followups --limit 0                 # follow-ups for unanswered threads
uv run agent ig-inbox                                    # draft replies to inbound DMs (uses the chat history)
uv run agent ig-inbox --send                             # send approved replies, confirming each one
uv run agent status
```

## Inbox (web app for replying to DMs)

```bash
cp faqs.example.json faqs.json   # then write your real FAQ answers (faqs.json is gitignored)
uv run agent serve               # opens http://localhost:8000

cd /Users/karthikmac/Documents/Code/MinimiseLovable/MarketingAgent
git checkout feature/sales-app          # the inbox lives on this branch
PYTHONPATH=src uv run --no-sync python -m marketing_agent.cli serve

```

1. **Check for new messages** reads chats from the last 23 hours (read-only, rate-limited) and
   prepares a reply for every customer still waiting. Common questions are pre-filled from the FAQ.
   The app also does this by itself when it starts and every 15 minutes while it runs, and the Chat
   page reloads every 20 seconds (it never reloads over a reply you're typing).
2. **FAQ answers** tab: approve each answer once. From then on, replies that use an approved
   answer unchanged can all be approved with **Approve all** on the inbox.
3. Filter by priority (high = asking about price/samples, sent a reference image, a clothing brand, or
   the 24-hour window is closing), topic, customer type or "closing soon". Select chats to approve,
   fill in an FAQ answer, or dismiss in bulk. **Draft with Claude** writes a reply from the whole chat.
4. **Send approved** shows every message that will go out and sends only after you confirm. A reply is
   skipped if the window has closed, the customer was already answered in the Instagram app, or they
   unsubscribed. At most 25 per batch.

5. **Settings › Brand profile** edits `brand.md`, the profile the model reads before every draft and
   strategist answer. **Load the draft into the editor** brings in `brand.draft.md` (from
   `agent build-brand`). **Tune with AI** proposes a revised profile and lists each change; nothing is
   saved until you press Save. Every save is kept as a version you can reopen. New drafts use the
   saved profile straight away. The Lambda run uses the copy bundled at deploy, so redeploy after
   changing it. Whatever the profile says, drafts are written as the founder of a one-person business:
   simple, humble, only facts from the profile, and "let me check" instead of a guess.

Nothing is sent without approval and that final confirmation, with one opt-in exception:

**Auto-send trusted FAQ answers** (Settings, off by default). An FAQ answer is *trusted* once you
approve it in Settings, or once you've approved drafts using it unchanged in Chat 3 times. With
auto-send on, a draft whose text is an unchanged trusted answer shows **Auto-sends in 60s**. If
nobody edits it, clicks into it, presses **Hold**, dismisses it or sends it within that minute, the
app approves it as `auto` and sends it through the same send gates. A failed attempt is held for you,
never retried. Editing an FAQ answer removes its trust. Auto-send only runs while `agent serve` is
running; the daily Lambda job never auto-sends.

`agent serve` uses the local
database (`AGENT_DB_PATH`); it refuses to start when `AGENT_DB_S3_URI` is set.

The sidebar has four modules:

- **Chat** - the reply inbox above.
- **Sales** - the funnel built from the DM archive: New → Engaged → Qualified → Quoted →
  Sample/Order → Won, with lost leads and why they were lost (we didn't reply, never sent a
  reference, fabric must be provided, price, out of scope, went quiet), lead sources, weekly volume
  and reply times. Stages are inferred by rules (no API cost); drag a card to correct one. **Ask the
  strategist** sends the funnel numbers and a few anonymised lost chats to Claude after showing the
  estimated cost (about $0.07 per question).
- **Marketing** - reply rates per outreach message (other brands' instant auto-replies excluded)
  and how each lead source converts.
- **Settings** - status, FAQ answers (with how often each was approved, and whether it's trusted),
  auto-send, the lost-after window and the drafting context window.

**Drafting context.** All raw messages stay in the database. When Claude drafts a reply it sees the
last N days of the chat word for word (Settings, default 7) plus a short summary of everything
older, which is updated incrementally and stored per conversation (`ig_summaries`).

## Daily run on AWS Lambda

Every morning at **08:00 India time**, EventBridge Scheduler starts a Lambda function that runs the
same steps as `uv run agent daily`:

1. **Sync Gmail:** record replies, bounces and unsubscribes (so no one who just replied gets a follow-up)
2. **Send** the emails you approved since the last run, spaced to fit Lambda's 15-minute limit
3. **Discover** leads from queued targets (`agent targets add domain acme.com`, `... instagram user`, `... hashtag tag`)
4. **Enrich:** Hunter finds emails for Instagram leads with a website
5. **Draft** new emails, follow-ups and DMs with Claude
6. **Draft replies** to inbound Instagram DMs that are still inside the 24h window
7. **Email you a digest:** new replies, drafts waiting for review, errors

You then review on your laptop (`agent review`, `agent ig-queue`), and the next morning's run sends
what you approved. Nothing goes out without approval.

```
EventBridge Scheduler (08:00 Asia/Kolkata)
        │
        ▼
Lambda marketing-agent-daily ── Secrets Manager (API keys, Gmail refresh token)
        │
        ├── S3: agent.db (+ agent.db.lock) ◀── your laptop's `agent` CLI uses the same file
        └── CloudWatch alarm ──▶ SNS email if a run fails
```

The SQLite database lives in a versioned S3 bucket. Each run, and each CLI command, takes a lock
object, downloads the database, works on it and uploads it. If the Lambda run is in progress,
CLI commands wait for it and tell you to try again in a few minutes. S3 versioning keeps a backup
of every run for 90 days.

## Prospecting (Marketing › Prospecting in the app)

Finds emerging womenswear brands in any country and tracks them until they buy. Open
`uv run agent serve`, go to **Marketing › Prospecting** and press **Run search**. The search runs in the
background (about 12 minutes) and prospects appear as they are verified. `uv run agent brands run`
does the same from the terminal. Needs `SERPER_API_KEY` (or `BRAVE_SEARCH_API_KEY`) and the Instagram token.

1. **Search setup:** the countries to search (any of the 249 ISO countries; USA and India to start),
   each with its cities or states. **All states** fills in every state or province. Also keywords,
   Indian-craft terms (searched outside India), extra queries, words to exclude, and limits: prospects
   per run (all countries together) and at most so many from one country. Every keyword is searched
   with every place (`site:instagram.com "small batch clothing" "Brooklyn"`), interleaved across
   countries, about 100 searches a run. Each saved change is a version you can restore.
2. **Tune the search with AI:** say what you want ("more Indian-owned labels in Texas"). The model reads
   how each query performed (candidates, prospects, contacted) and why candidates were rejected, then
   proposes a new setup. The cost is shown first, and nothing changes until you apply the proposal.
3. **Verify:** every candidate is checked with Instagram's Business Discovery (followers, bio and last
   posts, word for word). It must be under 10K followers, have 3–800 posts and a post in the last 60 days,
   and be contemporary womenswear based in one of the searched countries. The country is read from the
   bio (country, state and city names, flag emoji) and the website's domain (.in, .co.uk), else the model.
4. **Work the prospects:** *Found → Shortlisted → Contacted* (or *Dismissed*). **Open DM** opens the
   Instagram chat; cold DMs are always sent by you. A prospect becomes *Contacted* on its own once an
   Instagram conversation with it exists.
5. **To close:** once a prospect replies, its chat moves through the Sales funnel like any other lead,
   under the source "Brand finder". **Sales › Prospecting → close** shows the whole path: found,
   shortlisted, contacted, replied, qualified, quoted, order, won.

Everything is stored in `agent.db`: `brand_candidates` (every handle checked, with its prospect stage),
`brand_searches` (each run), `brand_search_queries` (each search and what it found) and
`brand_search_configs` (setup versions).

Only official APIs are used: web search APIs and the Instagram Graph API. Instagram is never scraped.

### Deploy

Requires Node.js (for `npx aws-cdk`) and AWS credentials (`aws sts get-caller-identity`). Docker is not needed.

```bash
# one time per AWS account/region
npx aws-cdk@latest bootstrap aws://<ACCOUNT_ID>/us-east-1

scripts/deploy.sh                      # builds build/lambda (with brand.md) and deploys the stack
uv run agent push-secrets              # uploads API keys + Gmail token from .env to Secrets Manager
```

Then set `AGENT_DB_S3_URI` in `.env` to the `DatabaseUri` output (e.g.
`s3://marketing-agent-data-<account>/agent.db`). Your local database is uploaded the first time you
run a command. Confirm the SNS subscription email so you receive failure alerts.

Run it once by hand to check everything is wired up:

```bash
aws lambda invoke --function-name marketing-agent-daily --invocation-type Event /dev/null
aws logs tail /aws/lambda/marketing-agent-daily --follow
```

**Redeploy after changing `brand.md` or non-secret `.env` settings** (`scripts/deploy.sh`), and
re-run `agent push-secrets` after changing API keys. Cost is a few cents a month for AWS (one
short run per day, a small S3 object, one secret), plus Claude and Hunter usage.

## Deliverability and compliance safeguards

- **Daily cap** (`DAILY_EMAIL_CAP`, default 30 over a rolling 24h) and **randomized spacing** between sends.
  New Google Workspace inboxes should start at 10–20 a day and ramp up. Set up SPF, DKIM and DMARC
  for the sending domain.
- **CAN-SPAM:** every email ends with your physical address and an unsubscribe line, and carries a
  `List-Unsubscribe` header. The agent refuses to send if `PHYSICAL_ADDRESS` is empty.
- **Suppression list:** replies containing "unsubscribe", "remove me", "not interested" and similar,
  plus bounces and manual `agent unsubscribe <email|username>` entries, cancel queued messages and
  block future contact.
- **Hunter verification:** first emails only go to addresses verified as `valid` or `accept_all`.
- **Any reply stops the sequence.** Follow-ups stop after `MAX_FOLLOWUPS` (default 2), spaced
  `FOLLOWUP_AFTER_DAYS` apart.
- For EU/UK prospects, make sure you have a legitimate-interest basis under GDPR/PECR before emailing.

## Project structure

```
src/marketing_agent/
├── cli.py              # Typer commands (entry point: `agent`)
├── config.py           # Settings from environment / .env
├── db.py               # SQLite: leads, messages, targets, suppression list
├── storage.py          # opens the database locally or via S3 with a lock
├── clients.py          # builds Gmail / Hunter / Instagram / Claude clients from settings
├── pipeline.py         # discover → enrich → draft → send → sync steps
├── daily.py            # the daily job + digest email
├── lambda_handler.py   # Lambda entry point (loads secrets, runs daily.py or the brand job)
├── personalize.py      # Claude drafting (structured outputs, cached brand prompt)
├── brands/             # daily emerging-brand list
│   ├── sweeps.py       # candidate sources: web search, brand sites, snowball, hashtags
│   ├── criteria.py     # numeric checks, keyword rules, region and founder extraction
│   ├── judge.py        # model check: contemporary womenswear? based where?
│   └── finder.py       # orchestration, ledger, run file, delivery
├── sources/
│   ├── hunter.py       # Hunter.io v2: domain search, email finder, verifier
│   ├── instagram.py    # Graph API: business discovery, hashtags, conversations, replies
│   └── web_search.py   # Brave / Serper search + Instagram handle parsing
└── channels/
    └── gmail.py        # OAuth, MIME building, sending, thread/bounce inspection
infra/app.py            # AWS CDK stack: Lambda, S3, Secrets Manager, schedule, alarm
scripts/                # build_lambda.sh, deploy.sh
tests/                  # offline tests (HTTP, Gmail and S3 are mocked)
```

**Message lifecycle:** `pending` → `approved` → `sent` (or `rejected` / `failed`).
**Lead lifecycle:** `new` → `contacted` → `replied` / `bounced` / `unsubscribed`.

## Development

```bash
uv run pytest
```

The tests make no network calls. Hunter, the Graph API and Claude are mocked at the HTTP layer,
and Gmail is replaced with a fake.
