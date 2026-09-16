"""
The Roku channel's half of The Hub.

The sideloaded channel calls exactly these paths — they match what the
BrightScript in TheHub_Roku_App expects, so the app keeps working while
gaining an account behind it:

    GET  /api/roku/register?device_uid=…     first contact, returns a pair code
    GET  /api/roku/status?device_uid=…       poll; returns the token once paired
    GET  /api/roku/search?q=…                the row feed  (Authorization: Bearer)
    GET  /api/roku/stream/<video_id>         302 to something playable
    GET  /api/roku/ping                      reachability check

Why the feed is shaped the way it is
------------------------------------
An unpaired device gets the same public rows a logged-out browser would see.
A paired device gets the *same content the person sees on the website*, in the
same order of priority: what they were part way through, what they subscribe
to, what they saved, then the general catalogue. That is the point of pairing
— the TV stops being a separate library.

Every item is normalised before it leaves this module. A ContentNode field on
Roku that receives an empty string renders as blank space with no error, so a
missing title on the server is invisible on the TV and maddening to debug. The
guarantee is made here, once, rather than defended in BrightScript.
"""
from __future__ import annotations

import logging
import re
import socket

from flask import Blueprint, current_app, jsonify, redirect, request

from .. import __version__
from ..repo import devices as devices_repo
from ..repo import library as library_repo
from ..repo import subs as subs_repo
from ..repo import videos as videos_repo
from ..security import csrf_exempt
from ..services import local_media, streams, tubi

log = logging.getLogger(__name__)
bp = Blueprint("roku", __name__, url_prefix="/api/roku")

# Rows are capped so a TV with a few hundred videos in a shelf doesn't spend
# ten seconds parsing JSON before it draws anything.
ITEMS_PER_ROW = 40
MAX_ROWS = 12

# Reused from views/api.py so the TV and any other device agree on categories.
KEYWORD_ROWS = [
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
    ("Gaming", ["game", "gaming", "gameplay", "playthrough", "speedrun"]),
    ("How-to", ["diy", "how to", "tutorial", "restoration", "woodworking"]),
    ("Food", ["cooking", "recipe", "chef", "street food", "bake"]),
]


# --------------------------------------------------------------------------
# Addressing
# --------------------------------------------------------------------------
def _base_url() -> str:
    """
    The address the TV should call back to.

    request.host_url is preferred over a probed IP: the Roku already reached
    this server somehow, and whatever address it used is by definition one
    that works from the TV's position on the network. A guess from a UDP probe
    can pick the wrong interface on a machine with a VPN or a second NIC, and
    the symptom is a grid that loads with thumbnails that never appear.
    """
    configured = current_app.config.get("PUBLIC_HOST")
    port = current_app.config.get("PORT", 5002)
    if configured:
        return (configured if "://" in configured
                else f"http://{configured}:{port}").rstrip("/")

    if request:
        host = request.host_url.rstrip("/")
        if host and "127.0.0.1" not in host and "localhost" not in host:
            return host

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
    except OSError:
        address = "127.0.0.1"
    return f"http://{address}:{port}"


def _bearer() -> str:
    """
    Pull the device token from wherever the channel put it.

    roUrlTransfer can set headers, so Authorization is the normal path; the
    query-string fallback exists because a Video node fetching a stream URL
    cannot attach headers to that request at all.
    """
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.args.get("token") or "").strip()


def _viewer() -> dict | None:
    """The account behind this request, or None for an unpaired device."""
    token = _bearer()
    if not token:
        return None
    device = devices_repo.by_token(token)
    if not device:
        return None

    from ..repo import users as users_repo

    row = users_repo.by_id(device["user_id"])
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Item shaping
# --------------------------------------------------------------------------
_WHITESPACE = re.compile(r"\s+")


def _clean_text(value, fallback: str = "") -> str:
    """
    Collapse to a single line of plain text, or fall back.

    Newlines and tabs inside a title are the quiet killer here: a Label on
    Roku will happily render a title containing a stray newline as a single
    visible line with the rest clipped away, which reads as a truncated title
    rather than as bad data.
    """
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    return text or fallback


def _absolute(url: str, base: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return base + url
    return url


def _item(video: dict, base: str, token: str = "") -> dict:
    """
    One tile, in the shape MainScene.brs maps onto a ContentNode.

    Both `title` and `titleSeason` carry the title, and both `thumbnail` and
    `hdposterurl` carry the image. That duplication is deliberate: it lets the
    BrightScript read whichever key it was written against without the feed
    having to know which generation of the app is calling.
    """
    video_id = str(video.get("video_id") or video.get("id") or "").strip()
    title = _clean_text(video.get("title"), "Untitled")
    author = _clean_text(
        video.get("channel_name") or video.get("channel"), "The Hub"
    )
    thumbnail = _absolute(video.get("thumbnail") or "", base)

    stream = f"{base}/api/roku/stream/{video_id}" if video_id else ""
    if stream and token:
        # The Video node can't send an Authorization header, so a paired
        # device's stream URL carries the token inline instead.
        stream = f"{stream}?token={token}"

    duration = video.get("duration") or 0
    try:
        duration = int(float(duration))
    except (TypeError, ValueError):
        duration = 0

    return {
        "id": video_id,
        "title": title,
        "titleSeason": title,
        "shortDescriptionLine1": title,
        "shortDescriptionLine2": author,
        "author": author,
        "url": stream,
        "streamformat": "mp4",
        "thumbnail": thumbnail,
        "hdposterurl": thumbnail,
        "HDPosterUrl": thumbnail,
        "description": _clean_text(video.get("description"))[:400],
        "length": duration,
    }


def _row(title: str, videos: list[dict], base: str, token: str = "") -> dict | None:
    """A shelf. Returns None rather than an empty row — a row with no items
    still reserves vertical space on the TV and looks like a rendering fault."""
    items = [_item(video, base, token) for video in videos[:ITEMS_PER_ROW]]
    items = [item for item in items if item["url"]]
    if not items:
        return None
    return {"row_title": _clean_text(title, "More"), "items": items}


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------
@bp.route("/register", methods=["GET", "POST"])
@csrf_exempt
def register():
    """First contact from a channel. Returns a pairing code to display."""
    device_uid = (
        request.values.get("device_uid")
        or request.values.get("uid")
        or ""
    ).strip()
    if not device_uid:
        return jsonify({"error": "device_uid is required"}), 400

    try:
        result = devices_repo.begin_pairing(
            device_uid,
            label=request.values.get("label", "Roku"),
            model=request.values.get("model", ""),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    result["pair_url"] = f"{_base_url()}/settings/devices"
    return jsonify(result)


@bp.route("/status")
@csrf_exempt
def status():
    """
    Polled by the channel while it waits. Once the browser has linked the
    code, this returns the bearer token — exactly once.
    """
    device_uid = (request.args.get("device_uid") or "").strip()
    if not device_uid:
        return jsonify({"error": "device_uid is required"}), 400

    device = devices_repo.get_by_uid(device_uid)
    if not device:
        return jsonify({"paired": False, "known": False})

    if not device.get("user_id"):
        return jsonify({
            "paired": False,
            "known": True,
            "pair_code": device.get("pair_code") or "",
        })

    token = devices_repo.collect_token(device_uid)
    payload = {"paired": True, "known": True}
    if token:
        payload["token"] = token

    from ..repo import users as users_repo

    row = users_repo.by_id(device["user_id"])
    if row:
        account = dict(row)
        payload["account"] = account.get("display_name") or account["username"]
    return jsonify(payload)


# --------------------------------------------------------------------------
# The feed
# --------------------------------------------------------------------------
@bp.route("/search")
@csrf_exempt
def search():
    """
    Rows for the TV grid. `q` turns it into a search.

    Returns a bare JSON array — MainScene.brs iterates the top level directly,
    so wrapping it in an object would break the existing channel.
    """
    query = (request.args.get("q") or "").strip()
    base = _base_url()
    token = _bearer()
    viewer = _viewer()

    sections: list[dict] = []
    seen: set[str] = set()

    def add(title: str, videos: list[dict]) -> None:
        fresh = [
            video for video in videos
            if str(video.get("video_id") or video.get("id") or "") not in seen
        ]
        row = _row(title, fresh, base, token)
        if row:
            sections.append(row)
            seen.update(item["id"] for item in row["items"])

    if query:
        for shelf, items in local_media.by_shelf().items():
            matched = [
                item for item in items if query.lower() in item["title"].lower()
            ]
            add(shelf, matched)
        add(f"Results for {query}", videos_repo.search_catalogue(query, limit=80))

        if not sections:
            sections = [_no_results(query)]
        return jsonify(sections[:MAX_ROWS])

    # ---- the personal rows, when a device has been paired ----
    if viewer:
        try:
            add("Continue watching", library_repo.continue_watching(viewer["id"], limit=20))
            add("From your subscriptions", subs_repo.feed_videos(viewer["id"], limit=40))

            watch_later = library_repo.system_playlist(viewer["id"], "watch_later")
            if watch_later:
                add("Watch later", library_repo.playlist_videos(watch_later["id"]))
        except Exception as exc:  # noqa: BLE001
            # A broken personal row should degrade to the public feed, not
            # take the whole TV down with a 500.
            log.warning("Personal rows failed for user %s: %s", viewer["id"], exc)

    # ---- everything else ----
    for shelf, items in local_media.by_shelf().items():
        add(shelf, items)

    add("Recently added", videos_repo.recent(limit=ITEMS_PER_ROW))

    for title, keywords in KEYWORD_ROWS:
        if len(sections) >= MAX_ROWS:
            break
        add(title, _by_keyword(keywords, seen))

    if not sections:
        sections = [_empty_row()]
    return jsonify(sections[:MAX_ROWS])


def _by_keyword(keywords: list[str], seen: set[str]) -> list[dict]:
    from ..db import get_db

    conditions, params = [], []
    for keyword in keywords:
        conditions.append("(title LIKE ? OR channel_name LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    params.append(ITEMS_PER_ROW + len(seen))

    rows = get_db().execute(
        f"SELECT {videos_repo.VIDEO_FIELDS} FROM videos v "
        f"WHERE {' OR '.join(conditions)} ORDER BY RANDOM() LIMIT ?",
        params,
    ).fetchall()
    matched = [dict(row) for row in rows if row["video_id"] not in seen]
    return matched if len(matched) >= 3 else []


def _empty_row() -> dict:
    return {
        "row_title": "The Hub",
        "items": [{
            "id": "",
            "title": "Nothing in the library yet",
            "titleSeason": "Nothing in the library yet",
            "shortDescriptionLine1": "Nothing in the library yet",
            "shortDescriptionLine2": "Search from the web app to fill it",
            "author": "The Hub",
            "url": "",
            "streamformat": "mp4",
            "thumbnail": "",
            "hdposterurl": "",
            "HDPosterUrl": "",
            "description": "",
            "length": 0,
        }],
    }


def _no_results(query: str) -> dict:
    row = _empty_row()
    row["row_title"] = f"No results for {_clean_text(query)}"
    row["items"][0]["title"] = "Nothing matched that search"
    row["items"][0]["titleSeason"] = "Nothing matched that search"
    row["items"][0]["shortDescriptionLine1"] = "Nothing matched that search"
    return row


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------
@bp.route("/stream/<path:video_id>")
@csrf_exempt
def stream(video_id: str):
    """
    Send the Roku player straight at something it can play.

    A redirect rather than a proxy: the bytes go from wherever they live to
    the TV without passing through this machine, which is the difference
    between a laptop that can serve one stream and one that can serve several.
    """
    if video_id.startswith("local_"):
        token = _bearer()
        suffix = f"?token={token}" if token else ""
        return redirect(f"/media/stream/{video_id}{suffix}", code=302)

    try:
        info = (
            tubi.resolve_stream(video_id)
            if tubi.is_tubi_id(video_id)
            else streams.resolve(video_id)
        )
    except streams.StreamError as exc:
        log.warning("Roku stream resolve failed for %s: %s", video_id, exc)
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error resolving %s for Roku: %s", video_id, exc)
        return jsonify({"error": "Could not resolve that video."}), 502

    return redirect(info["url"], code=302)


@bp.route("/ping")
@csrf_exempt
def ping():
    """Reachability and version, for the channel's diagnostics screen."""
    viewer = _viewer()
    return jsonify({
        "status": "ok",
        "version": __version__,
        "base_url": _base_url(),
        "catalogue": videos_repo.catalogue_size(),
        "local_files": local_media.stats()["count"],
        "paired": bool(viewer),
        "account": (viewer.get("display_name") or viewer["username"])
        if viewer else "",
    })
