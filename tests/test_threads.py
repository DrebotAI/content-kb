import json
import os
from types import SimpleNamespace

from content_kb import threads
from content_kb.threads import (
    _threads_headers,
    _threads_proxy,
    clean_url,
    download_post,
    extract_code,
    extract_media_from_html,
    is_threads,
)


def test_is_threads_recognizes_domains():
    assert is_threads("https://www.threads.net/@creator/post/123")
    assert is_threads("https://threads.net/@creator/post/123")
    assert is_threads("https://www.threads.com/@creator/post/123")
    assert is_threads("https://threads.com/@creator/post/123")
    assert is_threads("http://threads.net/t/123")
    assert not is_threads("https://www.instagram.com/reel/123")
    assert not is_threads("https://www.tiktok.com/@creator/video/123")
    assert not is_threads("https://example.com/threads.net")


def test_clean_url_canonicalizes_and_strips_tracking():
    url_with_tracking = (
        "https://www.threads.com/@creator/post/DdG22JTkkGr"
        "?xmt=AQG034IcRaP2uXziaxgtEJlxzCX5o6NN4PFsGDQ0riAEpNFgks-jKGSXWsYF1zsykia5qSdL&slof=1"
    )
    assert clean_url(url_with_tracking) == "https://www.threads.net/@creator/post/DdG22JTkkGr"

    assert clean_url("https://threads.net/@creator/post/DdG22JTkkGr/") == \
        "https://www.threads.net/@creator/post/DdG22JTkkGr"

    assert clean_url("https://threads.com/t/DdG22JTkkGr?ref=share") == \
        "https://www.threads.net/t/DdG22JTkkGr"


def test_extract_code():
    assert extract_code("https://www.threads.com/@creator/post/DdG22JTkkGr") == "DdG22JTkkGr"
    assert extract_code("https://threads.net/t/AbC123_-z") == "AbC123_-z"
    assert extract_code("https://threads.net/@creator/other") is None


def test_extract_media_from_html_finds_nested_shortcode():
    code = "DdG22JTkkGr"
    payload = {
        "require": [
            ["ScheduledServerJS", "handle", None, [
                {"__bbox": {"result": {"data": {"media": {
                    "code": code,
                    "caption": {"text": "Test caption"},
                    "user": {"username": "author"},
                }}}}}
            ]]
        ]
    }
    html = f"""
    <html>
      <head><title>Threads</title></head>
      <body>
        <script type="application/json">{json.dumps(payload)}</script>
      </body>
    </html>
    """
    media = extract_media_from_html(html, code)
    assert media is not None
    assert media["code"] == code
    assert media["caption"]["text"] == "Test caption"
    assert media["user"]["username"] == "author"


def test_download_post_text_only(monkeypatch):
    code = "TextPost123"
    payload = {
        "media": {
            "code": code,
            "caption": {"text": "A purely text thought on leadership"},
            "user": {"username": "thoughtleader"},
        }
    }
    html = f'<script type="application/json">{json.dumps(payload)}</script>'

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            return SimpleNamespace(text=html, raise_for_status=lambda: None)

    monkeypatch.setattr(threads.httpx, "Client", FakeClient)

    post = download_post(f"https://www.threads.net/@thoughtleader/post/{code}")
    assert post.creator == "@thoughtleader"
    assert post.source == "Threads"
    assert post.text == "A purely text thought on leadership"
    assert post.audio_path is None
    assert post.image_paths == []
    assert post.silent_videos == []
    if post.tmp_dir and os.path.exists(post.tmp_dir):
        threads.shutil.rmtree(post.tmp_dir)


def test_download_post_with_quoted_post(monkeypatch):
    code = "QuotePost123"
    payload = {
        "media": {
            "code": code,
            "caption": {"text": "My reaction to this insight"},
            "user": {"username": "react_user"},
            "text_post_app_info": {
                "share_info": {
                    "quoted_post": {
                        "user": {"username": "orig_user"},
                        "caption": {"text": "Original wisdom"},
                    }
                }
            }
        }
    }
    html = f'<script type="application/json">{json.dumps(payload)}</script>'

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            return SimpleNamespace(text=html, raise_for_status=lambda: None)

    monkeypatch.setattr(threads.httpx, "Client", FakeClient)

    post = download_post(f"https://www.threads.net/@react_user/post/{code}")
    assert post.creator == "@react_user"
    assert "My reaction to this insight" in post.text
    assert "[Quoting @orig_user]:\nOriginal wisdom" in post.text
    if post.tmp_dir and os.path.exists(post.tmp_dir):
        threads.shutil.rmtree(post.tmp_dir)


def test_download_post_video_with_audio(monkeypatch):
    code = "VideoPost123"
    payload = {
        "media": {
            "code": code,
            "caption": {"text": "Watch this"},
            "user": {"username": "videocreator"},
            "has_audio": True,
            "video_versions": [
                {"url": "https://cdn.example.com/video.mp4", "width": 720, "height": 1280}
            ]
        }
    }
    html = f'<script type="application/json">{json.dumps(payload)}</script>'

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            return SimpleNamespace(text=html, raise_for_status=lambda: None)

    def fake_subprocess_run(cmd, *args, **kwargs):
        # cmd has ffmpeg ... audio.mp3
        out_path = cmd[cmd.index("-vn") + 1]
        with open(out_path, "wb") as f:
            f.write(b"FAKE_AUDIO_BYTES")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(threads.httpx, "Client", FakeClient)
    monkeypatch.setattr(threads.subprocess, "run", fake_subprocess_run)

    post = download_post(f"https://www.threads.net/@videocreator/post/{code}")
    assert post.creator == "@videocreator"
    assert post.text == "Watch this"
    assert post.audio_path is not None
    assert os.path.exists(post.audio_path)
    if post.tmp_dir and os.path.exists(post.tmp_dir):
        threads.shutil.rmtree(post.tmp_dir)


def test_download_post_images(monkeypatch):
    code = "ImgPost123"
    payload = {
        "media": {
            "code": code,
            "caption": {"text": "Slides"},
            "user": {"username": "designer"},
            "carousel_media": [
                {"image_versions2": {"candidates": [
                    {"url": "https://cdn.example.com/slide0.jpg", "width": 1080, "height": 1080}
                ]}},
                {"image_versions2": {"candidates": [
                    {"url": "https://cdn.example.com/slide1.jpg", "width": 1080, "height": 1080}
                ]}},
            ]
        }
    }
    html = f'<script type="application/json">{json.dumps(payload)}</script>'

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            if "slide" in url:
                return SimpleNamespace(content=b"FAKE_IMAGE_DATA", raise_for_status=lambda: None)
            return SimpleNamespace(text=html, raise_for_status=lambda: None)

    monkeypatch.setattr(threads.httpx, "Client", FakeClient)

    post = download_post(f"https://www.threads.net/@designer/post/{code}")
    assert post.creator == "@designer"
    assert len(post.image_paths) == 2
    for p in post.image_paths:
        assert os.path.exists(p)
    if post.tmp_dir and os.path.exists(post.tmp_dir):
        threads.shutil.rmtree(post.tmp_dir)


def test_download_post_fallback_meta(monkeypatch):
    code = "FallbackPost123"
    html = """
    <html>
      <head>
        <meta property="og:title" content="Some Author (@some_author) on Threads" />
        <meta property="og:description" content="This is extracted from open graph meta" />
      </head>
      <body><div>No scripts</div></body>
    </html>
    """
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            return SimpleNamespace(text=html, raise_for_status=lambda: None)

    monkeypatch.setattr(threads.httpx, "Client", FakeClient)
    # Ensure playwright fallback doesn't trigger real browser in tests
    monkeypatch.setattr(threads, "_fetch_via_playwright", lambda u: html)

    post = download_post(f"https://www.threads.net/@some_author/post/{code}")
    assert post.creator == "@some_author"
    assert post.text == "This is extracted from open graph meta"


def test_threads_proxy_and_headers(monkeypatch):
    monkeypatch.setenv("THREADS_PROXY_URL", "http://proxy.test:8080")
    assert _threads_proxy() == "http://proxy.test:8080"

    monkeypatch.setenv("THREADS_USER_AGENT", "CustomThreadsUA/1.0")
    headers = _threads_headers()
    assert headers["User-Agent"] == "CustomThreadsUA/1.0"
    assert headers["Sec-Fetch-Dest"] == "document"


def test_download_post_silent_video(monkeypatch):
    code = "SilentVid123"
    payload = {
        "media": {
            "code": code,
            "caption": {"text": "No sound here"},
            "user": {"username": "silentuser"},
            "has_audio": False,
            "video_versions": [
                {"url": "https://cdn.example.com/silent.mp4", "width": 720, "height": 1280}
            ],
        }
    }
    html = f'<script type="application/json">{json.dumps(payload)}</script>'

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            return SimpleNamespace(text=html, raise_for_status=lambda: None)
        def stream(self, method, url):
            class StreamContext:
                def __enter__(self):
                    return SimpleNamespace(
                        raise_for_status=lambda: None,
                        iter_bytes=lambda: [b"FAKE_SILENT_VIDEO_BYTES"],
                    )
                def __exit__(self, *args):
                    pass
            return StreamContext()

    monkeypatch.setattr(threads.httpx, "Client", FakeClient)

    post = download_post(f"https://www.threads.net/@silentuser/post/{code}")
    assert post.creator == "@silentuser"
    assert post.audio_path is None
    assert len(post.silent_videos) == 1
    assert os.path.exists(post.silent_videos[0])
    if post.tmp_dir and os.path.exists(post.tmp_dir):
        threads.shutil.rmtree(post.tmp_dir)


def test_download_post_single_image(monkeypatch):
    code = "SingleImg123"
    payload = {
        "media": {
            "code": code,
            "caption": {"text": "One photo"},
            "user": {"username": "photographer"},
            "image_versions2": {
                "candidates": [
                    {"url": "https://cdn.example.com/small.jpg", "width": 200, "height": 200},
                    {"url": "https://cdn.example.com/large.jpg", "width": 1080, "height": 1080},
                ]
            },
        }
    }
    html = f'<script type="application/json">{json.dumps(payload)}</script>'

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            if "large.jpg" in url:
                return SimpleNamespace(content=b"LARGE_IMG_DATA", raise_for_status=lambda: None)
            return SimpleNamespace(text=html, raise_for_status=lambda: None)

    monkeypatch.setattr(threads.httpx, "Client", FakeClient)

    post = download_post(f"https://www.threads.net/@photographer/post/{code}")
    assert post.creator == "@photographer"
    assert len(post.image_paths) == 1
    assert os.path.exists(post.image_paths[0])
    if post.tmp_dir and os.path.exists(post.tmp_dir):
        threads.shutil.rmtree(post.tmp_dir)
