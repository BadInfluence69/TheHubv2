"""Home, search, explore, and the notification bell."""
from __future__ import annotations

import logging

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from ..repo import library as library_repo
from ..repo import subs as subs_repo
from ..repo import users as users_repo
from ..repo import videos as videos_repo
from ..security import current_user, login_required
from ..services import local_media, recommend, tubi, youtube_search

bp = Blueprint("main", __name__)

log = logging.getLogger(__name__)


@bp.route("/")
@login_required
def home():
    user = current_user()
    videos = recommend.home_feed(user["id"], limit=48)
    videos_repo.attach_user_state(videos, user["id"])

    return render_template(
        "home.html",
        videos=videos,
        continue_watching=library_repo.continue_watching(user["id"], limit=8),
        subscription_count=subs_repo.count(user["id"]),
    )


@bp.route("/feed/why")
@login_required
def feed_why():
    """
    What the recommender currently thinks this account likes, and the search
    phrases it built from that. Everything shown here was derived from rows in
    this database — it is the whole input to the feed, not a summary of it.
    """
    return jsonify(recommend.explain(current_user()["id"]))


@bp.route("/explore")
@login_required
def explore():
    user = current_user()
    shelves = recommend.explore_shelves(user["id"])
    for shelf in shelves:
        videos_repo.attach_user_state(shelf["videos"], user["id"])
    return render_template("explore.html", shelves=shelves)


@bp.route("/trending")
@login_required
def trending():
    user = current_user()
    videos = videos_repo.trending(limit=60)
    videos_repo.attach_user_state(videos, user["id"])
    return render_template(
        "listing.html",
        heading="Trending on this server",
        subheading="Ranked by the likes, comments, and views recorded here — "
                   "not by anything YouTube reports.",
        videos=videos,
    )


@bp.route("/search")
@login_required
def search():
    user = current_user()
    term = (request.args.get("q") or "").strip()
    source = request.args.get("source", "all")

    if not term:
        return redirect(url_for("main.home"))

    settings = users_repo.get_settings(user["id"])
    if settings.get("pause_history") != "1":
        library_repo.record_search(user["id"], term)

    results: list[dict] = []

    if source in ("all", "local"):
        results += local_media.search(term)

    if source in ("all", "youtube"):
        found = youtube_search.search(term)
        if found:
            videos_repo.upsert_many(found)
        results += found

    # Tubi search reads undocumented endpoints, so it is the most likely of
    # these to break. Keep the reason it failed rather than letting a dead
    # backend look identical to a search with no matches.
    tubi_notice = ""
    if source in ("all", "tubi"):
        outcome = tubi.search_detailed(term)
        if outcome.results:
            videos_repo.upsert_many(outcome.results)
            results += outcome.results
            if outcome.drm_filtered:
                tubi_notice = (
                    f"{outcome.drm_filtered} Tubi "
                    f"{'title is' if outcome.drm_filtered == 1 else 'titles are'} "
                    "DRM-protected and can only be watched on Tubi itself."
                )
        elif not outcome.ok:
            tubi_notice = (
                "Tubi search isn't responding right now — the rest of these "
                "results are unaffected."
            )
            log.warning("Tubi search unavailable for %r: %s", term, outcome.reason)

    if source in ("all", "library"):
        results += videos_repo.search_catalogue(term, limit=30)

    # Fold in local like counts and drop duplicates.
    seen, merged = set(), []
    stored = videos_repo.get_many(
        [r.get("id") or r.get("video_id") for r in results if r.get("id") or r.get("video_id")]
    )
    for item in results:
        key = item.get("id") or item.get("video_id")
        if not key or key in seen:
            continue
        seen.add(key)
        record = stored.get(key)
        if record:
            item["likes"] = record["likes"]
            item["dislikes"] = record["dislikes"]
            item["comment_count"] = record["comment_count"]
        merged.append(item)

    videos_repo.attach_user_state(merged, user["id"])

    return render_template(
        "search.html",
        term=term,
        source=source,
        videos=merged,
        result_count=len(merged),
        tubi_notice=tubi_notice,
    )


@bp.route("/api/suggest")
@login_required
def suggest():
    """Search box autocomplete: your own past searches plus catalogue titles."""
    user = current_user()
    prefix = (request.args.get("q") or "").strip()

    recent = library_repo.recent_searches(user["id"], limit=6, prefix=prefix)
    titles = []
    if prefix:
        titles = [
            video["title"]
            for video in videos_repo.search_catalogue(prefix, limit=6)
        ]

    seen, suggestions = set(), []
    for value in recent + titles:
        low = value.lower()
        if low not in seen:
            seen.add(low)
            suggestions.append(value)

    return jsonify({"suggestions": suggestions[:10]})


@bp.post("/api/search-history/clear")
@login_required
def clear_search_history():
    library_repo.clear_searches(current_user()["id"])
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Notifications
#
# One inbox, everything in it, filterable by kind. Somebody with five hundred
# unread rows should be able to get to the twelve that are messages without
# scrolling past four hundred and eighty likes.
# --------------------------------------------------------------------------
@bp.route("/notifications")
@login_required
def notifications():
    user = current_user()
    kind = request.args.get("kind")
    kind = kind if kind in users_repo.KINDS else None
    unread_only = request.args.get("unread") == "1"

    items = users_repo.notifications(
        user["id"], limit=200, kind=kind, unread_only=unread_only
    )
    unread_by_kind = users_repo.unread_by_kind(user["id"])
    total_by_kind = users_repo.total_by_kind(user["id"])

    # Mark read only what's actually on screen, so filtering to one tag
    # doesn't quietly clear the others.
    users_repo.mark_read(user["id"], kind=kind)

    return render_template(
        "notifications.html",
        notifications=items,
        kinds=users_repo.KINDS,
        active_kind=kind,
        unread_only=unread_only,
        unread_by_kind=unread_by_kind,
        total_by_kind=total_by_kind,
        total=sum(total_by_kind.values()),
    )


@bp.route("/api/notifications")
@login_required
def notifications_json():
    user = current_user()
    return jsonify(
        {
            "unread": users_repo.unread_count(user["id"]),
            "by_kind": users_repo.unread_by_kind(user["id"]),
            "items": users_repo.notifications(user["id"], limit=12),
        }
    )


@bp.post("/api/notifications/read")
@login_required
def mark_notifications_read():
    payload = request.get_json(silent=True) or {}
    users_repo.mark_read(
        current_user()["id"], payload.get("id"), kind=payload.get("kind")
    )
    return jsonify({"ok": True})


@bp.post("/api/notifications/clear")
@login_required
def clear_notifications():
    payload = request.get_json(silent=True) or {}
    users_repo.clear_notifications(current_user()["id"], kind=payload.get("kind"))
    return jsonify({"ok": True})


@bp.route("/about")
def about():
    return render_template("about.html")


@bp.route("/privacy")
def privacy():
    """
    Public on purpose — a privacy page behind a login is no use to anyone
    deciding whether to make an account in the first place.
    """
    return render_template("privacy.html")
