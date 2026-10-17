"""The story kit: link, times, token, email, and the channel's one story page. All pure; no network."""

from datetime import datetime, timedelta, timezone

from app.services import story_kit as kit
from app.services.trial_graduation_email import format_graduate_email

IST = timezone(timedelta(hours=5, minutes=30))
REEL = datetime(2026, 10, 17, 9, 30, tzinfo=IST)


def _item(slug="two-sum-3-ways", thumb="https://x/c.jpg"):
    return kit.KitItem("vid-1", slug, "Two Sum, 3 Ways", REEL, thumb)


def test_story_link_carries_the_story_medium_and_the_slug():
    assert kit.story_link("two-sum-3-ways") == (
        "https://tryalgoviz.com/r/two-sum-3-ways/?utm_medium=story&utm_campaign=two-sum-3-ways"
    )


def test_story_goes_up_seven_minutes_after_the_reel():
    assert _item().story_time == REEL + timedelta(minutes=7)
    assert kit.clock(_item().story_time) == "9:37 AM"
    assert kit.day_clock(REEL.replace(hour=21)) == "Sat 17 Oct, 9:30 PM IST"


def test_naive_mongo_time_is_read_as_utc_then_shown_in_ist():
    naive_utc = datetime(2026, 10, 17, 4, 0)  # 09:30 IST
    assert kit.reel_time_ist(naive_utc).hour == 9 and kit.reel_time_ist(naive_utc).minute == 30


def test_tokens_are_long_and_never_repeat():
    tokens = {kit.new_token() for _ in range(50)}
    assert len(tokens) == 50 and all(len(t) >= 40 for t in tokens)


def test_a_kit_expires_after_three_days():
    made = datetime(2026, 10, 17, tzinfo=timezone.utc)
    assert not kit.is_expired(made + kit.KIT_LIFETIME, made + timedelta(days=2, hours=23))
    assert kit.is_expired(made + kit.KIT_LIFETIME, made + timedelta(days=3, minutes=1))
    # Mongo's naive datetimes count as UTC.
    assert kit.is_expired(datetime(2026, 10, 20), datetime(2026, 10, 21))


def test_email_lists_each_reel_with_its_story_time_and_one_button():
    subject, text, html = kit.kit_email([_item(), _item("weird-sorts")], "https://s/s/tok")
    assert subject == "Stories to schedule: 2 reels"
    assert "9:37 AM" in text and "https://s/s/tok" in text
    assert html.count("Schedule the story for") == 2 and html.count("Get stories") == 1


def test_page_has_a_card_a_button_and_the_sticker_text_per_reel():
    page = kit.kit_page("tok", [_item()])
    assert (
        'data-link="https://tryalgoviz.com/r/two-sum-3-ways/?utm_medium=story&amp;utm_campaign=two-sum-3-ways"' in page
    )
    assert "/s/tok/vid-1/video" in page and "/s/tok/vid-1/cover" in page
    assert kit.STICKER_TEXT in page and "noindex" in page and "Scheduled</label>" in page
    # The tick is saved on the server, not in one phone's storage.
    assert 'data-url="/s/tok/vid-1/done"' in page and "localStorage" not in page


def test_scheduled_reels_sink_to_their_own_section_ticked():
    page = kit.kit_page("tok", [_item(), _item("weird-sorts")._replace(video_id="vid-2", done=True)])
    todo, done = page.split("Scheduled (1)")
    assert "card-vid-1" in todo and "card-vid-2" in done
    assert 'data-url="/s/tok/vid-2/done" checked' in done


def test_an_empty_page_says_so():
    assert "Nothing to schedule right now." in kit.kit_page("tok", [])


# --- which reels the page lists -----------------------------------------------

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def _video(video_id: str, hours_from_now: float, **over):
    doc = {
        "video_id": video_id,
        "story_slug": f"slug-{video_id}",
        "title": video_id,
        "status": "scheduled",
        # Mongo hands back naive UTC.
        "scheduled_at": (NOW + timedelta(hours=hours_from_now)).replace(tzinfo=None),
    }
    doc.update(over)
    return doc


def test_page_lists_coming_and_recent_stories_soonest_first_and_done_last():
    videos = [
        _video("later", 30),
        _video("soon", 2),
        _video("yesterday", -24, status="published", story_scheduled_at=NOW),
        _video("ago-2d", -48, status="published"),
    ]
    assert [i.video_id for i in kit.page_items(videos, NOW)] == ["ago-2d", "soon", "later", "yesterday"]
    assert [i.done for i in kit.page_items(videos, NOW)] == [False, False, False, True]


def test_a_story_drops_off_three_days_after_its_time():
    kept = _video("edge", -71)
    gone = _video("old", -73)
    assert [i.video_id for i in kit.page_items([kept, gone], NOW)] == ["edge"]


def test_trial_reels_wait_until_they_graduate():
    pending = _video("trial", -5, trial_graduation="MANUAL", trial_check_state="pending")
    met = _video(
        "grad",
        -100,
        trial_graduation="MANUAL",
        trial_check_state="met",
        trial_check_checked_at=NOW - timedelta(hours=1),
    )
    [item] = kit.page_items([pending, met], NOW)
    # Graduated an hour ago, so it is listed though its reel went up four days back.
    assert item.video_id == "grad" and item.graduated
    assert "as soon as you share it to followers" in kit.kit_page("t", [item])


def test_archived_or_slugless_reels_are_left_off():
    assert kit.page_items([_video("a", 2, status="archived"), _video("b", 2, story_slug=None)], NOW) == []


def test_page_escapes_a_hostile_title():
    bad = kit.KitItem("v", "s", "<script>alert(1)</script>", REEL, None)
    assert "<script>alert(1)" not in kit.kit_page("t", [bad]).split("<script>")[0]
    assert "&lt;script&gt;" in kit.kit_page("t", [bad])


def test_graduation_email_carries_the_story_card_only_when_given_one():
    kwargs = dict(title="T", channel="TryAlgoViz", permalink="https://ig/p/1", after_hours=6.0, groups=[])
    plain = format_graduate_email(**kwargs)
    card = format_graduate_email(
        **kwargs,
        story_html=kit.story_card_html("https://s/s/tok", "as soon as you graduate it"),
        story_text=kit.story_card_text("https://s/s/tok", "as soon as you graduate it"),
    )
    assert "Get story" not in plain.html and "Get story" in card.html
    assert "https://s/s/tok" in card.text and "https://s/s/tok" not in plain.text
