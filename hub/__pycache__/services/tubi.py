"""
Tubi support.

Two ways in: paste an ID or URL from Tubi's address bar, or search.

Searching is the fragile part. Tubi publishes no documented search API, so
every route in here is reading something that was not meant to be read by us
and can change without notice. The response to that is not one clever parser
but several dumb ones, tried in order, each independently allowed to fail:

    1. the JSON API on api.tubitv.com, with the query parameters their own
       web client sends
    2. the same API without those parameters, in case they are dropped
    3. the search page itself, which embeds its data as JSON — both the old
       __NEXT_DATA__ blob and the newer streamed format
    4. a headless browser, if TUBI_USE_PLAYWRIGHT is on and Playwright is
       installed, for when the page only assembles its results in JavaScript

Extraction is deliberately shape-agnostic. Rather than reaching into a fixed
path like payload["contents"][0]["images"]["thumbnail"][0], it walks whatever
came back looking for objects that have the fields a piece of content has.
When Tubi moves things around, a walker usually survives; a fixed path never
does.

On DRM: some Tubi titles are protected and cannot be played by a generic
downloader. This module *detects* those and marks them so the UI can say so.
It does not attempt to work around the protection, and nothing here would
help you do that.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field

import requests

from . import streams

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://tubitv.com/",
    "Origin": "https://tubitv.com",
}

# Stored IDs stay lowercase, because that is what is already in the database
# and what four other modules test for with startswith("tubi_"). The public
# watch path is built separately - see watch_path() below.
ID_PREFIX = "tubi_"

# Their web client identifies itself with these. Sending them is what usually
# separates a 200 from a 400 on the API route.
API_PARAMS = {
    "platform": "web",
    "device_id": "00000000-0000-4000-8000-000000000000",
    "app_id": "tubitv",
    "content_mode": "all",
    "limit": "40",
}

SEARCH_ENDPOINTS = (
    "https://tubitv.com/oz/search",
    "https://api.tubitv.com/v3/search",
    "https://api.tubitv.com/search",
)

TIMEOUT = 8


# --------------------------------------------------------------------------
# Result envelope
# --------------------------------------------------------------------------
@dataclass
class SearchOutcome:
    """
    What a search actually did, as opposed to what it returned.

    The old code returned a bare list, which made "Tubi has nothing called
    that" and "every backend errored" look identical to the caller - and so
    the search tab showed an empty grid either way. Keeping the reason
    alongside the results is what lets the UI tell the difference.
    """

    results: list[dict] = field(default_factory=list)
    ok: bool = True
    reason: str = ""
    attempts: list[str] = field(default_factory=list)
    drm_filtered: int = 0

    def __bool__(self) -> bool:
        return bool(self.results)

    def __iter__(self):
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)


# --------------------------------------------------------------------------
# IDs and routes
# --------------------------------------------------------------------------
def extract_id(raw: str) -> str | None:
    """Accepts a bare ID, a prefixed ID, or any tubitv.com URL."""
    if not raw:
        return None
    raw = raw.strip()

    if raw.lower().startswith(ID_PREFIX):
        raw = raw[len(ID_PREFIX):]

    if raw.isdigit():
        return raw

    match = re.search(
        r"tubitv\.com/(?:movies|tv-shows|series|videos|video)/(\d+)", raw, re.I
    )
    if match:
        return match.group(1)

    match = re.search(r"(\d{5,})", raw)
    return match.group(1) if match else None


def canonical_id(raw: str) -> str:
    """
    The form used as a primary key: tubi_123456, lowercase, always.

    Accepts Tubi_123456, TUBI_123456, a bare 123456, or a full URL. Without
    this, the same film reached by two different paths becomes two rows in
    the videos table.
    """
    numeric = extract_id(raw)
    return f"{ID_PREFIX}{numeric}" if numeric else str(raw)


def watch_path(raw: str) -> str:
    """The click target for a result: /watch/Tubi_123456."""
    numeric = extract_id(raw) or str(raw)
    return f"/watch/Tubi_{numeric}"


def is_tubi_id(raw: str) -> bool:
    return bool(raw) and str(raw).lower().startswith(ID_PREFIX)


# --------------------------------------------------------------------------
# DRM
# --------------------------------------------------------------------------
# Fields that, when present and set, mean the title is protected. This is used
# to label results honestly and keep unplayable rows out of the grid - not to
# get around anything.
DRM_HINTS = (
    "drm",
    "has_drm",
    "is_drm",
    "drm_protected",
    "license_server",
    "licenseUrl",
    "widevine",
    "playready",
    "fairplay",
)


def _looks_drm_protected(item: dict) -> bool:
    for key in DRM_HINTS:
        value = item.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, (str, dict, list)) and value:
            return True

    # Some payloads bury it one level down in the playback description.
    for container in ("video_resources", "playback", "manifest"):
        nested = item.get(container)
        if isinstance(nested, list):
            for entry in nested:
                if isinstance(entry, dict) and _looks_drm_protected(entry):
                    return True
        elif isinstance(nested, dict) and _looks_drm_protected(nested):
            return True

    return False


# --------------------------------------------------------------------------
# Shaping
# --------------------------------------------------------------------------
def _shape(raw_id: str, title: str, thumbnail: str, description: str,
           duration=None, year=None, drm: bool = False) -> dict:
    video_id = f"{ID_PREFIX}{raw_id}"
    return {
        "id": video_id,
        "video_id": video_id,
        "source": "tubi",
        "title": title or "Untitled",
        "channel": "Tubi",
        "channel_name": "Tubi",
        "thumbnail": thumbnail or "",
        "description": description or "",
        "duration": duration,
        "published_at": str(year) if year else None,
        "likes": 0,
        "dislikes": 0,
        "comment_count": 0,
        # Requested click target. The templates route through url_for(), which
        # produces the same page; this is here for any caller that wants the
        # literal path, and for the device/API JSON.
        "watch_path": watch_path(raw_id),
        "drm_protected": drm,
    }


def _first_image(item: dict) -> str:
    """
    Pull a poster out of whatever shape the images field is in today.

    Seen in the wild: a dict of named lists, a flat list of URLs, a list of
    dicts with a url key, and a bare string. All of them end up here.
    """
    images = item.get("images") or item.get("image") or item.get("posterarts")

    def first_url(value) -> str:
        if isinstance(value, str):
            return value if value.startswith("http") else ""
        if isinstance(value, dict):
            for key in ("url", "src", "href"):
                if isinstance(value.get(key), str):
                    return value[key]
            for nested in value.values():
                found = first_url(nested)
                if found:
                    return found
            return ""
        if isinstance(value, (list, tuple)):
            for entry in value:
                found = first_url(entry)
                if found:
                    return found
        return ""

    if isinstance(images, dict):
        # Prefer a real poster, then a thumbnail, then anything at all.
        for key in ("poster", "posterarts", "thumbnail", "landscape", "hero", "backgrounds"):
            found = first_url(images.get(key))
            if found:
                return found

    found = first_url(images)
    if found:
        return found

    for key in ("thumbnail", "poster", "backgroundImage", "landscape_images"):
        found = first_url(item.get(key))
        if found:
            return found
    return ""


def _looks_like_content(item) -> bool:
    """Does this object look like a film or show, rather than page furniture?"""
    if not isinstance(item, dict):
        return False
    if not (item.get("title") or item.get("name")):
        return False

    raw_id = item.get("id") or item.get("content_id") or item.get("contentId")
    if raw_id is None:
        return False

    # Tubi content IDs are numeric. Anything else is a row header, a genre
    # tag, or some other bit of the page that happens to have a title.
    return bool(re.fullmatch(r"\d{3,}", str(raw_id)))


def _walk_for_content(node, found: list, seen: set, depth: int = 0) -> None:
    """
    Depth-first hunt for content objects anywhere in a payload.

    Shape-agnostic on purpose: it does not matter whether the results arrive
    under "contents", "results", "hits", or nested three levels inside a
    Next.js state tree. Depth is bounded so a pathological payload cannot
    stall a request.
    """
    if depth > 12:
        return

    if isinstance(node, dict):
        if _looks_like_content(node):
            raw_id = str(
                node.get("id") or node.get("content_id") or node.get("contentId")
            )
            if raw_id not in seen:
                seen.add(raw_id)
                found.append(node)
            # Keep descending: series carry their episodes inside them.
        for value in node.values():
            _walk_for_content(value, found, seen, depth + 1)

    elif isinstance(node, list):
        for entry in node:
            _walk_for_content(entry, found, seen, depth + 1)


def _shape_many(payload, skip_drm: bool = True) -> tuple[list[dict], int]:
    """Turn any payload into result dicts. Returns (results, drm_skipped)."""
    found: list = []
    _walk_for_content(payload, found, set())

    results, drm_count = [], 0
    for item in found:
        raw_id = str(item.get("id") or item.get("content_id") or item.get("contentId"))
        drm = _looks_drm_protected(item)
        if drm:
            drm_count += 1
            if skip_drm:
                continue

        results.append(
            _shape(
                raw_id,
                item.get("title") or item.get("name"),
                _first_image(item),
                item.get("description") or item.get("synopsis") or "",
                item.get("duration") or item.get("duration_seconds"),
                item.get("year") or item.get("release_year"),
                drm=drm,
            )
        )
    return results, drm_count


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------
def _from_api(query: str) -> tuple[list[dict], int, str]:
    """Their JSON API, with and then without the web client's parameters."""
    last_error = ""

    for endpoint in SEARCH_ENDPOINTS:
        for params in ({"search": query, "q": query, **API_PARAMS}, {"q": query}):
            try:
                response = requests.get(
                    endpoint, params=params, headers=HEADERS, timeout=TIMEOUT
                )
            except requests.RequestException as exc:
                last_error = f"{endpoint}: {type(exc).__name__}"
                log.debug("Tubi API request failed (%s): %s", endpoint, exc)
                continue

            if response.status_code != 200:
                last_error = f"{endpoint}: HTTP {response.status_code}"
                log.debug("Tubi API %s returned %s", endpoint, response.status_code)
                continue

            try:
                payload = response.json()
            except ValueError:
                last_error = f"{endpoint}: response was not JSON"
                continue

            results, drm = _shape_many(payload)
            if results:
                return results, drm, ""
            last_error = f"{endpoint}: 200 but nothing matched"

    return [], 0, last_error


def _extract_embedded_json(html: str) -> list:
    """
    Every JSON blob the search page ships, whichever way it ships it.

    Tubi has used at least three: the classic __NEXT_DATA__ script, a window
    assignment, and the newer streamed format where the payload arrives in
    many self.__next_f.push() calls that have to be stitched together.
    """
    payloads: list = []

    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if match:
        try:
            payloads.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            pass

    for pattern in (
        r"window\.__data\s*=\s*({.*?});</script>",
        r"window\.__INITIAL_STATE__\s*=\s*({.*?});",
        r"window\.__PRELOADED_STATE__\s*=\s*({.*?});",
    ):
        for blob in re.findall(pattern, html, re.DOTALL):
            try:
                payloads.append(json.loads(blob))
            except json.JSONDecodeError:
                continue

    # Streamed React payload: concatenate the chunks, then pull out any
    # balanced JSON objects that mention a title.
    chunks = re.findall(r'self\.__next_f\.push\(\[\d+,\s*"(.*?)"\]\)', html, re.DOTALL)
    if chunks:
        joined = "".join(chunks)
        try:
            joined = joined.encode("utf-8").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        payloads += _balanced_objects(joined)

    return payloads


def _balanced_objects(text: str, limit: int = 400) -> list:
    """
    Scan for complete {...} objects containing a title, and parse each one.

    Brace-counting rather than a regex, because JSON nests and regexes do not
    count. Quote-aware so a brace inside a string does not throw it off.
    """
    objects: list = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    blob = text[start:index + 1]
                    if '"title"' in blob or '"name"' in blob:
                        try:
                            objects.append(json.loads(blob))
                        except json.JSONDecodeError:
                            pass
                        if len(objects) >= limit:
                            return objects
                    start = -1

    return objects


def _from_website(query: str) -> tuple[list[dict], int, str]:
    """Read the search page and mine whatever JSON it carries."""
    url = f"https://tubitv.com/search/{urllib.parse.quote(query)}"
    page_headers = dict(HEADERS)
    page_headers["Accept"] = "text/html,application/xhtml+xml"

    try:
        response = requests.get(url, headers=page_headers, timeout=TIMEOUT + 4)
    except requests.RequestException as exc:
        return [], 0, f"search page: {type(exc).__name__}"

    if response.status_code != 200:
        return [], 0, f"search page: HTTP {response.status_code}"

    for payload in _extract_embedded_json(response.text):
        results, drm = _shape_many(payload)
        if results:
            return results, drm, ""

    return [], 0, "search page: no usable JSON found in the markup"


def _from_playwright(query: str) -> tuple[list[dict], int, str]:
    """
    Last resort: run the page in a real browser and read the result afterwards.

    Off by default. It is slow, it needs a browser installed, and it is a lot
    of machinery for a search box. Turn it on with TUBI_USE_PLAYWRIGHT=1 when
    the cheaper routes have stopped working:

        pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 0, "playwright is not installed"

    url = f"https://tubitv.com/search/{urllib.parse.quote(query)}"
    captured: list = []

    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=HEADERS["User-Agent"])

                # Grab any JSON the page fetches for itself - usually cleaner
                # than whatever ends up in the rendered markup.
                def on_response(response):
                    if "search" not in response.url:
                        return
                    content_type = (response.headers or {}).get("content-type", "")
                    if "json" not in content_type:
                        return
                    try:
                        captured.append(response.json())
                    except Exception:
                        pass

                page.on("response", on_response)
                page.goto(url, wait_until="networkidle", timeout=25000)
                html = page.content()
            finally:
                browser.close()
    except Exception as exc:      # any browser failure is just another miss
        log.warning("Tubi Playwright fetch failed: %s", exc)
        return [], 0, f"playwright: {type(exc).__name__}"

    for payload in captured:
        results, drm = _shape_many(payload)
        if results:
            return results, drm, ""

    for payload in _extract_embedded_json(html):
        results, drm = _shape_many(payload)
        if results:
            return results, drm, ""

    return [], 0, "playwright: page loaded but held no recognisable results"


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------
def search_detailed(query: str, use_playwright: bool | None = None) -> SearchOutcome:
    """
    Search Tubi, reporting how it went.

    Every backend is tried in turn and any one of them may fail without
    stopping the others. The only way this raises is if the caller has done
    something wrong; network faults, protocol changes and empty responses all
    come back as a SearchOutcome with ok=False and a readable reason.
    """
    query = (query or "").strip()
    if not query:
        return SearchOutcome(ok=True, reason="empty query")

    if use_playwright is None:
        try:
            from flask import current_app
            use_playwright = bool(current_app.config.get("TUBI_USE_PLAYWRIGHT", False))
        except (ImportError, RuntimeError):
            use_playwright = False

    backends = [("api", _from_api), ("website", _from_website)]
    if use_playwright:
        backends.append(("playwright", _from_playwright))

    outcome = SearchOutcome()
    problems = []

    for name, backend in backends:
        outcome.attempts.append(name)
        try:
            results, drm_count, error = backend(query)
        except Exception as exc:      # a backend must never take the page down
            log.warning("Tubi %s backend raised: %s", name, exc, exc_info=True)
            problems.append(f"{name}: {type(exc).__name__}")
            continue

        if results:
            outcome.results = results
            outcome.drm_filtered = drm_count
            outcome.ok = True
            log.info(
                "Tubi search for %r: %s results via %s%s",
                query, len(results), name,
                f", {drm_count} DRM-protected skipped" if drm_count else "",
            )
            return outcome

        if error:
            problems.append(error)

    outcome.ok = False
    outcome.reason = "; ".join(problems) or "no results"
    log.warning("Tubi search for %r found nothing. Tried: %s", query, outcome.reason)
    return outcome


def search(query: str) -> list[dict]:
    """
    Plain list of results, for callers that only want the happy path.

    Kept at this signature because the search view and the device API both
    already call it this way. Use search_detailed() if you want to tell a
    failure from a genuine blank.
    """
    return search_detailed(query).results


def diagnose(query: str = "matrix") -> dict:
    """
    Run every backend and report what each one did.

    Meant to be run by hand when the Tubi tab goes quiet:

        flask --app run shell
        >>> from hub.services import tubi; tubi.diagnose("matrix")

    Tells you which route broke and how, instead of leaving you to guess.
    """
    report: dict = {"query": query, "backends": {}}

    for name, backend in (
        ("api", _from_api),
        ("website", _from_website),
        ("playwright", _from_playwright),
    ):
        try:
            results, drm_count, error = backend(query)
            report["backends"][name] = {
                "results": len(results),
                "drm_skipped": drm_count,
                "error": error or None,
                "sample": results[0]["title"] if results else None,
            }
        except Exception as exc:
            report["backends"][name] = {"results": 0, "error": f"{type(exc).__name__}: {exc}"}

    working = [n for n, r in report["backends"].items() if r.get("results")]
    report["working"] = working
    report["summary"] = (
        f"{', '.join(working)} working" if working else "no backend returned results"
    )
    return report


def resolve_stream(video_id: str) -> dict:
    """
    Hand back a playable stream for a Tubi ID.

    Accepts tubi_123456, Tubi_123456, or a bare 123456. If the title turns out
    to be DRM-protected, this reports that plainly rather than half-failing:
    the stream genuinely cannot be played here, and saying so is more useful
    than a generic timeout.
    """
    raw_id = extract_id(video_id)
    if not raw_id:
        raise streams.StreamError(f"{video_id!r} is not a Tubi ID.")

    last_error: Exception | None = None
    for path in ("movies", "tv-shows", "series", "video"):
        try:
            return streams.resolve_generic(f"https://tubitv.com/{path}/{raw_id}")
        except streams.StreamError as exc:
            last_error = exc

    detail = str(last_error or "")
    if re.search(r"drm|widevine|playready|fairplay|encrypt", detail, re.I):
        raise streams.StreamError(
            f"Tubi title {raw_id} is DRM-protected, so it can't be played here. "
            "Watch it on tubitv.com instead."
        )

    raise streams.StreamError(
        f"Tubi wouldn't hand over a stream for {raw_id}. {detail}".strip()
    )
