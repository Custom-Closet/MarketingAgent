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
based on it, so be specific. After that, add credentials to `.env`:

### 1. Claude
Create a key at [console.anthropic.com](https://console.anthropic.com) and set `ANTHROPIC_API_KEY`.
The default model is `claude-opus-5-5` at `medium` effort. Change it with `ANTHROPIC_MODEL` / `ANTHROPIC_EFFORT`.

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
├── lambda_handler.py   # Lambda entry point (loads secrets, runs daily.py)
├── personalize.py      # Claude drafting (structured outputs, cached brand prompt)
├── sources/
│   ├── hunter.py       # Hunter.io v2: domain search, email finder, verifier
│   └── instagram.py    # Graph API: business discovery, hashtags, conversations, replies
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
