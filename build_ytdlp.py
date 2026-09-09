#!/usr/bin/env python3
"""
Build yt-dlp from the vendored source into a single executable.

yt-dlp already ships the real build machinery (bundle/pyinstaller.py, the
Makefile, devscripts/). This just runs the four steps in the right order so
you get a reproducible binary instead of remembering the incantation.

Put this next to the yt-dlp checkout:

    YT1/
      build_ytdlp.py     <- here
      yt-dlp-src/
        yt-dlp/          <- the source tree

Usage:
    python build_ytdlp.py                 # full build
    python build_ytdlp.py --skip-deps     # deps already installed
    python build_ytdlp.py --onedir        # folder instead of one file
    python build_ytdlp.py --install       # copy result next to the hub

Platform note: PyInstaller does not cross-compile. Run this on Windows to
get yt-dlp.exe, on Linux to get an ELF binary, on macOS for a Mach-O one.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SRC = HERE / "yt-dlp-src" / "yt-dlp"
EXE_NAME = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"


def fail(message: str) -> None:
    print(f"\n  ERROR: {message}\n", file=sys.stderr)
    raise SystemExit(1)


def run(args: list[str], cwd: Path, label: str) -> None:
    print(f"\n>>> {label}")
    print(f"    {' '.join(args)}")
    result = subprocess.run(args, cwd=str(cwd))
    if result.returncode != 0:
        fail(f"{label} failed with exit code {result.returncode}")


def find_source(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not (path / "yt_dlp" / "__init__.py").is_file():
            fail(f"{path} doesn't look like a yt-dlp checkout")
        return path

    for candidate in (DEFAULT_SRC, HERE / "yt-dlp", HERE):
        if (candidate / "yt_dlp" / "__init__.py").is_file():
            return candidate.resolve()

    fail(
        "Couldn't find the yt-dlp source. Pass it explicitly:\n"
        "    python build_ytdlp.py --source path/to/yt-dlp"
    )


def check_python() -> None:
    if sys.version_info < (3, 9):
        fail(f"yt-dlp needs Python 3.9+; this is {sys.version.split()[0]}")


def read_version(source: Path) -> str:
    namespace: dict = {}
    try:
        exec((source / "yt_dlp" / "version.py").read_text(), namespace)  # noqa: S102
        return namespace.get("__version__", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="path to the yt-dlp checkout")
    parser.add_argument("--skip-deps", action="store_true",
                        help="don't pip install build requirements")
    parser.add_argument("--skip-lazy", action="store_true",
                        help="skip lazy extractors (slower startup, faster build)")
    parser.add_argument("--onedir", action="store_true",
                        help="produce a folder rather than a single file")
    parser.add_argument("--install", metavar="DIR", nargs="?", const=str(HERE),
                        help="copy the finished binary here when done")
    args = parser.parse_args()

    check_python()
    source = find_source(args.source)
    version = read_version(source)

    print("=" * 66)
    print(f"  Building yt-dlp {version}")
    print(f"  Source:   {source}")
    print(f"  Python:   {sys.version.split()[0]} ({sys.executable})")
    print(f"  Platform: {sys.platform}")
    print("=" * 66)

    if not args.skip_deps:
        run([sys.executable, "-m", "pip", "install", "-U", "pip", "wheel"],
            source, "Updating pip")
        # yt-dlp's own resolver reads pyproject.toml, so this picks up the
        # exact pinned build/runtime set rather than a guess.
        run([sys.executable, "devscripts/install_deps.py", "--include", "pyinstaller"],
            source, "Installing build dependencies")

    if not args.skip_lazy:
        # Without this the binary imports every extractor at startup, which
        # costs a couple of seconds on each invocation.
        run([sys.executable, "devscripts/make_lazy_extractors.py"],
            source, "Generating lazy extractors")

    build = [sys.executable, "bundle/pyinstaller.py"]
    if args.onedir:
        build.append("--onedir")
    run(build, source, "Running PyInstaller")

    produced = source / "dist" / EXE_NAME
    if args.onedir:
        produced = source / "dist" / "yt-dlp" / EXE_NAME
    if not produced.exists():
        fail(f"Build reported success but {produced} isn't there")

    size_mb = produced.stat().st_size / (1024 * 1024)
    print(f"\n>>> Built {produced}  ({size_mb:.1f} MB)")

    # Smoke test: if --version works, the bundle is at least importable.
    check = subprocess.run([str(produced), "--version"],
                           capture_output=True, text=True)
    if check.returncode == 0:
        print(f">>> Smoke test OK: reports {check.stdout.strip()}")
    else:
        print(f">>> WARNING: --version exited {check.returncode}",
              file=sys.stderr)
        print(check.stderr.strip()[:400], file=sys.stderr)

    if args.install:
        target_dir = Path(args.install).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / EXE_NAME
        if target.exists():
            backup = target.with_suffix(target.suffix + ".bak")
            shutil.move(str(target), str(backup))
            print(f">>> Kept the old binary at {backup}")
        shutil.copy2(produced, target)
        print(f">>> Installed to {target}")
        print("\n    Point the hub at it with YTDLP_PATH in .env:")
        print(f"    YTDLP_PATH={target}")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
