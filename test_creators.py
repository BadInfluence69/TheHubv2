"""
Creator profiles, fan terminology, and database concurrency.

The concurrency section is a real load test, not an assertion that the pragmas
were set. It spawns threads that actually hammer the file, because "we turned
WAL on" and "it survives 40 concurrent writers" are different claims.

Run with:  python test_creators.py
"""
from __future__ import annotations

import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="hub-creator-test-")
os.environ.update(
    {
        "DB_PATH": os.path.join(TMP, "test.db"),
        "UPLOAD_DIR": os.path.join(TMP, "uploads"),
        "CACHE_DIR": os.path.join(TMP, "cache"),
        "HLS_DIR": os.path.join(TMP, "hls"),
        "SECRET_KEY": "test-secret-key-creators",
        "ALLOW_REGISTRATION": "1",
    }
)

PASSED, FAILED = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        FAILED += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"  — {detail}" if detail else ""))


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hub import create_app, db, security  # noqa: E402
from hub.repo import creators as creators_repo  # noqa: E402
from hub.repo import users as users_repo  # noqa: E402

app = create_app()
app.config["TESTING"] = True


def make_users():
    with app.app_context():
        owner = users_repo.create("owner", security.hash_password("hunter2222"), True)
        creator = users_repo.create("Nova", security.hash_password("hunter2222"), False)
        fan = users_repo.create("fan", security.hash_password("hunter2222"), False)
        db.get_db().commit()
    return owner, creator, fan


def client_for(user_id):
    """
    A signed-in test client that also posts a valid CSRF token.

    Every state-changing route in this app requires one, so a client that
    can't produce it would fail every POST for the wrong reason.
    """
    import flask

    with app.test_request_context():
        security.start_session(user_id)
        db.get_db().commit()
        cookie = dict(flask.session)

    client = app.test_client()
    with client.session_transaction() as session:
        session.update(cookie)

    csrf = cookie.get("csrf", "")
    original_post = client.post

    def post(*args, **kwargs):
        data = kwargs.get("data")
        if isinstance(data, dict) and "csrf_token" not in data:
            data = dict(data)
            data["csrf_token"] = csrf
            kwargs["data"] = data
        elif data is None:
            kwargs["headers"] = {**kwargs.get("headers", {}), "X-CSRF-Token": csrf}
        return original_post(*args, **kwargs)

    client.post = post
    return client


OWNER, CREATOR, FAN = make_users()


# ==========================================================================
def test_url_validation() -> None:
    print("\nURL validation")
    cases_bad = [
        ("javascript:alert(1)", "javascript scheme rejected"),
        ("data:text/html,<script>alert(1)</script>", "data scheme rejected"),
        ("http://ko-fi.com/nova", "plain http rejected"),
        ("", "empty rejected"),
        ("notaurl", "bare word rejected"),
        ("vbscript:msgbox", "vbscript rejected"),
    ]
    for raw, label in cases_bad:
        _, error = creators_repo.clean_url(raw)
        check(label, error is not None, f"accepted {raw!r}")

    cases_good = [
        ("https://ko-fi.com/nova", "https accepted"),
        ("ko-fi.com/nova", "bare domain gets https:// added"),
        ("https://buy.stripe.com/abc123", "stripe link accepted"),
    ]
    for raw, label in cases_good:
        url, error = creators_repo.clean_url(raw)
        check(label, error is None and url.startswith("https://"), f"{raw!r} -> {error}")

    check("provider detected for ko-fi",
          creators_repo.provider_for("https://ko-fi.com/nova") == "Ko-fi")
    check("provider detected for a stripe subdomain",
          creators_repo.provider_for("https://buy.stripe.com/x") == "Stripe")
    check("unknown host gives no provider",
          creators_repo.provider_for("https://example.com/pay") == "")


def test_permissions() -> None:
    print("\nWho can edit a profile")
    with app.app_context():
        creator_user = dict(users_repo.by_id(CREATOR))
        fan_user = dict(users_repo.by_id(FAN))
        owner_user = dict(users_repo.by_id(OWNER))

        check("creator can claim the channel matching their own name",
              creators_repo.can_edit("Nova", creator_user))
        check("an unrelated account cannot",
              not creators_repo.can_edit("Nova", fan_user))
        check("admin can edit any channel",
              creators_repo.can_edit("Nova", owner_user))
        check("signed-out cannot", not creators_repo.can_edit("Nova", None))

        creators_repo.claim("Nova", CREATOR)
        db.get_db().commit()
        check("once claimed, the claimant keeps access",
              creators_repo.can_edit("Nova", creator_user))
        check("once claimed, others are locked out",
              not creators_repo.can_edit("Nova", fan_user))


def test_profile_flow() -> None:
    print("\nProfile and links")
    creator_client = client_for(CREATOR)

    response = creator_client.post(
        "/creator/Nova",
        data={"tagline": "Field recordings", "about": "About me",
              "support_note": "Pays for tape.", "is_published": "on"},
        follow_redirects=True,
    )
    check("creator saves their profile", response.status_code == 200)

    for kind, url, label in [
        ("support", "https://ko-fi.com/nova", "Buy me a coffee"),
        ("merch", "https://nova.gumroad.com/l/shirt", "Tour shirt"),
        ("affiliate", "https://example.com/mic?ref=nova", "The mic I use"),
    ]:
        creator_client.post(
            "/creator/Nova/links/add",
            data={"kind": kind, "label": label, "url": url, "detail": "$5"},
            follow_redirects=True,
        )

    with app.app_context():
        profile = creators_repo.public_profile("Nova")
        check("published profile is visible", profile is not None)
        check("support link stored", len(profile["links"]["support"]) == 1)
        check("merch link stored", len(profile["links"]["merch"]) == 1)
        check("affiliate link stored", len(profile["links"]["affiliate"]) == 1)
        check("provider badge resolved", profile["links"]["support"][0]["provider"] == "Ko-fi")

    # A bad URL must be refused through the web layer too, not just the repo.
    creator_client.post(
        "/creator/Nova/links/add",
        data={"kind": "support", "label": "Bad", "url": "javascript:alert(1)"},
        follow_redirects=True,
    )
    with app.app_context():
        check("javascript: link never reaches the database",
              len(creators_repo.links("Nova", "support")) == 1)

    # Another account must not be able to edit it.
    fan_client = client_for(FAN)
    response = fan_client.post(
        "/creator/Nova/links/add",
        data={"kind": "support", "label": "Mine now", "url": "https://evil.example.com"},
    )
    check("another account is refused (403)", response.status_code == 403,
          f"got {response.status_code}")

    response = fan_client.get("/creator/Nova")
    check("another account can't open the editor", response.status_code == 403)


def test_public_rendering() -> None:
    print("\nWhat a fan sees")
    fan_client = client_for(FAN)
    response = fan_client.get("/channel/Nova")
    body = response.data.decode("utf-8", "replace")

    check("channel page loads", response.status_code == 200)
    check("support link is rendered", "ko-fi.com/nova" in body)
    check("support note is shown", "Pays for tape." in body)
    check("affiliate disclosure appears", "may earn a commission" in body)
    check("affiliate links carry rel=sponsored", 'sponsored' in body)
    check("outbound links carry noopener", "noopener" in body)
    check("outbound links carry nofollow", "nofollow" in body)

    # Terminology
    check("button says 'Become a fan'", "Become a fan" in body)
    check("the word Subscribe is gone from the channel page",
          "Subscribe" not in body, "still present")
    check("count reads 'fans'", "fan" in body.lower())

    # An unpublished profile must not leak.
    with app.app_context():
        creators_repo.update("Nova", is_published=0)
        db.get_db().commit()
    body2 = fan_client.get("/channel/Nova").data.decode("utf-8", "replace")
    check("unpublished profile is hidden from fans", "ko-fi.com/nova" not in body2)
    with app.app_context():
        creators_repo.update("Nova", is_published=1)
        db.get_db().commit()


def test_click_counter() -> None:
    print("\nClick counting")
    with app.app_context():
        link = creators_repo.links("Nova", "support")[0]
        link_id = link["id"]
        before = link["clicks"]

    fan_client = client_for(FAN)
    for _ in range(3):
        fan_client.post(f"/creator/links/{link_id}/click")

    with app.app_context():
        after = creators_repo.get_link(link_id)["clicks"]
        check("clicks are counted", after == before + 3, f"{before} -> {after}")

        columns = {
            row[1]
            for row in db.get_db().execute("PRAGMA table_info(creator_links)")
        }
        check("no user column on the click table",
              "user_id" not in columns and "clicked_by" not in columns)
        check("no per-click row table exists",
              db.query_one(
                  "SELECT name FROM sqlite_master WHERE type='table' "
                  "AND name LIKE '%click%'"
              ) is None)


# ==========================================================================
# Database concurrency — a real load test
# ==========================================================================
def test_pragmas() -> None:
    print("\nDatabase settings")
    with app.app_context():
        con = db.get_db()

        def pragma(name):
            return con.execute(f"PRAGMA {name}").fetchone()[0]

        check("journal_mode is WAL", str(pragma("journal_mode")).lower() == "wal",
              str(pragma("journal_mode")))
        check("synchronous is NORMAL (1)", int(pragma("synchronous")) == 1)
        check("busy_timeout is generous", int(pragma("busy_timeout")) >= 20000)
        check("mmap_size is set", int(pragma("mmap_size")) > 0)
        check("temp_store is MEMORY (2)", int(pragma("temp_store")) == 2)
        check("foreign keys on", int(pragma("foreign_keys")) == 1)


def test_indexes_used() -> None:
    print("\nQuery plans")
    with app.app_context():
        con = db.get_db()

        def plan(sql):
            return " ".join(row[3] for row in con.execute("EXPLAIN QUERY PLAN " + sql))

        checks = [
            ("watch_history by video uses an index",
             "SELECT * FROM watch_history WHERE video_id='x'"),
            ("comments by user uses an index",
             "SELECT * FROM comments WHERE user_id=1"),
            ("subscriber count uses an index",
             "SELECT COUNT(*) FROM subscriptions WHERE channel_key='x'"),
            ("uploads by status uses an index",
             "SELECT * FROM uploads WHERE status='done'"),
            ("session expiry sweep uses an index",
             "SELECT * FROM sessions WHERE expires_at < datetime('now')"),
            ("playlist items by video uses an index",
             "SELECT * FROM playlist_items WHERE video_id='x'"),
        ]
        for label, sql in checks:
            detail = plan(sql)
            check(label, "USING INDEX" in detail or "USING COVERING INDEX" in detail,
                  detail)


def test_concurrent_load() -> None:
    """
    Threads writing and reading the same file at once.

    This is the claim worth testing: that a burst of simultaneous activity
    completes without SQLITE_BUSY errors and without losing a write.
    """
    print("\nConcurrent load")

    db_path = os.path.join(TMP, "load.db")
    with app.app_context():
        pass

    # Build a small schema on a separate file so the test is self-contained.
    setup = db.connect(db_path)
    setup.executescript(
        """
        CREATE TABLE hits (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           worker INTEGER, note TEXT,
                           at TEXT DEFAULT (datetime('now')));
        CREATE INDEX idx_hits_worker ON hits(worker);
        """
    )
    setup.close()

    WRITERS, READERS, PER_WRITER = 24, 24, 40
    errors: list[str] = []
    reads_done = [0]
    lock = threading.Lock()
    start = threading.Barrier(WRITERS + READERS)

    def writer(worker_id: int) -> None:
        con = db.connect(db_path)
        try:
            start.wait(timeout=30)
            for n in range(PER_WRITER):
                try:
                    con.execute(
                        "INSERT INTO hits (worker, note) VALUES (?, ?)",
                        (worker_id, f"row-{n}"),
                    )
                except sqlite3.OperationalError as exc:
                    with lock:
                        errors.append(f"writer {worker_id}: {exc}")
                time.sleep(random.uniform(0, 0.002))
        finally:
            con.close()

    def reader(_worker_id: int) -> None:
        con = db.connect(db_path)
        try:
            start.wait(timeout=30)
            for _ in range(PER_WRITER):
                try:
                    con.execute("SELECT COUNT(*) FROM hits").fetchone()
                    con.execute(
                        "SELECT * FROM hits WHERE worker = ? LIMIT 5",
                        (random.randint(0, WRITERS - 1),),
                    ).fetchall()
                    with lock:
                        reads_done[0] += 1
                except sqlite3.OperationalError as exc:
                    with lock:
                        errors.append(f"reader: {exc}")
                time.sleep(random.uniform(0, 0.002))
        finally:
            con.close()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(WRITERS)]
    threads += [threading.Thread(target=reader, args=(i,)) for i in range(READERS)]

    began = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    elapsed = time.time() - began

    con = db.connect(db_path)
    total = con.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
    con.close()

    expected = WRITERS * PER_WRITER
    writes_per_sec = total / elapsed if elapsed else 0

    print(f"        {WRITERS} writers + {READERS} readers, {elapsed:.2f}s")
    print(f"        {total}/{expected} writes, {reads_done[0]} read passes, "
          f"~{writes_per_sec:.0f} writes/sec")

    check("no writes were lost", total == expected, f"{total}/{expected}")
    check("no lock errors surfaced", not errors, "; ".join(errors[:3]))
    check("reads completed throughout", reads_done[0] >= READERS * PER_WRITER * 0.9,
          str(reads_done[0]))
    check("throughput is sane (>200 writes/sec)", writes_per_sec > 200,
          f"{writes_per_sec:.0f}/sec")


def test_retry_helper() -> None:
    print("\nRetry helper")
    calls = [0]

    def flaky():
        calls[0] += 1
        if calls[0] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    with app.app_context():
        check("retries a locked write and succeeds", db.with_retry(flaky) == "ok")
        check("retried the expected number of times", calls[0] == 3, str(calls[0]))

        def real_error():
            raise sqlite3.OperationalError("no such table: nope")

        try:
            db.with_retry(real_error)
            check("a genuine error is not retried away", False, "no exception raised")
        except sqlite3.OperationalError:
            check("a genuine error is not retried away", True)


# ==========================================================================
if __name__ == "__main__":
    print("=" * 66)
    print("Creator profiles + database concurrency")
    print("=" * 66)
    try:
        test_url_validation()
        test_permissions()
        test_profile_flow()
        test_public_rendering()
        test_click_counter()
        test_pragmas()
        test_indexes_used()
        test_concurrent_load()
        test_retry_helper()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 66)
    print(f"  {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
