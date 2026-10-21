"""An Instagram caption must never carry more than 30 hashtags, or the publish is refused."""

from __future__ import annotations

import re

from app.services.schedule_operation import MAX_HASHTAGS, _build_instagram_caption, cap_hashtags


def _count(text: str) -> int:
    return len(re.findall(r"#\w+", text))


def test_a_caption_within_the_limit_is_left_exactly_as_it_is():
    text = "Hello  there\n\n#a #b  #c"
    assert cap_hashtags(text) == text


def test_hashtags_past_the_limit_are_dropped_from_the_end():
    text = "intro " + " ".join(f"#t{i}" for i in range(40))
    capped = cap_hashtags(text)
    assert _count(capped) == MAX_HASHTAGS
    assert "#t29" in capped and "#t30" not in capped
    assert capped.startswith("intro #t0")


def test_the_prose_around_dropped_tags_survives():
    text = "Story one.\n\n" + " ".join(f"#a{i}" for i in range(31)) + "\n\nSubscribe for more"
    capped = cap_hashtags(text)
    assert "Story one." in capped and "Subscribe for more" in capped
    assert _count(capped) == 30


def test_the_failed_spain_caption_shape_now_fits():
    """A description already holding 27 hashtags plus 28 tags used to total well over 30."""
    desc_tags = [f"Desc{i}" for i in range(27)]
    video = {
        "title": "Why Spain Owns Two Cities",
        "description": "Spain owns two cities.\n\n" + " ".join(f"#{t}" for t in desc_tags),
        "tags": desc_tags[:5] + [f"Extra{i}" for i in range(23)],
    }
    caption = _build_instagram_caption(video)
    assert _count(caption) == MAX_HASHTAGS
    assert "Spain owns two cities." in caption


def test_a_normal_video_keeps_all_its_tags():
    video = {"title": "t", "description": "d", "tags": ["One", "Two Words", "#Three"]}
    assert _build_instagram_caption(video) == "t\n\nd\n\n#One #TwoWords #Three"
