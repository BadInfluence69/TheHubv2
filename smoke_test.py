#!/usr/bin/env python3
"""
Smoke test: create an account, then walk every page and API endpoint.

    python smoke_test.py

Uses a throwaway database in a temp folder and never touches network services,
so it's safe to run any time. A green run means the routing, templates, and
database layer all agree with each other.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="hub-smoke-")
os.environ.update(
    {
        "SECRET_KEY": "smoke-test-key-not-for-real-use",
        "DB_PATH": os.path.join(TMP, "hub.db"),
        "UPLOAD_DIR": os.path.join(TMP, "uploads"),
        "CACHE_DIR": os.path.join(TMP, "cache"),
        "HLS_DIR": os.path.join(TMP, "hls"),
        "DEBUG": "false",
        "ALLOW_REGISTRATION": "true",
        "YOUTUBE_API_KEY": "",
    }
)

from hub import create_app  # noqa: E402
from hub.config import Config  # noqa: E402

GREEN, RED, AMBER, DIM, OFF = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"

passed = 0
failed = 0
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  {GREEN}pass{OFF}  {label}")
    else:
        failed += 1
        failures.append(f"{label} — {detail}")
        print(f"  {RED}FAIL{OFF}  {label}  {DIM}{detail}{OFF}")


def main() -> int:
    app = create_app(Config)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()

    print(f"\n{DIM}database: {app.config['DB_PATH']}{OFF}\n")

    # ---------------------------------------------------------- auth flow --
    print("Accounts")
    response = client.get("/login")
    check("GET /login renders", response.status_code == 200)
    token = extract_csrf(response.get_data(as_text=True))

    response = client.post(
        "/register",
        data={
            "username": "smoketester",
            "password": "correct-horse-9",
            "confirm": "correct-horse-9",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    check("POST /register creates the first account", response.status_code == 302,
          f"got {response.status_code}")

    response = client.get("/")
    check("GET / is signed in", response.status_code == 200, f"got {response.status_code}")

    body = response.get_data(as_text=True)
    token = extract_csrf(body)
    check("CSRF token is present on pages", bool(token))

    # ------------------------------------------------------------- pages --
    print("\nPages")
    pages = [
        "/", "/explore", "/trending", "/subscriptions", "/subscriptions/manage",
        "/library", "/history", "/library/local", "/notifications", "/settings",
        "/feedback/", "/about", "/studio/", "/studio/upload", "/studio/videos",
        "/live", "/playlist/watch-later", "/playlist/liked",
        "/channel/Some%20Channel", "/search?q=test",
    ]
    for path in pages:
        response = client.get(path)
        check(f"GET {path}", response.status_code == 200, f"got {response.status_code}")

    # ------------------------------------------------------- seed a video --
    print("\nSocial features")
    with app.app_context():
        from hub import db
        from hub.repo import videos as videos_repo

        con = db.connect(app.config["DB_PATH"])
        con.execute(
            """INSERT OR REPLACE INTO videos
               (video_id, source, title, channel_key, channel_name, thumbnail,
                description, duration)
               VALUES ('smoke123', 'youtube', 'Smoke test video', 'test channel',
                       'Test Channel', '', 'A description', 212)"""
        )
        con.commit()
        con.close()

    headers = {"X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest"}

    response = client.post("/api/videos/smoke123/vote", json={"value": 1}, headers=headers)
    data = response.get_json() or {}
    check("like a video", response.status_code == 200 and data.get("likes") == 1,
          f"{response.status_code} {data}")

    response = client.post("/api/videos/smoke123/vote", json={"value": 1}, headers=headers)
    data = response.get_json() or {}
    check("liking again clears the vote", data.get("my_vote") == 0 and data.get("likes") == 0,
          str(data))

    response = client.post("/api/videos/smoke123/vote", json={"value": -1}, headers=headers)
    data = response.get_json() or {}
    check("dislike a video", data.get("dislikes") == 1, str(data))

    response = client.post(
        "/api/videos/smoke123/comments", json={"body": "First comment"}, headers=headers
    )
    data = response.get_json() or {}
    comment_id = (data.get("comment") or {}).get("id")
    check("post a comment", response.status_code == 200 and comment_id, str(data)[:120])

    response = client.post(
        "/api/videos/smoke123/comments",
        json={"body": "A reply", "parent_id": comment_id},
        headers=headers,
    )
    check("reply to a comment", response.status_code == 200, str(response.status_code))

    response = client.post(f"/api/comments/{comment_id}/vote", json={"value": 1},
                           headers=headers)
    data = response.get_json() or {}
    check("like a comment", data.get("likes") == 1, str(data))

    response = client.post(f"/api/comments/{comment_id}/edit",
                           json={"body": "Edited comment"}, headers=headers)
    check("edit own comment", response.status_code == 200)

    response = client.post("/api/subscriptions/toggle",
                           json={"channel": "Test Channel"}, headers=headers)
    data = response.get_json() or {}
    check("subscribe to a channel", data.get("subscribed") is True, str(data))

    response = client.post("/api/subscriptions/toggle",
                           json={"channel": "Test Channel"}, headers=headers)
    data = response.get_json() or {}
    check("unsubscribe again", data.get("subscribed") is False, str(data))

    response = client.post("/api/watch-later", json={"video_id": "smoke123"},
                           headers=headers)
    data = response.get_json() or {}
    check("save to Watch later", data.get("saved") is True, str(data))

    response = client.post("/api/playlists/create",
                           json={"title": "Smoke playlist", "video_id": "smoke123"},
                           headers=headers)
    data = response.get_json() or {}
    check("create a playlist", response.status_code == 200 and data.get("playlist"),
          str(data)[:120])

    response = client.post("/api/videos/smoke123/progress",
                           json={"position": 90, "duration": 212}, headers=headers)
    check("save watch progress", response.status_code == 200)

    # ----------------------------------------------------------- watch page --
    print("\nWatch page")
    response = client.get("/watch/smoke123")
    body = response.get_data(as_text=True)
    check("GET /watch/<id>", response.status_code == 200, f"got {response.status_code}")
    check("watch page shows the title", "Smoke test video" in body)
    check("watch page shows comments", "Edited comment" in body)
    check("watch page shows the comment form", "Add a comment" in body)

    # ------------------------------------------------------------- device API --
    print("\nDevice API")
    for path in ["/api/health", "/api/feed", "/api/search?q=smoke"]:
        response = client.get(path)
        check(f"GET {path}", response.status_code == 200, f"got {response.status_code}")

    response = client.get("/api/health")
    data = response.get_json() or {}
    check("health reports a catalogue count", "catalogue" in data, str(data))

    # -------------------------------------------------------------- security --
    print("\nSecurity")
    response = client.post("/api/videos/smoke123/vote", json={"value": 1})
    check("POST without a CSRF token is rejected", response.status_code == 400,
          f"got {response.status_code}")

    response = client.get("/proxy/stream?url=http://169.254.169.254/latest/meta-data/")
    check("proxy refuses arbitrary hosts", response.status_code == 403,
          f"got {response.status_code}")

    response = client.get("/media/stream/local_fake_deadbeef")
    check("unknown local file is a 404", response.status_code == 404,
          f"got {response.status_code}")

    anon = app.test_client()
    response = anon.get("/", follow_redirects=False)
    check("signed-out visitors are redirected to sign in",
          response.status_code == 302 and "/login" in response.headers.get("Location", ""),
          f"got {response.status_code}")

    response = anon.get("/watch/smoke123", follow_redirects=False)
    check("watch page requires sign in", response.status_code == 302)

    # ---------------------------------------------------------------- errors --
    print("\nError handling")
    response = client.get("/definitely-not-a-page")
    check("unknown page returns 404", response.status_code == 404)

    response = client.get("/playlist/does-not-exist")
    check("unknown playlist returns 404", response.status_code == 404)

    # ------------------------------------------------------------- teardown --
    print()
    if failed:
        print(f"{RED}{failed} failed{OFF}, {passed} passed\n")
        for failure in failures:
            print(f"  {RED}·{OFF} {failure}")
        print()
        return 1

    print(f"{GREEN}All {passed} checks passed.{OFF}\n")
    return 0


def extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else ""


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
