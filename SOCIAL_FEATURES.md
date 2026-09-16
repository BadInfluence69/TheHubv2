# The Hub — social layer

Direct messages, public rooms, video responses, and a privacy page that
matches the code. This document covers what was added, how it works, and what
it deliberately does not claim.

---

## 1. Direct messages

`/messages/` — an inbox, per-conversation threads, and a people picker limited
to accounts on this server.

### The encryption, in short

| Step | Where | What happens |
|---|---|---|
| Key generation | Browser | ECDH P-256 key pair |
| Private key protection | Browser | AES-GCM under PBKDF2-SHA256, 600,000 iterations |
| Upload | → Server | Public key, plus the *encrypted* private key, salt, and nonce |
| Message key | Browser | ECDH(my private, their public) → HKDF-SHA256 |
| Message | Browser | AES-256-GCM, fresh 12-byte nonce, conversation id as AAD |
| Storage | Server | base64 ciphertext + nonce. No plaintext column exists |

The passphrase never leaves the browser. It is deliberately **not** the account
password, because the server necessarily sees that at sign-in — a vault locked
with a password the server sees is a vault the server can open.

The unwrapped key is held in IndexedDB as a **non-extractable** `CryptoKey`, so
it survives a page load without script ever being able to read the bytes back
out. "Lock this device" deletes it.

### Verified, not asserted

`verify_e2ee.js` loads the real `hub/static/js/e2ee.js` and checks, among other
things, that a third party holding **both public keys and the wrapped private
key** — precisely the server's position — cannot decrypt anything.

```
node verify_e2ee.js       # 28 checks
python3 social_test.py    # 75 checks
python3 smoke_test.py     # 51 checks, unchanged from before
```

### What it does not protect

Stated here, in the UI, and on the privacy page — not buried:

- **Metadata.** Who messaged whom, and when, is stored in the clear. Routing a
  message means knowing where to route it.
- **Forward secrecy.** One long-lived key pair per account, no ratchet. A
  compromised passphrase exposes the entire history.
- **Key substitution.** The server hands out public keys and could hand out one
  it holds the private half of. Compare fingerprints out of band — that is why
  they are displayed on the key page and in each thread.
- **Your own device.** Encryption stops at the screen.

### Lost passphrases

There is no reset. Rotating generates a new key and permanently abandons every
message sent before it — the UI says so twice and requires confirmation. This
is the same property as "the operator can't read them", seen from the other
side.

---

## 2. Public rooms

`/chat/` — a sidebar tab with three seeded rooms (Lobby, What we're watching,
Help & requests) plus rooms anyone can create.

**Access rule:** every route is behind `login_required`. An account on this
server can read and post; nobody else can reach any of it. That is the
membership rule enforced structurally rather than by a check somebody could
forget to add.

**Rooms are not encrypted, and cannot be.** A room with open membership has no
key that only the participants hold. Room posts are plain text and the operator
can read them. This is said plainly on the room page itself rather than left
for someone to discover.

Includes `@mention` pings that resolve against real accounts only, per-room
read cursors for the unread badges, admin lock/delete, and blocked accounts'
posts hidden from the people who blocked them.

---

## 3. Video responses

The old YouTube feature: a comment can be a video rather than a paragraph.

Two ways to attach one:

- **Upload a file** → stored under `data/uploads/responses/<user id>/`, served
  with byte-range support so seeking works, poster frame pulled with ffmpeg.
- **Reference something already here** → paste a Hub watch URL, a YouTube link,
  or a bare video id. Nothing is copied.

A response becomes an ordinary row in `videos` with a `resp_` id, so everything
the app already does with a video works on it for free: its own watch page,
likes, playlists, catalogue search.

Also included:
- a responses shelf at the top of the comment section
- a **Responses** sort tab alongside Top / Newest / Oldest
- a "Responding to" backlink on a response's own watch page, so it is never an
  orphan clip with no context
- deleting the comment deletes an uploaded response file; *referenced* videos
  are left alone, since they were not ours to delete
- works without JavaScript, via an ordinary multipart form post

---

## 4. Notifications

One inbox for replies, video responses, messages, mentions, and uploads, tagged
by kind and filterable — built for the case where somebody has hundreds.

- Filter chips per kind, with counts and unread dots
- Clear all, or clear just one kind
- **Repeat collapsing** — twenty messages from one person produce one badge,
  not twenty. That failure mode is what makes people stop reading the bell.
- **500-row cap** per account. Read rows are dropped first.

Message notifications never contain message text. The server has not seen it.
That reads as a limitation and is really the feature working.

---

## 5. Privacy page

`/privacy`, readable signed out — a privacy page behind a login is no use to
someone deciding whether to sign up.

It states what is actually stored, including the unflattering parts: comments,
room posts, watch history, search history, sessions, message metadata. The
legal-requests section is a table of what could and could not be produced under
a valid order, rather than a claim that nothing could be.

**Why it is written that way.** A policy that overstates its protections is not
a stronger policy. It is one that falls apart the first time anyone tests it,
and in the US it is the specific thing that draws FTC attention under Section 5
as a deceptive practice. A subpoena reaches what an operator actually holds,
not what a page says they hold — so the only real protection is data never
collected, or data held in a form nobody can read.

The honest version here is genuinely strong: message content is producible but
unreadable, and passwords and passphrases are not stored at all.

> This describes how the software stores data. It is not legal advice. If you
> run this for other people, what you can be compelled to produce depends on
> your jurisdiction — ask a lawyer rather than relying on a page inside your own
> app.

---

## 6. Schema changes

New tables: `user_keys`, `conversations`, `messages`, `user_blocks`,
`chat_rooms`, `chat_messages`, `chat_reads`.
New column: `comments.response_video_id`.

Existing databases upgrade in place on startup via the project's existing
migration path. Nothing is dropped or rewritten.

New config: `STORE_SESSION_IP` (default `1`). Set to `0` to stop recording IP
addresses and user agents with sessions. The privacy page tells people this
switch exists, so it has to keep working.

---

## 7. Housekeeping

`hub/templates/studio.py` and `hub/templates/google_oauth.py` were stray copies
of `views/studio.py` and `services/google_oauth.py` sitting in the templates
directory. Nothing imported them; they are removed.

---

## 8. Before you deploy

- **Test the crypto in a real browser.** The Node harness runs the real file,
  but IndexedDB and the UI wiring are stubbed. Make two accounts, exchange a
  message, confirm fingerprints match.
- **Serve over HTTPS.** Web Crypto is unavailable on insecure origins other
  than `localhost`, so messaging silently will not work over plain HTTP on a
  LAN address. Set `SESSION_COOKIE_SECURE=1` at the same time.
- **Back up `hub.db`.** Lost keys mean lost messages, with no recovery path.
- **Re-read `/privacy` if you change how data is handled**, and change it to
  match. The page is only worth having while it is accurate.
