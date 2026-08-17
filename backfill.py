"""Міграція бази під дві шкали:

    python backfill.py --schema          # додати колонки, які з'явилися (безпечно повторювати)
    python backfill.py                   # заповнити порожні поля в наявних записах
    python backfill.py --limit 5         # спершу на п'яти, подивитись очима

Ідемпотентно: сторінка, де Content Potential уже стоїть, пропускається; заповнені руками
поля не перезаписуються. Кут беремо з тіла сторінки, якщо він там уже є, — те, що модель
колись згенерувала, не викидаємо.
"""
import sys

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

import ai_engine
import notion_store
import setup_notion

# нове в схемі, чого не було в першій версії бази
NEW_COLUMNS = ("Content Potential", "Content Angle", "Hook", "Recommended Format")
_ANGLE_HEADING = "Кут для Reels"


def _pages(tenant) -> list:
    out, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        result = notion_store._retry(
            notion_store._client(tenant).request,
            path=f"databases/{tenant.notion_database_id}/query", method="POST", body=body)
        out += result["results"]
        if not result.get("has_more"):
            return out
        cursor = result["next_cursor"]


def migrate_schema(tenant) -> list:
    """Доливає в базу колонки, яких бракує. Наявні не чіпає.

    Колонки живуть у data source, а не в базі: на запінених 2022-06-28 `databases.update`
    повертає 200 і мовчки не додає нічого — а потім сторінки падають на
    «Content Potential is not a property that exists». Тому саме для схеми — 2025-09-03.
    """
    client = notion_store._client(tenant)
    db = notion_store._retry(client.databases.retrieve, database_id=tenant.notion_database_id)
    missing = {name: setup_notion.SCHEMA[name]
               for name in NEW_COLUMNS if name not in (db.get("properties") or {})}
    if not missing:
        return []
    schema_client = Client(auth=tenant.notion_token, notion_version="2025-09-03")
    sources = schema_client.databases.retrieve(
        database_id=tenant.notion_database_id)["data_sources"]
    notion_store._retry(schema_client.request, path=f"data_sources/{sources[0]['id']}",
                        method="PATCH", body={"properties": missing})
    return list(missing)


def _plain(prop: dict) -> str:
    return "".join(t["plain_text"] for t in (prop or {}).get("rich_text") or [])


def angle_from_body(tenant, page_id: str) -> str:
    """Кут, який стара версія писала лише в тіло сторінки, — під заголовком «Кут для Reels»."""
    blocks = notion_store._retry(
        notion_store._client(tenant).request, path=f"blocks/{page_id}/children", method="GET")
    children = blocks["results"]
    for i, block in enumerate(children):
        heading = (block.get("heading_2") or {}).get("rich_text") or []
        if heading and _ANGLE_HEADING in heading[0]["plain_text"] and i + 1 < len(children):
            text = (children[i + 1].get("paragraph") or {}).get("rich_text") or []
            if text:
                return text[0]["plain_text"].strip()
    return ""


def _rescore(transcript: str, link: str, tenant) -> dict:
    """Тільки контентна половина: value/tags/why useful не чіпаємо, вони вже проставлені."""
    prompt = (
        "Ти оцінюєш, чи можна зі збереженого матеріалу зробити ВЛАСНИЙ контент. Ось власник:\n\n"
        f"{ai_engine.profile(tenant.profile_path)}\n\n"
        + ai_engine._CRITERIA +
        "\n\nНе вигадуй за власника досвід, кейси, результати чи цифри.\n"
        "Відповідь — ЛИШЕ JSON:\n"
        f'{{"content_potential": одне з {list(ai_engine._POTENTIALS)}, '
        '"content_angle": "авторський кут, або порожній рядок", '
        '"hook": "перший рядок Reels/поста українською", '
        f'"recommended_format": одне з {list(ai_engine._FORMATS)}}}\n'
        f"Посилання: {link}\nМатеріал:\n{transcript}"
    )
    return ai_engine._normalize(ai_engine._extract_json(ai_engine._run_codex(prompt)))


def backfill(tenant, limit: int | None = None) -> tuple:
    updated = skipped = failed = 0
    for page in _pages(tenant)[:limit]:
        props = page["properties"]
        if (props.get("Content Potential") or {}).get("select"):
            skipped += 1
            continue
        transcript = _plain(props.get("Transcript"))
        title = _plain(props.get("Name")) or (props["Name"]["title"][0]["plain_text"]
                                              if props["Name"]["title"] else "")
        if not transcript:
            skipped += 1
            print(f"  — {title[:60]}: нема транскрипту, нема з чого оцінювати")
            continue
        try:
            scored = _rescore(transcript, (props.get("Link") or {}).get("url") or "", tenant)
            patch = {"Content Potential": {"select": {"name": scored["content_potential"]}}}
            # кут із тіла сторінки має пріоритет: він уже пройшов через очі власника
            angle = angle_from_body(tenant, page["id"]) or scored["angle"]
            for column, text in (("Content Angle", angle), ("Hook", scored["hook"])):
                if text and not _plain(props.get(column)):
                    patch[column] = {"rich_text": [{"text": {"content": text[:1900]}}]}
            if scored["recommended_format"] and not (props.get("Recommended Format") or {}).get("select"):
                patch["Recommended Format"] = {"select": {"name": scored["recommended_format"]}}
            notion_store._retry(notion_store._client(tenant).pages.update,
                                page_id=page["id"], properties=patch)
            updated += 1
            print(f"  ✅ {title[:60]} → {scored['content_potential']}")
        except Exception as exc:
            failed += 1
            print(f"  ❌ {title[:60]}: {type(exc).__name__}: {exc}")
    return updated, skipped, failed


def main() -> None:
    import tenants
    registry = tenants.load()
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    for tenant in registry.values():
        print(f"\n[{tenant.name}]")
        added = migrate_schema(tenant)
        print(f"  колонки: {'додав ' + ', '.join(added) if added else 'усі вже на місці'}")
        if "--schema" in sys.argv:
            continue
        updated, skipped, failed = backfill(tenant, limit)
        print(f"  updated={updated} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
