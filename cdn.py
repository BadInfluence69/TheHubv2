"""
CDN handoff for locally-stored media.

The server's job here is to *name* the bytes, not to move them. Given a local
video ID, this returns a URL pointing at the CDN edge that fronts the same
files, optionally signed so the URL expires and can't be shared forever.

Three modes, chosen by CDN_MODE:

  off   (default)  -> returns None; callers fall back to serving locally.
  hmac             -> nginx secure_link / Cloudflare-style signed URL.
  s3               -> boto3 presigned GET against an S3-compatible bucket
                      (S3, Cloudflare R2, Backblaze B2, MinIO).

Nothing in here touches the database or the scanner. It maps a path that
local_media already validated onto a public URL and stops.

Drop this file at: hub/services/cdn.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from pathlib import Path
from urllib.parse import quote

from flask import current_app

log = logging.getLogger(__name__)

# Only these are safe to hand straight to a <video> tag. Anything else still
# needs the local transcode path, so we refuse to CDN it.
DIRECT_PLAYABLE = {".mp4", ".m4v", ".webm", ".mov"}

_s3_client = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def enabled() -> bool:
    return (current_app.config.get("CDN_MODE") or "off").lower() != "off"


def _ttl() -> int:
    return int(current_app.config.get("CDN_URL_TTL", 21600))


def object_key(path: Path) -> str | None:
    """
    Turn an absolute local path into the key it has on the CDN.

    Assumes the CDN mirrors the media folders with the shelf name as the top
    level, e.g. E:/Media/Movies/Heat.mp4 -> Movies/Heat.mp4. If the file isn't
    under any configured shelf, returns None and the caller serves it locally.
    """
    from . import local_media

    for shelf, folder in (local_media.shelves() or {}).items():
        try:
            root = Path(folder).expanduser().resolve()
        except OSError:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        prefix = (current_app.config.get("CDN_KEY_PREFIX") or "").strip("/")
        parts = [p for p in (prefix, shelf, relative.as_posix()) if p]
        return "/".join(parts)
    return None


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------
def _sign_hmac(key: str) -> str:
    """
    nginx `secure_link_md5 "$secure_link_expires$uri SECRET"` style, which
    Cloudflare Workers and most edge configs can validate with a few lines.
    """
    base = current_app.config["CDN_BASE_URL"].rstrip("/")
    secret = current_app.config.get("CDN_SIGNING_KEY") or ""
    encoded = quote(key)

    if not secret:
        return f"{base}/{encoded}"

    expires = int(time.time()) + _ttl()
    digest = hashlib.md5(f"{expires}/{encoded} {secret}".encode()).digest()
    token = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"{base}/{encoded}?md5={token}&expires={expires}"


def _sign_s3(key: str) -> str | None:
    global _s3_client
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        log.error("CDN_MODE=s3 but boto3 isn't installed (pip install boto3)")
        return None

    config = current_app.config
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            endpoint_url=config.get("S3_ENDPOINT_URL") or None,
            aws_access_key_id=config.get("S3_ACCESS_KEY"),
            aws_secret_access_key=config.get("S3_SECRET_KEY"),
            region_name=config.get("S3_REGION") or "auto",
            config=Config(signature_version="s3v4"),
        )

    try:
        url = _s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": config["S3_BUCKET"], "Key": key},
            ExpiresIn=_ttl(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Presign failed for %s: %s", key, exc)
        return None

    # If a vanity domain fronts the bucket, swap the host but keep the query.
    base = (config.get("CDN_BASE_URL") or "").rstrip("/")
    if base:
        tail = url.split("/", 3)[-1]
        return f"{base}/{tail}"
    return url


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def url_for_path(path: Path, *, require_playable: bool = True) -> str | None:
    """
    The CDN URL for a local file, or None if this file must be served locally.

    None means: not configured, not mirrored, or a container the browser
    can't play without the transcode pipe.
    """
    if not enabled():
        return None
    if require_playable and path.suffix.lower() not in DIRECT_PLAYABLE:
        return None

    key = object_key(path)
    if not key:
        return None

    mode = current_app.config["CDN_MODE"].lower()
    if mode == "s3":
        return _sign_s3(key)
    if mode == "hmac":
        return _sign_hmac(key)
    return None


def expires_at() -> int:
    """Unix timestamp the URLs handed out right now stop working."""
    return int(time.time()) + _ttl()
