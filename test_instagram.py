import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

import instagram
from instagram import (InstagramSessionInvalid, NoAudio, _apply_ydl_proxy, _entries, _meta, _purge_old,
                       _story_json, profile_to_stories, source_from_url, story_media_plan)


def test_bare_profile_becomes_stories_url():
    assert profile_to_stories("https://www.instagram.com/some_creator/") == \
        "https://www.instagram.com/stories/some_creator/"
    assert profile_to_stories("https://instagram.com/some_creator") == \
        "https://www.instagram.com/stories/some_creator/"


def test_story_item_url_becomes_whole_active_story_feed():
    assert profile_to_stories("https://instagram.com/stories/user/3961050217333492442/") == \
        "https://www.instagram.com/stories/user/"


def test_story_media_plan_keeps_photo_and_video_stories_in_order():
    payload = {"reels": {"77": {"items": [
        {"pk": "1", "image_versions2": {"candidates": [
            {"url": "https://cdn/small.jpg", "width": 100, "height": 100},
            {"url": "https://cdn/photo.jpg", "width": 1080, "height": 1920},
        ]}},
        {"pk": "2", "has_audio": True, "video_versions": [
            {"url": "https://cdn/small.mp4", "width": 360, "height": 640},
            {"url": "https://cdn/video.mp4", "width": 1080, "height": 1920},
        ]},
        {"pk": "3", "has_audio": False, "video_versions": [
            {"url": "https://cdn/silent.mp4", "width": 1080, "height": 1920},
        ]},
    ]}}}

    assert story_media_plan(payload, "77") == [
        {"id": "1", "kind": "image", "url": "https://cdn/photo.jpg"},
        {"id": "2", "kind": "video", "url": "https://cdn/video.mp4", "has_audio": True},
        {"id": "3", "kind": "video", "url": "https://cdn/silent.mp4", "has_audio": False},
    ]


def test_story_json_reports_invalid_session_instead_of_post_fallback():
    response = SimpleNamespace(
        url="https://www.instagram.com/accounts/login/?next=/api/v1/feed/reels_media/",
        status_code=200,
    )
    client = SimpleNamespace(get=lambda *args, **kwargs: response)

    with pytest.raises(InstagramSessionInvalid, match="Instagram session invalid"):
        _story_json(client, "/api/v1/feed/reels_media/")


def test_proxy_is_applied_to_httpx_story_client(monkeypatch, tmp_path):
    cookiefile = tmp_path / "cookies.txt"
    cookiefile.write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\ttest-session\n")
    captured = {}
    monkeypatch.setenv("IG_PROXY_URL", "http://user:pass@proxy.example:1000")
    monkeypatch.setattr(instagram.httpx, "Client",
                        lambda **kwargs: captured.update(kwargs) or SimpleNamespace())
    instagram._story_client(str(cookiefile))
    assert captured["proxy"] == "http://user:pass@proxy.example:1000"


def test_proxy_is_applied_to_ytdlp_options(monkeypatch):
    monkeypatch.setenv("IG_PROXY_URL", "http://user:pass@proxy.example:1000")
    monkeypatch.delenv("IG_USER_AGENT", raising=False)
    opts = _apply_ydl_proxy({"quiet": True})
    assert opts == {"quiet": True, "proxy": "http://user:pass@proxy.example:1000"}


def test_instagram_user_agent_is_applied_to_ytdlp(monkeypatch):
    monkeypatch.delenv("IG_PROXY_URL", raising=False)
    monkeypatch.setenv("IG_USER_AGENT", "Instagram Android Test UA")
    assert _apply_ydl_proxy({}) == {
        "http_headers": {"User-Agent": "Instagram Android Test UA"}}


def test_tiktok_bypasses_instagram_proxy_and_user_agent(monkeypatch):
    monkeypatch.setenv("IG_PROXY_URL", "http://user:pass@proxy.example:1000")
    monkeypatch.setenv("IG_USER_AGENT", "Instagram Android Test UA")
    for url in (
        "https://vt.tiktok.com/ZSVNB2bLU?share_app_id=1233",
        "https://www.tiktok.com/@nick/video/7647032788402867487?_r=1&_t=test",
    ):
        assert _apply_ydl_proxy({"quiet": True}, url) == {"quiet": True}


def test_reels_posts_and_story_feed_untouched():
    for u in ("https://www.instagram.com/reel/ABC/",
              "https://instagram.com/stories/user/",
              "https://www.instagram.com/p/XYZ/"):
        assert profile_to_stories(u) == u


def test_entries_flattens_playlist_and_single():
    assert _entries({"entries": [{"id": "a"}, {"id": "b"}]}) == [{"id": "a"}, {"id": "b"}]
    assert _entries({"id": "a"}) == [{"id": "a"}]


def test_meta_takes_uploader_from_first_entry_of_playlist():
    info = {"entries": [{"uploader_id": "dude"}, {"uploader_id": "dude"}]}
    assert _meta("https://instagram.com/p/A/", info)["creator"] == "@dude"


def test_stories_creator_comes_from_url_not_numeric_id():
    # yt-dlp для сторіз віддає числовий id — нік беремо з лінка
    meta = _meta("https://www.instagram.com/stories/some_creator/", {"uploader_id": "1256089223"})
    assert meta == {"creator": "@some_creator", "source": "IG Story"}


def test_source_detection():
    assert source_from_url("https://www.instagram.com/reel/ABC/") == "IG Reel"
    assert source_from_url("https://instagram.com/stories/user/123/") == "IG Story"
    assert source_from_url("https://www.instagram.com/p/XYZ/") == "IG Post"


def test_meta_prefixes_username():
    meta = _meta("https://instagram.com/reel/A/", {"uploader_id": "some_creator"})
    assert meta == {"creator": "@some_creator", "source": "IG Reel"}


def test_reel_creator_prefers_channel_over_numeric_id_and_display_name():
    # yt-dlp: channel=нік, uploader_id=число, uploader=відображуване ім'я
    meta = _meta("https://instagram.com/reel/A/", {
        "channel": "ksyushafedorova", "uploader_id": "565100657",
        "uploader": "Маркетинг, нейросети и система",
    })
    assert meta["creator"] == "@ksyushafedorova"


def test_numeric_only_falls_back_to_display_name():
    meta = _meta("https://instagram.com/reel/A/", {"uploader_id": "565100657", "uploader": "Ксюша"})
    assert meta["creator"] == "@Ксюша"


def test_meta_missing_uploader():
    assert _meta("https://instagram.com/p/A/", {})["creator"] == ""


def _silent_mp4(path: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=9",
                    "-pix_fmt", "yuv420p", path], check=True)


def test_frames_reads_silent_video():
    """Через це відео й падав пайплайн: mp3 нема, але зміст на екрані є."""
    if not shutil.which("ffmpeg"):
        pytest.skip("нема ffmpeg")
    with tempfile.TemporaryDirectory() as d:
        video = os.path.join(d, "a.mp4")
        _silent_mp4(video)
        got = instagram.frames([video])
        assert 1 < len(got) <= instagram.FRAMES_PER_VIDEO
        assert all(os.path.getsize(p) > 0 for p in got)


def test_frames_without_any_frame_is_an_error():
    with tempfile.TemporaryDirectory() as d:
        broken = os.path.join(d, "b.mp4")
        open(broken, "wb").write(b"not a video")
        with pytest.raises(RuntimeError):
            instagram.frames([broken])


class _FakeResponse:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_bytes(self): yield b"jpeg-bytes"


def _fake_info(url, out_dir):
    # як його віддає yt-dlp з process=False: entries — генератор, картинки в thumbnails
    return {
        "channel": "di.sukharev", "description": "важнейшая тема",
        "entries": (e for e in [
            {"thumbnails": [{"url": "http://small"}, {"url": "http://big1"}]},
            {"thumbnails": [{"url": "http://small"}, {"url": "http://big2"}]},
        ]),
    }


def test_download_images_takes_every_slide_and_biggest_thumbnail(monkeypatch):
    got = []
    monkeypatch.setattr(instagram, "_image_info", _fake_info)
    monkeypatch.setattr(instagram.httpx, "stream",
                        lambda m, u, **kw: got.append(u) or _FakeResponse())
    paths, meta = instagram.download_images("https://www.instagram.com/p/A/")
    assert got == ["http://big1", "http://big2"]  # останній thumbnail — найбільший
    assert len(paths) == 2 and all(os.path.getsize(p) for p in paths)
    assert meta == {"creator": "@di.sukharev", "source": "IG Post", "caption": "важнейшая тема"}


def test_download_images_without_thumbnails_is_an_error(monkeypatch):
    monkeypatch.setattr(instagram, "_image_info",
                        lambda url, out_dir: {"entries": iter([{"thumbnails": []}])})
    with pytest.raises(RuntimeError, match="жодної картинки"):
        instagram.download_images("https://www.instagram.com/p/A/")


def test_no_audio_carries_videos_and_meta():
    e = NoAudio(["/tmp/x.mp4"], {"creator": "@dude"})
    assert e.videos == ["/tmp/x.mp4"] and e.meta["creator"] == "@dude"


def test_download_audio_reraises_no_audio_not_generic(monkeypatch):
    # обидва заходи кажуть «звуку нема» — нагору має піти NoAudio, а не RuntimeError,
    # інакше бот піде качати картинки замість того, щоб прочитати кадри
    def always_silent(url, use_cookies):
        raise NoAudio(["/tmp/x.mp4"], {})
    monkeypatch.setattr(instagram, "_download", always_silent)
    with pytest.raises(NoAudio):
        instagram.download_audio("https://www.instagram.com/reel/A/")


def test_purge_old_removes_stale_dirs_only(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    old, fresh = tmp_path / "ig_old", tmp_path / "ig_fresh"
    old.mkdir(), fresh.mkdir()
    os.utime(old, (0, 0))
    _purge_old()
    assert not old.exists() and fresh.exists()


if __name__ == "__main__":
    test_source_detection()
    test_meta_prefixes_username()
    test_reel_creator_prefers_channel_over_numeric_id_and_display_name()
    test_numeric_only_falls_back_to_display_name()
    test_meta_missing_uploader()
    test_bare_profile_becomes_stories_url()
    test_reels_posts_stories_untouched()
    test_entries_flattens_playlist_and_single()
    test_meta_takes_uploader_from_first_entry_of_playlist()
    test_stories_creator_comes_from_url_not_numeric_id()
    print("ok")


def test_tiktok_source_and_creator():
    assert source_from_url("https://www.tiktok.com/@nick/video/123") == "TikTok"
    assert source_from_url("https://vm.tiktok.com/ZMabc/") == "TikTok"
    # у TikTok нік — це uploader, а channel — людське ім'я
    meta = _meta("https://www.tiktok.com/@nick/video/123",
                 {"channel": "Nick Display", "uploader": "nick"})
    assert meta == {"creator": "@nick", "source": "TikTok"}
