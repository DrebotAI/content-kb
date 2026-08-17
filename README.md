# content-kb

A Telegram bot that turns saved content into structured knowledge base entries in Notion. Transcribes audio, OCRs images, analyzes with AI, and writes rich Notion pages.

content-kb solves the problem of capturing fleeting content (Instagram reels, TikTok videos, forwarded messages, voice notes, images, links) and turning it into a queryable knowledge base. It handles the entire pipeline: download, extract, transcribe, analyze, and store.

## What it does

Accepts these input types from Telegram:

- **IG/TikTok links** — downloads and transcribes audio (or extracts frames from silent videos for OCR), then analyzes
- **Instagram stories** — downloads all stories from a user, transcribes/OCRs each, compiles into one entry
- **Photos & carousels** — OCRs the images and any caption
- **Voice notes, audio, video notes** — transcribes to text
- **Text messages** — batches them into one entry if sent together
- **Forwarded messages** — preserves creator attribution

For each piece of content, the bot:

1. **Extracts & downloads** media (audio track, image frames, or screenshots)
2. **Transcribes** audio via Deepgram (with multilingual support and product-name keyterm boosting)
3. **OCRs** images via Codex CLI
4. **Analyzes** the combined text via Codex AI to extract:
   - Title, TLDR, summary
   - Key ideas, practical takeaways, learning actions
   - Tags (from: content-idea, product/course, delivery, sales, lead-gen)
   - Two independent 3-level scales:
     - **Value** (Must-know / Useful / Reference) — learning & work value
     - **Content Potential** (Strong angle / Adaptable / Weak) — repackageable into creator's own content
   - Hook (first line of a Reel, in Ukrainian)
   - Content angle and recommended format (Reel, carousel, case study, etc.)
   - Adaptation steps to turn it into original content
5. **Saves to Notion** as a rich page with:
   - All metadata as queryable properties (Name, Source, Value, Tags, Creator, Content Angle, Hook, Recommended Format, etc.)
   - Summary & key ideas as formatted blocks
   - Learning takeaway and practical steps as bullet lists
   - Transcription in a searchable property + expandable toggle block
   - Link to the original

## How it works

```
Telegram message
    ↓
[Link / Photo / Voice / Text]
    ↓
Download media (yt-dlp; ffmpeg for frames)
    ↓
Transcribe (Deepgram) or OCR (Codex) images
    ↓
Batch debounce (25 sec): hold consecutive messages
    ↓
If more than one: Codex stitches them into a single document
    ↓
AI analysis (Codex CLI, model gpt-5.6-sol)
    ↓
Save to Notion (rich page + properties)
    ↓
✅ Reply to user with title, TLDR, link
```

Messages sent rapidly are batched into one entry; silence for `BATCH_DEBOUNCE_SECONDS` (default 25 s) triggers processing. Voice transcriptions can also be returned as text without saving to the database (via `/voice` command).

## Multi-tenant

One bot process, multiple Notion database owners. Configured in `tenants.json`:

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
    "notion_database_id": "...",
    "context_file": "context.collaborator.md"
  }
]
```

Each tenant has:
- **telegram_id** — only messages from this Telegram user are processed; others ignored
- **notion_token** — their own Notion integration token
- **notion_database_id** — their Notion Knowledge Base database
- **context_file** — their personal profile/goals (used by AI to calibrate analysis; e.g., one person's must-know is another's reference material)

The same bot serves all tenants from one process. Without `tenants.json`, it falls back to single-user mode via `.env` variables (`ALLOWED_USER_ID`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`).

## Before you start

This is a self-hosted bot wired to accounts you own. Get these four things first —
the Quick start assumes you have them.

| What | Where | Cost |
|------|-------|------|
| Telegram bot token | [@BotFather](https://t.me/BotFather) → `/newbot` | free |
| Your numeric Telegram id | start your new bot, send it `/id` — it answers anyone | free |
| Deepgram API key | [deepgram.com](https://deepgram.com) — speech-to-text | paid, per minute of audio (free credit to start) |
| Notion integration token + a page | see [Notion setup](#notion-setup) below | free |

Plus the `codex` CLI — see [Requirements](#requirements).

**This is not free to run.** Deepgram bills per minute of audio and the Codex CLI needs a
paid account. Telegram's and Notion's APIs are free at this volume.

**Language.** Analysis follows the language of the content, except the `hook` and
`content_angle` fields, which are always Ukrainian — that is hardcoded in the prompt in
`ai_engine.py`. Change those lines to target another language.

## Requirements

- **Python** 3.12 (what production runs and tests are green on)
- **CLI binaries** — these are *not* installed by `requirements.txt`, get them separately:
  - `ffmpeg` — extracts frames from silent videos and the audio track
  - `codex` — the [OpenAI Codex CLI](https://github.com/openai/codex). It does the content
    analysis and the image OCR, called as a subprocess (`codex exec`). Install it per its own
    README, then run `codex login` once. It is *not* what transcribes audio — that is Deepgram.
    To swap in a different model or tool, `ai_engine.py` is the only file that shells out to it.
- **Installed for you** by `requirements.txt`: `yt-dlp` (downloads IG/TikTok media),
  `playwright` (only for the optional Instagram session guardian), the Telegram, Deepgram
  and Notion SDKs.
- **Environment variables** — full annotated list in [`.env.example`](.env.example). The ones
  you must set: `TELEGRAM_BOT_TOKEN`, `DEEPGRAM_API_KEY`, and either a `tenants.json` or
  `ALLOWED_USER_ID` + `NOTION_TOKEN` + `NOTION_DATABASE_ID`. Everything else has a working
  default.

## Notion setup

Do this once per knowledge-base owner, in that person's own Notion workspace.

1. Go to **Notion → Settings → Connections → Internal connections → Create a new connection**.
   Only a Workspace Owner can create one. Capabilities needed: Read, Update, Insert content.
2. Copy the **Internal Integration Token** (`ntn_…`) — this is your `NOTION_TOKEN`.
3. Create an empty Notion page that will hold the database.
4. On that page: **•••** → **Connections** → **Add connection** → pick your integration.
   Skipping this step is the single most common failure — the token exists but sees nothing.
5. Let `setup_notion.py` build the database and its schema (Quick start step 4).

## Quick start

```bash
git clone https://github.com/DrebotAI/content-kb.git && cd content-kb
```

1. **Install** Python dependencies, plus `ffmpeg` and `codex` (see Requirements):
   ```bash
   pip install -r requirements.txt
   codex login
   ```

2. **Set up environment** — the file is commented, fill in the values you have:
   ```bash
   cp .env.example .env
   ```

3. **Write your profile.** `context.md` is a plain-text description of who you are and what
   you care about; the AI reads it on every analysis to decide whether a piece of content is
   valuable *to you*. Without it everything scores as "reference material".
   ```bash
   cp context.example.md context.md   # then rewrite it as yourself
   ```
   Edits take effect on the next message — no restart.

4. **Create the Notion database** (after [Notion setup](#notion-setup) above):
   ```bash
   python setup_notion.py <notion-page-url> env:NOTION_TOKEN
   ```
   Creates the "Knowledge Base" database with the required schema and prints a config block.

5. **Configure owners** — single-user setups can skip this and use the `.env` variables instead:
   ```bash
   cp tenants.example.json tenants.json   # paste in the block from step 4
   ```

6. **Verify** before you trust it:
   ```bash
   python doctor.py                # check every owner's token, schema and IG session
   python doctor.py owner --probe  # also create and archive a real test page
   ```

7. **Start the bot**, then send it an Instagram link:
   ```bash
   python bot.py
   ```

Full setup guide, with the Notion migration and Instagram session details (Ukrainian):
[SETUP.md](SETUP.md)

## Repo layout

| File | Purpose |
|------|---------|
| `bot.py` | Main Telegram bot loop; message handlers for links, photos, voice, text; batch debouncing; `/id` and `/voice` commands |
| `notion_store.py` | Notion API client; saves pages with properties & blocks; checks schema; retries on transient errors |
| `ai_engine.py` | Codex CLI subprocess wrapper; AI analysis (JSON parsing, value/potential scoring); image OCR; message digest compilation; profile fallback |
| `instagram.py` | yt-dlp downloader wrapper; handles IG reels/stories/posts and TikTok; audio extraction; silent video frame extraction; story batch download |
| `transcribe.py` | Deepgram API client; speech-to-text with keyterm boosting (Claude Code, product names, etc.) |
| `tenants.py` | Multi-tenant config parser; loads `tenants.json` or `.env` fallback; validates & caches tenant registry |
| `delivery.py` | Telegram message sending utility; splits large text (>3500 chars) into files |
| `setup_notion.py` | One-time database creator; builds schema and prints config block for new tenants |
| `doctor.py` | Pre-deployment health check; verifies Notion access, schema, Instagram cookies, token validity |
| `backfill.py` | Schema migration & content re-scoring; adds missing columns; fills empty fields in existing pages |
| `ig_session_guardian.py` | Persistent Playwright browser; maintains Instagram session cookies; handles login challenges & proxy rotation |
| `test_*.py` | Unit tests (8 files, 105 tests) |

## Tests

Run tests with:
```bash
pytest
```

105 tests, no network access — they run offline.

Test files: `test_bot.py`, `test_notion_store.py`, `test_ai_engine.py`, `test_instagram.py`, `test_tenants.py`, `test_transcribe.py`, `test_delivery.py`, `test_ig_session_guardian.py` (8 files).

## Deployment

Runs as a systemd service. Unit files in `deploy/`:

| Unit | Purpose |
|------|---------|
| `content-kb.service` | the bot itself |
| `ig-session-guardian.service` | keeps the Instagram session cookie alive |
| `ig-session-guardian.timer` | schedules the guardian |

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now content-kb
```

Adjust `User=` and `WorkingDirectory=` to match your host.

## License

MIT — see [LICENSE](LICENSE).
