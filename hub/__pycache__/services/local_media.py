"""
The local media library.

Walks the folders listed in MEDIA_LIBRARY and turns each video file into a
catalogue entry the rest of the app treats like any other video. Results are
cached for a minute so page loads don't re-walk a large drive every time.

Every path handed back to a route is verified to sit inside one of the
configured folders, so a crafted video ID can't be used to read arbitrary
files off the machine.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path

from flask import current_app

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".m2ts",
}
SCAN_TTL = 60
MAX_DEPTH = 4

_cache: dict = {"at": 0.0, "items": []}
_lock = threading.Lock()

# Strips the usual release-name noise so titles read like titles.
_NOISE = re.compile(
    r"\b(1080p|720p|2160p|480p|4k|uhd|hdr|x264|x265|h264|h265|hevc|aac|ac3|"
    r"dts|bluray|brrip|bdrip|webrip|web-dl|hdrip|dvdrip|xvid|remux|proper|"
    r"repack|extended|unrated|yify|rarbg)\b",
    re.IGNORECASE,
)


def _clean_title(stem: str) -> str:
    text = stem.replace("_", " ").replace(".", " ")
    text = _NOISE.sub("", text)
    text = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -–—")
    return text or stem


def _video_id(shelf: str, path: Path) -> str:
    """Stable ID from the full path, so two files can share a title safely."""
    digest = hashlib.sha1(str(path).encode("utf-8", "replace")).hexdigest()[:12]
    shelf_token = re.sub(r"[^a-z0-9]+", "", shelf.lower())[:16] or "media"
    return f"local_{shelf_token}_{digest}"


def _walk(root: Path, depth: int = 0):
    if depth > MAX_DEPTH:
        return
    try:
        entries = list(os.scandir(root))
    except (OSError, PermissionError):
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(Path(entry.path), depth + 1)
            elif entry.is_file(follow_symlinks=False):
                if Path(entry.name).suffix.lower() in VIDEO_EXTENSIONS:
                    yield Path(entry.path), entry.stat()
        except (OSError, PermissionError):
            continue


def shelves() -> dict[str, str]:
    return current_app.config.get("MEDIA_LIBRARY") or {}


def scan(force: bool = False) -> list[dict]:
    """Every video file across every configured folder."""
    with _lock:
        if not force and _cache["items"] and time.time() - _cache["at"] < SCAN_TTL:
            return _cache["items"]

    items: list[dict] = []
    for shelf, folder in shelves().items():
        root = Path(folder).expanduser()
        if not root.is_dir():
            continue
        for path, stat in _walk(root):
            title = _clean_title(path.stem)
            relative = path.relative_to(root)
            items.append(
                {
                    "id": _video_id(shelf, path),
                    "video_id": _video_id(shelf, path),
                    "source": "local",
                    "title": title,
                    "channel": shelf,
                    "channel_name": shelf,
                    "shelf": shelf,
                    "thumbnail": f"/media/thumb/{_video_id(shelf, path)}",
                    "description": f"Local file · {relative}",
                    "file_path": str(path),
                    "file_size": stat.st_size,
                    "modified": stat.st_mtime,
                    "duration": None,
                    "likes": 0,
                    "dislikes": 0,
                    "comment_count": 0,
                }
            )

    items.sort(key=lambda item: item["title"].lower())
    with _lock:
        _cache["items"] = items
        _cache["at"] = time.time()
    return items


def by_shelf() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {shelf: [] for shelf in shelves()}
    for item in scan():
        grouped.setdefault(item["shelf"], []).append(item)
    return grouped


def get(video_id: str) -> dict | None:
    return next((item for item in scan() if item["id"] == video_id), None)


def search(term: str) -> list[dict]:
    term = term.lower().strip()
    if not term:
        return []
    return [item for item in scan() if term in item["title"].lower()]


def safe_path(video_id: str) -> Path | None:
    """
    Resolve a local video ID to a real file, refusing anything that doesn't
    live inside a configured media folder.
    """
    item = get(video_id)
    if not item:
        return None

    path = Path(item["file_path"]).resolve()
    for folder in shelves().values():
        try:
            root = Path(folder).expanduser().resolve()
        except OSError:
            continue
        if path == root or root in path.parents:
            return path if path.is_file() else None
    return None


def invalidate() -> None:
    with _lock:
        _cache["items"] = []
        _cache["at"] = 0.0


def stats() -> dict:
    items = scan()
    return {
        "count": len(items),
        "bytes": sum(item["file_size"] for item in items),
        "shelves": len(shelves()),
    }
