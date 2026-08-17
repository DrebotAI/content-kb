"""Кілька власників баз на одному боті.

Джерело правди — `tenants.json` поруч із кодом (у git не їде — там токени).
Файлу немає — конфіг збирається з `.env`, і бот працює як раніше, на одного:
старий деплой оновлюється без жодної правки конфігу.

Секрети можна не дублювати у другому файлі:
    "notion_token": "env:KENT_NOTION_TOKEN"
бере значення зі змінної оточення, а сам `tenants.json` лишається таким,
що його не соромно відкрити при демонстрації екрана.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).parent
CONFIG_FILE = Path(os.getenv("TENANTS_FILE") or _HERE / "tenants.json")

# id бази — 32 hex у самому кінці шляху. Беремо останній hex-«прогін» і з нього
# останні 32: у слагу типу «Knowledge-Base-<id>» дефіси зникають і хвіст назви
# ("...Bas-e") прилипає до id спереду, тож рівно-32 з межами тут не спрацює.
_HEX_RUN = re.compile(r"[0-9a-fA-F]{32,}")

_REQUIRED = ("name", "telegram_id", "notion_token", "notion_database_id")


class ConfigError(RuntimeError):
    """Конфіг кривий. Падаємо на старті, а не на першому повідомленні о 2 ночі."""


@dataclass(frozen=True)
class Tenant:
    name: str
    telegram_id: int
    notion_token: str
    notion_database_id: str
    context_file: str = "context.md"

    @property
    def profile_path(self) -> Path:
        """Профіль власника для оцінки цінності. У кожного свій — інакше кентів
        контент оцінюється під мої деали й усе поспіль стає 📎 Довідково."""
        path = Path(self.context_file).expanduser()
        return path if path.is_absolute() else _HERE / path


def database_id(value: str) -> str:
    """32 hex із чого завгодно: голий id, id з дефісами або URL бази.

    Query-частину відрізаємо ПЕРЕД пошуком: у `?v=<32 hex>` лежить id вʼю, і
    взявши «останній hex-шматок» із повного URL, ми б стабільно чіпляли саме
    його — а Notion на такий id відповідає 404, який шукають годину.
    """
    head = str(value).strip().split("?", 1)[0].replace("-", "")
    runs = _HEX_RUN.findall(head)
    if not runs:
        raise ConfigError(
            f"не бачу id бази у {value!r} — треба 32 hex-символи або URL самої бази")
    raw = runs[-1][-32:].lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _secret(value, field: str, where: str) -> str:
    text = str(value).strip()
    if not text.startswith("env:"):
        return text
    var = text[4:].strip()
    got = os.getenv(var)
    if not got:
        raise ConfigError(
            f"{where}: {field} посилається на {var}, а такої змінної в оточенні немає")
    return got


def _one(raw, where: str) -> Tenant:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: очікую обʼєкт {{...}}, а не {type(raw).__name__}")
    missing = [k for k in _REQUIRED if not str(raw.get(k, "")).strip()]
    if missing:
        raise ConfigError(f"{where}: не заповнено {', '.join(missing)}")
    try:
        telegram_id = int(str(raw["telegram_id"]).strip())
    except ValueError:
        raise ConfigError(
            f"{where}: telegram_id має бути числом, а не {raw['telegram_id']!r} "
            "(це числовий id, не @нік)") from None
    return Tenant(
        name=str(raw["name"]).strip(),
        telegram_id=telegram_id,
        notion_token=_secret(raw["notion_token"], "notion_token", where),
        notion_database_id=database_id(_secret(
            raw["notion_database_id"], "notion_database_id", where)),
        context_file=str(raw.get("context_file") or "context.md").strip(),
    )


def parse(items) -> dict:
    """Список сирих записів → {telegram_id: Tenant}. Кидає ConfigError на будь-якій кривизні."""
    if not isinstance(items, list):
        raise ConfigError("tenants.json має бути списком [ {...}, {...} ]")
    if not items:
        raise ConfigError("tenants.json порожній — нікому писати в базу")
    registry: dict = {}
    for i, raw in enumerate(items, 1):
        tenant = _one(raw, f"тенант #{i}")
        twin = registry.get(tenant.telegram_id)
        if twin:
            # мовчазний перезапис означав би, що один із двох просто ніколи
            # нічого не отримує, і шукати це довелося б по логах
            raise ConfigError(
                f"telegram_id {tenant.telegram_id} вказано двічі: "
                f"«{twin.name}» і «{tenant.name}»")
        registry[tenant.telegram_id] = tenant
    return registry


def _from_env() -> dict:
    """Старий однокористувацький режим — щоб деплой без tenants.json не впав."""
    missing = [v for v in ("ALLOWED_USER_ID", "NOTION_TOKEN", "NOTION_DATABASE_ID")
               if not os.getenv(v)]
    if missing:
        raise ConfigError(
            f"немає {CONFIG_FILE.name}, а в .env бракує {', '.join(missing)}. "
            f"Або створи {CONFIG_FILE.name} (див. tenants.example.json), "
            "або допиши ці змінні в .env")
    return parse([{
        "name": os.getenv("TENANT_NAME", "owner"),
        "telegram_id": os.environ["ALLOWED_USER_ID"],
        "notion_token": os.environ["NOTION_TOKEN"],
        "notion_database_id": os.environ["NOTION_DATABASE_ID"],
        "context_file": os.getenv("CONTEXT_FILE", "context.md"),
    }])


_cache: dict | None = None


def load(force: bool = False) -> dict:
    global _cache
    if _cache is not None and not force:
        return _cache
    if CONFIG_FILE.exists():
        try:
            items = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{CONFIG_FILE.name} — не валідний JSON: {exc}") from None
        _cache = parse(items)
    else:
        _cache = _from_env()
    return _cache


def get(telegram_id: int):
    """Tenant або None. None — чужий, бот мовчить."""
    return load().get(telegram_id)


def by_name(name: str):
    return next((t for t in load().values() if t.name == name), None)
