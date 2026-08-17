import os
from types import SimpleNamespace

os.environ.setdefault("DEEPGRAM_API_KEY", "test")

from transcribe import _transcript_from


def _response(*transcripts):
    alts = [SimpleNamespace(transcript=t) for t in transcripts]
    channels = [SimpleNamespace(alternatives=alts)] if alts else []
    return SimpleNamespace(results=SimpleNamespace(channels=channels))


def test_returns_transcript():
    assert _transcript_from(_response("  привіт світ  ")) == "привіт світ"


def test_no_channels_raises():
    try:
        _transcript_from(_response())
    except RuntimeError as e:
        assert "аудіодоріжки" in str(e)
    else:
        raise AssertionError("мало впасти на файлі без аудіо")


def test_empty_transcript_raises():
    try:
        _transcript_from(_response("   "))
    except RuntimeError as e:
        assert "мовлення" in str(e)
    else:
        raise AssertionError("мало впасти на тиші")


if __name__ == "__main__":
    test_returns_transcript()
    test_no_channels_raises()
    test_empty_transcript_raises()
    print("ok")
