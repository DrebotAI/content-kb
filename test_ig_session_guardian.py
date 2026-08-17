import os
from pathlib import Path

import ig_session_guardian as guardian


def test_playwright_proxy_splits_credentials():
    assert guardian._playwright_proxy("http://user:pass@proxy.example:1002") == {
        "server": "http://proxy.example:1002",
        "username": "user",
        "password": "pass",
    }


def test_export_netscape_writes_session_cookie_atomically(tmp_path):
    destination = tmp_path / "cookies.txt"
    guardian._export_netscape([
        {"domain": ".instagram.com", "path": "/", "secure": True,
         "expires": 0, "name": "sessionid", "value": "secret"},
    ], destination)
    text = destination.read_text()
    assert "\tsessionid\tsecret" in text
    assert os.stat(destination).st_mode & 0o777 == 0o600


def test_replace_env_preserves_unrelated_keys(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=yes\nIG_PROXY_URL=old\n")
    monkeypatch.setattr(guardian, "ENV_FILE", env_file)
    monkeypatch.setattr(guardian, "APP_DIR", tmp_path)
    guardian._replace_env({"IG_PROXY_URL": "new"})
    assert env_file.read_text() == "KEEP=yes\nIG_PROXY_URL=new\n"
    assert os.stat(env_file).st_mode & 0o777 == 0o600
