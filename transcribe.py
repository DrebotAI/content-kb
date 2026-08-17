import os

from deepgram import DeepgramClient, FileSource, PrerecordedOptions

_client = DeepgramClient(os.environ["DEEPGRAM_API_KEY"])

# ponytail: keyterm prompting (nova-3 only) boosts proper nouns/product names — plain
# `keywords` on nova-2 explicitly does NOT help proper nouns per Deepgram's own docs.
# Extend as new mangled terms show up (e.g. "codecode" -> "Claude Code").
KEYTERMS = [
    "Claude Code", "Codex", "ChatGPT", "OpenAI", "Cursor", "Notion", "Copilot",
    "vibe coding", "vibecoding", "MCP", "GitHub", "no-code",
]


def transcribe_file(path: str) -> str:
    with open(path, "rb") as f:
        payload: FileSource = {"buffer": f.read()}
    options = PrerecordedOptions(
        model="nova-3", smart_format=True, detect_language=True, keyterm=KEYTERMS
    )
    response = _client.listen.rest.v("1").transcribe_file(payload, options)
    return _transcript_from(response)


def _transcript_from(response) -> str:
    """Порожній транскрипт — це помилка, а не результат: інакше в базу піде пустий запис."""
    channels = response.results.channels
    if not channels or not channels[0].alternatives:
        raise RuntimeError("Deepgram не побачив аудіодоріжки у файлі")
    transcript = channels[0].alternatives[0].transcript.strip()
    if not transcript:
        raise RuntimeError("У файлі немає розпізнаваного мовлення")
    return transcript
