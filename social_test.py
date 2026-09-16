#!/usr/bin/env python3
"""
Smoke tests for the social layer: direct messages, rooms, video responses,
notifications, and privacy.

Runs against a throwaway database. Nothing here touches the network.

    python social_test.py

The encryption itself can only be exercised in a browser — Web Crypto has no
server-side half. What these tests can and do check is the property that
matters most on this side: that the server accepts ciphertext, stores it
unchanged, hands it back unchanged, and never has a plaintext field to leak.
"""
from __future__ import annotations

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"

passed = failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  {GREEN}pass{RESET}  {label}")
    else:
        failed += 1
        print(f"  {RED}FAIL{RESET}  {label}" + (f"  — {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n{name}")


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
tmp_dir = tempfile.mkdtemp(prefix="hub-social-")
os.environ.update(
    {
        "DB_PATH": str(Path(tmp_dir) / "test.db"),
        "UPLOAD_DIR": str(Path(tmp_dir) / "uploads"),
        "CACHE_DIR": str(Path(tmp_dir) / "cache"),
        "HLS_DIR": str(Path(tmp_dir) / "hls"),
        "SECRET_KEY": "test-only-key",
        "MEDIA_LIBRARY": "{}",
    }
)

from hub import create_app  # noqa: E402

app = create_app()
app.config["TESTING"] = True


def sign_up(client, username: str, password: str = "test-password-123"):
    client.get("/register")
    with client.session_transaction() as session:
        token = session.get("csrf")
    return client.post(
        "/register",
        data={"username": username, "password": password,
              "confirm": password, "csrf_token": token},
        follow_redirects=True,
    )


def csrf_for(client) -> str:
    client.get("/")
    with client.session_transaction() as session:
        return session.get("csrf", "")


def post_json(client, url: str, payload: dict):
    return client.post(
        url,
        json=payload,
        headers={"X-CSRF-Token": csrf_for(client), "X-Requested-With": "XMLHttpRequest"},
    )


def fake_key_material() -> dict:
    """Shaped like real output from e2ee.js, but random bytes — the server
    can't tell the difference, which is rather the point."""
    return {
        "public_key": base64.b64encode(secrets.token_bytes(91)).decode(),
        "wrapped_private_key": base64.b64encode(secrets.token_bytes(154)).decode(),
        "wrap_iv": base64.b64encode(secrets.token_bytes(12)).decode(),
        "kdf_salt": base64.b64encode(secrets.token_bytes(16)).decode(),
        "kdf_iterations": 600000,
        "fingerprint": "A1B2 C3D4 E5F6 7890 1234",
    }


print(f"\n{YELLOW}The Hub — social layer tests{RESET}")
print(f"Scratch database: {tmp_dir}")

alice = app.test_client()
bob = app.test_client()
carol = app.test_client()

# --------------------------------------------------------------------------
section("Accounts")
# --------------------------------------------------------------------------
sign_up(alice, "alice")
sign_up(bob, "bob")
sign_up(carol, "carol")

with app.app_context():
    from hub import db as hub_db
    from hub.repo import users as users_repo

    con = hub_db.connect(app.config["DB_PATH"])
    rows = con.execute("SELECT username FROM users ORDER BY username").fetchall()
    names = [r[0] for r in rows]
    con.close()

check("three accounts created", names == ["alice", "bob", "carol"], str(names))

response = alice.get("/messages/")
check("signed-in account reaches the inbox", response.status_code == 200)

anon = app.test_client()
check("messages require sign-in", anon.get("/messages/", follow_redirects=False).status_code == 302)
check("rooms require sign-in", anon.get("/chat/", follow_redirects=False).status_code == 302)

# --------------------------------------------------------------------------
section("Key exchange")
# --------------------------------------------------------------------------
alice_keys = fake_key_material()
bob_keys = fake_key_material()

response = post_json(alice, "/messages/api/keys", alice_keys)
check("alice uploads her public key + wrapped private key", response.status_code == 200)

response = post_json(bob, "/messages/api/keys", bob_keys)
check("bob uploads his", response.status_code == 200)

response = alice.get("/messages/api/keys/me")
data = response.get_json()
check("alice can fetch her own wrapped key back",
      data["exists"] and data["wrapped_private_key"] == alice_keys["wrapped_private_key"])

response = alice.get("/messages/api/keys/bob")
data = response.get_json()
check("alice can fetch bob's PUBLIC key", data["public_key"] == bob_keys["public_key"])

# The wrapped private key must never be reachable through someone else's lookup.
check("bob's wrapped private key is not exposed to alice",
      "wrapped_private_key" not in data, str(list(data.keys())))

weak = fake_key_material()
weak["kdf_iterations"] = 1000
response = post_json(carol, "/messages/api/keys", weak)
check("a weak KDF iteration count is refused", response.status_code == 400)

rotate = fake_key_material()
response = post_json(alice, "/messages/api/keys", rotate)
check("rotating without confirmation is refused", response.status_code == 409,
      f"got {response.status_code}")

# --------------------------------------------------------------------------
section("Direct messages")
# --------------------------------------------------------------------------
with app.app_context():
    con = hub_db.connect(app.config["DB_PATH"])
    ids = {r[1]: r[0] for r in con.execute("SELECT id, username FROM users")}
    con.close()

response = post_json(alice, "/messages/api/start", {"user_id": ids["bob"]})
conversation_id = response.get_json()["conversation_id"]
check("alice opens a conversation with bob", response.status_code == 200)

response = post_json(alice, "/messages/api/start", {"user_id": ids["bob"]})
check("opening it again returns the same thread",
      response.get_json()["conversation_id"] == conversation_id)

response = post_json(alice, "/messages/api/start", {"user_id": ids["alice"]})
check("messaging yourself is refused", response.status_code == 400)

CIPHERTEXT = base64.b64encode(b"this is not really encrypted but the server can't tell").decode()
IV = base64.b64encode(secrets.token_bytes(12)).decode()

response = post_json(alice, f"/messages/api/{conversation_id}/send",
                     {"ciphertext": CIPHERTEXT, "iv": IV})
check("alice sends a message", response.status_code == 200)

response = bob.get(f"/messages/api/{conversation_id}/messages")
messages = response.get_json()["messages"]
check("bob receives it", len(messages) == 1)
check("ciphertext is returned byte-for-byte",
      messages[0]["ciphertext"] == CIPHERTEXT)
check("the iv survives the round trip", messages[0]["iv"] == IV)

# The important structural check: there is no plaintext column at all.
with app.app_context():
    con = hub_db.connect(app.config["DB_PATH"])
    columns = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    con.close()
check("the messages table has no plaintext body column",
      "body" not in columns and "text" not in columns and "plaintext" not in columns,
      str(sorted(columns)))

response = carol.get(f"/messages/api/{conversation_id}/messages")
check("a third party cannot read the thread", response.status_code == 404)

response = post_json(carol, f"/messages/api/{conversation_id}/send",
                     {"ciphertext": CIPHERTEXT, "iv": IV})
check("a third party cannot post into the thread", response.status_code == 404)

response = post_json(alice, f"/messages/api/{conversation_id}/send",
                     {"ciphertext": "x" * 70000, "iv": IV})
check("an oversized message is refused", response.status_code == 400)

response = post_json(alice, f"/messages/api/{conversation_id}/send",
                     {"ciphertext": "", "iv": ""})
check("an empty message is refused", response.status_code == 400)

# --------------------------------------------------------------------------
section("Read state, unsend, blocking")
# --------------------------------------------------------------------------
with app.app_context():
    from hub.repo import messages as messages_repo

    with app.test_request_context():
        pass

response = bob.get("/messages/")
check("bob's inbox renders", response.status_code == 200)

bob.get(f"/messages/{conversation_id}")
response = alice.get(f"/messages/api/{conversation_id}/messages")
check("alice sees her message marked read once bob opens it",
      response.get_json()["messages"][0]["read_at"] is not None)

message_id = messages[0]["id"]
response = post_json(bob, f"/messages/api/message/{message_id}/delete", {})
check("bob cannot unsend alice's message", response.status_code == 403)

response = post_json(alice, f"/messages/api/message/{message_id}/delete", {})
check("alice can unsend her own", response.status_code == 200)

response = bob.get(f"/messages/api/{conversation_id}/messages")
unsent = response.get_json()["messages"][0]
check("unsent message keeps its row but loses its ciphertext",
      unsent["is_deleted"] == 1 and unsent["ciphertext"] == "")

response = post_json(bob, "/messages/api/block", {"user_id": ids["alice"]})
check("bob blocks alice", response.status_code == 200)

response = post_json(alice, f"/messages/api/{conversation_id}/send",
                     {"ciphertext": CIPHERTEXT, "iv": IV})
check("a blocked sender's message is refused", response.status_code == 403)
check("the refusal doesn't reveal the block",
      "block" not in response.get_json()["error"].lower(),
      response.get_json()["error"])

post_json(bob, "/messages/api/block", {"user_id": ids["alice"], "undo": True})
response = post_json(alice, f"/messages/api/{conversation_id}/send",
                     {"ciphertext": CIPHERTEXT, "iv": IV})
check("unblocking restores delivery", response.status_code == 200)

# --------------------------------------------------------------------------
section("People directory")
# --------------------------------------------------------------------------
response = alice.get("/messages/api/people?q=bo")
people = response.get_json()["people"]
check("searching finds bob", any(p["username"] == "bob" for p in people))
check("search never returns yourself", not any(p["username"] == "alice" for p in people))
check("the directory reports who has keys set up",
      any(p["username"] == "bob" and p["has_keys"] for p in people))

response = alice.get("/messages/api/people?q=nobody-by-this-name")
check("an unknown name returns nothing", response.get_json()["people"] == [])

# --------------------------------------------------------------------------
section("Public rooms")
# --------------------------------------------------------------------------
response = alice.get("/chat/")
check("room list renders", response.status_code == 200)
check("default rooms were seeded", b"Lobby" in response.data)

with app.app_context():
    from hub.repo import chat as chat_repo

    with app.test_request_context():
        con = hub_db.connect(app.config["DB_PATH"])
        lobby_id = con.execute(
            "SELECT id FROM chat_rooms WHERE slug = 'lobby'"
        ).fetchone()[0]
        con.close()

response = post_json(alice, f"/chat/api/{lobby_id}/post", {"body": "first post in here"})
check("alice posts to the lobby", response.status_code == 200)

response = bob.get(f"/chat/api/{lobby_id}/messages")
posts = response.get_json()["messages"]
check("bob sees it", any(p["body"] == "first post in here" for p in posts))

response = post_json(alice, f"/chat/api/{lobby_id}/post", {"body": "x" * 3000})
check("an oversized post is refused", response.status_code == 400)

response = post_json(alice, f"/chat/api/{lobby_id}/post", {"body": "   "})
check("an empty post is refused", response.status_code == 400)

response = post_json(bob, f"/chat/api/{lobby_id}/post", {"body": "hey @alice look at this"})
check("bob mentions alice", response.status_code == 200)

response = alice.get("/notifications?kind=mention")
check("alice gets a mention notification", b"mentioned you" in response.data)

response = post_json(alice, f"/chat/api/{lobby_id}/post", {"body": "hi @ghostuser"})
check("mentioning a non-existent account is harmless", response.status_code == 200)

# Blocked accounts disappear from the room, not just from DMs.
post_json(carol, "/messages/api/block", {"user_id": ids["alice"]})
response = carol.get(f"/chat/api/{lobby_id}/messages")
check("a blocked account's room posts are hidden",
      not any(p["username"] == "alice" for p in response.get_json()["messages"]))
post_json(carol, "/messages/api/block", {"user_id": ids["alice"], "undo": True})

response = alice.post(
    "/chat/create",
    data={"name": "Late night horror", "topic": "Spooky", "csrf_token": csrf_for(alice)},
    follow_redirects=True,
)
check("alice creates a room", response.status_code == 200 and b"Late night horror" in response.data)

response = bob.post(
    "/chat/lobby/delete",
    data={"csrf_token": csrf_for(bob)},
    follow_redirects=True,
)
check("a non-owner cannot delete a room", response.status_code == 403)

# --------------------------------------------------------------------------
section("Video responses")
# --------------------------------------------------------------------------
with app.app_context():
    from hub.repo import videos as videos_repo

    with app.test_request_context():
        videos_repo.upsert("target123ab", "A video to respond to", "Someone")
        videos_repo.upsert("answer456cd", "The answer video", "Alice")
        hub_db.get_db().commit()

response = alice.post(
    "/api/videos/target123ab/comments",
    data={"body": "here's my take", "response_ref": "answer456cd",
          "csrf_token": csrf_for(alice)},
    headers={"X-Requested-With": "XMLHttpRequest"},
)
check("a comment can carry a video response", response.status_code == 200)
data = response.get_json()
check("the response is linked to the comment",
      data["comment"]["response_video_id"] == "answer456cd")
check("the response count is reported back", data.get("responses") == 1)

response = alice.post(
    "/api/videos/target123ab/comments",
    data={"body": "", "response_ref": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
          "csrf_token": csrf_for(alice)},
    headers={"X-Requested-With": "XMLHttpRequest"},
)
check("a YouTube URL is accepted as a response reference", response.status_code == 200)
check("a video response needs no text to stand on its own",
      response.get_json()["comment"]["response_video_id"] == "dQw4w9WgXcQ")

response = alice.post(
    "/api/videos/target123ab/comments",
    data={"body": "nope", "response_ref": "not a video at all",
          "csrf_token": csrf_for(alice)},
    headers={"X-Requested-With": "XMLHttpRequest"},
)
check("nonsense in the reference field is rejected", response.status_code == 400)

response = alice.post(
    "/api/videos/target123ab/comments",
    data={"body": "", "csrf_token": csrf_for(alice)},
    headers={"X-Requested-With": "XMLHttpRequest"},
)
check("a comment with neither text nor video is still refused", response.status_code == 400)

response = alice.get("/watch/target123ab")
check("the watch page shows the responses shelf",
      b"video response" in response.data.lower(), "shelf missing")

with app.app_context():
    with app.test_request_context():
        from hub.repo import comments as comments_repo

        check("response_count counts only video responses",
              comments_repo.response_count("target123ab") == 2)
        back = comments_repo.responded_to_by("answer456cd")
        check("a response knows what it was responding to",
              back is not None and back["video_id"] == "target123ab")

# --------------------------------------------------------------------------
section("Notifications")
# --------------------------------------------------------------------------
with app.app_context():
    with app.test_request_context():
        for i in range(30):
            users_repo.notify(ids["carol"], f"Test notice {i}", kind="info")
        hub_db.get_db().commit()

        counts = users_repo.total_by_kind(ids["carol"])
        check("notifications are counted per kind", counts["info"] >= 30)

        users_repo.notify_once(ids["carol"], "Repeat", url="/same", kind="message")
        users_repo.notify_once(ids["carol"], "Repeat", url="/same", kind="message")
        users_repo.notify_once(ids["carol"], "Repeat", url="/same", kind="message")
        hub_db.get_db().commit()
        check("repeat notifications collapse into one row",
              users_repo.total_by_kind(ids["carol"])["message"] == 1)

        for i in range(600):
            users_repo.notify(ids["carol"], f"Flood {i}", kind="info")
        users_repo.trim_notifications(ids["carol"], keep=500)
        hub_db.get_db().commit()
        total = sum(users_repo.total_by_kind(ids["carol"]).values())
        check("the inbox is capped so it can't grow forever",
              total <= 500, f"held {total}")

response = carol.get("/notifications")
check("the notifications page renders", response.status_code == 200)
check("filter chips are present", b"chip" in response.data)

response = carol.get("/notifications?kind=message")
check("filtering by kind works", response.status_code == 200)

response = carol.get("/notifications?kind=nonsense")
check("an unknown filter falls back to everything", response.status_code == 200)

response = post_json(carol, "/api/notifications/clear", {"kind": "message"})
check("clearing one kind works", response.status_code == 200)

with app.app_context():
    with app.test_request_context():
        check("clearing one kind left the others alone",
              users_repo.total_by_kind(ids["carol"])["info"] > 0)

# --------------------------------------------------------------------------
section("Privacy page")
# --------------------------------------------------------------------------
response = anon.get("/privacy")
check("the privacy page is readable signed out", response.status_code == 200)

body = response.data.decode("utf-8", "replace").lower()
check("it admits metadata is stored", "metadata" in body)
check("it admits rooms are not encrypted",
      "not encrypted" in body or "plain text" in body)
check("it addresses legal requests honestly", "producible" in body)
check("it names the forward secrecy limitation", "forward secrecy" in body)
check("it does not claim nothing is stored",
      "we don't store a single thing" not in body)

# --------------------------------------------------------------------------
section("Session IP opt-out")
# --------------------------------------------------------------------------
app.config["STORE_SESSION_IP"] = False
dave = app.test_client()
sign_up(dave, "dave")

with app.app_context():
    con = hub_db.connect(app.config["DB_PATH"])
    row = con.execute(
        "SELECT s.ip, s.user_agent FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE u.username = 'dave'"
    ).fetchone()
    con.close()

check("with the opt-out on, no IP is recorded", row is not None and row[0] == "")
check("with the opt-out on, no user agent is recorded", row is not None and row[1] == "")
app.config["STORE_SESSION_IP"] = True

# --------------------------------------------------------------------------
print()
if failed:
    print(f"{RED}{failed} check{'' if failed == 1 else 's'} failed{RESET}, {passed} passed.")
    sys.exit(1)

print(f"{GREEN}All {passed} checks passed.{RESET}\n")
