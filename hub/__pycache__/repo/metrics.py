"""
Aggregate metrics for the operator's private dashboard.

Everything in here is a read-only roll-up of tables that already exist. No new
collection, no event log, no tracking of any kind is added by this module —
which is deliberate. The privacy page tells people this server runs no
analytics and lists exactly what is stored; counting rows that were already
being written keeps every one of those claims true. If you ever want metrics
that need data the app doesn't currently keep (impressions, click-through,
dwell time), that means new collection, and the privacy page has to change to
match. Don't add it quietly here.

Direct messages are counted, never read. The server holds ciphertext and no
key, so message *volume* is the only thing that can be reported, and the
metadata caveat on the privacy page already covers it.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import db

# Tables the dashboard reports row counts for, in display order.
COUNTED_TABLES = (
    "users", "sessions", "videos", "video_votes", "comments", "comment_votes",
    "subscriptions", "playlists", "playlist_items", "watch_history",
    "search_history", "notifications", "uploads", "feedback",
    "conversations", "messages", "chat_rooms", "chat_messages", "user_keys",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _count(table: str, where: str = "", params: tuple = ()) -> int:
    """COUNT(*) that returns 0 instead of blowing up on a missing table."""
    clause = f" WHERE {where}" if where else ""
    try:
        return int(db.scalar(f"SELECT COUNT(*) FROM {table}{clause}", params, 0) or 0)
    except Exception:  # noqa: BLE001 - an older database may lack a table
        return 0


def _scalar(sql: str, params: tuple = (), default=0):
    try:
        value = db.scalar(sql, params, default)
        return default if value is None else value
    except Exception:  # noqa: BLE001
        return default


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    try:
        return db.rows_to_dicts(db.query(sql, params))
    except Exception:  # noqa: BLE001
        return []


def _since(days: int) -> str:
    """A 'YYYY-MM-DD HH:MM:SS' cutoff, matching how the schema writes stamps."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


# --------------------------------------------------------------------------
# Headline numbers
# --------------------------------------------------------------------------
def overview() -> dict:
    """The top row of the dashboard: the numbers worth seeing first."""
    watch_seconds = _scalar(
        """
        SELECT COALESCE(SUM(
            CASE WHEN position_seconds > 0 AND position_seconds < 86400
                 THEN position_seconds ELSE 0 END
        ), 0) FROM watch_history
        """
    )

    return {
        "users": _count("users"),
        "users_new_30d": _count("users", "created_at >= ?", (_since(30),)),
        "videos": _count("videos"),
        "videos_new_30d": _count("videos", "first_seen >= ?", (_since(30),)),
        "watches": _count("watch_history"),
        "watches_30d": _count("watch_history", "watched_at >= ?", (_since(30),)),
        "watch_seconds": int(watch_seconds),
        "comments": _count("comments", "is_deleted = 0"),
        "comments_30d": _count(
            "comments", "is_deleted = 0 AND created_at >= ?", (_since(30),)
        ),
        "votes": _count("video_votes"),
        "subscriptions": _count("subscriptions"),
        "searches": _count("search_history"),
        "searches_30d": _count("search_history", "created_at >= ?", (_since(30),)),
        "chat_posts": _count("chat_messages", "is_deleted = 0"),
        "dms": _count("messages", "is_deleted = 0"),
        "uploads": _count("uploads"),
        "playlists": _count("playlists"),
        "active_sessions": _count("sessions", "expires_at > datetime('now')"),
    }


# --------------------------------------------------------------------------
# Active accounts
# --------------------------------------------------------------------------
def active_users() -> dict:
    """
    Distinct accounts that did anything at all in each window.

    "Anything" spans watching, searching, commenting, voting, posting in a
    room, and sending a DM, so an account that only lurks in chat still counts
    as active.
    """

    def distinct_since(days: int) -> int:
        cutoff = _since(days)
        return int(
            _scalar(
                """
                SELECT COUNT(*) FROM (
                    SELECT user_id FROM watch_history  WHERE watched_at >= ?
                    UNION
                    SELECT user_id FROM search_history WHERE created_at >= ?
                    UNION
                    SELECT user_id FROM comments       WHERE created_at >= ?
                    UNION
                    SELECT user_id FROM video_votes    WHERE created_at >= ?
                    UNION
                    SELECT user_id FROM chat_messages  WHERE created_at >= ?
                    UNION
                    SELECT sender_id FROM messages     WHERE created_at >= ?
                )
                WHERE user_id IS NOT NULL
                """,
                (cutoff,) * 6,
            )
            or 0
        )

    total = _count("users")
    day, week, month = distinct_since(1), distinct_since(7), distinct_since(30)
    return {
        "day": day,
        "week": week,
        "month": month,
        "total": total,
        "day_pct": _pct(day, total),
        "week_pct": _pct(week, total),
        "month_pct": _pct(month, total),
        # Rough stickiness: daily actives as a share of monthly actives.
        "stickiness": _pct(day, month),
    }


# --------------------------------------------------------------------------
# Daily activity series
# --------------------------------------------------------------------------
_SERIES_SOURCES = {
    "watches": ("watch_history", "watched_at"),
    "comments": ("comments", "created_at"),
    "votes": ("video_votes", "created_at"),
    "searches": ("search_history", "created_at"),
    "signups": ("users", "created_at"),
    "chat": ("chat_messages", "created_at"),
    "messages": ("messages", "created_at"),
}


def activity_series(days: int = 30) -> dict:
    """
    Per-day counts for the last `days` days, gap-filled so the chart doesn't
    silently skip quiet days (a chart with the empty days removed reads as a
    flat line of activity, which is the opposite of the truth).
    """
    start = date.today() - timedelta(days=days - 1)
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    cutoff = start.isoformat()

    series: dict[str, list[int]] = {}
    for name, (table, column) in _SERIES_SOURCES.items():
        buckets = {
            row["day"]: int(row["n"])
            for row in _rows(
                f"""
                SELECT date({column}) AS day, COUNT(*) AS n
                FROM {table}
                WHERE date({column}) >= ?
                GROUP BY day
                """,
                (cutoff,),
            )
            if row.get("day")
        }
        series[name] = [buckets.get(label, 0) for label in labels]

    return {"labels": labels, "series": series, "days": days}


# --------------------------------------------------------------------------
# Content and engagement
# --------------------------------------------------------------------------
def source_breakdown() -> list[dict]:
    """Where the catalogue came from, and how much of it actually gets played."""
    rows = _rows(
        """
        SELECT v.source                       AS source,
               COUNT(DISTINCT v.video_id)     AS videos,
               COUNT(w.id)                    AS watches
        FROM videos v
        LEFT JOIN watch_history w ON w.video_id = v.video_id
        GROUP BY v.source
        ORDER BY watches DESC, videos DESC
        """
    )
    total_watches = sum(row["watches"] for row in rows) or 0
    total_videos = sum(row["videos"] for row in rows) or 0
    for row in rows:
        row["watch_share"] = _pct(row["watches"], total_watches)
        row["video_share"] = _pct(row["videos"], total_videos)
    return rows


def top_videos(limit: int = 15, days: int | None = None) -> list[dict]:
    """Most-watched videos, with the engagement that landed on each."""
    where, params = "", []
    if days:
        where = "WHERE w.watched_at >= ?"
        params.append(_since(days))

    return _rows(
        f"""
        SELECT v.video_id                                   AS video_id,
               v.title                                      AS title,
               v.source                                     AS source,
               v.channel_name                               AS channel_name,
               v.thumbnail                                  AS thumbnail,
               v.duration                                   AS duration,
               COUNT(w.id)                                  AS watches,
               COUNT(DISTINCT w.user_id)                    AS viewers,
               COALESCE(SUM(w.position_seconds), 0)         AS seconds,
               (SELECT COUNT(*) FROM video_votes vv
                 WHERE vv.video_id = v.video_id AND vv.value = 1)   AS likes,
               (SELECT COUNT(*) FROM video_votes vv
                 WHERE vv.video_id = v.video_id AND vv.value = -1)  AS dislikes,
               (SELECT COUNT(*) FROM comments c
                 WHERE c.video_id = v.video_id AND c.is_deleted = 0) AS comments
        FROM watch_history w
        JOIN videos v ON v.video_id = w.video_id
        {where}
        GROUP BY v.video_id
        ORDER BY watches DESC, seconds DESC
        LIMIT ?
        """,
        (*params, limit),
    )


def top_channels(limit: int = 12) -> list[dict]:
    """Channels ranked by plays, with subscriber counts from this server only."""
    return _rows(
        """
        SELECT v.channel_name                     AS channel_name,
               COUNT(w.id)                        AS watches,
               COUNT(DISTINCT v.video_id)         AS videos,
               COUNT(DISTINCT w.user_id)          AS viewers,
               (SELECT COUNT(*) FROM subscriptions s
                 WHERE s.channel_key = v.channel_key) AS subscribers
        FROM watch_history w
        JOIN videos v ON v.video_id = w.video_id
        WHERE v.channel_name IS NOT NULL AND TRIM(v.channel_name) != ''
        GROUP BY v.channel_key
        ORDER BY watches DESC
        LIMIT ?
        """,
        (limit,),
    )


def engagement_rates() -> dict:
    """
    Like/dislike split, comment and completion rates.

    Completion only counts rows where a duration was recorded — a resume
    position with no known runtime can't be turned into a percentage, and
    guessing one would quietly inflate the number.
    """
    likes = _count("video_votes", "value = 1")
    dislikes = _count("video_votes", "value = -1")
    votes = likes + dislikes

    watches = _count("watch_history")
    measurable = _count(
        "watch_history", "duration_seconds IS NOT NULL AND duration_seconds > 0"
    )
    avg_completion = _scalar(
        """
        SELECT AVG(MIN(position_seconds / duration_seconds, 1.0)) * 100
        FROM watch_history
        WHERE duration_seconds IS NOT NULL AND duration_seconds > 0
          AND position_seconds > 0
        """,
        default=0.0,
    )
    finished = _count(
        "watch_history",
        "duration_seconds IS NOT NULL AND duration_seconds > 0 "
        "AND position_seconds >= duration_seconds * 0.9",
    )

    watched_videos = int(
        _scalar("SELECT COUNT(DISTINCT video_id) FROM watch_history") or 0
    )
    commented_videos = int(
        _scalar("SELECT COUNT(DISTINCT video_id) FROM comments WHERE is_deleted = 0")
        or 0
    )

    return {
        "likes": likes,
        "dislikes": dislikes,
        "votes": votes,
        "like_ratio": _pct(likes, votes),
        "avg_completion": round(float(avg_completion or 0), 1),
        "measurable_watches": measurable,
        "measurable_pct": _pct(measurable, watches),
        "finished": finished,
        "finish_rate": _pct(finished, measurable),
        "commented_videos": commented_videos,
        "watched_videos": watched_videos,
        "comment_rate": _pct(commented_videos, watched_videos),
        "replies": _count("comments", "parent_id IS NOT NULL AND is_deleted = 0"),
        "video_responses": _count(
            "comments", "response_video_id IS NOT NULL AND is_deleted = 0"
        ),
    }


def watch_time_by_hour() -> list[dict]:
    """
    Plays bucketed by hour of day (UTC, matching how stamps are written).
    Useful for spotting when the server is actually under load.
    """
    counts = {
        int(row["hour"]): int(row["n"])
        for row in _rows(
            """
            SELECT CAST(strftime('%H', watched_at) AS INTEGER) AS hour,
                   COUNT(*) AS n
            FROM watch_history
            GROUP BY hour
            """
        )
        if row.get("hour") is not None
    }
    return [{"hour": h, "watches": counts.get(h, 0)} for h in range(24)]


def top_searches(limit: int = 15) -> list[dict]:
    return _rows(
        """
        SELECT TRIM(LOWER(query))        AS query,
               COUNT(*)                  AS runs,
               COUNT(DISTINCT user_id)   AS searchers,
               MAX(created_at)           AS last_run
        FROM search_history
        WHERE TRIM(query) != ''
        GROUP BY TRIM(LOWER(query))
        ORDER BY runs DESC, last_run DESC
        LIMIT ?
        """,
        (limit,),
    )


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------
def user_activity(limit: int = 25) -> list[dict]:
    """
    Per-account activity counts. Aggregates only — this deliberately does not
    surface what anyone watched, searched for, or said. The operator can read
    those tables directly if they need to; a dashboard that puts one person's
    viewing history on screen by default is a different thing entirely.
    """
    return _rows(
        """
        SELECT u.id                                   AS id,
               u.username                             AS username,
               u.display_name                         AS display_name,
               u.avatar_hue                           AS avatar_hue,
               u.is_admin                             AS is_admin,
               u.created_at                           AS created_at,
               (SELECT COUNT(*) FROM watch_history w  WHERE w.user_id = u.id) AS watches,
               (SELECT COUNT(*) FROM comments c
                 WHERE c.user_id = u.id AND c.is_deleted = 0)                 AS comments,
               (SELECT COUNT(*) FROM video_votes v    WHERE v.user_id = u.id) AS votes,
               (SELECT COUNT(*) FROM subscriptions s  WHERE s.user_id = u.id) AS subs,
               (SELECT COUNT(*) FROM playlists p
                 WHERE p.user_id = u.id AND p.system_kind IS NULL)            AS playlists,
               (SELECT COUNT(*) FROM chat_messages m
                 WHERE m.user_id = u.id AND m.is_deleted = 0)                 AS posts,
               (SELECT COUNT(*) FROM uploads up       WHERE up.user_id = u.id) AS uploads,
               (SELECT MAX(w.watched_at) FROM watch_history w
                 WHERE w.user_id = u.id)                                      AS last_watch,
               (SELECT COUNT(*) FROM sessions s
                 WHERE s.user_id = u.id AND s.expires_at > datetime('now'))   AS sessions
        FROM users u
        ORDER BY watches DESC, comments DESC, u.id ASC
        LIMIT ?
        """,
        (limit,),
    )


def signup_series(days: int = 90) -> list[dict]:
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    return _rows(
        """
        SELECT date(created_at) AS day, COUNT(*) AS n
        FROM users
        WHERE date(created_at) >= ?
        GROUP BY day
        ORDER BY day
        """,
        (start,),
    )


def retention_cohorts() -> list[dict]:
    """
    For each account: did it come back after the day it signed up, and has it
    done anything in the last 30 days? With a handful of accounts this is
    anecdote rather than statistics — it's here because on a private server the
    handful *is* the whole population.
    """
    return _rows(
        """
        SELECT u.id                          AS id,
               u.username                    AS username,
               date(u.created_at)            AS joined,
               (SELECT COUNT(DISTINCT date(w.watched_at)) FROM watch_history w
                 WHERE w.user_id = u.id)     AS active_days,
               (SELECT COUNT(*) FROM watch_history w
                 WHERE w.user_id = u.id
                   AND date(w.watched_at) > date(u.created_at)) AS returned,
               (SELECT COUNT(*) FROM watch_history w
                 WHERE w.user_id = u.id AND w.watched_at >= datetime('now','-30 days')
               )                             AS recent
        FROM users u
        ORDER BY u.id
        """
    )


# --------------------------------------------------------------------------
# Community
# --------------------------------------------------------------------------
def community_stats() -> dict:
    """
    Room and DM volume. Message *bodies* are never touched: DMs are ciphertext
    the server can't decrypt, and room posts, while readable, aren't quoted
    here — a metrics page has no reason to display anyone's words.
    """
    return {
        "rooms": _count("chat_rooms"),
        "posts": _count("chat_messages", "is_deleted = 0"),
        "posts_30d": _count(
            "chat_messages", "is_deleted = 0 AND created_at >= ?", (_since(30),)
        ),
        "posters": int(
            _scalar(
                "SELECT COUNT(DISTINCT user_id) FROM chat_messages WHERE is_deleted = 0"
            )
            or 0
        ),
        "conversations": _count("conversations"),
        "dms": _count("messages", "is_deleted = 0"),
        "dms_30d": _count(
            "messages", "is_deleted = 0 AND created_at >= ?", (_since(30),)
        ),
        "dms_unread": _count("messages", "read_at IS NULL AND is_deleted = 0"),
        "keys": _count("user_keys"),
        "blocks": _count("user_blocks"),
        "feedback_open": _count("feedback", "status = 'open'"),
        "feedback_total": _count("feedback"),
        # notifications uses is_read; messages uses read_at. Different tables,
        # different conventions — worth spelling out so neither gets "fixed"
        # into the other.
        "notifications_unread": _count("notifications", "is_read = 0"),
    }


def room_activity(limit: int = 10) -> list[dict]:
    return _rows(
        """
        SELECT r.name                          AS name,
               r.slug                          AS slug,
               r.is_locked                     AS is_locked,
               COUNT(m.id)                     AS posts,
               COUNT(DISTINCT m.user_id)       AS posters,
               MAX(m.created_at)               AS last_post
        FROM chat_rooms r
        LEFT JOIN chat_messages m ON m.room_id = r.id AND m.is_deleted = 0
        GROUP BY r.id
        ORDER BY posts DESC, r.name
        LIMIT ?
        """,
        (limit,),
    )


# --------------------------------------------------------------------------
# Studio / publishing
# --------------------------------------------------------------------------
def upload_stats() -> dict:
    rows = _rows("SELECT status, COUNT(*) AS n FROM uploads GROUP BY status")
    by_status = {row["status"]: int(row["n"]) for row in rows}
    return {
        "by_status": by_status,
        "total": sum(by_status.values()),
        "published": by_status.get("done", 0),
        "failed": by_status.get("error", 0),
        "in_flight": sum(
            by_status.get(s, 0) for s in ("pending", "uploading", "processing")
        ),
        "bytes_sent": int(_scalar("SELECT COALESCE(SUM(bytes_sent),0) FROM uploads") or 0),
        "bytes_total": int(_scalar("SELECT COALESCE(SUM(file_size),0) FROM uploads") or 0),
        "connected_accounts": _count("google_accounts"),
    }


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------
def _dir_size(path: str | Path, max_files: int = 200_000) -> int:
    """
    Total bytes under a directory. Capped so a mis-set path can't turn the
    dashboard into a filesystem crawl. Media library folders are never walked.
    """
    total = 0
    seen = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
                seen += 1
                if seen >= max_files:
                    return total
    except OSError:
        return total
    return total


def storage_stats(config) -> dict:
    db_path = Path(config["DB_PATH"])
    db_bytes = db_path.stat().st_size if db_path.exists() else 0

    # WAL and shared-memory files sit alongside the database and can be large.
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            db_bytes += side.stat().st_size

    return {
        "db_bytes": db_bytes,
        "uploads_bytes": _dir_size(config["UPLOAD_DIR"]),
        "cache_bytes": _dir_size(config["CACHE_DIR"]),
        "hls_bytes": _dir_size(config["HLS_DIR"]),
        "media_folders": len(config.get("MEDIA_LIBRARY") or {}),
    }


def table_counts() -> list[dict]:
    return [{"table": name, "rows": _count(name)} for name in COUNTED_TABLES]


def database_health() -> dict:
    """Cheap PRAGMA reads — no integrity_check, which locks on a large file."""
    return {
        "page_count": int(_scalar("PRAGMA page_count") or 0),
        "page_size": int(_scalar("PRAGMA page_size") or 0),
        "freelist": int(_scalar("PRAGMA freelist_count") or 0),
        "journal_mode": _scalar("PRAGMA journal_mode", default="unknown"),
        "expired_sessions": _count("sessions", "expires_at <= datetime('now')"),
    }


# --------------------------------------------------------------------------
# One call for the whole page
# --------------------------------------------------------------------------
def dashboard(config, days: int = 30) -> dict:
    return {
        "overview": overview(),
        "active": active_users(),
        "activity": activity_series(days),
        "sources": source_breakdown(),
        "top_videos": top_videos(15),
        "top_videos_recent": top_videos(10, days=30),
        "top_channels": top_channels(12),
        "engagement": engagement_rates(),
        "by_hour": watch_time_by_hour(),
        "searches": top_searches(15),
        "users": user_activity(25),
        "retention": retention_cohorts(),
        "community": community_stats(),
        "rooms": room_activity(10),
        "uploads": upload_stats(),
        "storage": storage_stats(config),
        "tables": table_counts(),
        "health": database_health(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
