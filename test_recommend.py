#!/usr/bin/env python3
"""
Offline checks for the recommendation engine.

Builds a throwaway database, fills it with a synthetic history, and asserts
the four behaviours the feed is supposed to have: deduplication, a weighted
and decayed preference profile, diversity caps, and query generation. The
YouTube search call is stubbed, so this runs with no API key and no network.

    python test_recommend.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from hub import db
from hub.config import TestConfig
from hub.repo import library as library_repo
from hub.repo import subs as subs_repo
from hub.repo import users as users_repo
from hub.repo import videos as videos_repo
from hub.services import preferences, recommend, youtube_search

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}{(' - ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
CHANNELS = {
    "Thrash Vault": ["thrash metal", "crossover", "punk", "live show"],
    "Riff Workshop": ["guitar", "thrash metal", "tutorial", "gear"],
    "Circuit Bent": ["synth", "electronics", "diy", "gear"],
    "Deep Field": ["astronomy", "space", "documentary"],
    "Slow Roast": ["cooking", "bbq", "recipe"],
}

TITLES = {
    "Thrash Vault": "Crossover Thrash Deep Cuts Volume {n}",
    "Riff Workshop": "Downpicking Technique Breakdown Part {n}",
    "Circuit Bent": "Building A Modular Synth Voice {n}",
    "Deep Field": "Voyager Interstellar Mission Explained {n}",
    "Slow Roast": "Low And Slow Brisket Method {n}",
}


def seed_catalogue() -> None:
    for channel, tags in CHANNELS.items():
        for n in range(1, 13):
            videos_repo.upsert(
                video_id=f"{channel[:2].lower()}{n:03d}",
                title=TITLES[channel].format(n=n),
                channel_name=channel,
                thumbnail="https://example.invalid/t.jpg",
                description="",
                tags=tags,
            )


def seed_history(user_id: int) -> None:
    """
    A history with a clear shape: heavy recent thrash/guitar watching, a
    stale month-old astronomy phase, and one disliked channel.
    """
    def watch(video_id, days_ago, position, duration):
        db.execute(
            "INSERT INTO watch_history (user_id, video_id, watched_at, "
            "position_seconds, duration_seconds) "
            "VALUES (?, ?, datetime('now', ?), ?, ?)",
            (user_id, video_id, f"-{days_ago} day", position, duration),
        )

    for n in range(1, 7):                      # recent, watched to the end
        watch(f"th{n:03d}", n, 580, 600)
    for n in range(1, 5):                      # recent, watched most of it
        watch(f"ri{n:03d}", n + 1, 400, 600)
    for n in range(1, 7):                      # a month ago, should have faded
        watch(f"de{n:03d}", 35 + n, 590, 600)
    for n in range(1, 3):                      # opened and abandoned
        watch(f"sl{n:03d}", 2, 20, 900)

    videos_repo.set_vote(user_id, "th001", 1)
    videos_repo.set_vote(user_id, "th002", 1)
    videos_repo.set_vote(user_id, "sl001", -1)


# --------------------------------------------------------------------------
# Stubbed search: returns fresh, unwatched videos plus some it has seen
# --------------------------------------------------------------------------
def fake_search(query: str, limit: int | None = None) -> list[dict]:
    limit = limit or 20
    results = []

    # Half the results are from one channel, to give the diversity cap
    # something to actually push back against.
    for n in range(limit):
        if n % 2 == 0:
            channel, tags = "Thrash Vault", CHANNELS["Thrash Vault"]
            title = f"Crossover Thrash Live Set {query} {n}"
        else:
            channel = list(CHANNELS)[n % len(CHANNELS)]
            tags = CHANNELS[channel]
            title = f"{TITLES[channel].format(n=n)} {query}"
        results.append({
            "id": f"new-{abs(hash(query)) % 9999}-{n}",
            "video_id": f"new-{abs(hash(query)) % 9999}-{n}",
            "source": "youtube",
            "title": title,
            "channel": channel,
            "channel_name": channel,
            "thumbnail": "",
            "description": "",
            "tags": tags,
            "duration": 600,
            "published_at": None,
        })

    # Two videos the account has definitely already watched, so dedupe has
    # something real to remove.
    for already in ("th001", "th002"):
        results.append({
            "id": already, "video_id": already, "source": "youtube",
            "title": "Already watched", "channel": "Thrash Vault",
            "channel_name": "Thrash Vault", "thumbnail": "", "description": "",
            "tags": CHANNELS["Thrash Vault"], "duration": 600,
            "published_at": None,
        })
    return results


# --------------------------------------------------------------------------
def main() -> int:
    from flask import Flask

    tmp = Path(tempfile.mkdtemp())
    app = Flask(__name__)
    app.config.from_object(TestConfig)
    app.config["DB_PATH"] = tmp / "test.db"
    db.init_app(app)
    db.init_db(app.config["DB_PATH"])

    youtube_search.search = fake_search      # no network, no quota, no key

    with app.app_context():
        user_id = users_repo.create("tester", "not-a-real-hash")
        library_repo.ensure_system_playlists(user_id)
        seed_catalogue()
        seed_history(user_id)

        print("\nPreference profile")
        profile = preferences.build_profile(user_id)
        print("   ", profile.summary())

        top_tags = [tag for tag, _ in profile.top("tags", 5)]
        top_channels = [c for c, _ in profile.top("channels", 5)]

        check("profile is built from history", not profile.is_empty(),
              f"{profile.interactions} interactions")
        check("liked channel outranks the rest",
              top_channels[0] == "thrash vault", f"top: {top_channels[:3]}")
        check("recent interest beats stale interest",
              profile.tags.get("thrash metal", 0) > profile.tags.get("astronomy", 0),
              f"thrash {profile.tags.get('thrash metal', 0)} vs "
              f"astronomy {profile.tags.get('astronomy', 0)}")
        check("decay actually fades old rows",
              profile.tags.get("astronomy", 1) < 0.5,
              f"astronomy weight {profile.tags.get('astronomy', 0)}")
        finished = preferences._interaction_weight(
            {"kind": "watch", "position_seconds": 580, "duration_seconds": 600})
        bounced = preferences._interaction_weight(
            {"kind": "watch", "position_seconds": 20, "duration_seconds": 900})
        check("an abandoned watch is worth less than a finished one",
              bounced < finished, f"{bounced:.2f} vs {finished:.2f}")
        check("a disliked channel is dropped from the profile",
              "slow roast" not in profile.channels)
        check("dislike registers as negative", "slow roast" in profile.negatives,
              f"negatives: {sorted(profile.negatives)}")
        check("tags are recovered from the database", "thrash metal" in top_tags,
              f"top tags: {top_tags}")

        print("\nQuery generation")
        queries = recommend.build_queries(profile, user_id)
        print("   ", queries)
        check("queries are generated", len(queries) > 0)
        check("queries respect the configured cap",
              len(queries) <= app.config["RECOMMEND_QUERIES"],
              f"{len(queries)} <= {app.config['RECOMMEND_QUERIES']}")
        check("queries are distinct", len(queries) == len(set(queries)))
        check("queries are drawn from the profile",
              any(any(t in q.lower() for t in top_tags) for q in queries))
        check("queries are stable within a rotation window",
              recommend.build_queries(profile, user_id) == queries)
        check("different accounts get different queries",
              recommend.build_queries(profile, user_id + 7) != queries)

        cold = preferences.Profile()
        cold_queries = recommend.build_queries(cold, user_id)
        check("cold start still produces queries", len(cold_queries) > 0,
              f"{cold_queries}")

        print("\nDeduplication")
        watched = library_repo.watched_ids(user_id)
        check("watch history is readable as a set", len(watched) == 18,
              f"{len(watched)} watched")
        raw = fake_search("thrash metal", 20)
        deduped = recommend.dedupe(raw, exclude=watched)
        check("already-watched videos are removed",
              not ({v["id"] for v in deduped} & watched))
        check("repeats within a batch are removed",
              len(recommend.dedupe(raw + raw, exclude=watched)) == len(deduped))

        print("\nFeed assembly")
        feed = recommend.home_feed(user_id, limit=24)
        ids = [v["id"] for v in feed]
        check("feed is filled", len(feed) == 24, f"{len(feed)} items")
        check("no duplicates in the feed", len(ids) == len(set(ids)))
        check("nothing already watched is in the feed",
              not (set(ids) & watched))

        counts: dict[str, int] = {}
        for video in feed:
            key = videos_repo.channel_key(video.get("channel_name") or video.get("channel"))
            counts[key] = counts.get(key, 0) + 1
        cap = int(24 * app.config["RECOMMEND_MAX_SHARE"])
        worst = max(counts.items(), key=lambda kv: kv[1])
        print(f"    channel spread: {counts}")
        check("no channel runs away with the feed",
              worst[1] <= cap * recommend.RELAXATION_TIERS[1],
              f"{worst[0]} has {worst[1]}, strict cap {cap}")
        check("the feed is not one channel", len(counts) >= 3,
              f"{len(counts)} channels")

        print("\nDiversity under pressure")
        # Everything from a single channel: the cap has to give way, but only
        # after it has been raised a step at a time.
        single = [dict(v, channel_name="Thrash Vault", channel="Thrash Vault")
                  for v in fake_search("x", 40)]
        squeezed = recommend.enforce_diversity(single, profile, 24)
        check("a homogeneous pool still fills the feed", len(squeezed) == 24,
              f"{len(squeezed)} of 24")

        strict = recommend.enforce_diversity(single, profile, 24, strict=True)
        check("strict mode holds the cap and returns a short feed",
              len(strict) <= int(24 * app.config["RECOMMEND_MAX_SHARE"]),
              f"{len(strict)} items")

        # A pool with plenty of variety must not need any relaxation at all.
        varied = []
        for index in range(60):
            channel = list(CHANNELS)[index % len(CHANNELS)]
            varied.append({
                "id": f"v{index}", "video_id": f"v{index}",
                "title": f"Something {index}", "channel": channel,
                "channel_name": channel, "tags": CHANNELS[channel],
            })
        spread = recommend.enforce_diversity(varied, profile, 24)
        by_channel: dict[str, int] = {}
        for video in spread:
            key = videos_repo.channel_key(video["channel_name"])
            by_channel[key] = by_channel.get(key, 0) + 1
        check("a varied pool never breaches the strict cap",
              max(by_channel.values()) <= int(24 * app.config["RECOMMEND_MAX_SHARE"]),
              f"worst channel has {max(by_channel.values())}")

        print("\nUp next rail")
        current = videos_repo.get("th005")
        rail = recommend.up_next(current, user_id, limit=12)
        check("rail is filled", len(rail) > 0, f"{len(rail)} items")
        check("current video is not in its own rail",
              "th005" not in {v.get("video_id") or v.get("id") for v in rail})

        print("\nExplain endpoint")
        explanation = recommend.explain(user_id)
        check("explain returns the profile",
              explanation["interactions"] > 0 and bool(explanation["queries"]))

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("Failures: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
