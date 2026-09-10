from __future__ import annotations

import glob
import html as html_lib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_TG_RE = re.compile(
    r"^https?://(?:t\.me|telegram\.me)/"
    r"([A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9])/(\d+)(?:[/?#]|$)"
)


@dataclass
class TelegramPost:
    creator: str
    source: str = "Telegram"
    text: str = ""
    audio_path: str | None = None
    image_paths: list[str] = field(default_factory=list)
    silent_videos: list[str] = field(default_factory=list)
    url: str = ""
    tmp_dir: str | None = None


def is_telegram_channel(url: str) -> bool:
    return bool(_TG_RE.match(url))


def clean_url(url: str) -> str:
    m = _TG_RE.match(url)
    if m:
        return f"https://t.me/{m.group(1)}/{m.group(2)}"
    return url.split("?")[0].split("#")[0].rstrip("/")


def _purge_old(max_age_seconds: int = 3600) -> None:
    cutoff = time.time() - max_age_seconds
    for directory in glob.glob(os.path.join(tempfile.gettempdir(), "tg_*")):
        if os.path.isdir(directory) and os.path.getmtime(directory) < cutoff:
            shutil.rmtree(directory, ignore_errors=True)


def download_post(url: str) -> TelegramPost:
    _purge_old()
    canonical = clean_url(url)
    embed_url = f"{canonical}?embed=1&mode=tme"
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        r = client.get(embed_url)
        r.raise_for_status()
        html = r.text

    creator = ""
    author_m = re.search(
        r'<a class="tgme_widget_message_owner_name[^>]*><span dir="auto">(.*?)</span>',
        html,
        re.DOTALL,
    )
    if author_m:
        creator = html_lib.unescape(author_m.group(1)).strip()

    text = ""
    desc_m = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', html, re.DOTALL)
    if desc_m:
        raw = desc_m.group(1)
        raw = re.sub(r'<br\s*/?>', '\n', raw)
        text = html_lib.unescape(re.sub(r'<[^>]+>', '', raw)).strip()

    post = TelegramPost(creator=creator, text=text, url=canonical)

    video_m = re.search(r'<video[^>]+src="(https://[^"]+)"', html)
    if video_m:
        video_url = html_lib.unescape(video_m.group(1))
        post.tmp_dir = tempfile.mkdtemp(prefix="tg_")

        audio_path = os.path.join(post.tmp_dir, "audio.mp3")
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", video_url, "-vn", audio_path],
            capture_output=True, timeout=120,
        )
        if res.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            post.audio_path = audio_path
        else:
            video_path = os.path.join(post.tmp_dir, "video.mp4")
            with httpx.Client(follow_redirects=True, timeout=60) as dl_client:
                with dl_client.stream("GET", video_url) as vr:
                    vr.raise_for_status()
                    with open(video_path, "wb") as f:
                        for chunk in vr.iter_bytes():
                            f.write(chunk)
            post.silent_videos = [video_path]
    else:
        photos = re.findall(
            r'<a class="tgme_widget_message_photo_image"[^>]+style="background-image:'
            r"url\('([^']+)'\)",
            html,
        )
        if photos:
            post.tmp_dir = tempfile.mkdtemp(prefix="tg_")
            with httpx.Client(follow_redirects=True, timeout=30) as dl_client:
                for i, p_url in enumerate(photos):
                    p_url = html_lib.unescape(p_url)
                    img_path = os.path.join(post.tmp_dir, f"photo_{i:02d}.jpg")
                    response = dl_client.get(p_url)
                    response.raise_for_status()
                    with open(img_path, "wb") as f:
                        f.write(response.content)
                    post.image_paths.append(img_path)

    return post
