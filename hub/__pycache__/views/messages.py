"""
Direct messages.

Every route in here handles ciphertext. There is no endpoint that accepts or
returns a message body, because the server has no way to produce one. The
browser encrypts before it posts and decrypts after it fetches; this module
routes opaque blobs and enforces who is allowed to see which blob.
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

from ..repo import keys as keys_repo
from ..repo import messages as messages_repo
from ..repo import users as users_repo
from ..security import current_user, login_required

bp = Blueprint("messages", __name__, url_prefix="/messages")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@bp.route("/")
@login_required
def inbox():
    user = current_user()
    return render_template(
        "messages/inbox.html",
        conversations=messages_repo.list_for(user["id"]),
        my_keys=keys_repo.get(user["id"]),
        blocked=messages_repo.blocked_list(user["id"]),
    )


@bp.route("/with/<username>")
@login_required
def open_thread(username: str):
    """Start or resume a conversation by name — the link used from profiles."""
    user = current_user()
    other = users_repo.by_username(username)
    if not other:
        abort(404)
    if other["id"] == user["id"]:
        flash("You can't message yourself.", "error")
        return redirect(url_for("messages.inbox"))

    conversation_id = messages_repo.open_with(user["id"], other["id"])
    return redirect(url_for("messages.thread", conversation_id=conversation_id))


@bp.route("/<int:conversation_id>")
@login_required
def thread(conversation_id: int):
    user = current_user()
    conversation = messages_repo.get(conversation_id)
    if not conversation or not messages_repo.is_participant(conversation_id, user["id"]):
        abort(404)

    other_id = messages_repo.other_party(conversation, user["id"])
    other = users_repo.by_id(other_id)
    if not other:
        abort(404)

    messages = messages_repo.thread(conversation_id)
    messages_repo.mark_read(conversation_id, user["id"])

    return render_template(
        "messages/thread.html",
        conversation=conversation,
        conversation_id=conversation_id,
        other=dict(other),
        other_public_key=keys_repo.public_key(other_id),
        my_keys=keys_repo.get(user["id"]),
        messages=messages,
        is_blocked=other_id in messages_repo.blocked_ids(user["id"]),
        they_blocked_me=messages_repo.is_blocked(user["id"], other_id),
        conversations=messages_repo.list_for(user["id"]),
    )


@bp.route("/keys")
@login_required
def key_setup():
    """Where a passphrase is chosen, and where a lost one is dealt with."""
    user = current_user()
    return render_template(
        "messages/keys.html",
        my_keys=keys_repo.get(user["id"]),
        conversation_count=len(messages_repo.list_for(user["id"])),
    )


# --------------------------------------------------------------------------
# Key exchange
#
# The browser does all the cryptography. These endpoints only move blobs.
# --------------------------------------------------------------------------
@bp.route("/api/keys/me")
@login_required
def my_key_record():
    record = keys_repo.get(current_user()["id"])
    if not record:
        return jsonify({"exists": False})
    return jsonify(
        {
            "exists": True,
            "public_key": record["public_key"],
            "wrapped_private_key": record["wrapped_private_key"],
            "wrap_iv": record["wrap_iv"],
            "kdf_salt": record["kdf_salt"],
            "kdf_iterations": record["kdf_iterations"],
            "fingerprint": record["fingerprint"],
        }
    )


@bp.post("/api/keys")
@login_required
def store_keys():
    """
    Accept a key pair generated in the browser.

    Everything here except `public_key` is already ciphertext when it arrives.
    The server checks shape and size, stores it, and forgets about it.
    """
    user = current_user()
    payload = request.get_json(silent=True) or {}

    required = (
        "public_key", "wrapped_private_key", "wrap_iv", "kdf_salt", "kdf_iterations",
    )
    missing = [field for field in required if not payload.get(field)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400

    try:
        iterations = int(payload["kdf_iterations"])
    except (TypeError, ValueError):
        return jsonify({"error": "Bad KDF parameters."}), 400

    if iterations < 100_000:
        # A low iteration count would make the wrapped key cheap to attack
        # offline, which quietly undoes the guarantee on the privacy page.
        return jsonify({"error": "Key derivation is too weak to accept."}), 400

    if any(len(str(payload[field])) > 4000 for field in required[:4]):
        return jsonify({"error": "Key material is larger than expected."}), 400

    existing = keys_repo.get(user["id"])
    if existing and not payload.get("confirm_rotate"):
        return jsonify(
            {
                "error": "This account already has a key. Rotating it makes every "
                         "message sent before now permanently unreadable.",
                "needs_confirmation": True,
            }
        ), 409

    keys_repo.store(
        user_id=user["id"],
        public_key=str(payload["public_key"]),
        wrapped_private_key=str(payload["wrapped_private_key"]),
        wrap_iv=str(payload["wrap_iv"]),
        kdf_salt=str(payload["kdf_salt"]),
        kdf_iterations=iterations,
        fingerprint=str(payload.get("fingerprint", ""))[:120],
    )
    return jsonify({"ok": True})


@bp.route("/api/keys/<username>")
@login_required
def public_key_for(username: str):
    other = users_repo.by_username(username)
    if not other:
        return jsonify({"error": "No account by that name."}), 404
    record = keys_repo.get(other["id"])
    if not record:
        return jsonify({"exists": False})
    return jsonify(
        {
            "exists": True,
            "user_id": other["id"],
            "public_key": record["public_key"],
            "fingerprint": record["fingerprint"],
        }
    )


# --------------------------------------------------------------------------
# Sending and reading
# --------------------------------------------------------------------------
@bp.post("/api/<int:conversation_id>/send")
@login_required
def send(conversation_id: int):
    user = current_user()
    if not messages_repo.is_participant(conversation_id, user["id"]):
        abort(404)

    conversation = messages_repo.get(conversation_id)
    other_id = messages_repo.other_party(conversation, user["id"])

    if messages_repo.is_blocked(user["id"], other_id):
        # Deliberately vague. Telling someone they've been blocked invites
        # them to make a second account, which helps nobody.
        return jsonify({"error": "That message couldn't be delivered."}), 403

    payload = request.get_json(silent=True) or {}
    ciphertext = (payload.get("ciphertext") or "").strip()
    iv = (payload.get("iv") or "").strip()

    if not ciphertext or not iv:
        return jsonify({"error": "Nothing to send."}), 400
    if len(ciphertext) > messages_repo.MAX_CIPHERTEXT:
        return jsonify({"error": "That message is too long."}), 400

    message_id = messages_repo.send(
        conversation_id, user["id"], ciphertext, iv,
        alg=str(payload.get("alg") or "ECDH-P256/HKDF-SHA256/AES-256-GCM")[:80],
    )

    # The notification cannot quote the message — the server has never seen it.
    # That reads as a limitation and is really the feature working.
    users_repo.notify_once(
        other_id,
        f"{user.get('display_name') or user['username']} sent you a message",
        "Encrypted — open the thread to read it.",
        url=url_for("messages.thread", conversation_id=conversation_id),
        kind="message",
    )
    users_repo.trim_notifications(other_id)

    return jsonify({"ok": True, "id": message_id})


@bp.route("/api/<int:conversation_id>/messages")
@login_required
def fetch(conversation_id: int):
    """Polling endpoint. `after` for new mail, `before` for scrollback."""
    user = current_user()
    if not messages_repo.is_participant(conversation_id, user["id"]):
        abort(404)

    after = request.args.get("after", type=int)
    before = request.args.get("before", type=int)

    if after is not None:
        items = messages_repo.since(conversation_id, after)
        if items:
            messages_repo.mark_read(conversation_id, user["id"])
    else:
        items = messages_repo.thread(conversation_id, before_id=before)

    return jsonify({"messages": items, "me": user["id"]})


@bp.post("/api/<int:conversation_id>/read")
@login_required
def mark_read(conversation_id: int):
    user = current_user()
    if not messages_repo.is_participant(conversation_id, user["id"]):
        abort(404)
    count = messages_repo.mark_read(conversation_id, user["id"])
    return jsonify({"ok": True, "marked": count})


@bp.post("/api/message/<int:message_id>/delete")
@login_required
def unsend(message_id: int):
    if not messages_repo.delete_message(message_id, current_user()["id"]):
        return jsonify({"error": "You can only unsend your own messages."}), 403
    return jsonify({"ok": True})


@bp.post("/<int:conversation_id>/delete")
@login_required
def delete_thread(conversation_id: int):
    user = current_user()
    if not messages_repo.is_participant(conversation_id, user["id"]):
        abort(404)
    messages_repo.delete_conversation(conversation_id)
    flash("Conversation deleted for both people.", "success")
    return redirect(url_for("messages.inbox"))


# --------------------------------------------------------------------------
# Finding people, and not hearing from them
# --------------------------------------------------------------------------
@bp.route("/api/people")
@login_required
def people():
    user = current_user()
    found = messages_repo.find_people(
        request.args.get("q", ""), exclude_id=user["id"], limit=12
    )
    return jsonify({"people": found})


@bp.post("/api/start")
@login_required
def start():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    other_id = payload.get("user_id")

    try:
        other_id = int(other_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Pick someone to message."}), 400

    if not users_repo.by_id(other_id):
        return jsonify({"error": "No account with that id."}), 404

    try:
        conversation_id = messages_repo.open_with(user["id"], other_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(
        {
            "ok": True,
            "conversation_id": conversation_id,
            "url": url_for("messages.thread", conversation_id=conversation_id),
        }
    )


@bp.post("/api/block")
@login_required
def block():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    try:
        other_id = int(payload.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Who?"}), 400

    if payload.get("undo"):
        messages_repo.unblock(user["id"], other_id)
        return jsonify({"ok": True, "blocked": False})

    messages_repo.block(user["id"], other_id)
    return jsonify({"ok": True, "blocked": True})
