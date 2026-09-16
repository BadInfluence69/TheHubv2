"""
Local comments. Threaded one level deep, exactly like YouTube: top-level
comments, each with a reply list.

These never touch YouTube. Nothing written here is visible to anybody outside
this server.
"""
from __future__ import annotations

from .. import db

MAX_LENGTH = 8000

FIELDS = """
    c.id, c.video_id, c.user_id, c.username, c.parent_id, c.body,
    c.response_video_id, c.created_at, c.edited_at, c.is_deleted, c.is_pinned,
    u.display_name, u.avatar_hue,
    rv.title      AS response_title,
    rv.thumbnail  AS response_thumbnail,
    rv.duration   AS response_duration,
    rv.source     AS response_source,
    (SELECT COUNT(*) FROM comment_votes cv
     WHERE cv.comment_id = c.id AND cv.value = 1) AS likes,
    (SELECT COUNT(*) FROM comment_votes cv
     WHERE cv.comment_id = c.id AND cv.value = -1) AS dislikes,
    (SELECT COUNT(*) FROM comments r
     WHERE r.parent_id = c.id AND r.is_deleted = 0) AS reply_count
"""

JOINS = """
    LEFT JOIN users u   ON u.id = c.user_id
    LEFT JOIN videos rv ON rv.video_id = c.response_video_id
"""

SORTS = {
    "top": "c.is_pinned DESC, likes DESC, c.id DESC",
    "newest": "c.is_pinned DESC, c.id DESC",
    "oldest": "c.is_pinned DESC, c.id ASC",
    # Video responses first, so a video with a lot of them reads as a thread
    # rather than burying them under text.
    "responses": "c.is_pinned DESC, (c.response_video_id IS NULL), c.id DESC",
}


def add(
    video_id: str,
    user: dict,
    body: str,
    parent_id: int | None = None,
    response_video_id: str | None = None,
) -> int:
    body = body.strip()[:MAX_LENGTH]
    if parent_id is not None:
        # Only one level of nesting: a reply to a reply attaches to its parent.
        root = db.scalar(
            "SELECT COALESCE(parent_id, id) FROM comments WHERE id = ?", (parent_id,)
        )
        parent_id = root
    return db.insert(
        """
        INSERT INTO comments (video_id, user_id, username, parent_id, body,
                              response_video_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (video_id, user["id"], user["username"], parent_id, body, response_video_id),
    )


def get(comment_id: int) -> dict | None:
    row = db.query_one(
        f"SELECT {FIELDS} FROM comments c {JOINS} WHERE c.id = ?",
        (comment_id,),
    )
    return dict(row) if row else None


def for_video(
    video_id: str, user_id: int | None = None, sort: str = "top"
) -> list[dict]:
    """Top-level comments, each with its replies nested under `replies`."""
    order = SORTS.get(sort, SORTS["top"])
    rows = db.query(
        f"""
        SELECT {FIELDS} FROM comments c {JOINS}
        WHERE c.video_id = ? AND c.parent_id IS NULL
        ORDER BY {order}
        """,
        (video_id,),
    )
    tops = db.rows_to_dicts(rows)
    if not tops:
        return []

    reply_rows = db.query(
        f"""
        SELECT {FIELDS} FROM comments c {JOINS}
        WHERE c.video_id = ? AND c.parent_id IS NOT NULL
        ORDER BY c.id ASC
        """,
        (video_id,),
    )
    replies: dict[int, list[dict]] = {}
    for row in reply_rows:
        replies.setdefault(row["parent_id"], []).append(dict(row))

    for comment in tops:
        comment["replies"] = replies.get(comment["id"], [])

    _attach_votes(tops, user_id)
    return tops


def _attach_votes(comments: list[dict], user_id: int | None) -> None:
    flat = []
    for comment in comments:
        flat.append(comment)
        flat.extend(comment.get("replies", []))

    for comment in flat:
        comment["my_vote"] = 0

    if not user_id or not flat:
        return

    ids = [c["id"] for c in flat]
    placeholders = ",".join("?" * len(ids))
    votes = {
        row["comment_id"]: row["value"]
        for row in db.query(
            f"SELECT comment_id, value FROM comment_votes "
            f"WHERE user_id = ? AND comment_id IN ({placeholders})",
            [user_id, *ids],
        )
    }
    for comment in flat:
        comment["my_vote"] = votes.get(comment["id"], 0)


def count_for_video(video_id: str) -> int:
    return db.scalar(
        "SELECT COUNT(*) FROM comments WHERE video_id = ? AND is_deleted = 0",
        (video_id,),
        default=0,
    )


def edit(comment_id: int, user_id: int, body: str) -> bool:
    owner = db.scalar("SELECT user_id FROM comments WHERE id = ?", (comment_id,))
    if owner != user_id:
        return False
    db.execute(
        "UPDATE comments SET body = ?, edited_at = datetime('now') WHERE id = ?",
        (body.strip()[:MAX_LENGTH], comment_id),
    )
    return True


def delete(comment_id: int, user_id: int, is_admin: bool = False) -> bool:
    """Soft delete, so replies underneath a removed comment survive."""
    owner = db.scalar("SELECT user_id FROM comments WHERE id = ?", (comment_id,))
    if owner != user_id and not is_admin:
        return False
    db.execute(
        "UPDATE comments SET is_deleted = 1, body = '' WHERE id = ?", (comment_id,)
    )
    return True


def set_pinned(comment_id: int, pinned: bool) -> None:
    db.execute(
        "UPDATE comments SET is_pinned = ? WHERE id = ?", (int(pinned), comment_id)
    )


def vote(user_id: int, comment_id: int, value: int) -> int:
    current = db.scalar(
        "SELECT value FROM comment_votes WHERE user_id = ? AND comment_id = ?",
        (user_id, comment_id),
        default=0,
    )
    if value == 0 or current == value:
        db.execute(
            "DELETE FROM comment_votes WHERE user_id = ? AND comment_id = ?",
            (user_id, comment_id),
        )
        return 0
    db.execute(
        """
        INSERT INTO comment_votes (user_id, comment_id, value) VALUES (?, ?, ?)
        ON CONFLICT(user_id, comment_id) DO UPDATE SET value = excluded.value
        """,
        (user_id, comment_id, value),
    )
    return value


def vote_counts(comment_id: int) -> dict:
    row = db.query_one(
        """
        SELECT
            SUM(CASE WHEN value =  1 THEN 1 ELSE 0 END) AS likes,
            SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END) AS dislikes
        FROM comment_votes WHERE comment_id = ?
        """,
        (comment_id,),
    )
    return {
        "likes": (row["likes"] if row else 0) or 0,
        "dislikes": (row["dislikes"] if row else 0) or 0,
    }


def recent_by_user(user_id: int, limit: int = 50) -> list[dict]:
    rows = db.query(
        """
        SELECT c.id, c.body, c.created_at, c.video_id, c.response_video_id,
               v.title, v.thumbnail
        FROM comments c
        LEFT JOIN videos v ON v.video_id = c.video_id
        WHERE c.user_id = ? AND c.is_deleted = 0
        ORDER BY c.id DESC LIMIT ?
        """,
        (user_id, limit),
    )
    return db.rows_to_dicts(rows)


# --------------------------------------------------------------------------
# Video responses
# --------------------------------------------------------------------------
def response_count(video_id: str) -> int:
    return db.scalar(
        """
        SELECT COUNT(*) FROM comments
        WHERE video_id = ? AND response_video_id IS NOT NULL AND is_deleted = 0
        """,
        (video_id,),
        default=0,
    )


def responses_to(video_id: str, limit: int = 24) -> list[dict]:
    """
    The video responses posted against a video, as playable video rows.

    Used to build the "responses" shelf on the watch page, which is where the
    old YouTube version of this lived.
    """
    rows = db.query(
        f"""
        SELECT {FIELDS}, rv.video_id AS resp_id, rv.channel_name AS response_channel
        FROM comments c {JOINS}
        WHERE c.video_id = ? AND c.response_video_id IS NOT NULL AND c.is_deleted = 0
        ORDER BY c.id DESC LIMIT ?
        """,
        (video_id, limit),
    )
    return db.rows_to_dicts(rows)


def responded_to_by(response_video_id: str) -> dict | None:
    """
    The other direction: given a response video, what was it replying to?

    This is what lets a response's own watch page show "responding to ..."
    instead of appearing as an orphan clip with no context.
    """
    row = db.query_one(
        """
        SELECT c.id AS comment_id, c.video_id, c.username, c.created_at,
               v.title, v.thumbnail, v.channel_name
        FROM comments c
        LEFT JOIN videos v ON v.video_id = c.video_id
        WHERE c.response_video_id = ? AND c.is_deleted = 0
        ORDER BY c.id ASC LIMIT 1
        """,
        (response_video_id,),
    )
    return dict(row) if row else None


def response_video_for(comment_id: int) -> str | None:
    return db.scalar(
        "SELECT response_video_id FROM comments WHERE id = ?", (comment_id,)
    )
