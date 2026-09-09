"""
The Hub - a self-hosted front end for YouTube, Tubi, and your own media.

Create the app with create_app(); run it with run.py in development or a WSGI
server in production. See README.md.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from flask import Flask, g, jsonify, render_template, request

__version__ = "2.0.0"


def create_app(config_object=None) -> Flask:
    from .config import Config

    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_object or Config)

    _configure_logging(app)
    _configure_paths(app)
    _register_core(app)
    _register_blueprints(app)
    _register_errors(app)
    _register_context(app)

    return app


# --------------------------------------------------------------------------
def _configure_logging(app: Flask) -> None:
    level = logging.DEBUG if app.config.get("DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # These two are chatty and rarely useful.
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("werkzeug").setLevel(
        logging.DEBUG if app.config.get("DEBUG") else logging.WARNING
    )


def _configure_paths(app: Flask) -> None:
    from .config import Config

    Config.ensure_dirs()
    app.config["DB_PATH"] = str(app.config["DB_PATH"])
    app.config["UPLOAD_DIR"] = str(app.config["UPLOAD_DIR"])
    app.config["CACHE_DIR"] = str(app.config["CACHE_DIR"])
    app.config["HLS_DIR"] = str(app.config["HLS_DIR"])
    app.config["COOKIES_FILE"] = str(app.config["COOKIES_FILE"])
    app.config["GOOGLE_CLIENT_SECRETS"] = str(app.config["GOOGLE_CLIENT_SECRETS"])


def _register_core(app: Flask) -> None:
    from . import db, filters, security

    db.init_db(app.config["DB_PATH"])
    db.init_app(app)
    filters.register(app)

    app.permanent_session_lifetime = timedelta(days=app.config["SESSION_DAYS"])

    @app.before_request
    def _before():
        security.load_user()
        security.check_csrf()

    @app.after_request
    def _after(response):
        # Sensible defaults for a server exposed on a home network.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer-when-downgrade")
        if request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
        return response


def _register_blueprints(app: Flask) -> None:
    from .views import (
        api,
        auth,
        channels,
        chat,
        creators,
        feedback,
        library,
        main,
        media,
        messages,
        metrics,
        studio,
        watch,
        #roku,
    )

    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(watch.bp)
    app.register_blueprint(channels.bp)
    app.register_blueprint(creators.bp)
    app.register_blueprint(library.bp)
    app.register_blueprint(studio.bp)
    app.register_blueprint(media.bp)
    app.register_blueprint(feedback.bp)
    app.register_blueprint(messages.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(api.bp)

    # The private dashboard. Mounted last, at a path from .env, and skipped
    # entirely when disabled — a blueprint that was never registered can't be
    # reached by any request, however it's addressed.
    if app.config.get("METRICS_ENABLED", True):
        app.register_blueprint(
            metrics.bp, url_prefix=app.config.get("METRICS_PATH", "/metrics")
        )


def _register_errors(app: Flask) -> None:
    from .security import wants_json

    def render_error(code: int, title: str, message: str):
        if wants_json():
            return jsonify({"error": message}), code
        return render_template(
            "error.html", code=code, title=title, message=message
        ), code

    @app.errorhandler(400)
    def bad_request(exc):
        return render_error(
            400, "That didn't go through",
            getattr(exc, "description", "The request was malformed."),
        )

    @app.errorhandler(403)
    def forbidden(_exc):
        return render_error(403, "Not your account", "You don't have access to this.")

    @app.errorhandler(404)
    def not_found(_exc):
        return render_error(
            404, "Nothing here", "That page or video isn't in the library."
        )

    @app.errorhandler(413)
    def too_large(_exc):
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return render_error(
            413, "File too big", f"Uploads are capped at {limit} MB. Raise "
            "MAX_UPLOAD_MB in .env if you need more room."
        )

    @app.errorhandler(500)
    def server_error(exc):
        app.logger.exception("Unhandled error: %s", exc)
        return render_error(
            500, "Something broke", "The server hit an error. Check the console log."
        )


def _register_context(app: Flask) -> None:
    from .repo import chat as chat_repo
    from .repo import library as library_repo
    from .repo import messages as messages_repo
    from .repo import subs as subs_repo
    from .repo import users as users_repo
    from .security import csrf_token, current_user

    @app.context_processor
    def inject():
        user = current_user()
        context = {
            "current_user": user,
            "csrf_token": csrf_token,
            "app_version": __version__,
            "nav_playlists": [],
            "nav_subscriptions": [],
            "nav_rooms": [],
            "unread_notifications": 0,
            "unread_messages": 0,
            "unread_chat": 0,
            "user_settings": {},
            # Gates the sidebar entry for the private dashboard. False for
            # every ordinary account, and false for admins too unless
            # METRICS_SHOW_LINK is on — the default is a page with no link
            # pointing at it anywhere in the interface.
            "show_metrics_link": False,
        }
        if user:
            context["nav_playlists"] = library_repo.playlists_for(user["id"])
            context["nav_subscriptions"] = subs_repo.list_for(user["id"])[:12]
            context["unread_notifications"] = users_repo.unread_count(user["id"])
            context["user_settings"] = users_repo.get_settings(user["id"])
            context["unread_messages"] = messages_repo.unread_total(user["id"])
            context["nav_rooms"] = chat_repo.rooms(user["id"])[:6]
            context["unread_chat"] = sum(
                room["unread"] for room in context["nav_rooms"]
            )
            context["show_metrics_link"] = bool(
                user.get("is_admin")
                and app.config.get("METRICS_ENABLED", True)
                and app.config.get("METRICS_SHOW_LINK", False)
            )
        return context
