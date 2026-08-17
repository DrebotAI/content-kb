"""Одноразово, на кожного власника бази:

    python setup_notion.py <сторінка: id або URL> [notion_token | env:VAR]

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

import tenants

SCHEMA = {
    "Name": {"title": {}},
    "Creator": {"select": {}},
    "Source": {"select": {"options": [
        {"name": n} for n in ("IG Reel", "IG Story", "IG Post", "TikTok", "Telegram", "Voice")
    ]}},
    "Link": {"url": {}},
    "Tags": {"multi_select": {}},
    "Value": {"select": {"options": [
        {"name": "🔥 Must-know", "color": "red"},
        {"name": "👍 Корисно", "color": "green"},
        {"name": "📎 Довідково", "color": "gray"},
    ]}},
    # друга шкала, незалежна від Value: банальне може мати сильний кут, і навпаки
    "Content Potential": {"select": {"options": [
        {"name": "🔥 Strong angle", "color": "red"},
        {"name": "👍 Adaptable", "color": "green"},
        {"name": "📎 Weak", "color": "gray"},
    ]}},
    "Content Angle": {"rich_text": {}},
    "Hook": {"rich_text": {}},
    "Recommended Format": {"select": {"options": [{"name": n} for n in (
        "Reel", "talking-head Reel", "screen recording", "carousel", "Telegram post",
        "story sequence", "technical breakdown", "case study", "не для контенту")]}},
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
        sys.exit("Використання: python setup_notion.py <сторінка: id або URL> [notion_token]")
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
