#!/usr/bin/env python3
"""
Offline checks for the Tubi search parser.

No network. Every backend is stubbed with a payload in one of the shapes Tubi
has been seen to use, and the parser is asked to cope. The point is not to
prove it matches Tubi's current API — that cannot be known from here — but to
prove it survives the shape changing, which is the thing that broke it before.

    python test_tubi.py
"""
from __future__ import annotations

import json

from hub.services import tubi

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# Payload shapes
# --------------------------------------------------------------------------
FLAT = {
    "contents": [
        {
            "id": "100059630",
            "title": "The Quiet Earth",
            "description": "A scientist wakes to find himself alone.",
            "year": 1985,
            "duration": 5460,
            "images": {"poster": ["https://images.tubi.video/poster/100059630.jpg"]},
        }
    ]
}

# Same data, different envelope and different image shape.
NESTED = {
    "data": {
        "search": {
            "results": {
                "hits": [
                    {
                        "content_id": 100059630,
                        "name": "The Quiet Earth",
                        "synopsis": "A scientist wakes to find himself alone.",
                        "release_year": 1985,
                        "images": [
                            {"url": "https://images.tubi.video/poster/100059630.jpg"}
                        ],
                    }
                ]
            }
        }
    }
}

# Nothing but a bare string for the image, and the content buried in state.
DEEP = {
    "props": {
        "pageProps": {
            "initialState": {
                "video": {
                    "byId": {
                        "100059630": {
                            "id": "100059630",
                            "title": "The Quiet Earth",
                            "description": "A scientist wakes to find himself alone.",
                            "thumbnail": "https://images.tubi.video/poster/100059630.jpg",
                        }
                    }
                }
            }
        }
    }
}

DRM_ITEM = {
    "contents": [
        {
            "id": "200000001",
            "title": "Protected Feature",
            "description": "Licensed title.",
            "images": {"poster": ["https://images.tubi.video/p/200000001.jpg"]},
            "drm": True,
        },
        {
            "id": "200000002",
            "title": "Open Feature",
            "description": "Plays fine.",
            "images": {"poster": ["https://images.tubi.video/p/200000002.jpg"]},
        },
    ]
}

# Page furniture that must NOT be mistaken for content.
NOISE = {
    "rows": [
        {"id": "genre-horror", "title": "Horror"},
        {"id": "row_1", "name": "Because you watched"},
        {"title": "Missing an id entirely"},
        {"id": "100059630", "title": "The Quiet Earth"},
    ]
}


def main() -> int:
    print("\nExtraction across payload shapes")
    for label, payload in (("flat", FLAT), ("nested", NESTED), ("deep state", DEEP)):
        results, _ = tubi._shape_many(payload)
        ok = len(results) == 1 and results[0]["title"] == "The Quiet Earth"
        check(f"parses the {label} shape", ok, f"{len(results)} result(s)")
        if ok:
            check(f"  {label}: id extracted", results[0]["video_id"] == "tubi_100059630")
            check(f"  {label}: poster extracted",
                  results[0]["thumbnail"].startswith("https://images.tubi.video"))

    results, _ = tubi._shape_many(FLAT)
    item = results[0]
    print("\nRequired fields")
    check("title", item["title"] == "The Quiet Earth")
    check("video id", item["video_id"] == "tubi_100059630")
    check("poster url", item["thumbnail"].endswith("100059630.jpg"))
    check("description", item["description"].startswith("A scientist"))
    check("route is /watch/Tubi_{id}", item["watch_path"] == "/watch/Tubi_100059630",
          item["watch_path"])

    print("\nNoise rejection")
    results, _ = tubi._shape_many(NOISE)
    titles = [r["title"] for r in results]
    check("genre rows and headers are ignored", titles == ["The Quiet Earth"],
          f"kept: {titles}")

    print("\nDRM handling")
    results, drm_count = tubi._shape_many(DRM_ITEM)
    check("DRM titles are detected", drm_count == 1, f"{drm_count} flagged")
    check("DRM titles are kept out of results",
          [r["title"] for r in results] == ["Open Feature"])
    kept, _ = tubi._shape_many(DRM_ITEM, skip_drm=False)
    check("they can be kept and labelled instead",
          len(kept) == 2 and kept[0]["drm_protected"] is True)

    print("\nID handling")
    for raw, expect in (
        ("100059630", "tubi_100059630"),
        ("tubi_100059630", "tubi_100059630"),
        ("Tubi_100059630", "tubi_100059630"),
        ("TUBI_100059630", "tubi_100059630"),
        ("https://tubitv.com/movies/100059630/the-quiet-earth", "tubi_100059630"),
    ):
        check(f"canonical_id({raw[:34]!r})", tubi.canonical_id(raw) == expect,
              tubi.canonical_id(raw))
    check("is_tubi_id is case-insensitive",
          tubi.is_tubi_id("Tubi_1") and tubi.is_tubi_id("tubi_1")
          and not tubi.is_tubi_id("dQw4w9WgXcQ"))

    print("\nEmbedded JSON extraction")
    next_data = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(DEEP)
        + "</script></body></html>"
    )
    payloads = tubi._extract_embedded_json(next_data)
    check("reads __NEXT_DATA__", any(tubi._shape_many(p)[0] for p in payloads))

    streamed = (
        '<html><script>self.__next_f.push([1,"'
        + json.dumps(FLAT).replace('"', '\\"')
        + '"])</script></html>'
    )
    payloads = tubi._extract_embedded_json(streamed)
    check("reads the streamed push() format",
          any(tubi._shape_many(p)[0] for p in payloads),
          f"{len(payloads)} blob(s) found")

    check("survives markup with no JSON at all",
          tubi._extract_embedded_json("<html><body>nothing</body></html>") == [])

    print("\nGraceful failure")
    for label, payload in (
        ("empty dict", {}),
        ("empty list", []),
        ("null", None),
        ("a bare string", "service unavailable"),
        ("an error envelope", {"error": "rate limited", "status": 429}),
        ("deeply nested junk", {"a": {"b": {"c": [{"d": "e"}]}}}),
    ):
        try:
            results, _ = tubi._shape_many(payload)
            check(f"{label} yields no results and no exception", results == [])
        except Exception as exc:
            check(f"{label} yields no results and no exception", False,
                  f"raised {type(exc).__name__}")

    print("\nSearch never raises")
    original = (tubi._from_api, tubi._from_website, tubi._from_playwright)

    def boom(query):
        raise ConnectionError("network is down")

    tubi._from_api = boom
    tubi._from_website = boom
    tubi._from_playwright = boom
    try:
        outcome = tubi.search_detailed("anything", use_playwright=False)
        check("a total backend failure returns an outcome, not an exception",
              outcome.results == [] and outcome.ok is False)
        check("the failure reason is recorded", bool(outcome.reason), outcome.reason)
        check("the plain search() wrapper still returns a list",
              tubi.search("anything") == [])
    finally:
        tubi._from_api, tubi._from_website, tubi._from_playwright = original

    def empty(query):
        return [], 0, ""

    tubi._from_api = empty
    tubi._from_website = empty
    try:
        outcome = tubi.search_detailed("zzzznotathing", use_playwright=False)
        check("an honest blank is distinguishable from a breakage",
              outcome.results == [] and outcome.attempts == ["api", "website"])
    finally:
        tubi._from_api, tubi._from_website = original[0], original[1]

    check("an empty query short-circuits",
          tubi.search_detailed("  ").ok and tubi.search_detailed("  ").results == [])

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("Failures: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
