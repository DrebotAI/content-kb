"""Перевірка тенантів перед тим, як віддавати бота людині:

    python doctor.py                # усі тенанти
    python doctor.py kent           # один
    python doctor.py kent --probe   # ще й створити тестову сторінку і заархівувати

Ловить рівно ті три речі, на яких спотикається кожен новий власник бази:
токен не той, інтеграцію не додали в Connections, у базі бракує колонок.
"""
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

import notion_store
import tenants


def _check_instagram() -> bool:
    """Куки IG тихо протухають — і бот тижнями віддає «не скачав» замість сторіз."""
    path = os.getenv("IG_COOKIES_FILE")
    print("\n[instagram]")
    if not path or not os.path.exists(path):
        print(f"  ⚠️  файлу кук нема ({path or 'IG_COOKIES_FILE не заданий'}) — сторіз не буде")
        return True  # не всім тенантам він потрібен
    jar = {ln.split("\t")[5]: ln.split("\t")[6].strip()
           for ln in open(path, encoding="utf-8") if ln.count("\t") >= 6}
    if "sessionid" not in jar:
        print(f"  ❌ у {path} нема sessionid — залогінься в IG і перевикладай куки")
        return False
    r = httpx.get("https://www.instagram.com/accounts/edit/", cookies=jar,
                  follow_redirects=False, timeout=15,
                  headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"})
    if "/accounts/login/" in r.headers.get("location", ""):
        print("  ❌ сесія протухла — IG кидає на логін")
        return False
    print("  ✅ сесія жива")
    return True


def _check(tenant, probe: bool) -> bool:
    print(f"\n[{tenant.name}]  telegram_id={tenant.telegram_id}")
    print(f"  база    {tenant.notion_database_id}")
    print(f"  профіль {tenant.profile_path.name}"
          f"{'' if tenant.profile_path.exists() else '  ⚠️  файлу немає — оцінка без контексту'}")
    try:
        problems = notion_store.check_access(tenant)
    except Exception as exc:
        print(f"  ❌ Notion недоступний: {exc}")
        return False
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
        return False
    print("  ✅ токен бачить базу, схема на місці")
    if probe:
        try:
            notion_store.probe(tenant)
        except Exception as exc:
            print(f"  ❌ тестовий запис не пройшов: {exc}")
            return False
        print("  ✅ тестова сторінка створена й заархівована")
    return True


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    probe = "--probe" in sys.argv[1:]
    try:
        registry = tenants.load()
    except tenants.ConfigError as exc:
        sys.exit(f"❌ конфіг: {exc}")

    chosen = list(registry.values())
    if args:
        chosen = [t for t in chosen if t.name in args]
        unknown = set(args) - {t.name for t in registry.values()}
        if unknown:
            sys.exit(f"❌ нема таких тенантів: {', '.join(sorted(unknown))}")

    print(f"Тенантів у конфігу: {len(registry)}")
    ok = [_check(t, probe) for t in chosen]
    ok.append(_check_instagram())
    print()
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
