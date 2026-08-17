import asyncio
import os
from types import SimpleNamespace

for var in ("DEEPGRAM_API_KEY", "TELEGRAM_BOT_TOKEN"):
    os.environ.setdefault(var, "test")

import tenants
from tenants import Tenant

KENT = Tenant("kent", 42, "ntn_kent", "11111111-1111-1111-1111-111111111111",
              "context.kent.md")
OWNER = Tenant("owner", 7, "ntn_owner", "22222222-2222-2222-2222-222222222222")
tenants._cache = {t.telegram_id: t for t in (KENT, OWNER)}

import bot
from bot import (_process_stories, _queue_item, _tenant, batch_meta,
                 creator_from_forward, links_from)


def _item(text, creator="", is_voice=False):
    return {"text": text, "creator": creator, "is_voice": is_voice}


def _update(user_id):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


class _Jobs:
    def __init__(self):
        self.scheduled = []

    def get_jobs_by_name(self, name):
        return []

    def run_once(self, callback, when, **kwargs):
        self.scheduled.append(kwargs)


class _Ctx:
    def __init__(self):
        self.job_queue = _Jobs()


def test_each_user_lands_in_own_base():
    assert _tenant(_update(42)).notion_database_id == KENT.notion_database_id
    assert _tenant(_update(7)).notion_database_id == OWNER.notion_database_id


def test_stranger_is_ignored():
    assert _tenant(_update(999999)) is None
    assert _tenant(SimpleNamespace(effective_user=None)) is None


def test_batch_job_carries_the_tenant():
    """Через 25 с оригінального update вже нема — тенант має їхати з джобою,
    інакше пачка кента пішла б у мою базу."""
    bot._pending.clear()
    ctx = _Ctx()
    assert _queue_item(ctx, 5, KENT, "перше", "", False) is True
    assert _queue_item(ctx, 5, KENT, "друге", "", False) is False  # репліка тільки на перше
    assert ctx.job_queue.scheduled[-1]["data"] is KENT
    assert len(bot._pending[5]) == 2
    bot._pending.clear()


def test_voice_mode_is_per_chat():
    """Був глобальний прапорець: кент вмикав /voice — і мої голосові теж переставали
    писатись у базу."""
    bot._voice_mode.clear()
    bot._voice_mode.add(555)
    assert 555 in bot._voice_mode and 777 not in bot._voice_mode
    bot._voice_mode.clear()


def test_batch_with_any_voice_is_voice_source():
    assert batch_meta([_item("текст"), _item("голос", is_voice=True)]) == ("", "Voice")


def test_batch_of_text_only_is_telegram():
    assert batch_meta([_item("а"), _item("б")]) == ("", "Telegram")


def test_batch_creator_is_first_non_empty():
    items = [_item("а"), _item("б", creator="@channel"), _item("в", creator="@other")]
    assert batch_meta(items)[0] == "@channel"


def test_all_links_taken_not_just_first():
    text = ("глянь https://www.instagram.com/reel/AAA/ і ще "
            "https://www.instagram.com/reel/BBB/, та https://www.instagram.com/p/CCC/")
    assert links_from(text) == [
        "https://www.instagram.com/reel/AAA/",
        "https://www.instagram.com/reel/BBB/",   # кома в кінці не має прилипати
        "https://www.instagram.com/p/CCC/",
    ]


def test_duplicate_links_in_one_message_collapse():
    text = "https://www.instagram.com/reel/AAA/ https://www.instagram.com/reel/AAA/"
    assert links_from(text) == ["https://www.instagram.com/reel/AAA/"]


def test_bare_profile_link_becomes_stories():
    assert links_from("https://www.instagram.com/somebody/") == \
        ["https://www.instagram.com/stories/somebody/"]


def test_mixed_story_batch_is_saved_once_with_full_ordered_transcript(monkeypatch):
    items = [
        {"kind": "images", "paths": ["photo.jpg"]},
        {"kind": "audio", "paths": ["voice.mp3"]},
        {"kind": "images", "paths": ["frame1.jpg", "frame2.jpg"]},
    ]
    monkeypatch.setattr(bot.instagram, "download_stories",
                        lambda url: (items, {"creator": "@somebody", "source": "IG Story"}))
    monkeypatch.setattr(bot.ai_engine, "read_image",
                        lambda paths, caption: "OCR:" + ",".join(paths))
    monkeypatch.setattr(bot.transcribe, "transcribe_file", lambda path: "SPEECH:" + path)
    monkeypatch.setattr(bot.ai_engine, "compile_digest", lambda parts: "DIGEST:" + "|".join(parts))

    saved = []
    async def fake_save(*args, **kwargs):
        saved.append(kwargs)
    monkeypatch.setattr(bot, "_save_and_reply", fake_save)

    class _Bot:
        async def send_message(self, *args, **kwargs):
            pass
    context = SimpleNamespace(bot=_Bot())
    asyncio.run(_process_stories(
        context, 5, KENT, "https://www.instagram.com/stories/somebody/"))

    assert len(saved) == 1
    assert saved[0]["content"] == \
        "DIGEST:OCR:photo.jpg|SPEECH:voice.mp3|OCR:frame1.jpg,frame2.jpg"
    assert saved[0]["transcript"] == (
        "OCR:photo.jpg\n\n--- STORY 2 ---\n\nSPEECH:voice.mp3"
        "\n\n--- STORY 3 ---\n\nOCR:frame1.jpg,frame2.jpg")


def test_channel_with_username():
    msg = SimpleNamespace(forward_origin=SimpleNamespace(
        chat=SimpleNamespace(username="channel", title="Канал"), sender_user=None))
    assert creator_from_forward(msg) == "@channel"


def test_channel_title_only():
    msg = SimpleNamespace(forward_origin=SimpleNamespace(
        chat=SimpleNamespace(username=None, title="Канал"), sender_user=None))
    assert creator_from_forward(msg) == "Канал"


def test_user_origin():
    msg = SimpleNamespace(forward_origin=SimpleNamespace(
        chat=None, sender_user=SimpleNamespace(username="dude", full_name="Dude")))
    assert creator_from_forward(msg) == "@dude"


def test_hidden_origin_falls_back_to_name():
    msg = SimpleNamespace(forward_origin=SimpleNamespace(
        chat=None, sender_user=None, sender_user_name="Хтось"))
    assert creator_from_forward(msg) == "Хтось"


def test_not_forwarded():
    assert creator_from_forward(SimpleNamespace(forward_origin=None)) == ""


if __name__ == "__main__":
    for _name, _fn in sorted(dict(globals()).items()):
        if _name.startswith("test_"):
            _fn()
    print("ok")


def test_links_from_picks_tiktok():
    text = "гля https://www.tiktok.com/@a/video/1 і https://vm.tiktok.com/ZMabc/."
    assert links_from(text) == ["https://www.tiktok.com/@a/video/1",
                                "https://vm.tiktok.com/ZMabc/"]


def test_links_from_preserves_tiktok_query_parameters():
    url = "https://www.tiktok.com/@nick/video/123?_r=1&_t=ZS-test"
    assert links_from(url) == [url]


def test_tiktok_extractor_error_is_not_masked_as_image_post(monkeypatch):
    monkeypatch.setattr(bot.notion_store, "find_by_link", lambda tenant, url: None)

    def fail_download(url):
        raise RuntimeError("TikTok extractor unavailable")

    monkeypatch.setattr(bot.instagram, "download_audio", fail_download)
    image_fallbacks = []

    async def fake_image_fallback(*args, **kwargs):
        image_fallbacks.append((args, kwargs))

    monkeypatch.setattr(bot, "_process_image_post", fake_image_fallback)

    messages = []

    class _Bot:
        async def send_message(self, chat_id, text):
            messages.append(text)

    asyncio.run(bot._process_link(
        SimpleNamespace(bot=_Bot()), 5, KENT,
        "https://vt.tiktok.com/ZSVNB2bLU?share_app_id=1233"))

    assert image_fallbacks == []
    assert messages[-1] == "❌ Не скачав TikTok-відео: TikTok extractor unavailable"
