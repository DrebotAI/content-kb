from types import SimpleNamespace

from content_kb import telegram_channel


def test_public_channel_url_detection_and_cleanup():
    assert telegram_channel.is_telegram_channel("https://t.me/example_channel/123?single")
    assert telegram_channel.is_telegram_channel("https://telegram.me/example_channel/123")
    assert not telegram_channel.is_telegram_channel("https://t.me/c/42/7")
    assert not telegram_channel.is_telegram_channel("https://t.me/share/url?url=https://example.com")
    assert telegram_channel.clean_url(
        "https://telegram.me/example_channel/123?single"
    ) == "https://t.me/example_channel/123"


def test_download_text_post(monkeypatch):
    html = """
    <a class="tgme_widget_message_owner_name"><span dir="auto">Example &amp; Co</span></a>
    <div class="tgme_widget_message_text"><b>Hello</b><br>world &amp; friends</div>
    """

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            assert url == "https://t.me/example_channel/123?embed=1&mode=tme"
            return SimpleNamespace(text=html, raise_for_status=lambda: None)

    monkeypatch.setattr(telegram_channel.httpx, "Client", FakeClient)

    post = telegram_channel.download_post("https://t.me/example_channel/123")

    assert post.creator == "Example & Co"
    assert post.text == "Hello\nworld & friends"
    assert post.url == "https://t.me/example_channel/123"
    assert post.audio_path is None
    assert post.image_paths == []
    assert post.silent_videos == []


def test_download_photo_post(monkeypatch):
    html = (
        '<a class="tgme_widget_message_photo_image" '
        'style="background-image:url(\'https://cdn.example/photo.jpg?x=1&amp;y=2\')">'
    )

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            if "embed=1" in url:
                return SimpleNamespace(text=html, raise_for_status=lambda: None)
            assert url == "https://cdn.example/photo.jpg?x=1&y=2"
            return SimpleNamespace(
                content=b"FAKE_IMAGE", raise_for_status=lambda: None
            )

    monkeypatch.setattr(telegram_channel.httpx, "Client", FakeClient)

    post = telegram_channel.download_post("https://t.me/example_channel/123")
    try:
        assert len(post.image_paths) == 1
        with open(post.image_paths[0], "rb") as image_file:
            assert image_file.read() == b"FAKE_IMAGE"
    finally:
        if post.tmp_dir:
            telegram_channel.shutil.rmtree(post.tmp_dir, ignore_errors=True)
