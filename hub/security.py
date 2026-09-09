"""
Sign-in, sessions, and request protection.

The original build trusted a cookie containing a plain username, which meant
anyone could become anyone by editing one value in their browser. This replaces
that with random session tokens stored (hashed) server-side, plus CSRF tokens
on every state-changing request.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import (
    current_app,
    g,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db

SESSION_KEY = "sid"
CSRF_KEY = "csrf"

# In-process login throttle: {ip: [timestamps]}. Plenty for a home server;
# swap for Redis if this ever runs somewhere busier.
_login_attempts: dict[str, list[float]] = {}


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return generate_password_hash(password, method="pbkdf2:sha256:600000")


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    """Return a human-readable reason the password won't do, or None."""
    if len(password) < 8:
        return "Passwords need at least 8 characters."
    if password.lower() in {"password", "12345678", "letmein1", "qwerty12"}:
        return "That password is one of the first anyone would try. Pick another."
    return None


def username_problem(username: str) -> str | None:
    if not 3 <= len(username) <= 32:
        return "Usernames are 3 to 32 characters long."
    if not all(c.isalnum() or c in "_-." for c in username):
        return "Usernames can use letters, numbers, and _ - . only."
    return None


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(
        days=current_app.config["SESSION_DAYS"]
    )

    # Honour the opt-out rather than writing the columns and hoping nobody
    # looks. With it off, these stay empty for the life of the session.
    if current_app.config.get("STORE_SESSION_IP", True):
        user_agent = (request.user_agent.string or "")[:300]
        ip = request.remote_addr or ""
    else:
        user_agent, ip = "", ""

    db.execute(
        """
        INSERT INTO sessions (token_hash, user_id, expires_at, user_agent, ip)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            _hash_token(token),
            user_id,
            expires.strftime("%Y-%m-%d %H:%M:%S"),
            user_agent,
            ip,
        ),
    )
    session.clear()
    session[SESSION_KEY] = token
    session[CSRF_KEY] = secrets.token_urlsafe(32)
    session.permanent = True
    return token


def end_session() -> None:
    token = session.get(SESSION_KEY)
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
    session.clear()


def end_all_sessions(user_id: int) -> None:
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired_sessions() -> None:
    db.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")


def load_user() -> None:
    """before_request hook: resolve the signed-in account into flask.g."""
    g.user = None
    token = session.get(SESSION_KEY)
    if not token:
        return

    row = db.query_one(
        """
        SELECT u.id, u.username, u.display_name, u.avatar_hue, u.is_admin, u.bio
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ? AND s.expires_at > datetime('now')
        """,
        (_hash_token(token),),
    )
    if row is None:
        session.clear()
        return

    g.user = dict(row)
    g.user["display_name"] = g.user["display_name"] or g.user["username"]


def current_user() -> dict | None:
    return getattr(g, "user", None)


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def csrf_token() -> str:
    if CSRF_KEY not in session:
        session[CSRF_KEY] = secrets.token_urlsafe(32)
    return session[CSRF_KEY]


def check_csrf() -> None:
    """before_request hook. Aborts unsafe requests without a matching token."""
    if request.method in SAFE_METHODS:
        return
    if getattr(current_app.view_functions.get(request.endpoint), "_csrf_exempt", False):
        return

    sent = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
        or (request.get_json(silent=True) or {}).get("csrf_token")
        or ""
    )
    expected = session.get(CSRF_KEY, "")
    if not expected or not hmac.compare_digest(str(sent), str(expected)):
        from flask import abort

        abort(400, description="That form expired. Reload the page and try again.")


def csrf_exempt(view):
    """Mark a view as not needing a CSRF token (device endpoints, webhooks)."""
    view._csrf_exempt = True
    return view


# --------------------------------------------------------------------------
# Decorators
# --------------------------------------------------------------------------
def wants_json() -> bool:
    return (
        request.path.startswith("/api/")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
    )


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            if wants_json():
                return jsonify({"error": "Sign in to continue."}), 401
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user().get("is_admin"):
            if wants_json():
                return jsonify({"error": "Admins only."}), 403
            from flask import abort

            abort(403)
        return view(*args, **kwargs)

    return wrapped


# --------------------------------------------------------------------------
# Login throttle
# --------------------------------------------------------------------------
def login_throttled(ip: str) -> bool:
    limit = current_app.config["LOGIN_RATE_LIMIT"]
    window = 300.0
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < window]
    _login_attempts[ip] = attempts
    return len(attempts) >= limit


def record_login_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


def clear_login_attempts(ip: str) -> None:
    _login_attempts.pop(ip, None)
