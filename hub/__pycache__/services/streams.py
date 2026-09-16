"""
Turning a video ID into something a <video> tag can actually play.

yt-dlp does the hard part. This module wraps it with a cache (resolved URLs
are valid for a few hours but expensive to fetch), a set of player-client
fallbacks for when one of them is being refused, and a format list so the
player can offer a quality menu.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from urllib.parse import urlparse

from flask import current_app

log = logging.getLogger(__name__)

# Tried in order until one produces a playable URL.
PLAYER_CLIENTS = ["android", "ios", "web_safari", "web", "tv"]

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# Hosts the proxy is willing to forward to. Anything resolved by yt-dlp gets
# added here for the life of the process, which stops the proxy from being
# turned into an open relay for arbitrary URLs.
_allowed_hosts: set[str] = set()
_host_lock = threading.Lock()


class StreamError(RuntimeError):
    """Raised when nothing playable could be resolved."""


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
def _cache_get(key: str) -> dict | None:
    with _lock:
        entry = _cache.get(key)
        if entry and time.time() < entry[0]:
            return entry[1]
        _cache.pop(key, None)
    return None


def _cache_put(key: str, value: dict) -> None:
    ttl = current_app.config.get("STREAM_CACHE_TTL", 300)
    with _lock:
        _cache[key] = (time.time() + ttl, value)
        now = time.time()
        for stale in [k for k, v in _cache.items() if v[0] < now]:
            _cache.pop(stale, None)


def invalidate(video_id: str) -> None:
    with _lock:
        for key in [k for k in _cache if k.startswith(f"{video_id}:")]:
            _cache.pop(key, None)


# --------------------------------------------------------------------------
# Proxy allow-list
# --------------------------------------------------------------------------
def allow_host(url: str) -> None:
    host = urlparse(url).hostname
    if host:
        with _host_lock:
            _allowed_hosts.add(host.lower())


def host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    with _host_lock:
        if host in _allowed_hosts:
            return True
    # Google/YouTube CDN edges rotate constantly, so match the domain suffix.
    return host.endswith(
        (
            ".googlevideo.com",
            ".ytimg.com",
            ".youtube.com",
            ".tubitv.com",
            ".adrise.tv",
            ".akamaized.net",
            ".cloudfront.net",
        )
    )


# --------------------------------------------------------------------------
# yt-dlp
# --------------------------------------------------------------------------
def _base_args(player_client: str) -> list[str]:
    config = current_app.config
    args = [
        config["YTDLP_PATH"],
        "--no-warnings",
        "--no-playlist",
        "--no-check-certificates",
        "--socket-timeout", "15",
        "--extractor-args", f"youtube:player_client={player_client}",
    ]
    ffmpeg = config.get("FFMPEG_PATH")
    if ffmpeg:
        args += ["--ffmpeg-location", ffmpeg]
    cookies = config.get("COOKIES_FILE")
    if cookies and os.path.exists(cookies):
        args += ["--cookies", str(cookies)]
    if config.get("SPONSORBLOCK"):
        args += ["--sponsorblock-mark", "sponsor,selfpromo,interaction"]
    return args


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def _extract_info(url: str, player_client: str) -> dict | None:
    args = _base_args(player_client) + ["--dump-single-json", url]
    try:
        result = _run(args)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("yt-dlp failed to start (%s): %s", player_client, exc)
        return None

    if result.returncode != 0 or not result.stdout.strip():
        log.debug(
            "yt-dlp %s client failed: %s",
            player_client,
            (result.stderr or "").strip()[:400],
        )
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _pick_formats(info: dict) -> dict:
    """
    Pull out a progressive (audio+video in one file) stream for the player,
    plus a quality list. The browser <video> tag can't mux separate tracks,
    so progressive is what we need even though it caps out lower.
    """
    formats = info.get("formats") or []
    progressive, video_only = [], []

    for fmt in formats:
        if fmt.get("protocol") not in (None, "https", "http", "m3u8_native", "m3u8"):
            continue
        url = fmt.get("url")
        if not url:
            continue
        has_video = fmt.get("vcodec") not in (None, "none")
        has_audio = fmt.get("acodec") not in (None, "none")
        entry = {
            "url": url,
            "height": fmt.get("height") or 0,
            "fps": fmt.get("fps"),
            "ext": fmt.get("ext"),
            "format_id": fmt.get("format_id"),
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            "label": (f"{fmt.get('height')}p" if fmt.get("height") else fmt.get("format_note") or "audio"),
        }
        if has_video and has_audio:
            progressive.append(entry)
        elif has_video:
            video_only.append(entry)

    def sort_key(item):
        mp4_first = 0 if item.get("ext") == "mp4" else 1
        return (-(item.get("height") or 0), mp4_first)

    progressive.sort(key=sort_key)

    # Deduplicate the quality menu by height, best format per height.
    qualities, seen_heights = [], set()
    for entry in progressive:
        height = entry["height"]
        if height and height not in seen_heights:
            seen_heights.add(height)
            qualities.append(entry)

    primary = progressive[0] if progressive else (
        {"url": info.get("url")} if info.get("url") else None
    )

    return {
        "url": primary["url"] if primary else None,
        "qualities": qualities,
        "hls": next(
            (f["url"] for f in formats if f.get("protocol", "").startswith("m3u8")), None
        ),
    }


def resolve(video_id: str, quality: str = "auto") -> dict:
    """
    Resolve a YouTube ID to a playable stream.

    Returns {url, qualities, title, duration, thumbnail, subtitles, chapters}.
    Raises StreamError when every player client refuses.
    """
    cache_key = f"{video_id}:{quality}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    url = f"https://www.youtube.com/watch?v={video_id}"
    errors = []

    for client in PLAYER_CLIENTS:
        info = _extract_info(url, client)
        if not info:
            errors.append(client)
            continue

        picked = _pick_formats(info)
        if not picked.get("url"):
            errors.append(f"{client} (no progressive format)")
            continue

        payload = {
            "url": picked["url"],
            "qualities": picked["qualities"],
            "hls": picked["hls"],
            "title": info.get("title"),
            "channel": info.get("uploader") or info.get("channel"),
            "channel_url": info.get("channel_url"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "description": info.get("description") or "",
            "upload_date": info.get("upload_date"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "chapters": info.get("chapters") or [],
            "subtitles": _subtitle_tracks(info),
            "client": client,
        }
        allow_host(payload["url"])
        for entry in payload["qualities"]:
            allow_host(entry["url"])
        _cache_put(cache_key, payload)
        return payload

    raise StreamError(
        "yt-dlp could not resolve this video. Tried: " + ", ".join(errors or PLAYER_CLIENTS)
    )


def _subtitle_tracks(info: dict) -> list[dict]:
    tracks = []
    for source in ("subtitles", "automatic_captions"):
        for lang, entries in (info.get(source) or {}).items():
            best = next(
                (e for e in entries if e.get("ext") == "vtt"),
                entries[0] if entries else None,
            )
            if best and best.get("url"):
                tracks.append(
                    {
                        "lang": lang,
                        "label": lang + (" (auto)" if source != "subtitles" else ""),
                        "url": best["url"],
                        "auto": source != "subtitles",
                    }
                )
                allow_host(best["url"])
    # Prefer real subtitles over machine ones, English first.
    tracks.sort(key=lambda t: (t["auto"], not t["lang"].startswith("en"), t["lang"]))
    return tracks[:20]


def resolve_generic(url: str) -> dict:
    """Resolve any URL yt-dlp supports (used for Tubi and direct links)."""
    info = _extract_info(url, "web")
    if not info:
        raise StreamError(f"yt-dlp could not read {url}")
    picked = _pick_formats(info)
    if not picked.get("url"):
        raise StreamError("No playable format found")
    allow_host(picked["url"])
    return {
        "url": picked["url"],
        "qualities": picked["qualities"],
        "hls": picked["hls"],
        "title": info.get("title"),
        "channel": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "description": info.get("description") or "",
        "subtitles": _subtitle_tracks(info),
        "chapters": info.get("chapters") or [],
    }


def probe_duration(file_path: str) -> float | None:
    """Read a local file's duration with ffprobe."""
    try:
        result = _run(
            [
                current_app.config["FFPROBE_PATH"],
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return None


def probe_video_codec(file_path: str) -> str:
    try:
        result = _run(
            [
                current_app.config["FFPROBE_PATH"],
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "csv=p=0",
                file_path,
            ],
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"
