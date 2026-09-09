"""
Database access and schema management.

One SQLite connection per request, handed out through flask.g. WAL mode is on
so the background upload worker can write while a page is being rendered.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from flask import current_app, g

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Tuning
#
# Read the honest version of this before turning any of it up: SQLite allows
# any number of concurrent READERS but exactly ONE writer at a time, database
# -wide. WAL mode means that single writer no longer blocks readers, which is
# most of the win and is why it's on. It does not make writes parallel, and no
# pragma will. If this server ever reaches sustained write concurrency that a
# single writer can't absorb, the answer is Postgres, not a bigger cache.
#
# What that ceiling looks like in practice, on ordinary hardware:
#
#   * Reads scale to thousands per second and are effectively free. A feed page
#     or a watch page is almost all reads, so heavy viewing traffic is fine.
#   * Writes commit in well under a millisecond each with synchronous=NORMAL,
#     so a few hundred writes a second is comfortable. Writes here are small
#     and infrequent per viewer — a watch-progress row, a vote, a comment.
#   * The failure mode is not slowness, it's SQLITE_BUSY: a writer that waited
#     longer than busy_timeout and gave up. That's what the retry helper below
#     is for, and what to watch in the logs.
#
# A realistic estimate for this schema: several hundred simultaneous viewers is
# unremarkable, a few thousand is reachable with the settings below, and past
# that the bottleneck stops being the database anyway — it becomes bandwidth
# for the video bytes themselves, which is a CDN problem, not a SQLite one.
# --------------------------------------------------------------------------

# Page cache per connection. Negative means KiB rather than pages, so this is
# 64 MB of hot pages held in memory instead of re-read from disk.
CACHE_SIZE_KIB = -64_000

# Memory-mapped I/O window (256 MB). Lets SQLite read pages without a syscall
# per page, which is the single biggest read win on a database this shape.
MMAP_SIZE = 268_435_456

# How much WAL is allowed to build before a checkpoint folds it back into the
# main file. The default (1000 pages, ~4 MB) checkpoints often enough to stall
# writers under load; a larger window trades disk space for smoother writes.
WAL_AUTOCHECKPOINT = 4000

# How long a blocked writer waits for the lock before giving up. Generous on
# purpose — waiting 20 seconds beats failing a comment post.
BUSY_TIMEOUT_MS = 20_000


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a tuned connection. Callers outside a request use this directly."""
    con = sqlite3.connect(
        str(db_path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        # Connections are handed out per request via flask.g, but background
        # upload threads and the WAL checkpointer also hold one. SQLite itself
        # is compiled thread-safe; this just stops Python's own guard from
        # refusing a connection used by the thread that took it.
        check_same_thread=False,
    )
    con.row_factory = sqlite3.Row

    # WAL survives across connections once set, but setting it every time costs
    # nothing and means a database restored from a backup comes up correct.
    con.execute("PRAGMA journal_mode = WAL")

    # NORMAL fsyncs at checkpoints rather than on every commit. In WAL mode the
    # documented risk is losing the last few commits if the machine loses power
    # mid-write — not corruption. Worth it; FULL roughly halves write
    # throughput. Set SQLITE_SYNCHRONOUS=FULL in .env if you'd rather not.
    con.execute("PRAGMA synchronous = NORMAL")

    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    con.execute(f"PRAGMA cache_size = {CACHE_SIZE_KIB}")
    con.execute(f"PRAGMA mmap_size = {MMAP_SIZE}")
    con.execute(f"PRAGMA wal_autocheckpoint = {WAL_AUTOCHECKPOINT}")

    # Temp tables and sort scratch space in RAM rather than on disk. ORDER BY
    # over the feed queries is the main beneficiary.
    con.execute("PRAGMA temp_store = MEMORY")

    return con


def get_db() -> sqlite3.Connection:
    """The connection for the current request, created on first use."""
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    con = g.pop("db", None)
    if con is not None:
        con.close()


# --------------------------------------------------------------------------
# Small query helpers - these keep the repo modules readable
# --------------------------------------------------------------------------
def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return get_db().execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return get_db().execute(sql, params).fetchone()


def scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    row = query_one(sql, params)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


# --------------------------------------------------------------------------
# Write retries
#
# busy_timeout already makes a blocked writer wait rather than fail, so this
# only catches what's left: the case where the timeout itself expired under a
# burst. Retrying with a little randomised backoff turns a hard error into a
# short pause. Without it, the symptom users see is a comment or a vote that
# silently doesn't save.
# --------------------------------------------------------------------------
WRITE_RETRIES = 4
RETRY_BASE_DELAY = 0.05


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def with_retry(operation: Callable[[], Any]) -> Any:
    """
    Run a write, retrying briefly if the database was locked.

    The backoff is randomised so that several blocked writers don't all wake up
    and collide again on the same tick.
    """
    last: sqlite3.OperationalError | None = None
    for attempt in range(WRITE_RETRIES):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise
            last = exc
            if attempt == WRITE_RETRIES - 1:
                break
            delay = RETRY_BASE_DELAY * (2**attempt) * (0.5 + random.random())
            log.warning(
                "Database busy (attempt %d/%d), retrying in %.0f ms",
                attempt + 1,
                WRITE_RETRIES,
                delay * 1000,
            )
            time.sleep(delay)

    log.error("Write failed after %d attempts: %s", WRITE_RETRIES, last)
    raise last  # type: ignore[misc]


def execute(sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    return with_retry(lambda: get_db().execute(sql, params))


def execute_many(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    # Materialised because a generator can't be replayed on a retry.
    rows = list(seq)
    with_retry(lambda: get_db().executemany(sql, rows))


def insert(sql: str, params: Sequence[Any] = ()) -> int:
    """Run an INSERT and return the new rowid."""
    return with_retry(lambda: get_db().execute(sql, params).lastrowid)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Schema setup
# --------------------------------------------------------------------------
def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _add_missing_columns(
    con: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    existing = _columns(con, table)
    if not existing:
        return
    for name, ddl in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# Columns added to a table after its first release. Each entry is applied
# before the schema script runs, because that script builds indexes on some of
# them - idx_comments_response needs comments.response_video_id to exist, and
# CREATE TABLE IF NOT EXISTS will not add a column to a table that is already
# there. Getting this order wrong is what produced:
#
#     sqlite3.OperationalError: no such column: response_video_id
#
# on any database created before video responses were added.
LATE_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "display_name": "TEXT",
        "bio": "TEXT DEFAULT ''",
        "avatar_hue": "INTEGER DEFAULT 0",
        "is_admin": "INTEGER NOT NULL DEFAULT 0",
    },
    "watch_history": {
        "position_seconds": "REAL NOT NULL DEFAULT 0",
        "duration_seconds": "REAL",
    },
    # Video responses arrived after the first databases were created.
    "comments": {"response_video_id": "TEXT"},
    # Video tags arrived with the recommendation engine.
    "videos": {"tags": "TEXT DEFAULT ''"},
}


def _apply_column_migrations(con: sqlite3.Connection) -> None:
    """
    Bring every existing table up to the current column list.

    A no-op for tables that do not exist yet - those get created complete by
    the schema script - so this is safe to call on a brand new database.
    """
    for table, columns in LATE_COLUMNS.items():
        _add_missing_columns(con, table, columns)


def init_db(db_path: str | Path) -> None:
    """Create the database if needed, migrating any older copy in place."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = connect(db_path)
    try:
        _rename_legacy_tables(con)

        # Order matters. Existing tables are widened first, then the schema
        # script runs - it creates anything missing and builds the indexes,
        # some of which sit on columns added just above.
        _apply_column_migrations(con)

        try:
            con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        except sqlite3.OperationalError as exc:
            raise sqlite3.OperationalError(
                f"Could not bring {db_path} up to the current schema: {exc}.\n"
                "This usually means the database predates a column the schema "
                "indexes. Add that column to LATE_COLUMNS in hub/db.py, or "
                "move the file aside and let a fresh one be created."
            ) from exc

        # Again, for any table the script has only just created.
        _apply_column_migrations(con)

        _import_legacy_data(con)
        _ensure_system_playlists(con)
        _ensure_default_rooms(con)
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# Legacy migration
#
# The original single-file app used a flatter schema. Rather than dropping any
# of it, tables with the old shape are renamed to legacy_* and their contents
# copied into the new tables. The legacy_* tables are left in the file as a
# backup - delete them yourself once you're happy everything came across.
# --------------------------------------------------------------------------
LEGACY_SIGNATURES = {
    # table: (column that only exists in the OLD schema,
    #         column that only exists in the NEW schema)
    "videos": ("channel", "channel_name"),
    "comments": ("comment_text", "body"),
    "feedback": ("timestamp", "created_at"),
    "feedback_comments": ("comment_text", "body"),
}


def _rename_legacy_tables(con: sqlite3.Connection) -> None:
    for table, (old_col, new_col) in LEGACY_SIGNATURES.items():
        cols = _columns(con, table)
        if old_col in cols and new_col not in cols:
            con.execute(f"DROP TABLE IF EXISTS legacy_{table}")
            con.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")

    # Subscriptions were a single global list with no owner.
    cols = _columns(con, "subscriptions")
    if cols and "user_id" not in cols:
        con.execute("DROP TABLE IF EXISTS legacy_subscriptions")
        con.execute("ALTER TABLE subscriptions RENAME TO legacy_subscriptions")


def _already_migrated(con: sqlite3.Connection, key: str) -> bool:
    row = con.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row is not None


def _mark_migrated(con: sqlite3.Connection, key: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, datetime('now'))",
        (key,),
    )


def _import_legacy_data(con: sqlite3.Connection) -> None:
    if _already_migrated(con, "legacy_import"):
        return

    # ---- videos ----------------------------------------------------------
    if _table_exists(con, "legacy_videos"):
        con.execute(
            """
            INSERT OR IGNORE INTO videos
                (video_id, source, title, channel_key, channel_name,
                 thumbnail, description, legacy_likes, legacy_dislikes)
            SELECT
                video_id,
                CASE
                    WHEN video_id LIKE 'local_%' THEN 'local'
                    WHEN video_id LIKE 'tubi_%'  THEN 'tubi'
                    ELSE 'youtube'
                END,
                COALESCE(title, 'Untitled'),
                LOWER(TRIM(COALESCE(channel, ''))),
                channel,
                thumbnail,
                COALESCE(description, ''),
                COALESCE(likes_count, 0),
                COALESCE(dislikes_count, 0)
            FROM legacy_videos
            """
        )

    # ---- comments --------------------------------------------------------
    if _table_exists(con, "legacy_comments"):
        con.execute(
            """
            INSERT INTO comments (video_id, user_id, username, body, created_at)
            SELECT
                c.video_id,
                (SELECT u.id FROM users u WHERE u.username = c.username),
                COALESCE(c.username, 'Anonymous'),
                c.comment_text,
                COALESCE(c.timestamp, datetime('now'))
            FROM legacy_comments c
            WHERE c.comment_text IS NOT NULL AND TRIM(c.comment_text) != ''
            """
        )

    # ---- subscriptions ---------------------------------------------------
    # The old list was shared by everyone, so give a copy to every account.
    if _table_exists(con, "legacy_subscriptions"):
        con.execute(
            """
            INSERT OR IGNORE INTO subscriptions (user_id, channel_key, channel_name)
            SELECT u.id, LOWER(TRIM(s.channel_name)), s.channel_name
            FROM legacy_subscriptions s
            CROSS JOIN users u
            WHERE s.channel_name IS NOT NULL AND TRIM(s.channel_name) != ''
            """
        )

    # ---- feedback --------------------------------------------------------
    if _table_exists(con, "legacy_feedback"):
        con.execute(
            """
            INSERT INTO feedback (id, user_id, username, title, suggestion,
                                  upvotes, created_at)
            SELECT id, user_id, COALESCE(username, 'Anonymous'),
                   COALESCE(title, 'Untitled'), COALESCE(suggestion, ''),
                   COALESCE(upvotes, 0), COALESCE(timestamp, datetime('now'))
            FROM legacy_feedback
            """
        )

    if _table_exists(con, "legacy_feedback_comments"):
        con.execute(
            """
            INSERT INTO feedback_comments (feedback_id, user_id, username, body, created_at)
            SELECT fc.feedback_id,
                   (SELECT u.id FROM users u WHERE u.username = fc.username),
                   COALESCE(fc.username, 'Anonymous'),
                   fc.comment_text,
                   COALESCE(fc.timestamp, datetime('now'))
            FROM legacy_feedback_comments fc
            WHERE fc.feedback_id IN (SELECT id FROM feedback)
            """
        )

    _mark_migrated(con, "legacy_import")


def _ensure_system_playlists(con: sqlite3.Connection) -> None:
    """
    Every account gets a Watch later and a Liked videos playlist. New accounts
    get them at sign-up; this covers accounts that already existed.
    """
    for slug, title, kind in (
        ("watch-later", "Watch later", "watch_later"),
        ("liked", "Liked videos", "liked"),
    ):
        con.execute(
            """
            INSERT OR IGNORE INTO playlists (user_id, slug, title, system_kind)
            SELECT id, ?, ?, ? FROM users
            """,
            (slug, title, kind),
        )


def _ensure_default_rooms(con: sqlite3.Connection) -> None:
    """
    A brand new server with an empty room list is a dead end — nobody starts a
    conversation in a room that doesn't exist yet. Three rooms are created up
    front. They are ordinary rooms apart from is_system, which only stops them
    being deleted out from under everyone.
    """
    for slug, name, topic in (
        ("lobby", "Lobby", "Anything goes. Say hello."),
        ("what-were-watching", "What we're watching",
         "Post what you're watching and why it's worth someone's time."),
        ("help", "Help & requests",
         "Something broken, or a video you can't find? Ask here."),
    ):
        con.execute(
            """
            INSERT OR IGNORE INTO chat_rooms (slug, name, topic, is_system)
            VALUES (?, ?, ?, 1)
            """,
            (slug, name, topic),
        )


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
