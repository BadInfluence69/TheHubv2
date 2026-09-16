"""
Direct messages between two accounts on this server.

The server is a dumb relay here. It stores ciphertext, the nonce needed to
decrypt it, and enough routing metadata to hand the row to the right account.
It never sees a message body, and there is no code path anywhere in this
project that could produce one.

What the server *does* know, and what an honest privacy page has to admit:
who talked to whom, when, and how many times. That metadata is unavoidable —
routing a message requires knowing where to route it.
"""
from __future__ import annotations

from .. import db

MAX_CIPHERTEXT = 64_000  # ~40 KB of plaintext once base64 and padding are paid


# --------------------------------------------------------------------------
# Conversations
# --------------------------------------------------------------------------
def _pair(user_id: int, other_id: int) -> tuple[int, int]:
    """Conversations are stored with the lower id first, so a pair is unique."""
    return (user_id, other_id) if user_id < other_id else (other_id, user_id)


def find(user_id: int, other_id: int) -> dict | None:
    a, b = _pair(user_id, other_id)
    row = db.query_one(
        "SELECT * FROM conversations WHERE user_a = ? AND user_b = ?", (a, b)
    )
    return dict(row) if row else None


def open_with(user_id: int, other_id: int) -> int:
    """Conversation id for these two accounts, creating it if needed."""
    if user_id == other_id:
        raise ValueError("You can't message yourself.")

    existing = find(user_id, other_id)
    if existing:
        return existing["id"]

    a, b = _pair(user_id, other_id)
    return db.insert(
        "INSERT INTO conversations (user_a, user_b) VALUES (?, ?)", (a, b)
    )


def get(conversation_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    return dict(row) if row else None


def is_participant(conversation_id: int, user_id: int) -> bool:
    return db.query_one(
        "SELECT 1 FROM conversations WHERE id = ? AND (user_a = ? OR user_b = ?)",
        (conversation_id, user_id, user_id),
    ) is not None


def other_party(conversation: dict, user_id: int) -> int:
    return conversation["user_b"] if conversation["user_a"] == user_id else conversation["user_a"]


def list_for(user_id: int, limit: int = 100) -> list[dict]:
    """
    Every conversation this account is in, newest activity first.

    The preview column everyone expects here is missing for a good reason:
    there is no readable preview to give. The browser decrypts the last message
    itself once the person has unlocked their key.
    """
    rows = db.query(
        """
        SELECT
            c.id,
            c.last_at,
            u.id            AS other_id,
            u.username      AS other_username,
            u.display_name  AS other_display_name,
            u.avatar_hue    AS other_avatar_hue,
            k.public_key    AS other_public_key,
            (SELECT COUNT(*) FROM messages m
              WHERE m.conversation_id = c.id
                AND m.sender_id != ?
                AND m.read_at IS NULL
                AND m.is_deleted = 0)          AS unread,
            (SELECT m.id FROM messages m
              WHERE m.conversation_id = c.id AND m.is_deleted = 0
              ORDER BY m.id DESC LIMIT 1)      AS last_message_id,
            (SELECT m.ciphertext FROM messages m
              WHERE m.conversation_id = c.id AND m.is_deleted = 0
              ORDER BY m.id DESC LIMIT 1)      AS last_ciphertext,
            (SELECT m.iv FROM messages m
              WHERE m.conversation_id = c.id AND m.is_deleted = 0
              ORDER BY m.id DESC LIMIT 1)      AS last_iv,
            (SELECT m.sender_id FROM messages m
              WHERE m.conversation_id = c.id AND m.is_deleted = 0
              ORDER BY m.id DESC LIMIT 1)      AS last_sender_id
        FROM conversations c
        JOIN users u
          ON u.id = CASE WHEN c.user_a = ? THEN c.user_b ELSE c.user_a END
        LEFT JOIN user_keys k ON k.user_id = u.id
        WHERE c.user_a = ? OR c.user_b = ?
        ORDER BY c.last_at DESC, c.id DESC
        LIMIT ?
        """,
        (user_id, user_id, user_id, user_id, limit),
    )
    return db.rows_to_dicts(rows)


def unread_total(user_id: int) -> int:
    return db.scalar(
        """
        SELECT COUNT(*)
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE (c.user_a = ? OR c.user_b = ?)
          AND m.sender_id != ?
          AND m.read_at IS NULL
          AND m.is_deleted = 0
        """,
        (user_id, user_id, user_id),
        default=0,
    )


def delete_conversation(conversation_id: int) -> None:
    """Removes the thread and its messages for both parties. There is no
    'delete for me only' — with two participants that would be a lie."""
    db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------
def send(conversation_id: int, sender_id: int, ciphertext: str, iv: str,
         alg: str = "ECDH-P256/HKDF-SHA256/AES-256-GCM") -> int:
    message_id = db.insert(
        """
        INSERT INTO messages (conversation_id, sender_id, ciphertext, iv, alg)
        VALUES (?, ?, ?, ?, ?)
        """,
        (conversation_id, sender_id, ciphertext, iv, alg),
    )
    db.execute(
        "UPDATE conversations SET last_at = datetime('now') WHERE id = ?",
        (conversation_id,),
    )
    return message_id


def thread(conversation_id: int, limit: int = 200, before_id: int | None = None) -> list[dict]:
    """Oldest-first page of a conversation, for rendering top to bottom."""
    if before_id:
        rows = db.query(
            """
            SELECT id, sender_id, ciphertext, iv, alg, created_at, read_at, is_deleted
            FROM messages
            WHERE conversation_id = ? AND id < ?
            ORDER BY id DESC LIMIT ?
            """,
            (conversation_id, before_id, limit),
        )
    else:
        rows = db.query(
            """
            SELECT id, sender_id, ciphertext, iv, alg, created_at, read_at, is_deleted
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (conversation_id, limit),
        )
    return list(reversed(db.rows_to_dicts(rows)))


def since(conversation_id: int, after_id: int) -> list[dict]:
    """Anything newer than a message the browser already has. Used for polling."""
    rows = db.query(
        """
        SELECT id, sender_id, ciphertext, iv, alg, created_at, read_at, is_deleted
        FROM messages
        WHERE conversation_id = ? AND id > ?
        ORDER BY id ASC LIMIT 200
        """,
        (conversation_id, after_id),
    )
    return db.rows_to_dicts(rows)


def mark_read(conversation_id: int, reader_id: int) -> int:
    """Marks everything the other party sent as read. Returns the row count."""
    cursor = db.execute(
        """
        UPDATE messages SET read_at = datetime('now')
        WHERE conversation_id = ? AND sender_id != ? AND read_at IS NULL
        """,
        (conversation_id, reader_id),
    )
    return cursor.rowcount or 0


def delete_message(message_id: int, user_id: int) -> bool:
    """
    Unsend. The ciphertext is overwritten with an empty string rather than the
    row being dropped, so the thread keeps its shape for the other person.
    """
    owner = db.scalar("SELECT sender_id FROM messages WHERE id = ?", (message_id,))
    if owner != user_id:
        return False
    db.execute(
        "UPDATE messages SET is_deleted = 1, ciphertext = '', iv = '' WHERE id = ?",
        (message_id,),
    )
    return True


# --------------------------------------------------------------------------
# Blocking
# --------------------------------------------------------------------------
def block(user_id: int, blocked_id: int) -> None:
    if user_id == blocked_id:
        return
    db.execute(
        "INSERT OR IGNORE INTO user_blocks (user_id, blocked_id) VALUES (?, ?)",
        (user_id, blocked_id),
    )


def unblock(user_id: int, blocked_id: int) -> None:
    db.execute(
        "DELETE FROM user_blocks WHERE user_id = ? AND blocked_id = ?",
        (user_id, blocked_id),
    )


def is_blocked(sender_id: int, recipient_id: int) -> bool:
    """True when the recipient has blocked the sender."""
    return db.query_one(
        "SELECT 1 FROM user_blocks WHERE user_id = ? AND blocked_id = ?",
        (recipient_id, sender_id),
    ) is not None


def blocked_ids(user_id: int) -> set[int]:
    return {
        row["blocked_id"]
        for row in db.query(
            "SELECT blocked_id FROM user_blocks WHERE user_id = ?", (user_id,)
        )
    }


def blocked_list(user_id: int) -> list[dict]:
    rows = db.query(
        """
        SELECT u.id, u.username, u.display_name, u.avatar_hue, b.created_at
        FROM user_blocks b
        JOIN users u ON u.id = b.blocked_id
        WHERE b.user_id = ?
        ORDER BY b.created_at DESC
        """,
        (user_id,),
    )
    return db.rows_to_dicts(rows)


# --------------------------------------------------------------------------
# People search, for the "new message" box
# --------------------------------------------------------------------------
def find_people(term: str, exclude_id: int, limit: int = 12) -> list[dict]:
    """
    Accounts on this server matching a partial name.

    Only accounts that exist here are searchable, which is the membership rule
    the whole social layer runs on: if you don't have an account, you're not in
    the directory and nobody can message you.
    """
    term = (term or "").strip()
    if not term:
        rows = db.query(
            """
            SELECT u.id, u.username, u.display_name, u.avatar_hue,
                   (k.user_id IS NOT NULL) AS has_keys
            FROM users u
            LEFT JOIN user_keys k ON k.user_id = u.id
            WHERE u.id != ?
            ORDER BY u.username COLLATE NOCASE
            LIMIT ?
            """,
            (exclude_id, limit),
        )
    else:
        like = f"%{term}%"
        rows = db.query(
            """
            SELECT u.id, u.username, u.display_name, u.avatar_hue,
                   (k.user_id IS NOT NULL) AS has_keys
            FROM users u
            LEFT JOIN user_keys k ON k.user_id = u.id
            WHERE u.id != ?
              AND (u.username LIKE ? OR u.display_name LIKE ?)
            ORDER BY
                CASE WHEN u.username LIKE ? THEN 0 ELSE 1 END,
                u.username COLLATE NOCASE
            LIMIT ?
            """,
            (exclude_id, like, like, f"{term}%", limit),
        )
    return db.rows_to_dicts(rows)
