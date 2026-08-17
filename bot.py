import asyncio
import logging
import os
import re
import tempfile

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.error import NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import ai_engine
import instagram
import notion_store
import tenants
import transcribe
from delivery import send_text_or_file

logging.basicConfig(level=logging.INFO)
# httpx логує повний URL запиту, а в ньому — токен бота; у journald це назавжди.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

VOICE_MODE_IDLE_SECONDS = 60
# скільки чекати наступного повідомлення, перш ніж зшити пачку в один запис
BATCH_DEBOUNCE_SECONDS = int(os.getenv("BATCH_DEBOUNCE_SECONDS", "25"))
LINK_URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)?(?:instagram\.com|tiktok\.com)/\S+")

# chat_id-и з увімкненим /voice. Був глобальний прапорець — на двох власниках
# баз це означало б, що кент вмикає режим транскрипції заразом і мені.
_voice_mode = set()

# chat_id -> шматки поточної пачки; голосові й тексти, надіслані підряд, — це одна думка
_pending = {}


def batch_meta(items: list) -> tuple:
    """(creator, source) для зшитої пачки: голос переважає, автор — перший непорожній."""
    creator = next((i["creator"] for i in items if i["creator"]), "")
    source = "Voice" if any(i["is_voice"] for i in items) else "Telegram"
    return creator, source


def _queue_item(context, chat_id: int, tenant, text: str, creator: str, is_voice: bool) -> bool:
    first = not _pending.get(chat_id)
    _pending.setdefault(chat_id, []).append(
        {"text": text, "creator": creator, "is_voice": is_voice})
    job_name = f"batch-{chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    # тенант їде разом із джобою: до моменту флашу оригінального update вже нема
    context.job_queue.run_once(
        _flush_batch, BATCH_DEBOUNCE_SECONDS, chat_id=chat_id, name=job_name, data=tenant)
    return first


async def _flush_batch(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    tenant = context.job.data
    items = _pending.pop(chat_id, [])
    if not items:
        return
    parts = [i["text"] for i in items]
    transcript = "\n\n---\n\n".join(parts)
    creator, source = batch_meta(items)

    if len(parts) == 1:
        content = parts[0]
    else:
        await context.bot.send_message(chat_id, f"📚 Зшиваю {len(parts)} повідомлень в один запис…")
        try:
            content = await asyncio.to_thread(ai_engine.compile_digest, parts)
        except Exception as e:
            logger.exception("digest failed")
            await _rescue(context, chat_id, f"❌ Codex не зшив пачку: {e}", transcript)
            return
    await _save_and_reply(context, chat_id, tenant, content=content, link=None,
                          creator=creator, source=source, transcript=transcript)


def _tenant(update: Update):
    """Tenant власника цього повідомлення або None — тоді бот мовчить."""
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
    """Хвіст пайплайну впав — але транскрипт уже оплачений хвилинами й Deepgram-ом.
    Віддаємо його користувачу, щоб не качати й не транскрибувати те саме вдруге."""
    await context.bot.send_message(chat_id, f"{reason}\n\n📄 Транскрипт не загубився:")
    try:
        await send_text_or_file(context.bot, chat_id, transcript, "transcript.txt")
    except Exception:
        logger.exception("не віддав транскрипт після падіння")


async def _save_and_reply(context, chat_id: int, tenant, content: str, link: str | None,
                          creator: str, source: str, transcript: str) -> None:
    try:
        analysis = await asyncio.to_thread(
            ai_engine.analyze, content, link or "", tenant.profile_path)
    except Exception as e:
        logger.exception("analyze failed")
        await _rescue(context, chat_id, f"❌ Codex не проаналізував: {e}", transcript)
        return
    try:
        page_url = await asyncio.to_thread(
            notion_store.save_entry, tenant, analysis, link, creator, source, transcript)
    except Exception as e:
        logger.exception("notion save failed [%s]", tenant.name)
        await _rescue(context, chat_id, f"❌ Notion не зберіг: {e}", transcript)
        return
    await context.bot.send_message(
        chat_id, f"✅ {analysis['title']}\n\n{analysis['tldr']}\n\n{page_url}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Відповідає всім — саме цим числом новий власник бази прописується в tenants.json."""
    user = update.effective_user
    tenant = tenants.get(user.id) if user else None
    known = f"\n\nТи вже підключений як «{tenant.name}»." if tenant else \
        "\n\nТебе ще нема в конфігу — скинь це число власнику бота."
    await update.message.reply_text(f"Твій Telegram ID: `{user.id}`{known}",
                                    parse_mode="Markdown")


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _tenant(update) is None:
        return
    chat_id = update.effective_chat.id
    _voice_mode.add(chat_id)
    _reset_voice_mode_timer(context, chat_id)
    await update.message.reply_text(
        "🎙 Режим транскрипції: голосові повертаю текстом, у базу не пишу. "
        f"Вимкнеться сам через {VOICE_MODE_IDLE_SECONDS} с тиші.")


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
        await context.bot.send_message(chat_id, f"❌ Не транскрибував: {e}")
        return
    if chat_id in _voice_mode:
        _reset_voice_mode_timer(context, chat_id)
        await send_text_or_file(context.bot, chat_id, transcript, "transcript.txt")
        return
    if _queue_item(context, chat_id, tenant, transcript, creator_from_forward(msg), is_voice=True):
        await context.bot.send_message(
            chat_id, f"📥 Прийняв. Кидай ще — зшию в один запис. Тиша {BATCH_DEBOUNCE_SECONDS} с — записую.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tenant = _tenant(update)
    if tenant is None:
        return
    msg = update.message
    chat_id = msg.chat_id
    # карусель прилітає окремим апдейтом на кожен слайд — репліка тільки на перший,
    # інакше на 10 слайдів буде 10 однакових повідомлень
    if not _pending.get(chat_id):
        await context.bot.send_message(
            chat_id, f"📸 Читаю картинку… Кидай ще — зшию в один запис, тиша {BATCH_DEBOUNCE_SECONDS} с — записую.")
    try:
        # photo[-1] — найбільший розмір; дрібні прев'ю Telegram не варто віддавати на OCR
        tg_file = await msg.photo[-1].get_file()
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "photo.jpg")
            await tg_file.download_to_drive(path)
            text = await asyncio.to_thread(ai_engine.read_image, [path], msg.caption or "")
    except Exception as e:
        logger.exception("photo read failed")
        await context.bot.send_message(chat_id, f"❌ Не прочитав картинку: {e}")
        return
    _queue_item(context, chat_id, tenant, text, creator_from_forward(msg), is_voice=False)


def links_from(text: str) -> list:
    """Усі IG/TikTok-лінки повідомлення, без дублів і без хвостової пунктуації."""
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
        await update.message.reply_text(f"📥 Знайшов {len(urls)} лінків — беру по черзі, окремим записом кожен")
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
        await context.bot.send_message(chat_id, f"{tag}❌ Не скачав stories: {e}")
        return
    await context.bot.send_message(chat_id, f"{tag}📚 Обробляю всі {len(items)} stories в один запис…")
    try:
        parts = await asyncio.to_thread(_story_texts, items)
    except Exception as e:
        logger.exception("story transcription/OCR failed")
        await context.bot.send_message(chat_id, f"{tag}❌ Не розпізнав stories: {e}")
        return
    transcript = parts[0] + "".join(
        f"\n\n--- STORY {index} ---\n\n{text}" for index, text in enumerate(parts[1:], 2))
    try:
        content = await asyncio.to_thread(ai_engine.compile_digest, parts) if len(parts) > 1 else parts[0]
    except Exception as e:
        logger.exception("story digest failed")
        await _rescue(context, chat_id, f"❌ Codex не зшив stories: {e}", transcript)
        return
    await _save_and_reply(context, chat_id, tenant, content=content, link=url,
                          creator=meta["creator"], source=meta["source"], transcript=transcript)


async def _process_link(context, chat_id: int, tenant, url: str, tag: str = "") -> None:
    try:  # перевірка ДО качання: інакше платимо Deepgram-у за те, що вже в базі
        existing = await asyncio.to_thread(notion_store.find_by_link, tenant, url)
    except Exception:
        logger.exception("перевірка дубля не вдалась — качаю як звичайно")
        existing = None
    if existing:
        await context.bot.send_message(chat_id, f"{tag}♻️ Це вже в базі:\n{existing}")
        return
    await context.bot.send_message(chat_id, f"{tag}⏳ Качаю…")
    if "/stories/" in url:
        await _process_stories(context, chat_id, tenant, url, tag)
        return
    try:
        paths, meta = await asyncio.to_thread(instagram.download_audio, url)
    except instagram.NoAudio as silent:
        # відео є, звуку нема: увесь зміст на екрані — читаємо кадри як слайди
        await _process_image_post(context, chat_id, tenant, url, tag, silent=silent)
        return
    except Exception as e:
        if instagram.source_from_url(url) == "TikTok":
            # TikTok video extraction failures are not evidence of an image post.
            # Do not hide the real downloader error behind a misleading thumbnail error.
            logger.warning("TikTok video download failed: %s", e)
            error = " ".join(str(e).split())[:400] or type(e).__name__
            await context.bot.send_message(chat_id, f"{tag}❌ Не скачав TikTok-відео: {error}")
            return
        # Instagram post без відео — це не помилка, а картинка чи карусель слайдів
        logger.warning("аудіо не вийшло (%s) — пробую як пост із картинок", e)
        await _process_image_post(context, chat_id, tenant, url, tag)
        return

    if len(paths) > 1:
        await context.bot.send_message(chat_id, f"🎙 Транскрибую {len(paths)} шт…")
    transcripts, skipped = [], 0
    for path in paths:
        try:
            transcripts.append(await asyncio.to_thread(transcribe.transcribe_file, path))
        except Exception as e:  # німа сторі не має валити всю пачку
            logger.warning("пропускаю %s: %s", path, e)
            skipped += 1
    if not transcripts:
        await context.bot.send_message(chat_id, "❌ Ніде немає мовлення — записувати нічого")
        return

    if len(transcripts) > 1:
        note = f"📚 Зшиваю {len(transcripts)} шт в один запис"
        await context.bot.send_message(chat_id, note + (f" (без мовлення: {skipped})" if skipped else ""))
        try:
            content = await asyncio.to_thread(ai_engine.compile_digest, transcripts)
        except Exception as e:
            logger.exception("digest failed")
            await context.bot.send_message(chat_id, f"❌ Codex не зшив пачку: {e}")
            return
    else:
        content = transcripts[0]

    await _save_and_reply(context, chat_id, tenant, content=content, link=url,
                          creator=meta["creator"], source=meta["source"],
                          transcript="\n\n---\n\n".join(transcripts))


async def _process_image_post(context, chat_id: int, tenant, url: str, tag: str = "",
                              silent=None) -> None:
    """Пост без мовлення: або слайди поста, або кадри з німого відео."""
    try:
        if silent is not None:
            paths, meta = await asyncio.to_thread(instagram.frames, silent.videos), silent.meta
            meta.setdefault("caption", "")
        else:
            paths, meta = await asyncio.to_thread(instagram.download_images, url)
    except Exception as e:
        logger.exception("image post download failed")
        await context.bot.send_message(chat_id, f"{tag}❌ Не скачав: {e}")
        return
    what = f"🔇 Відео без звуку — читаю {len(paths)} кадр(и)…" if silent is not None else \
        f"📸 Відео нема — читаю {len(paths)} картинк(и) й підпис…"
    await context.bot.send_message(chat_id, f"{tag}{what}")
    try:
        # усі слайди одним запитом: карусель — це один хід думки, не N окремих
        content = await asyncio.to_thread(ai_engine.read_image, paths, meta["caption"])
    except Exception as e:
        logger.exception("image read failed")
        await _rescue(context, chat_id, f"❌ Codex не прочитав картинки: {e}", meta["caption"])
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
            f"📥 Прийняв. Кидай ще — зшию в один запис. Тиша {BATCH_DEBOUNCE_SECONDS} с — записую.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # обрив long-polling — рутина; без хендлера PTB сипле повний трейсбек на кожен
    if isinstance(context.error, NetworkError):
        logger.warning("мережа: %s", context.error)
        return
    logger.error("необроблена помилка", exc_info=context.error)


def main() -> None:
    # конфіг читаємо до старту polling: краще впасти тут із зрозумілим текстом,
    # ніж мовчки ігнорувати повідомлення живого власника бази
    registry = tenants.load()
    for tenant in registry.values():
        logger.info("тенант %s (id %s) → база %s, профіль %s",
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
