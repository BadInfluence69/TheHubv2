# The Hub
# watch/tubi_100000935
A self-hosted front end for YouTube, Tubi, and the video files on your own
drives. Search, watch, comment, like, subscribe, build playlists — and publish
to YouTube when you want to.

The difference from just using youtube.com: **every social feature here writes
to a SQLite file on your machine and nowhere else.** Commenting here doesn't
comment on YouTube. Liking here doesn't touch your account. Subscribing here
changes your feed on this server only. The one exception is Studio, which
uploads videos to YouTube because that's what you asked it to do.

---

## Contents

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Setting up YouTube uploads](#setting-up-youtube-uploads)
- [Your local media library](#your-local-media-library)
- [How the feed is built](#how-the-feed-is-built)
- [Tubi search](#tubi-search)
- [Messages, rooms, and video responses](#messages-rooms-and-video-responses)
- [Running it properly](#running-it-properly)
- [Watching on a TV or Roku](#watching-on-a-tv-or-roku)
- [Upgrading from the old single-file version](#upgrading-from-the-old-single-file-version)
- [How it's put together](#how-its-put-together)
- [When things go wrong](#when-things-go-wrong)

---

## Quick start

You need **Python 3.10 or newer**, plus **yt-dlp** and **ffmpeg**.

```bash
# 1. Get the dependencies
pip install -r requirements.txt

# 2. Make your config file
cp .env.example .env

# 3. Generate a secret key and paste it into .env as SECRET_KEY=
python -c "import secrets; print(secrets.token_hex(32))"

# 4. Start it
python run.py
```

Open `http://localhost:5002`. The first account you create becomes the
administrator.

`run.py` prints a checklist at startup showing which tools it found and which
optional pieces aren't configured yet, so you can see at a glance what's ready.

### Getting yt-dlp and ffmpeg

`yt-dlp` resolves a video into a playable stream. Without it, nothing streams.

```bash
pip install -U yt-dlp
```

`ffmpeg` makes thumbnails for local files and converts formats browsers won't
play (MKV, HEVC, and friends). Local MP4s work without it; everything else
doesn't.

| Platform | Command |
|---|---|
| macOS | `brew install ffmpeg` |
| Debian / Ubuntu | `sudo apt install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg`, or download from [ffmpeg.org](https://ffmpeg.org/download.html) |

On Windows you can also just drop `yt-dlp.exe`, `ffmpeg.exe`, and `ffprobe.exe`
next to `run.py` — they'll be found automatically.

> **Keep yt-dlp updated.** YouTube changes how streams are served fairly often,
> and yt-dlp chases those changes. If videos suddenly stop playing, run
> `pip install -U yt-dlp` before assuming anything else is broken.

---

## Configuration

Everything lives in `.env`. Nothing is hard-coded, and `.env` is gitignored so
your keys stay out of version control.

### The one setting you must change

```ini
SECRET_KEY=<paste the generated value here>
```

This signs session cookies and encrypts your stored Google token. If you leave
it blank, a random one is generated at startup — which means **everyone gets
signed out every time you restart**, and any connected Google account has to be
reconnected.

### Settings worth knowing about

| Setting | Default | What it does |
|---|---|---|
| `PORT` | `5002` | Port to listen on |
| `DEBUG` | `false` | Leave off. Debug mode exposes a Python console to anyone who can reach the server |
| `ALLOW_REGISTRATION` | `true` | Turn off once everyone who needs an account has one |
| `MAX_UPLOAD_MB` | `4096` | Largest file accepted in one upload |
| `MEDIA_LIBRARY` | — | Your local video folders (see below) |
| `YOUTUBE_API_KEY` | — | Optional. Makes search faster and more reliable |
| `COOKIES_FILE` | `./cookies.txt` | Browser cookies, if you want age-restricted videos to resolve |
| `SPONSORBLOCK` | `true` | Marks sponsor segments on the timeline where data exists |
| `TUBI_USE_PLAYWRIGHT` | `false` | Adds a headless browser as a last-resort Tubi search backend |
| `RECOMMEND_HALF_LIFE_DAYS` | `14` | How long an interaction takes to count half as much |
| `RECOMMEND_QUERIES` | `4` | Search phrases per feed build. Each one costs quota |
| `RECOMMEND_MAX_SHARE` | `0.30` | Most of one feed any single channel or topic may occupy |
| `RECOMMEND_ROTATE_MINUTES` | `30` | How long the same phrases are reused before the feed turns over |

The full annotated list is in `.env.example`.

### About the YouTube API key

Optional. With one, search uses the official Data API — fast, reliable,
returns durations. Without one, The Hub reads YouTube's public results page
instead, which works but breaks whenever they change their markup.

Get one at [console.cloud.google.com](https://console.cloud.google.com) →
**APIs & Services** → **Credentials** → **Create credentials** → **API key**,
with the **YouTube Data API v3** enabled on the project.

An API key **cannot upload videos**. That needs OAuth — next section.

---

## Setting up YouTube uploads

Uploading acts on your behalf, so it needs OAuth rather than an API key. You do
this once; after that The Hub keeps a refresh token and won't ask again.

**1.** Go to [console.cloud.google.com](https://console.cloud.google.com) and
create a project (or reuse one).

**2.** Enable **YouTube Data API v3** under *APIs & Services → Library*.

**3.** Configure the **OAuth consent screen**:
- User type: **External**
- Fill in an app name and your email
- Under *Test users*, add the Google account you'll upload from

  You don't need to publish or verify the app. Leaving it in Testing mode is
  fine for personal use — the only catch is that refresh tokens expire after
  seven days, so you'll reconnect weekly. Publishing the app (it stays private
  either way) removes that.

**4.** Create credentials: *APIs & Services → Credentials → Create credentials
→ OAuth client ID → **Web application***.

**5.** Add this **authorised redirect URI**, matching the host and port you
actually use:

```
http://localhost:5002/studio/oauth/callback
```

The Studio page shows the exact URI to paste, so copy it from there rather than
typing it.

**6.** Download the JSON and save it as `client_secret.json` next to `run.py`.
Or skip the file and put the two values in `.env`:

```ini
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
```

**7.** Restart, open **Studio**, and click **Connect Google account**.

### What you can do once connected

- Upload from your computer, or publish a file already in your local library
  (no second copy is made — it streams straight off disk)
- Title, description, tags, category, thumbnail
- Private / unlisted / public, plus scheduled publishing
- Live progress with resumable chunked transfer, so a dropped connection
  retries the failed chunk instead of restarting a 3 GB file
- Edit or delete videos on your channel from **Studio → My channel**

### The quota ceiling

Google gives each Cloud project **10,000 API units a day**, and an upload costs
**1,600**. That's roughly **six uploads per day** before you get
`quotaExceeded`. It resets at midnight Pacific. You can request more from the
Cloud Console if you need it.

This is Google's limit, not something the app can work around. The Hub
translates the error into plain language rather than showing you a raw API
response.

### What Studio does not do

It never posts comments, likes, or subscriptions to YouTube. The OAuth scopes
allow it, but the app deliberately doesn't — keeping your local activity
separate from your real account is the entire point of the design.

---

## Your local media library

Point The Hub at your video folders with a JSON object of `{shelf name: path}`:

```ini
MEDIA_LIBRARY={"Movies": "E:/Media/Movies", "TV": "E:/Media/TV"}
```

Each entry becomes its own section on the **Local files** page and its own row
in the TV feed. Add as many as you want, named however you like.

For the common layout, point at one root instead and get Movies and TV for
free:

```ini
MEDIA_ROOT=E:/Media
```

**Windows paths:** use forward slashes (`E:/Media`) or doubled backslashes
(`E:\\Media`). A single backslash will be read as an escape character.

Folders are scanned up to four levels deep. Recognised extensions: `.mp4`,
`.mkv`, `.avi`, `.mov`, `.ts`, `.webm`, `.m4v`, `.mpg`, `.mpeg`, `.wmv`,
`.flv`, `.m2ts`.

Results cache for a minute so page loads don't re-walk a large drive. **Rescan
drives** on the Local files page forces a fresh look.

### How local playback works

- **MP4** streams directly with byte-range support, so seeking works properly
- **MKV, AVI, WMV, and HEVC** get converted on the fly by ffmpeg. H.264 inside
  a stubborn container is copied across, which is nearly free; anything else
  gets a real encode, which is CPU-heavy
- **Thumbnails** are pulled from three minutes into each file and cached, so
  the first load of a big folder is slow and every load after is instant

Local files are never moved, modified, or deleted by The Hub.

---

## How the feed is built

The home page does not show YouTube's recommendations. It shows ones computed
here, from rows this server already has: your watch history, how far into each
video you actually got, and your local likes and dislikes.

Nothing is sent to Google to build this. The only thing that leaves the machine
is the search phrase at the end of it.

### The pipeline

| Step | What happens |
|---|---|
| **1. Profile** | Your history becomes weighted preferences (`services/preferences.py`) |
| **2. Queries** | Those preferences become a handful of search phrases |
| **3. Fetch** | The phrases run through the Data API, or the scraper if you have no key |
| **4. Dedupe** | Anything you have already watched is removed, along with repeats |
| **5. Score** | What is left is ranked against the profile |
| **6. Diversify** | Caps stop one channel or topic taking over |
| **7. Backfill** | Subscriptions and the local catalogue, so the page is never empty |

### What counts, and for how much

Three things are tracked separately, because they are not equally trustworthy:
the **channel** (strongest, and unambiguous), the video's own **tags** (good
when present, often absent), and **keywords** from the title (always there,
noisiest).

Interactions are weighted before anything else happens:

| Interaction | Weight |
|---|---|
| Like | `+3.0` |
| Dislike | `-5.0` — deliberately louder than a like |
| Watch | `0.5 + 1.5 x completion` |

A watch is scored by *how much of it you watched*, not by the click. Sitting
through 90% of something is worth nearly four times opening it and bouncing,
which is the difference between "I liked this" and "the thumbnail worked".

Everything then decays: `0.5 ** (age_days / RECOMMEND_HALF_LIFE_DAYS)`. At the
default of 14 days, last month still counts for something and last year barely
registers. Lower the value to react faster to a change of taste; raise it to
keep long-standing interests alive.

Features that end up net-negative are pushed down the feed rather than removed
outright, so one bad afternoon does not permanently blacklist a topic.

### How the search phrases are chosen

Not "the top four things you like" — that just returns more of what your
history is already made of. The mix is:

- **two pairings** of a strong interest with a second one, picked
  weighted-random so they vary between refreshes
- **one channel** you watch often but have not subscribed to
- **one long-tail interest**, so the feed keeps a way out of whatever it has
  decided you are

### Quota, and why the feed holds still

`search.list` costs 100 units against a default allowance of 10,000 a day. Four
phrases per feed build is roughly 25 builds a day at worst.

Two things keep it well under that. Search results are cached for five minutes,
and the phrases themselves are held steady for `RECOMMEND_ROTATE_MINUTES` at a
time. Fully random phrases would defeat the cache completely — every refresh
would be four fresh API calls. Holding them means two page loads a minute apart
get the same feed and cost nothing extra, while an hour apart gets a new one.

It also just reads better. A feed that reshuffles itself every time you hit
back is a feed you cannot find anything in.

### The diversity cap

`RECOMMEND_MAX_SHARE` (default `0.30`) is the most of one feed any single
channel or topic may occupy. On a 48-slot feed that is 14 videos.

When the candidate pool genuinely is all one thing, the cap is raised a step at
a time — 1x, 1.5x, 2x, then off — rather than abandoned the moment the first
video is set aside. The ceiling gives way gradually instead of collapsing, and
the feed still fills. `enforce_diversity(..., strict=True)` holds the cap
absolutely and accepts a shorter feed instead.

Videos with no clear topic are exempt from the topic cap. Otherwise everything
unclassifiable would compete for a single bucket and throttle itself.

### Why am I being shown this

```
GET /feed/why
```

Returns the profile as it currently stands — top channels, tags and keywords
with their weights, anything scored negative, the search phrases built from
them, and how many videos are being excluded as already watched.

That is the whole input to the feed, not a summary of it. If the home page is
showing you something strange, this says why.

### Tags

The Data API only returns a video's tags from `videos.list`, not from search
results. The Hub already makes that second call to fill in durations, and the
endpoint is charged per call rather than per part — so asking for `snippet` at
the same time costs nothing extra and gives the profile its best signal. Tags
are stored in the `videos.tags` column as JSON.

If you are upgrading, that column is added automatically on first start.

---

## Tubi search

Tubi publishes no search API. Everything here reads something that was not
meant to be read by us, and it will break again eventually — so the design
assumes that rather than pretending otherwise.

### Four ways in, tried in order

| Backend | What it reads | Notes |
|---|---|---|
| `api` | `tubitv.com/oz/search` and two `api.tubitv.com` paths | Fastest. Sends the query parameters their own web client sends |
| `website` | The search page's embedded JSON | Handles the old `__NEXT_DATA__` blob and the newer streamed format |
| `playwright` | The page, in a real browser | Off by default. See below |
| — | — | If all of them miss, the rest of the search results still render |

Each backend may fail on its own without stopping the others.

### Why it broke before

Three reasons, all worth knowing about because they will recur:

1. **The parser reached into a fixed path.** It wanted
   `payload["contents"]`, so any other envelope returned nothing — even from a
   perfectly good `200`.
2. **`__NEXT_DATA__` went away.** Tubi's newer pages stream their data through
   many `self.__next_f.push()` calls instead of one script tag, so the regex
   matched nothing.
3. **Failures were invisible.** Everything was logged at `debug` and returned
   an empty list, so a dead backend and a search with no matches produced the
   identical blank grid.

### What replaced it

Extraction is now shape-agnostic. Instead of a fixed path, it walks the whole
payload looking for objects that *look* like content — a title, and a numeric
ID — wherever they happen to sit. Genre rows and carousel headers are rejected
because their IDs are not numeric. Posters are pulled through a resolver that
copes with a dict of named lists, a flat list of URLs, a list of `{url: ...}`
dicts, or a bare string.

When Tubi moves things around, a walker usually survives. A fixed path never
does.

### Telling a breakage from a blank

```python
outcome = tubi.search_detailed("the quiet earth")

outcome.results        # list of videos
outcome.ok             # False if every backend failed
outcome.reason         # "api: HTTP 403; search page: no usable JSON found"
outcome.attempts       # ["api", "website"]
outcome.drm_filtered   # how many protected titles were held back
```

The search page uses this to say *"Tubi search isn't responding right now"*
instead of *"No matches"*, which are very different pieces of information.

`tubi.search()` still returns a plain list, so existing callers are unchanged.

### When it stops working

```python
from hub.services import tubi
tubi.diagnose("matrix")
```

Runs every backend and reports what each one did — status codes, parse
failures, and a sample title from whichever route worked. That tells you which
layer moved instead of leaving you to guess.

If `api` and `website` are both failing, turn on the browser backend:

```bash
pip install playwright && playwright install chromium
# then set TUBI_USE_PLAYWRIGHT=1 in .env
```

It is slow and heavy, which is why it is off by default — but it survives
changes the other two cannot, because it runs the page the way a browser does.

### DRM

Some Tubi titles are DRM-protected and cannot be played by a generic
downloader. These are **detected and held back** from the results, with a note
saying how many were skipped, so the grid does not fill with entries that will
fail when clicked. `resolve_stream()` says so plainly if you reach one
directly.

The Hub does not attempt to work around the protection. Those titles are
watchable on tubitv.com, and that is where the link sends you.

### Route spelling

Results carry `watch_path`, which is `/watch/Tubi_{id}` as requested.

Stored IDs stay lowercase (`tubi_123456`), because that is the existing primary
key in your `videos` table and what four other modules test for. The watch
route accepts any capitalisation and redirects to the canonical form, so
`/watch/Tubi_123`, `/watch/TUBI_123` and `/watch/tubi_123` are one page and one
database row rather than three.

---

## Messages, rooms, and video responses

Three social features, plus a privacy page that describes what this server
actually stores. Full detail in **[SOCIAL_FEATURES.md](SOCIAL_FEATURES.md)**.

### Direct messages — `/messages/`

Encrypted end to end. Your browser generates an ECDH P-256 key pair and wraps
the private half with a passphrase that never reaches the server. Messages are
stored as ciphertext this server holds no key for.

The passphrase is **not** your account password, on purpose: the server sees
your account password when you sign in, so a vault locked with it would be a
vault the server could open. There is no reset — losing it loses your messages,
which is the same property as "the operator can't read them" viewed from the
other side.

> **Messaging needs HTTPS.** Browsers switch off Web Crypto on insecure
> origins, so reaching this server at `http://192.168.x.x:5002` leaves
> encryption unavailable and the UI will say so. Put it behind a reverse proxy
> with HTTPS, or use `localhost`. Everything else in The Hub works fine over
> plain HTTP.

### Rooms — `/chat/`

Public conversation between accounts on this server. Every route requires a
sign-in, so a room is readable here and nowhere else.

Rooms are **not** encrypted and cannot be — open membership means no key that
only the participants hold. Room posts are plain text and whoever runs this
server can read them. The room page says so. Use direct messages for anything
that belongs between two people.

### Video responses

A comment can carry a video instead of, or as well as, text — the old YouTube
feature. Upload a clip, or paste a link to something already in the catalogue.
Responses get their own watch page, a shelf on the video they answer, and a
backlink saying what they were responding to.

### Verifying the encryption

The claim on the privacy page rests on `hub/static/js/e2ee.js`, so it is worth
running rather than trusting:

```bash
node verify_e2ee.js       # loads the real crypto file, 28 checks
python3 social_test.py    # messaging, rooms, responses, 75 checks
python3 smoke_test.py     # everything else, 51 checks
```

The interesting one: a third party holding **both public keys and the wrapped
private key** — exactly what this server stores — cannot decrypt anything.

### What is and isn't protected

| | |
|---|---|
| Message content | Encrypted. Server holds no key |
| Who messaged whom, and when | **Stored in the clear.** Routing requires it |
| Room posts | **Plain text.** Readable by the operator |
| Comments, likes, watch history | **Plain text**, as before |
| Old messages if your passphrase leaks | **Exposed** — no forward secrecy |

`/privacy` has the full version, including what could and could not be produced
under a legal order. If you change how this app handles data, change that page
to match — it is only worth having while it is accurate.

---

## Running it properly

`python run.py` uses Flask's development server. It's fine for trying things
out, but it's single-threaded under load and not meant to stay running.

### Linux and macOS

```bash
gunicorn -c gunicorn.conf.py wsgi:app
```

### Windows

```bash
waitress-serve --port=5002 --threads=16 wsgi:app
```

Both are in `requirements.txt`, installed per platform.

The gunicorn config runs **one worker with many threads** on purpose: upload
jobs run as background threads inside the process that started them, so
multiple worker processes would lose track of them. Threads are the right shape
here anyway, since the work is mostly waiting on yt-dlp, ffmpeg, and the
network.

### Docker

```bash
docker compose up -d
```

Edit `docker-compose.yml` first to mount your media folders. The image includes
ffmpeg and yt-dlp, so there's nothing to install on the host.

### Keeping it running

**Linux (systemd)** — save as `/etc/systemd/system/thehub.service`:

```ini
[Unit]
Description=The Hub
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/hub
ExecStart=/path/to/hub/.venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now thehub
```

**Windows** — use Task Scheduler with *At startup* as the trigger, or
[NSSM](https://nssm.cc/) to run it as a real service.

### A note on exposing this

The Hub is built for a home network. If you want to reach it from outside:

- Put it behind a reverse proxy (Caddy, nginx) with HTTPS
- Set `SESSION_COOKIE_SECURE=true`
- Set `ALLOW_REGISTRATION=false`
- Never set `DEBUG=true`
- Consider `STORE_SESSION_IP=0` to stop recording IP addresses with sessions

HTTPS is also what makes encrypted messaging work at all — see
[Messages, rooms, and video responses](#messages-rooms-and-video-responses).

The device API endpoints under `/api/` are unauthenticated by design, since TVs
and streaming boxes can't hold a session cookie. They're read-only, but they do
list your catalogue. If the server is reachable from the internet, restrict
`/api/` at the proxy.

---

## Watching on a TV or Roku

The device endpoints return plain JSON that a simple TV app can consume:

| Endpoint | Returns |
|---|---|
| `GET /api/feed` | Rows of content, grouped into categories |
| `GET /api/feed?q=term` | Everything filtered by a search |
| `GET /api/search?q=term` | Flat search results |
| `GET /api/stream/<video_id>` | Redirects to something playable |
| `GET /api/health` | Status, catalogue size, and the URL other devices should use |

Hit `/api/health` from the device to confirm it can reach the server and to see
the address it should be calling. If auto-detection picks the wrong network
interface (common with VPNs or multiple adapters), set `PUBLIC_HOST` in `.env`.

---

## Upgrading from the old single-file version

**Your data comes across automatically.** Copy your existing `hub.db` to
`data/hub.db` and start the app. On first run it will:

- Rename the old tables to `legacy_*` as a backup, then create the new ones
- Bring across every video, comment, feedback post, and subscription
- Preserve your existing like and dislike counts, adding new votes on top
- Match old comments to accounts by username
- Give every account the subscription list that used to be shared globally
- Create Watch later and Liked videos playlists for existing accounts

It's safe to run more than once — nothing is duplicated. The `legacy_*` tables
stay in the file until you delete them yourself, so you can check everything
arrived before cleaning up.

### Things that changed, and why

**Sign-in was rewritten.** The old version stored your plain username in a
cookie and trusted it, which meant anyone could open devtools, type
`local_user_session=aiden`, and be you. Sessions are now random tokens stored
hashed server-side, and every form carries a CSRF token.

**Likes became real votes.** `increment_likes` only ever counted up — no
un-liking, no per-user record, and a refresh could double-count. There's now
one vote per person per video, and pressing the same button twice clears it,
the way YouTube behaves. Your old totals are preserved separately and added to
the new counts.

**The stream proxy was locked down.** `/proxy/stream` would fetch any URL handed
to it, including addresses on your internal network. It now only forwards to
hosts yt-dlp actually resolved.

**A bug in the Roku feed was fixed.** In the old file, `"streamformat": "mp4",
"mkv"` silently created a key called `"mkvthumbnail"`, so thumbnails went
missing from that row.

**The API key left the source code.** Everything sensitive is in `.env` now.
Rotate the old key if it was ever committed anywhere — treat it as exposed.

---

## How it's put together

```
hub/
├── run.py                  Development server, with a startup checklist
├── wsgi.py                 Entry point for gunicorn / waitress
├── smoke_test.py           Walks every route against a throwaway database
├── test_recommend.py       Checks the feed offline, with search stubbed out
├── test_tubi.py            Checks the Tubi parser against recorded payload shapes
├── test_migrations.py      Upgrades databases of several ages and checks nothing is lost
├── .env.example            Annotated configuration template
│
└── hub/
    ├── __init__.py         App factory
    ├── config.py           Environment-driven settings
    ├── db.py               Connections, schema setup, legacy migration
    ├── schema.sql          Table definitions
    ├── security.py         Sessions, CSRF, password handling
    ├── filters.py          Template formatting helpers
    │
    ├── repo/               Data access — every SQL statement lives here
    │   ├── users.py        Accounts, settings, notifications
    │   ├── videos.py       Catalogue and the vote ledger
    │   ├── comments.py     Threaded comments
    │   ├── subs.py         Subscriptions
    │   ├── library.py      Playlists, history, search history
    │   ├── studio.py       Upload jobs, connected Google account
    │   └── feedback.py     Ideas board
    │
    ├── services/           Everything that talks to the outside world
    │   ├── youtube_search.py   Data API, with a scraping fallback
    │   ├── streams.py          yt-dlp wrapper, caching, quality list
    │   ├── local_media.py      Drive scanning, safe path resolution
    │   ├── tubi.py             Tubi search, with four fallback backends
    │   ├── google_oauth.py     OAuth flow, encrypted token storage
    │   ├── youtube_upload.py   Resumable uploads, video management
    │   ├── preferences.py      Weighted, decayed picture of what you like
    │   └── recommend.py        Home feed assembly
    │
    ├── views/              Routes, one module per area
    ├── templates/
    └── static/
```

The layering is deliberate: **views** handle HTTP and nothing else, **repo**
owns all SQL, **services** own all outside contact. If you need to change how
something is stored, there's exactly one file to open.

### Checking your changes

```bash
python smoke_test.py       # every route, against a throwaway database
python social_test.py      # messages, rooms, blocking, encryption
python test_recommend.py   # the feed: profile, dedupe, decay, diversity
python test_tubi.py        # the Tubi parser, against recorded payload shapes
python test_migrations.py  # old databases upgrade cleanly and keep their data
```

The first creates an account in a temporary database, walks every page and API
endpoint, exercises the social features, and checks that CSRF protection, the
proxy allow-list, and the sign-in requirement all still hold.

The third stubs out the YouTube call entirely, so it needs no API key and no
network — it builds a synthetic watch history with a known shape and asserts
the feed behaves the way it is supposed to. All three run offline in a few
seconds.

---

## When things go wrong

**It won't start: `no such column: response_video_id`**

An older database, and a bug in how it was upgraded. `schema.sql` builds an
index on `comments.response_video_id`, but `CREATE TABLE IF NOT EXISTS` will
not add a column to a table that already exists — so on a database created
before video responses, the index was built against a column that was not
there yet. The column migrations now run *before* the schema script rather than
after, which is the order that was always intended.

Nothing is lost and there is nothing to do by hand: start it again and the
database upgrades in place. `python test_migrations.py` proves it against
databases of several ages.

If you add a column later and index it in `schema.sql`, add it to
`LATE_COLUMNS` in `hub/db.py` too, or this returns.


**Videos won't play — "stream unavailable"**

Almost always a stale yt-dlp:

```bash
pip install -U yt-dlp
```

If that doesn't do it, try the same video from the command line:

```bash
yt-dlp -F "https://www.youtube.com/watch?v=VIDEO_ID"
```

The error yt-dlp prints will be more specific than anything the app can infer.
Age-restricted and members-only videos need browser cookies — export them with
a `cookies.txt` extension and point `COOKIES_FILE` at the file.

**Search returns nothing**

Without an API key, The Hub reads YouTube's results page, and YouTube blocks
some networks and most datacentre IP ranges. Setting `YOUTUBE_API_KEY` in
`.env` fixes it properly.

**A local file won't play**

Check that ffmpeg is installed and that `run.py`'s startup checklist finds it.
MKV, AVI, and HEVC all need it. Bear in mind that transcoding is CPU-heavy —
on a low-power NAS, converting 4K HEVC in real time may simply be beyond the
hardware.

**"That form expired. Reload the page and try again."**

The CSRF token didn't match, usually because the server restarted with a
randomly generated `SECRET_KEY`. Set a fixed one in `.env`.

**Everyone gets signed out on restart**

Same cause. Set `SECRET_KEY`.

**Studio says the Google account needs reconnecting**

Either `SECRET_KEY` changed (which makes the stored token undecryptable — a
deliberate safety property), or your OAuth consent screen is still in Testing
mode, where refresh tokens expire after seven days. Publishing the app removes
the weekly reconnect; it stays private either way.

**Uploads fail with a quota message**

You've used the day's 10,000 units. It resets at midnight Pacific. Request a
higher quota from the Cloud Console if six uploads a day isn't enough.

**Port already in use**

Something else has 5002. Change `PORT` in `.env`, or stop the other process.

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `/` | Jump to search |
| `space` or `k` | Play / pause |
| `j` / `l` | Back / forward 10 seconds |
| `←` / `→` | Back / forward 5 seconds |
| `0`–`9` | Jump to that point in the video |
| `m` | Mute |
| `f` | Fullscreen |
| `c` | Captions |
| `<` / `>` | Slower / faster |

---

## A word on what this is for

The Hub is a personal tool for watching content you already have the right to
watch, on hardware you own. Scraping and stream extraction sit in a grey area
that varies by jurisdiction and by the terms of the services involved — worth
knowing before you point it at anything beyond your own use.

The upload side is different and unambiguous: it uses Google's official API
with your own credentials, and only publishes videos you explicitly choose to
publish.
