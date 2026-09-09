# CDN offload — audit and change set

## What's actually consuming your bandwidth

Every route below is in `thehub/hub/views/media.py` unless noted.

| Route | Lines | Bytes through origin | After |
|---|---|---|---|
| `/media/stream/<id>` | 74–91, `_ranged_file_response` 104–146 | **Full file, every viewer.** `generate()` at 124–133 reads 256 KB chunks off disk and yields them through Flask. This is the big one. | 302 to signed CDN URL |
| `/media/download/<id>` | 258–264 | Full file via `send_file` | 302 to signed CDN URL |
| `_transcode_response` | 149–212 | Full re-encode piped from ffmpeg `pipe:1` (line 182) | Unchanged — can't be offloaded, see caveats |
| `/media/thumb/<id>` | 218–255 | Small but one per card per page load | Unchanged — cached at 218/232, cheap |
| `/live/<path:filename>` | 338–355 | Every HLS segment through `send_file` | Segments 302 to CDN, playlist stays local |
| `/proxy/stream` | 270–320 | Full remote stream, twice over the wire | **Deleted** |

## Three things worth knowing before you apply this

**1. Your YouTube path is already direct-to-CDN.** `/proxy/stream` is dead code
— nothing calls it. The only references are the SSRF test at `smoke_test.py:208`
and its own definition. The real path is:

- `templates/watch.html:23` — `<source src="{{ stream.url }}">` puts the raw
  `googlevideo.com` URL straight in the page.
- `views/api.py:174` and 181 — `/api/stream/<id>` already 302s to the CDN URL.
- `static/js/player.js:102, 130` — sets `video.src` to the resolved URL.

So there was never any YouTube bandwidth crossing your box. Removing the proxy
frees the memory and the worker slots it would have held, and closes the SSRF
surface, but it won't move your bandwidth graph.

**2. The bandwidth you're paying for is local files, and no CDN can offload
those until the bytes live on the CDN.** `/media/stream/` serves files off
`MEDIA_LIBRARY` folders on your own drive. A signed URL pointing at a CDN edge
only works if that edge can fetch the file — so you need either an origin-pull
CDN configured against a host that can reach those folders, or the files synced
into object storage (R2, S3, B2). The change set supports both; `CDN_MODE=off`
leaves current behaviour exactly as it is.

**3. Transcoded files can't be offloaded at all.** `_needs_transcode()`
(lines 94–101) catches MKV/AVI/WMV/FLV/M2TS/MPG and HEVC/VC1/MPEG-4 content.
Those are generated on the fly and have no stable object to sign. `cdn.py`
refuses anything outside `.mp4/.m4v/.webm/.mov` for playback for exactly this
reason and falls through to the existing pipe. If MKVs are a big share of your
traffic, pre-remuxing them to MP4 once (`-c copy`, near-free for H.264) will do
more for your bill than any of this.

## Files in this change set

- **`cdn.py`** → drop at `thehub/hub/services/cdn.py`. New file, no edits to
  anything it imports.
- **`cdn-offload.patch`** → `git apply cdn-offload.patch` from `thehub/`.
  Touches four files: `views/media.py`, `views/watch.py`, `config.py`,
  `static/js/player.js`. No repo, database, or scanner logic is changed.
- **`build_ytdlp.py`** → drop at `YT1/build_ytdlp.py`.

## Config

Add to `.env`. Leaving `CDN_MODE=off` is a no-op — every route behaves as it
does today.

```ini
# off | hmac | s3
CDN_MODE=s3
CDN_URL_TTL=21600
CDN_KEY_PREFIX=media

# Vanity domain in front of the bucket or edge. Required for hmac.
CDN_BASE_URL=https://cdn.example.com

# hmac mode only — must match the secret in your nginx/Worker config
CDN_SIGNING_KEY=

# s3 mode only (works with R2, B2, MinIO — set S3_ENDPOINT_URL for those)
S3_BUCKET=my-media
S3_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
S3_ACCESS_KEY=
S3_SECRET_KEY=
S3_REGION=auto

# Optional: separate origin for live HLS segments
CDN_LIVE_BASE_URL=
```

`s3` mode needs `boto3` (`pip install boto3`); `hmac` mode has no new
dependencies.

### Key layout

`cdn.object_key()` maps `E:/Media/Movies/Heat.mp4` → `media/Movies/Heat.mp4`,
i.e. `{CDN_KEY_PREFIX}/{shelf name}/{path relative to that shelf's folder}`.
Mirror your folders with that shape and the mapping needs no database column:

```bash
rclone sync "E:/Media/Movies" r2:my-media/media/Movies \
  --include "*.{mp4,m4v,webm,mov}"
```

Anything not present under that key just falls back to local streaming, so a
partial sync is safe.

### hmac verification

If you go the `hmac` route, the edge validates
`md5(f"{expires}/{uri} {secret}")`, base64url, padding stripped. In nginx:

```nginx
location / {
    secure_link        $arg_md5,$arg_expires;
    secure_link_md5    "$secure_link_expires$uri $CDN_SIGNING_KEY";
    if ($secure_link = "")  { return 403; }
    if ($secure_link = "0") { return 410; }
}
```

## Auth note

`/media/stream/<id>` deliberately has no `@login_required` (see the docstring
at 76–83) because `<video>` elements don't reliably carry the session cookie.
Signed URLs are a straight improvement here: `/api/media/<id>/url` **is**
`@login_required`, so the session check happens once at the metadata call, and
what the player gets is a credential that expires on its own. Set
`CDN_URL_TTL` to something a bit longer than your longest file.

## Client behaviour

`initCdnDirect()` in `player.js` handles the one thing signed URLs break: a tab
left paused past the TTL, or a seek that lands after expiry. Both surface as a
`MediaError` code 2 or 4, and both are fixed by re-fetching
`/api/media/<id>/url` — a few hundred bytes — and restoring the playhead. It
only binds for `local_` IDs, so YouTube playback is untouched.

## Building yt-dlp

The vendored tree at `yt-dlp-src/yt-dlp/` is stock **2025.09.05**. It already
ships its own build system (`bundle/pyinstaller.py`, `devscripts/`, a Makefile),
so `build_ytdlp.py` is a wrapper that runs the steps in order — deps, lazy
extractors, PyInstaller, smoke test — rather than a new build system.

```bash
python build_ytdlp.py --install
```

PyInstaller can't cross-compile, so run it on Windows to get `yt-dlp.exe`.

One caveat worth flagging: that source is roughly eleven months old, and
YouTube extraction is the part of yt-dlp that breaks most often — signature and
player-client handling change every few weeks. Building the vendored 2025.09.05
tree gives you a binary that behaves exactly like the `yt-dlp.exe` already in
the archive, including whatever is currently failing in
`streams.PLAYER_CLIENTS`. If the goal is working extraction rather than a
reproducible build of that specific revision, pull current upstream into
`yt-dlp-src/` first and build that.

If "update the yt-dlp source" meant something more specific than refreshing it,
say what and I'll take a look.
