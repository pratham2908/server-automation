"""The post publisher's state machine, driven tick by tick against fakes.

Each ``tick`` call is one pass of the worker at a given instant, so "waiting
across ticks" and "resume after a restart" are literal here: state survives
only through what the fake Mongo stored.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from post_fakes import FakeDB, FakeInstagram, FakeManager, FakeR2

from app.config import get_settings
from app.models.post import PostDoc, PublishState, Slide
from app.services import post_publisher as pub
from app.timezone import UTC, now_ist

NOW = now_ist().replace(microsecond=0)
CHANNEL = {"channel_id": "c", "name": "Chan", "platform": "instagram", "instagram_user_id": "ig1"}


def img(n, **kw):
    return Slide(
        slide_id=f"s{n}",
        media_type="image",
        content_type="image/jpeg",
        r2_object_key=f"c/posts/p/s{n}.jpg",
        width=1080,
        height=1350,
        size_bytes=1000,
        uploaded=True,
        **kw,
    )


def vid(n):
    return Slide(
        slide_id=f"v{n}",
        media_type="video",
        content_type="video/mp4",
        r2_object_key=f"c/posts/p/v{n}.mp4",
        width=1080,
        height=1350,
        duration_seconds=10,
        size_bytes=1000,
        uploaded=True,
    )


def doc(post_id="p", kind="image", slides=None, **kw):
    fields = {"status": "scheduled", "scheduled_at": NOW - timedelta(minutes=1), "caption": "Hello #world", **kw}
    return PostDoc(post_id=post_id, channel_id="c", kind=kind, slides=slides or [img(1)], **fields).model_dump()


def setup(posts, channel=None):
    db = FakeDB(channels=[channel or CHANNEL], posts=posts)
    ig = FakeInstagram()
    return db, ig, FakeR2(), FakeManager(ig)


async def tick(db, ig, r2, mgr, at=NOW):
    await pub.publish_due_posts(db, r2, mgr, at)


def stored(db, post_id="p"):
    return PostDoc.model_validate(db.posts.one(post_id=post_id))


# ---- API publishing -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_post_publishes_in_one_tick():
    db, ig, r2, mgr = setup([doc(slides=[img(1, alt_text="a cat")])])
    await tick(db, ig, r2, mgr)

    post = stored(db)
    assert post.status == "published"
    assert post.instagram_media_id == "media-img-1"
    assert post.permalink == "https://instagram.com/p/media-img-1"
    assert post.published_at is not None
    ((cid, params),) = ig.created
    assert params["caption"] == "Hello #world"
    assert params["alt_text"] == "a cat"
    assert params["is_carousel_item"] is False
    # Instagram fetches from a presigned GET with the 6 h lifetime.
    assert params["image_url"] == "https://r2.test/c/posts/p/s1.jpg?exp=21600"


@pytest.mark.asyncio
async def test_carousel_waits_for_children_across_ticks_then_publishes_the_parent():
    db, ig, r2, mgr = setup([doc(kind="carousel", slides=[img(1), vid(2)])])
    ig.statuses["vid-2"] = ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]

    await tick(db, ig, r2, mgr)
    post = stored(db)
    assert post.status == "publishing"
    assert [c.container_id for c in post.publish_state.children] == ["img-1", "vid-2"]
    assert [c.finished for c in post.publish_state.children] == [True, False]
    assert post.publish_state.container_id is None

    await tick(db, ig, r2, mgr, NOW + timedelta(minutes=1))
    assert stored(db).status == "publishing"
    assert len(ig.created) == 2  # nothing recreated while waiting

    await tick(db, ig, r2, mgr, NOW + timedelta(minutes=2))
    post = stored(db)
    assert post.status == "published"
    parent_id, parent = ig.created[-1]
    assert parent_id == "car-3"
    assert parent["children"] == ["img-1", "vid-2"]
    assert parent["caption"] == "Hello #world"
    assert ig.created[0][1]["is_carousel_item"] is True
    assert ig.published == ["car-3"]


@pytest.mark.asyncio
async def test_container_error_fails_the_post():
    db, ig, r2, mgr = setup([doc()])
    ig.statuses["img-1"] = ["ERROR"]
    await tick(db, ig, r2, mgr)

    post = stored(db)
    assert post.status == "failed"
    assert "ERROR" in post.last_error and "bad media" in post.last_error
    assert ig.published == []
    assert db.errors.docs, "a container failure is logged through the error service"


@pytest.mark.asyncio
async def test_expired_carousel_child_fails_the_post():
    db, ig, r2, mgr = setup([doc(kind="carousel", slides=[img(1), img(2)])])
    ig.statuses["img-2"] = ["EXPIRED"]
    await tick(db, ig, r2, mgr)
    assert stored(db).status == "failed"
    assert "slide 2" in stored(db).last_error


@pytest.mark.asyncio
async def test_transient_errors_retry_then_fail_at_five_attempts():
    db, ig, r2, mgr = setup([doc()])
    ig.create_error = RuntimeError("Graph timed out")

    for i in range(1, 5):
        await tick(db, ig, r2, mgr, NOW + timedelta(minutes=i))
        post = stored(db)
        assert post.status == "publishing"
        assert post.attempts == i
        assert post.last_error == "Graph timed out"
    assert not db.errors.docs

    await tick(db, ig, r2, mgr, NOW + timedelta(minutes=5))
    post = stored(db)
    assert post.status == "failed"
    assert post.attempts == 5
    assert db.errors.docs


@pytest.mark.asyncio
async def test_paused_channel_leaves_the_post_scheduled():
    db, ig, r2, mgr = setup([doc()], channel={**CHANNEL, "paused": True})
    await tick(db, ig, r2, mgr)
    assert stored(db).status == "scheduled"
    assert ig.created == []


@pytest.mark.asyncio
async def test_future_posts_are_left_alone():
    db, ig, r2, mgr = setup([doc(scheduled_at=NOW + timedelta(minutes=5))])
    await tick(db, ig, r2, mgr)
    assert stored(db).status == "scheduled"


@pytest.mark.asyncio
async def test_resume_from_persisted_state_does_not_recreate_finished_containers():
    state = PublishState(
        started_at=NOW - timedelta(minutes=10),
        children=[
            {"slide_id": "s1", "container_id": "old-1", "created_at": NOW - timedelta(minutes=10), "finished": True},
            {"slide_id": "v2", "container_id": "old-2", "created_at": NOW - timedelta(minutes=10)},
        ],
    )
    db, ig, r2, mgr = setup(
        [doc(kind="carousel", slides=[img(1), vid(2)], status="publishing", publish_state=state.model_dump())]
    )
    ig.statuses["old-1"] = ["ERROR"]  # would fail the post if it were re-polled

    await tick(db, ig, r2, mgr)

    post = stored(db)
    assert post.status == "published"
    assert [cid for cid, _ in ig.created] == ["car-1"]
    assert ig.created[0][1]["children"] == ["old-1", "old-2"]


@pytest.mark.asyncio
async def test_resume_publishes_an_existing_parent_without_recreating_it():
    state = PublishState(started_at=NOW - timedelta(minutes=10), container_id="img-9", container_created_at=NOW)
    db, ig, r2, mgr = setup([doc(status="publishing", publish_state=state.model_dump())])
    await tick(db, ig, r2, mgr)
    assert ig.created == []
    assert ig.published == ["img-9"]


@pytest.mark.asyncio
async def test_a_publish_stuck_for_two_hours_fails():
    state = PublishState(started_at=NOW - timedelta(hours=2, minutes=1), container_id="img-9")
    db, ig, r2, mgr = setup([doc(status="publishing", publish_state=state.model_dump())])
    ig.statuses["img-9"] = ["IN_PROGRESS"]
    await tick(db, ig, r2, mgr)
    post = stored(db)
    assert post.status == "failed"
    assert post.last_error == "Instagram never finished processing"


@pytest.mark.asyncio
async def test_a_container_older_than_23_hours_is_recreated():
    # Only reachable if started_at is recent but the container is not — the
    # guard is defensive, so exercise it directly.
    state = PublishState(started_at=NOW, container_id="img-9", container_created_at=NOW - timedelta(hours=23))
    db, ig, r2, mgr = setup([doc(status="publishing", publish_state=state.model_dump())])
    await tick(db, ig, r2, mgr)
    assert [cid for cid, _ in ig.created] == ["img-1"]
    assert ig.published == ["img-1"]


@pytest.mark.asyncio
async def test_story_video_uses_video_url_and_no_caption():
    db, ig, r2, mgr = setup([doc(kind="story", slides=[vid(1)])])
    await tick(db, ig, r2, mgr)
    ((_, params),) = ig.created
    assert params == {"image_url": None, "video_url": "https://r2.test/c/posts/p/v1.mp4?exp=21600"}
    assert stored(db).status == "published"


@pytest.mark.asyncio
async def test_a_scheduled_post_with_problems_fails_instead_of_publishing():
    db, ig, r2, mgr = setup([doc(kind="carousel", slides=[img(1)])])
    await tick(db, ig, r2, mgr)
    post = stored(db)
    assert post.status == "failed"
    assert "carousel needs" in post.last_error
    assert ig.created == []


@pytest.mark.asyncio
async def test_first_comment_posted_is_recorded():
    db, ig, r2, mgr = setup([doc(first_comment="#more #tags")])
    await tick(db, ig, r2, mgr)
    assert ig.comments == [("media-img-1", "#more #tags")]
    assert stored(db).first_comment_status == "posted"


@pytest.mark.asyncio
async def test_first_comment_failure_is_recorded_and_the_post_stays_published():
    db, ig, r2, mgr = setup([doc(first_comment="hi")])
    ig.comment_error = RuntimeError("no permission")
    await tick(db, ig, r2, mgr)
    post = stored(db)
    assert post.status == "published"
    assert post.first_comment_status == "failed"


# ---- in-app music hand-off --------------------------------------------------------------


@pytest.fixture
def mail(monkeypatch):
    sent = []

    async def fake_send(settings, recipient, subject, body, html_body=None):
        sent.append({"to": recipient, "subject": subject, "body": body, "html": html_body})
        return True

    monkeypatch.setattr(pub, "send_email", fake_send)
    return sent


@pytest.mark.asyncio
async def test_handoff_at_t_minus_30_sets_awaiting_manual_and_emails_once(mail):
    db, ig, r2, mgr = setup(
        [doc(music_mode="in_app", music_note="Espresso from 0:32", scheduled_at=NOW + timedelta(minutes=25))]
    )
    settings = get_settings()

    await pub.send_handoffs(db, settings, NOW)
    post = stored(db)
    assert post.status == "awaiting_manual"
    assert post.handoff_sent_at == NOW.astimezone(UTC)
    assert len(mail) == 1
    assert mail[0]["to"] == "owner@example.com"
    assert f"{settings.ANALYZER_PUBLIC_URL}/handoff/c/p" in mail[0]["body"]
    assert "Espresso from 0:32" in mail[0]["body"]

    await pub.send_handoffs(db, settings, NOW + timedelta(minutes=1))
    assert len(mail) == 1


@pytest.mark.asyncio
async def test_handoff_waits_until_the_lead_window(mail):
    db, ig, r2, mgr = setup([doc(music_mode="in_app", scheduled_at=NOW + timedelta(minutes=45))])
    await pub.send_handoffs(db, get_settings(), NOW)
    assert stored(db).status == "scheduled"
    assert mail == []


@pytest.mark.asyncio
async def test_in_app_posts_are_never_auto_published(mail):
    db, ig, r2, mgr = setup([doc(music_mode="in_app", scheduled_at=NOW - timedelta(hours=1))])
    await tick(db, ig, r2, mgr)
    assert ig.created == []
    assert stored(db).status == "scheduled"

    await pub.run_tick(db, r2, mgr, get_settings(), NOW)
    assert stored(db).status == "awaiting_manual"
    assert ig.created == [] and ig.published == []


def _feed_item(mid, caption, at):
    return {
        "id": mid,
        "caption": caption,
        "permalink": f"https://instagram.com/p/{mid}",
        "timestamp": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000"),
    }


@pytest.mark.asyncio
async def test_manual_post_is_detected_by_caption():
    db, ig, r2, mgr = setup(
        [doc(status="awaiting_manual", music_mode="in_app", caption="Sunset  over the BAY", handoff_sent_at=NOW)]
    )
    ig.media = [
        _feed_item("m0", "something else", NOW + timedelta(minutes=10)),
        _feed_item("m1", "sunset over the bay", NOW + timedelta(minutes=5)),
    ]
    matched = await pub.detect_manual_posts(db, mgr, NOW + timedelta(minutes=15), {})
    assert matched == 1
    post = stored(db)
    assert post.status == "published"
    assert post.instagram_media_id == "m1"
    assert post.permalink == "https://instagram.com/p/m1"


@pytest.mark.asyncio
async def test_manual_detection_skips_media_linked_to_another_post():
    other = doc(post_id="other", status="published", instagram_media_id="m1")
    mine = doc(status="awaiting_manual", music_mode="in_app", caption="same caption", handoff_sent_at=NOW)
    db, ig, r2, mgr = setup([other, mine])
    ig.media = [_feed_item("m1", "same caption", NOW + timedelta(minutes=5))]

    assert await pub.detect_manual_posts(db, mgr, NOW + timedelta(minutes=15), {}) == 0
    assert stored(db).status == "awaiting_manual"


@pytest.mark.asyncio
async def test_manual_detection_reads_the_feed_at_most_every_few_minutes():
    db, ig, r2, mgr = setup([doc(status="awaiting_manual", music_mode="in_app", handoff_sent_at=NOW)])
    last: dict = {}
    await pub.detect_manual_posts(db, mgr, NOW, last)
    await pub.detect_manual_posts(db, mgr, NOW + timedelta(minutes=1), last)
    assert ig.media_page_calls == 1
    await pub.detect_manual_posts(db, mgr, NOW + timedelta(minutes=6), last)
    assert ig.media_page_calls == 2
