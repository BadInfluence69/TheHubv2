"""Channel pages and the subscription list.

Subscriptions are local. Following a channel here changes your feed on this
server and nothing else — your YouTube account is untouched.
"""
from __future__ import annotations

from urllib.parse import unquote

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from ..repo import creators as creators_repo
from ..repo import subs as subs_repo
from ..repo import videos as videos_repo
from ..security import current_user, login_required
from ..services import youtube_search

bp = Blueprint("channels", __name__)


@bp.route("/subscriptions")
@login_required
def feed():
    user = current_user()
    videos = subs_repo.feed_videos(user["id"], limit=60)

    # If the local catalogue is thin, pull fresh uploads for a few channels.
    if len(videos) < 12:
        for subscription in subs_repo.list_for(user["id"])[:4]:
            found = youtube_search.channel_uploads(subscription["channel_name"], limit=12)
            if found:
                videos_repo.upsert_many(found)
        videos = subs_repo.feed_videos(user["id"], limit=60)

    videos_repo.attach_user_state(videos, user["id"])
    return render_template(
        "subscriptions.html",
        videos=videos,
        subscriptions=subs_repo.list_for(user["id"]),
    )


@bp.route("/subscriptions/manage")
@login_required
def manage():
    user = current_user()
    return render_template(
        "manage_subscriptions.html",
        subscriptions=subs_repo.list_for(user["id"]),
        suggestions=[
            channel
            for channel in videos_repo.channels(limit=40)
            if not subs_repo.is_subscribed(user["id"], channel["channel_name"])
        ][:20],
    )


@bp.route("/channel/<path:channel_name>")
@login_required
def channel(channel_name: str):
    user = current_user()
    name = unquote(channel_name)
    key = videos_repo.channel_key(name)

    videos = videos_repo.by_channel(key, limit=60)
    if len(videos) < 6:
        found = youtube_search.channel_uploads(name, limit=24)
        if found:
            videos_repo.upsert_many(found)
            videos = videos_repo.by_channel(key, limit=60)

    videos_repo.attach_user_state(videos, user["id"])

    return render_template(
        "channel.html",
        channel_name=name,
        videos=videos,
        subscribed=subs_repo.is_subscribed(user["id"], name),
        subscriber_count=subs_repo.subscriber_count(name),
        subscription=subs_repo.get(user["id"], name),
        video_count=len(videos),
        creator=creators_repo.public_profile(name),
        can_edit_creator=creators_repo.can_edit(name, user),
    )


@bp.post("/api/subscriptions/toggle")
@login_required
def toggle():
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    channel_name = (payload.get("channel") or "").strip()
    if not channel_name:
        return jsonify({"error": "Which channel?"}), 400

    subscribed = subs_repo.toggle(
        user["id"], channel_name, payload.get("thumbnail", "")
    )
    return jsonify(
        {
            "subscribed": subscribed,
            "count": subs_repo.subscriber_count(channel_name),
            "total": subs_repo.count(user["id"]),
        }
    )


@bp.post("/api/subscriptions/notify")
@login_required
def set_notify():
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    channel_name = (payload.get("channel") or "").strip()
    notify = str(payload.get("notify", "1")) in ("1", "true", "True", "on")
    subs_repo.set_notify(user["id"], channel_name, notify)
    return jsonify({"ok": True, "notify": notify})


@bp.post("/subscriptions/add")
@login_required
def add_form():
    user = current_user()
    channel_name = (request.form.get("channel") or "").strip()
    if channel_name:
        subs_repo.subscribe(user["id"], channel_name)
    return redirect(url_for("channels.manage"))


@bp.post("/subscriptions/remove")
@login_required
def remove_form():
    user = current_user()
    channel_name = (request.form.get("channel") or "").strip()
    if channel_name:
        subs_repo.unsubscribe(user["id"], channel_name)
    return redirect(url_for("channels.manage"))
