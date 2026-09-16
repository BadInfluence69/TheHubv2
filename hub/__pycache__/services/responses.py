"""
Video responses — the old YouTube feature where a reply could be a video
rather than a paragraph.

A response is an ordinary row in `videos` with a `resp_` id and `source` set
to 'response', plus a comment row pointing at it. That means everything the
app already knows how to do with a video works on a response for free: it has
a watch page, it can be liked, it can be put in a playlist, it turns up in the
catalogue.

Two ways to attach one:

  · upload a file, which lands in UPLOAD_DIR/responses/<user id>/ and is
    served straight off the disk by the media blueprint
  · point at something already here — another Hub video, a local file, a
    YouTube id — in which case nothing is copied and the comment simply
    references the existing row
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
import uuid
from pathlib import Path

from flask import current_app
from werkzeug.security import safe_join
from werkzeug.utils import secure_filename

from ..repo import videos as videos_repo
from . import streams

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".wmv", ".flv",
}

# 11-character YouTube ids, and the handful of URL shapes they hide inside.
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_URL = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
HUB_WATCH_URL = re.compile(r"/watch/([^/?#]+)")


def responses_dir(user_id: int) -> Path:
    path = Path(current_app.config["UPLOAD_DIR"]) / "responses" / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(video_id: str) -> Path | None:
    """
    Resolve a response id to a file on disk.

    The stored path is checked against the responses root before it is opened,
    so a tampered database row can't be used to read something else.
    """
    if not video_id.startswith("resp_"):
        return None

    record = videos_repo.get(video_id)
    if not record or not record.get("file_path"):
        return None

    candidate = Path(record["file_path"])
    root = (Path(current_app.config["UPLOAD_DIR"]) / "responses").resolve()
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        log.warning("Response %s points outside the responses directory", video_id)
        return None

    return resolved if resolved.is_file() else None


# --------------------------------------------------------------------------
# Uploading
# --------------------------------------------------------------------------
def save_upload(user: dict, file_storage, title: str = "") -> str:
    """
    Store an uploaded response and register it. Returns the new video id.

    Raises ValueError with a message meant for the person, not the log.
    """
    if not file_storage or not file_storage.filename:
        raise ValueError("Choose a video file first.")

    suffix = Path(file_storage.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"{suffix or 'That file type'} isn't a video format this player handles."
        )

    video_id = f"resp_{uuid.uuid4().hex[:16]}"
    safe_name = secure_filename(file_storage.filename) or f"response{suffix}"
    destination = responses_dir(user["id"]) / f"{int(time.time())}-{video_id}-{safe_name}"

    file_storage.save(destination)

    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("That file arrived empty. Try again.")

    duration = streams.probe_duration(str(destination))
    thumbnail = _extract_poster(destination, video_id)

    videos_repo.upsert(
        video_id=video_id,
        title=(title or Path(file_storage.filename).stem)[:200],
        channel_name=user.get("display_name") or user["username"],
        thumbnail=thumbnail,
        description="",
        source="response",
        duration=int(duration) if duration else None,
        file_path=str(destination),
    )
    return video_id


def _extract_poster(path: Path, video_id: str) -> str:
    """Grab a frame for the card. A response without one still works."""
    cache_dir = Path(current_app.config["CACHE_DIR"]) / "responses"
    cache_dir.mkdir(parents=True, exist_ok=True)
    poster = cache_dir / f"{video_id}.jpg"

    args = [
        current_app.config["FFMPEG_PATH"],
        "-hide_banner", "-loglevel", "error",
        "-ss", "00:00:02",
        "-i", str(path),
        "-frames:v", "1",
        "-vf", "scale=640:-2",
        "-q:v", "4",
        "-y", str(poster),
    ]
    try:
        subprocess.run(args, capture_output=True, timeout=45)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("Response poster failed for %s: %s", video_id, exc)

    if poster.exists() and poster.stat().st_size > 0:
        return f"/media/response-thumb/{video_id}"
    return ""


def poster_path(video_id: str) -> Path | None:
    poster = Path(current_app.config["CACHE_DIR"]) / "responses" / f"{video_id}.jpg"
    return poster if poster.exists() and poster.stat().st_size > 0 else None


# --------------------------------------------------------------------------
# Linking something that already exists
# --------------------------------------------------------------------------
def resolve_reference(reference: str) -> str | None:
    """
    Turn whatever someone pasted into a video id this server can play.

    Accepts a bare id, a Hub watch URL, or a YouTube link. Returns None if it
    doesn't look like anything; the caller turns that into a message.
    """
    reference = (reference or "").strip()
    if not reference:
        return None

    hub_match = HUB_WATCH_URL.search(reference)
    if hub_match:
        return hub_match.group(1)

    youtube_match = YOUTUBE_URL.search(reference)
    if youtube_match:
        return youtube_match.group(1)

    if reference.startswith(("local_", "tubi_", "resp_", "upload_")):
        return reference

    if YOUTUBE_ID.match(reference):
        return reference

    return None


def delete_response(video_id: str) -> None:
    """Remove the file and the catalogue row. Used when a comment is deleted."""
    path = path_for(video_id)
    if path:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove response file %s: %s", video_id, exc)

    poster = poster_path(video_id)
    if poster:
        poster.unlink(missing_ok=True)
