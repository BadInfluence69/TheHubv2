"""
Tests for the private metrics dashboard.

The important assertions here are the negative ones. A metrics page that
renders correctly but answers the wrong people is worse than no metrics page,
so most of this file is about who *cannot* get in.

Run with:  python test_metrics.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

# A throwaway database and predictable settings, before hub.config is imported.
TMP = tempfile.mkdtemp(prefix="hub-metrics-test-")
os.environ.update(
    {
        "DB_PATH": os.path.join(TMP, "test.db"),
        "UPLOAD_DIR": os.path.join(TMP, "uploads"),
        "CACHE_DIR": os.path.join(TMP, "cache"),
        "HLS_DIR": os.path.join(TMP, "hls"),
        "SECRET_KEY": "test-secret-key-for-metrics",
        "ALLOW_REGISTRATION": "1",
        "DEBUG": "0",
    }
)

PASSED, FAILED = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        FAILED += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"  — {detail}" if detail else ""))


_BUILD = 0


def build_app(**overrides):
    """
    Fresh app + config, picking up any env overrides set just before.

    Each build gets its own database file. Reusing one file across builds meant
    the second call tripped over the accounts the first had already created.
    """
    global _BUILD
    _BUILD += 1

    for module in [m for m in list(sys.modules) if m.startswith("hub")]:
        del sys.modules[module]

    os.environ["DB_PATH"] = os.path.join(TMP, f"test-{_BUILD}.db")
    os.environ.update({k: str(v) for k, v in overrides.items()})

    from hub import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def make_accounts(app):
    """First account becomes admin (first_run); the second is an ordinary user."""
    from hub import db, security
    from hub.repo import users as users_repo

    with app.app_context():
        admin_id = users_repo.create("owner", security.hash_password("hunter2222"), True)
        plain_id = users_repo.create("viewer", security.hash_password("hunter2222"), False)
        db.get_db().commit()
    return admin_id, plain_id


def sign_in(app, client, user_id):
    """Start a real server-side session for a user id."""
    from hub import db, security

    with app.test_request_context():
        token = security.start_session(user_id)
        db.get_db().commit()
        cookie = dict(__import__("flask").session)

    with client.session_transaction() as session:
        session.update(cookie)
    return token


# ==========================================================================
def test_access_control() -> None:
    print("\nAccess control")
    app = build_app(METRICS_PATH="/metrics", METRICS_ENABLED="1")
    admin_id, plain_id = make_accounts(app)

    # --- signed out --------------------------------------------------------
    with app.test_client() as client:
        response = client.get("/metrics/")
        check(
            "signed-out visitor gets 404, not a login redirect",
            response.status_code == 404,
            f"got {response.status_code}",
        )
        check(
            "signed-out visitor is not redirected anywhere",
            response.status_code not in (301, 302, 303, 307, 308),
            f"got {response.status_code} -> {response.headers.get('Location')}",
        )

    # --- ordinary account --------------------------------------------------
    with app.test_client() as client:
        sign_in(app, client, plain_id)
        response = client.get("/metrics/")
        check(
            "ordinary account gets 404, not 403",
            response.status_code == 404,
            f"got {response.status_code}",
        )
        check(
            "404 body gives nothing away",
            b"Metrics" not in response.data and b"metric" not in response.data.lower(),
            "the word 'metric' appears in the 404 body",
        )

        for path in ("/metrics/data.json", "/metrics/health.json"):
            check(
                f"ordinary account blocked from {path}",
                client.get(path).status_code == 404,
            )

    # --- admin -------------------------------------------------------------
    with app.test_client() as client:
        sign_in(app, client, admin_id)
        response = client.get("/metrics/")
        check("admin reaches the dashboard", response.status_code == 200,
              f"got {response.status_code}")
        check("dashboard actually renders", b"Metrics" in response.data)

        response = client.get("/metrics/data.json")
        check("admin reaches the JSON feed", response.status_code == 200)
        check("JSON feed has the expected shape",
              response.is_json and "overview" in response.get_json())

        check("admin reaches the health feed",
              client.get("/metrics/health.json").status_code == 200)


def test_hidden_from_navigation() -> None:
    print("\nHidden from navigation")
    app = build_app(METRICS_SHOW_LINK="0")
    admin_id, plain_id = make_accounts(app)

    with app.test_client() as client:
        sign_in(app, client, plain_id)
        home = client.get("/")
        check(
            "ordinary account sees no metrics link on the home page",
            b"/metrics" not in home.data,
        )

    with app.test_client() as client:
        sign_in(app, client, admin_id)
        home = client.get("/")
        check(
            "admin sees no link either while METRICS_SHOW_LINK is off",
            b"/metrics" not in home.data,
        )

    # And with the link deliberately switched on.
    app = build_app(METRICS_SHOW_LINK="1")
    admin_id, plain_id = make_accounts(app)

    with app.test_client() as client:
        sign_in(app, client, admin_id)
        check(
            "admin sees the link once METRICS_SHOW_LINK is on",
            b"/metrics" in client.get("/").data,
        )

    with app.test_client() as client:
        sign_in(app, client, plain_id)
        check(
            "ordinary account still sees no link with the flag on",
            b"/metrics" not in client.get("/").data,
        )


def test_relocation_and_kill_switch() -> None:
    print("\nRelocation and kill switch")

    app = build_app(METRICS_PATH="/ops-7f3a91", METRICS_ENABLED="1")
    admin_id, _ = make_accounts(app)
    with app.test_client() as client:
        sign_in(app, client, admin_id)
        check("dashboard moves to a custom path",
              client.get("/ops-7f3a91/").status_code == 200)
        check("nothing is left behind at the old path",
              client.get("/metrics/").status_code == 404)

    app = build_app(METRICS_ENABLED="0", METRICS_PATH="/metrics")
    admin_id, _ = make_accounts(app)
    with app.test_client() as client:
        sign_in(app, client, admin_id)
        check("kill switch removes the route even for an admin",
              client.get("/metrics/").status_code == 404)

    with app.app_context():
        routes = [str(rule) for rule in app.url_map.iter_rules()]
    check("no metrics rule is registered at all when disabled",
          not any("metric" in route for route in routes))


def test_path_normalisation() -> None:
    print("\nPath normalisation")
    from hub.config import _metrics_path

    cases = [
        ("metrics", "/metrics"),
        ("/metrics", "/metrics"),
        ("/metrics/", "/metrics"),
        ("", "/metrics"),
        ("/", "/metrics"),
        ("ops/secret", "/ops/secret"),
        ("bad path!;drop", "/badpathdrop"),
    ]
    for raw, expected in cases:
        os.environ["METRICS_PATH"] = raw
        check(f"{raw!r} normalises to {expected!r}", _metrics_path() == expected,
              f"got {_metrics_path()!r}")
    os.environ["METRICS_PATH"] = "/metrics"


def test_numbers_are_real() -> None:
    print("\nFigures")
    app = build_app(METRICS_ENABLED="1", METRICS_PATH="/metrics")
    admin_id, plain_id = make_accounts(app)

    from hub import db
    from hub.repo import metrics as metrics_repo

    with app.app_context():
        connection = db.get_db()
        connection.execute(
            "INSERT OR IGNORE INTO videos (video_id, source, title, channel_key, "
            "channel_name, duration) VALUES ('vid1','youtube','Test One','chan','Chan',600)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO videos (video_id, source, title, channel_key, "
            "channel_name, duration) VALUES ('vid2','local','Test Two','chan','Chan',300)"
        )
        for _ in range(3):
            connection.execute(
                "INSERT INTO watch_history (user_id, video_id, position_seconds, "
                "duration_seconds) VALUES (?,'vid1',570,600)",
                (plain_id,),
            )
        connection.execute(
            "INSERT INTO watch_history (user_id, video_id, position_seconds, "
            "duration_seconds) VALUES (?,'vid2',30,300)",
            (admin_id,),
        )
        connection.execute(
            "INSERT INTO video_votes (user_id, video_id, value) VALUES (?,'vid1',1)",
            (plain_id,),
        )
        connection.execute(
            "INSERT INTO search_history (user_id, query) VALUES (?, 'test query')",
            (plain_id,),
        )
        connection.commit()

        overview = metrics_repo.overview()
        check("play count is right", overview["watches"] == 4, str(overview["watches"]))
        check("account count is right", overview["users"] == 2)
        check("votes counted", overview["votes"] == 1)

        top = metrics_repo.top_videos(10)
        check("most-watched ranks first", top and top[0]["video_id"] == "vid1")
        check("distinct viewers counted", top and top[0]["viewers"] == 1)

        engagement = metrics_repo.engagement_rates()
        check("like ratio is 100% with one like and no dislikes",
              engagement["like_ratio"] == 100.0, str(engagement["like_ratio"]))
        check("finish rate counts the three ≥90% plays",
              engagement["finished"] == 3, str(engagement["finished"]))

        sources = {row["source"]: row for row in metrics_repo.source_breakdown()}
        check("youtube source has 3 plays", sources.get("youtube", {}).get("watches") == 3)
        check("local source has 1 play", sources.get("local", {}).get("watches") == 1)

        active = metrics_repo.active_users()
        check("both accounts count as active today", active["day"] == 2, str(active["day"]))

        series = metrics_repo.activity_series(30)
        check("series is gap-filled to 30 days", len(series["labels"]) == 30)
        check("today's plays land in the series", series["series"]["watches"][-1] == 4,
              str(series["series"]["watches"][-1]))

        check("table counts come back", len(metrics_repo.table_counts()) > 10)
        check("storage stats read the database file",
              metrics_repo.storage_stats(app.config)["db_bytes"] > 0)

    # And the whole page renders with this data in place.
    with app.test_client() as client:
        sign_in(app, client, admin_id)
        response = client.get("/metrics/?days=90")
        check("dashboard renders with data and a custom window",
              response.status_code == 200, f"got {response.status_code}")
        check("a video title reaches the page", b"Test One" in response.data)


def test_window_clamping() -> None:
    print("\nWindow clamping")
    app = build_app()
    admin_id, _ = make_accounts(app)
    with app.test_client() as client:
        sign_in(app, client, admin_id)
        for query in ("?days=0", "?days=-5", "?days=99999", "?days=abc", "?days="):
            check(f"survives {query!r}", client.get(f"/metrics/{query}").status_code == 200)


# ==========================================================================
if __name__ == "__main__":
    print("=" * 66)
    print("Private metrics dashboard — tests")
    print("=" * 66)
    try:
        test_access_control()
        test_hidden_from_navigation()
        test_relocation_and_kill_switch()
        test_path_normalisation()
        test_numbers_are_real()
        test_window_clamping()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 66)
    print(f"  {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    sys.exit(1 if FAILED else 0)
