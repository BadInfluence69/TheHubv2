"""Upload jobs and the connected Google account (used for YouTube uploads)."""
from __future__ import annotations

from .. import db

ACTIVE_STATES = ("pending", "uploading", "processing")


# --------------------------------------------------------------------------
# Upload jobs
# --------------------------------------------------------------------------
def create_job(user_id: int, file_path: str, original_name: str, file_size: int,
               title: str, description: str = "", tags: str = "",
               category_id: str = "22", privacy: str = "private",
               made_for_kids: bool = False, publish_at: str | None = None,
               thumbnail_path: str | None = None) -> int:
    return db.insert(
        """
        INSERT INTO uploads (user_id, file_path, original_name, file_size, title,
                             description, tags, category_id, privacy, made_for_kids,
                             publish_at, thumbnail_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, file_path, original_name, file_size, title, description, tags,
         category_id, privacy, int(made_for_kids), publish_at, thumbnail_path),
    )


def get_job(job_id: int, user_id: int | None = None) -> dict | None:
    if user_id is None:
        row = db.query_one("SELECT * FROM uploads WHERE id = ?", (job_id,))
    else:
        row = db.query_one(
            "SELECT * FROM uploads WHERE id = ? AND user_id = ?", (job_id, user_id)
        )
    return dict(row) if row else None


def jobs_for(user_id: int, limit: int = 100) -> list[dict]:
    rows = db.query(
        "SELECT * FROM uploads WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return db.rows_to_dicts(rows)


def active_jobs(user_id: int) -> list[dict]:
    placeholders = ",".join("?" * len(ACTIVE_STATES))
    rows = db.query(
        f"SELECT * FROM uploads WHERE user_id = ? AND status IN ({placeholders}) "
        "ORDER BY id DESC",
        [user_id, *ACTIVE_STATES],
    )
    return db.rows_to_dicts(rows)


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    allowed = {
        "status", "bytes_sent", "youtube_video_id", "error", "title",
        "description", "tags", "privacy", "category_id", "made_for_kids",
        "publish_at", "thumbnail_path", "file_size",
    }
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(job_id)
    db.execute(f"UPDATE uploads SET {', '.join(sets)} WHERE id = ?", params)


def delete_job(job_id: int, user_id: int) -> None:
    db.execute("DELETE FROM uploads WHERE id = ? AND user_id = ?", (job_id, user_id))


def stats(user_id: int) -> dict:
    row = db.query_one(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'done'  THEN 1 ELSE 0 END) AS published,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status IN ('pending','uploading','processing')
                     THEN 1 ELSE 0 END) AS in_flight,
            COALESCE(SUM(file_size), 0) AS bytes_total
        FROM uploads WHERE user_id = ?
        """,
        (user_id,),
    )
    return {k: (row[k] or 0) for k in row.keys()} if row else {}


# --------------------------------------------------------------------------
# Connected Google account
# --------------------------------------------------------------------------
def save_google_account(user_id: int, token_json: str, scopes: str,
                        channel_id: str = "", channel_title: str = "",
                        thumbnail: str = "") -> None:
    db.execute(
        """
        INSERT INTO google_accounts (user_id, channel_id, channel_title, thumbnail,
                                     token_json, scopes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            channel_id    = COALESCE(NULLIF(excluded.channel_id, ''),
                                     google_accounts.channel_id),
            channel_title = COALESCE(NULLIF(excluded.channel_title, ''),
                                     google_accounts.channel_title),
            thumbnail     = COALESCE(NULLIF(excluded.thumbnail, ''),
                                     google_accounts.thumbnail),
            token_json    = excluded.token_json,
            scopes        = excluded.scopes,
            updated_at    = datetime('now')
        """,
        (user_id, channel_id, channel_title, thumbnail, token_json, scopes),
    )


def google_account(user_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM google_accounts WHERE user_id = ?", (user_id,))
    return dict(row) if row else None


def update_token(user_id: int, token_json: str) -> None:
    db.execute(
        "UPDATE google_accounts SET token_json = ?, updated_at = datetime('now') "
        "WHERE user_id = ?",
        (token_json, user_id),
    )


def disconnect_google(user_id: int) -> None:
    db.execute("DELETE FROM google_accounts WHERE user_id = ?", (user_id,))
