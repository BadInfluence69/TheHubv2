"""The feature request board."""
from __future__ import annotations

from .. import db

STATUSES = ("open", "planned", "shipped", "declined")


def create(user: dict, title: str, suggestion: str) -> int:
    return db.insert(
        "INSERT INTO feedback (user_id, username, title, suggestion) VALUES (?, ?, ?, ?)",
        (user["id"], user["username"], title.strip(), suggestion.strip()),
    )


def listing(user_id: int | None = None, status: str = "") -> list[dict]:
    where, params = "", []
    if status in STATUSES:
        where = "WHERE f.status = ?"
        params.append(status)

    rows = db.query(
        f"""
        SELECT f.*,
               (SELECT COUNT(*) FROM feedback_comments fc WHERE fc.feedback_id = f.id)
                   AS comment_count,
               (SELECT COUNT(*) FROM feedback_votes fv
                WHERE fv.feedback_id = f.id AND fv.user_id = ?) AS voted
        FROM feedback f
        {where}
        ORDER BY f.upvotes DESC, f.id DESC
        """,
        [user_id or 0, *params],
    )
    return db.rows_to_dicts(rows)


def get(feedback_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM feedback WHERE id = ?", (feedback_id,))
    return dict(row) if row else None


def comments_for(feedback_id: int) -> list[dict]:
    rows = db.query(
        "SELECT * FROM feedback_comments WHERE feedback_id = ? ORDER BY id ASC",
        (feedback_id,),
    )
    return db.rows_to_dicts(rows)


def add_comment(feedback_id: int, user: dict, body: str) -> None:
    db.insert(
        """
        INSERT INTO feedback_comments (feedback_id, user_id, username, body)
        VALUES (?, ?, ?, ?)
        """,
        (feedback_id, user["id"], user["username"], body.strip()),
    )


def toggle_vote(user_id: int, feedback_id: int) -> bool:
    exists = db.scalar(
        "SELECT 1 FROM feedback_votes WHERE user_id = ? AND feedback_id = ?",
        (user_id, feedback_id),
    )
    if exists:
        db.execute(
            "DELETE FROM feedback_votes WHERE user_id = ? AND feedback_id = ?",
            (user_id, feedback_id),
        )
        db.execute(
            "UPDATE feedback SET upvotes = MAX(upvotes - 1, 0) WHERE id = ?",
            (feedback_id,),
        )
        return False

    db.execute(
        "INSERT INTO feedback_votes (user_id, feedback_id) VALUES (?, ?)",
        (user_id, feedback_id),
    )
    db.execute("UPDATE feedback SET upvotes = upvotes + 1 WHERE id = ?", (feedback_id,))
    return True


def set_status(feedback_id: int, status: str) -> None:
    if status in STATUSES:
        db.execute("UPDATE feedback SET status = ? WHERE id = ?", (status, feedback_id))


def delete(feedback_id: int) -> None:
    db.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,))
