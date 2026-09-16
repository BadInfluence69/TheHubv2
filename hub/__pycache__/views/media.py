"""
Serving local files.

Byte-range streaming so seeking works, on-demand thumbnail extraction with
ffmpeg, on-the-fly transcoding for containers browsers won't touch, and a
narrow proxy for remote CDN streams.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import subprocess
from pathlib import Path

import requests
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
)

from ..repo import videos as videos_repo
from ..security import current_user, login_required
from ..services import local_media, responses, streams

log = logging.getLogger(__name__)
bp = Blueprint("media", __name__)

CHUNK = 1024 * 256
# Containers and codecs no browser will play natively; these get transcoded.
TRANSCODE_EXTENSIONS = {".mkv", ".avi", ".wmv", ".flv", ".m2ts", ".mpg", ".mpeg"}
TRANSCODE_CODECS = {"hevc", "h265", "vp9-unsupported", "mpeg4", "msmpeg4v3", "vc1"}


# --------------------------------------------------------------------------
# Library page
# --------------------------------------------------------------------------
@bp.route("/library/local")
@login_required
def library():
    user = current_user()
    shelves = local_media.by_shelf()
    for items in shelves.values():
        videos_repo.attach_user_state(items, user["id"])

    return render_template(
        "media_library.html",
        shelves=shelves,
        stats=local_media.stats(),
        configured=bool(local_media.shelves()),
    )


@bp.post("/library/local/rescan")
@login_required
def rescan():
    local_media.invalidate()
    local_media.scan(force=True)
    return redirect(request.referrer or "/library/local")


# --------------------------------------------------------------------------
# Streaming a local file
# --------------------------------------------------------------------------
@bp.route("/media/stream/<video_id>")
def stream_local(video_id: str):
    """
    Range-aware file streaming.

    No login check here on purpose: <video> elements and TV apps don't carry
    the session cookie reliably. The video ID is a SHA-1 of the full path, so
    it can't be guessed, and safe_path() refuses anything outside a configured
    media folder.
    """
    path = local_media.safe_path(video_id)
    if not path:
        abort(404)

    if _needs_transcode(path):
        return _transcode_response(path)

    return _ranged_file_response(path)


def _needs_transcode(path: Path) -> bool:
    if path.suffix.lower() in TRANSCODE_EXTENSIONS:
        codec = streams.probe_video_codec(str(path)).lower()
        # An MKV holding H.264 can be remuxed cheaply rather than re-encoded,
        # but either way it can't be served as-is.
        return True
    codec = streams.probe_video_codec(str(path)).lower()
    return codec in TRANSCODE_CODECS


def _ranged_file_response(path: Path) -> Response:
    file_size = path.stat().st_size
    mimetype = mimetypes.guess_type(str(path))[0] or "video/mp4"
    range_header = request.headers.get("Range")

    if not range_header:
        response = send_file(path, mimetype=mimetype, conditional=True)
        response.headers["Accept-Ranges"] = "bytes"
        return response

    match = re.search(r"bytes=(\d*)-(\d*)", range_header)
    if not match:
        abort(416)

    start = int(match.group(1)) if match.group(1) else 0
    end = int(match.group(2)) if match.group(2) else file_size - 1
    start = max(0, min(start, file_size - 1))
    end = max(start, min(end, file_size - 1))
    length = end - start + 1

    def generate():
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                data = handle.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    response = Response(
        stream_with_context(generate()), status=206, mimetype=mimetype
    )
    response.headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "no-cache",
        }
    )
    return response


def _transcode_response(path: Path) -> Response:
    """
    Remux or re-encode to fragmented MP4 on the fly.

    H.264 inside a stubborn container is copied straight across, which costs
    almost nothing. Anything else gets a real encode, which is CPU-heavy — a
    veryfast preset keeps it watchable on modest hardware.
    """
    codec = streams.probe_video_codec(str(path)).lower()
    can_copy = codec in ("h264", "avc1")

    args = [
        current_app.config["FFMPEG_PATH"],
        "-hide_banner", "-loglevel", "error",
        "-i", str(path),
    ]
    if can_copy:
        args += ["-c:v", "copy"]
    else:
        args += [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-maxrate", "6M",
            "-bufsize", "12M",
            "-pix_fmt", "yuv420p",
        ]
    args += [
        "-c:a", "aac",
        "-b:a", "192k",
        "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "pipe:1",
    ]

    log.info("Transcoding %s (codec=%s, copy=%s)", path.name, codec, can_copy)

    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=CHUNK
        )
    except FileNotFoundError:
        abort(500, description="ffmpeg isn't installed, so this file can't be converted.")

    def generate():
        try:
            while True:
                data = process.stdout.read(CHUNK)
                if not data:
                    break
                yield data
        finally:
            process.stdout.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    response = Response(stream_with_context(generate()), mimetype="video/mp4")
    response.headers["Accept-Ranges"] = "none"
    response.headers["Cache-Control"] = "no-cache"
    return response


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------
@bp.route("/media/thumb/<video_id>")
def thumbnail(video_id: str):
    """Pull a frame out of a local file, cached to disk after the first go."""
    path = local_media.safe_path(video_id)
    if not path:
        return redirect("/static/img/placeholder.svg")

    cache_dir = Path(current_app.config["CACHE_DIR"]) / "thumbs"
    cache_dir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha1(str(path).encode("utf-8", "replace")).hexdigest()[:16]
    cached = cache_dir / f"{digest}.jpg"

    if cached.exists() and cached.stat().st_size > 0:
        return send_file(cached, mimetype="image/jpeg", max_age=604800)

    args = [
        current_app.config["FFMPEG_PATH"],
        "-hide_banner", "-loglevel", "error",
        "-ss", "00:03:00",
        "-i", str(path),
        "-frames:v", "1",
        "-vf", "scale=640:-2",
        "-q:v", "4",
        "-y", str(cached),
    ]
    try:
        result = subprocess.run(args, capture_output=True, timeout=45)
        if result.returncode != 0 or not cached.exists():
            # Short clip? Try the very beginning instead.
            args[args.index("-ss") + 1] = "00:00:03"
            subprocess.run(args, capture_output=True, timeout=45)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.debug("Thumbnail extraction failed for %s: %s", path.name, exc)

    if cached.exists() and cached.stat().st_size > 0:
        return send_file(cached, mimetype="image/jpeg", max_age=604800)
    return redirect("/static/img/placeholder.svg")


@bp.route("/media/response/<video_id>")
@login_required
def stream_response(video_id: str):
    """
    Serve an uploaded video response.

    Same byte-range path as the local library, but resolved through the
    responses service so the file has to be inside the responses directory to
    be readable at all.
    """
    path = responses.path_for(video_id)
    if not path:
        abort(404)
    if _needs_transcode(path):
        return _transcode_response(path)
    return _ranged_file_response(path)


@bp.route("/media/response-thumb/<video_id>")
def response_thumbnail(video_id: str):
    poster = responses.poster_path(video_id)
    if not poster:
        return redirect("/static/img/placeholder.svg")
    return send_file(poster, mimetype="image/jpeg", max_age=604800)


@bp.route("/media/download/<video_id>")
@login_required
def download_local(video_id: str):
    path = local_media.safe_path(video_id)
    if not path:
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


# --------------------------------------------------------------------------
# Proxy for remote streams
# --------------------------------------------------------------------------
@bp.route("/proxy/stream")
def proxy_stream():
    """
    Forwards a CDN stream through this server, which fixes the CORS and
    referrer checks that otherwise break playback in the browser.

    Only hosts yt-dlp has actually handed us are forwarded to — see
    streams.host_allowed. Without that check this route would be an open
    proxy for anything on the internal network.
    """
    url = request.args.get("url", "")
    if not url or not streams.host_allowed(url):
        abort(403, description="That URL isn't one this server will fetch.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.youtube.com/",
    }
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    try:
        upstream = requests.get(url, headers=headers, stream=True, timeout=20)
    except requests.RequestException as exc:
        log.warning("Proxy fetch failed: %s", exc)
        abort(502, description="The upstream server didn't answer.")

    response_headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"),
    }
    for header in ("Content-Range", "Content-Length"):
        if header in upstream.headers:
            response_headers[header] = upstream.headers[header]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=CHUNK):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=response_headers,
    )


# --------------------------------------------------------------------------
# HLS passthrough (for an OBS or ffmpeg live feed)
# --------------------------------------------------------------------------
@bp.route("/live")
@login_required
def live():
    hls_dir = Path(current_app.config["HLS_DIR"])
    playlists = sorted(hls_dir.glob("*.m3u8")) if hls_dir.exists() else []
    return render_template(
        "live.html",
        playlists=[p.name for p in playlists],
        hls_dir=str(hls_dir),
    )


@bp.route("/live/<path:filename>")
def live_file(filename: str):
    hls_dir = Path(current_app.config["HLS_DIR"]).resolve()
    target = (hls_dir / filename).resolve()

    if hls_dir not in target.parents or not target.is_file():
        abort(404)

    mimetype = {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
        ".m4s": "video/iso.segment",
        ".mp4": "video/mp4",
    }.get(target.suffix.lower(), "application/octet-stream")

    response = send_file(target, mimetype=mimetype)
    response.headers["Cache-Control"] = "no-cache"
    return response
