"""PostService and the posts routes: what the UI can and cannot do to a post,
plus the storage purge and presigned-URL changes that shipped with posts."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from post_fakes import FakeDB, FakeInstagram, FakeManager, FakeR2

from app.models.post import MarkPublishedRequest, PostCreate, PostDoc, PostUpdate, Slide, SlideCreate
from app.services.post_service import PostConflictError, PostError, PostNotFoundError, PostService
from app.timezone import IST, now_ist

IG = {"channel_id": "c", "name": "Chan", "platform": "instagram", "instagram_user_id": "ig1"}
YT = {"channel_id": "y", "name": "Tube", "platform": "youtube"}
JPEG = SlideCreate(media_type="image", content_type="image/jpeg", size_bytes=400_000, width=1080, height=1350)


def make(posts=None, sizes=None):
    db = FakeDB(channels=[IG, YT], posts=posts)
    r2 = FakeR2(sizes)
    woke = []
    svc = PostService(db, r2, FakeManager(FakeInstagram()), wake=lambda: woke.append(1))
    return svc, db, r2, woke


def slide(n, **kw):
    return Slide(
        slide_id=f"s{n}",
        media_type="image",
        content_type="image/jpeg",
        r2_object_key=f"c/posts/p/s{n}.jpg",
        width=1080,
        height=1350,
        size_bytes=1000,
        uploaded=kw.pop("uploaded", True),
        **kw,
    )


def post_doc(status="draft", kind="image", slides=None, **kw):
    return PostDoc(
        post_id="p", channel_id="c", kind=kind, status=status, slides=slides if slides is not None else [slide(1)], **kw
    ).model_dump()


def future(hours=2):
    return (now_ist() + timedelta(hours=hours)).isoformat()


# ---- channels ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_posts_are_created_only_on_instagram_channels():
    svc, db, _, _ = make()
    out = await svc.create_post("c", PostCreate(kind="carousel", caption="hi", first_comment="  "))
    assert out.status == "draft" and out.kind == "carousel"
    assert out.first_comment is None  # blank means "no comment"
    assert out.problems == ["A carousel needs 2–10 slides (it has 0)"]

    with pytest.raises(PostError) as exc:
        await svc.create_post("y", PostCreate(kind="image"))
    assert exc.value.status_code == 400
    with pytest.raises(PostError):
        await svc.create_post("missing", PostCreate(kind="image"))


# ---- upload flow ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slide_upload_url_is_signed_for_the_slides_content_type():
    svc, db, r2, _ = make()
    created = await svc.create_post("c", PostCreate(kind="image"))
    out = await svc.create_slide("c", created.post_id, JPEG)

    key = f"c/posts/{created.post_id}/{out.slide.slide_id}.jpg"
    assert r2.put_calls == [(key, "image/jpeg")]
    assert out.upload_headers == {"Content-Type": "image/jpeg"}
    assert out.slide.uploaded is False and out.slide.preview_url is None


@pytest.mark.asyncio
async def test_disallowed_type_or_oversized_file_is_refused_before_a_url_exists():
    svc, db, r2, _ = make([post_doc(slides=[])])
    for bad in (
        SlideCreate(media_type="image", content_type="image/png", size_bytes=10),
        SlideCreate(media_type="image", content_type="image/jpeg", size_bytes=9 * 1024 * 1024),
        SlideCreate(media_type="video", content_type="video/mp4", size_bytes=10),  # video on an image post
    ):
        with pytest.raises(PostError):
            await svc.create_slide("c", "p", bad)
    assert r2.put_calls == []
    assert db.posts.one(post_id="p")["slides"] == []


@pytest.mark.asyncio
async def test_slide_count_limits():
    svc, *_ = make([post_doc(slides=[slide(1)])])
    with pytest.raises(PostError, match="at most 1"):
        await svc.create_slide("c", "p", JPEG)

    svc, *_ = make([post_doc(kind="carousel", slides=[slide(i) for i in range(10)])])
    with pytest.raises(PostError, match="at most 10"):
        await svc.create_slide("c", "p", JPEG)


@pytest.mark.asyncio
async def test_complete_only_once_the_object_exists():
    svc, db, r2, _ = make([post_doc(slides=[slide(1, uploaded=False)])])
    with pytest.raises(PostError, match="not arrived"):
        await svc.complete_slide("c", "p", "s1")
    assert db.posts.one(post_id="p")["slides"][0]["uploaded"] is False

    r2.sizes["c/posts/p/s1.jpg"] = 123_456
    out = await svc.complete_slide("c", "p", "s1")
    assert out.slides[0].uploaded is True
    assert out.slides[0].size_bytes == 123_456  # the real size, not the declared one
    assert out.slides[0].preview_url.startswith("https://r2.test/c/posts/p/s1.jpg")
    assert out.problems == []


@pytest.mark.asyncio
async def test_complete_refuses_and_removes_an_object_larger_than_declared_limits():
    svc, db, r2, _ = make([post_doc(slides=[slide(1, uploaded=False)])], sizes={"c/posts/p/s1.jpg": 20 * 1024 * 1024})
    with pytest.raises(PostError, match="limit"):
        await svc.complete_slide("c", "p", "s1")
    assert r2.deleted == ["c/posts/p/s1.jpg"]


# ---- schedule -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_refused_with_problems():
    svc, *_ = make([post_doc(kind="carousel")])
    with pytest.raises(PostError, match="carousel needs"):
        await svc.schedule("c", "p", future())


@pytest.mark.asyncio
async def test_schedule_refused_in_the_past():
    svc, *_ = make([post_doc()])
    with pytest.raises(PostError, match="future"):
        await svc.schedule("c", "p", (now_ist() - timedelta(minutes=1)).isoformat())


@pytest.mark.asyncio
async def test_schedule_accepts_a_naive_time_as_ist():
    svc, db, _, _ = make([post_doc()])
    when = (now_ist() + timedelta(days=1)).replace(microsecond=0)
    out = await svc.schedule("c", "p", when.replace(tzinfo=None).isoformat())
    assert out.status == "scheduled"
    assert out.scheduled_at == when.astimezone(IST).isoformat()


@pytest.mark.asyncio
async def test_unschedule_and_publish_now():
    svc, db, _, woke = make([post_doc(status="scheduled", scheduled_at=now_ist() + timedelta(hours=1))])
    out = await svc.unschedule("c", "p")
    assert out.status == "draft" and out.scheduled_at is None

    out = await svc.publish_now("c", "p")
    assert out.status == "scheduled"
    assert woke == [1]


# ---- edits ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_editing_a_scheduled_post_into_problems_is_refused():
    svc, db, _, _ = make([post_doc(status="scheduled", scheduled_at=now_ist() + timedelta(hours=1))])
    with pytest.raises(PostError, match="hashtags"):
        await svc.update_post("c", "p", PostUpdate(caption=" ".join(f"#t{i}" for i in range(31))))
    assert db.posts.one(post_id="p")["caption"] == ""

    out = await svc.update_post("c", "p", PostUpdate(caption="fine"))
    assert out.caption == "fine" and out.status == "scheduled"


@pytest.mark.asyncio
async def test_a_draft_may_hold_problems_while_being_edited():
    svc, *_ = make([post_doc()])
    out = await svc.update_post("c", "p", PostUpdate(kind="carousel"))
    assert out.kind == "carousel"
    assert out.problems


@pytest.mark.asyncio
async def test_slide_order_and_alt_text():
    svc, *_ = make([post_doc(kind="carousel", slides=[slide(1), slide(2)])])
    out = await svc.update_post("c", "p", PostUpdate(slide_order=["s2", "s1"], alt_texts={"s1": "a dog"}))
    assert [s.slide_id for s in out.slides] == ["s2", "s1"]
    assert out.slides[1].alt_text == "a dog"
    with pytest.raises(PostError):
        await svc.update_post("c", "p", PostUpdate(slide_order=["s1"]))


@pytest.mark.asyncio
async def test_awaiting_manual_allows_caption_but_not_slides():
    svc, *_ = make([post_doc(status="awaiting_manual", music_mode="in_app")])
    out = await svc.update_post("c", "p", PostUpdate(music_note="Espresso"))
    assert out.music_note == "Espresso"
    with pytest.raises(PostConflictError):
        await svc.update_post("c", "p", PostUpdate(kind="story"))
    with pytest.raises(PostConflictError):
        await svc.create_slide("c", "p", JPEG)


@pytest.mark.asyncio
async def test_published_posts_cannot_be_edited():
    svc, *_ = make([post_doc(status="published")])
    with pytest.raises(PostConflictError) as exc:
        await svc.update_post("c", "p", PostUpdate(caption="x"))
    assert exc.value.status_code == 409


# ---- delete / archive -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_only_in_allowed_states_and_removes_r2_objects():
    svc, db, r2, _ = make([post_doc(status="scheduled", slides=[slide(1)])])
    with pytest.raises(PostConflictError):
        await svc.delete_post("c", "p")
    assert r2.deleted == []

    svc, db, r2, _ = make([post_doc(status="failed", kind="carousel", slides=[slide(1), slide(2)])])
    assert (await svc.delete_post("c", "p")).deleted is True
    assert r2.deleted == ["c/posts/p/s1.jpg", "c/posts/p/s2.jpg"]
    assert db.posts.docs == []


@pytest.mark.asyncio
async def test_delete_slide_removes_its_object():
    svc, db, r2, _ = make([post_doc(kind="carousel", slides=[slide(1), slide(2)])])
    out = await svc.delete_slide("c", "p", "s1")
    assert [s.slide_id for s in out.slides] == ["s2"]
    assert r2.deleted == ["c/posts/p/s1.jpg"]
    with pytest.raises(PostNotFoundError):
        await svc.delete_slide("c", "p", "nope")


@pytest.mark.asyncio
async def test_archive_and_restore():
    svc, db, _, _ = make([post_doc(status="scheduled", scheduled_at=now_ist() - timedelta(minutes=5))])
    out = await svc.archive("c", "p")
    assert out.status == "archived"
    assert (await svc.list_posts("c", None)).posts == []
    assert len((await svc.list_posts("c", "archived")).posts) == 1

    out = await svc.restore("c", "p")
    assert out.status == "draft"  # its time passed while archived


@pytest.mark.asyncio
async def test_publishing_posts_cannot_be_archived():
    svc, *_ = make([post_doc(status="publishing")])
    with pytest.raises(PostConflictError):
        await svc.archive("c", "p")


@pytest.mark.asyncio
async def test_mark_published_only_from_awaiting_manual():
    svc, *_ = make([post_doc(status="draft")])
    with pytest.raises(PostConflictError):
        await svc.mark_published("c", "p", MarkPublishedRequest())

    svc, *_ = make([post_doc(status="awaiting_manual", music_mode="in_app")])
    out = await svc.mark_published("c", "p", MarkPublishedRequest(permalink="https://instagram.com/p/x"))
    assert out.status == "published" and out.permalink == "https://instagram.com/p/x"


@pytest.mark.asyncio
async def test_retry_moves_failed_back_to_scheduled_and_wakes_the_worker():
    svc, _, _, woke = make([post_doc(status="failed", attempts=5, last_error="boom")])
    out = await svc.retry("c", "p")
    assert out.status == "scheduled" and out.attempts == 0 and out.last_error is None
    assert woke == [1]


@pytest.mark.asyncio
async def test_handoff_lists_uploaded_slides_with_day_long_links():
    svc, *_ = make([post_doc(kind="carousel", slides=[slide(1), slide(2, uploaded=False)], caption="cap")])
    out = await svc.handoff("c", "p")
    assert [s.slide_id for s in out.slides] == ["s1"]
    assert out.slides[0].download_url.endswith("exp=86400")
    assert out.slides[0].filename == "post-p-01.jpg"


# ---- routes -----------------------------------------------------------------------------


class _LimitInstagram(FakeInstagram):
    def get_publishing_limit(self, ig_user_id):
        return {"quota_total": 100, "quota_usage": 3, "quota_duration": 86400}


@pytest.fixture
def api(auth_headers):
    from app.main import app
    from app.routers.posts import get_post_service

    db = FakeDB(channels=[IG, YT], posts=[post_doc()])
    svc = PostService(db, FakeR2(), FakeManager(_LimitInstagram()))
    app.dependency_overrides[get_post_service] = lambda: svc
    yield TestClient(app), auth_headers
    app.dependency_overrides.clear()


def test_routes_fixed_paths_are_not_read_as_post_ids(api):
    client, headers = api
    r = client.get("/api/v1/channels/c/posts/publishing-limit", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"quota_total": 100, "quota_usage": 3, "quota_duration_seconds": 86400}


def test_routes_list_create_and_errors(api):
    client, headers = api
    r = client.get("/api/v1/channels/c/posts", headers=headers)
    assert r.status_code == 200 and [p["post_id"] for p in r.json()["posts"]] == ["p"]

    r = client.post("/api/v1/channels/c/posts", json={"kind": "story"}, headers=headers)
    assert r.status_code == 201 and r.json()["status"] == "draft"

    r = client.post("/api/v1/channels/y/posts", json={"kind": "story"}, headers=headers)
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)

    r = client.get("/api/v1/channels/c/posts/nope", headers=headers)
    assert r.status_code == 404

    assert client.get("/api/v1/channels/c/posts", headers={"X-API-Key": "wrong"}).status_code == 401


# ---- storage purge -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_purge_protects_slides_of_unpublished_posts():
    from app.routers.videos import _active_r2_keys

    db = FakeDB(
        channels=[IG],
        videos=[{"video_id": "v", "status": "ready", "r2_object_key": "c/v.mp4"}],
        posts=[
            {**post_doc(status="draft", slides=[slide(1)]), "post_id": "a"},
            {**post_doc(status="scheduled", slides=[slide(2)]), "post_id": "b"},
            {**post_doc(status="published", slides=[slide(3)]), "post_id": "c"},
            {**post_doc(status="archived", archived_from_status="published", slides=[slide(4)]), "post_id": "d"},
        ],
    )
    keys = await _active_r2_keys(SimpleNamespace(db=db))
    assert keys == {"c/v.mp4", "c/posts/p/s1.jpg", "c/posts/p/s2.jpg"}


# ---- presigned PUT content type -----------------------------------------------------------


def _r2_capturing():
    from app.services.r2 import R2Service

    r2 = R2Service("https://example.r2.test", "k", "s", "bucket")
    seen = {}

    def fake(op, Params, ExpiresIn):
        seen.update(Params)
        return "https://signed"

    r2._client.generate_presigned_url = fake
    return r2, seen


def test_presigned_put_defaults_to_mp4_for_existing_callers():
    r2, seen = _r2_capturing()
    r2.generate_presigned_put_url("c/v.mp4")
    assert seen["ContentType"] == "video/mp4"


def test_presigned_put_takes_a_content_type():
    r2, seen = _r2_capturing()
    r2.generate_presigned_put_url("c/posts/p/s.jpg", content_type="image/jpeg")
    assert seen["ContentType"] == "image/jpeg"
