"""Одноразово, на кожного власника бази:

    python tools/setup_notion.py <сторінка: id або URL> [notion_token | env:VAR]

Створює в його воркспейсі базу "Knowledge Base" з потрібною схемою і друкує
готовий блок для tenants.json.

Токен другим аргументом — саме тому, що для нового тенанта потрібен ЙОГО токен:
база має лежати в його Notion, а не в моєму.
"""
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from notion_client import Client

from content_kb import tenants
# лейбли й теги живуть в ai_engine — це те саме джерело правди, що формує промпт
# для моделі; дублювати рядки тут означає, що KB_LANGUAGE/KB_TAGS одного дня
# розійдуться зі схемою бази, і select-колонка мовчки перестане приймати значення
from content_kb.ai_engine import FORMATS, POTENTIALS, TAGS, VALUES

SCHEMA = {
    "Name": {"title": {}},
    "Creator": {"select": {}},
    "Source": {"select": {"options": [
        {"name": n} for n in ("IG Reel", "IG Story", "IG Post", "TikTok", "Telegram", "Voice")
    ]}},
    "Link": {"url": {}},
    "Tags": {"multi_select": {"options": [{"name": n} for n in TAGS]}},
    "Value": {"select": {"options": [
        {"name": VALUES[0], "color": "red"},
        {"name": VALUES[1], "color": "green"},
        {"name": VALUES[2], "color": "gray"},
    ]}},
    # друга шкала, незалежна від Value: банальне може мати сильний кут, і навпаки
    "Content Potential": {"select": {"options": [
        {"name": POTENTIALS[0], "color": "red"},
        {"name": POTENTIALS[1], "color": "green"},
        {"name": POTENTIALS[2], "color": "gray"},
    ]}},
    "Content Angle": {"rich_text": {}},
    "Hook": {"rich_text": {}},
    "Recommended Format": {"select": {"options": [{"name": n} for n in FORMATS]}},
    "Why useful": {"rich_text": {}},
    "Transcript": {"rich_text": {}},  # шукабельна копія тіла: пошук по блоках не працює
    "Created": {"created_time": {}},
}


def _token(argv: list) -> str:
    if len(argv) > 2:
        raw = argv[2]
        return os.environ[raw[4:]] if raw.startswith("env:") else raw
    token = os.getenv("NOTION_TOKEN")
    if not token:
        sys.exit("Немає токена: передай другим аргументом або постав NOTION_TOKEN у .env")
    return token


def main() -> None:
    if len(sys.argv) not in (2, 3):
        sys.exit("Використання: python tools/setup_notion.py <сторінка: id або URL> [notion_token]")
    # той самий парсер, що й у конфігу: приймає і голий id, і скопійований URL
    page_id = tenants.database_id(sys.argv[1])
    # ponytail: та сама стара версія API, що й у notion_store
    notion = Client(auth=_token(sys.argv), notion_version="2022-06-28")
    db = notion.databases.create(
        parent={"type": "page_id", "page_id": page_id},
        title=[{"type": "text", "text": {"content": "Knowledge Base"}}],
        properties=SCHEMA,
    )
    print("Готово:", db["url"])
    print("\nБлок для tenants.json (впиши name, telegram_id і свій env: для токена):\n")
    print(json.dumps({
        "name": "kent",
        "telegram_id": 0,
        "notion_token": "env:KENT_NOTION_TOKEN",
        "notion_database_id": tenants.database_id(db["id"]),
        "context_file": "context.kent.md",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
