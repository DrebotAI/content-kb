"""Check the tenants before handing the bot to someone:

    python tools/doctor.py                # every tenant
    python tools/doctor.py alice          # just one
    python tools/doctor.py alice --probe  # also create a test page and archive it

Catches exactly the three things every new database owner trips over:
the wrong token, the integration not added under Connections, missing columns.
"""
import os
import sys
from http.cookiejar import MozillaCookieJar

import httpx
from dotenv import load_dotenv

load_dotenv()

from content_kb import notion_store, tenants


def _check_instagram() -> bool:
    """IG cookies go stale quietly — and the bot answers "download failed" for weeks."""
    path = os.getenv("IG_COOKIES_FILE")
    print("\n[instagram]")
    if not path or not os.path.exists(path):
        print(f"  ⚠️  no cookie file ({path or 'IG_COOKIES_FILE is not set'}) — no stories")
        return True  # not every tenant needs one
    cookiejar = MozillaCookieJar(path)
    try:
        cookiejar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError):
        print(f"  ❌ cookie file {path} is not readable")
        return False
    cookies = {c.name: c.value for c in cookiejar}
    if "sessionid" not in cookies:
        print(f"  ❌ no sessionid in {path} — log in to IG and export the cookies again")
        return False
    r = httpx.get("https://www.instagram.com/accounts/edit/", cookies=cookies,
                  follow_redirects=False, timeout=15,
                  headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"})
    if "/accounts/login/" in r.headers.get("location", ""):
        print("  ❌ the session is stale — IG redirects to login")
        return False
    print("  ✅ the session is alive")
    return True


def _check(tenant, probe: bool) -> bool:
    print(f"\n[{tenant.name}]  telegram_id={tenant.telegram_id}")
    print(f"  base     {tenant.notion_database_id}")
    print(f"  profile  {tenant.profile_path.name}"
          f"{'' if tenant.profile_path.exists() else '  ⚠️  file missing — rating without context'}")
    try:
        problems = notion_store.check_access(tenant)
    except Exception as exc:
        print(f"  ❌ Notion is unreachable: {exc}")
        return False
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
        return False
    print("  ✅ the token sees the base, the schema is in place")
    if probe:
        try:
            notion_store.probe(tenant)
        except Exception as exc:
            print(f"  ❌ the test entry did not go through: {exc}")
            return False
        print("  ✅ test page created and archived")
    return True


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    probe = "--probe" in sys.argv[1:]
    try:
        registry = tenants.load()
    except tenants.ConfigError as exc:
        sys.exit(f"❌ config: {exc}")

    chosen = list(registry.values())
    if args:
        chosen = [t for t in chosen if t.name in args]
        unknown = set(args) - {t.name for t in registry.values()}
        if unknown:
            sys.exit(f"❌ no such tenants: {', '.join(sorted(unknown))}")

    print(f"Tenants in the config: {len(registry)}")
    ok = [_check(t, probe) for t in chosen]
    ok.append(_check_instagram())
    print()
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
