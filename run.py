#!/usr/bin/env python3
"""
python -m pip install -U yt-dlp

Start The Hub in development mode.

    python run.py

For anything long-running or exposed to the network, use a real WSGI server
instead — see README.md.
"""

from __future__ import annotations

import os
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from hub import create_app
from hub.config import Config

BASE_DIR = Path(__file__).resolve().parent

GREEN = "\033[92m"
AMBER = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"

if os.name == "nt":
    # Ask Windows Terminal to honour ANSI colours; harmless if it can't.
    os.system("")


def local_ip() -> str:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return "localhost"


def check_binary(name: str, path: str) -> tuple[bool, str]:
    resolved = shutil.which(path) or (path if Path(path).exists() else None)
    if not resolved:
        return False, f"{name} not found"
    try:
        result = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, timeout=10
        )
        version = (result.stdout or result.stderr).strip().splitlines()[0][:60]
        return True, version
    except (subprocess.TimeoutExpired, OSError):
        return True, "found"


def line(ok: bool, label: str, detail: str = "") -> None:
    mark = f"{GREEN}ok  {OFF}" if ok else f"{AMBER}--  {OFF}"
    print(f"  {mark} {label:<22} {DIM}{detail}{OFF}")


def preflight() -> None:
    print(f"\n{BOLD}The Hub{OFF} {DIM}starting up{OFF}\n")

    print(f"{DIM}  external tools{OFF}")
    ytdlp_ok, ytdlp_info = check_binary("yt-dlp", Config.YTDLP_PATH)
    line(ytdlp_ok, "yt-dlp", ytdlp_info)
    if not ytdlp_ok:
        print(f"      {AMBER}Streaming won't work. Install it:{OFF} pip install -U yt-dlp")

    ffmpeg_ok, ffmpeg_info = check_binary("ffmpeg", Config.FFMPEG_PATH)
    line(ffmpeg_ok, "ffmpeg", ffmpeg_info)
    if not ffmpeg_ok:
        print(f"      {DIM}Thumbnails and MKV playback need it. ffmpeg.org/download{OFF}")

    print(f"\n{DIM}  configuration{OFF}")
    line(bool(os.getenv("SECRET_KEY")), "SECRET_KEY",
         "set in .env" if os.getenv("SECRET_KEY") else "random — sessions reset on restart")
    line(bool(Config.YOUTUBE_API_KEY), "YouTube API key",
         "set" if Config.YOUTUBE_API_KEY else "not set — search falls back to scraping")

    oauth_ready = (
        Path(Config.GOOGLE_CLIENT_SECRETS).exists()
        or (Config.GOOGLE_CLIENT_ID and Config.GOOGLE_CLIENT_SECRET)
    )
    line(oauth_ready, "Google OAuth",
         "ready" if oauth_ready else "not configured — uploading is off")

    shelves = Config.MEDIA_LIBRARY
    if shelves:
        for label, folder in shelves.items():
            exists = Path(folder).expanduser().is_dir()
            line(exists, f"media: {label}", folder if exists else f"{folder} (missing)")
    else:
        line(False, "media library", "no folders configured")

    print(f"\n{DIM}  storage{OFF}")
    line(True, "database", str(Config.DB_PATH))
    line(True, "uploads", str(Config.UPLOAD_DIR))


def main() -> int:
    preflight()

    app = create_app()
    port = Config.PORT
    ip = local_ip()

    print(f"\n{DIM}  reachable at{OFF}")
    print(f"  {GREEN}→{OFF}  http://localhost:{port}")
    print(f"  {GREEN}→{OFF}  http://{ip}:{port}   {DIM}(other devices on this network){OFF}")

    if Config.DEBUG:
        print(f"\n  {AMBER}Debug mode is on.{OFF} {DIM}Turn it off before exposing this "
              f"beyond your own network.{OFF}")

    print(f"\n{DIM}  Ctrl+C to stop{OFF}\n")

    try:
        app.run(
            host=Config.HOST,
            port=port,
            debug=Config.DEBUG,
            threaded=True,
            use_reloader=Config.DEBUG,
        )
    except OSError as exc:
        if getattr(exc, "errno", None) in (48, 98, 10048):
            print(f"\n{RED}Port {port} is already in use.{OFF}")
            print(f"{DIM}Either stop whatever has it, or set PORT in .env{OFF}\n")
            return 1
        raise
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{OFF}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
