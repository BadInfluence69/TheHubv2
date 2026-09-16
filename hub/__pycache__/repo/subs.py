"""
Subscriptions.

Local only. Subscribing to a channel here records it in this database and
changes what shows up in your Subscriptions feed. Your real YouTube
subscriptions are untouched.
"""
from __future__ import annotations

from .. import db


def key_for(channel_name: str) -> str:
    return (channel_name or "").strip().lower()


def list_for(user_id: int) -> list[dict]:
    rows = db.query(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM videos v WHERE v.channel_key = s.channel_key)
                   AS video_count,
               (SELECT v.thumbnail FROM videos v
                WHERE v.channel_key = s.channel_key AND v.thumbnail != ''
                ORDER BY v.last_seen DESC LIMIT 1) AS latest_thumb
        FROM subscriptions s
        WHERE s.user_id = ?
        ORDER BY s.channel_name COLLATE NOCASE
        """,
        (user_id,),
    )
    return db.rows_to_dicts(rows)


def keys_for(user_id: int) -> list[str]:
    return [
        row["channel_key"]
        for row in db.query(
            "SELECT channel_key FROM subscriptions WHERE user_id = ?", (user_id,)
        )
    ]


def is_subscribed(user_id: int, channel_name: str) -> bool:
    return (
        db.scalar(
            "SELECT 1 FROM subscriptions WHERE user_id = ? AND channel_key = ?",
            (user_id, key_for(channel_name)),
        )
        is not None
    )


def get(user_id: int, channel_name: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM subscriptions WHERE user_id = ? AND channel_key = ?",
        (user_id, key_for(channel_name)),
    )
    return dict(row) if row else None


def subscribe(user_id: int, channel_name: str, thumbnail: str = "") -> None:
    name = (channel_name or "").strip()
    if not name:
        return
    db.execute(
        """
        INSERT INTO subscriptions (user_id, channel_key, channel_name, thumbnail)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, channel_key) DO UPDATE
            SET channel_name = excluded.channel_name
        """,
        (user_id, key_for(name), name, thumbnail),
    )


def unsubscribe(user_id: int, channel_name: str) -> None:
    db.execute(
        "DELETE FROM subscriptions WHERE user_id = ? AND channel_key = ?",
        (user_id, key_for(channel_name)),
    )


def toggle(user_id: int, channel_name: str, thumbnail: str = "") -> bool:
    """Returns True if now subscribed."""
    if is_subscribed(user_id, channel_name):
        unsubscribe(user_id, channel_name)
        return False
    subscribe(user_id, channel_name, thumbnail)
    return True


def set_notify(user_id: int, channel_name: str, notify: bool) -> None:
    db.execute(
        "UPDATE subscriptions SET notify = ? WHERE user_id = ? AND channel_key = ?",
        (int(notify), user_id, key_for(channel_name)),
    )


def count(user_id: int) -> int:
    return db.scalar(
        "SELECT COUNT(*) FROM subscriptions WHERE user_id = ?", (user_id,), default=0
    )


def subscriber_count(channel_name: str) -> int:
    """How many accounts on this server follow the channel."""
    return db.scalar(
        "SELECT COUNT(*) FROM subscriptions WHERE channel_key = ?",
        (key_for(channel_name),),
        default=0,
    )


def feed_videos(user_id: int, limit: int = 60) -> list[dict]:
    """Everything in the catalogue from channels this account follows."""
    from .videos import VIDEO_FIELDS

    rows = db.query(
        f"""
        SELECT {VIDEO_FIELDS} FROM videos v
        JOIN subscriptions s
          ON s.channel_key = v.channel_key AND s.user_id = ?
        ORDER BY v.last_seen DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    return db.rows_to_dicts(rows)
