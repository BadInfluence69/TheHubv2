"""
Storage for message encryption keys.

Everything in this module is opaque to the server on purpose. The browser
generates an ECDH P-256 key pair, keeps the private half wrapped in AES-GCM
under a passphrase-derived key, and hands the server two things: a public key
meant to be shared, and a ciphertext blob it has no key for.

Nothing here can decrypt anything. That is the entire point, and it is what
makes the claim on the privacy page true rather than aspirational.
"""
from __future__ import annotations

from .. import db


def get(user_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM user_keys WHERE user_id = ?", (user_id,))
    return dict(row) if row else None


def has_keys(user_id: int) -> bool:
    return db.query_one(
        "SELECT 1 FROM user_keys WHERE user_id = ?", (user_id,)
    ) is not None


def public_key(user_id: int) -> str | None:
    return db.scalar("SELECT public_key FROM user_keys WHERE user_id = ?", (user_id,))


def public_keys_for(user_ids: list[int]) -> dict[int, str]:
    """Bulk lookup, used when rendering a conversation list."""
    if not user_ids:
        return {}
    placeholders = ",".join("?" * len(user_ids))
    return {
        row["user_id"]: row["public_key"]
        for row in db.query(
            f"SELECT user_id, public_key FROM user_keys WHERE user_id IN ({placeholders})",
            user_ids,
        )
    }


def store(
    user_id: int,
    public_key: str,
    wrapped_private_key: str,
    wrap_iv: str,
    kdf_salt: str,
    kdf_iterations: int,
    fingerprint: str,
) -> None:
    """
    Save a freshly generated key pair, replacing any earlier one.

    Replacing a key pair does not re-encrypt old messages — it cannot, since
    the server never had the plaintext. Callers are expected to have warned the
    person that rotating locks them out of everything sent before now.
    """
    db.execute(
        """
        INSERT INTO user_keys (user_id, public_key, wrapped_private_key, wrap_iv,
                               kdf_salt, kdf_iterations, fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            public_key          = excluded.public_key,
            wrapped_private_key = excluded.wrapped_private_key,
            wrap_iv             = excluded.wrap_iv,
            kdf_salt            = excluded.kdf_salt,
            kdf_iterations      = excluded.kdf_iterations,
            fingerprint         = excluded.fingerprint,
            rotated_at          = datetime('now')
        """,
        (
            user_id,
            public_key,
            wrapped_private_key,
            wrap_iv,
            kdf_salt,
            kdf_iterations,
            fingerprint,
        ),
    )


def delete(user_id: int) -> None:
    db.execute("DELETE FROM user_keys WHERE user_id = ?", (user_id,))
