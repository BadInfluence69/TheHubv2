"""
Finding videos on YouTube.

Two routes to the same result shape:
  1. The Data API v3, if you've set YOUTUBE_API_KEY. Fast, reliable, quota'd.
  2. Scraping the public results page, if you haven't. No key needed, but it
     depends on YouTube's page structure and can break without warning.

Results are cached briefly in memory so paging back and forth doesn't burn
quota or hammer the scraper.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse

import requests
from flask import current_app

from . import preferences

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SEARCH_CACHE_TTL = 300

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str) -> list[dict] | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry[0]:
            return entry[1]
        _cache.pop(key, None)
    return None


def _cache_put(key: str, value: list[dict]) -> None:
    with _cache_lock:
        _cache[key] = (time.time() + SEARCH_CACHE_TTL, value)
        if len(_cache) > 400:
            now = time.time()
            for k in [k for k, v in _cache.items() if v[0] < now]:
                _cache.pop(k, None)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_iso_duration(value: str | None) -> int | None:
    """PT1H2M3S -> seconds."""
    if not value:
        return None
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _parse_clock_duration(value: str | None) -> int | None:
    """'1:02:03' or '4:21' -> seconds."""
    if not value:
        return None
    parts = value.strip().split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    total = 0
    for number in numbers:
        total = total * 60 + number
    return total


def _thumb_for(video_id: str, thumbs: dict | None = None) -> str:
    if thumbs:
        for size in ("maxres", "standard", "high", "medium", "default"):
            url = (thumbs.get(size) or {}).get("url")
            if url:
                return url
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _shape(video_id, title, channel, thumbnail, description,
           duration=None, published_at=None, tags=None) -> dict:
    return {
        "id": video_id,
        "video_id": video_id,
        "source": "youtube",
        "title": title or "Untitled",
        "channel": channel or "Unknown channel",
        "channel_name": channel or "Unknown channel",
        "thumbnail": thumbnail,
        "description": description or "",
        "duration": duration,
        "published_at": published_at,
        "tags": tags or [],
        "likes": 0,
        "dislikes": 0,
        "comment_count": 0,
    }


# --------------------------------------------------------------------------
# Data API
# --------------------------------------------------------------------------
def _search_via_api(query: str, limit: int) -> list[dict]:
    key = current_app.config.get("YOUTUBE_API_KEY")
    if not key:
        return []

    try:
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "maxResults": min(limit, 50),
                "type": "video",
                "key": key,
            },
            timeout=8,
        )
        if response.status_code != 200:
            log.warning("YouTube API search returned %s", response.status_code)
            return []

        items = response.json().get("items", [])
        results, ids = [], []
        for item in items:
            video_id = (item.get("id") or {}).get("videoId")
            if not video_id:
                continue
            snippet = item.get("snippet", {})
            ids.append(video_id)
            results.append(
                _shape(
                    video_id,
                    snippet.get("title"),
                    snippet.get("channelTitle"),
                    _thumb_for(video_id, snippet.get("thumbnails")),
                    snippet.get("description"),
                    published_at=snippet.get("publishedAt"),
                )
            )

        _enrich_with_details(results, ids, key)
        return results
    except requests.RequestException as exc:
        log.warning("YouTube API search failed: %s", exc)
        return []


def _enrich_with_details(results: list[dict], ids: list[str], key: str) -> None:
    """
    A second call fills in the things search results leave out: durations,
    view counts, and the uploader's own tags. videos.list is charged per call
    rather than per part, so asking for snippet as well costs nothing extra
    and gives the recommender its best signal.
    """
    if not ids:
        return
    try:
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(ids[:50]),
                "key": key,
            },
            timeout=8,
        )
        if response.status_code != 200:
            return
        details = {
            item["id"]: item for item in response.json().get("items", []) if item.get("id")
        }
        for result in results:
            item = details.get(result["id"])
            if not item:
                continue
            result["duration"] = _parse_iso_duration(
                (item.get("contentDetails") or {}).get("duration")
            )
            stats = item.get("statistics") or {}
            result["youtube_views"] = int(stats.get("viewCount", 0) or 0)
            result["tags"] = (item.get("snippet") or {}).get("tags", []) or []
    except (requests.RequestException, ValueError):
        pass


def video_details(video_id: str) -> dict | None:
    """Full metadata for a single video, if an API key is configured."""
    key = current_app.config.get("YOUTUBE_API_KEY")
    if not key:
        return None
    try:
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet,contentDetails,statistics", "id": video_id, "key": key},
            timeout=8,
        )
        items = response.json().get("items", []) if response.status_code == 200 else []
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet", {})
        result = _shape(
            video_id,
            snippet.get("title"),
            snippet.get("channelTitle"),
            _thumb_for(video_id, snippet.get("thumbnails")),
            snippet.get("description"),
            _parse_iso_duration((item.get("contentDetails") or {}).get("duration")),
            snippet.get("publishedAt"),
            snippet.get("tags", []),
        )
        stats = item.get("statistics") or {}
        result["youtube_views"] = int(stats.get("viewCount", 0) or 0)
        return result
    except (requests.RequestException, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------
# Scrape fallback
# --------------------------------------------------------------------------
def _walk_for_renderers(node, key: str, out: list):
    """Depth-first hunt for every dict under `key`, wherever YouTube moved it."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict):
                out.append(v)
            else:
                _walk_for_renderers(v, key, out)
    elif isinstance(node, list):
        for item in node:
            _walk_for_renderers(item, key, out)


def _extract_initial_data(html: str) -> dict | None:
    for pattern in (
        r"var ytInitialData\s*=\s*({.*?});</script>",
        r"ytInitialData\"\]\s*=\s*({.*?});",
        r"ytInitialData\s*=\s*({.*?});",
    ):
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
    return None


def _search_via_scrape(query: str, limit: int) -> list[dict]:
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": query, "sp": "EgIQAQ%3D%3D"}  # filter: videos only
    )
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=10,
        )
        if response.status_code != 200:
            log.warning("YouTube results page returned %s", response.status_code)
            return []
    except requests.RequestException as exc:
        log.warning("YouTube scrape failed: %s", exc)
        return []

    data = _extract_initial_data(response.text)
    if not data:
        log.warning("Could not find ytInitialData on the results page")
        return []

    renderers: list[dict] = []
    _walk_for_renderers(data, "videoRenderer", renderers)

    results, seen = [], set()
    for video in renderers:
        video_id = video.get("videoId")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)

        title = _runs_text(video.get("title"))
        channel = _runs_text(video.get("ownerText")) or _runs_text(
            video.get("longBylineText")
        )
        thumbs = (video.get("thumbnail") or {}).get("thumbnails") or []
        thumbnail = thumbs[-1]["url"] if thumbs else _thumb_for(video_id)

        description = ""
        for snippet in video.get("detailedMetadataSnippets") or []:
            description += _runs_text(snippet.get("snippetText"))
        if not description:
            description = _runs_text(video.get("descriptionSnippet"))

        duration = _parse_clock_duration(
            _simple_text(video.get("lengthText"))
        )

        results.append(
            _shape(video_id, title, channel, thumbnail, description, duration)
        )
        if len(results) >= limit:
            break

    return results


def _runs_text(node) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(run.get("text", "") for run in node.get("runs", []))


def _simple_text(node) -> str:
    if not isinstance(node, dict):
        return ""
    return node.get("simpleText") or _runs_text(node)


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------
def search(query: str, limit: int | None = None) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []

    limit = limit or current_app.config.get("SEARCH_RESULTS", 40)
    cache_key = f"search:{query}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return [dict(item) for item in cached]

    results = _search_via_api(query, limit)
    if not results:
        results = _search_via_scrape(query, limit)

    if results:
        _cache_put(cache_key, results)
    return results


def channel_uploads(channel_name: str, limit: int = 24) -> list[dict]:
    """Best-effort recent videos for a channel, matched by name."""
    results = search(channel_name, limit * 2)
    key = channel_name.strip().lower()
    matched = [r for r in results if key in (r["channel"] or "").lower()]
    return (matched or results)[:limit]


def related(video_id: str, title: str, channel: str, limit: int = 20) -> list[dict]:
    """
    Suggestions for the "up next" rail. Uses distinctive words from the title
    plus the channel name, then drops the video you're already watching.
    """
    words = preferences.keywords_from(title)[:4]
    seed = " ".join(words) or channel or "trending"
    results = search(seed, limit + 5)
    return [r for r in results if r["id"] != video_id][:limit]
