"""
Creator profiles: the editor, and the click counter behind support links.

The money model is explained at length in repo/creators.py. The short version:
fans pay creators directly on the creator's own payment provider, and this
server only ever stores the link.
"""
from __future__ import annotations

from urllib.parse import unquote

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

from ..repo import creators as creators_repo
from ..repo import subs as subs_repo
from ..repo import videos as videos_repo
from ..security import current_user, login_required

bp = Blueprint("creators", __name__, url_prefix="/creator")


def _require_edit(channel_name: str):
    user = current_user()
    if not creators_repo.can_edit(channel_name, user):
        abort(403)
    return user


# --------------------------------------------------------------------------
# Editor
# --------------------------------------------------------------------------
@bp.route("/")
@login_required
def index():
    """Every channel this account can set up, plus its own by default."""
    user = current_user()
    owned = creators_repo.get_for_user(user["id"])

    # A creator's own channel is named after their account, so offer it even
    # before it has been claimed — otherwise a new creator lands on an empty
    # page with no obvious way forward.
    own_name = user.get("display_name") or user["username"]
    if not any(
        profile["channel_key"] == creators_repo.key_for(own_name) for profile in owned
    ):
        owned.insert(
            0,
            {
                "channel_key": creators_repo.key_for(own_name),
                "channel_name": own_name,
                "is_published": 0,
                "unclaimed": True,
            },
        )

    for profile in owned:
        profile["summary"] = creators_repo.summary(profile["channel_name"])

    return render_template("creator/index.html", profiles=owned)


@bp.route("/<path:channel_name>", methods=["GET", "POST"])
@login_required
def edit(channel_name: str):
    name = unquote(channel_name)
    user = _require_edit(name)

    profile = creators_repo.get(name)
    if profile is None:
        profile = creators_repo.claim(name, user["id"])

    if request.method == "POST":
        creators_repo.update(
            name,
            tagline=(request.form.get("tagline") or "").strip()[:120],
            about=(request.form.get("about") or "").strip()[:1000],
            support_note=(request.form.get("support_note") or "").strip()[:600],
            is_published=1 if request.form.get("is_published") == "on" else 0,
        )
        flash("Profile saved.", "success")
        return redirect(url_for("creators.edit", channel_name=name))

    return render_template(
        "creator/edit.html",
        profile=creators_repo.get(name),
        channel_name=name,
        grouped=creators_repo.links_by_kind(name, active_only=False),
        kinds=creators_repo.KINDS,
        kind_labels=creators_repo.KIND_LABELS,
        summary=creators_repo.summary(name),
        fan_count=subs_repo.subscriber_count(name),
        video_count=len(videos_repo.by_channel(videos_repo.channel_key(name), limit=200)),
    )


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------
@bp.post("/<path:channel_name>/links/add")
@login_required
def add_link(channel_name: str):
    name = unquote(channel_name)
    _require_edit(name)

    _, error = creators_repo.add_link(
        name,
        kind=request.form.get("kind", "support"),
        label=request.form.get("label", ""),
        url=request.form.get("url", ""),
        detail=request.form.get("detail", ""),
    )
    flash(error or "Link added.", "error" if error else "success")
    return redirect(url_for("creators.edit", channel_name=name))


@bp.post("/<path:channel_name>/links/<int:link_id>/update")
@login_required
def update_link(channel_name: str, link_id: int):
    name = unquote(channel_name)
    _require_edit(name)

    link = creators_repo.get_link(link_id)
    if not link or link["channel_key"] != creators_repo.key_for(name):
        abort(404)

    error = creators_repo.update_link(
        link_id,
        label=request.form.get("label", link["label"]),
        url=request.form.get("url", link["url"]),
        detail=request.form.get("detail", link["detail"]),
        is_active=request.form.get("is_active") == "on",
    )
    flash(error or "Link updated.", "error" if error else "success")
    return redirect(url_for("creators.edit", channel_name=name))


@bp.post("/<path:channel_name>/links/<int:link_id>/delete")
@login_required
def delete_link(channel_name: str, link_id: int):
    name = unquote(channel_name)
    _require_edit(name)

    link = creators_repo.get_link(link_id)
    if not link or link["channel_key"] != creators_repo.key_for(name):
        abort(404)

    creators_repo.delete_link(link_id)
    flash("Link removed.", "success")
    return redirect(url_for("creators.edit", channel_name=name))


# --------------------------------------------------------------------------
# Click counter
# --------------------------------------------------------------------------
@bp.post("/links/<int:link_id>/click")
@login_required
def click(link_id: int):
    """
    Fired by a beacon when a fan opens a creator link.

    Deliberately not a redirect endpoint. A /go/<id> route that sent visitors
    onward would be a URL on this domain that forwards anywhere a creator
    typed — which is the shape of an open redirect, and a gift to anyone
    wanting to borrow this server's name for a phishing link. The anchor
    points straight at the destination instead, and this only counts.
    """
    creators_repo.record_click(link_id)
    return jsonify({"ok": True})
