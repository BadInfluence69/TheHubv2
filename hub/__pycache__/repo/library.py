"""Playlists, Watch later, watch history, and search history."""
from __future__ import annotations

import re
import unicodedata

from .. import db
from .videos import VIDEO_FIELDS


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:60] or "playlist"


# --------------------------------------------------------------------------
# Playlists
# --------------------------------------------------------------------------
def ensure_system_playlists(user_id: int) -> None:
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


def playlists_for(user_id: int) -> list[dict]:
    rows = db.query(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM playlist_items pi WHERE pi.playlist_id = p.id)
                   AS item_count,
               (SELECT v.thumbnail FROM playlist_items pi
                JOIN videos v ON v.video_id = pi.video_id
                WHERE pi.playlist_id = p.id AND v.thumbnail != ''
                ORDER BY pi.position LIMIT 1) AS cover
        FROM playlists p
        WHERE p.user_id = ?
        ORDER BY (p.system_kind IS NULL), p.updated_at DESC
        """,
        (user_id,),
    )
    return db.rows_to_dicts(rows)


def get_playlist(user_id: int, slug: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM playlists WHERE user_id = ? AND slug = ?", (user_id, slug)
    )
    return dict(row) if row else None


def get_playlist_by_id(playlist_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
    return dict(row) if row else None


def system_playlist(user_id: int, kind: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM playlists WHERE user_id = ? AND system_kind = ?",
        (user_id, kind),
    )
    return dict(row) if row else None


def create_playlist(
    user_id: int, title: str, description: str = "", visibility: str = "private"
) -> dict:
    base = slugify(title)
    slug = base
    suffix = 2
    while db.scalar(
        "SELECT 1 FROM playlists WHERE user_id = ? AND slug = ?", (user_id, slug)
    ):
        slug = f"{base}-{suffix}"
        suffix += 1

    db.insert(
        """
        INSERT INTO playlists (user_id, slug, title, description, visibility)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, slug, title.strip() or "Untitled playlist", description, visibility),
    )
    return get_playlist(user_id, slug)


def update_playlist(
    user_id: int, slug: str, title: str, description: str, visibility: str
) -> None:
    db.execute(
        """
        UPDATE playlists
        SET title = ?, description = ?, visibility = ?, updated_at = datetime('now')
        WHERE user_id = ? AND slug = ? AND system_kind IS NULL
        """,
        (title.strip(), description, visibility, user_id, slug),
    )


def delete_playlist(user_id: int, slug: str) -> None:
    db.execute(
        "DELETE FROM playlists WHERE user_id = ? AND slug = ? AND system_kind IS NULL",
        (user_id, slug),
    )


def playlist_videos(playlist_id: int) -> list[dict]:
    rows = db.query(
        f"""
        SELECT {VIDEO_FIELDS}, pi.position, pi.added_at, pi.id AS item_id
        FROM playlist_items pi
        JOIN videos v ON v.video_id = pi.video_id
        WHERE pi.playlist_id = ?
        ORDER BY pi.position, pi.id
        """,
        (playlist_id,),
    )
    return db.rows_to_dicts(rows)


def add_to_playlist(playlist_id: int, video_id: str) -> bool:
    next_pos = db.scalar(
        "SELECT COALESCE(MAX(position) + 1, 0) FROM playlist_items WHERE playlist_id = ?",
        (playlist_id,),
        default=0,
    )
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO playlist_items (playlist_id, video_id, position)
        VALUES (?, ?, ?)
        """,
        (playlist_id, video_id, next_pos),
    )
    db.execute(
        "UPDATE playlists SET updated_at = datetime('now') WHERE id = ?", (playlist_id,)
    )
    return cursor.rowcount > 0


def remove_from_playlist(playlist_id: int, video_id: str) -> None:
    db.execute(
        "DELETE FROM playlist_items WHERE playlist_id = ? AND video_id = ?",
        (playlist_id, video_id),
    )
    db.execute(
        "UPDATE playlists SET updated_at = datetime('now') WHERE id = ?", (playlist_id,)
    )


def reorder_playlist(playlist_id: int, video_ids: list[str]) -> None:
    for position, video_id in enumerate(video_ids):
        db.execute(
            "UPDATE playlist_items SET position = ? WHERE playlist_id = ? AND video_id = ?",
            (position, playlist_id, video_id),
        )


def playlists_containing(user_id: int, video_id: str) -> set[int]:
    return {
        row["playlist_id"]
        for row in db.query(
            """
            SELECT pi.playlist_id FROM playlist_items pi
            JOIN playlists p ON p.id = pi.playlist_id
            WHERE p.user_id = ? AND pi.video_id = ?
            """,
            (user_id, video_id),
        )
    }


def toggle_watch_later(user_id: int, video_id: str) -> bool:
    playlist = system_playlist(user_id, "watch_later")
    if not playlist:
        ensure_system_playlists(user_id)
        playlist = system_playlist(user_id, "watch_later")
    exists = db.scalar(
        "SELECT 1 FROM playlist_items WHERE playlist_id = ? AND video_id = ?",
        (playlist["id"], video_id),
    )
    if exists:
        remove_from_playlist(playlist["id"], video_id)
        return False
    add_to_playlist(playlist["id"], video_id)
    return True


# --------------------------------------------------------------------------
# Watch history
# --------------------------------------------------------------------------
def record_watch(user_id: int, video_id: str) -> int:
    """Log a view, collapsing repeats of the same video within ten minutes."""
    recent = db.query_one(
        """
        SELECT id FROM watch_history
        WHERE user_id = ? AND video_id = ?
          AND watched_at > datetime('now', '-10 minute')
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, video_id),
    )
    if recent:
        db.execute(
            "UPDATE watch_history SET watched_at = datetime('now') WHERE id = ?",
            (recent["id"],),
        )
        return recent["id"]
    return db.insert(
        "INSERT INTO watch_history (user_id, video_id) VALUES (?, ?)",
        (user_id, video_id),
    )


def save_progress(
    user_id: int, video_id: str, position: float, duration: float | None
) -> None:
    row = db.query_one(
        "SELECT id FROM watch_history WHERE user_id = ? AND video_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (user_id, video_id),
    )
    if not row:
        return
    db.execute(
        "UPDATE watch_history SET position_seconds = ?, duration_seconds = ? WHERE id = ?",
        (position, duration, row["id"]),
    )


def resume_position(user_id: int, video_id: str) -> float:
    position = db.scalar(
        """
        SELECT position_seconds FROM watch_history
        WHERE user_id = ? AND video_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, video_id),
        default=0.0,
    )
    # Don't resume something that was effectively finished.
    return float(position or 0.0)


def history(user_id: int, limit: int = 100, term: str = "") -> list[dict]:
    params: list = [user_id]
    where = "WHERE h.user_id = ?"
    if term:
        where += " AND (v.title LIKE ? OR v.channel_name LIKE ?)"
        params.extend([f"%{term}%", f"%{term}%"])
    params.append(limit)

    rows = db.query(
        f"""
        SELECT {VIDEO_FIELDS}, h.watched_at, h.position_seconds, h.duration_seconds,
               h.id AS history_id
        FROM watch_history h
        JOIN videos v ON v.video_id = h.video_id
        {where}
        ORDER BY h.watched_at DESC, h.id DESC
        LIMIT ?
        """,
        params,
    )
    return db.rows_to_dicts(rows)


def continue_watching(user_id: int, limit: int = 12) -> list[dict]:
    rows = db.query(
        f"""
        SELECT {VIDEO_FIELDS}, MAX(h.watched_at) AS watched_at,
               h.position_seconds, h.duration_seconds
        FROM watch_history h
        JOIN videos v ON v.video_id = h.video_id
        WHERE h.user_id = ?
          AND h.position_seconds > 30
          AND (h.duration_seconds IS NULL
               OR h.position_seconds < h.duration_seconds * 0.92)
        GROUP BY v.video_id
        ORDER BY watched_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    return db.rows_to_dicts(rows)


def delete_history_entry(user_id: int, history_id: int) -> None:
    db.execute(
        "DELETE FROM watch_history WHERE id = ? AND user_id = ?", (history_id, user_id)
    )


def clear_history(user_id: int) -> None:
    db.execute("DELETE FROM watch_history WHERE user_id = ?", (user_id,))


def watched_ids(user_id: int) -> set[str]:
    """
    Every video this account has already opened.

    The recommender subtracts this from anything it fetches, so a video you
    have seen never comes back round. Returned as a set because it is checked
    once per candidate.
    """
    return {
        row["video_id"]
        for row in db.query(
            "SELECT DISTINCT video_id FROM watch_history WHERE user_id = ?",
            (user_id,),
        )
    }


def interaction_signals(user_id: int, limit: int = 500) -> list[dict]:
    """
    The raw material for a preference profile: one row per thing you did.

    Watches and votes are pulled together, newest first, each carrying the
    video's channel, title and tags plus how long ago it happened. Age is
    computed in SQLite (in days, fractional) so nothing has to parse the
    stored timestamps back into datetimes.

    kind is 'watch' or 'vote'. For votes, `value` is 1 or -1. For watches,
    position_seconds / duration_seconds say how much of it you actually sat
    through, which is a much better signal than the click itself.
    """
    rows = db.query(
        """
        SELECT v.video_id, v.channel_key, v.channel_name, v.title, v.tags,
               'watch' AS kind,
               julianday('now') - julianday(h.watched_at) AS age_days,
               h.position_seconds, h.duration_seconds,
               0 AS value
        FROM watch_history h
        JOIN videos v ON v.video_id = h.video_id
        WHERE h.user_id = ?

        UNION ALL

        SELECT v.video_id, v.channel_key, v.channel_name, v.title, v.tags,
               'vote' AS kind,
               julianday('now') - julianday(vv.created_at) AS age_days,
               0 AS position_seconds, NULL AS duration_seconds,
               vv.value
        FROM video_votes vv
        JOIN videos v ON v.video_id = vv.video_id
        WHERE vv.user_id = ?

        ORDER BY age_days
        LIMIT ?
        """,
        (user_id, user_id, limit),
    )
    return db.rows_to_dicts(rows)


def history_titles(user_id: int, limit: int = 25) -> list[str]:
    return [
        row["title"]
        for row in db.query(
            """
            SELECT v.title FROM watch_history h
            JOIN videos v ON v.video_id = h.video_id
            WHERE h.user_id = ?
            ORDER BY h.watched_at DESC LIMIT ?
            """,
            (user_id, limit),
        )
    ]


# --------------------------------------------------------------------------
# Search history
# --------------------------------------------------------------------------
def record_search(user_id: int, term: str) -> None:
    term = term.strip()
    if not term:
        return
    db.execute(
        "DELETE FROM search_history WHERE user_id = ? AND query = ?", (user_id, term)
    )
    db.insert(
        "INSERT INTO search_history (user_id, query) VALUES (?, ?)", (user_id, term)
    )
    db.execute(
        """
        DELETE FROM search_history WHERE user_id = ? AND id NOT IN (
            SELECT id FROM search_history WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 40
        )
        """,
        (user_id, user_id),
    )


def recent_searches(user_id: int, limit: int = 10, prefix: str = "") -> list[str]:
    if prefix:
        rows = db.query(
            """
            SELECT query FROM search_history
            WHERE user_id = ? AND query LIKE ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, f"{prefix}%", limit),
        )
    else:
        rows = db.query(
            "SELECT query FROM search_history WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
    return [row["query"] for row in rows]


def clear_searches(user_id: int) -> None:
    db.execute("DELETE FROM search_history WHERE user_id = ?", (user_id,))
