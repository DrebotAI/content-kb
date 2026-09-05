"""Run once per database owner:

    python tools/setup_notion.py <page: id or URL> [notion_token | env:VAR]

Creates a "Knowledge Base" database with the required schema in that person's
workspace and prints a ready-made block for tenants.json.

The token is the second argument precisely because a new tenant needs THEIR token:
the database has to live in their Notion, not in yours.
"""
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from notion_client import Client

from content_kb import tenants
# labels and tags live in ai_engine — the same source of truth that builds the model's
# prompt; duplicating the strings here would mean KB_LANGUAGE/KB_TAGS drifting away from
# the database schema one day, and a select column silently refusing values
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
    # the second scale, independent of Value: the mundane can carry a strong angle, and vice versa
    "Content Potential": {"select": {"options": [
        {"name": POTENTIALS[0], "color": "red"},
        {"name": POTENTIALS[1], "color": "green"},
        {"name": POTENTIALS[2], "color": "gray"},
    ]}},
    "Content Angle": {"rich_text": {}},
    "Hook": {"rich_text": {}},
    "Recommended Format": {"select": {"options": [{"name": n} for n in FORMATS]}},
    "Why useful": {"rich_text": {}},
    "Transcript": {"rich_text": {}},  # a searchable copy of the body: block search does not work
    "Created": {"created_time": {}},
}


def _token(argv: list) -> str:
    if len(argv) > 2:
        raw = argv[2]
        return os.environ[raw[4:]] if raw.startswith("env:") else raw
    token = os.getenv("NOTION_TOKEN")
    if not token:
        sys.exit("No token: pass it as the second argument or set NOTION_TOKEN in .env")
    return token


def main() -> None:
    if len(sys.argv) not in (2, 3):
        sys.exit("Usage: python tools/setup_notion.py <page: id or URL> [notion_token]")
    # the same parser the config uses: accepts a bare id as well as a copied URL
    page_id = tenants.database_id(sys.argv[1])
    # ponytail: the same old API version notion_store pins
    notion = Client(auth=_token(sys.argv), notion_version="2022-06-28")
    db = notion.databases.create(
        parent={"type": "page_id", "page_id": page_id},
        title=[{"type": "text", "text": {"content": "Knowledge Base"}}],
        properties=SCHEMA,
    )
    print("Done:", db["url"])
    print("\nBlock for tenants.json (fill in name, telegram_id and your own env: for the token):\n")
    print(json.dumps({
        "name": "kent",
        "telegram_id": 0,
        "notion_token": "env:KENT_NOTION_TOKEN",
        "notion_database_id": tenants.database_id(db["id"]),
        "context_file": "context.kent.md",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
