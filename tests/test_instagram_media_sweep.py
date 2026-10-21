"""The one-call-per-page media sweep: every post, its insights, deduped across page seams."""

from __future__ import annotations

from typing import Any

from app.services.instagram import InstagramService


def _post(media_id: str, views: int | None = 10, media_type: str = "VIDEO") -> dict[str, Any]:
    post: dict[str, Any] = {"id": media_id, "media_type": media_type, "like_count": 2, "comments_count": 1}
    if views is not None:
        post["insights"] = {
            "data": [{"name": "views", "values": [{"value": views}]}, {"name": "saved", "values": [{"value": 3}]}]
        }
    return post


def _service(pages: list[dict[str, Any]]) -> tuple[InstagramService, list[dict[str, Any] | None]]:
    svc = InstagramService(access_token="t")
    calls = iter(pages)
    seen_params: list[dict[str, Any] | None] = []

    def fake_get(_endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        seen_params.append(params)
        return next(calls)

    svc._get = fake_get  # type: ignore[method-assign]
    return svc, seen_params


def test_asks_for_insights_inside_the_listing() -> None:
    svc, params = _service([{"data": [_post("r1")], "paging": {}}])
    svc.get_media_sweep("123")
    assert "insights.metric(" in params[0]["fields"] and "views" in params[0]["fields"]


def test_pages_are_followed_and_a_seam_duplicate_kept_once() -> None:
    pages = [
        {"data": [_post("r1"), _post("r2")], "paging": {"next": "https://graph.instagram.com/v25.0/next"}},
        {"data": [_post("r2"), _post("r3")], "paging": {}},
    ]
    sweep = _service(pages)[0].get_media_sweep("123")
    assert [p["id"] for p in sweep.posts] == ["r1", "r2", "r3"]
    assert sweep.insights["r3"] == {"views": 10.0, "saved": 3.0}


def test_reels_are_the_video_posts_and_missing_insights_are_left_out() -> None:
    pages = [{"data": [_post("r1"), _post("img", media_type="IMAGE"), _post("r2", views=None)], "paging": {}}]
    sweep = _service(pages)[0].get_media_sweep("123")
    assert [r["id"] for r in sweep.reels] == ["r1", "r2"]
    assert set(sweep.insights) == {"r1", "img"}
