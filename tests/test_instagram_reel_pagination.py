"""get_reels must not return the same reel twice.

/media is newest-first and cursor-paged. Posting during pagination shifts every
item down a slot, so one can be returned on two consecutive pages — the same
seam that imported a YouTube video twice. Downstream that duplicate would be
categorised and inserted twice.
"""

from __future__ import annotations

from app.services.instagram import InstagramService


def _reel(media_id):
    return {"id": media_id, "media_type": "REEL"}


def _service(pages):
    svc = InstagramService(access_token="t")
    calls = iter(pages)

    def fake_get(_endpoint, params=None):
        return next(calls)

    svc._get = fake_get  # type: ignore[method-assign]
    return svc


def test_a_reel_straddling_a_page_seam_is_returned_once():
    pages = [
        {"data": [_reel("r1"), _reel("r2")], "paging": {"next": "https://graph.facebook.com/v25.0/next"}},
        {"data": [_reel("r2"), _reel("r3")], "paging": {}},
    ]
    ids = [r["id"] for r in _service(pages).get_reels("123")]
    assert ids == ["r1", "r2", "r3"]


def test_non_video_media_is_still_filtered_out():
    pages = [{"data": [_reel("r1"), {"id": "img", "media_type": "IMAGE"}], "paging": {}}]
    assert [r["id"] for r in _service(pages).get_reels("123")] == ["r1"]


def test_a_single_page_is_unchanged():
    pages = [{"data": [_reel("r1"), _reel("r2")], "paging": {}}]
    assert [r["id"] for r in _service(pages).get_reels("123")] == ["r1", "r2"]
