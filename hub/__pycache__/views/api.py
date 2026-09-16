"""
JSON endpoints for devices that aren't a browser — the Roku channel, a TV app,
or anything else on the network that wants a feed and a playable URL.

These are deliberately unauthenticated: living-room devices generally can't
carry a session cookie. They're read-only and only expose what's already in
the local catalogue. If this server is reachable from outside your network,
put it behind a reverse proxy with auth.
"""
from __future__ import annotations

import logging
import socket

from flask import Blueprint, current_app, jsonify, redirect, request

from .. import __version__
from ..db import get_db
from ..repo import videos as videos_repo
from ..security import csrf_exempt
from ..services import local_media, streams, tubi

log = logging.getLogger(__name__)
bp = Blueprint("api", __name__, url_prefix="/api")

ROWS = [
    ("Sports", ["sports", "football", "basketball", "nba", "nfl", "soccer",
                "highlights", "ufc", "f1", "boxing"]),
    ("Music", ["music", "song", "remix", "playlist", "lofi", "concert",
               "album", "live set"]),
    ("Tech", ["tech", "technology", "computer", "pc build", "review",
              "hardware", "linux", "gadget"]),
    ("News", ["news", "report", "breaking", "politics", "coverage"]),
    ("Movies", ["movie", "full movie", "feature film", "cinema", "trailer"]),
    ("Comedy", ["funny", "comedy", "parody", "skit", "stand up"]),
    ("Podcasts", ["podcast", "interview", "talk show", "episode", "clips"]),
    ("Gaming", ["game", "gaming", "gameplay", "playthrough", "speedrun",
                "walkthrough", "lets play"]),
    ("How-to", ["diy", "how to", "tutorial", "restoration", "woodworking"]),
    ("Food", ["cooking", "recipe", "chef", "street food", "bake"]),
    ("Travel", ["travel", "exploration", "road trip", "nature"]),
    ("Fitness", ["workout", "gym", "yoga", "meditation", "cardio"]),
    ("Cars", ["car review", "supercar", "motorcycle", "test drive", "racing"]),
]


def _base_url() -> str:
    """The address a TV on the same network should call back to."""
    configured = current_app.config.get("PUBLIC_HOST")
    port = current_app.config.get("PORT", 5002)
    if configured:
        return configured if "://" in configured else f"http://{configured}:{port}"

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        host = probe.getsockname()[0]
        probe.close()
    except OSError:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _feed_item(video: dict, base: str) -> dict:
    video_id = video.get("video_id") or video.get("id")
    thumbnail = video.get("thumbnail") or ""
    if thumbnail.startswith("/"):
        thumbnail = base + thumbnail
    return {
        "id": video_id,
        "title": video.get("title") or "Untitled",
        "url": f"{base}/api/stream/{video_id}",
        "streamformat": "mp4",
        "thumbnail": thumbnail,
        "author": video.get("channel_name") or video.get("channel") or "The Hub",
        "description": (video.get("description") or "")[:400],
        "length": video.get("duration") or 0,
    }


@bp.route("/feed")
@csrf_exempt
def feed():
    """Rows of content for a TV grid. `q` filters everything to a search."""
    query = (request.args.get("q") or "").strip()
    base = _base_url()
    sections: list[dict] = []
    used: set[str] = set()

    # Local files first — they always play, no resolving needed.
    for shelf, items in local_media.by_shelf().items():
        matched = [
            item for item in items
            if not query or query.lower() in item["title"].lower()
        ]
        if matched:
            sections.append(
                {
                    "row_title": shelf,
                    "items": [_feed_item(item, base) for item in matched[:60]],
                }
            )
            used.update(item["id"] for item in matched)

    if query:
        results = videos_repo.search_catalogue(query, limit=80)
        items = [
            _feed_item(video, base)
            for video in results
            if video["video_id"] not in used
        ]
        if items:
            sections.append({"row_title": f"Results for “{query}”", "items": items})
        return jsonify(sections or _empty_feed())

    for title, keywords in ROWS:
        matches = _keyword_rows(keywords, used)
        if len(matches) >= 3:
            sections.append(
                {"row_title": title, "items": [_feed_item(v, base) for v in matches]}
            )
            used.update(v["video_id"] for v in matches)

    recent = [v for v in videos_repo.recent(limit=60) if v["video_id"] not in used]
    if recent:
        sections.append(
            {"row_title": "Recently added", "items": [_feed_item(v, base) for v in recent]}
        )

    return jsonify(sections or _empty_feed())


def _keyword_rows(keywords: list[str], used: set[str], limit: int = 40) -> list[dict]:
    conditions, params = [], []
    for keyword in keywords:
        conditions.append("(title LIKE ? OR channel_name LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    params.append(limit + len(used))

    sql = f"""
        SELECT {videos_repo.VIDEO_FIELDS} FROM videos v
        WHERE {' OR '.join(conditions)}
        ORDER BY RANDOM() LIMIT ?
    """
    rows = get_db().execute(sql, params).fetchall()
    return [dict(row) for row in rows if row["video_id"] not in used][:limit]


def _empty_feed() -> list[dict]:
    return [
        {
            "row_title": "The Hub",
            "items": [
                {
                    "id": "",
                    "title": "Nothing in the library yet — search for something in the web app",
                    "url": "",
                    "streamformat": "mp4",
                    "thumbnail": "",
                    "author": "The Hub",
                    "description": "",
                    "length": 0,
                }
            ],
        }
    ]


@bp.route("/stream/<path:video_id>")
@csrf_exempt
def stream(video_id: str):
    """Redirect a device straight at something playable."""
    if video_id.startswith("local_"):
        return redirect(f"/media/stream/{video_id}", code=302)

    try:
        info = (
            tubi.resolve_stream(video_id)
            if tubi.is_tubi_id(video_id)
            else streams.resolve(video_id)
        )
        return redirect(info["url"], code=302)
    except streams.StreamError as exc:
        log.warning("Device stream resolve failed for %s: %s", video_id, exc)
        return jsonify({"error": str(exc)}), 502


@bp.route("/search")
@csrf_exempt
def search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"results": []})

    base = _base_url()
    results = [
        _feed_item(item, base) for item in local_media.search(query)
    ] + [
        _feed_item(video, base)
        for video in videos_repo.search_catalogue(query, limit=60)
    ]
    return jsonify({"results": results, "count": len(results)})


@bp.route("/health")
@csrf_exempt
def health():
    """For uptime checks and for confirming the box is reachable."""
    return jsonify(
        {
            "status": "ok",
            "version": __version__,
            "catalogue": videos_repo.catalogue_size(),
            "local_files": local_media.stats()["count"],
            "base_url": _base_url(),
        }
    )
