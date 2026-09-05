import asyncio
import logging
import os
import re
import shutil
import tempfile

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.error import NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import ai_engine, instagram, notion_store, tenants, transcribe
from .delivery import send_text_or_file

logging.basicConfig(level=logging.INFO)
# httpx logs the full request URL, and the bot token is in it; in journald that is forever.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

VOICE_MODE_IDLE_SECONDS = 60
# how long to wait for the next message before stitching a batch into one entry
BATCH_DEBOUNCE_SECONDS = int(os.getenv("BATCH_DEBOUNCE_SECONDS", "25"))
LINK_URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)?(?:instagram\.com|tiktok\.com)/\S+")

# chat ids with /voice enabled. This used to be a global flag — with two database
# owners that would mean one of them turning transcription mode on for the other too.
_voice_mode = set()

# chat_id -> parts of the current batch; voice notes and texts sent in a row are one thought
_pending = {}


def batch_meta(items: list) -> tuple:
    """(creator, source) for a stitched batch: voice wins, creator is the first non-empty one."""
    creator = next((i["creator"] for i in items if i["creator"]), "")
    source = "Voice" if any(i["is_voice"] for i in items) else "Telegram"
    return creator, source


def _queue_item(context, chat_id: int, tenant, text: str | None, creator: str, is_voice: bool,
                *, paths: list | None = None, tmp_dir: str | None = None, caption: str = "") -> bool:
    first = not _pending.get(chat_id)
    item = {"creator": creator, "is_voice": is_voice}
    if paths is not None:
        # ponytail: a photo carries paths+tmp_dir instead of text — OCR and directory
        # cleanup both wait until the flush
        item.update(paths=paths, tmp_dir=tmp_dir, caption=caption)
    else:
        item["text"] = text
    _pending.setdefault(chat_id, []).append(item)
    job_name = f"batch-{chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    # the tenant rides along with the job: by flush time the original update is long gone
    context.job_queue.run_once(
        _flush_batch, BATCH_DEBOUNCE_SECONDS, chat_id=chat_id, name=job_name, data=tenant)
    return first


async def _flush_batch(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    tenant = context.job.data
    items = _pending.pop(chat_id, [])
    if not items:
        return
    creator, source = batch_meta(items)

    photo_items = [i for i in items if "paths" in i]
    ocr_text = None
    try:
        if photo_items:
            # every carousel slide in a single Codex call, rather than one call per slide
            all_paths = [p for i in photo_items for p in i["paths"]]
            caption = next((i["caption"] for i in photo_items if i["caption"]), "")
            ocr_text = await asyncio.to_thread(ai_engine.read_image, all_paths, caption)
    except Exception as e:
        logger.exception("photo batch OCR failed")
        rest = "\n\n---\n\n".join(i["text"] for i in items if "text" in i)
        if rest:  # text and voice notes from the same batch must not vanish over one image
            await _rescue(context, chat_id, f"❌ Codex could not read the images: {e}", rest)
        else:
            await context.bot.send_message(chat_id, f"❌ Codex could not read the images: {e}")
        return
    finally:
        # ponytail: one mkdtemp directory per slide — simpler than sharing a single
        # directory across the separate updates of a carousel; cleaned up right after use
        for i in photo_items:
            shutil.rmtree(i["tmp_dir"], ignore_errors=True)

    parts, photo_placed = [], False
    for i in items:
        if "paths" in i:
            if not photo_placed:
                parts.append(ocr_text)
                photo_placed = True
        else:
            parts.append(i["text"])

    transcript = "\n\n---\n\n".join(parts)
    content = await _digest(context, chat_id, parts, transcript,
                            f"📚 Stitching {len(parts)} messages into one entry…")
    if content is None:
        return
    await _save_and_reply(context, chat_id, tenant, content=content, link=None,
                          creator=creator, source=source, transcript=transcript)


def _tenant(update: Update):
    """The tenant who owns this message, or None — in which case the bot stays silent."""
    user = update.effective_user
    return tenants.get(user.id) if user else None


def creator_from_forward(message) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return ""
    chat = getattr(origin, "chat", None)
    if chat is not None:
        return f"@{chat.username}" if chat.username else (chat.title or "")
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return f"@{user.username}" if user.username else (user.full_name or "")
    return getattr(origin, "sender_user_name", "") or ""


async def _rescue(context, chat_id: int, reason: str, transcript: str) -> None:
    """The tail of the pipeline failed — but the transcript is already paid for, in
    minutes and in Deepgram credit. Hand it to the user so the same media does not have
    to be downloaded and transcribed twice."""
    await context.bot.send_message(chat_id, f"{reason}\n\n📄 The transcript is not lost:")
    try:
        await send_text_or_file(context.bot, chat_id, transcript, "transcript.txt")
    except Exception:
        logger.exception("failed to hand over the transcript after a failure")


async def _digest(context, chat_id: int, parts: list[str], transcript: str, notice: str) -> str | None:
    """One part goes through as-is. Several are stitched by Codex into one entry.
    A failed stitch rescues the already-paid-for transcript and returns None."""
    if len(parts) == 1:
        return parts[0]
    await context.bot.send_message(chat_id, notice)
    try:
        return await asyncio.to_thread(ai_engine.compile_digest, parts)
    except Exception as e:
        logger.exception("digest failed")
        await _rescue(context, chat_id, f"❌ Codex could not stitch the batch: {e}", transcript)
        return None


async def _save_and_reply(context, chat_id: int, tenant, content: str, link: str | None,
                          creator: str, source: str, transcript: str) -> None:
    try:
        analysis = await asyncio.to_thread(
            ai_engine.analyze, content, link or "", tenant.profile_path)
    except Exception as e:
        logger.exception("analyze failed")
        await _rescue(context, chat_id, f"❌ Codex could not analyze it: {e}", transcript)
        return
    try:
        page_url = await asyncio.to_thread(
            notion_store.save_entry, tenant, analysis, link, creator, source, transcript)
    except Exception as e:
        logger.exception("notion save failed [%s]", tenant.name)
        await _rescue(context, chat_id, f"❌ Notion did not save it: {e}", transcript)
        return
    await context.bot.send_message(
        chat_id, f"✅ {analysis['title']}\n\n{analysis['tldr']}\n\n{page_url}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answers anyone — this number is how a new database owner gets into tenants.json."""
    user = update.effective_user
    tenant = tenants.get(user.id) if user else None
    known = f"\n\nYou are already connected as «{tenant.name}»." if tenant else \
        "\n\nYou are not in the config yet — send this number to the bot's owner."
    await update.message.reply_text(f"Your Telegram ID: `{user.id}`{known}",
                                    parse_mode="Markdown")


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _tenant(update) is None:
        return
    chat_id = update.effective_chat.id
    _voice_mode.add(chat_id)
    _reset_voice_mode_timer(context, chat_id)
    await update.message.reply_text(
        "🎙 Transcription mode: voice notes come back as text, nothing is written to the base. "
        f"Turns itself off after {VOICE_MODE_IDLE_SECONDS} s of silence.")


def _reset_voice_mode_timer(context, chat_id: int) -> None:
    job_name = f"voice-mode-off-{chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    context.job_queue.run_once(
        _voice_mode_off, VOICE_MODE_IDLE_SECONDS, name=job_name, data=chat_id)


async def _voice_mode_off(context) -> None:
    _voice_mode.discard(context.job.data)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tenant = _tenant(update)
    if tenant is None:
        return
    msg = update.message
    chat_id = update.effective_chat.id
    media = msg.voice or msg.audio or msg.video_note or msg.video
    try:
        tg_file = await media.get_file()
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = os.path.join(tmp_dir, "media")
            await tg_file.download_to_drive(local_path)
            transcript = await asyncio.to_thread(transcribe.transcribe_file, local_path)
    except Exception as e:
        logger.exception("transcription failed")
        await context.bot.send_message(chat_id, f"❌ Could not transcribe: {e}")
        return
    if chat_id in _voice_mode:
        _reset_voice_mode_timer(context, chat_id)
        await send_text_or_file(context.bot, chat_id, transcript, "transcript.txt")
        return
    if _queue_item(context, chat_id, tenant, transcript, creator_from_forward(msg), is_voice=True):
        await context.bot.send_message(
            chat_id, f"📥 Got it. Send more — I'll stitch them into one entry. {BATCH_DEBOUNCE_SECONDS} s of silence and I write.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tenant = _tenant(update)
    if tenant is None:
        return
    msg = update.message
    chat_id = msg.chat_id
    # a carousel arrives as a separate update per slide — reply only to the first,
    # otherwise 10 slides mean 10 identical messages
    if not _pending.get(chat_id):
        await context.bot.send_message(
            chat_id, f"📸 Reading the image… Send more — I'll stitch them into one entry, {BATCH_DEBOUNCE_SECONDS} s of silence and I write.")
    try:
        # photo[-1] is the largest size; Telegram's small previews are not worth OCR-ing
        tg_file = await msg.photo[-1].get_file()
        # ponytail: one mkdtemp directory per slide — a carousel is N separate updates,
        # and a shared TemporaryDirectory would have to be coordinated across them; OCR
        # of all the slides together happens once, at the flush (_flush_batch)
        tmp_dir = tempfile.mkdtemp()
        path = os.path.join(tmp_dir, "photo.jpg")
        await tg_file.download_to_drive(path)
    except Exception as e:
        logger.exception("photo download failed")
        await context.bot.send_message(chat_id, f"❌ Could not download the image: {e}")
        return
    _queue_item(context, chat_id, tenant, None, creator_from_forward(msg), is_voice=False,
               paths=[path], tmp_dir=tmp_dir, caption=msg.caption or "")


def links_from(text: str) -> list:
    """Every IG/TikTok link in the message, deduplicated and without trailing punctuation."""
    seen = []
    for raw in LINK_URL_RE.findall(text):
        url = instagram.profile_to_stories(raw.rstrip(".,);:»\"'"))
        if url not in seen:
            seen.append(url)
    return seen


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tenant = _tenant(update)
    if tenant is None:
        return
    chat_id = update.effective_chat.id
    urls = links_from(update.message.text)
    if len(urls) > 1:
        await update.message.reply_text(f"📥 Found {len(urls)} links — taking them one by one, a separate entry each")
    for i, url in enumerate(urls, 1):
        await _process_link(context, chat_id, tenant, url, f"[{i}/{len(urls)}] " if len(urls) > 1 else "")


def _story_texts(items: list[dict]) -> list[str]:
    texts = []
    for item in items:
        if item["kind"] == "audio":
            texts.append(transcribe.transcribe_file(item["paths"][0]))
        else:
            texts.append(ai_engine.read_image(item["paths"], ""))
    return texts


async def _process_stories(context, chat_id: int, tenant, url: str, tag: str = "") -> None:
    try:
        items, meta = await asyncio.to_thread(instagram.download_stories, url)
    except Exception as e:
        logger.exception("story batch download failed")
        await context.bot.send_message(chat_id, f"{tag}❌ Could not download the stories: {e}")
        return
    await context.bot.send_message(chat_id, f"{tag}📚 Processing all {len(items)} stories into one entry…")
    try:
        parts = await asyncio.to_thread(_story_texts, items)
    except Exception as e:
        logger.exception("story transcription/OCR failed")
        await context.bot.send_message(chat_id, f"{tag}❌ Could not read the stories: {e}")
        return
    transcript = parts[0] + "".join(
        f"\n\n--- STORY {index} ---\n\n{text}" for index, text in enumerate(parts[1:], 2))
    content = await _digest(context, chat_id, parts, transcript,
                            f"{tag}📚 Stitching {len(parts)} stories into one entry…")
    if content is None:
        return
    await _save_and_reply(context, chat_id, tenant, content=content, link=url,
                          creator=meta["creator"], source=meta["source"], transcript=transcript)


async def _process_link(context, chat_id: int, tenant, url: str, tag: str = "") -> None:
    try:  # check BEFORE downloading: otherwise we pay Deepgram for what is already stored
        existing = await asyncio.to_thread(notion_store.find_by_link, tenant, url)
    except Exception:
        logger.exception("duplicate check failed — downloading as usual")
        existing = None
    if existing:
        await context.bot.send_message(chat_id, f"{tag}♻️ Already in the base:\n{existing}")
        return
    await context.bot.send_message(chat_id, f"{tag}⏳ Downloading…")
    if "/stories/" in url:
        await _process_stories(context, chat_id, tenant, url, tag)
        return
    try:
        paths, meta = await asyncio.to_thread(instagram.download_audio, url)
    except instagram.NoAudio as silent:
        # there is video but no sound: the content is all on screen — read the frames as slides
        await _process_image_post(context, chat_id, tenant, url, tag, silent=silent)
        return
    except Exception as e:
        if instagram.source_from_url(url) == "TikTok":
            # TikTok video extraction failures are not evidence of an image post.
            # Do not hide the real downloader error behind a misleading thumbnail error.
            logger.warning("TikTok video download failed: %s", e)
            error = " ".join(str(e).split())[:400] or type(e).__name__
            await context.bot.send_message(chat_id, f"{tag}❌ Could not download the TikTok video: {error}")
            return
        # an Instagram post with no video is not an error — it is an image or a carousel
        logger.warning("no audio (%s) — trying it as an image post", e)
        await _process_image_post(context, chat_id, tenant, url, tag)
        return

    if len(paths) > 1:
        await context.bot.send_message(chat_id, f"🎙 Transcribing {len(paths)} of them…")
    transcripts, skipped = [], 0
    for path in paths:
        try:
            transcripts.append(await asyncio.to_thread(transcribe.transcribe_file, path))
        except Exception as e:  # one silent story must not take down the whole batch
            logger.warning("skipping %s: %s", path, e)
            skipped += 1
    if not transcripts:
        await context.bot.send_message(chat_id, "❌ No speech anywhere — nothing to write")
        return

    transcript = "\n\n---\n\n".join(transcripts)
    notice = f"📚 Stitching {len(transcripts)} of them into one entry" + \
        (f" (no speech in {skipped})" if skipped else "")
    content = await _digest(context, chat_id, transcripts, transcript, notice)
    if content is None:
        return

    await _save_and_reply(context, chat_id, tenant, content=content, link=url,
                          creator=meta["creator"], source=meta["source"], transcript=transcript)


async def _process_image_post(context, chat_id: int, tenant, url: str, tag: str = "",
                              silent=None) -> None:
    """A post with no speech: either the slides of the post, or frames from a silent video."""
    try:
        if silent is not None:
            paths, meta = await asyncio.to_thread(instagram.frames, silent.videos), silent.meta
            meta.setdefault("caption", "")
        else:
            paths, meta = await asyncio.to_thread(instagram.download_images, url)
    except Exception as e:
        logger.exception("image post download failed")
        await context.bot.send_message(chat_id, f"{tag}❌ Could not download it: {e}")
        return
    what = f"🔇 Video with no sound — reading {len(paths)} frame(s)…" if silent is not None else \
        f"📸 No video — reading {len(paths)} image(s) and the caption…"
    await context.bot.send_message(chat_id, f"{tag}{what}")
    try:
        # all the slides in one call: a carousel is a single train of thought, not N separate ones
        content = await asyncio.to_thread(ai_engine.read_image, paths, meta["caption"])
    except Exception as e:
        logger.exception("image read failed")
        await _rescue(context, chat_id, f"❌ Codex could not read the images: {e}", meta["caption"])
        return
    await _save_and_reply(context, chat_id, tenant, content=content, link=url,
                          creator=meta["creator"], source=meta["source"], transcript=content)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tenant = _tenant(update)
    if tenant is None:
        return
    msg = update.message
    if _queue_item(context, msg.chat_id, tenant, msg.text, creator_from_forward(msg), is_voice=False):
        await msg.reply_text(
            f"📥 Got it. Send more — I'll stitch them into one entry. {BATCH_DEBOUNCE_SECONDS} s of silence and I write.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # a dropped long-polling connection is routine; without a handler PTB dumps a full
    # traceback for every one of them
    if isinstance(context.error, NetworkError):
        logger.warning("network: %s", context.error)
        return
    logger.error("unhandled error", exc_info=context.error)


def main() -> None:
    # read the config before polling starts: better to fail here with a legible message
    # than to silently ignore messages from a live database owner
    registry = tenants.load()
    for tenant in registry.values():
        logger.info("tenant %s (id %s) → base %s, profile %s",
                    tenant.name, tenant.telegram_id, tenant.notion_database_id,
                    tenant.profile_path.name)

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.VIDEO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(LINK_URL_RE), handle_link))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
