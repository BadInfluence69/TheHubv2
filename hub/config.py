"""
Configuration for The Hub.

Everything is read from environment variables (or a .env file next to the
project root). Nothing sensitive is hard-coded here, so this file is safe to
commit to source control.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser() if raw else default


def _resolve_binary(env_name: str, binary: str) -> str:
    """
    Find an external binary. Priority:
      1. explicit path in the environment
      2. a copy sitting next to the project (yt-dlp.exe, ffmpeg.exe, ...)
      3. bare name, letting the OS resolve it from PATH
    """
    explicit = os.getenv(env_name)
    if explicit and Path(explicit).exists():
        return explicit

    suffix = ".exe" if os.name == "nt" else ""
    local = BASE_DIR / f"{binary}{suffix}"
    if local.exists():
        return str(local)

    return binary


def _default_media_library() -> dict:
    """
    The local media library is a plain mapping of {folder label: path}.
    Override it entirely with MEDIA_LIBRARY as a JSON object, e.g.

        MEDIA_LIBRARY={"Movies": "E:/Media/Movies", "TV": "E:/Media/TV"}

    Any number of folders can be listed; each becomes its own shelf in the
    library UI and its own row in the TV/Roku feed.
    """
    raw = os.getenv("MEDIA_LIBRARY")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            pass

    base = os.getenv("MEDIA_ROOT")
    if not base:
        return {}
    return {
        "Movies": str(Path(base) / "Movies"),
        "TV": str(Path(base) / "TV"),
    }


def _metrics_path() -> str:
    """
    Normalise METRICS_PATH into a usable Flask url_prefix.

    Accepts "metrics", "/metrics", or "/metrics/" and returns "/metrics".
    An empty or "/" value would mount the dashboard over the home page, so
    that falls back to the default instead.
    """
    raw = (os.getenv("METRICS_PATH") or "").strip().strip("/")
    if not raw:
        return "/metrics"
    # Keep it to characters that survive a URL without escaping.
    safe = "".join(c for c in raw if c.isalnum() or c in "-_/")
    return f"/{safe}" if safe else "/metrics"


class Config:
    # ---- Core Flask ----------------------------------------------------
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
    DEBUG = _bool("DEBUG", False)
    TESTING = False

    # Sessions are stored server-side; this only controls the cookie itself.
    SESSION_COOKIE_NAME = "hub_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    SESSION_DAYS = _int("SESSION_DAYS", 30)

    MAX_CONTENT_LENGTH = _int("MAX_UPLOAD_MB", 4096) * 1024 * 1024

    # Record the IP address and browser user agent alongside each session.
    # Useful for spotting a session you don't recognise; also the most
    # identifying thing this app keeps. Set STORE_SESSION_IP=0 to stop
    # collecting it — the privacy page tells people this switch exists, so it
    # has to keep working.
    STORE_SESSION_IP = _bool("STORE_SESSION_IP", True)

    # ---- Networking ----------------------------------------------------
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = _int("PORT", 5002)
    # Address other devices on your network (Roku, phone, TV) use to reach
    # this server. Falls back to auto-detection at startup.
    PUBLIC_HOST = os.getenv("PUBLIC_HOST", "")

    # ---- Storage -------------------------------------------------------
    DB_PATH = _path("DB_PATH", BASE_DIR / "data" / "hub.db")
    UPLOAD_DIR = _path("UPLOAD_DIR", BASE_DIR / "data" / "uploads")
    CACHE_DIR = _path("CACHE_DIR", BASE_DIR / "data" / "cache")
    HLS_DIR = _path("HLS_DIR", BASE_DIR / "data" / "hls")

    # ---- External tools ------------------------------------------------
    YTDLP_PATH = _resolve_binary("YTDLP_PATH", "yt-dlp")
    FFMPEG_PATH = _resolve_binary("FFMPEG_PATH", "ffmpeg")
    FFPROBE_PATH = _resolve_binary("FFPROBE_PATH", "ffprobe")
    COOKIES_FILE = _path("COOKIES_FILE", BASE_DIR / "cookies.txt")

    # ---- YouTube -------------------------------------------------------
    # Read-only search. Optional: without it the app scrapes public results.
    YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

    # Uploading requires OAuth, not an API key. Download the client secret
    # JSON from Google Cloud Console and point at it here.
    GOOGLE_CLIENT_SECRETS = _path(
        "GOOGLE_CLIENT_SECRETS", BASE_DIR / "client_secret.json"
    )
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "")
    # Allows OAuth over plain http:// for LAN use. Google only permits this
    # for localhost redirect URIs.
    OAUTH_INSECURE_TRANSPORT = _bool("OAUTH_INSECURE_TRANSPORT", True)

    # ---- Tubi ----------------------------------------------------------
    # Tubi publishes no search API, so search reads their JSON endpoints and
    # then their own search page. If both stop working, this adds a headless
    # browser as a third attempt. Off by default: it is slow and needs
    #     pip install playwright && playwright install chromium
    TUBI_USE_PLAYWRIGHT = _bool("TUBI_USE_PLAYWRIGHT", False)

    # ---- Recommendations -----------------------------------------------
    # How long an interaction takes to count half as much. Lower reacts to a
    # change of taste faster; higher keeps long-standing interests alive.
    RECOMMEND_HALF_LIFE_DAYS = _float("RECOMMEND_HALF_LIFE_DAYS", 14.0)

    # Search phrases per feed build. search.list costs 100 quota units against
    # a default 10,000/day, so four is roughly 40 feed builds a day before the
    # 5-minute result cache is taken into account. Raise it only if you have
    # asked Google for more quota.
    RECOMMEND_QUERIES = _int("RECOMMEND_QUERIES", 4)

    # Ceiling on how much of one feed a single channel or topic may occupy.
    RECOMMEND_MAX_SHARE = _float("RECOMMEND_MAX_SHARE", 0.30)

    # How long the same search phrases are reused. Keeps the feed stable
    # between page loads instead of reshuffling on every refresh, and lets the
    # search cache do its job.
    RECOMMEND_ROTATE_MINUTES = _int("RECOMMEND_ROTATE_MINUTES", 30)

    # ---- Private metrics dashboard --------------------------------------
    # Operator-only engagement figures. Admin accounts get in; everyone else
    # gets a 404, so the page is invisible rather than merely forbidden.
    METRICS_ENABLED = _bool("METRICS_ENABLED", True)

    # Where the dashboard is mounted. Changing this to something unguessable
    # keeps the URL out of logs and casual poking — but the permission check is
    # what actually protects it, so don't rely on the path alone.
    METRICS_PATH = _metrics_path()

    # Off by default: no navigation entry is rendered at all, and the dashboard
    # is reached by typing its URL. Set to 1 to show a link in the sidebar,
    # which is still only ever drawn for admin accounts.
    METRICS_SHOW_LINK = _bool("METRICS_SHOW_LINK", False)

    # ---- Behaviour -----------------------------------------------------
    MEDIA_LIBRARY = _default_media_library()
    STREAM_CACHE_TTL = _int("STREAM_CACHE_TTL", 300)
    SEARCH_RESULTS = _int("SEARCH_RESULTS", 40)
    SPONSORBLOCK = _bool("SPONSORBLOCK", True)
    # Registration is open by default so you can make your first account.
    # Turn it off once everyone who needs an account has one.
    ALLOW_REGISTRATION = _bool("ALLOW_REGISTRATION", True)
    LOGIN_RATE_LIMIT = _int("LOGIN_RATE_LIMIT", 10)

    @classmethod
    def ensure_dirs(cls) -> None:
        for directory in (
            cls.DB_PATH.parent,
            cls.UPLOAD_DIR,
            cls.CACHE_DIR,
            cls.HLS_DIR,
        ):
            directory.mkdir(parents=True, exist_ok=True)


class TestConfig(Config):
    TESTING = True
    DEBUG = True
    SECRET_KEY = "test-secret-key"
    ALLOW_REGISTRATION = True
