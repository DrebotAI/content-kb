# tg-sorter

A Telegram bot that turns saved content into structured knowledge base entries in Notion. Transcribes audio, OCRs images, analyzes with AI, and writes rich Notion pages.

tg-sorter solves the problem of capturing fleeting content (Instagram reels, TikTok videos, forwarded messages, voice notes, images, links) and turning it into a queryable knowledge base. It handles the entire pipeline: download, extract, transcribe, analyze, and store.

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
Batch debounce (25 sec): combine consecutive messages into one entry
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

## Requirements

- **Python** 3.12 (what production runs and tests are green on)
- **External services** (environment variables; never commit values):
  - `TELEGRAM_BOT_TOKEN` — from @BotFather
  - `DEEPGRAM_API_KEY` — speech-to-text
  - `IG_COOKIES_FILE` — Instagram session cookies (optional, for stories)
  - `IG_PROXY_URL`, `IG_USER_AGENT` — optional proxy/headers for IG
  - `NOTION_TOKEN` (or `env:NAME` refs in tenants.json) — per-user Notion integration tokens
  - `CODEX_BIN`, `CODEX_MODEL`, `CODEX_REASONING`, `CODEX_TIMEOUT_SECONDS` — Codex CLI configuration (optional; defaults: `codex`, `gpt-5.6-sol`, `medium`, 300 s)
  - `BATCH_DEBOUNCE_SECONDS` — message batching window (default 25)
  - `TENANTS_FILE` — path to tenants.json (default: repo root)
- **CLI binaries** (installed via `requirements.txt` or system package manager):
  - `ffmpeg` — extracts frames from silent videos, muxes audio
  - `codex` — AI analysis & image OCR (must be logged in: `codex login`)
  - `yt-dlp` — downloads IG/TikTok media (installed via `requirements.txt`)

## Quick start

1. **Clone** the repo and enter the directory:
   ```bash
   git clone <repo> tg-sorter && cd tg-sorter
   ```

2. **Install** Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment**:
   ```bash
   cp .env.example .env   # then fill in the values
   ```

4. **Configure tenants**:
   ```bash
   cp tenants.example.json tenants.json
   # Edit tenants.json: replace telegram_id, notion_token refs, database_id, context_file
   ```

5. **Create Notion database** (per tenant, in their Notion workspace):
   ```bash
   python setup_notion.py <notion-page-url-or-id> env:NOTION_TOKEN
   ```
   This creates the "Knowledge Base" database with the required schema and prints a block ready to paste into `tenants.json`.

6. **Verify setup**:
   ```bash
   python doctor.py                # all tenants
   python doctor.py owner --probe  # test the owner's connection
   ```

7. **Start the bot**:
   ```bash
   python bot.py
   ```

Full setup guide (Ukrainian): [SETUP.md](SETUP.md)

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
| `test_*.py` | Unit tests (8 files, 104 tests) |

## Tests

Run tests with:
```bash
pytest
```

Test files: `test_bot.py`, `test_notion_store.py`, `test_ai_engine.py`, `test_instagram.py`, `test_tenants.py`, `test_transcribe.py`, `test_delivery.py`, `test_ig_session_guardian.py` (8 files).

## Deployment

Runs as a systemd service. Unit files in `deploy/`:

| Unit | Purpose |
|------|---------|
| `tg-sorter.service` | the bot itself |
| `ig-session-guardian.service` | keeps the Instagram session cookie alive |
| `ig-session-guardian.timer` | schedules the guardian |

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-sorter
```

Adjust `User=` and `WorkingDirectory=` to match your host.

## License

MIT — see [LICENSE](LICENSE).
