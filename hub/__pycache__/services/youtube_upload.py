"""
Publishing to YouTube.

Uploads run on a background thread using the API's resumable protocol, so a
dropped connection retries the failed chunk instead of restarting a 3 GB file.
Progress is written to the uploads table after every chunk; the Studio page
polls it.

Quota note: the API gives a project 10,000 units a day by default and each
upload costs 1,600, so roughly six uploads per day before Google starts
returning quotaExceeded. Requesting more is a form on the Cloud Console.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import threading
import time

from googleapiclient.errors import HttpError, ResumableUploadError
from googleapiclient.http import MediaFileUpload

from .. import db
from ..repo import studio as studio_repo
from ..repo import users as users_repo
from . import google_oauth

log = logging.getLogger(__name__)

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8

PRIVACY_CHOICES = ("private", "unlisted", "public")

# youtube.videoCategories, US list. Stable enough to hard-code.
CATEGORIES = [
    ("1", "Film & Animation"), ("2", "Autos & Vehicles"), ("10", "Music"),
    ("15", "Pets & Animals"), ("17", "Sports"), ("19", "Travel & Events"),
    ("20", "Gaming"), ("22", "People & Blogs"), ("23", "Comedy"),
    ("24", "Entertainment"), ("25", "News & Politics"),
    ("26", "Howto & Style"), ("27", "Education"),
    ("28", "Science & Technology"), ("29", "Nonprofits & Activism"),
]

_running: set[int] = set()
_running_lock = threading.Lock()


def is_running(job_id: int) -> bool:
    with _running_lock:
        return job_id in _running


# --------------------------------------------------------------------------
# Starting a job
# --------------------------------------------------------------------------
def start(app, job_id: int) -> bool:
    """Kick off an upload in the background. Returns False if already going."""
    with _running_lock:
        if job_id in _running:
            return False
        _running.add(job_id)

    thread = threading.Thread(
        target=_run_job, args=(app, job_id), name=f"upload-{job_id}", daemon=True
    )
    thread.start()
    return True


def _run_job(app, job_id: int) -> None:
    with app.app_context():
        try:
            _upload(app, job_id)
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            log.exception("Upload job %s crashed", job_id)
            try:
                studio_repo.update_job(job_id, status="error", error=str(exc)[:500])
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _running_lock:
                _running.discard(job_id)


def _upload(app, job_id: int) -> None:
    job = studio_repo.get_job(job_id)
    if not job:
        return

    user_id = job["user_id"]
    path = job["file_path"]

    if not os.path.exists(path):
        studio_repo.update_job(
            job_id, status="error", error="The video file is no longer on disk."
        )
        return

    youtube = google_oauth.youtube_client(user_id)
    if youtube is None:
        studio_repo.update_job(
            job_id,
            status="error",
            error="No Google account is connected. Connect one in Studio and retry.",
        )
        return

    body = _request_body(job)
    mimetype = mimetypes.guess_type(path)[0] or "video/*"
    media = MediaFileUpload(
        path, mimetype=mimetype, chunksize=CHUNK_SIZE, resumable=True
    )

    studio_repo.update_job(job_id, status="uploading", bytes_sent=0, error=None)

    request = youtube.videos().insert(
        part=",".join(body.keys()), body=body, media_body=media
    )

    response = None
    retries = 0
    file_size = os.path.getsize(path)

    while response is None:
        # Let a cancel request from the UI stop the loop cleanly.
        current = studio_repo.get_job(job_id)
        if current and current["status"] == "cancelled":
            log.info("Upload %s cancelled by request", job_id)
            return

        try:
            status, response = request.next_chunk()
            if status:
                sent = int(status.resumable_progress or 0)
                studio_repo.update_job(job_id, bytes_sent=sent)
                db.get_db().commit()
            retries = 0
        except ResumableUploadError as exc:
            studio_repo.update_job(
                job_id, status="error", error=_readable_error(exc)
            )
            return
        except HttpError as exc:
            if exc.resp.status in RETRIABLE_STATUS and retries < MAX_RETRIES:
                retries += 1
                sleep_for = min(2 ** retries, 60)
                log.warning(
                    "Upload %s hit HTTP %s, retrying in %ss",
                    job_id, exc.resp.status, sleep_for,
                )
                time.sleep(sleep_for)
                continue
            studio_repo.update_job(job_id, status="error", error=_readable_error(exc))
            return
        except (OSError, ConnectionError) as exc:
            if retries < MAX_RETRIES:
                retries += 1
                time.sleep(min(2 ** retries, 60))
                continue
            studio_repo.update_job(
                job_id, status="error", error=f"Connection failed: {exc}"
            )
            return

    video_id = (response or {}).get("id")
    if not video_id:
        studio_repo.update_job(
            job_id, status="error", error="YouTube accepted the file but returned no video ID."
        )
        return

    studio_repo.update_job(
        job_id,
        status="processing",
        bytes_sent=file_size,
        youtube_video_id=video_id,
        error=None,
    )

    if job["thumbnail_path"] and os.path.exists(job["thumbnail_path"]):
        _set_thumbnail(youtube, video_id, job["thumbnail_path"], job_id)

    studio_repo.update_job(job_id, status="done")
    users_repo.notify(
        user_id,
        "Published to YouTube",
        f"“{job['title']}” is live.",
        url=f"https://www.youtube.com/watch?v={video_id}",
        kind="upload",
    )
    db.get_db().commit()
    log.info("Upload %s finished as https://youtu.be/%s", job_id, video_id)


def _request_body(job: dict) -> dict:
    tags = [t.strip() for t in (job["tags"] or "").split(",") if t.strip()][:60]
    status = {
        "privacyStatus": job["privacy"] if job["privacy"] in PRIVACY_CHOICES else "private",
        "selfDeclaredMadeForKids": bool(job["made_for_kids"]),
        "embeddable": True,
    }
    if job["publish_at"]:
        # A scheduled video has to start out private.
        status["publishAt"] = job["publish_at"]
        status["privacyStatus"] = "private"

    return {
        "snippet": {
            "title": (job["title"] or "Untitled")[:100],
            "description": (job["description"] or "")[:5000],
            "tags": tags,
            "categoryId": job["category_id"] or "22",
        },
        "status": status,
    }


def _set_thumbnail(youtube, video_id: str, path: str, job_id: int) -> None:
    try:
        youtube.thumbnails().set(
            videoId=video_id, media_body=MediaFileUpload(path)
        ).execute()
    except HttpError as exc:
        # A custom thumbnail needs a verified channel; not worth failing over.
        log.info("Thumbnail rejected for %s: %s", video_id, exc)
        studio_repo.update_job(
            job_id,
            error="Video uploaded. The custom thumbnail was rejected — that "
                  "feature needs a verified YouTube channel.",
        )


def _readable_error(exc: HttpError) -> str:
    """Turn Google's error payloads into something worth showing a person."""
    try:
        import json as _json

        payload = _json.loads(exc.content.decode("utf-8"))
        error = payload.get("error", {})
        reason = (error.get("errors") or [{}])[0].get("reason", "")
        message = error.get("message", "")
    except Exception:  # noqa: BLE001
        reason, message = "", str(exc)

    friendly = {
        "quotaExceeded":
            "Your YouTube API quota is used up for the day. It resets at "
            "midnight Pacific. Each upload costs 1,600 of the default 10,000 units.",
        "uploadLimitExceeded":
            "This channel has hit its daily upload limit. Try again tomorrow.",
        "forbidden":
            "YouTube refused this upload. Check the channel is verified and in "
            "good standing.",
        "youtubeSignupRequired":
            "That Google account has no YouTube channel yet. Create one at "
            "youtube.com, then reconnect.",
        "invalidVideoMetadata":
            "YouTube rejected the title, description, or tags. Titles cap at "
            "100 characters and descriptions at 5,000.",
        "mediaBodyRequired": "The video file was empty or unreadable.",
        "invalidCategoryId": "That category isn't valid for your region.",
        "authError": "The Google connection expired. Reconnect in Studio and retry.",
    }
    return friendly.get(reason) or message or "YouTube rejected the upload."


# --------------------------------------------------------------------------
# Managing videos already on YouTube
# --------------------------------------------------------------------------
def list_my_videos(user_id: int, limit: int = 50) -> list[dict]:
    """Videos on the connected channel, newest first."""
    youtube = google_oauth.youtube_client(user_id)
    if youtube is None:
        return []

    try:
        channels = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = channels.get("items") or []
        if not items:
            return []
        uploads_playlist = (
            (items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}
        ).get("uploads")
        if not uploads_playlist:
            return []

        playlist = (
            youtube.playlistItems()
            .list(part="snippet,contentDetails", playlistId=uploads_playlist,
                  maxResults=min(limit, 50))
            .execute()
        )
        video_ids = [
            item["contentDetails"]["videoId"]
            for item in playlist.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        ]
        if not video_ids:
            return []

        details = (
            youtube.videos()
            .list(part="snippet,status,statistics", id=",".join(video_ids))
            .execute()
        )
        results = []
        for item in details.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            results.append(
                {
                    "id": item["id"],
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "thumbnail": (
                        (snippet.get("thumbnails") or {}).get("medium") or {}
                    ).get("url", ""),
                    "privacy": (item.get("status") or {}).get("privacyStatus", ""),
                    "views": int(stats.get("viewCount", 0) or 0),
                    "likes": int(stats.get("likeCount", 0) or 0),
                    "comments": int(stats.get("commentCount", 0) or 0),
                    "tags": snippet.get("tags", []),
                    "category_id": snippet.get("categoryId", ""),
                }
            )
        return results
    except HttpError as exc:
        log.warning("Listing channel videos failed: %s", exc)
        return []


def update_video(user_id: int, video_id: str, title: str, description: str,
                 tags: str, privacy: str, category_id: str = "22") -> tuple[bool, str]:
    youtube = google_oauth.youtube_client(user_id)
    if youtube is None:
        return False, "No Google account connected."

    try:
        youtube.videos().update(
            part="snippet,status",
            body={
                "id": video_id,
                "snippet": {
                    "title": title[:100],
                    "description": description[:5000],
                    "tags": [t.strip() for t in tags.split(",") if t.strip()][:60],
                    "categoryId": category_id or "22",
                },
                "status": {
                    "privacyStatus": privacy if privacy in PRIVACY_CHOICES else "private"
                },
            },
        ).execute()
        return True, "Changes saved to YouTube."
    except HttpError as exc:
        return False, _readable_error(exc)


def delete_video(user_id: int, video_id: str) -> tuple[bool, str]:
    youtube = google_oauth.youtube_client(user_id)
    if youtube is None:
        return False, "No Google account connected."
    try:
        youtube.videos().delete(id=video_id).execute()
        return True, "Deleted from YouTube."
    except HttpError as exc:
        return False, _readable_error(exc)


def upload_thumbnail(user_id: int, video_id: str, path: str) -> tuple[bool, str]:
    youtube = google_oauth.youtube_client(user_id)
    if youtube is None:
        return False, "No Google account connected."
    try:
        youtube.thumbnails().set(
            videoId=video_id, media_body=MediaFileUpload(path)
        ).execute()
        return True, "Thumbnail updated."
    except HttpError as exc:
        return False, _readable_error(exc)
