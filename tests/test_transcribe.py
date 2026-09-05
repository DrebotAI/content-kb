import os
from types import SimpleNamespace

os.environ.setdefault("DEEPGRAM_API_KEY", "test")

from content_kb.transcribe import _transcript_from


def _response(*transcripts):
    alts = [SimpleNamespace(transcript=t) for t in transcripts]
    channels = [SimpleNamespace(alternatives=alts)] if alts else []
    return SimpleNamespace(results=SimpleNamespace(channels=channels))


def test_returns_transcript():
    assert _transcript_from(_response("  hello world  ")) == "hello world"


def test_no_channels_raises():
    try:
        _transcript_from(_response())
    except RuntimeError as e:
        assert "no audio track" in str(e)
    else:
        raise AssertionError("should have failed on a file with no audio")


def test_empty_transcript_raises():
    try:
        _transcript_from(_response("   "))
    except RuntimeError as e:
        assert "no recognisable speech" in str(e)
    else:
        raise AssertionError("should have failed on silence")


if __name__ == "__main__":
    test_returns_transcript()
    test_no_channels_raises()
    test_empty_transcript_raises()
    print("ok")
