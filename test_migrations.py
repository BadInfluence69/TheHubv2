#!/usr/bin/env python3
"""
Schema migration checks.

Guards the ordering bug that produced:

    sqlite3.OperationalError: no such column: response_video_id

schema.sql builds indexes on columns that were added to tables after their
first release. CREATE TABLE IF NOT EXISTS will not widen a table that already
exists, so on any older database those columns have to be added *before* the
schema script runs, not after. This asserts that databases at several stages
of age all upgrade cleanly and keep their contents.

    python test_migrations.py
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from hub import db

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")


def make_db(script: str = "") -> Path:
    path = Path(tempfile.mkdtemp()) / "hub.db"
    if script:
        con = sqlite3.connect(path)
        con.executescript(script)
        con.commit()
        con.close()
    return path


def columns(path: Path, table: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def indexes(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        con.close()


# The shape of a database from before video responses existed.
PRE_RESPONSES = """
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL,
    parent_id INTEGER,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Older still: several tables behind at once.
PRE_EVERYTHING = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE watch_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    watched_at TEXT
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL,
    parent_id INTEGER,
    body TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE videos (
    video_id TEXT PRIMARY KEY, source TEXT, title TEXT, channel_key TEXT,
    channel_name TEXT, thumbnail TEXT, description TEXT, duration INTEGER,
    published_at TEXT, file_path TEXT, view_count INTEGER DEFAULT 0,
    legacy_likes INTEGER DEFAULT 0, legacy_dislikes INTEGER DEFAULT 0,
    first_seen TEXT, last_seen TEXT
);
"""

WITH_DATA = PRE_EVERYTHING + """
INSERT INTO users (username, password_hash) VALUES ('aiden', 'stored-hash');
INSERT INTO videos (video_id, title, channel_name)
    VALUES ('abc123', 'A real video', 'Some Channel');
INSERT INTO comments (video_id, user_id, username, body)
    VALUES ('abc123', 1, 'aiden', 'my comment');
INSERT INTO watch_history (user_id, video_id, watched_at)
    VALUES (1, 'abc123', datetime('now'));
"""


def main() -> int:
    print("\nUpgrading databases of different ages")

    for label, script in (
        ("a brand new database", ""),
        ("one from before video responses", PRE_RESPONSES),
        ("one several versions behind", PRE_EVERYTHING),
    ):
        path = make_db(script)
        try:
            db.init_db(path)
            check(f"{label} upgrades without error", True)
        except Exception as exc:
            check(f"{label} upgrades without error", False,
                  f"{type(exc).__name__}: {exc}")
            continue

        check(f"  {label}: comments.response_video_id exists",
              "response_video_id" in columns(path, "comments"))
        check(f"  {label}: videos.tags exists", "tags" in columns(path, "videos"))
        check(f"  {label}: idx_comments_response built",
              "idx_comments_response" in indexes(path))

    print("\nEvery column the schema indexes must exist first")
    schema = (Path("hub") / "schema.sql").read_text(encoding="utf-8")
    path = make_db(PRE_EVERYTHING)
    db.init_db(path)
    import re
    missing = []
    for table, cols in re.findall(
        r"CREATE INDEX IF NOT EXISTS \w+\s+ON\s+(\w+)\s*\(([^)]+)\)", schema
    ):
        present = columns(path, table)
        for col in cols.split(","):
            col = col.strip().split()[0]
            if col and col not in present:
                missing.append(f"{table}.{col}")
    check("no index references a column that does not exist",
          not missing, ", ".join(missing) or "all present")

    print("\nExisting data survives")
    path = make_db(WITH_DATA)
    db.init_db(path)
    con = sqlite3.connect(path)
    try:
        check("accounts kept",
              con.execute("SELECT username FROM users").fetchall() == [("aiden",)])
        check("videos kept",
              con.execute("SELECT title FROM videos").fetchall() == [("A real video",)])
        check("comments kept",
              con.execute("SELECT body FROM comments").fetchall() == [("my comment",)])
        check("watch history kept",
              con.execute("SELECT video_id FROM watch_history").fetchall() == [("abc123",)])
        check("new columns default to empty rather than dropping the row",
              con.execute("SELECT response_video_id FROM comments").fetchone()[0] is None)
    finally:
        con.close()

    print("\nRunning it twice changes nothing")
    path = make_db(PRE_RESPONSES)
    db.init_db(path)
    before = (columns(path, "comments"), indexes(path))
    try:
        db.init_db(path)
        check("a second init_db is a no-op", (columns(path, "comments"), indexes(path)) == before)
    except Exception as exc:
        check("a second init_db is a no-op", False, f"{type(exc).__name__}: {exc}")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("Failures: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
