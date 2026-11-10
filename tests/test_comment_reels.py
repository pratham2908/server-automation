from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.comment_reels import build_comment_reels, group_by_media, pick_samples, reel_row
from app.services.comment_reels_email import format_comment_reels_email
from app.services.daily_digest import video_url

T0 = datetime(2026, 11, 10, 12, 0)


def c(i, text, media="m1", minutes=0, **extra):
    return {
        "comment_id": f"c{i}",
        "media_id": media,
        "username": f"user{i}",
        "text": text,
        "commented_at": T0 + timedelta(minutes=minutes),
        **extra,
    }


def test_samples_are_the_longest_comments_oldest_first_and_skip_short_ones():
    comments = [
        c(1, "meet", minutes=1),
        c(2, "Tokyo to Osaka would be amazing", minutes=2),
        c(3, "do Leidseplein to Dam Square please", minutes=3),
        c(4, "🔥🔥", minutes=4),
        c(5, "a much longer comment about the Amsterdam canal ring", minutes=5),
        c(6, "another reasonably long comment here", minutes=6),
    ]
    picked = pick_samples(comments)
    assert [p["comment_id"] for p in picked] == ["c3", "c5", "c6"]
    assert all(len(p["text"]) >= 12 for p in picked) and len(picked) == 3
    assert picked == sorted(picked, key=lambda p: p["commented_at"])


def test_group_by_media_ignores_comments_without_a_post():
    grouped = group_by_media(
        [c(1, "x" * 20, media="a"), c(2, "y" * 20, media="b"), c(3, "z" * 20, media="a"), {"text": "orphan"}]
    )
    assert {k: len(v) for k, v in grouped.items()} == {"a": 2, "b": 1}


def test_reel_row_builds_comment_deep_links_from_the_permalink():
    video = {"title": " Tokyo to Osaka ", "published_at": datetime(2026, 11, 9, 13, 0), "metadata": {"views": 12400}}
    row = reel_row(video, [c(1, "Tokyo to Osaka would be amazing")], "https://www.instagram.com/reel/AbC123/")
    assert row["title"] == "Tokyo to Osaka" and row["views"] == 12400 and row["comment_count"] == 1
    assert row["samples"][0]["link"] == "https://www.instagram.com/reel/AbC123/c/c1/"
    assert row["published_at"].startswith("2026-11-09T18:30")


def test_reel_row_without_a_permalink_has_no_links():
    row = reel_row({"title": "t"}, [c(1, "long enough comment")], None)
    assert row["permalink"] is None and row["samples"][0]["link"] is None


def test_instagram_links_need_a_real_permalink():
    video = {"instagram_media_id": "1789"}
    assert video_url("instagram", video) is None
    assert video_url("instagram", video, {"1789": "https://www.instagram.com/reel/X/"}).endswith("/reel/X/")
    assert video_url("instagram", {**video, "instagram_permalink": "https://www.instagram.com/reel/Y/"}).endswith("/Y/")


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d

        return gen()


class _Coll:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        def ok(d):
            for k, cond in query.items():
                v = d.get(k)
                if isinstance(cond, dict):
                    if "$ne" in cond and v == cond["$ne"]:
                        return False
                    if "$gte" in cond and not (v is not None and v >= cond["$gte"]):
                        return False
                    if "$in" in cond and v not in cond["$in"]:
                        return False
                elif v != cond:
                    return False
            return True

        return _Cursor([d for d in self.docs if ok(d)])

    async def find_one(self, query):
        return next((d for d in self.docs if all(d.get(k) == v for k, v in query.items())), None)


@pytest.mark.asyncio
async def test_build_counts_only_other_peoples_top_level_comments_in_the_window(monkeypatch):
    from app.services import comment_reels as cr

    async def fake_graph(db, manager, channel_id):
        return None

    async def fake_permalinks(db, graph, ids):
        return {i: f"https://www.instagram.com/reel/{i}/" for i in ids}

    monkeypatch.setattr(cr, "graph_for", fake_graph)
    monkeypatch.setattr(cr, "permalinks", fake_permalinks)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    comments = [
        c(1, "a real comment about maps", channel_id="ch"),
        c(2, "our own reply to someone", channel_id="ch", own=True),
        c(3, "a reply inside a thread", channel_id="ch", is_reply=True),
        {**c(4, "too old to count here", channel_id="ch"), "commented_at": T0 - timedelta(days=5)},
        c(5, "another one on a second reel", media="m2", channel_id="ch"),
    ]
    db = SimpleNamespace(
        channels=_Coll([{"channel_id": "ch", "name": "The Map World", "handle": "@themapworld"}]),
        comment_log=_Coll(comments),
        videos=_Coll(
            [
                {"channel_id": "ch", "instagram_media_id": "m1", "title": "One", "published_at": T0},
                {
                    "channel_id": "ch",
                    "instagram_media_id": "m2",
                    "title": "Two",
                    "published_at": T0 + timedelta(hours=1),
                },
            ]
        ),
    )
    data = await build_comment_reels(db, None, ["ch", "missing"], 3, now)
    assert data["totals"] == {"reels": 2, "comments": 2}
    assert [r["title"] for r in data["channels"][0]["reels"]] == ["Two", "One"]


def test_email_has_a_button_per_reel_and_escapes_text():
    row = reel_row(
        {"title": "Tokyo <fast>", "published_at": datetime(2026, 11, 9, 13, 0), "metadata": {"views": 5}},
        [c(1, "Amsterdam <b>canal</b> ring please")],
        "https://www.instagram.com/reel/AbC123/",
    )
    data = {
        "days": 3,
        "channels": [{"name": "TMW", "handle": "@tmw", "reels": [row], "comments": 1}],
        "totals": {"reels": 1, "comments": 1},
    }
    mail = format_comment_reels_email(data)
    assert "1 reels with comments" in mail.subject
    assert 'href="https://www.instagram.com/reel/AbC123/"' in mail.html and "Open reel" in mail.html
    assert "/reel/AbC123/c/c1/" in mail.html
    assert "&lt;fast&gt;" in mail.html and "<b>canal" not in mail.html
    assert "https://www.instagram.com/reel/AbC123/" in mail.text


def test_comments_that_only_tag_friends_are_not_sampled():
    tags = c(1, "@vedantkale10_, @omkar_hivrale, @heramb__pednekar, @advaitvanne51")
    mixed = c(2, "@arjun this is so true for Pune honestly")
    assert [p["comment_id"] for p in pick_samples([tags, mixed])] == ["c2"]
