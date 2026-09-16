"""Your library: playlists, Watch later, liked videos, and history."""
from __future__ import annotations

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

from ..repo import library as library_repo
from ..repo import videos as videos_repo
from ..security import current_user, login_required

bp = Blueprint("library", __name__)


@bp.route("/library")
@login_required
def index():
    user = current_user()
    playlists = library_repo.playlists_for(user["id"])
    history = library_repo.history(user["id"], limit=12)
    videos_repo.attach_user_state(history, user["id"])

    return render_template(
        "library.html",
        playlists=playlists,
        history=history,
        continue_watching=library_repo.continue_watching(user["id"], limit=8),
    )


@bp.route("/history")
@login_required
def history():
    user = current_user()
    term = (request.args.get("q") or "").strip()
    items = library_repo.history(user["id"], limit=200, term=term)
    videos_repo.attach_user_state(items, user["id"])
    return render_template("history.html", videos=items, term=term)


@bp.post("/history/clear")
@login_required
def clear_history():
    library_repo.clear_history(current_user()["id"])
    flash("Watch history cleared.", "success")
    return redirect(url_for("library.history"))


@bp.post("/api/history/<int:history_id>/delete")
@login_required
def delete_history_entry(history_id: int):
    library_repo.delete_history_entry(current_user()["id"], history_id)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Playlists
# --------------------------------------------------------------------------
@bp.route("/playlist/<slug>")
@login_required
def playlist(slug: str):
    user = current_user()
    record = library_repo.get_playlist(user["id"], slug)
    if not record:
        abort(404)

    videos = library_repo.playlist_videos(record["id"])
    videos_repo.attach_user_state(videos, user["id"])

    return render_template(
        "playlist.html",
        playlist=record,
        videos=videos,
        total_duration=sum(v.get("duration") or 0 for v in videos),
    )


@bp.post("/playlists/create")
@login_required
def create_playlist():
    user = current_user()
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Give the playlist a name.", "error")
        return redirect(url_for("library.index"))

    record = library_repo.create_playlist(
        user["id"],
        title,
        request.form.get("description", ""),
        request.form.get("visibility", "private"),
    )
    return redirect(url_for("library.playlist", slug=record["slug"]))


@bp.post("/playlist/<slug>/update")
@login_required
def update_playlist(slug: str):
    user = current_user()
    library_repo.update_playlist(
        user["id"],
        slug,
        request.form.get("title", ""),
        request.form.get("description", ""),
        request.form.get("visibility", "private"),
    )
    flash("Playlist updated.", "success")
    return redirect(url_for("library.playlist", slug=slug))


@bp.post("/playlist/<slug>/delete")
@login_required
def delete_playlist(slug: str):
    library_repo.delete_playlist(current_user()["id"], slug)
    flash("Playlist deleted.", "success")
    return redirect(url_for("library.index"))


@bp.post("/api/playlists/add")
@login_required
def add_to_playlist():
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    video_id = payload.get("video_id")
    playlist_id = payload.get("playlist_id")

    if not video_id or not playlist_id:
        return jsonify({"error": "Need a video and a playlist."}), 400

    record = library_repo.get_playlist_by_id(int(playlist_id))
    if not record or record["user_id"] != user["id"]:
        return jsonify({"error": "That isn't your playlist."}), 403

    added = library_repo.add_to_playlist(record["id"], video_id)
    if not added:
        library_repo.remove_from_playlist(record["id"], video_id)

    return jsonify({"ok": True, "added": added, "playlist": record["title"]})


@bp.post("/api/playlists/create")
@login_required
def create_playlist_json():
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Give the playlist a name."}), 400

    record = library_repo.create_playlist(user["id"], title)
    video_id = payload.get("video_id")
    if video_id:
        library_repo.add_to_playlist(record["id"], video_id)

    return jsonify({"ok": True, "playlist": record})


@bp.post("/api/playlists/remove")
@login_required
def remove_from_playlist():
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    record = library_repo.get_playlist_by_id(int(payload.get("playlist_id", 0)))
    if not record or record["user_id"] != user["id"]:
        return jsonify({"error": "That isn't your playlist."}), 403

    library_repo.remove_from_playlist(record["id"], payload.get("video_id"))
    return jsonify({"ok": True})


@bp.post("/api/playlists/reorder")
@login_required
def reorder_playlist():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    record = library_repo.get_playlist_by_id(int(payload.get("playlist_id", 0)))
    if not record or record["user_id"] != user["id"]:
        return jsonify({"error": "That isn't your playlist."}), 403

    library_repo.reorder_playlist(record["id"], payload.get("video_ids") or [])
    return jsonify({"ok": True})


@bp.post("/api/watch-later")
@login_required
def toggle_watch_later():
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    video_id = payload.get("video_id")
    if not video_id:
        return jsonify({"error": "Which video?"}), 400
    saved = library_repo.toggle_watch_later(user["id"], video_id)
    return jsonify({"ok": True, "saved": saved})
