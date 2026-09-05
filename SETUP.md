# Setup

## What it does today
`content_kb/bot.py` accepts IG links, TikTok links, forwarded posts, images and voice notes
→ analysis via Codex → an entry in Notion.
One process serves several database owners: whoever wrote is the base it writes to.

Accepted input:
- Instagram: posts, reels, stories, profiles
- TikTok: videos
- Telegram: forwarded messages, images, voice notes, text
- Silent video: the frames are read as images
- Carousel posts and multi-image messages: every slide → OCR → one entry

## 1. Dependencies

### Python packages
```
pip install -e .
```

### Binaries (install separately)

The code calls these CLI tools directly through `subprocess`. Make sure they are on `$PATH`:

- **`ffmpeg`** — extracts frames from silent videos and the audio track
- **`codex`** — post analysis and image OCR; installed separately and must be logged in
  (`codex login`). It is **not** what transcribes audio — Deepgram is
  (`content_kb/transcribe.py`)

yt-dlp is already in pyproject.toml (via pip); no separate install needed.

## 2. `.env` — what is shared by everyone

### Required
- `TELEGRAM_BOT_TOKEN` — the token from @BotFather
- `DEEPGRAM_API_KEY` — for transcribing voice notes
- Notion tokens: one per database owner, named however you like (`NOTION_TOKEN`,
  `COLLABORATOR_NOTION_TOKEN`), referenced from `tenants.json` through `env:`

### Optional
- `BATCH_DEBOUNCE_SECONDS` — how long to wait for the next message before stitching a batch
  into one entry (default 25 s; at 0, every message is its own entry)
- `KB_LANGUAGE` — the language of everything the AI writes (title, summary, key ideas, hook,
  angle): `en` (default), `uk`, or `auto` (the language of the content itself; the labels
  stay English in that case)
- `KB_TAGS` — your own comma-separated tag vocabulary, e.g. `"research, hiring, competitors"`
  (default: content idea, product/course, delivery, sales, lead gen)
- `CODEX_BIN` — path to `codex` if it is not on `$PATH` (default `"codex"`)
- `CODEX_MODEL` — the model used for post analysis (default `"gpt-5.6-sol"`)
- `CODEX_REASONING` — Codex reasoning effort (default `"medium"`)
- `CODEX_TIMEOUT_SECONDS` — timeout for a single analysis, in seconds (default 300)
- `IG_COOKIES_FILE` — cookie file for stories (optional; not needed for anonymous posts)
- `IG_USER_AGENT` — User-Agent for Instagram requests (optional)
- `IG_PROXY_URL` — proxy for IG requests (optional)
- `IG_BROWSER_PROFILE` — browser profile directory for ig_session_guardian
  (default `~/.cache/content-kb/ig-browser-profile`)
- `GPROXY_API_KEY` — key for automatic proxy generation (optional)
- `GPROXY_API_URL` — proxy API URL (default `https://gproxy.net/api/v1/proxy/generate/`)
- `GPROXY_COUNTRY` — proxy country (default `"VN"`)
- `IG_USERNAME` — for ig_session_guardian auto-login (optional)
- `IG_PASSWORD` — for ig_session_guardian auto-login (optional)
- `TENANTS_FILE` — path to `tenants.json` (default: the project root)

**Set `KB_LANGUAGE` and `KB_TAGS` BEFORE running `setup_notion.py`** — the database schema
is built from them. You can change them later, but old entries keep their old labels and
the select columns end up holding both sets.
- `CONTEXT_FILE` — for the old single-user mode without `tenants.json` (default `"context.md"`)
- `ALLOWED_USER_ID`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`, `TENANT_NAME` — for the old mode
  without tenants.json

## 3. `tenants.json` — who writes to which base

It lives in the project root and stays out of git. Example: `tenants.example.json`:

```json
[
  {
    "name": "owner",
    "telegram_id": 111111111,
    "notion_token": "env:NOTION_TOKEN",
    "notion_database_id": "0123456789abcdef0123456789abcdef",
    "context_file": "context.md"
  },
  {
    "name": "collaborator",
    "telegram_id": 222222222,
    "notion_token": "env:COLLABORATOR_NOTION_TOKEN",
    "notion_database_id": "https://www.notion.so/Knowledge-Base-0123…?v=…",
    "context_file": "context.collaborator.md"
  }
]
```

- `telegram_id` — the numeric id. Anyone not on the list is ignored entirely.
  To find it: send the bot `/id`, it answers anyone.
- `notion_database_id` — you can paste the whole database URL; the id is extracted
  (and it will not confuse it with the view id in `?v=`).
- `notion_token` — either the value itself, or `env:VARIABLE_NAME` from `.env`.
- `context_file` — the owner's profile, used to calibrate value. **Everyone has their own.**
  With no file the bot rates without context and mostly assigns 📎 rather than measuring
  someone else's content against your deals. Template: `context.example.md`.

With no `tenants.json` at all, the config is assembled from `.env`
(`ALLOWED_USER_ID` + `NOTION_TOKEN` + `NOTION_DATABASE_ID`), as it used to be.

## 4. Notion Knowledge Base — separately for each owner

The owner does this themselves, in their own workspace:

1. https://app.notion.com/developers/connections → in the sidebar **Internal connections**
   → **Create a new connection** → name it and pick your workspace.
   Only a **Workspace Owner** can create one.
2. **Configuration** tab → copy the **Installation access token** (`ntn_…`).
   Capabilities: Read, Update, Insert content.
3. Create an empty page and call it "Knowledge Base".
4. On that page: **•••** (top right) → **Connections** → **+ Add connection** → pick your
   integration → confirm.
5. Send the operator: the token, the link to the page, and your Telegram ID (`/id` to the bot).

Notion renamed integrations → connections; the old `notion.so/my-integrations` address still
redirects, but the UI there is already different.

Then:
```
python tools/setup_notion.py <link to the page> env:COLLABORATOR_NOTION_TOKEN
```
— creates the database with the right schema and prints the block for `tenants.json`.

**If the database already exists**: `tools/setup_notion.py` is not needed, just put the id
into `tenants.json`. But columns then have to be added through the new API version:
databases created in Notion after 2025-09-03 keep their schema in a *data source*, and
`PATCH /v1/databases/<id>` on the old API version silently changes nothing
(200 OK, same schema).

The client is pinned to `Notion-Version: 2022-06-28` (`content_kb/notion_store.py`,
`tools/setup_notion.py`) — creating pages with `parent: database_id` works on it and has
been verified with a live write.

## 5. Verification before handing the bot to someone

```
python tools/doctor.py                       # every tenant
python tools/doctor.py collaborator --probe  # also create a test page and archive it
```
Catches exactly what every new owner trips over: the wrong token, the integration not added
under Connections, missing columns, a dead Instagram session.

## 6. Codex CLI

`codex` must be logged in on the server (`codex login`). Before the first run, check
`codex --help` — `content_kb/ai_engine.py` calls `codex exec` in non-interactive mode.

## 7. Instagram session: keeping it alive

If stories are downloaded regularly, the session goes stale within days. Instead of
refreshing the cookies by hand, run the guardian as a systemd timer:

```bash
sudo cp deploy/ig-session-guardian.service deploy/ig-session-guardian.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ig-session-guardian.timer
```

The `tools/ig_session_guardian.py` service:
- Opens a browser and logs into Instagram through the UI (even when 2FA is required)
- Extracts the cookies and writes them to `IG_COOKIES_FILE`
- Can rotate proxies automatically through GProxy (if `GPROXY_API_KEY` is set)
- Runs on a timer; it is not needed continuously

It is optional — without it stories cannot be downloaded, but everything else
(posts, reels, TikTok) works anonymously.

## 8. Bot modes

- Forward an IG/TikTok link / post / image / voice note → an entry in your base plus a card
  in reply
- `/voice`, then a batch of voice notes → transcripts only, nothing written to the base
  (turns off after 60 s of silence; the mode is per chat, not global)
- Several texts in a row → one entry after `BATCH_DEBOUNCE_SECONDS` (default 25 s of
  silence), stitched together by Codex
- A carousel or multi-image message → OCR of every slide → one entry
- Silent video → frames extracted → read as images
- `/id` → your numeric Telegram ID; answers anyone

## 9. Running it

Test:
```
python -m content_kb.bot
```
For continuous operation (systemd) the unit is in `deploy/`. Adjust `User=` and
`WorkingDirectory=` for your host:
```bash
sudo cp deploy/content-kb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now content-kb
```

If the unit is already installed on the server under the old name (`tg-sorter.service`)
there is no need to rename it — just use `systemctl` with the old name.

## 10. Tests

```
pytest
```
The tests touch no network — run them locally and on the server before restarting.
