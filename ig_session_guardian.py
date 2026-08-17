#!/usr/bin/env python3
"""Maintain one persistent Instagram browser session and export Netscape cookies."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
PROFILE_DIR = Path(os.getenv(
    "IG_BROWSER_PROFILE", Path.home() / ".cache" / "content-kb" / "ig-browser-profile"))


def _replace_env(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in updates.items() if key not in seen)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=APP_DIR, text=True)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(out).rstrip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_FILE)


def _generate_proxy() -> str:
    key = (os.getenv("GPROXY_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GPROXY_API_KEY missing")
    api = os.getenv("GPROXY_API_URL", "https://gproxy.net/api/v1/proxy/generate/")
    country = os.getenv("GPROXY_COUNTRY", "VN")
    response = httpx.post(
        api,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"countries": [country], "protocol": "http", "sticky": True,
              "lifetime": 60, "count": 1},
        timeout=60,
    )
    response.raise_for_status()
    raw = (response.json().get("proxies") or [None])[0]
    if not raw:
        raise RuntimeError("GProxy returned no proxy")
    proxy = raw if "://" in raw else f"http://{raw}"
    _replace_env({"IG_PROXY_URL": proxy})
    os.environ["IG_PROXY_URL"] = proxy
    return proxy


def _playwright_proxy(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("IG_PROXY_URL is malformed")
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


def _export_netscape(cookies: list[dict], destination: Path) -> None:
    rows = ["# Netscape HTTP Cookie File", "# Exported by ig_session_guardian.py"]
    for cookie in cookies:
        domain = cookie.get("domain") or ".instagram.com"
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = int(cookie.get("expires") or 2147483647)
        if expires <= 0:
            expires = 2147483647
        rows.append("\t".join([
            domain, include_subdomains, cookie.get("path") or "/", secure,
            str(expires), cookie["name"], cookie["value"],
        ]))
    fd, tmp = tempfile.mkstemp(prefix="cookies.", dir=destination.parent, text=True)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(rows) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, destination)


def _authenticated(context) -> bool:
    response = context.request.get(
        "https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
        headers={"X-IG-App-ID": "936619743392459", "X-Requested-With": "XMLHttpRequest"},
        timeout=30_000,
        fail_on_status_code=False,
    )
    if response.status != 200:
        return False
    try:
        payload = response.json()
    except Exception:
        return False
    return bool(payload.get("user")) and payload.get("status") == "ok"


def run(refresh_only: bool = False, rotate_proxy: bool = False) -> int:
    load_dotenv(ENV_FILE, override=True)
    proxy_url = "" if rotate_proxy else (os.getenv("IG_PROXY_URL") or "").strip()
    if not proxy_url:
        proxy_url = _generate_proxy()
    print(json.dumps({"phase": "proxy_ready"}), flush=True)
    username = (os.getenv("IG_USERNAME") or "").strip()
    password = os.getenv("IG_PASSWORD") or ""
    cookie_file = Path(os.getenv("IG_COOKIES_FILE", str(APP_DIR / "cookies.txt")))
    if not cookie_file.is_absolute():
        cookie_file = APP_DIR / cookie_file
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=True, proxy=_playwright_proxy(proxy_url),
            locale="en-US", timezone_id="Asia/Ho_Chi_Minh",
            args=["--disable-blink-features=AutomationControlled"],
        )
        print(json.dumps({"phase": "browser_started"}), flush=True)
        try:
            authenticated = _authenticated(context)
            print(json.dumps({"phase": "initial_auth_checked", "authenticated": authenticated}), flush=True)
            if not authenticated:
                if refresh_only:
                    print(json.dumps({"ok": False, "status": "login_required"}))
                    return 2
                if not username or not password:
                    print(json.dumps({"ok": False, "status": "credentials_missing"}))
                    return 2
                page = context.pages[0] if context.pages else context.new_page()
                print(json.dumps({"phase": "opening_login"}), flush=True)
                page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded",
                          timeout=60_000)
                print(json.dumps({"phase": "login_page_loaded", "url": page.url,
                                  "title": page.title()}), flush=True)
                username_field = page.locator(
                    'input[name="username"], input[autocomplete="username"], '
                    'input[placeholder*="Mobile number"]'
                ).first
                password_field = page.locator('input[name="password"], input[type="password"]').first
                try:
                    username_field.wait_for(state="visible", timeout=8_000)
                except PlaywrightTimeoutError:
                    debug_path = APP_DIR / "ig-login-debug.png"
                    page.screenshot(path=str(debug_path), full_page=True)
                    print(json.dumps({"ok": False, "status": "login_form_missing",
                                      "url": page.url, "title": page.title(),
                                      "screenshot": str(debug_path)}), flush=True)
                    return 5
                username_field.fill(username)
                password_field.fill(password)
                password_field.press("Enter")
                print(json.dumps({"phase": "login_submitted"}), flush=True)
                try:
                    page.wait_for_url(lambda url: "/accounts/login" not in url, timeout=45_000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(4_000)
                print(json.dumps({"phase": "login_wait_finished"}), flush=True)
                if "/challenge/" in page.url or "/two_factor" in page.url:
                    print(json.dumps({"ok": False, "status": "challenge_required"}))
                    return 3
                if not _authenticated(context):
                    debug_path = APP_DIR / "ig-login-result.png"
                    page.screenshot(path=str(debug_path), full_page=True)
                    alerts = page.locator('[role="alert"]').all_inner_texts()
                    print(json.dumps({"ok": False, "status": "login_failed",
                                      "url": page.url, "title": page.title(),
                                      "alerts": alerts[:5], "screenshot": str(debug_path)}), flush=True)
                    return 4
            cookies = context.cookies(["https://www.instagram.com/"])
            if not any(cookie.get("name") == "sessionid" for cookie in cookies):
                print(json.dumps({"ok": False, "status": "sessionid_missing"}))
                return 4
            _export_netscape(cookies, cookie_file)
            print(json.dumps({"ok": True, "status": "authenticated", "cookies_written": True}))
            return 0
        finally:
            context.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-only", action="store_true")
    parser.add_argument("--rotate-proxy", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(refresh_only=args.refresh_only, rotate_proxy=args.rotate_proxy))


if __name__ == "__main__":
    main()
