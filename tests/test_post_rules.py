"""Pure rules for Instagram posts: every problem and warning in the contract,
the state machine's allowed moves, key naming, and caption matching."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.post import PostDoc, Slide, SlideCreate
from app.services import post_rules as rules
from app.timezone import IST, UTC

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=IST)


def img(n=1, w=1080, h=1350, **kw):
    return Slide(
        slide_id=f"s{n}",
        media_type="image",
        content_type=kw.pop("content_type", "image/jpeg"),
        r2_object_key=f"c/posts/p/s{n}.jpg",
        width=w,
        height=h,
        size_bytes=kw.pop("size_bytes", 500_000),
        uploaded=kw.pop("uploaded", True),
        **kw,
    )


def vid(n=1, w=1080, h=1920, secs=10.0, **kw):
    return Slide(
        slide_id=f"v{n}",
        media_type="video",
        content_type=kw.pop("content_type", "video/mp4"),
        r2_object_key=f"c/posts/p/v{n}.mp4",
        width=w,
        height=h,
        duration_seconds=secs,
        size_bytes=kw.pop("size_bytes", 5_000_000),
        uploaded=kw.pop("uploaded", True),
        **kw,
    )


def post(kind="image", slides=None, **kw):
    return PostDoc(post_id="p", channel_id="c", kind=kind, slides=slides if slides is not None else [img()], **kw)


def problems(p):
    return rules.validate(p).problems


def warnings(p):
    return rules.validate(p).warnings


# ---- slide counts ------------------------------------------------------------


def test_a_valid_image_post_has_no_problems_or_warnings():
    assert rules.validate(post()) == rules.Validation([], [])


def test_image_post_needs_exactly_one_image():
    assert "An image post needs exactly one image" in problems(post(slides=[]))
    assert "An image post needs exactly one image" in problems(post(slides=[img(1), img(2)]))
    assert "An image post needs exactly one image" in problems(post(slides=[vid()]))


def test_story_needs_exactly_one_slide_of_either_type():
    assert problems(post("story", [vid()])) == []
    assert problems(post("story", [img(1, 1080, 1920)])) == []
    assert "A story needs exactly one image or video" in problems(post("story", []))
    assert "A story needs exactly one image or video" in problems(post("story", [vid(1), vid(2)]))


@pytest.mark.parametrize("count,ok", [(1, False), (2, True), (10, True), (11, False)])
def test_carousel_needs_two_to_ten_slides(count, ok):
    p = post("carousel", [img(i) for i in range(count)])
    assert (problems(p) == []) is ok


# ---- per-slide problems --------------------------------------------------------


def test_every_slide_must_be_uploaded():
    assert "Slide 2 hasn't finished uploading" in problems(post("carousel", [img(1), img(2, uploaded=False)]))


def test_image_must_be_jpeg_and_at_most_8_mb():
    bad = img(content_type="image/png")
    assert "Slide 1 must be a JPEG image" in problems(post(slides=[bad]))
    assert "Slide 1 is larger than 8 MB" in problems(post(slides=[img(size_bytes=8 * 1024 * 1024 + 1)]))


def test_image_width_bounds():
    assert any("320–1440 px wide" in p for p in problems(post(slides=[img(w=300, h=300)])))
    assert any("320–1440 px wide" in p for p in problems(post(slides=[img(w=1500, h=1500)])))
    assert problems(post(slides=[img(w=1440, h=1440)])) == []


def test_image_aspect_bounds_except_for_stories():
    tall = img(w=1080, h=1920)  # 0.5625 < 0.8
    assert "Slide 1 has an aspect ratio outside 4:5 … 1.91:1" in problems(post(slides=[tall]))
    assert problems(post("story", [tall])) == []
    wide = img(w=1440, h=700)  # 2.06 > 1.91
    assert "Slide 1 has an aspect ratio outside 4:5 … 1.91:1" in problems(post(slides=[wide]))


def test_unknown_dimensions_are_not_problems():
    assert problems(post(slides=[img(w=None, h=None)])) == []


def test_video_must_be_mp4_and_at_most_300_mb():
    assert "Slide 1 must be an MP4 video" in problems(post("story", [vid(content_type="video/quicktime")]))
    assert "Slide 1 is larger than 300 MB" in problems(post("story", [vid(size_bytes=300 * 1024 * 1024 + 1)]))


def test_carousel_and_story_videos_are_3_to_60_seconds_when_known():
    assert "Slide 1 must be 3–60 seconds long" in problems(post("story", [vid(secs=61)]))
    assert "Slide 2 must be 3–60 seconds long" in problems(post("carousel", [img(1), vid(2, secs=2)]))
    assert problems(post("story", [vid(secs=None)])) == []


# ---- caption and comment -------------------------------------------------------


def test_caption_length_and_hashtag_limits():
    assert "Caption is longer than 2200 characters" in problems(post(caption="x" * 2201))
    tags = " ".join(f"#t{i}" for i in range(31))
    assert "Caption has 31 hashtags; Instagram allows 30" in problems(post(caption=tags))
    assert problems(post(caption=" ".join(f"#t{i}" for i in range(30)))) == []


def test_caption_is_not_checked_on_stories():
    assert problems(post("story", [vid()], caption="x" * 3000)) == []


def test_first_comment_limit():
    assert any(p.startswith("First comment:") for p in problems(post(first_comment="x" * 2201)))
    assert problems(post(first_comment="x" * 2200)) == []


def test_music_note_is_optional_with_in_app_music():
    assert problems(post(music_mode="in_app", music_note=None)) == []


# ---- warnings -----------------------------------------------------------------


def test_carousel_slide_with_a_different_aspect_is_warned_about():
    p = post("carousel", [img(1, 1080, 1350), img(2, 1080, 1080), img(3, 1080, 1351)])
    assert warnings(p) == ["Slide 2 will be cropped to match slide 1"]


def test_story_not_9_16_is_letterboxed():
    assert "Stories are 9:16; this will be letterboxed" in warnings(post("story", [img(1, 1080, 1080)]))
    assert warnings(post("story", [vid(1, 1080, 1920)])) == []


def test_narrow_image_may_look_soft():
    assert any("may look soft on Instagram" in w for w in warnings(post(slides=[img(w=800, h=1000)])))


def test_caption_on_a_story_is_warned_about():
    assert "Stories have no caption; it won't be posted" in warnings(post("story", [vid()], caption="hi"))


# ---- helpers ------------------------------------------------------------------


def test_hashtag_counting():
    assert rules.count_hashtags("#a #b c#d ##e #f_g #é") == 4
    assert rules.count_hashtags("") == 0


def test_slide_key_naming():
    assert rules.slide_key("ch", "p1", "s1", "image/jpeg") == "ch/posts/p1/s1.jpg"
    assert rules.slide_key("ch", "p1", "s1", "video/mp4") == "ch/posts/p1/s1.mp4"


def test_caption_normalisation():
    assert rules.normalise_caption("  Hello\n\n  WORLD \t!") == "hello world !"
    assert rules.normalise_caption("x" * 150) == "x" * 100
    assert rules.normalise_caption(None) == ""


def test_upload_refusals():
    ok = SlideCreate(media_type="image", content_type="image/jpeg", size_bytes=1000)
    assert rules.upload_refusal("carousel", 0, ok) is None
    assert rules.upload_refusal("carousel", 0, SlideCreate(media_type="image", content_type="image/png", size_bytes=1))
    assert rules.upload_refusal(
        "carousel", 0, SlideCreate(media_type="image", content_type="image/jpeg", size_bytes=9 * 1024 * 1024)
    )
    assert rules.upload_refusal(
        "story", 0, SlideCreate(media_type="video", content_type="video/mp4", size_bytes=301 * 1024 * 1024)
    )
    assert rules.upload_refusal("image", 0, SlideCreate(media_type="video", content_type="video/mp4", size_bytes=1))
    assert rules.upload_refusal("image", 1, ok)
    assert rules.upload_refusal("carousel", 10, ok)
    assert rules.upload_refusal("carousel", 9, ok) is None


# ---- state machine ----------------------------------------------------------------


def test_edit_permissions_by_status():
    for s in ("draft", "scheduled", "failed", "awaiting_manual"):
        assert rules.can_edit_text(s)
    for s in ("publishing", "published", "archived"):
        assert not rules.can_edit_text(s)
    for s in ("draft", "scheduled", "failed"):
        assert rules.can_edit_slides(s)
    for s in ("awaiting_manual", "publishing", "published", "archived"):
        assert not rules.can_edit_slides(s)


def test_delete_archive_and_other_transitions():
    assert {s for s in rules.DELETABLE} == {"draft", "failed", "archived"}
    assert not rules.can_archive("publishing")
    assert not rules.can_archive("archived")
    assert rules.can_archive("published")
    assert rules.can_retry("failed") and not rules.can_retry("draft")
    assert rules.can_mark_published("awaiting_manual") and not rules.can_mark_published("scheduled")
    assert rules.can_unschedule("awaiting_manual") and rules.can_unschedule("scheduled")
    assert not rules.can_unschedule("draft")


def test_restore_of_a_scheduled_post_whose_time_passed_becomes_draft():
    assert rules.restore_status("scheduled", NOW - timedelta(minutes=1), NOW) == "draft"
    assert rules.restore_status("scheduled", NOW + timedelta(hours=1), NOW) == "scheduled"
    assert rules.restore_status("failed", None, NOW) == "failed"
    # Mongo's naive UTC must not be read as IST.
    naive_future_utc = (NOW + timedelta(hours=1)).astimezone(UTC).replace(tzinfo=None)
    assert rules.restore_status("scheduled", naive_future_utc, NOW) == "scheduled"


def test_handoff_timing():
    assert rules.handoff_due(NOW + timedelta(minutes=30), NOW)
    assert not rules.handoff_due(NOW + timedelta(minutes=31), NOW)
    assert not rules.handoff_due(None, NOW)


# ---- manual-post matching ----------------------------------------------------------


def _media(mid, caption, at):
    return {"id": mid, "caption": caption, "timestamp": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")}


def test_manual_match_by_normalised_caption_after_handoff():
    p = post(caption="Sunset  over\nthe BAY #travel", handoff_sent_at=NOW)
    feed = [
        _media("old", "sunset over the bay #travel", NOW - timedelta(hours=3)),  # before the window
        _media("m1", "SUNSET over the bay #travel", NOW + timedelta(minutes=5)),
    ]
    assert rules.match_manual_media(p, feed, set())["id"] == "m1"


def test_manual_match_skips_media_already_linked_to_another_post():
    p = post(caption="hello", handoff_sent_at=NOW)
    feed = [_media("m1", "hello", NOW)]
    assert rules.match_manual_media(p, feed, {"m1"}) is None


def test_captionless_posts_are_never_auto_matched():
    p = post(caption="", handoff_sent_at=NOW)
    assert rules.match_manual_media(p, [_media("m1", "", NOW)], set()) is None


def test_protected_slide_keys():
    posts = [
        post(slides=[img(1)], status="draft"),
        PostDoc(post_id="q", channel_id="c", kind="image", status="published", slides=[img(2)]),
        PostDoc(
            post_id="r",
            channel_id="c",
            kind="image",
            status="archived",
            archived_from_status="published",
            slides=[img(3)],
        ),
        PostDoc(
            post_id="s", channel_id="c", kind="image", status="archived", archived_from_status="draft", slides=[img(4)]
        ),
    ]
    assert rules.protected_slide_keys(posts) == {"c/posts/p/s1.jpg", "c/posts/p/s4.jpg"}
