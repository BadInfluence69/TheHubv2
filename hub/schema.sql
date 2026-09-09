-- ============================================================================
-- The Hub - local database schema
--
-- Everything social (likes, dislikes, comments, subscriptions, playlists,
-- history) lives in this file's tables and NOWHERE else. Nothing here is ever
-- written back to YouTube. The only data that leaves this machine is a video
-- file you explicitly upload from the Studio page.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- accounts --
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT,
    bio           TEXT    DEFAULT '',
    avatar_hue    INTEGER DEFAULT 0,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Server-side sessions. The cookie holds a random token; only its hash is
-- stored here, so a stolen database still can't be used to forge a login.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT    NOT NULL,
    user_agent TEXT,
    ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT    NOT NULL,
    value   TEXT,
    PRIMARY KEY (user_id, key)
);

-- ------------------------------------------------------------------ videos --
-- One row per video the app has ever seen, whatever the source. `source` is
-- one of: youtube | tubi | local | upload.
CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    source          TEXT    NOT NULL DEFAULT 'youtube',
    title           TEXT    NOT NULL DEFAULT 'Untitled',
    channel_key     TEXT,
    channel_name    TEXT,
    thumbnail       TEXT,
    description     TEXT    DEFAULT '',
    -- JSON array of the video's own tags, when the Data API gives them to
    -- us. Feeds the preference profile in services/preferences.py.
    tags            TEXT    DEFAULT '',
    duration        INTEGER,
    published_at    TEXT,
    file_path       TEXT,
    view_count      INTEGER NOT NULL DEFAULT 0,
    legacy_likes    INTEGER NOT NULL DEFAULT 0,
    legacy_dislikes INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_key);
CREATE INDEX IF NOT EXISTS idx_videos_source  ON videos(source);
CREATE INDEX IF NOT EXISTS idx_videos_seen    ON videos(last_seen DESC);

-- One vote per person per video. value is 1 (like) or -1 (dislike).
-- Voting again with the same value clears it, exactly like YouTube.
CREATE TABLE IF NOT EXISTS video_votes (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    video_id   TEXT    NOT NULL,
    value      INTEGER NOT NULL CHECK (value IN (-1, 1)),
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_video_votes_video ON video_votes(video_id);

-- ---------------------------------------------------------------- comments --
-- parent_id NULL = a top-level comment; otherwise it's a reply.
--
-- response_video_id is the old YouTube "video response": a comment can carry a
-- video instead of, or as well as, text. It points at a row in videos, which
-- may be an upload, a local file, or anything else already in the catalogue.
CREATE TABLE IF NOT EXISTS comments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id          TEXT    NOT NULL,
    user_id           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username          TEXT    NOT NULL,
    parent_id         INTEGER REFERENCES comments(id) ON DELETE CASCADE,
    body              TEXT    NOT NULL,
    response_video_id TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    edited_at         TEXT,
    is_deleted        INTEGER NOT NULL DEFAULT 0,
    is_pinned         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comments_response ON comments(response_video_id);
CREATE INDEX IF NOT EXISTS idx_comments_video  ON comments(video_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);

CREATE TABLE IF NOT EXISTS comment_votes (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    value      INTEGER NOT NULL CHECK (value IN (-1, 1)),
    PRIMARY KEY (user_id, comment_id)
);

-- ----------------------------------------------------------- subscriptions --
-- Local-only. Subscribing here does not touch your real YouTube account.
CREATE TABLE IF NOT EXISTS subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_key  TEXT    NOT NULL,
    channel_name TEXT    NOT NULL,
    thumbnail    TEXT,
    notify       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, channel_key)
);

-- --------------------------------------------------------------- playlists --
-- system_kind marks the built-ins that every account gets automatically:
-- 'watch_later' and 'liked'. Everything else is user-created.
CREATE TABLE IF NOT EXISTS playlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slug        TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    description TEXT    DEFAULT '',
    visibility  TEXT    NOT NULL DEFAULT 'private',
    system_kind TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, slug)
);

CREATE TABLE IF NOT EXISTS playlist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id    TEXT    NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (playlist_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_items ON playlist_items(playlist_id, position);

-- ----------------------------------------------------------------- history --
CREATE TABLE IF NOT EXISTS watch_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    video_id         TEXT    NOT NULL,
    watched_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    position_seconds REAL    NOT NULL DEFAULT 0,
    duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_history_user ON watch_history(user_id, watched_at DESC);

CREATE TABLE IF NOT EXISTS search_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query      TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_search_user ON search_history(user_id, created_at DESC);

-- ----------------------------------------------------------- notifications --
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL DEFAULT 'info',
    title      TEXT    NOT NULL,
    body       TEXT    DEFAULT '',
    url        TEXT,
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notifications_user
    ON notifications(user_id, is_read, created_at DESC);

-- ------------------------------------------------------------ studio/upload --
-- status: pending | uploading | processing | done | error | cancelled
CREATE TABLE IF NOT EXISTS uploads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_path         TEXT    NOT NULL,
    original_name     TEXT,
    file_size         INTEGER NOT NULL DEFAULT 0,
    title             TEXT    NOT NULL,
    description       TEXT    DEFAULT '',
    tags              TEXT    DEFAULT '',
    category_id       TEXT    DEFAULT '22',
    privacy           TEXT    NOT NULL DEFAULT 'private',
    made_for_kids     INTEGER NOT NULL DEFAULT 0,
    publish_at        TEXT,
    thumbnail_path    TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending',
    bytes_sent        INTEGER NOT NULL DEFAULT 0,
    youtube_video_id  TEXT,
    error             TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_uploads_user ON uploads(user_id, created_at DESC);

-- Stored OAuth credentials, one Google account per Hub account.
CREATE TABLE IF NOT EXISTS google_accounts (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    channel_id    TEXT,
    channel_title TEXT,
    thumbnail     TEXT,
    token_json    TEXT    NOT NULL,
    scopes        TEXT,
    connected_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- feedback --
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username   TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    suggestion TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open',
    upvotes    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feedback_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_id INTEGER NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username    TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feedback_votes (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    feedback_id INTEGER NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, feedback_id)
);

-- ============================================================================
-- Social layer: direct messages, public rooms, blocking
-- ============================================================================

-- ------------------------------------------------------ end-to-end key pair --
-- One identity key pair per account, used to encrypt direct messages.
--
-- `public_key` is a base64 SPKI blob and is meant to be public — it is handed
-- out to anyone who wants to message this account.
--
-- `wrapped_private_key` is the PKCS8 private key encrypted in the BROWSER with
-- AES-GCM, under a key derived by PBKDF2 from a passphrase the server never
-- receives. The server stores the ciphertext and the KDF parameters and has no
-- way to turn them back into a private key. If the passphrase is lost the
-- messages are unrecoverable — by design, and stated plainly in the UI.
CREATE TABLE IF NOT EXISTS user_keys (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    public_key          TEXT    NOT NULL,
    wrapped_private_key TEXT    NOT NULL,
    wrap_iv             TEXT    NOT NULL,
    kdf_salt            TEXT    NOT NULL,
    kdf_iterations      INTEGER NOT NULL DEFAULT 600000,
    fingerprint         TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    rotated_at          TEXT
);

-- --------------------------------------------------------- direct messages --
-- Exactly two participants. user_a < user_b is enforced so a pair of accounts
-- can only ever have one conversation row, whichever of them opens it.
CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_b     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    last_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (user_a < user_b),
    UNIQUE (user_a, user_b)
);
CREATE INDEX IF NOT EXISTS idx_conversations_a ON conversations(user_a, last_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_b ON conversations(user_b, last_at DESC);

-- There is deliberately no `body` column here. The server stores ciphertext it
-- cannot decrypt, the nonce needed to decrypt it, and the routing metadata it
-- needs to deliver the row to the right account. Nothing else.
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ciphertext      TEXT    NOT NULL,
    iv              TEXT    NOT NULL,
    alg             TEXT    NOT NULL DEFAULT 'ECDH-P256/HKDF-SHA256/AES-256-GCM',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    read_at         TEXT,
    is_deleted      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON messages(conversation_id, read_at);

-- Blocking is one-directional and hides both DMs and room posts.
CREATE TABLE IF NOT EXISTS user_blocks (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, blocked_id)
);
CREATE INDEX IF NOT EXISTS idx_blocks_blocked ON user_blocks(blocked_id);

-- ------------------------------------------------------------- chat rooms --
-- Public rooms, readable and writable by any signed-in account on this server.
-- These are NOT end-to-end encrypted and cannot be: a room has an open-ended
-- membership list, so there is no key that only the participants hold. Room
-- posts are stored as plain text and the operator of this server can read them.
-- The privacy page says so in as many words.
CREATE TABLE IF NOT EXISTS chat_rooms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name        TEXT    NOT NULL,
    topic       TEXT    DEFAULT '',
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_locked   INTEGER NOT NULL DEFAULT 0,
    is_system   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id    INTEGER NOT NULL REFERENCES chat_rooms(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username   TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    is_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_room ON chat_messages(room_id, id);

-- Read cursor per account per room, so the sidebar can show an unread dot
-- without storing a row per message per reader.
CREATE TABLE IF NOT EXISTS chat_reads (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    room_id      INTEGER NOT NULL REFERENCES chat_rooms(id) ON DELETE CASCADE,
    last_seen_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, room_id)
);

-- Tracks which migration steps have run.
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ============================================================================
-- Performance indexes
--
-- Added after EXPLAIN QUERY PLAN showed these paths falling back to full table
-- scans. Each one below fixes a specific scan that gets more expensive as the
-- table grows, which is exactly the traffic case they matter for. An index
-- costs a little write time and some disk; a scan costs more every day.
-- ============================================================================

-- watch_history was scanned whenever anything asked "who watched this video" —
-- the most-watched ranking, the play count on a watch page, and the per-video
-- roll-ups on the metrics page all hit it.
CREATE INDEX IF NOT EXISTS idx_history_video ON watch_history(video_id);
CREATE INDEX IF NOT EXISTS idx_history_when  ON watch_history(watched_at);

-- "Comments by this account" and the daily comment series both scanned.
CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_id, created_at DESC);

-- subscriber_count() runs on every channel page and every watch page. It was
-- scanning a covering index, which is cheap at six rows and linear at 60,000.
CREATE INDEX IF NOT EXISTS idx_subs_channel ON subscriptions(channel_key);

-- Room post counts per account.
CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages(user_id);

-- The Studio dashboard filters by status on every poll.
CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status);

-- "Which playlists is this video already in" runs behind the save button.
CREATE INDEX IF NOT EXISTS idx_playlist_items_video ON playlist_items(video_id);

-- purge_expired_sessions() scanned the whole session table on every sweep.
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

-- Sign-up series on the metrics page.
CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at);

-- Vote lookups from the video side (the user side already had one).
CREATE INDEX IF NOT EXISTS idx_votes_user ON video_votes(user_id, created_at);


-- ============================================================================
-- Creator profiles: direct support, merch, and affiliate links
--
-- This platform takes no cut and never holds anyone's money. A creator links
-- their OWN payment destination — their Stripe page, PayPal, Ko-fi, whatever —
-- and a fan who clicks it pays that creator directly, on that provider's site.
-- Nothing about the transaction touches this server.
--
-- That is a deliberate design decision, not a limitation. See the long comment
-- in repo/creators.py for why routing the money through the platform instead
-- would change what this software legally is.
-- ============================================================================

-- One profile per channel. channel_key matches videos.channel_key, so a
-- profile attaches to a channel that already exists in the catalogue.
CREATE TABLE IF NOT EXISTS creator_profiles (
    channel_key   TEXT    PRIMARY KEY,
    channel_name  TEXT    NOT NULL,
    -- The account that claimed this channel. NULL means unclaimed.
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    tagline       TEXT    DEFAULT '',
    about         TEXT    DEFAULT '',
    -- Shown above the support links: what a fan's money actually pays for.
    -- Creators who answer this well get supported more than ones who don't.
    support_note  TEXT    DEFAULT '',
    -- Nothing is shown to fans until the creator publishes it.
    is_published  INTEGER NOT NULL DEFAULT 0,
    claimed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_creator_user ON creator_profiles(user_id);

-- Every outbound link a creator offers: support, merch, affiliate, social.
--
-- `clicks` is a single running total per link with no user id and no timestamp
-- attached. A creator needs to know whether a link works at all; they do not
-- need to know which of their fans clicked it, and this table is shaped so
-- that question can't be answered from it even by whoever runs the server.
CREATE TABLE IF NOT EXISTS creator_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_key  TEXT    NOT NULL REFERENCES creator_profiles(channel_key)
                            ON DELETE CASCADE,
    -- support | merch | affiliate | social
    kind         TEXT    NOT NULL DEFAULT 'support',
    -- Free-text provider name, used only to pick an icon and a badge.
    provider     TEXT    DEFAULT '',
    label        TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    detail       TEXT    DEFAULT '',
    position     INTEGER NOT NULL DEFAULT 0,
    is_active    INTEGER NOT NULL DEFAULT 1,
    clicks       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_creator_links
    ON creator_links(channel_key, kind, position);
