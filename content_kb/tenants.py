"""Several database owners on one bot.

The source of truth is `tenants.json` in the project root (kept out of git — it holds
tokens). With no such file the config is assembled from `.env` and the bot runs
single-user as before: an old deployment upgrades without touching its config.

Secrets need not be duplicated into a second file:
    "notion_token": "env:SOMEONE_NOTION_TOKEN"
takes the value from an environment variable, leaving `tenants.json` safe to open
while sharing your screen.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# project root, one level above the content_kb package: tenants.json and
# per-tenant context files live next to the repo, not inside the package
_HERE = Path(__file__).resolve().parents[1]
CONFIG_FILE = Path(os.getenv("TENANTS_FILE") or _HERE / "tenants.json")

# a database id is 32 hex at the very end of the path. Take the last hex run and its last
# 32 characters: in a slug like «Knowledge-Base-<id>» the hyphens vanish and the tail of
# the name ("...Bas-e") sticks to the front of the id, so an exactly-32 match with word
# boundaries does not work here.
_HEX_RUN = re.compile(r"[0-9a-fA-F]{32,}")

_REQUIRED = ("name", "telegram_id", "notion_token", "notion_database_id")


class ConfigError(RuntimeError):
    """The config is broken. Fail at startup, not on the first message at 2am."""


@dataclass(frozen=True)
class Tenant:
    name: str
    telegram_id: int
    notion_token: str
    notion_database_id: str
    context_file: str = "context.md"

    @property
    def profile_path(self) -> Path:
        """The owner's profile, used to calibrate value. Everyone has their own — otherwise
        one person's content gets rated against another's deals and everything becomes
        📎 Reference."""
        path = Path(self.context_file).expanduser()
        return path if path.is_absolute() else _HERE / path


def database_id(value: str) -> str:
    """32 hex out of anything: a bare id, a hyphenated id, or a database URL.

    The query string is stripped BEFORE the search: `?v=<32 hex>` holds the *view* id, and
    taking "the last hex chunk" of a full URL would reliably grab that one instead — and
    Notion answers such an id with a 404 that takes an hour to track down.
    """
    head = str(value).strip().split("?", 1)[0].replace("-", "")
    runs = _HEX_RUN.findall(head)
    if not runs:
        raise ConfigError(
            f"cannot see a database id in {value!r} — expected 32 hex characters "
            "or the URL of the database itself")
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
            f"{where}: {field} points at {var}, but there is no such environment variable")
    return got


def _one(raw, where: str) -> Tenant:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected an object {{...}}, not {type(raw).__name__}")
    missing = [k for k in _REQUIRED if not str(raw.get(k, "")).strip()]
    if missing:
        raise ConfigError(f"{where}: not filled in: {', '.join(missing)}")
    try:
        telegram_id = int(str(raw["telegram_id"]).strip())
    except ValueError:
        raise ConfigError(
            f"{where}: telegram_id must be a number, not {raw['telegram_id']!r} "
            "(it is the numeric id, not the @handle)") from None
    return Tenant(
        name=str(raw["name"]).strip(),
        telegram_id=telegram_id,
        notion_token=_secret(raw["notion_token"], "notion_token", where),
        notion_database_id=database_id(_secret(
            raw["notion_database_id"], "notion_database_id", where)),
        context_file=str(raw.get("context_file") or "context.md").strip(),
    )


def parse(items) -> dict:
    """Raw records → {telegram_id: Tenant}. Raises ConfigError on anything malformed."""
    if not isinstance(items, list):
        raise ConfigError("tenants.json must be a list [ {...}, {...} ]")
    if not items:
        raise ConfigError("tenants.json is empty — nobody to write to a base")
    registry: dict = {}
    for i, raw in enumerate(items, 1):
        tenant = _one(raw, f"tenant #{i}")
        twin = registry.get(tenant.telegram_id)
        if twin:
            # a silent overwrite would mean one of the two simply never receives anything,
            # and finding that out would take a trip through the logs
            raise ConfigError(
                f"telegram_id {tenant.telegram_id} is listed twice: "
                f"«{twin.name}» and «{tenant.name}»")
        registry[tenant.telegram_id] = tenant
    return registry


def _from_env() -> dict:
    """The old single-user mode — so a deployment without tenants.json still starts."""
    missing = [v for v in ("ALLOWED_USER_ID", "NOTION_TOKEN", "NOTION_DATABASE_ID")
               if not os.getenv(v)]
    if missing:
        raise ConfigError(
            f"no {CONFIG_FILE.name}, and .env is missing {', '.join(missing)}. "
            f"Either create {CONFIG_FILE.name} (see tenants.example.json), "
            "or add those variables to .env")
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
            raise ConfigError(f"{CONFIG_FILE.name} is not valid JSON: {exc}") from None
        _cache = parse(items)
    else:
        _cache = _from_env()
    return _cache


def get(telegram_id: int):
    """A Tenant, or None. None means a stranger, and the bot stays silent."""
    return load().get(telegram_id)
