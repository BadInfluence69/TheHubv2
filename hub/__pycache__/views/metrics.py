"""
Private metrics dashboard. Operator only.

Three things keep this off the public surface, in descending order of how much
they actually matter:

1. **Authorisation.** Every route is wrapped in `owner_only`, which admits
   admin accounts and nobody else. This is the part doing the real work.

2. **Non-disclosure.** Failures return 404, not 403. A 403 confirms the page
   exists and only says you're not allowed in, which tells a curious account
   exactly where to keep poking. A 404 is indistinguishable from a URL that was
   never routed, so from outside, the dashboard simply isn't there. The same
   answer goes to signed-out visitors, and there is no redirect to the sign-in
   page — a redirect would give the game away just as loudly.

3. **An unguessable path.** METRICS_PATH in .env moves the whole blueprint. On
   its own this would be security by obscurity and worth nothing; layered on
   top of a real permission check, it keeps the endpoint out of logs, browser
   history, and casual URL guessing. Treat it as a convenience, never as the
   protection.

No link to any of this is rendered anywhere in the app unless METRICS_SHOW_LINK
is switched on, and even then only for admins.
"""
from __future__ import annotations

import functools

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from ..repo import metrics as metrics_repo
from ..security import current_user

# url_prefix is filled in at registration time from config, so the path can be
# changed without touching code.
bp = Blueprint("metrics", __name__)


def owner_only(view):
    """
    Admit admins; give everyone else a plain 404.

    Deliberately not built on login_required: that redirects anonymous visitors
    to the sign-in page, which would advertise that something worth signing in
    for lives at this URL.
    """

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or not user.get("is_admin"):
            abort(404)
        return view(*args, **kwargs)

    return wrapped


def _window() -> int:
    """Chart window in days, clamped to something a SQLite roll-up can serve."""
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    return max(7, min(days, 365))


@bp.route("/")
@owner_only
def dashboard():
    days = _window()
    data = metrics_repo.dashboard(current_app.config, days=days)
    return render_template(
        "metrics/dashboard.html",
        m=data,
        days=days,
        metrics_path=current_app.config.get("METRICS_PATH", "/metrics"),
    )


@bp.route("/data.json")
@owner_only
def data_json():
    """The same figures as JSON, for piping somewhere else."""
    return jsonify(metrics_repo.dashboard(current_app.config, days=_window()))


@bp.route("/health.json")
@owner_only
def health_json():
    """A small endpoint worth polling if you want an uptime check with teeth."""
    return jsonify(
        {
            "ok": True,
            "database": metrics_repo.database_health(),
            "storage": metrics_repo.storage_stats(current_app.config),
            "active_sessions": metrics_repo.overview()["active_sessions"],
        }
    )
