"""
The Hub's Algorithm System

Building the home feed.

This replaces YouTube's recommendations with ones computed here, from rows
this server owns. The pipeline, in order:

    1. profile     what the account likes, weighted and decayed  (preferences.py)
    2. queries     turn that profile into a handful of search phrases
    3. fetch       run them through the Data API (or the scrape fallback)
    4. dedupe      drop anything already watched, and any repeats in the batch
    5. score       rank what is left against the profile
    6. diversify   cap how much of the feed one channel or topic may take
    7. backfill    subscriptions and the local catalogue, so it is never empty

Quota note: search.list costs 100 units against a default 10,000/day budget,
so the number of queries per feed is deliberately small and the phrases are
held steady for RECOMMEND_ROTATE_MINUTES at a time. That makes the 5-minute
result cache in youtube_search actually useful, and stops the feed reshuffling
itself every time the page is refreshed.
"""
from __future__ import annotations

import logging
import random
import time

from flask import current_app

from ..repo import library as library_repo
from ..repo import subs as subs_repo
from ..repo import videos as videos_repo
from . import local_media, preferences, youtube_search

log = logging.getLogger(__name__)

# Used before there is any history to work with.
COLD_START = [
    "documentary", "gaming leaks", "live music", "tech review", "classic movie scenes",
     "space exploration", "retro gaming", "car restoration",
]

# Fallbacks for anything the config does not set.
DEFAULT_QUERIES = 5
DEFAULT_MAX_SHARE = 0.30
DEFAULT_ROTATE_MINUTES = 30
DEFAULT_HALF_LIFE_DAYS = 14

# Roughly how many candidates to gather per slot in the finished feed. Scoring
# and the diversity caps both need something to choose between.
CANDIDATE_MULTIPLIER = 3


def _setting(name: str, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:      # outside an application context
        return default


# --------------------------------------------------------------------------
# Query generation
# --------------------------------------------------------------------------
def _rng(user_id: int) -> random.Random:
    """
    A generator that returns the same numbers for the same account within one
    rotation window. Two page loads a minute apart get the same feed; an hour
    apart they get a different one.
    """
    minutes = int(_setting("RECOMMEND_ROTATE_MINUTES", DEFAULT_ROTATE_MINUTES))
    window = int(time.time() // max(minutes * 60, 60))
    return random.Random(f"{user_id}:{window}")


def _weighted_sample(
    entries: list[tuple[str, float]], count: int, rng: random.Random
) -> list[str]:
    """Pick `count` distinct features, favouring the heavier ones."""
    pool = [(name, weight) for name, weight in entries if weight > 0]
    chosen: list[str] = []
    while pool and len(chosen) < count:
        total = sum(weight for _, weight in pool)
        if total <= 0:
            break
        target = rng.uniform(0, total)
        running = 0.0
        for index, (name, weight) in enumerate(pool):
            running += weight
            if running >= target:
                chosen.append(name)
                pool.pop(index)
                break
    return chosen


def build_queries(
    profile: preferences.Profile, user_id: int, count: int | None = None
) -> list[str]:
    """
    Turn the weighted profile into search phrases.

    The mix is intentional rather than just "top N terms":

      - a couple of pairings of a strong interest with a second one, which is
        what actually finds things you have not seen rather than more of what
        your history is already made of
      - one channel you watch a lot but have not subscribed to
      - one deliberately weaker interest, so the feed keeps a way out of
        whatever it has decided you are

    Phrases are de-duplicated and capped, because each one costs quota.
    """
    count = count or int(_setting("RECOMMEND_QUERIES", DEFAULT_QUERIES))
    rng = _rng(user_id)

    if profile.is_empty():
        return rng.sample(COLD_START, min(count, len(COLD_START)))

    interests = profile.top("tags", 12) + profile.top("keywords", 12)
    interests.sort(key=lambda kv: -kv[1])

    strong = interests[:8]
    long_tail = interests[8:20]

    queries: list[str] = []

    # Pairings from the strong end.
    for _ in range(max(count - 2, 1)):
        picked = _weighted_sample(strong, 2, rng)
        if picked:
            queries.append(" ".join(picked))

    # One channel, by name, that is watched but not subscribed to.
    subscribed = set(subs_repo.keys_for(user_id))
    for channel_key, _weight in profile.top("channels", 6):
        if channel_key not in subscribed:
            name = profile.channel_names.get(channel_key, channel_key)
            if name:
                queries.append(name)
            break

    # One from the long tail, to keep the feed from closing in on itself.
    if long_tail:
        queries += _weighted_sample(long_tail, 1, rng)
    elif strong:
        queries.append(strong[-1][0])

    # De-duplicate, preserving order.
    seen, unique = set(), []
    for query in queries:
        key = query.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(query.strip())

    return unique[:count]


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------
def _video_id(video: dict) -> str | None:
    return video.get("video_id") or video.get("id")


def dedupe(videos: list[dict], exclude: set[str] | None = None) -> list[dict]:
    """
    Drop repeats within the batch and anything in `exclude` — which is how
    already-watched videos are kept out of the feed.
    """
    exclude = exclude or set()
    seen, output = set(), []
    for video in videos:
        key = _video_id(video)
        if not key or key in seen or key in exclude:
            continue
        seen.add(key)
        output.append(video)
    return output


# --------------------------------------------------------------------------
# Diversity
# --------------------------------------------------------------------------
# If the strict cap cannot fill the feed, it is raised a step at a time
# rather than abandoned. None means "no cap left", which is only reached when
# the candidate pool genuinely has nothing else in it.
RELAXATION_TIERS = (1.0, 1.5, 2.0, None)


def enforce_diversity(
    videos: list[dict],
    profile: preferences.Profile,
    limit: int,
    max_share: float | None = None,
    strict: bool = False,
) -> list[dict]:
    """
    Stop any one channel or topic taking over.

    Candidates are walked best-first and admitted while their channel and
    their topic are both under the cap. If that leaves the feed short — which
    happens when the candidate pool really is all one thing — the cap is
    raised a step and the remaining candidates are walked again, and so on.

    The cap therefore degrades in a predictable way instead of collapsing:
    a homogeneous pool loosens it gradually rather than jumping straight to
    unlimited the moment the first video is set aside.

    Pass strict=True to hold the cap absolutely and accept a shorter feed.
    """
    if not videos:
        return []

    max_share = max_share or float(_setting("RECOMMEND_MAX_SHARE", DEFAULT_MAX_SHARE))
    base_cap = max(1, int(limit * max_share))

    channel_counts: dict[str, int] = {}
    topic_counts: dict[str, int] = {}
    chosen: list[dict] = []
    taken: set[str] = set()

    tiers = (1.0,) if strict else RELAXATION_TIERS

    for tier in tiers:
        if len(chosen) >= limit:
            break
        cap = None if tier is None else max(1, int(base_cap * tier))

        for video in videos:
            if len(chosen) >= limit:
                break

            key = _video_id(video)
            if not key or key in taken:
                continue

            channel = (
                video.get("channel_key")
                or videos_repo.channel_key(
                    video.get("channel_name") or video.get("channel")
                )
                or "unknown"
            )
            topic = preferences.topic_of(video, profile)

            if cap is not None:
                if channel_counts.get(channel, 0) >= cap:
                    continue
                # A video with no clear topic is exempt from the topic cap;
                # otherwise everything unclassifiable would compete for one
                # bucket and throttle itself.
                if topic and topic_counts.get(topic, 0) >= cap:
                    continue

            channel_counts[channel] = channel_counts.get(channel, 0) + 1
            if topic:
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
            taken.add(key)
            chosen.append(video)

        if tier is not None and len(chosen) < limit:
            log.debug(
                "Diversity cap of %s could only fill %s/%s - relaxing",
                cap, len(chosen), limit,
            )

    return chosen[:limit]


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def fetch_candidates(queries: list[str], per_query: int = 20) -> list[dict]:
    """Run each phrase and pool the results, recording them in the catalogue."""
    candidates: list[dict] = []
    for query in queries:
        try:
            results = youtube_search.search(query, limit=per_query)
        except Exception as exc:      # a bad query must not blank the feed
            log.warning("Recommendation query %r failed: %s", query, exc)
            continue
        if results:
            videos_repo.upsert_many(results)
            candidates += results
    return candidates


# --------------------------------------------------------------------------
# The feed
# --------------------------------------------------------------------------
def home_feed(user_id: int, limit: int = 48) -> list[dict]:
    """
    The main grid on the home page.

    Partly-watched videos are deliberately absent: they have their own
    "Continue watching" shelf above this, and repeating them here would waste
    the row.
    """
    half_life = float(_setting("RECOMMEND_HALF_LIFE_DAYS", DEFAULT_HALF_LIFE_DAYS))
    profile = preferences.build_profile(user_id, half_life_days=half_life)
    watched = library_repo.watched_ids(user_id)

    queries = build_queries(profile, user_id)
    per_query = max(10, (limit * CANDIDATE_MULTIPLIER) // max(len(queries), 1))

    candidates = fetch_candidates(queries, per_query=per_query)
    candidates = dedupe(candidates, exclude=watched)

    ranked = sorted(candidates, key=lambda video: -preferences.score(video, profile))
    feed = enforce_diversity(ranked, profile, limit)

    # Backfill, in descending order of how much it says about this account.
    if len(feed) < limit:
        already = {_video_id(v) for v in feed} | watched
        subscribed = subs_repo.feed_videos(user_id, limit=limit)
        feed += dedupe(subscribed, exclude=already)[: limit - len(feed)]

    if len(feed) < limit:
        already = {_video_id(v) for v in feed} | watched
        feed += dedupe(videos_repo.recent(limit=limit * 2), exclude=already)[
            : limit - len(feed)
        ]

    if len(feed) < limit:
        already = {_video_id(v) for v in feed} | watched
        feed += dedupe(local_media.scan(), exclude=already)[: limit - len(feed)]

    return feed[:limit]


def up_next(video: dict, user_id: int, limit: int = 20) -> list[dict]:
    """
    The rail beside the player. Same rules as the home feed, with the video
    on screen used as the strongest signal.
    """
    video_id = _video_id(video)
    profile = preferences.build_profile(user_id)

    suggestions: list[dict] = []

    channel = video.get("channel_name") or video.get("channel") or ""
    if channel:
        suggestions += videos_repo.by_channel(videos_repo.channel_key(channel), limit=8)

    if video.get("source") == "local":
        siblings = [
            item
            for item in local_media.scan()
            if item.get("shelf") == video.get("channel_name")
        ]
        random.shuffle(siblings)
        suggestions += siblings[:10]
    else:
        try:
            results = youtube_search.related(
                video_id, video.get("title", ""), channel, limit=limit
            )
        except Exception as exc:
            log.warning("Related lookup failed for %s: %s", video_id, exc)
            results = []
        if results:
            videos_repo.upsert_many(results)
            suggestions += results

    # The rail is allowed to repeat things you have already seen — rewatching
    # from here is normal — but not the video already on screen.
    suggestions = dedupe(suggestions, exclude={video_id})
    suggestions.sort(key=lambda item: -preferences.score(item, profile))
    suggestions = enforce_diversity(suggestions, profile, limit)

    if len(suggestions) < limit:
        already = {_video_id(v) for v in suggestions} | {video_id}
        suggestions += dedupe(videos_repo.recent(limit=limit * 2), exclude=already)[
            : limit - len(suggestions)
        ]

    return suggestions[:limit]


def explore_shelves(user_id: int) -> list[dict]:
    """Named rows for the Explore page, built from the local catalogue."""
    shelves = []

    trending = videos_repo.trending(limit=18)
    if trending:
        shelves.append({"title": "Trending here", "videos": trending})

    watch_later = library_repo.system_playlist(user_id, "watch_later")
    if watch_later:
        saved = library_repo.playlist_videos(watch_later["id"])[:18]
        if saved:
            shelves.append({"title": "Saved for later", "videos": saved})

    # A row per strong interest, drawn from what is already catalogued so this
    # page costs no quota.
    profile = preferences.build_profile(user_id)
    for tag, _weight in profile.top("tags", 3):
        matches = videos_repo.search_catalogue(tag, limit=18)
        if len(matches) >= 4:
            shelves.append({"title": f"More {tag}", "videos": matches})

    for shelf, items in local_media.by_shelf().items():
        if items:
            shelves.append({"title": shelf, "videos": items[:18], "local": True})

    for channel in videos_repo.channels(limit=6):
        videos = videos_repo.by_channel(channel["channel_key"], limit=18)
        if len(videos) >= 4:
            shelves.append(
                {"title": channel["channel_name"], "videos": videos,
                 "channel": channel["channel_name"]}
            )

    return shelves


def explain(user_id: int) -> dict:
    """
    Why the feed looks the way it does. Handy when tuning the weights, and
    the honest answer to "why am I being shown this".
    """
    profile = preferences.build_profile(user_id)
    return {
        "interactions": profile.interactions,
        "channels": profile.top("channels", 10),
        "tags": profile.top("tags", 10),
        "keywords": profile.top("keywords", 10),
        "negatives": sorted(profile.negatives),
        "queries": build_queries(profile, user_id),
        "watched_count": len(library_repo.watched_ids(user_id)),
    }
