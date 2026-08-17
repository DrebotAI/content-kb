import time

import httpx
from notion_client import Client
from notion_client.errors import HTTPResponseError

RETRIES = 3
RETRY_BACKOFF_SECONDS = 2  # тести ставлять 0

# ponytail: пін старої версії API — у 2025-09-03 схема інша (data_source, initial_data_source).
# Мігрувати, коли Notion почне відмовляти 2022-06-28.
_NOTION_VERSION = "2022-06-28"

# по клієнту на токен: у кожного тенанта свій, а Client тримає httpx-пул —
# створювати його на кожен запис означало б нове зʼєднання щоразу
_clients: dict = {}

_BLOCK_CHAR_LIMIT = 1900  # ліміт Notion rich_text на об'єкт — 2000 символів (перевірено)
# ponytail: 45 чанків ≈ 85k символів ≈ 1.5 години мовлення. Транскрипт лежить двічі —
# у property (щоб фільтр `contains` його знаходив; блоки пошуку не піддаються) і в toggle
# (щоб читалось). Кирилиця — 2 байти, тож удвічі по 45 чанків ще влазить у ліміт запиту 500 КБ.
_MAX_TRANSCRIPT_CHUNKS = 45

# Колонки, без яких запис не створиться. Тип має значення: select замість
# multi_select на Tags — і Notion відкине вже живий пост із 400.
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
    """Обрив мережі чи 5xx/429 — варте повтору. 4xx (крива схема) — ні, повтор не допоможе."""
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
    """URL уже збереженої сторінки з таким лінком, або None. Дублі — в межах
    бази тенанта: те, що кент уже зберіг собі, мене не стосується."""
    result = _retry(
        _client(tenant).request,
        path=f"databases/{tenant.notion_database_id}/query",
        method="POST",
        body={"page_size": 1, "filter": {"property": "Link", "url": {"equals": link}}},
    )
    pages = result.get("results") or []
    return pages[0]["url"] if pages else None


def check_access(tenant) -> list:
    """Список проблем людською мовою; порожній — тенант готовий приймати записи."""
    try:
        db = _retry(_client(tenant).databases.retrieve,
                    database_id=tenant.notion_database_id)
    except HTTPResponseError as exc:
        if exc.status == 401:
            return ["Notion не приймає токен (401) — інтеграцію видалено "
                    "або токен скопійовано не повністю"]
        if exc.status in (403, 404):
            return [f"інтеграція не бачить базу {tenant.notion_database_id} ({exc.status}) — "
                    "відкрий базу → ⋯ → Connections → додай туди інтеграцію"]
        raise
    problems = []
    props = db.get("properties") or {}
    for name, kind in REQUIRED_PROPERTIES.items():
        got = props.get(name)
        if got is None:
            problems.append(f"немає колонки «{name}» ({kind})")
        elif got.get("type") != kind:
            problems.append(f"колонка «{name}»: тип {got.get('type')}, а треба {kind}")
    return problems


def probe(tenant) -> str:
    """Створює тестову сторінку і одразу архівує її — той самий шлях, яким
    піде живий запис. Дешевша перевірка, ніж перший реальний пост о 2 ночі."""
    client = _client(tenant)
    page = _retry(
        client.pages.create,
        parent={"database_id": tenant.notion_database_id},
        properties={"Name": {"title": [{"text": {"content": "✅ Перевірка доступу"}}]}},
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
    # друга шкала й кут — окремими колонками: саме за ними будується вью «що знімати»,
    # а з тіла сторінки їх не відфільтруєш
    if analysis.get("content_potential"):
        props["Content Potential"] = {"select": {"name": analysis["content_potential"]}}
    for column, key in (("Content Angle", "angle"), ("Hook", "hook")):
        if analysis.get(key):
            props[column] = {"rich_text": [{"text": {"content": analysis[key][:_BLOCK_CHAR_LIMIT]}}]}
    if analysis.get("recommended_format"):
        props["Recommended Format"] = {"select": {"name": analysis["recommended_format"]}}
    if transcript:
        # шукабельна копія: databases/query з filter rich_text.contains бачить її цілком
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
        blocks += [_heading("📝 Summary"), _paragraph(analysis["summary"][:_BLOCK_CHAR_LIMIT])]
    if analysis.get("source_idea"):
        blocks += [_heading("🎯 Ідея оригіналу"),
                   _paragraph(analysis["source_idea"][:_BLOCK_CHAR_LIMIT])]
    if analysis["key_ideas"]:
        blocks.append(_heading("💡 Ключові думки"))
        blocks += [_bullet(i) for i in analysis["key_ideas"]]
    if analysis["practical"]:
        blocks.append(_heading("🛠 Практично"))
        blocks += [_bullet(i) for i in analysis["practical"]]
    if analysis.get("learning_takeaway"):
        blocks += [_heading("🧠 Що перевірити або застосувати"),
                   _paragraph(analysis["learning_takeaway"][:_BLOCK_CHAR_LIMIT])]
    if analysis.get("hook"):
        blocks += [_heading("🪝 Хук"), _paragraph(analysis["hook"][:_BLOCK_CHAR_LIMIT])]
    if analysis.get("angle"):
        blocks += [_heading("🎬 Кут для Reels"), _paragraph(analysis["angle"][:_BLOCK_CHAR_LIMIT])]
    if analysis.get("adaptation"):
        blocks.append(_heading("♻️ Як перепакувати"))
        blocks += [_bullet(i) for i in analysis["adaptation"]]
    if analysis.get("own_proof"):
        blocks += [_heading("🧾 Власний доказ"),
                   _paragraph(analysis["own_proof"][:_BLOCK_CHAR_LIMIT])]
    if transcript:
        blocks.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": _rt("📄 Транскрипт"),
                "children": [_paragraph(c) for c in _chunks(transcript)],
            },
        })
    return blocks
