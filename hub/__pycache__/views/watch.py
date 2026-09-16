"""The watch page: player, description, votes, comments, and the up-next rail."""
from __future__ import annotations

import logging

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import db
from ..repo import comments as comments_repo
from ..repo import library as library_repo
from ..repo import creators as creators_repo
from ..repo import subs as subs_repo
from ..repo import users as users_repo
from ..repo import videos as videos_repo
from ..security import current_user, login_required
from ..services import (
    local_media,
    recommend,
    responses,
    streams,
    tubi,
    youtube_search,
)


log = logging.getLogger(__name__)
bp = Blueprint("watch", __name__)


# --------------------------------------------------------------------------
# Resolving a video from any source
# --------------------------------------------------------------------------
def _load_video(video_id: str) -> dict | None:
    """Metadata for a video, whatever it is and wherever it came from."""
    if video_id.startswith("resp_"):
        # Video responses are registered at upload time, so the catalogue row
        # is the whole story — there is nothing external to go and ask.
        return videos_repo.get(video_id)

    if video_id.startswith("local_"):
        item = local_media.get(video_id)
        if item:
            videos_repo.upsert(
                video_id=item["id"],
                title=item["title"],
                channel_name=item["channel"],
                thumbnail=item["thumbnail"],
                description=item["description"],
                source="local",
                file_path=item["file_path"],
            )
        stored = videos_repo.get(video_id)
        if stored and item:
            stored.update({"file_size": item["file_size"], "shelf": item["shelf"]})
        return stored

    stored = videos_repo.get(video_id)
    if stored and stored.get("title") not in (None, "", "Untitled"):
        return stored

    # Not seen before: go and find out what it is.
    if tubi.is_tubi_id(video_id):
        try:
            info = tubi.resolve_stream(video_id)
            videos_repo.upsert(
                video_id=video_id,
                title=info.get("title") or "Tubi title",
                channel_name="Tubi",
                thumbnail=info.get("thumbnail") or "",
                description=info.get("description") or "",
                source="tubi",
                duration=info.get("duration"),
            )
        except streams.StreamError:
            videos_repo.upsert(video_id, "Tubi title", "Tubi", source="tubi")
    else:
        details = youtube_search.video_details(video_id)
        if details:
            videos_repo.upsert(
                video_id=video_id,
                title=details["title"],
                channel_name=details["channel"],
                thumbnail=details["thumbnail"],
                description=details["description"],
                tags=details.get("tags"),
                source="youtube",
                duration=details.get("duration"),
                published_at=details.get("published_at"),
            )
        else:
            videos_repo.upsert(
                video_id=video_id,
                title="Untitled",
                channel_name="",
                thumbnail=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                source="youtube",
            )

    return videos_repo.get(video_id)


def _resolve_stream(video_id: str, quality: str = "auto") -> tuple[dict | None, str]:
    """Returns (stream_info, error_message)."""
    try:
        if video_id.startswith("resp_"):
            if not responses.path_for(video_id):
                return None, "That video response isn't on the drive any more."
            return {
                "url": url_for("media.stream_response", video_id=video_id),
                "qualities": [],
                "subtitles": [],
                "chapters": [],
                "direct": True,
            }, ""

        if video_id.startswith("local_"):
            path = local_media.safe_path(video_id)
            if not path:
                return None, "That file isn't on the drive any more."
            return {
                "url": url_for("media.stream_local", video_id=video_id),
                "qualities": [],
                "subtitles": [],
                "chapters": [],
                "direct": True,
            }, ""

        if tubi.is_tubi_id(video_id):
            return tubi.resolve_stream(video_id), ""

        return streams.resolve(video_id, quality), ""
    except streams.StreamError as exc:
        log.warning("Stream resolve failed for %s: %s", video_id, exc)
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected stream failure for %s", video_id)
        return None, f"Could not resolve a stream: {exc}"


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
@bp.route("/watch/<path:video_id>")
@login_required
def watch(video_id: str):
    user = current_user()

    # Tubi links are written /watch/Tubi_123456, but the catalogue keys them
    # lowercase. Canonicalise here so both spellings reach the same row
    # instead of quietly creating a duplicate.
    if tubi.is_tubi_id(video_id):
        canonical = tubi.canonical_id(video_id)
        if canonical != video_id:
            return redirect(url_for("watch.watch", video_id=canonical))

    video = _load_video(video_id)
    if not video:
        abort(404)

    settings = users_repo.get_settings(user["id"])
    if settings.get("pause_history") != "1":
        library_repo.record_watch(user["id"], video_id)
        videos_repo.record_view(video_id)

    quality = request.args.get("quality", settings.get("default_quality", "auto"))
    stream, stream_error = _resolve_stream(video_id, quality)

    # yt-dlp often knows more than the search results did.
    if stream and stream.get("duration") and not video.get("duration"):
        videos_repo.upsert(
            video_id=video_id,
            title=video["title"],
            channel_name=video.get("channel_name") or "",
            thumbnail=video.get("thumbnail") or "",
            description=video.get("description") or "",
            source=video.get("source", "youtube"),
            duration=stream.get("duration"),
        )
        video["duration"] = stream["duration"]

    if stream and stream.get("description") and len(stream["description"]) > len(
        video.get("description") or ""
    ):
        video["description"] = stream["description"]

    sort = request.args.get("sort", "top")
    comment_list = comments_repo.for_video(video_id, user["id"], sort=sort)

    channel_name = video.get("channel_name") or ""
    playlists = library_repo.playlists_for(user["id"])
    in_playlists = library_repo.playlists_containing(user["id"], video_id)

    up_next = recommend.up_next(video, user["id"], limit=20)
    videos_repo.attach_user_state(up_next, user["id"])

    return render_template(
        "watch.html",
        video=video,
        video_id=video_id,
        stream=stream,
        stream_error=stream_error,
        comments=comment_list,
        comment_sort=sort,
        comment_count=comments_repo.count_for_video(video_id),
        response_count=comments_repo.response_count(video_id),
        video_responses=comments_repo.responses_to(video_id, limit=12),
        responding_to=comments_repo.responded_to_by(video_id)
                      if video_id.startswith("resp_") else None,
        my_vote=videos_repo.get_vote(user["id"], video_id),
        subscribed=subs_repo.is_subscribed(user["id"], channel_name) if channel_name else False,
        subscriber_count=subs_repo.subscriber_count(channel_name) if channel_name else 0,
        creator=creators_repo.public_profile(channel_name) if channel_name else None,
        up_next=up_next,
        playlists=playlists,
        in_playlists=in_playlists,
        resume_at=library_repo.resume_position(user["id"], video_id),
        autoplay=settings.get("autoplay") == "1",
    )


@bp.route("/api/stream/<path:video_id>")
@login_required
def stream_json(video_id: str):
    """Used by the quality picker and by the retry button."""
    quality = request.args.get("quality", "auto")
    if request.args.get("refresh") == "1":
        streams.invalidate(video_id)
    stream, error = _resolve_stream(video_id, quality)
    if not stream:
        return jsonify({"error": error}), 502
    return jsonify(stream)


# --------------------------------------------------------------------------
# Votes
# --------------------------------------------------------------------------
@bp.post("/api/videos/<path:video_id>/vote")
@login_required
def vote(video_id: str):
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    try:
        value = int(payload.get("value", 0))
    except (TypeError, ValueError):
        value = 0
    if value not in (-1, 0, 1):
        return jsonify({"error": "Vote must be 1, -1, or 0."}), 400

    new_vote = videos_repo.set_vote(user["id"], video_id, value)
    counts = videos_repo.counts(video_id)
    return jsonify({"my_vote": new_vote, **counts})


# --------------------------------------------------------------------------
# Watch progress
# --------------------------------------------------------------------------
@bp.post("/api/videos/<path:video_id>/progress")
@login_required
def save_progress(video_id: str):
    user = current_user()
    payload = request.get_json(silent=True) or {}
    try:
        position = float(payload.get("position", 0))
        duration = payload.get("duration")
        duration = float(duration) if duration else None
    except (TypeError, ValueError):
        return jsonify({"error": "Bad numbers."}), 400

    settings = users_repo.get_settings(user["id"])
    if settings.get("pause_history") != "1":
        library_repo.save_progress(user["id"], video_id, position, duration)
    return jsonify({"ok": True})



@bp.route("/api/next-video")
@login_required
def next_video():
    """What autoplay should roll into when the current video ends."""
    user = current_user()
    current_id = request.args.get("current", "")
    video = videos_repo.get(current_id) or {"video_id": current_id, "title": ""}
    suggestions = recommend.up_next(video, user["id"], limit=5)
    if not suggestions:
        return jsonify({"next_id": None})
    nxt = suggestions[0]
    return jsonify(
        {
            "next_id": nxt.get("video_id") or nxt.get("id"),
            "title": nxt.get("title"),
            "channel": nxt.get("channel_name") or nxt.get("channel"),
            "thumbnail": nxt.get("thumbnail"),
        }
    )


# --------------------------------------------------------------------------
# Comments, and video responses
# --------------------------------------------------------------------------
def _attach_response(user: dict) -> tuple[str | None, str | None]:
    """
    Work out what video, if any, a new comment is carrying.

    Two shapes: an uploaded file under `response_file`, or a reference to
    something that already exists under `response_ref`. Returns
    (video_id, error_message) — exactly one of them is set.
    """
    uploaded = request.files.get("response_file")
    if uploaded and uploaded.filename:
        try:
            return responses.save_upload(
                user, uploaded, title=request.form.get("response_title", "")
            ), None
        except ValueError as exc:
            return None, str(exc)

    reference = (request.form.get("response_ref") or "").strip()
    if not reference:
        payload = request.get_json(silent=True) or {}
        reference = (payload.get("response_ref") or "").strip()

    if reference:
        resolved = responses.resolve_reference(reference)
        if not resolved:
            return None, "That doesn't look like a video link or id."
        return resolved, None

    return None, None


@bp.post("/api/videos/<path:video_id>/comments")
@login_required
def add_comment(video_id: str):
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    body = (payload.get("body") or "").strip()
    parent_id = payload.get("parent_id")

    response_video_id, response_error = _attach_response(user)
    if response_error:
        return jsonify({"error": response_error}), 400

    # A video response doesn't need words with it — the video is the comment.
    if not body and not response_video_id:
        return jsonify({"error": "Write something first."}), 400
    if len(body) > comments_repo.MAX_LENGTH:
        return jsonify(
            {"error": f"Comments cap at {comments_repo.MAX_LENGTH} characters."}
        ), 400

    try:
        parent_id = int(parent_id) if parent_id else None
    except (TypeError, ValueError):
        parent_id = None

    # Make sure a referenced video is in the catalogue, so the card has a title
    # to show rather than a bare id.
    if response_video_id and not videos_repo.get(response_video_id):
        _load_video(response_video_id)

    comment_id = comments_repo.add(
        video_id, user, body, parent_id, response_video_id=response_video_id
    )
    comment = comments_repo.get(comment_id)
    comment["replies"] = []
    comment["my_vote"] = 0

    # Tell the parent commenter someone replied to them.
    if parent_id:
        parent = comments_repo.get(parent_id)
        if parent and parent["user_id"] and parent["user_id"] != user["id"]:
            users_repo.notify(
                parent["user_id"],
                f"{user['display_name'] or user['username']} replied",
                body[:140] or "Replied with a video.",
                url=f"/watch/{video_id}#comment-{comment_id}",
                kind="reply",
            )
            users_repo.trim_notifications(parent["user_id"])

    # And tell whoever uploaded the video that it picked up a video response.
    if response_video_id and not parent_id:
        _notify_video_owner(video_id, user, comment_id)

    html = render_template(
        "partials/comment.html", comment=comment, video_id=video_id, is_reply=bool(parent_id)
    )
    return jsonify(
        {
            "ok": True,
            "comment": comment,
            "html": html,
            "count": comments_repo.count_for_video(video_id),
            "responses": comments_repo.response_count(video_id),
        }
    )


def _notify_video_owner(video_id: str, sender: dict, comment_id: int) -> None:
    """
    Only uploads and responses have an owner on this server — a YouTube video
    belongs to somebody who has no account here, so there's nobody to tell.
    """
    video = videos_repo.get(video_id)
    if not video or video.get("source") not in ("upload", "response", "local"):
        return

    owner_id = db.scalar(
        "SELECT user_id FROM uploads WHERE youtube_video_id = ? LIMIT 1", (video_id,)
    )
    if not owner_id:
        owner_id = db.scalar(
            """
            SELECT c.user_id FROM comments c
            WHERE c.response_video_id = ? AND c.is_deleted = 0
            ORDER BY c.id ASC LIMIT 1
            """,
            (video_id,),
        )
    if not owner_id or owner_id == sender["id"]:
        return

    users_repo.notify(
        owner_id,
        f"{sender['display_name'] or sender['username']} posted a video response",
        video.get("title") or "",
        url=f"/watch/{video_id}#comment-{comment_id}",
        kind="response",
    )
    users_repo.trim_notifications(owner_id)


@bp.post("/api/comments/<int:comment_id>/edit")
@login_required
def edit_comment(comment_id: int):
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    body = (payload.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Write something first."}), 400

    if not comments_repo.edit(comment_id, user["id"], body):
        return jsonify({"error": "You can only edit your own comments."}), 403
    return jsonify({"ok": True, "comment": comments_repo.get(comment_id)})


@bp.post("/api/comments/<int:comment_id>/delete")
@login_required
def delete_comment(comment_id: int):
    user = current_user()
    # Grab this before the delete, or there's nothing left to look up.
    response_video_id = comments_repo.response_video_for(comment_id)

    if not comments_repo.delete(comment_id, user["id"], bool(user.get("is_admin"))):
        return jsonify({"error": "You can only delete your own comments."}), 403

    # An uploaded response exists only for its comment. Once that's gone the
    # file is dead weight, so it goes too. References to videos that were
    # already here are left alone — they weren't ours to delete.
    if response_video_id and response_video_id.startswith("resp_"):
        responses.delete_response(response_video_id)

    return jsonify({"ok": True})


@bp.post("/api/comments/<int:comment_id>/vote")
@login_required
def vote_comment(comment_id: int):
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    try:
        value = int(payload.get("value", 0))
    except (TypeError, ValueError):
        value = 0
    if value not in (-1, 0, 1):
        return jsonify({"error": "Vote must be 1, -1, or 0."}), 400

    new_vote = comments_repo.vote(user["id"], comment_id, value)
    return jsonify({"my_vote": new_vote, **comments_repo.vote_counts(comment_id)})


@bp.post("/api/comments/<int:comment_id>/pin")
@login_required
def pin_comment(comment_id: int):
    user = current_user()
    comment = comments_repo.get(comment_id)
    if not comment:
        abort(404)
    if not user.get("is_admin") and comment["user_id"] != user["id"]:
        return jsonify({"error": "Only the author or an admin can pin."}), 403
    comments_repo.set_pinned(comment_id, not comment["is_pinned"])
    return jsonify({"ok": True, "pinned": not comment["is_pinned"]})


# --------------------------------------------------------------------------
# Plain-form fallbacks, so the page still works without JavaScript
# --------------------------------------------------------------------------
@bp.post("/watch/<path:video_id>/comment")
@login_required
def add_comment_form(video_id: str):
    """Plain-form path. Video responses work here too — an ordinary multipart
    upload, no JavaScript involved."""
    user = current_user()
    body = (request.form.get("body") or "").strip()

    response_video_id, response_error = _attach_response(user)
    if response_error:
        flash(response_error, "error")
        return redirect(url_for("watch.watch", video_id=video_id) + "#comments")

    if body or response_video_id:
        if response_video_id and not videos_repo.get(response_video_id):
            _load_video(response_video_id)
        comment_id = comments_repo.add(
            video_id, user, body, response_video_id=response_video_id
        )
        if response_video_id:
            _notify_video_owner(video_id, user, comment_id)

    return redirect(url_for("watch.watch", video_id=video_id) + "#comments")


@bp.post("/watch/<path:video_id>/like")
@login_required
def like_form(video_id: str):
    videos_repo.set_vote(current_user()["id"], video_id, 1)
    return redirect(url_for("watch.watch", video_id=video_id))


@bp.post("/watch/<path:video_id>/dislike")
@login_required
def dislike_form(video_id: str):
    videos_repo.set_vote(current_user()["id"], video_id, -1)
    return redirect(url_for("watch.watch", video_id=video_id))