"""
Public chat rooms.

Membership is the whole access rule: every route is behind login_required, so
a room is readable by accounts on this server and by nobody else. There is no
anonymous view, no share link, no public transcript.

Room posts are plain text in the database. That is a real difference from
direct messages and it is not papered over anywhere in the UI.
"""
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

from ..repo import chat as chat_repo
from ..repo import messages as messages_repo
from ..repo import users as users_repo
from ..security import current_user, login_required

bp = Blueprint("chat", __name__, url_prefix="/chat")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@bp.route("/")
@login_required
def index():
    user = current_user()
    rooms = chat_repo.rooms(user["id"])
    return render_template("chat/index.html", rooms=rooms)


@bp.route("/<slug>")
@login_required
def room(slug: str):
    user = current_user()
    record = chat_repo.by_slug(slug)
    if not record:
        abort(404)

    hidden = messages_repo.blocked_ids(user["id"])
    posts = chat_repo.recent(record["id"], limit=100, hide_from=hidden)

    if posts:
        chat_repo.mark_seen(user["id"], record["id"], posts[-1]["id"])

    return render_template(
        "chat/room.html",
        room=record,
        posts=posts,
        rooms=chat_repo.rooms(user["id"]),
        members=chat_repo.active_posters(record["id"]),
        can_manage=bool(user.get("is_admin")) or record["created_by"] == user["id"],
    )


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------
@bp.post("/api/<int:room_id>/post")
@login_required
def post(room_id: int):
    user = current_user()
    record = _room_or_404(room_id)

    if record["is_locked"] and not user.get("is_admin"):
        return jsonify({"error": "This room is read-only."}), 403

    payload = request.get_json(silent=True) or request.form
    body = (payload.get("body") or "").strip()

    if not body:
        return jsonify({"error": "Write something first."}), 400
    if len(body) > chat_repo.MAX_LENGTH:
        return jsonify(
            {"error": f"Room posts cap at {chat_repo.MAX_LENGTH} characters."}
        ), 400

    message_id = chat_repo.post(record["id"], user, body)
    chat_repo.mark_seen(user["id"], record["id"], message_id)
    _notify_mentions(record, user, body, message_id)

    return jsonify({"ok": True, "message": chat_repo.get_post(message_id)})


def _notify_mentions(record: dict, user: dict, body: str, message_id: int) -> None:
    """@handle pings the named account, unless they've blocked the sender."""
    for target in chat_repo.mentioned_users(body, exclude_id=user["id"]):
        if messages_repo.is_blocked(user["id"], target["id"]):
            continue
        users_repo.notify(
            target["id"],
            f"{user.get('display_name') or user['username']} mentioned you in "
            f"{record['name']}",
            body[:140],
            url=url_for("chat.room", slug=record["slug"]) + f"#post-{message_id}",
            kind="mention",
        )
        users_repo.trim_notifications(target["id"])


@bp.route("/api/<int:room_id>/messages")
@login_required
def fetch(room_id: int):
    user = current_user()
    record = _room_or_404(room_id)
    hidden = messages_repo.blocked_ids(user["id"])

    after = request.args.get("after", type=int)
    before = request.args.get("before", type=int)

    if after is not None:
        posts = chat_repo.since(record["id"], after, hide_from=hidden)
    else:
        posts = chat_repo.recent(record["id"], before_id=before, hide_from=hidden)

    if posts:
        chat_repo.mark_seen(user["id"], record["id"], max(p["id"] for p in posts))

    return jsonify({"messages": posts, "me": user["id"]})


@bp.post("/api/post/<int:message_id>/delete")
@login_required
def delete_post(message_id: int):
    user = current_user()
    if not chat_repo.delete_post(message_id, user["id"], bool(user.get("is_admin"))):
        return jsonify({"error": "You can only delete your own posts."}), 403
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Managing rooms
# --------------------------------------------------------------------------
@bp.post("/create")
@login_required
def create():
    user = current_user()
    name = (request.form.get("name") or "").strip()
    topic = (request.form.get("topic") or "").strip()

    if len(name) < 3:
        flash("Give the room a name of at least three characters.", "error")
        return redirect(url_for("chat.index"))

    slug = chat_repo.create_room(name, topic, user["id"])
    flash(f"Room “{name}” is open.", "success")
    return redirect(url_for("chat.room", slug=slug))


@bp.post("/<slug>/update")
@login_required
def update(slug: str):
    user = current_user()
    record = chat_repo.by_slug(slug)
    if not record:
        abort(404)
    if not user.get("is_admin") and record["created_by"] != user["id"]:
        abort(403)

    chat_repo.update_room(
        record["id"],
        request.form.get("name") or record["name"],
        request.form.get("topic") or "",
    )
    if user.get("is_admin"):
        chat_repo.set_locked(record["id"], request.form.get("is_locked") == "on")

    flash("Room updated.", "success")
    return redirect(url_for("chat.room", slug=slug))


@bp.post("/<slug>/delete")
@login_required
def delete(slug: str):
    user = current_user()
    record = chat_repo.by_slug(slug)
    if not record:
        abort(404)
    if not user.get("is_admin") and record["created_by"] != user["id"]:
        abort(403)

    if not chat_repo.delete_room(record["id"]):
        flash("Built-in rooms can't be deleted.", "error")
        return redirect(url_for("chat.room", slug=slug))

    flash("Room deleted.", "success")
    return redirect(url_for("chat.index"))


# --------------------------------------------------------------------------
def _room_or_404(room_id: int) -> dict:
    record = next((r for r in chat_repo.rooms() if r["id"] == room_id), None)
    if not record:
        abort(404)
    return record
