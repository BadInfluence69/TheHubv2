"""
Studio: connect a Google account and publish videos to YouTube.

This is the one part of the app that sends anything outward. Everything else -
comments, likes, subscriptions, playlists - stays in the local database.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from ..repo import studio as studio_repo
from ..security import current_user, login_required
from ..services import google_oauth, local_media, streams, youtube_upload

log = logging.getLogger(__name__)
bp = Blueprint("studio", __name__, url_prefix="/studio")

ALLOWED_VIDEO = {
    ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
    ".mpeg", ".3gp", ".ts",
}
ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@bp.route("/")
@login_required
def dashboard():
    user = current_user()
    return render_template(
        "studio/dashboard.html",
        connection=google_oauth.connection_status(user["id"]),
        jobs=studio_repo.jobs_for(user["id"], limit=40),
        stats=studio_repo.stats(user["id"]),
        redirect_uri=_redirect_uri_safe(),
    )


def _redirect_uri_safe() -> str:
    try:
        return google_oauth.redirect_uri()
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# Connecting Google
# --------------------------------------------------------------------------
@bp.route("/connect")
@login_required
def connect():
    if not google_oauth.is_configured():
        flash(
            "Add your Google OAuth credentials first — client_secret.json in the "
            "project folder, or GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env.",
            "error",
        )
        return redirect(url_for("studio.dashboard"))

    try:
        url, state, code_verifier = google_oauth.authorization_url()
    except google_oauth.OAuthNotConfigured as exc:
        flash(str(exc), "error")
        return redirect(url_for("studio.dashboard"))

    session["oauth_state"] = state
    # PKCE: Google is given a hash of this now and demands the original back
    # at the token exchange. It has to survive the round trip to Google.
    session["oauth_code_verifier"] = code_verifier
    return redirect(url)


@bp.route("/oauth/callback")
@login_required
def oauth_callback():
    user = current_user()
    state = session.pop("oauth_state", None)
    code_verifier = session.pop("oauth_code_verifier", None)

    if request.args.get("error"):
        flash(f"Google cancelled the connection: {request.args['error']}", "error")
        return redirect(url_for("studio.dashboard"))

    if not state or state != request.args.get("state"):
        flash("That sign-in didn't match the one this browser started. Try again.", "error")
        return redirect(url_for("studio.dashboard"))

    if not code_verifier:
        flash(
            "This browser lost track of the sign-in partway through. "
            "Start the connection again from this tab.",
            "error",
        )
        return redirect(url_for("studio.dashboard"))

    try:
        channel = google_oauth.finish_authorization(
            user["id"], state, request.url, code_verifier
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("OAuth exchange failed")
        flash(f"Connecting failed: {exc}", "error")
        return redirect(url_for("studio.dashboard"))

    name = channel.get("title") or "your channel"
    flash(f"Connected to {name}. You can publish from here now.", "success")
    return redirect(url_for("studio.dashboard"))


@bp.post("/disconnect")
@login_required
def disconnect():
    google_oauth.disconnect(current_user()["id"])
    flash("Google account disconnected. Local data is untouched.", "success")
    return redirect(url_for("studio.dashboard"))


# --------------------------------------------------------------------------
# Uploading
# --------------------------------------------------------------------------
@bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    user = current_user()
    connection = google_oauth.connection_status(user["id"])

    if request.method == "GET":
        return render_template(
            "studio/upload.html",
            connection=connection,
            categories=youtube_upload.CATEGORIES,
            library=local_media.scan()[:400],
        )

    if not connection["connected"]:
        flash("Connect a Google account before uploading.", "error")
        return redirect(url_for("studio.dashboard"))

    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Give the video a title.", "error")
        return redirect(url_for("studio.upload"))

    try:
        path, original_name = _resolve_source_file(user["id"])
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("studio.upload"))

    thumbnail_path = _save_thumbnail(user["id"])

    job_id = studio_repo.create_job(
        user_id=user["id"],
        file_path=str(path),
        original_name=original_name,
        file_size=os.path.getsize(path),
        title=title[:100],
        description=(request.form.get("description") or "")[:5000],
        tags=request.form.get("tags", ""),
        category_id=request.form.get("category_id", "22"),
        privacy=request.form.get("privacy", "private"),
        made_for_kids=request.form.get("made_for_kids") == "on",
        publish_at=_publish_at(),
        thumbnail_path=thumbnail_path,
    )

    youtube_upload.start(current_app._get_current_object(), job_id)
    flash("Upload started. Progress is on this page.", "success")
    return redirect(url_for("studio.dashboard"))


def _resolve_source_file(user_id: int) -> tuple[Path, str]:
    """
    Either a browser upload or a file already sitting in the media library.
    Library files are streamed straight from disk — no second copy.
    """
    library_id = (request.form.get("library_video") or "").strip()
    if library_id:
        path = local_media.safe_path(library_id)
        if not path:
            raise ValueError("That library file couldn't be found on disk.")
        return path, path.name

    uploaded = request.files.get("video_file")
    if not uploaded or not uploaded.filename:
        raise ValueError("Choose a video file, or pick one from your library.")

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in ALLOWED_VIDEO:
        raise ValueError(
            f"{suffix or 'That file type'} isn't a video format YouTube accepts."
        )

    upload_dir = Path(current_app.config["UPLOAD_DIR"]) / str(user_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    safe_name = secure_filename(uploaded.filename) or f"video{suffix}"
    destination = upload_dir / f"{int(time.time())}-{uuid.uuid4().hex[:8]}-{safe_name}"
    uploaded.save(destination)

    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("That file arrived empty. Try the upload again.")

    return destination, uploaded.filename


def _save_thumbnail(user_id: int) -> str | None:
    image = request.files.get("thumbnail_file")
    if not image or not image.filename:
        return None

    suffix = Path(image.filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE:
        return None

    thumb_dir = Path(current_app.config["UPLOAD_DIR"]) / str(user_id) / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    destination = thumb_dir / f"{uuid.uuid4().hex[:10]}{suffix}"
    image.save(destination)
    return str(destination)


def _publish_at() -> str | None:
    """Turn the datetime-local field into the RFC 3339 stamp Google expects."""
    raw = (request.form.get("publish_at") or "").strip()
    if not raw:
        return None
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(raw)
        return parsed.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Job control
# --------------------------------------------------------------------------
@bp.route("/api/jobs")
@login_required
def jobs_json():
    user = current_user()
    jobs = studio_repo.jobs_for(user["id"], limit=40)
    for job in jobs:
        job["running"] = youtube_upload.is_running(job["id"])
        job["percent"] = (
            round(job["bytes_sent"] / job["file_size"] * 100, 1)
            if job["file_size"]
            else 0
        )
    return jsonify({"jobs": jobs})


@bp.post("/jobs/<int:job_id>/retry")
@login_required
def retry_job(job_id: int):
    user = current_user()
    job = studio_repo.get_job(job_id, user["id"])
    if not job:
        abort(404)

    studio_repo.update_job(job_id, status="pending", error=None, bytes_sent=0)
    youtube_upload.start(current_app._get_current_object(), job_id)
    flash("Retrying that upload.", "success")
    return redirect(url_for("studio.dashboard"))


@bp.post("/jobs/<int:job_id>/cancel")
@login_required
def cancel_job(job_id: int):
    user = current_user()
    if not studio_repo.get_job(job_id, user["id"]):
        abort(404)
    studio_repo.update_job(job_id, status="cancelled")
    flash("Upload cancelled.", "success")
    return redirect(url_for("studio.dashboard"))


@bp.post("/jobs/<int:job_id>/delete")
@login_required
def delete_job(job_id: int):
    user = current_user()
    job = studio_repo.get_job(job_id, user["id"])
    if not job:
        abort(404)

    # Only clean up copies this app made; never touch library originals.
    upload_root = Path(current_app.config["UPLOAD_DIR"]).resolve()
    if request.form.get("delete_file") == "on" and job["file_path"]:
        try:
            path = Path(job["file_path"]).resolve()
            if upload_root in path.parents:
                path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove %s: %s", job["file_path"], exc)

    studio_repo.delete_job(job_id, user["id"])
    flash("Removed from the list.", "success")
    return redirect(url_for("studio.dashboard"))


# --------------------------------------------------------------------------
# Managing videos already on YouTube
# --------------------------------------------------------------------------
@bp.route("/videos")
@login_required
def my_videos():
    user = current_user()
    connection = google_oauth.connection_status(user["id"])
    videos = youtube_upload.list_my_videos(user["id"]) if connection["connected"] else []
    return render_template(
        "studio/videos.html",
        connection=connection,
        videos=videos,
        categories=youtube_upload.CATEGORIES,
    )


@bp.post("/videos/<video_id>/update")
@login_required
def update_video(video_id: str):
    user = current_user()
    ok, message = youtube_upload.update_video(
        user["id"],
        video_id,
        request.form.get("title", ""),
        request.form.get("description", ""),
        request.form.get("tags", ""),
        request.form.get("privacy", "private"),
        request.form.get("category_id", "22"),
    )
    flash(message, "success" if ok else "error")
    return redirect(url_for("studio.my_videos"))


@bp.post("/videos/<video_id>/delete")
@login_required
def delete_video(video_id: str):
    user = current_user()
    ok, message = youtube_upload.delete_video(user["id"], video_id)
    flash(message, "success" if ok else "error")
    return redirect(url_for("studio.my_videos"))


# --------------------------------------------------------------------------
# Pull a video down from a URL, then publish it
# --------------------------------------------------------------------------
@bp.post("/fetch")
@login_required
def fetch_remote():
    """
    Download something with yt-dlp into the uploads folder so it can be
    reviewed and published. Useful for re-publishing your own content.
    """
    user = current_user()
    url = (request.form.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        flash("Paste a full http:// or https:// link.", "error")
        return redirect(url_for("studio.upload"))

    upload_dir = Path(current_app.config["UPLOAD_DIR"]) / str(user["id"])
    upload_dir.mkdir(parents=True, exist_ok=True)

    template = str(upload_dir / "%(title).80s-%(id)s.%(ext)s")
    args = [
        current_app.config["YTDLP_PATH"],
        "--no-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", template,
        url,
    ]
    ffmpeg = current_app.config.get("FFMPEG_PATH")
    if ffmpeg and shutil.which(ffmpeg) or (ffmpeg and os.path.exists(ffmpeg)):
        args[1:1] = ["--ffmpeg-location", ffmpeg]

    try:
        result = streams._run(args, timeout=1800)  # noqa: SLF001 - shared runner
        if result.returncode != 0:
            flash(
                "Download failed: " + ((result.stderr or "").strip()[:300] or "unknown error"),
                "error",
            )
        else:
            local_media.invalidate()
            flash("Downloaded. It's in your uploads folder, ready to publish.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Download failed: {exc}", "error")

    return redirect(url_for("studio.upload"))