import html
import io

from telegram import Bot
from telegram.constants import ParseMode

# ponytail: 4096 — ліміт Telegram; запас на <pre>-обгортку та екранування &lt;/&amp;
MAX_MESSAGE_LEN = 3500


async def send_text_or_file(bot: Bot, chat_id: int, text: str, filename: str) -> None:
    if not text:
        return
    if len(text) <= MAX_MESSAGE_LEN:
        # моноспейс: Telegram малює <pre> блоком з кнопкою "копіювати"
        await bot.send_message(
            chat_id, f"<pre>{html.escape(text)}</pre>", parse_mode=ParseMode.HTML)
    else:
        await bot.send_document(chat_id, document=io.BytesIO(text.encode("utf-8")), filename=filename)
