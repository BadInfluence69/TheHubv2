"""
Connecting a Google account so the Studio can publish to YouTube.

An API key can only read public data. Uploading a video means acting *as you*,
which requires OAuth. You do this once; the refresh token is kept (encrypted)
in the local database and reused from then on.

Scopes requested:
  youtube.upload    - publish new videos
  youtube.readonly  - read your channel and your own video list
  youtube.force-ssl - edit and delete videos you uploaded through this app

This app never posts comments, likes, or subscriptions to YouTube. Those all
stay in the local database, which is the whole point of the design.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, url_for
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from ..repo import studio as studio_repo

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


class OAuthNotConfigured(RuntimeError):
    """Raised when no client credentials have been supplied yet."""


# --------------------------------------------------------------------------
# Token encryption
# --------------------------------------------------------------------------
def _fernet() -> Fernet:
    """
    Derive an encryption key from SECRET_KEY. If SECRET_KEY changes, stored
    tokens become unreadable and the account simply shows as disconnected -
    which is why the .env file should pin SECRET_KEY to a fixed value.
    """
    secret = str(current_app.config["SECRET_KEY"]).encode("utf-8")
    digest = hashlib.sha256(b"hub-oauth-v1" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(payload: str) -> str:
    return _fernet().encrypt(payload.encode("utf-8")).decode("ascii")


def _decrypt(payload: str) -> str | None:
    try:
        return _fernet().decrypt(payload.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


# --------------------------------------------------------------------------
# Client configuration
# --------------------------------------------------------------------------
def is_configured() -> bool:
    config = current_app.config
    if config.get("GOOGLE_CLIENT_ID") and config.get("GOOGLE_CLIENT_SECRET"):
        return True
    secrets_file = config.get("GOOGLE_CLIENT_SECRETS")
    return bool(secrets_file and os.path.exists(secrets_file))


def _client_config(redirect_uri: str) -> dict:
    config = current_app.config

    if config.get("GOOGLE_CLIENT_ID") and config.get("GOOGLE_CLIENT_SECRET"):
        return {
            "web": {
                "client_id": config["GOOGLE_CLIENT_ID"],
                "client_secret": config["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }

    secrets_file = config.get("GOOGLE_CLIENT_SECRETS")
    if secrets_file and os.path.exists(secrets_file):
        with open(secrets_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        # Desktop-app credentials come under "installed"; normalise to "web"
        # so the browser redirect flow works either way.
        if "installed" in data and "web" not in data:
            data = {"web": data["installed"]}
        data["web"].setdefault("redirect_uris", []).append(redirect_uri)
        return data

    raise OAuthNotConfigured(
        "No Google credentials found. Add client_secret.json to the project "
        "folder, or set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env."
    )


def redirect_uri() -> str:
    configured = current_app.config.get("OAUTH_REDIRECT_URI")
    if configured:
        return configured
    return url_for("studio.oauth_callback", _external=True)


def _flow(state: str | None = None, code_verifier: str | None = None) -> Flow:
    uri = redirect_uri()
    if current_app.config.get("OAUTH_INSECURE_TRANSPORT"):
        # Google allows plain http only for localhost redirect URIs.
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = Flow.from_client_config(_client_config(uri), scopes=SCOPES, state=state)
    flow.redirect_uri = uri
    if code_verifier:
        # PKCE: the callback runs on a brand-new Flow object, so the verifier
        # generated back in authorization_url() has to be handed back in here.
        flow.code_verifier = code_verifier
    return flow


def authorization_url() -> tuple[str, str, str]:
    """
    Returns (url, state, code_verifier). Send the person to url; keep both
    state and code_verifier in the session until the callback comes back.

    google-auth-oauthlib 1.1+ turns on PKCE automatically, which means the
    authorization request carries a code_challenge and the token request must
    carry the matching code_verifier. Losing the verifier between the two
    requests is what produces "(invalid_grant) Missing code verifier".
    """
    flow = _flow()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # forces a refresh token on repeat connections
    )
    return url, state, flow.code_verifier


def finish_authorization(
    user_id: int,
    state: str,
    response_url: str,
    code_verifier: str | None = None,
) -> dict:
    """Exchange the callback for tokens and remember the channel."""
    flow = _flow(state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=response_url)
    credentials = flow.credentials

    channel = _fetch_channel(credentials)
    studio_repo.save_google_account(
        user_id=user_id,
        token_json=_encrypt(credentials.to_json()),
        scopes=" ".join(credentials.scopes or SCOPES),
        channel_id=channel.get("id", ""),
        channel_title=channel.get("title", ""),
        thumbnail=channel.get("thumbnail", ""),
    )
    return channel


def _fetch_channel(credentials: Credentials) -> dict:
    try:
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        response = (
            youtube.channels()
            .list(part="snippet,statistics,contentDetails", mine=True)
            .execute()
        )
        items = response.get("items") or []
        if not items:
            return {}
        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        return {
            "id": item.get("id", ""),
            "title": snippet.get("title", ""),
            "thumbnail": (
                (snippet.get("thumbnails") or {}).get("default") or {}
            ).get("url", ""),
            "subscribers": stats.get("subscriberCount"),
            "videos": stats.get("videoCount"),
            "views": stats.get("viewCount"),
            "uploads_playlist": (
                (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
            ).get("uploads", ""),
        }
    except Exception as exc:  # noqa: BLE001 - surfaces as "connect again"
        log.warning("Could not read channel details: %s", exc)
        return {}


# --------------------------------------------------------------------------
# Using stored credentials
# --------------------------------------------------------------------------
def load_credentials(user_id: int) -> Credentials | None:
    """Stored credentials, refreshed if expired. None if not connected."""
    account = studio_repo.google_account(user_id)
    if not account:
        return None

    raw = _decrypt(account["token_json"])
    if not raw:
        log.warning(
            "Stored Google token for user %s could not be decrypted "
            "(SECRET_KEY probably changed). Reconnect required.",
            user_id,
        )
        return None

    try:
        credentials = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    except (ValueError, KeyError):
        return None

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(GoogleRequest())
            studio_repo.update_token(user_id, _encrypt(credentials.to_json()))
        except Exception as exc:  # noqa: BLE001
            log.warning("Refreshing Google token failed: %s", exc)
            return None

    return credentials if credentials.valid else None


def youtube_client(user_id: int):
    credentials = load_credentials(user_id)
    if not credentials:
        return None
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def connection_status(user_id: int) -> dict:
    account = studio_repo.google_account(user_id)
    if not account:
        return {"connected": False, "configured": is_configured()}
    return {
        "connected": True,
        "configured": True,
        "channel_id": account["channel_id"],
        "channel_title": account["channel_title"] or "Your channel",
        "thumbnail": account["thumbnail"],
        "connected_at": account["connected_at"],
        "scopes": (account["scopes"] or "").split(),
    }


def disconnect(user_id: int) -> None:
    """Revoke with Google where possible, then forget the token locally."""
    credentials = load_credentials(user_id)
    if credentials and credentials.token:
        try:
            import requests

            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": credentials.token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=6,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Token revoke call failed (harmless): %s", exc)
    studio_repo.disconnect_google(user_id)
