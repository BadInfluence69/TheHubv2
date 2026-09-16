"""
Public chat rooms.

Unlike direct messages, room posts are stored in plain text. A room has an
open membership — anyone with an account here can walk in and read the
backlog — so there is no set of keys that only the participants hold, and
calling it encrypted would be dressing up a lock with no door.

The trade-off is stated on the privacy page instead of hidden: rooms are for
things you're happy for every account on this server to read, DMs are for
everything else.
"""
from __future__ import annotations

import re

from .. import db

MAX_LENGTH = 2000
MENTION_RE = re.compile(r"@([A-Za-z0-9_.-]{3,32})")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48] or "room"


# --------------------------------------------------------------------------
# Rooms
# --------------------------------------------------------------------------
def rooms(user_id: int | None = None) -> list[dict]:
    """All rooms with a post count and, for a given account, an unread count."""
    rows = db.query(
        """
        SELECT
            r.*,
            (SELECT COUNT(*) FROM chat_messages m
              WHERE m.room_id = r.id AND m.is_deleted = 0)  AS message_count,
            (SELECT MAX(m.id) FROM chat_messages m
              WHERE m.room_id = r.id)                       AS last_message_id,
            (SELECT MAX(m.created_at) FROM chat_messages m
              WHERE m.room_id = r.id AND m.is_deleted = 0)  AS last_at,
            COALESCE((SELECT cr.last_seen_id FROM chat_reads cr
                       WHERE cr.room_id = r.id AND cr.user_id = ?), 0) AS last_seen_id
        FROM chat_rooms r
        ORDER BY r.is_system DESC, r.name COLLATE NOCASE
        """,
        (user_id or 0,),
    )
    out = []
    for row in db.rows_to_dicts(rows):
        last_id = row.get("last_message_id") or 0
        seen = row.get("last_seen_id") or 0
        row["unread"] = max(0, last_id - seen) if user_id else 0
        out.append(row)
    return out


def by_slug(slug: str) -> dict | None:
    row = db.query_one("SELECT * FROM chat_rooms WHERE slug = ?", (slug,))
    return dict(row) if row else None


def create_room(name: str, topic: str, created_by: int) -> str:
    """Returns the slug. Appends a counter if the obvious slug is taken."""
    name = name.strip()[:60]
    base = _slugify(name)
    slug, suffix = base, 2
    while by_slug(slug):
        slug = f"{base}-{suffix}"
        suffix += 1

    db.insert(
        """
        INSERT INTO chat_rooms (slug, name, topic, created_by)
        VALUES (?, ?, ?, ?)
        """,
        (slug, name, topic.strip()[:200], created_by),
    )
    return slug


def update_room(room_id: int, name: str, topic: str) -> None:
    db.execute(
        "UPDATE chat_rooms SET name = ?, topic = ? WHERE id = ?",
        (name.strip()[:60], topic.strip()[:200], room_id),
    )


def set_locked(room_id: int, locked: bool) -> None:
    db.execute(
        "UPDATE chat_rooms SET is_locked = ? WHERE id = ?", (int(locked), room_id)
    )


def delete_room(room_id: int) -> bool:
    """System rooms are refused, so a server can't end up with no rooms at all."""
    if db.scalar("SELECT is_system FROM chat_rooms WHERE id = ?", (room_id,), default=0):
        return False
    db.execute("DELETE FROM chat_rooms WHERE id = ?", (room_id,))
    return True


# --------------------------------------------------------------------------
# Posts
# --------------------------------------------------------------------------
FIELDS = """
    m.id, m.room_id, m.user_id, m.username, m.body, m.created_at, m.is_deleted,
    u.display_name, u.avatar_hue
"""


def post(room_id: int, user: dict, body: str) -> int:
    return db.insert(
        "INSERT INTO chat_messages (room_id, user_id, username, body) VALUES (?, ?, ?, ?)",
        (room_id, user["id"], user["username"], body.strip()[:MAX_LENGTH]),
    )


def get_post(message_id: int) -> dict | None:
    row = db.query_one(
        f"SELECT {FIELDS} FROM chat_messages m LEFT JOIN users u ON u.id = m.user_id "
        "WHERE m.id = ?",
        (message_id,),
    )
    return dict(row) if row else None


def recent(room_id: int, limit: int = 100, before_id: int | None = None,
           hide_from: set[int] | None = None) -> list[dict]:
    """Oldest-first page of a room. Posts from blocked accounts are dropped."""
    if before_id:
        rows = db.query(
            f"""
            SELECT {FIELDS} FROM chat_messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.room_id = ? AND m.id < ?
            ORDER BY m.id DESC LIMIT ?
            """,
            (room_id, before_id, limit),
        )
    else:
        rows = db.query(
            f"""
            SELECT {FIELDS} FROM chat_messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.room_id = ?
            ORDER BY m.id DESC LIMIT ?
            """,
            (room_id, limit),
        )
    items = list(reversed(db.rows_to_dicts(rows)))
    if hide_from:
        items = [i for i in items if i["user_id"] not in hide_from]
    return items


def since(room_id: int, after_id: int, hide_from: set[int] | None = None) -> list[dict]:
    rows = db.query(
        f"""
        SELECT {FIELDS} FROM chat_messages m
        LEFT JOIN users u ON u.id = m.user_id
        WHERE m.room_id = ? AND m.id > ?
        ORDER BY m.id ASC LIMIT 200
        """,
        (room_id, after_id),
    )
    items = db.rows_to_dicts(rows)
    if hide_from:
        items = [i for i in items if i["user_id"] not in hide_from]
    return items


def delete_post(message_id: int, user_id: int, is_admin: bool = False) -> bool:
    owner = db.scalar("SELECT user_id FROM chat_messages WHERE id = ?", (message_id,))
    if owner != user_id and not is_admin:
        return False
    db.execute(
        "UPDATE chat_messages SET is_deleted = 1, body = '' WHERE id = ?", (message_id,)
    )
    return True


def active_posters(room_id: int, limit: int = 20) -> list[dict]:
    """Who has been talking in here lately, for the room sidebar."""
    rows = db.query(
        """
        SELECT u.id, u.username, u.display_name, u.avatar_hue,
               COUNT(*) AS posts, MAX(m.created_at) AS last_at
        FROM chat_messages m
        JOIN users u ON u.id = m.user_id
        WHERE m.room_id = ? AND m.is_deleted = 0
        GROUP BY u.id
        ORDER BY last_at DESC
        LIMIT ?
        """,
        (room_id, limit),
    )
    return db.rows_to_dicts(rows)


# --------------------------------------------------------------------------
# Read cursors and mentions
# --------------------------------------------------------------------------
def mark_seen(user_id: int, room_id: int, message_id: int) -> None:
    db.execute(
        """
        INSERT INTO chat_reads (user_id, room_id, last_seen_id) VALUES (?, ?, ?)
        ON CONFLICT(user_id, room_id) DO UPDATE
            SET last_seen_id = MAX(last_seen_id, excluded.last_seen_id)
        """,
        (user_id, room_id, message_id),
    )


def unread_total(user_id: int) -> int:
    return db.scalar(
        """
        SELECT COALESCE(SUM(unread), 0) FROM (
            SELECT MAX(m.id) - COALESCE(
                (SELECT cr.last_seen_id FROM chat_reads cr
                  WHERE cr.room_id = r.id AND cr.user_id = ?), 0) AS unread
            FROM chat_rooms r
            JOIN chat_messages m ON m.room_id = r.id
            GROUP BY r.id
        )
        WHERE unread > 0
        """,
        (user_id,),
        default=0,
    )


def mentioned_users(body: str, exclude_id: int) -> list[dict]:
    """
    Accounts named with @handle in a post.

    Only real accounts come back — an @ in front of a word that isn't anyone
    here is just an @ in front of a word.
    """
    handles = {h.lower() for h in MENTION_RE.findall(body or "")}
    if not handles:
        return []
    placeholders = ",".join("?" * len(handles))
    rows = db.query(
        f"""
        SELECT id, username, display_name FROM users
        WHERE LOWER(username) IN ({placeholders}) AND id != ?
        """,
        [*handles, exclude_id],
    )
    return db.rows_to_dicts(rows)
