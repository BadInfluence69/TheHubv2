"""
Working out what someone actually likes.

Everything here is derived from rows this server already has: watch history,
watch progress, and local likes/dislikes. Nothing is fetched from Google to
build the profile, and the profile never leaves this process — the only thing
that reaches YouTube is the search phrase built from it.

The shape of it:

    interaction  ->  base weight  ->  decayed by age  ->  spread over features

A *feature* is one of three things, kept in three separate buckets because
they are not equally trustworthy:

    channels   who made it            strongest, and unambiguous
    tags       the uploader's tags    good when present, often absent
    keywords   words from the title   always available, noisiest

Weights are relative, not absolute. Each bucket is normalised so its best
entry is 1.0, which keeps scoring stable whether an account has twenty
interactions or two thousand.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..repo import library as library_repo
from ..repo import videos as videos_repo

# --------------------------------------------------------------------------
# Tuning
# --------------------------------------------------------------------------
# How long it takes an interaction to count half as much. Two weeks means
# last month still matters and last year barely registers.
DEFAULT_HALF_LIFE_DAYS = 14.0

# What each kind of interaction is worth before decay.
LIKE_WEIGHT = 3.0
DISLIKE_WEIGHT = -5.0      # a dislike is louder than a like, deliberately
WATCH_BASE = 0.5           # opening it at all
WATCH_COMPLETION_BONUS = 1.5   # scaled by how much of it you watched

# How much of an interaction's weight each bucket receives.
CHANNEL_SHARE = 1.0
TAG_SHARE = 0.8
KEYWORD_SHARE = 0.45

# Interactions older than this are not worth the arithmetic.
MAX_AGE_DAYS = 365

# A feature has to end up at least this negative to count as "stop showing me
# this". One dislike alone will not do it if you also watched the channel.
NEGATIVE_THRESHOLD = -1.0

STOPWORDS = {
    "about", "again", "against", "album", "also", "another", "audio", "back",
    "because", "been", "before", "being", "best", "better", "between", "both",
    "call", "came", "come", "could", "does", "doing", "done", "down", "during",
    "each", "episode", "even", "ever", "every", "feat", "featuring", "first",
    "free", "from", "full", "give", "goes", "going", "gone", "good", "great",
    "have", "here", "high", "himself", "hour", "hours", "into", "just", "keep",
    "kind", "know", "last", "left", "less", "life", "like", "little", "live",
    "long", "look", "made", "make", "many", "more", "most", "much",
    "must", "need", "never", "next", "official", "once", "only", "other",
    "over", "part", "people", "place", "play", "really", "right", "said",
    "same", "says", "series", "shorts", "should", "show", "side", "since",
    "some", "something", "soon", "sound", "still", "such", "take", "than",
    "that", "their", "them", "then", "there", "these", "they", "thing",
    "things", "think", "this", "those", "three", "through", "time", "times",
    "trailer", "true", "turn", "under", "until", "update", "used", "using",
    "very", "video", "want", "watch", "week", "well", "went", "were", "what",
    "when", "where", "which", "while", "will", "with", "without", "work",
    "world", "would", "year", "years", "your",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'&-]{3,17}")


def keywords_from(text: str) -> list[str]:
    """Distinctive words from a title. Stopwords and bare numbers dropped."""
    seen, output = set(), []
    for match in _WORD_RE.findall(text or ""):
        word = match.strip("'-&").lower()
        if len(word) < 4 or word in STOPWORDS or word in seen:
            continue
        seen.add(word)
        output.append(word)
    return output


# --------------------------------------------------------------------------
# The profile
# --------------------------------------------------------------------------
@dataclass
class Profile:
    """Weighted preferences for one account. All weights are 0.0 - 1.0."""

    channels: dict[str, float] = field(default_factory=dict)
    tags: dict[str, float] = field(default_factory=dict)
    keywords: dict[str, float] = field(default_factory=dict)

    # channel_key -> the name as it should be typed into a search box
    channel_names: dict[str, str] = field(default_factory=dict)

    # Features that came out net-negative. Anything matching these is pushed
    # down the feed rather than removed outright, so one bad afternoon does
    # not permanently blacklist a topic.
    negatives: set[str] = field(default_factory=set)

    interactions: int = 0

    def is_empty(self) -> bool:
        return not (self.channels or self.tags or self.keywords)

    def top(self, bucket: str, count: int = 10) -> list[tuple[str, float]]:
        weights: dict[str, float] = getattr(self, bucket, {})
        return sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[:count]

    def weight_of(self, feature: str) -> float:
        """Best weight for a feature, whichever bucket it lives in."""
        return max(
            self.channels.get(feature, 0.0),
            self.tags.get(feature, 0.0),
            self.keywords.get(feature, 0.0),
        )

    def summary(self) -> str:
        """One line for logs and the debug endpoint."""
        parts = []
        for bucket in ("channels", "tags", "keywords"):
            top = ", ".join(name for name, _ in self.top(bucket, 3))
            if top:
                parts.append(f"{bucket}: {top}")
        return f"{self.interactions} interactions | " + " | ".join(parts)


# --------------------------------------------------------------------------
# Building it
# --------------------------------------------------------------------------
def _interaction_weight(signal: dict) -> float:
    """
    What one watch or vote is worth before age is taken into account.

    A watch is scored by completion where we know it: sitting through 90% of
    something says far more than opening it and bouncing. Where duration is
    unknown (local files, older rows) it counts as a middling watch.
    """
    if signal.get("kind") == "vote":
        value = signal.get("value") or 0
        return LIKE_WEIGHT if value > 0 else DISLIKE_WEIGHT

    position = float(signal.get("position_seconds") or 0.0)
    duration = signal.get("duration_seconds")
    if duration and float(duration) > 0:
        completion = min(1.0, position / float(duration))
    else:
        completion = 0.5

    return WATCH_BASE + WATCH_COMPLETION_BONUS * completion


def _decay(age_days: float, half_life: float) -> float:
    """0.5 ** (age / half-life). One half-life old counts half as much."""
    if age_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / max(half_life, 0.5))


def _normalise(weights: dict[str, float]) -> dict[str, float]:
    """Scale so the strongest entry is 1.0. Non-positive entries are dropped."""
    positive = {k: v for k, v in weights.items() if v > 0}
    if not positive:
        return {}
    peak = max(positive.values())
    if peak <= 0:
        return {}
    return {k: round(v / peak, 4) for k, v in positive.items()}


def build_profile(
    user_id: int,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    limit: int = 500,
) -> Profile:
    """
    Read this account's history and votes and turn them into weighted
    preferences. Cheap enough to call per page load — it is a single indexed
    query plus arithmetic over at most `limit` rows.
    """
    signals = library_repo.interaction_signals(user_id, limit=limit)

    channels: dict[str, float] = {}
    tags: dict[str, float] = {}
    keywords: dict[str, float] = {}
    channel_names: dict[str, str] = {}
    counted = 0

    for signal in signals:
        age = float(signal.get("age_days") or 0.0)
        if age > MAX_AGE_DAYS:
            continue

        weight = _interaction_weight(signal) * _decay(age, half_life_days)
        if abs(weight) < 0.005:
            continue
        counted += 1

        channel = (signal.get("channel_key") or "").strip()
        if channel:
            channels[channel] = channels.get(channel, 0.0) + weight * CHANNEL_SHARE
            channel_names.setdefault(channel, signal.get("channel_name") or channel)

        for tag in videos_repo.parse_tags(signal.get("tags")):
            tags[tag] = tags.get(tag, 0.0) + weight * TAG_SHARE

        for word in keywords_from(signal.get("title") or ""):
            keywords[word] = keywords.get(word, 0.0) + weight * KEYWORD_SHARE

    negatives = {
        feature
        for bucket in (channels, tags, keywords)
        for feature, weight in bucket.items()
        if weight <= NEGATIVE_THRESHOLD
    }

    return Profile(
        channels=_normalise(channels),
        tags=_normalise(tags),
        keywords=_normalise(keywords),
        channel_names=channel_names,
        negatives=negatives,
        interactions=counted,
    )


# --------------------------------------------------------------------------
# Scoring a candidate against a profile
# --------------------------------------------------------------------------
# How much each kind of match contributes to a candidate's score.
CHANNEL_MATCH = 2.5
TAG_MATCH = 1.5
KEYWORD_MATCH = 1.0
NEGATIVE_PENALTY = 3.0
MATCHES_COUNTED = 3   # only the strongest few matches count, so a video with
                      # forty tags cannot out-score a genuinely good result


def _best_matches(features: list[str], weights: dict[str, float]) -> float:
    hits = sorted((weights.get(f, 0.0) for f in features), reverse=True)
    return sum(hits[:MATCHES_COUNTED])


def score(video: dict, profile: Profile) -> float:
    """
    How well one candidate fits the profile. Higher is better; anything can
    go negative if it matches something the account has pushed away.
    """
    if profile.is_empty():
        return 0.0

    channel = (
        video.get("channel_key")
        or videos_repo.channel_key(video.get("channel_name") or video.get("channel"))
    )
    tags = videos_repo.parse_tags(video.get("tags"))
    words = keywords_from(video.get("title") or "")

    total = profile.channels.get(channel, 0.0) * CHANNEL_MATCH
    total += _best_matches(tags, profile.tags) * TAG_MATCH
    total += _best_matches(words, profile.keywords) * KEYWORD_MATCH

    if channel in profile.negatives:
        total -= NEGATIVE_PENALTY
    if any(tag in profile.negatives for tag in tags):
        total -= NEGATIVE_PENALTY / 2

    return round(total, 4)


def topic_of(video: dict, profile: Profile) -> str | None:
    """
    The single feature a video most represents, used to stop one subject
    filling the feed. None means "nothing in particular", which is exempt
    from the diversity cap — otherwise unclassifiable videos would be
    throttled as if they were all the same topic.
    """
    tags = videos_repo.parse_tags(video.get("tags"))
    words = keywords_from(video.get("title") or "")

    best, best_weight = None, 0.0
    for feature in tags + words:
        weight = profile.weight_of(feature)
        if weight > best_weight:
            best, best_weight = feature, weight

    if best:
        return best
    return tags[0] if tags else None
