# content-kb

[![tests](https://github.com/DrebotAI/content-kb/actions/workflows/test.yml/badge.svg)](https://github.com/DrebotAI/content-kb/actions/workflows/test.yml)

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
   - Tags (from a fixed vocabulary you can replace via `KB_TAGS`)
   - Two independent 3-level scales:
     - **Value** (Must-know / Useful / Reference) — learning & work value
     - **Content Potential** (Strong angle / Adaptable / Weak) — repackageable into creator's own content
   - Hook (first line of a Reel)
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

**Language.** `KB_LANGUAGE` sets the language of everything the AI writes — title, summary,
key ideas, hook, content angle: `uk` for Ukrainian (the default), `en` for English, or
`auto` to follow whatever language the content itself is in. The label vocabulary stored in
Notion (Value, Content Potential, Tags, Recommended Format) follows `uk` and `en`, but stays
Ukrainian under `auto`, because those are enum values in the database and cannot change from
one message to the next. `setup_notion.py` creates the select options from the same setting,
so choose the language *before* creating the database — switch it later and old entries keep
their old labels while the columns end up holding both sets.

## Requirements

- **Python** 3.12+ (production runs 3.12; tests are green up to 3.14)
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

Full setup guide, with the Instagram session details (Ukrainian):
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
| `ig_session_guardian.py` | Persistent Playwright browser; maintains Instagram session cookies; handles login challenges & proxy rotation |
| `test_*.py` | Unit tests — run with pytest, no network |

## Tests

Run tests with:
```bash
pytest
```

They need no network access and no API keys.

## Making it yours

This bot assumes the user is a content creator building a learning library. To adapt it to a different use case, there are three places to customize, in order:

1. **`context.md`** — your profile. The AI reads this on every analysis to decide what scores as valuable to you. Without it, everything defaults to reference material. This is the single biggest lever on scoring quality.
2. **`KB_LANGUAGE` and `KB_TAGS`** — output language and tag vocabulary. Set these before running `setup_notion.py` to create the database; changing them later breaks the label enum.
3. **`ai_engine.py`** — the scoring scales, criteria text, and repackaging rules live in one file. If your use case is not "building a learning library," rewrite the prompt in that file; it is the only place you need to touch to swap scoring logic or output format.

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

`ig-session-guardian` is optional and its proxy rotation is written against [GProxy](https://gproxy.net)'s API — treat `ig_session_guardian.py` as an example to adapt for your proxy provider, or skip it and export `cookies.txt` from your browser manually.

## License

MIT — see [LICENSE](LICENSE).
