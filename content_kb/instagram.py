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

# a bare profile: instagram.com/handle — no /p/, /reel/, /stories/ and so on
_BARE_PROFILE_RE = re.compile(r"^https?://(?:www\.)?instagram\.com/([^/?#]+)/?(?:[?#].*)?$")
_NOT_PROFILE = {"p", "reel", "reels", "stories", "tv", "explore", "share"}
_STORIES_USER_RE = re.compile(r"/stories/([^/?#]+)")
_STORY_ITEM_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/stories/([^/?#]+)/\d+/?(?:[?#].*)?$")

# how many frames to grab from a silent video, and how often
FRAME_EVERY_SECONDS = 3
FRAMES_PER_VIDEO = 4


class NoAudio(RuntimeError):
    """The video downloaded, but it has no audio track — there is nothing to transcribe.

    A type of its own, because this is not a download failure: such a post is read from
    its frames (`frames`), not through `download_images` like an image post.
    """

    def __init__(self, videos: list, meta: dict):
        super().__init__("the video has no audio track")
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
    """Downloads audio from Instagram. Returns (list of mp3s, meta: creator/source).

    For a single reel/post that is a one-element list; for stories, all of that user's stories.
    """
    # Stories are not served at all without cookies, while for reels stale cookies break
    # the request (Instagram answers 400, even though the same reel downloads anonymously).
    # So try both paths, starting with whichever is likelier for this kind of link.
    prefer_cookies = "/stories/" in url
    # TikTok is served anonymously, and IG cookies mean nothing to it anyway
    attempts = (False,) if _is_tiktok(url) else (prefer_cookies, not prefer_cookies)
    errors, silent = [], None
    for use_cookies in attempts:
        try:
            return _download(url, use_cookies)
        except NoAudio as e:
            # the second attempt may still find a track — if it does not, we fall back to frames
            silent = e
            errors.append(f"{'with cookies' if use_cookies else 'anonymous'}: {e}")
        except Exception as e:
            errors.append(f"{'with cookies' if use_cookies else 'anonymous'}: {e}")
    if silent:
        raise silent
    raise RuntimeError(" | ".join(errors))


def frames(videos: list) -> list:
    """Frames from a silent video — they go to OCR the same way carousel slides do."""
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
        raise RuntimeError("ffmpeg extracted no frames from the silent video")
    return out


def _purge_old(max_age_seconds: int = 3600) -> None:
    """Every download leaves a directory in /tmp; over a week that is hundreds of MB of video."""
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
        raise RuntimeError("this is not a link to a user's active stories")
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
            raise RuntimeError("the user has no accessible active stories")

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
        "noprogress": True,  # otherwise the progress bar litters journald
        "ignoreerrors": True,  # one broken story must not take down the whole batch
    }, url)
    cookies_file = os.getenv("IG_COOKIES_FILE")
    if use_cookies and cookies_file and os.path.exists(cookies_file):
        # yt-dlp writes the cookie jar back to cookiefile, and Instagram clears sessionid
        # in its response — after the very first download the file was left logged out for
        # good. So hand over a copy and leave the original alone.
        ydl_opts["cookiefile"] = shutil.copy(cookies_file, os.path.join(out_dir, "cookies.txt"))
    elif use_cookies:
        raise RuntimeError("cookie file not found")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("yt-dlp extracted nothing — check the link and whether the cookies are fresh")
        entries = _entries(info)
        paths = [os.path.splitext(ydl.prepare_filename(e))[0] + ".mp3" for e in entries]

    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        # a silent video (text on screen, no music): the bestaudio/best format falls back
        # to video-only and ffmpeg produces no mp3 — but the video itself is in the
        # directory and gets read from its frames
        videos = sorted(p for p in glob.glob(os.path.join(out_dir, "*.mp4")))
        if videos:
            raise NoAudio(videos, _meta(url, info))
        raise RuntimeError("nothing downloaded (there may have been no stories, or the cookies are stale)")
    return paths, _meta(url, info)


def download_images(url: str) -> tuple[list, dict]:
    """A post with no video: every carousel slide plus the caption. Returns (images, meta).

    This used to use gallery-dl, but its IG extractor is dead: rest redirects to login and
    graphql answers 401 — with a fresh session too. So it is the same yt-dlp, just routed
    around format selection: process=False yields raw entries (one per carousel slide), and
    ignore_no_formats_error keeps it from dying on "There is no video in this post".
    The images live in thumbnails, and the last element is the largest.
    """
    _purge_old()
    out_dir = tempfile.mkdtemp(prefix="ig_img_")
    info = _image_info(url, out_dir)
    # entries under process=False is a generator, and _meta walks it a second time
    info["entries"] = _entries(info)

    client_kwargs = {"timeout": 60, "follow_redirects": True}
    proxy = _instagram_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy

    paths = []
    with httpx.Client(**client_kwargs) as client:
        for i, entry in enumerate(info["entries"]):
            thumbs = entry.get("thumbnails") or []
            if not thumbs:
                continue
            path = os.path.join(out_dir, f"slide{i:02d}.jpg")
            _write_response(client, thumbs[-1]["url"], path)
            paths.append(path)
    if not paths:
        raise RuntimeError("yt-dlp found no images in the post")

    meta = _meta(url, info)
    meta["caption"] = str(info.get("description")
                          or (info["entries"][0].get("description") if info["entries"] else "")
                          or "")
    return paths, meta


def _image_info(url: str, out_dir: str) -> dict:
    """The post's raw info. Cookies go in as a copy: otherwise yt-dlp wipes sessionid from the original."""
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
            errors.append(f"{'with cookies' if use_cookies else 'anonymous'}: empty response")
        except Exception as e:
            errors.append(f"{'with cookies' if use_cookies else 'anonymous'}: {e}")
    raise RuntimeError(" | ".join(errors) or "neither cookies nor anonymous access")


def profile_to_stories(url: str) -> str:
    """A bare profile or a single story means "every active story of that user"."""
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
    # ponytail: the module stays instagram.py — either way the download is the same yt-dlp
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
    # for stories yt-dlp returns a numeric uploader_id, while the handle sits right in the link
    from_url = _STORIES_USER_RE.search(url)
    if from_url:
        return {"creator": f"@{from_url.group(1)}", "source": source_from_url(url)}
    first = _entries(info)[0] if info.get("entries") else info
    # IG: channel is the handle, uploader_id is numeric, uploader is the display name.
    # TikTok is the other way round: the handle is in uploader, and channel is the human name.
    keys = ("uploader", "channel") if _is_tiktok(url) else ("channel", "uploader_id", "uploader")
    for src in (info, first):
        for key in keys:
            name = str(src.get(key) or "").strip()
            if name and not name.isdigit():
                return {"creator": f"@{name}"[:100], "source": source_from_url(url)}
    return {"creator": "", "source": source_from_url(url)}
