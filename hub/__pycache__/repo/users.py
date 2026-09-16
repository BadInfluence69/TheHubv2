"""Accounts, per-account settings, and the notification bell."""
from __future__ import annotations

import zlib

from .. import db

DEFAULT_SETTINGS = {
    "theme": "dark",
    "autoplay": "1",
    "pause_history": "0",
    "default_quality": "auto",
    "restricted_mode": "0",
    "player_volume": "1",
}


def _hue_for(username: str) -> int:
    """Stable colour for an account's avatar, derived from the name."""
    return zlib.crc32(username.lower().encode("utf-8")) % 360


def create(username: str, password_hash: str, is_admin: bool = False) -> int:
    user_id = db.insert(
        """
        INSERT INTO users (username, password_hash, display_name, avatar_hue, is_admin)
        VALUES (?, ?, ?, ?, ?)
        """,
        (username, password_hash, username, _hue_for(username), int(is_admin)),
    )
    _create_system_playlists(user_id)
    return user_id


def _create_system_playlists(user_id: int) -> None:
    for slug, title, kind in (
        ("watch-later", "Watch later", "watch_later"),
        ("liked", "Liked videos", "liked"),
    ):
        db.execute(
            """
            INSERT OR IGNORE INTO playlists (user_id, slug, title, system_kind)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, slug, title, kind),
        )


def by_username(username: str):
    return db.query_one("SELECT * FROM users WHERE username = ?", (username,))


def by_id(user_id: int):
    return db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def count() -> int:
    return db.scalar("SELECT COUNT(*) FROM users", default=0)


def update_profile(user_id: int, display_name: str, bio: str) -> None:
    db.execute(
        "UPDATE users SET display_name = ?, bio = ? WHERE id = ?",
        (display_name.strip() or None, bio.strip(), user_id),
    )


def set_password(user_id: int, password_hash: str) -> None:
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
    )


def delete(user_id: int) -> None:
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def get_settings(user_id: int) -> dict:
    values = dict(DEFAULT_SETTINGS)
    for row in db.query(
        "SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,)
    ):
        values[row["key"]] = row["value"]
    return values


def set_setting(user_id: int, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
        """,
        (user_id, key, value),
    )


# --------------------------------------------------------------------------
# Notifications
#
# One inbox for everything: comment replies, finished uploads, video responses,
# new direct messages, room mentions. An account with five hundred unread rows
# needs to be able to cut that down to one kind, so every row is tagged and the
# view layer filters on the tag.
# --------------------------------------------------------------------------
KINDS = {
    "reply":     "Replies",
    "response":  "Video responses",
    "message":   "Messages",
    "mention":   "Mentions",
    "upload":    "Uploads",
    "info":      "Other",
}


def notify(
    user_id: int, title: str, body: str = "", url: str | None = None, kind: str = "info"
) -> int:
    return db.insert(
        """
        INSERT INTO notifications (user_id, kind, title, body, url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, kind, title, body, url),
    )


def notify_once(
    user_id: int, title: str, body: str = "", url: str | None = None,
    kind: str = "info", within_minutes: int = 30
) -> int | None:
    """
    Like notify(), but collapses a repeat.

    Twenty messages in a row from one person should not produce twenty bell
    badges — that is the failure mode that makes people stop reading the bell
    at all. If an unread row with the same kind and url already exists and is
    recent, its timestamp is bumped instead of a new row being written.
    """
    existing = db.query_one(
        """
        SELECT id FROM notifications
        WHERE user_id = ? AND kind = ? AND url IS ? AND is_read = 0
          AND created_at > datetime('now', ?)
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, kind, url, f"-{int(within_minutes)} minutes"),
    )
    if existing:
        db.execute(
            "UPDATE notifications SET created_at = datetime('now'), title = ?, body = ? "
            "WHERE id = ?",
            (title, body, existing["id"]),
        )
        return None
    return notify(user_id, title, body, url, kind)


def notifications(
    user_id: int, limit: int = 30, kind: str | None = None, unread_only: bool = False
) -> list[dict]:
    clauses = ["user_id = ?"]
    params: list = [user_id]
    if kind and kind in KINDS:
        clauses.append("kind = ?")
        params.append(kind)
    if unread_only:
        clauses.append("is_read = 0")
    params.append(limit)

    return db.rows_to_dicts(
        db.query(
            f"""
            SELECT * FROM notifications
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        )
    )


def unread_count(user_id: int) -> int:
    return db.scalar(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
        (user_id,),
        default=0,
    )


def unread_by_kind(user_id: int) -> dict[str, int]:
    """Per-tag unread counts, for the filter chips on the notifications page."""
    counts = {key: 0 for key in KINDS}
    for row in db.query(
        "SELECT kind, COUNT(*) AS n FROM notifications "
        "WHERE user_id = ? AND is_read = 0 GROUP BY kind",
        (user_id,),
    ):
        counts[row["kind"]] = counts.get(row["kind"], 0) + row["n"]
    return counts


def total_by_kind(user_id: int) -> dict[str, int]:
    counts = {key: 0 for key in KINDS}
    for row in db.query(
        "SELECT kind, COUNT(*) AS n FROM notifications WHERE user_id = ? GROUP BY kind",
        (user_id,),
    ):
        counts[row["kind"]] = counts.get(row["kind"], 0) + row["n"]
    return counts


def mark_read(
    user_id: int, notification_id: int | None = None, kind: str | None = None
) -> None:
    if notification_id is not None:
        db.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND id = ?",
            (user_id, notification_id),
        )
    elif kind and kind in KINDS:
        db.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND kind = ?",
            (user_id, kind),
        )
    else:
        db.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,)
        )


def clear_notifications(user_id: int, kind: str | None = None) -> None:
    if kind and kind in KINDS:
        db.execute(
            "DELETE FROM notifications WHERE user_id = ? AND kind = ?", (user_id, kind)
        )
    else:
        db.execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))


def trim_notifications(user_id: int, keep: int = 500) -> None:
    """
    Caps the inbox so it can't grow without bound on a busy server.

    Called after writing a notification. Read rows go first; if there are still
    too many, the oldest go regardless of state.
    """
    db.execute(
        """
        DELETE FROM notifications
        WHERE user_id = ? AND id NOT IN (
            SELECT id FROM notifications WHERE user_id = ?
            ORDER BY is_read ASC, created_at DESC, id DESC LIMIT ?
        )
        """,
        (user_id, user_id, keep),
    )
