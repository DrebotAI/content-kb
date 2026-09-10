from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar

import httpx

logger = logging.getLogger(__name__)

_POST_RE = re.compile(
    r"https?://(?:[\w-]+\.)?threads\.(?:net|com)/(?:@[^/?#]+/post/|t/)([\w-]+)", re.I)
_IS_THREADS_RE = re.compile(r"https?://(?:[\w-]+\.)?threads\.(?:net|com)/", re.I)
_CANONICAL_POST_RE = re.compile(r"threads\.(?:net|com)/(@[\w.-]+)/post/([\w-]+)", re.I)
_CANONICAL_T_RE = re.compile(r"threads\.(?:net|com)/t/([\w-]+)", re.I)


@dataclass
class ThreadsPost:
    creator: str
    source: str = "Threads"
    text: str = ""
    audio_path: str | None = None
    image_paths: list[str] = field(default_factory=list)
    silent_videos: list[str] = field(default_factory=list)
    url: str = ""
    tmp_dir: str | None = None


def is_threads(url: str) -> bool:
    """Return True if the URL points to threads.net or threads.com."""
    return bool(_IS_THREADS_RE.match(url))


def clean_url(url: str) -> str:
    """Normalize Threads URL to https://www.threads.net canonical form, stripping tracking query parameters."""
    m = _CANONICAL_POST_RE.search(url)
    if m:
        return f"https://www.threads.net/{m.group(1)}/post/{m.group(2)}"
    t = _CANONICAL_T_RE.search(url)
    if t:
        return f"https://www.threads.net/t/{t.group(1)}"
    return url.split("?")[0].split("#")[0].rstrip("/")


def extract_code(url: str) -> str | None:
    """Extract post shortcode from a Threads URL."""
    m = _POST_RE.search(url)
    return m.group(1) if m else None


def _threads_proxy() -> str | None:
    value = (os.getenv("THREADS_PROXY_URL") or os.getenv("IG_PROXY_URL") or "").strip()
    return value or None


def _threads_headers() -> dict[str, str]:
    # Threads requires a standard desktop browser UA and sec-fetch headers to render full SSR JSON
    user_agent = (os.getenv("THREADS_USER_AGENT") or os.getenv("IG_USER_AGENT") or "").strip()
    if not user_agent or "Android" in user_agent:
        user_agent = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _purge_old(max_age_seconds: int = 3600) -> None:
    cutoff = time.time() - max_age_seconds
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "threads_*")):
        if os.path.isdir(d) and os.path.getmtime(d) < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _find_media_by_code(obj, code: str) -> dict | None:
    """Recursively traverse arbitrary JSON payload to locate post media object for shortcode."""
    if isinstance(obj, dict):
        if "media" in obj and isinstance(obj["media"], dict) and obj["media"].get("code") == code:
            return obj["media"]
        if "post" in obj and isinstance(obj["post"], dict) and obj["post"].get("code") == code:
            return obj["post"]
        if obj.get("code") == code and ("caption" in obj or "video_versions" in obj or "image_versions2" in obj):
            return obj
        for v in obj.values():
            res = _find_media_by_code(v, code)
            if res:
                return res
    elif isinstance(obj, list):
        for v in obj:
            res = _find_media_by_code(v, code)
            if res:
                return res
    return None


def extract_media_from_html(html_text: str, code: str) -> dict | None:
    """Find and parse the embedded application/json script containing the post data."""
    scripts = re.findall(
        r"<script\s+type=[\"\x27]application/json[\"\x27][^>]*>(.*?)</script>",
        html_text, re.DOTALL)
    for s in scripts:
        if code not in s:
            continue
        try:
            data = json.loads(s)
        except Exception:
            continue
        found = _find_media_by_code(data, code)
        if found:
            return found
    return None


def _fetch_via_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            return page.content()
        finally:
            browser.close()


def _fallback_meta_post(html_text: str, url: str) -> ThreadsPost | None:
    import html as html_lib
    og_desc = re.search(
        r"<meta\s+(?:property|name)=[\"\x27](?:og:description|description)[\"\x27]\s+content=[\"\x27]([^\"\x27]*)[\"\x27]",
        html_text, re.I)
    og_title = re.search(
        r"<meta\s+(?:property|name)=[\"\x27]og:title[\"\x27]\s+content=[\"\x27]([^\"\x27]*)[\"\x27]",
        html_text, re.I)
    if not og_desc and not og_title:
        return None
    text = html_lib.unescape(og_desc.group(1)).strip() if og_desc else ""
    title = html_lib.unescape(og_title.group(1)).strip() if og_title else ""
    creator = ""
    author_m = re.search(r"\(@([A-Za-z0-9_.-]+)\)", title)
    if author_m:
        creator = f"@{author_m.group(1)}"
    return ThreadsPost(creator=creator, text=text, url=clean_url(url))


def download_post(url: str) -> ThreadsPost:
    """Download a Threads post, extracting text, audio (if video), images, or frames.

    Returns a ThreadsPost dataclass. Callers are responsible for cleaning up post.tmp_dir.
    """
    _purge_old()
    code = extract_code(url)
    if not code:
        raise RuntimeError(f"Could not extract Threads post shortcode from {url}")

    canonical = clean_url(url)
    client_kwargs = {
        "headers": _threads_headers(),
        "follow_redirects": True,
        "timeout": 30,
    }
    proxy = _threads_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy

    cookies_file = os.getenv("IG_COOKIES_FILE")
    if cookies_file and os.path.exists(cookies_file):
        jar = MozillaCookieJar(cookies_file)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            client_kwargs["cookies"] = {c.name: c.value for c in jar}
        except Exception as e:
            logger.warning("failed to load cookies for Threads: %s", e)

    with httpx.Client(**client_kwargs) as client:
        resp = client.get(canonical)
        resp.raise_for_status()
        html_text = resp.text

    media = extract_media_from_html(html_text, code)
    if not media:
        # Fallback to Playwright if available
        try:
            playwright_html = _fetch_via_playwright(canonical)
            media = extract_media_from_html(playwright_html, code)
            if not media:
                html_text = playwright_html
        except Exception as e:
            logger.debug("Playwright fallback did not resolve Threads post: %s", e)

    if not media:
        meta_post = _fallback_meta_post(html_text, url)
        if meta_post and meta_post.text:
            return meta_post
        raise RuntimeError(f"Could not find Threads post media payload for {code}")

    username = (media.get("user") or {}).get("username", "")
    creator = f"@{username}" if username else ""
    caption = (media.get("caption") or {}).get("text", "").strip()

    tp_info = media.get("text_post_app_info") or {}
    share_info = tp_info.get("share_info") or {}
    quoted = share_info.get("quoted_post")
    if quoted:
        q_user = (quoted.get("user") or {}).get("username", "")
        q_text = (quoted.get("caption") or {}).get("text", "").strip()
        if q_text:
            q_str = f"[Quoting @{q_user}]:\n{q_text}" if q_user else f"[Quoting]:\n{q_text}"
            caption = f"{caption}\n\n{q_str}" if caption else q_str

    tmp_dir = tempfile.mkdtemp(prefix="threads_")
    post = ThreadsPost(creator=creator, text=caption, url=canonical, tmp_dir=tmp_dir)

    video_versions = (media.get("video_versions") or
                      (tp_info.get("linked_inline_media") or {}).get("video_versions") or [])
    has_audio = media.get("has_audio", True)

    if video_versions:
        video_url = video_versions[0]["url"]
        if has_audio:
            audio_path = os.path.join(tmp_dir, "audio.mp3")
            res = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", video_url, "-vn", audio_path],
                capture_output=True, timeout=120,
            )
            if res.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                post.audio_path = audio_path
            else:
                has_audio = False

        if not has_audio:
            video_path = os.path.join(tmp_dir, "video.mp4")
            with httpx.Client(follow_redirects=True, timeout=60) as dl_client:
                with dl_client.stream("GET", video_url) as r:
                    r.raise_for_status()
                    with open(video_path, "wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
            post.silent_videos = [video_path]
    else:
        carousel = media.get("carousel_media") or []
        image_candidates = (media.get("image_versions2") or {}).get("candidates") or []
        with httpx.Client(follow_redirects=True, timeout=30) as dl_client:
            if carousel:
                for i, item in enumerate(carousel):
                    cands = (item.get("image_versions2") or {}).get("candidates") or []
                    if cands:
                        best_img = max(
                            cands, key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0))
                        img_path = os.path.join(tmp_dir, f"slide{i:02d}.jpg")
                        response = dl_client.get(best_img["url"])
                        response.raise_for_status()
                        with open(img_path, "wb") as f:
                            f.write(response.content)
                        post.image_paths.append(img_path)
            elif image_candidates:
                best_img = max(
                    image_candidates, key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0))
                img_path = os.path.join(tmp_dir, "slide00.jpg")
                response = dl_client.get(best_img["url"])
                response.raise_for_status()
                with open(img_path, "wb") as f:
                    f.write(response.content)
                post.image_paths.append(img_path)

    return post
