import json
import os
import tempfile
from pathlib import Path

from content_kb import tenants
from content_kb.tenants import ConfigError, Tenant, database_id, parse

OK = {"name": "kent", "telegram_id": 42, "notion_token": "ntn_x",
      "notion_database_id": "0123456789abcdef0123456789abcdef"}


def _fails(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except ConfigError as exc:
        return str(exc)
    raise AssertionError("should have raised ConfigError")


def test_id_from_plain_and_dashed():
    want = "01234567-89ab-cdef-0123-456789abcdef"
    assert database_id("0123456789abcdef0123456789abcdef") == want
    assert database_id("01234567-89ab-cdef-0123-456789abcdef") == want
    assert database_id("  0123456789ABCDEF0123456789ABCDEF ") == want


def test_id_from_url_ignores_view_id():
    """The main trap: ?v=... holds the view id, also 32 hex. Take that one and you get a 404."""
    url = ("https://www.notion.so/workspace/Knowledge-Base-0123456789abcdef0123456789abcdef"
           "?v=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&pvs=4")
    assert database_id(url) == "01234567-89ab-cdef-0123-456789abcdef"


def test_id_survives_hex_letters_in_the_slug():
    """«...Base-<id>» without hyphens fuses into 'e'+id — an exactly-32 match missed here."""
    assert database_id("https://notion.so/Deadbeef-Cafe-0123456789abcdef0123456789abcdef") \
        == "01234567-89ab-cdef-0123-456789abcdef"
    assert database_id("https://www.notion.so/workspace/0123456789abcdef0123456789abcdef/") \
        == "01234567-89ab-cdef-0123-456789abcdef"


def test_id_too_short_is_rejected():
    assert "database id" in _fails(database_id, "0123456789abcdef0123456789abcde")  # 31 hex


def test_id_garbage_is_loud():
    assert "database id" in _fails(database_id, "https://notion.so/my-page")


def test_env_secret_resolved():
    os.environ["KENT_TOKEN_TEST"] = "ntn_secret"
    reg = parse([dict(OK, notion_token="env:KENT_TOKEN_TEST")])
    assert reg[42].notion_token == "ntn_secret"


def test_env_secret_missing_names_the_var():
    os.environ.pop("NOPE_TEST", None)
    assert "NOPE_TEST" in _fails(parse, [dict(OK, notion_token="env:NOPE_TEST")])


def test_duplicate_telegram_id_rejected():
    msg = _fails(parse, [OK, dict(OK, name="owner")])
    assert "twice" in msg  # a silent overwrite = one of the two simply gets nothing


def test_missing_field_names_it():
    assert "notion_token" in _fails(parse, [{k: v for k, v in OK.items() if k != "notion_token"}])


def test_telegram_id_must_be_number():
    assert "must be a number" in _fails(parse, [dict(OK, telegram_id="@kent")])


def test_two_tenants_two_bases():
    other = {"name": "owner", "telegram_id": 7, "notion_token": "ntn_y",
             "notion_database_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
    reg = parse([OK, other])
    assert set(reg) == {42, 7}
    assert reg[42].notion_database_id != reg[7].notion_database_id


def test_context_file_defaults_and_resolves_next_to_code():
    reg = parse([OK, dict(OK, name="s", telegram_id=7, context_file="context.kent.md")])
    assert reg[42].profile_path == tenants._HERE / "context.md"
    assert reg[7].profile_path.name == "context.kent.md"


def test_absolute_context_file_kept():
    t = Tenant("k", 1, "t", "d", "/etc/profiles/kent.md")
    assert str(t.profile_path) == "/etc/profiles/kent.md"


def test_file_config_beats_env(tmp_path=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tenants.json"
        path.write_text(json.dumps([OK], ensure_ascii=False))
        tenants.CONFIG_FILE, tenants._cache = path, None
        try:
            assert tenants.get(42).name == "kent"
            assert tenants.get(999) is None  # a stranger — the bot stays silent
        finally:
            tenants.CONFIG_FILE = tenants._HERE / "tenants.json"
            tenants._cache = None


def test_env_fallback_keeps_old_single_user_deploy_alive():
    missing = Path(tempfile.gettempdir()) / "no-such-tenants.json"
    os.environ.update({"ALLOWED_USER_ID": "111111111", "NOTION_TOKEN": "ntn_env",
                       "NOTION_DATABASE_ID": "0123456789abcdef0123456789abcdef"})
    tenants.CONFIG_FILE, tenants._cache = missing, None
    try:
        owner = tenants.get(111111111)
        assert owner and owner.notion_token == "ntn_env"
    finally:
        tenants.CONFIG_FILE = tenants._HERE / "tenants.json"
        tenants._cache = None


def test_env_fallback_says_what_is_missing():
    for var in ("ALLOWED_USER_ID", "NOTION_TOKEN", "NOTION_DATABASE_ID"):
        os.environ.pop(var, None)
    tenants.CONFIG_FILE = Path(tempfile.gettempdir()) / "no-such-tenants.json"
    tenants._cache = None
    try:
        assert "ALLOWED_USER_ID" in _fails(tenants.load, force=True)
    finally:
        tenants.CONFIG_FILE = tenants._HERE / "tenants.json"
        tenants._cache = None


if __name__ == "__main__":
    for name, fn in sorted(dict(globals()).items()):
        if name.startswith("test_"):
            fn()
    print("ok")
