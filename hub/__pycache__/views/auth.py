"""Signing in, signing up, and account settings."""
from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import security
from ..repo import comments as comments_repo
from ..repo import library as library_repo
from ..repo import subs as subs_repo
from ..repo import users as users_repo

bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str:
    """Only follow redirects that stay on this site."""
    if not target:
        return url_for("main.home")
    if target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("main.home")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if security.current_user():
        return redirect(url_for("main.home"))

    first_run = users_repo.count() == 0
    next_url = request.args.get("next") or request.form.get("next")

    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if security.login_throttled(ip):
            flash("Too many attempts. Wait five minutes and try again.", "error")
            return render_template("auth.html", mode="login", first_run=first_run,
                                   next_url=next_url), 429

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = users_repo.by_username(username)
        if user and security.verify_password(user["password_hash"], password):
            security.clear_login_attempts(ip)
            security.start_session(user["id"])
            library_repo.ensure_system_playlists(user["id"])
            security.purge_expired_sessions()
            return redirect(_safe_next(next_url))

        security.record_login_attempt(ip)
        flash("That username and password don't match.", "error")

    return render_template(
        "auth.html", mode="login", first_run=first_run, next_url=next_url
    )


@bp.route("/register", methods=["GET", "POST"])
def register():
    if security.current_user():
        return redirect(url_for("main.home"))

    first_run = users_repo.count() == 0
    if not current_app.config["ALLOW_REGISTRATION"] and not first_run:
        flash("New accounts are turned off on this server.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        problem = (
            security.username_problem(username)
            or security.password_problem(password)
            or ("Those passwords don't match." if password != confirm else None)
            or ("That username is taken." if users_repo.by_username(username) else None)
        )
        if problem:
            flash(problem, "error")
            return render_template("auth.html", mode="register", first_run=first_run)

        # The first account on a fresh install runs the place.
        user_id = users_repo.create(
            username, security.hash_password(password), is_admin=first_run
        )
        security.start_session(user_id)
        users_repo.notify(
            user_id,
            "Welcome to The Hub",
            "Connect a Google account in Studio when you want to publish to YouTube.",
            url=url_for("studio.dashboard"),
        )
        return redirect(url_for("main.home"))

    return render_template("auth.html", mode="register", first_run=first_run)


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    # A GET here is convenient from a bookmark and can't do damage, since
    # ending a session is not a destructive action.
    security.end_session()
    return redirect(url_for("auth.login"))


@bp.route("/settings", methods=["GET"])
@security.login_required
def settings():
    user = security.current_user()
    return render_template(
        "settings.html",
        settings=users_repo.get_settings(user["id"]),
        subscription_count=subs_repo.count(user["id"]),
        comment_count=len(comments_repo.recent_by_user(user["id"], limit=999)),
    )


@bp.post("/settings/profile")
@security.login_required
def update_profile():
    user = security.current_user()
    users_repo.update_profile(
        user["id"],
        request.form.get("display_name", ""),
        request.form.get("bio", ""),
    )
    flash("Profile updated.", "success")
    return redirect(url_for("auth.settings"))


@bp.post("/settings/preferences")
@security.login_required
def update_preferences():
    user = security.current_user()
    for key in ("theme", "autoplay", "pause_history", "default_quality", "restricted_mode"):
        if key in request.form:
            users_repo.set_setting(user["id"], key, request.form.get(key, ""))
    flash("Preferences saved.", "success")
    return redirect(url_for("auth.settings"))


@bp.post("/settings/password")
@security.login_required
def change_password():
    user = security.current_user()
    row = users_repo.by_id(user["id"])

    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""

    if not security.verify_password(row["password_hash"], current):
        flash("Your current password isn't right.", "error")
        return redirect(url_for("auth.settings"))

    problem = security.password_problem(new) or (
        "Those passwords don't match." if new != confirm else None
    )
    if problem:
        flash(problem, "error")
        return redirect(url_for("auth.settings"))

    users_repo.set_password(user["id"], security.hash_password(new))
    security.end_all_sessions(user["id"])
    security.start_session(user["id"])
    flash("Password changed. Other devices have been signed out.", "success")
    return redirect(url_for("auth.settings"))


@bp.post("/settings/sign-out-everywhere")
@security.login_required
def sign_out_everywhere():
    user = security.current_user()
    security.end_all_sessions(user["id"])
    return redirect(url_for("auth.login"))
