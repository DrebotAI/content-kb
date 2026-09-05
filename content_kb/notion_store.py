import time

import httpx
from notion_client import Client
from notion_client.errors import HTTPResponseError

from .ai_engine import LABEL_LANG

RETRIES = 3
RETRY_BACKOFF_SECONDS = 2  # the tests set this to 0

# ponytail: pinned to the old API version — the 2025-09-03 one has a different schema
# (data_source, initial_data_source). Migrate when Notion starts refusing 2022-06-28.
_NOTION_VERSION = "2022-06-28"

# one client per token: every tenant has their own, and Client holds an httpx pool —
# building one per entry would mean a fresh connection every time
_clients: dict = {}

_BLOCK_CHAR_LIMIT = 1900  # Notion's rich_text limit per object is 2000 characters (verified)
# ponytail: 45 chunks ≈ 85k characters ≈ 1.5 hours of speech. The transcript is stored twice —
# in a property (so a `contains` filter finds it; blocks are not searchable) and in a toggle
# (so it reads well). Non-ASCII costs 2 bytes, so 45 chunks twice over still fits the 500 KB
# request limit.
_MAX_TRANSCRIPT_CHUNKS = 45

# Page-body headings. These are content, so they follow KB_LANGUAGE like everything else
# the AI writes; unlike the select options they are free text and safe to change later.
_HEADINGS = {
    "en": {
        "summary": "📝 Summary",
        "source_idea": "🎯 Source idea",
        "key_ideas": "💡 Key ideas",
        "practical": "🛠 Practical",
        "learning_takeaway": "🧠 What to check or apply",
        "hook": "🪝 Hook",
        "angle": "🎬 Angle for a Reel",
        "adaptation": "♻️ How to repackage",
        "own_proof": "🧾 Own proof",
        "transcript": "📄 Transcript",
        "probe": "✅ Access check",
    },
    "uk": {
        "summary": "📝 Summary",
        "source_idea": "🎯 Ідея оригіналу",
        "key_ideas": "💡 Ключові думки",
        "practical": "🛠 Практично",
        "learning_takeaway": "🧠 Що перевірити або застосувати",
        "hook": "🪝 Хук",
        "angle": "🎬 Кут для Reels",
        "adaptation": "♻️ Як перепакувати",
        "own_proof": "🧾 Власний доказ",
        "transcript": "📄 Транскрипт",
        "probe": "✅ Перевірка доступу",
    },
}[LABEL_LANG]

# Columns without which an entry cannot be created. The type matters: a select instead of
# a multi_select on Tags and Notion rejects an already-live post with a 400.
REQUIRED_PROPERTIES = {
    "Name": "title",
    "Source": "select",
    "Value": "select",
    "Content Potential": "select",
    "Content Angle": "rich_text",
    "Hook": "rich_text",
    "Recommended Format": "select",
    "Tags": "multi_select",
    "Why useful": "rich_text",
    "Transcript": "rich_text",
    "Link": "url",
    "Creator": "select",
}


def _client(tenant) -> Client:
    client = _clients.get(tenant.notion_token)
    if client is None:
        client = Client(auth=tenant.notion_token, notion_version=_NOTION_VERSION)
        _clients[tenant.notion_token] = client
    return client


def _transient(exc: Exception) -> bool:
    """A dropped connection or 5xx/429 is worth retrying. A 4xx (broken schema) is not."""
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, HTTPResponseError):
        return exc.status >= 500 or exc.status == 429
    return False


def _retry(fn, *args, **kwargs):
    for attempt in range(RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == RETRIES - 1 or not _transient(exc):
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))


def save_entry(tenant, analysis: dict, link: str | None, creator: str, source: str,
               transcript: str) -> str:
    page = _retry(
        _client(tenant).pages.create,
        parent={"database_id": tenant.notion_database_id},
        properties=_build_properties(analysis, link, creator, source, transcript),
        children=_build_blocks(analysis, transcript),
    )
    return page["url"]


def _chunks(text: str) -> list:
    parts = [text[i : i + _BLOCK_CHAR_LIMIT] for i in range(0, len(text), _BLOCK_CHAR_LIMIT)]
    return parts[:_MAX_TRANSCRIPT_CHUNKS]


def find_by_link(tenant, link: str) -> str | None:
    """The URL of an already-saved page with this link, or None. Duplicates are scoped to
    the tenant's own base: what another owner saved for themselves is none of our business."""
    result = _retry(
        _client(tenant).request,
        path=f"databases/{tenant.notion_database_id}/query",
        method="POST",
        body={"page_size": 1, "filter": {"property": "Link", "url": {"equals": link}}},
    )
    pages = result.get("results") or []
    return pages[0]["url"] if pages else None


def check_access(tenant) -> list:
    """Problems in plain language; an empty list means the tenant is ready to take entries."""
    try:
        db = _retry(_client(tenant).databases.retrieve,
                    database_id=tenant.notion_database_id)
    except HTTPResponseError as exc:
        if exc.status == 401:
            return ["Notion rejects the token (401) — the integration was deleted "
                    "or the token was copied incompletely"]
        if exc.status in (403, 404):
            return [f"the integration cannot see database {tenant.notion_database_id} "
                    f"({exc.status}) — open the database → ⋯ → Connections → add the "
                    "integration there"]
        raise
    problems = []
    props = db.get("properties") or {}
    for name, kind in REQUIRED_PROPERTIES.items():
        got = props.get(name)
        if got is None:
            problems.append(f"missing column «{name}» ({kind})")
        elif got.get("type") != kind:
            problems.append(f"column «{name}»: type is {got.get('type')}, should be {kind}")
    return problems


def probe(tenant) -> str:
    """Creates a test page and archives it right away — the same path a live entry takes.
    A cheaper check than finding out on the first real post at 2am."""
    client = _client(tenant)
    page = _retry(
        client.pages.create,
        parent={"database_id": tenant.notion_database_id},
        properties={"Name": {"title": [{"text": {"content": _HEADINGS["probe"]}}]}},
    )
    _retry(client.pages.update, page_id=page["id"], archived=True)
    return page["url"]


def _build_properties(analysis: dict, link: str | None, creator: str, source: str,
                      transcript: str = "") -> dict:
    props = {
        "Name": {"title": [{"text": {"content": analysis["title"][:200]}}]},
        "Source": {"select": {"name": source}},
        "Value": {"select": {"name": analysis["value"]}},
        "Tags": {"multi_select": [{"name": t} for t in analysis["tags"]]},
        "Why useful": {"rich_text": [{"text": {"content": analysis["why_useful"][:_BLOCK_CHAR_LIMIT]}}]},
    }
    # the second scale and the angle get columns of their own: the "what to film" view is
    # built on them, and you cannot filter on the page body
    if analysis.get("content_potential"):
        props["Content Potential"] = {"select": {"name": analysis["content_potential"]}}
    for column, key in (("Content Angle", "angle"), ("Hook", "hook")):
        if analysis.get(key):
            props[column] = {"rich_text": [{"text": {"content": analysis[key][:_BLOCK_CHAR_LIMIT]}}]}
    if analysis.get("recommended_format"):
        props["Recommended Format"] = {"select": {"name": analysis["recommended_format"]}}
    if transcript:
        # a searchable copy: databases/query with a rich_text.contains filter sees all of it
        props["Transcript"] = {"rich_text": [{"text": {"content": c}} for c in _chunks(transcript)]}
    if link:
        props["Link"] = {"url": link}
    if creator:
        props["Creator"] = {"select": {"name": creator[:100]}}
    return props


def _rt(content: str) -> list:
    return [{"type": "text", "text": {"content": content}}]


def _heading(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(text)}}


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rt(text[:_BLOCK_CHAR_LIMIT])},
    }


def _build_blocks(analysis: dict, transcript: str) -> list:
    blocks = [{
        "object": "block",
        "type": "callout",
        "callout": {"rich_text": _rt(analysis["tldr"][:_BLOCK_CHAR_LIMIT]), "icon": {"emoji": "💬"}},
    }]
    if analysis["summary"]:
        blocks += [_heading(_HEADINGS["summary"]),
                   _paragraph(analysis["summary"][:_BLOCK_CHAR_LIMIT])]
    if analysis.get("source_idea"):
        blocks += [_heading(_HEADINGS["source_idea"]),
                   _paragraph(analysis["source_idea"][:_BLOCK_CHAR_LIMIT])]
    if analysis["key_ideas"]:
        blocks.append(_heading(_HEADINGS["key_ideas"]))
        blocks += [_bullet(i) for i in analysis["key_ideas"]]
    if analysis["practical"]:
        blocks.append(_heading(_HEADINGS["practical"]))
        blocks += [_bullet(i) for i in analysis["practical"]]
    if analysis.get("learning_takeaway"):
        blocks += [_heading(_HEADINGS["learning_takeaway"]),
                   _paragraph(analysis["learning_takeaway"][:_BLOCK_CHAR_LIMIT])]
    if analysis.get("hook"):
        blocks += [_heading(_HEADINGS["hook"]), _paragraph(analysis["hook"][:_BLOCK_CHAR_LIMIT])]
    if analysis.get("angle"):
        blocks += [_heading(_HEADINGS["angle"]),
                   _paragraph(analysis["angle"][:_BLOCK_CHAR_LIMIT])]
    if analysis.get("adaptation"):
        blocks.append(_heading(_HEADINGS["adaptation"]))
        blocks += [_bullet(i) for i in analysis["adaptation"]]
    if analysis.get("own_proof"):
        blocks += [_heading(_HEADINGS["own_proof"]),
                   _paragraph(analysis["own_proof"][:_BLOCK_CHAR_LIMIT])]
    if transcript:
        blocks.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": _rt(_HEADINGS["transcript"]),
                "children": [_paragraph(c) for c in _chunks(transcript)],
            },
        })
    return blocks
