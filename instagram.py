from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import time
from http.cookiejar import MozillaCookieJar

import httpx
import yt_dlp

# голий профіль: instagram.com/нік — без /p/, /reel/, /stories/ тощо
_BARE_PROFILE_RE = re.compile(r"^https?://(?:www\.)?instagram\.com/([^/?#]+)/?(?:[?#].*)?$")
_NOT_PROFILE = {"p", "reel", "reels", "stories", "tv", "explore", "share"}
_STORIES_USER_RE = re.compile(r"/stories/([^/?#]+)")
_STORY_ITEM_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/stories/([^/?#]+)/\d+/?(?:[?#].*)?$")

# скільки кадрів знімати з німого відео і як часто
FRAME_EVERY_SECONDS = 3
FRAMES_PER_VIDEO = 4


class NoAudio(RuntimeError):
    """Відео скачалось, але звукової доріжки в ньому нема — транскрибувати нічого.

    Окремий тип, бо це не збій качання: такий пост читається з кадрів (`frames`),
    а не через `download_images` як пост із картинок.
    """

    def __init__(self, videos: list, meta: dict):
        super().__init__("у відео немає звукової доріжки")
        self.videos, self.meta = videos, meta


class InstagramSessionInvalid(RuntimeError):
    pass


def _instagram_proxy() -> str | None:
    """One proxy boundary for browser/API/CDN/yt-dlp Instagram traffic."""
    value = (os.getenv("IG_PROXY_URL") or "").strip()
    return value or None


def _apply_ydl_proxy(opts: dict, url: str | None = None) -> dict:
    # IG proxy/Android UA are Instagram-specific. Reusing them for TikTok makes
    # otherwise public videos fail TikTok's webpage/API rehydration.
    if url and _is_tiktok(url):
        return opts
    proxy = _instagram_proxy()
    if proxy:
        opts["proxy"] = proxy
    user_agent = (os.getenv("IG_USER_AGENT") or "").strip()
    if user_agent:
        headers = dict(opts.get("http_headers") or {})
        headers["User-Agent"] = user_agent
        opts["http_headers"] = headers
    return opts


def download_audio(url: str) -> tuple[list, dict]:
    """Скачує аудіо з Instagram. Повертає (список mp3, meta: creator/source).

    Для одиничного рела/поста список з одного елемента, для сторіз — усі сторіз чувака.
    """
    # Сторіз без кук не віддаються взагалі, а релам протухлі куки ламають запит
    # (Instagram відповідає 400, хоча анонімно той самий рел качається).
    # Тому пробуємо обидва шляхи, починаючи з того, що ймовірніший для цього типу лінка.
    prefer_cookies = "/stories/" in url
    # TikTok віддається анонімно, а IG-куки для нього все одно нічого не значать
    attempts = (False,) if _is_tiktok(url) else (prefer_cookies, not prefer_cookies)
    errors, silent = [], None
    for use_cookies in attempts:
        try:
            return _download(url, use_cookies)
        except NoAudio as e:
            # другий захід ще може дістати доріжку — але якщо й він не дасть, віддамо кадри
            silent = e
            errors.append(f"{'з куками' if use_cookies else 'без кук'}: {e}")
        except Exception as e:
            errors.append(f"{'з куками' if use_cookies else 'без кук'}: {e}")
    if silent:
        raise silent
    raise RuntimeError(" | ".join(errors))


def frames(videos: list) -> list:
    """Кадри з німого відео — далі йдуть на OCR тим самим шляхом, що й слайди каруселі."""
    out = []
    for i, video in enumerate(videos):
        pattern = os.path.join(os.path.dirname(video), f"frame{i}_%02d.jpg")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", video,
             "-vf", f"fps=1/{FRAME_EVERY_SECONDS}", "-frames:v", str(FRAMES_PER_VIDEO), pattern],
            capture_output=True, timeout=300,
        )
        out += sorted(glob.glob(pattern.replace("%02d", "*")))
    if not out:
        raise RuntimeError("ffmpeg не витяг жодного кадру з німого відео")
    return out


def _purge_old(max_age_seconds: int = 3600) -> None:
    """Кожне качання лишає теку в /tmp; за тиждень це сотні мегабайт відео."""
    cutoff = time.time() - max_age_seconds
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "ig_*")):
        if os.path.isdir(d) and os.path.getmtime(d) < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def story_media_plan(payload: dict, user_id: str) -> list[dict]:
    """Ordered download plan from Instagram's raw reels payload, including photos."""
    reel = (payload.get("reels") or {}).get(str(user_id)) or {}
    plan = []
    for item in reel.get("items") or []:
        item_id = str(item.get("pk") or item.get("id") or len(plan))
        videos = item.get("video_versions") or []
        if videos:
            media = max(videos, key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0))
            plan.append({"id": item_id, "kind": "video", "url": media["url"],
                         "has_audio": bool(item.get("has_audio"))})
            continue
        images = ((item.get("image_versions2") or {}).get("candidates") or [])
        if images:
            media = max(images, key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0))
            plan.append({"id": item_id, "kind": "image", "url": media["url"]})
    return plan


def _story_json(client, path: str, **params) -> dict:
    response = client.get(f"https://www.instagram.com{path}", params=params or None)
    if "/accounts/login" in str(response.url) or response.status_code in (401, 403):
        raise InstagramSessionInvalid(
            "Instagram session invalid — export fresh instagram.com cookies on the new Mac")
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as e:
        raise InstagramSessionInvalid(
            "Instagram session invalid — Instagram returned a non-JSON login/challenge page") from e


def _story_client(cookiefile: str) -> httpx.Client:
    if not cookiefile or not os.path.exists(cookiefile):
        raise InstagramSessionInvalid("Instagram session invalid — cookie file not found")
    jar = MozillaCookieJar(cookiefile)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError) as e:
        raise InstagramSessionInvalid("Instagram session invalid — cookie file is unreadable") from e
    cookies = {cookie.name: cookie.value for cookie in jar}
    if not cookies.get("sessionid"):
        raise InstagramSessionInvalid("Instagram session invalid — sessionid is missing")
    headers = {
        "User-Agent": (os.getenv("IG_USER_AGENT") or "Mozilla/5.0").strip(),
        "X-IG-App-ID": "936619743392459",
        "X-CSRFToken": cookies.get("csrftoken", ""),
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
    }
    kwargs = {"cookies": cookies, "headers": headers, "follow_redirects": True, "timeout": 60}
    proxy = _instagram_proxy()
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def _write_response(client: httpx.Client, url: str, path: str) -> None:
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with open(path, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)


def download_stories(url: str) -> tuple[list[dict], dict]:
    """Download every active story (photos and videos) in Instagram's original order."""
    url = profile_to_stories(url)
    match = _STORIES_USER_RE.search(url)
    if not match or match.group(1) == "highlights":
        raise RuntimeError("це не лінк на активні stories користувача")
    username = match.group(1)
    _purge_old()
    out_dir = tempfile.mkdtemp(prefix="ig_story_")
    with _story_client(os.getenv("IG_COOKIES_FILE", "")) as client:
        profile = _story_json(client, "/api/v1/users/web_profile_info/", username=username)
        user = ((profile.get("data") or {}).get("user") or {})
        user_id = str(user.get("id") or user.get("pk") or "")
        if not user_id:
            raise InstagramSessionInvalid(
                "Instagram session invalid — profile lookup returned no authenticated user data")
        payload = _story_json(client, "/api/v1/feed/reels_media/", reel_ids=user_id)
        plan = story_media_plan(payload, user_id)
        if not plan:
            raise RuntimeError("у користувача немає доступних активних stories")

        items = []
        for index, media in enumerate(plan, 1):
            item_dir = os.path.join(out_dir, f"{index:03d}_{media['id']}")
            os.makedirs(item_dir)
            if media["kind"] == "image":
                path = os.path.join(item_dir, "story.jpg")
                _write_response(client, media["url"], path)
                items.append({"kind": "images", "paths": [path]})
                continue

            video = os.path.join(item_dir, "story.mp4")
            _write_response(client, media["url"], video)
            if media["has_audio"]:
                audio = os.path.join(item_dir, "story.mp3")
                result = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", video, "-vn", audio],
                    capture_output=True, timeout=300,
                )
                if result.returncode == 0 and os.path.exists(audio) and os.path.getsize(audio):
                    items.append({"kind": "audio", "paths": [audio]})
                    continue
            items.append({"kind": "images", "paths": frames([video])})
    return items, {"creator": f"@{username}", "source": "IG Story"}


def _download(url: str, use_cookies: bool) -> tuple[list, dict]:
    _purge_old()
    out_dir = tempfile.mkdtemp(prefix="ig_")
    out_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    ydl_opts = _apply_ydl_proxy({
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # інакше прогрес-бар засмічує journald
        "ignoreerrors": True,  # одна побита сторі не має валити всю пачку
    }, url)
    cookies_file = os.getenv("IG_COOKIES_FILE")
    if use_cookies and cookies_file and os.path.exists(cookies_file):
        # yt-dlp зберігає банку кук назад у cookiefile, а Instagram у відповіді гасить
        # sessionid — після першого ж качання файл лишався без логіна назавжди.
        # Тому віддаємо копію, оригінал не чіпаємо.
        ydl_opts["cookiefile"] = shutil.copy(cookies_file, os.path.join(out_dir, "cookies.txt"))
    elif use_cookies:
        raise RuntimeError("файл кук не знайдено")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("yt-dlp нічого не витяг — перевір лінк і свіжість кук")
        entries = _entries(info)
        paths = [os.path.splitext(ydl.prepare_filename(e))[0] + ".mp3" for e in entries]

    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        # німе відео (текст на екрані, музики нема): формат bestaudio/best падає на
        # video-only, ffmpeg не робить mp3 — але саме відео лежить у теці й читається з кадрів
        videos = sorted(p for p in glob.glob(os.path.join(out_dir, "*.mp4")))
        if videos:
            raise NoAudio(videos, _meta(url, info))
        raise RuntimeError("жоден файл не викачався (сторіз могло не бути, або куки протухли)")
    return paths, _meta(url, info)


def download_images(url: str) -> tuple[list, dict]:
    """Пост без відео: усі слайди каруселі + підпис. Повертає (список картинок, meta).

    Раніше тут був gallery-dl, але його IG-екстрактор лежить: rest кидає на логін,
    graphql віддає 401 — і зі свіжою сесією теж. Тому той самий yt-dlp, тільки в обхід
    вибору формату: process=False віддає сирі entries (по одному на слайд каруселі),
    а ignore_no_formats_error не дає впасти на «There is no video in this post».
    Картинки лежать у thumbnails, останній елемент — найбільший.
    """
    _purge_old()
    out_dir = tempfile.mkdtemp(prefix="ig_img_")
    info = _image_info(url, out_dir)
    # entries із process=False — генератор, а _meta пройдеться по ньому вдруге
    info["entries"] = _entries(info)

    paths = []
    for i, entry in enumerate(info["entries"]):
        thumbs = entry.get("thumbnails") or []
        if not thumbs:
            continue
        path = os.path.join(out_dir, f"slide{i:02d}.jpg")
        stream_kwargs = {"timeout": 60, "follow_redirects": True}
        proxy = _instagram_proxy()
        if proxy:
            stream_kwargs["proxy"] = proxy
        with httpx.stream("GET", thumbs[-1]["url"], **stream_kwargs) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        paths.append(path)
    if not paths:
        raise RuntimeError("yt-dlp не знайшов у пості жодної картинки")

    meta = _meta(url, info)
    meta["caption"] = str(info.get("description")
                          or (info["entries"][0].get("description") if info["entries"] else "")
                          or "")
    return paths, meta


def _image_info(url: str, out_dir: str) -> dict:
    """Сирий info поста. Куки — копією: інакше yt-dlp зітре з оригіналу sessionid."""
    ydl_opts = _apply_ydl_proxy(
        {"quiet": True, "no_warnings": True, "ignore_no_formats_error": True}, url)
    cookies_file = os.getenv("IG_COOKIES_FILE")
    errors = []
    for use_cookies in (True, False):
        opts = dict(ydl_opts)
        if use_cookies:
            if not (cookies_file and os.path.exists(cookies_file)):
                continue
            opts["cookiefile"] = shutil.copy(cookies_file, os.path.join(out_dir, "cookies.txt"))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
            if info:
                return info
            errors.append(f"{'з куками' if use_cookies else 'без кук'}: порожня відповідь")
        except Exception as e:
            errors.append(f"{'з куками' if use_cookies else 'без кук'}: {e}")
    raise RuntimeError(" | ".join(errors) or "нема ні кук, ні анонімного доступу")


def profile_to_stories(url: str) -> str:
    """Голий профіль або одна story означає "усі активні stories цього чувака"."""
    story = _STORY_ITEM_RE.match(url)
    if story and story.group(1) != "highlights":
        return f"https://www.instagram.com/stories/{story.group(1)}/"
    m = _BARE_PROFILE_RE.match(url)
    if m and m.group(1) not in _NOT_PROFILE:
        return f"https://www.instagram.com/stories/{m.group(1)}/"
    return url


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url


def source_from_url(url: str) -> str:
    # ponytail: модуль лишається instagram.py — качання в обох випадках той самий yt-dlp
    if _is_tiktok(url):
        return "TikTok"
    if "/reel" in url:
        return "IG Reel"
    if "/stories/" in url:
        return "IG Story"
    return "IG Post"


def _entries(info: dict) -> list:
    return [e for e in (info.get("entries") or [info]) if e]


def _meta(url: str, info: dict) -> dict:
    # для сторіз yt-dlp віддає числовий uploader_id, а нік лежить прямо в лінку
    from_url = _STORIES_USER_RE.search(url)
    if from_url:
        return {"creator": f"@{from_url.group(1)}", "source": source_from_url(url)}
    first = _entries(info)[0] if info.get("entries") else info
    # IG: channel — це нік, uploader_id числовий, uploader — відображуване ім'я.
    # TikTok навпаки: нік лежить в uploader, а channel — це людське ім'я.
    keys = ("uploader", "channel") if _is_tiktok(url) else ("channel", "uploader_id", "uploader")
    for src in (info, first):
        for key in keys:
            name = str(src.get(key) or "").strip()
            if name and not name.isdigit():
                return {"creator": f"@{name}"[:100], "source": source_from_url(url)}
    return {"creator": "", "source": source_from_url(url)}
