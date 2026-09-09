"""Formatting helpers used throughout the templates."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote

from markupsafe import Markup, escape


def compact_number(value) -> str:
    """1234 -> 1.2K, 5400000 -> 5.4M"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return "0"

    if number < 1000:
        return str(number)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if number >= divisor:
            scaled = number / divisor
            if scaled < 10:
                return f"{scaled:.1f}".rstrip("0").rstrip(".") + suffix
            return f"{int(scaled)}{suffix}"
    return str(number)


def duration(seconds) -> str:
    """Seconds -> 4:21 or 1:02:03."""
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d"):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def time_ago(value) -> str:
    parsed = _parse(value)
    if parsed is None:
        return ""

    delta = datetime.now(timezone.utc) - parsed
    seconds = delta.total_seconds()
    if seconds < 0:
        return "just now"

    for limit, divisor, name in (
        (60, 1, "second"),
        (3600, 60, "minute"),
        (86400, 3600, "hour"),
        (2592000, 86400, "day"),
        (31536000, 2592000, "month"),
        (float("inf"), 31536000, "year"),
    ):
        if seconds < limit:
            count = int(seconds // divisor)
            if count <= 0:
                return "just now"
            return f"{count} {name}{'s' if count != 1 else ''} ago"
    return ""


def file_size(value) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


URL_PATTERN = re.compile(r"(https?://[^\s<>\"]+)")
TIMESTAMP_PATTERN = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")


def linkify(text) -> Markup:
    """
    Turn URLs into links and 1:23 timestamps into seek links, on text that has
    already been escaped. Used for descriptions and comment bodies.
    """
    if not text:
        return Markup("")

    safe = str(escape(text))

    def url_replace(match):
        url = match.group(1)
        trimmed = url.rstrip(".,;:!?)")
        tail = url[len(trimmed):]
        label = trimmed if len(trimmed) <= 60 else trimmed[:57] + "…"
        return (
            f'<a href="{trimmed}" target="_blank" rel="noopener noreferrer nofollow" '
            f'class="ext-link">{label}</a>{tail}'
        )

    safe = URL_PATTERN.sub(url_replace, safe)

    def time_replace(match):
        stamp = match.group(1)
        parts = [int(p) for p in stamp.split(":")]
        total = 0
        for part in parts:
            total = total * 60 + part
        return f'<button type="button" class="seek-link" data-seek="{total}">{stamp}</button>'

    safe = TIMESTAMP_PATTERN.sub(time_replace, safe)
    return Markup(safe.replace("\n", "<br>"))


def avatar_letter(name) -> str:
    text = str(name or "?").strip()
    return text[0].upper() if text else "?"


def hue(name) -> int:
    import zlib

    return zlib.crc32(str(name or "").lower().encode("utf-8")) % 360


def thumb_url(video) -> str:
    """Best available thumbnail, with a graceful placeholder."""
    if isinstance(video, dict):
        url = video.get("thumbnail") or ""
        video_id = video.get("video_id") or video.get("id") or ""
    else:
        url, video_id = "", str(video)

    if url:
        return url
    if video_id and not video_id.startswith(("local_", "tubi_", "upload_", "resp_")):
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return "/static/img/placeholder.svg"


def watch_url(video) -> str:
    video_id = (
        video.get("video_id") or video.get("id") if isinstance(video, dict) else video
    )
    return f"/watch/{quote(str(video_id or ''), safe='')}"


def percent(part, whole) -> float:
    try:
        part, whole = float(part or 0), float(whole or 0)
    except (TypeError, ValueError):
        return 0.0
    if whole <= 0:
        return 0.0
    return max(0.0, min(100.0, part / whole * 100))


def register(app) -> None:
    app.jinja_env.filters.update(
        {
            "compact": compact_number,
            "duration": duration,
            "time_ago": time_ago,
            "file_size": file_size,
            "linkify": linkify,
            "avatar_letter": avatar_letter,
            "hue": hue,
            "thumb": thumb_url,
            "watch_url": watch_url,
        }
    )
    app.jinja_env.globals["percent"] = percent
