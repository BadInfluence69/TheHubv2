"""
The video catalogue and the local like/dislike ledger.

Vote counts are always derived from the video_votes table rather than kept as
a running total, so they can never drift. Counts carried over from the older
database are preserved in legacy_likes / legacy_dislikes and added on top.
"""
from __future__ import annotations

import json

from .. import db

VIDEO_FIELDS = """
    v.video_id, v.source, v.title, v.channel_key, v.channel_name,
    v.thumbnail, v.description, v.tags, v.duration, v.published_at, v.file_path,
    v.view_count, v.first_seen, v.last_seen,
    v.legacy_likes + (
        SELECT COUNT(*) FROM video_votes vv
        WHERE vv.video_id = v.video_id AND vv.value = 1
    ) AS likes,
    v.legacy_dislikes + (
        SELECT COUNT(*) FROM video_votes vv
        WHERE vv.video_id = v.video_id AND vv.value = -1
    ) AS dislikes,
    (SELECT COUNT(*) FROM comments c
     WHERE c.video_id = v.video_id AND c.is_deleted = 0) AS comment_count
"""


def channel_key(name: str | None) -> str:
    return (name or "").strip().lower()


def encode_tags(tags) -> str:
    """Store a video's own tags as a JSON array, lowercased and de-duplicated."""
    if not tags:
        return ""
    if isinstance(tags, str):
        tags = parse_tags(tags)
    cleaned, seen = [], set()
    for tag in tags:
        tag = str(tag).strip().lower()
        if tag and tag not in seen and len(tag) <= 60:
            seen.add(tag)
            cleaned.append(tag)
    return json.dumps(cleaned[:25]) if cleaned else ""


def parse_tags(raw) -> list[str]:
    """
    Read tags back out. Tolerates the JSON we write, plus comma- or
    newline-separated text, so a hand-edited database still loads.
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t).strip().lower() for t in raw if str(t).strip()]
    raw = str(raw).strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t).strip().lower() for t in parsed if str(t).strip()]
        except json.JSONDecodeError:
            pass
    separator = "," if "," in raw else "\n"
    return [part.strip().lower() for part in raw.split(separator) if part.strip()]


def upsert(
    video_id: str,
    title: str,
    channel_name: str = "",
    thumbnail: str = "",
    description: str = "",
    tags=None,
    source: str = "youtube",
    duration: int | None = None,
    published_at: str | None = None,
    file_path: str | None = None,
) -> None:
    """Record or refresh a video. Safe to call on every search result."""
    db.execute(
        """
        INSERT INTO videos (video_id, source, title, channel_key, channel_name,
                            thumbnail, description, tags, duration, published_at,
                            file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            title        = excluded.title,
            channel_key  = excluded.channel_key,
            channel_name = excluded.channel_name,
            thumbnail    = COALESCE(NULLIF(excluded.thumbnail, ''), videos.thumbnail),
            description  = COALESCE(NULLIF(excluded.description, ''), videos.description),
            tags         = COALESCE(NULLIF(excluded.tags, ''), videos.tags),
            duration     = COALESCE(excluded.duration, videos.duration),
            published_at = COALESCE(excluded.published_at, videos.published_at),
            file_path    = COALESCE(excluded.file_path, videos.file_path),
            source       = excluded.source,
            last_seen    = datetime('now')
        """,
        (
            video_id,
            source,
            title or "Untitled",
            channel_key(channel_name),
            channel_name or "",
            thumbnail or "",
            description or "",
            encode_tags(tags),
            duration,
            published_at,
            file_path,
        ),
    )


def upsert_many(items: list[dict]) -> None:
    for item in items:
        upsert(
            video_id=item["id"],
            title=item.get("title", "Untitled"),
            channel_name=item.get("channel", ""),
            thumbnail=item.get("thumbnail", ""),
            description=item.get("description", ""),
            tags=item.get("tags"),
            source=item.get("source", "youtube"),
            duration=item.get("duration"),
            published_at=item.get("published_at"),
            file_path=item.get("file_path"),
        )


def get(video_id: str) -> dict | None:
    row = db.query_one(
        f"SELECT {VIDEO_FIELDS} FROM videos v WHERE v.video_id = ?", (video_id,)
    )
    return dict(row) if row else None


def get_many(video_ids: list[str]) -> dict[str, dict]:
    if not video_ids:
        return {}
    placeholders = ",".join("?" * len(video_ids))
    rows = db.query(
        f"SELECT {VIDEO_FIELDS} FROM videos v WHERE v.video_id IN ({placeholders})",
        video_ids,
    )
    return {row["video_id"]: dict(row) for row in rows}


def record_view(video_id: str) -> None:
    db.execute(
        "UPDATE videos SET view_count = view_count + 1, last_seen = datetime('now') "
        "WHERE video_id = ?",
        (video_id,),
    )


def recent(limit: int = 48, source: str | None = None) -> list[dict]:
    if source:
        rows = db.query(
            f"SELECT {VIDEO_FIELDS} FROM videos v WHERE v.source = ? "
            "ORDER BY v.last_seen DESC LIMIT ?",
            (source, limit),
        )
    else:
        rows = db.query(
            f"SELECT {VIDEO_FIELDS} FROM videos v ORDER BY v.last_seen DESC LIMIT ?",
            (limit,),
        )
    return db.rows_to_dicts(rows)


def trending(limit: int = 48) -> list[dict]:
    """Most engaged-with videos in the local catalogue over the last month."""
    rows = db.query(
        f"""
        SELECT {VIDEO_FIELDS},
            (SELECT COUNT(*) FROM video_votes vv
             WHERE vv.video_id = v.video_id
               AND vv.created_at > datetime('now', '-30 day')) * 3
          + (SELECT COUNT(*) FROM comments c
             WHERE c.video_id = v.video_id
               AND c.created_at > datetime('now', '-30 day')) * 2
          + v.view_count AS heat
        FROM videos v
        ORDER BY heat DESC, v.last_seen DESC
        LIMIT ?
        """,
        (limit,),
    )
    return db.rows_to_dicts(rows)


def search_catalogue(term: str, limit: int = 60) -> list[dict]:
    like = f"%{term}%"
    rows = db.query(
        f"""
        SELECT {VIDEO_FIELDS} FROM videos v
        WHERE v.title LIKE ? OR v.channel_name LIKE ? OR v.description LIKE ?
        ORDER BY v.last_seen DESC
        LIMIT ?
        """,
        (like, like, like, limit),
    )
    return db.rows_to_dicts(rows)


def by_channel(key: str, limit: int = 60) -> list[dict]:
    rows = db.query(
        f"SELECT {VIDEO_FIELDS} FROM videos v WHERE v.channel_key = ? "
        "ORDER BY v.last_seen DESC LIMIT ?",
        (key, limit),
    )
    return db.rows_to_dicts(rows)


def channels(limit: int = 200) -> list[dict]:
    rows = db.query(
        """
        SELECT channel_key, MAX(channel_name) AS channel_name,
               COUNT(*) AS video_count, MAX(thumbnail) AS thumbnail
        FROM videos
        WHERE channel_key IS NOT NULL AND channel_key != ''
        GROUP BY channel_key
        ORDER BY video_count DESC
        LIMIT ?
        """,
        (limit,),
    )
    return db.rows_to_dicts(rows)


def catalogue_size() -> int:
    return db.scalar("SELECT COUNT(*) FROM videos", default=0)


# --------------------------------------------------------------------------
# Votes
# --------------------------------------------------------------------------
def get_vote(user_id: int, video_id: str) -> int:
    return db.scalar(
        "SELECT value FROM video_votes WHERE user_id = ? AND video_id = ?",
        (user_id, video_id),
        default=0,
    )


def set_vote(user_id: int, video_id: str, value: int) -> int:
    """
    Apply a vote and return the vote now in effect.
    Pressing the same button twice removes the vote, like YouTube does.
    """
    current = get_vote(user_id, video_id)

    if value == 0 or current == value:
        db.execute(
            "DELETE FROM video_votes WHERE user_id = ? AND video_id = ?",
            (user_id, video_id),
        )
        new_value = 0
    else:
        db.execute(
            """
            INSERT INTO video_votes (user_id, video_id, value) VALUES (?, ?, ?)
            ON CONFLICT(user_id, video_id) DO UPDATE
                SET value = excluded.value, created_at = datetime('now')
            """,
            (user_id, video_id, value),
        )
        new_value = value

    _sync_liked_playlist(user_id, video_id, new_value == 1)
    return new_value


def _sync_liked_playlist(user_id: int, video_id: str, liked: bool) -> None:
    """Keep the automatic "Liked videos" playlist in step with likes."""
    playlist_id = db.scalar(
        "SELECT id FROM playlists WHERE user_id = ? AND system_kind = 'liked'",
        (user_id,),
    )
    if not playlist_id:
        return
    if liked:
        db.execute(
            """
            INSERT OR IGNORE INTO playlist_items (playlist_id, video_id, position)
            VALUES (?, ?, COALESCE(
                (SELECT MAX(position) + 1 FROM playlist_items WHERE playlist_id = ?), 0))
            """,
            (playlist_id, video_id, playlist_id),
        )
    else:
        db.execute(
            "DELETE FROM playlist_items WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )


def counts(video_id: str) -> dict:
    row = db.query_one(
        """
        SELECT
            (SELECT legacy_likes FROM videos WHERE video_id = ?) +
            (SELECT COUNT(*) FROM video_votes WHERE video_id = ? AND value = 1) AS likes,
            (SELECT legacy_dislikes FROM videos WHERE video_id = ?) +
            (SELECT COUNT(*) FROM video_votes WHERE video_id = ? AND value = -1) AS dislikes
        """,
        (video_id, video_id, video_id, video_id),
    )
    if not row:
        return {"likes": 0, "dislikes": 0}
    return {"likes": row["likes"] or 0, "dislikes": row["dislikes"] or 0}


def attach_user_state(videos: list[dict], user_id: int | None) -> list[dict]:
    """Fold each video's local like state and saved status into the dicts."""
    if not videos:
        return videos
    if not user_id:
        for video in videos:
            video.setdefault("my_vote", 0)
            video.setdefault("in_watch_later", False)
        return videos

    ids = [v.get("video_id") or v.get("id") for v in videos]
    ids = [i for i in ids if i]
    if not ids:
        return videos
    placeholders = ",".join("?" * len(ids))

    votes = {
        row["video_id"]: row["value"]
        for row in db.query(
            f"SELECT video_id, value FROM video_votes "
            f"WHERE user_id = ? AND video_id IN ({placeholders})",
            [user_id, *ids],
        )
    }
    saved = {
        row["video_id"]
        for row in db.query(
            f"""
            SELECT pi.video_id FROM playlist_items pi
            JOIN playlists p ON p.id = pi.playlist_id
            WHERE p.user_id = ? AND p.system_kind = 'watch_later'
              AND pi.video_id IN ({placeholders})
            """,
            [user_id, *ids],
        )
    }
    for video in videos:
        vid = video.get("video_id") or video.get("id")
        video["my_vote"] = votes.get(vid, 0)
        video["in_watch_later"] = vid in saved
    return videos
