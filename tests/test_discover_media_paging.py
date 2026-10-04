"""Paging behaviour of business_discovery media fetches.

A 500 from Meta on these queries means "Please reduce the amount of data you're
asking for" — the page was too big, not wrong. Failing the whole request over
that loses a fetch that would have worked slightly smaller, and reporting it as
"the target must be a Business account" sends the caller after the wrong fix.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from app.services.instagram import MIN_MEDIA_PAGE_SIZE, InstagramService, graph_error_message


def _http_error(status: int, body: Any = None) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    if body is not None:
        import json as _json

        response._content = _json.dumps(body).encode()
    exc = requests.HTTPError(f"{status} Server Error: for url: https://graph.facebook.com/x")
    exc.response = response
    return exc


def _too_much_data() -> requests.HTTPError:
    return _http_error(500, {"error": {"message": "Please reduce the amount of data you're asking for", "code": 1}})


def _page(ids: list[str], after: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"data": [{"id": i} for i in ids]}
    if after:
        body["paging"] = {"cursors": {"after": after}}
    return {"business_discovery": {"media": body}}


class _Recorder:
    """Stands in for ``_get``, recording the media.limit() of every call."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.sizes: list[int] = []
        self.cursors: list[str | None] = []

    def __call__(self, _endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        fields = (params or {}).get("fields", "")
        self.sizes.append(int(fields.split("media.limit(")[1].split(")")[0]))
        self.cursors.append(fields.split(".after(")[1].split(")")[0] if ".after(" in fields else None)
        if not self._script:
            raise AssertionError("more calls than the test scripted")
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _service(script: list[Any]) -> tuple[InstagramService, _Recorder]:
    svc = InstagramService(access_token="t")
    rec = _Recorder(script)
    svc._get = rec  # type: ignore[method-assign]
    return svc, rec


# --- the page never exceeds the caller's total ---------------------------------


def test_the_page_is_never_larger_than_the_limit():
    """A limit of 3 should cost one small page, not a 50-item one we then slice
    — and an oversized page is exactly what provokes the 500."""
    svc, rec = _service([_page(["a", "b", "c"])])
    svc.discover_all_media("own", "target", limit=3)
    assert rec.sizes == [3]


def test_an_explicit_page_size_is_still_capped_by_the_limit():
    svc, rec = _service([_page(["a"])])
    svc.discover_all_media("own", "target", limit=1, page_size=50)
    assert rec.sizes == [1]


def test_a_page_size_below_the_limit_is_left_alone():
    svc, rec = _service([_page(["a", "b"], after="c1"), _page(["c", "d"])])
    svc.discover_all_media("own", "target", limit=100, page_size=2)
    assert rec.sizes == [2, 2]


# --- halving on 500 -----------------------------------------------------------


def test_a_500_retries_the_same_page_at_half_the_size():
    svc, rec = _service([_too_much_data(), _page(["a"])])
    svc.discover_all_media("own", "target", limit=50, page_size=50)
    assert rec.sizes == [50, 25]


def test_it_halves_down_to_the_floor_before_giving_up():
    """50 → 25 → 12 → 6, then stop: below the floor the request is not the
    problem and retrying only burns calls against a shared rate budget."""
    svc, rec = _service([_too_much_data() for _ in range(4)])
    with pytest.raises(requests.HTTPError):
        svc.discover_all_media("own", "target", limit=50, page_size=50)
    assert rec.sizes == [50, 25, 12, MIN_MEDIA_PAGE_SIZE]


def test_the_retried_page_asks_for_the_same_cursor():
    """Halving must re-fetch the page that failed, not skip past it."""
    svc, rec = _service([_page(["a"], after="CUR"), _too_much_data(), _page(["b"])])
    svc.discover_all_media("own", "target", limit=100, page_size=50)
    assert rec.cursors == [None, "CUR", "CUR"]


def test_the_smaller_page_is_kept_for_the_rest_of_the_run():
    """Going back up to 50 would rediscover the same ceiling on every page."""
    svc, rec = _service([_too_much_data(), _page(["a"], after="c1"), _page(["b"])])
    svc.discover_all_media("own", "target", limit=100, page_size=50)
    assert rec.sizes == [50, 25, 25]


def test_an_error_that_is_not_a_500_is_not_retried():
    """A bad token or a missing target is not a size problem."""
    svc, rec = _service([_http_error(400, {"error": {"message": "Invalid OAuth token", "code": 190}})])
    with pytest.raises(requests.HTTPError):
        svc.discover_all_media("own", "target", limit=50, page_size=50)
    assert rec.sizes == [50]


def test_a_500_at_the_floor_is_raised_rather_than_looping():
    svc, rec = _service([_too_much_data()])
    with pytest.raises(requests.HTTPError):
        svc.discover_all_media("own", "target", limit=6, page_size=6)
    assert rec.sizes == [MIN_MEDIA_PAGE_SIZE]


def test_shrinking_mid_run_can_still_reach_the_limit():
    """The page budget is keyed off the floor, not the starting size, so a run
    forced down to smaller pages is not cut short of what was asked for."""
    script: list[Any] = [
        _too_much_data(),
        _page([f"a{n}" for n in range(10)], after="c1"),
        _page([f"b{n}" for n in range(10)]),
    ]
    svc, rec = _service(script)
    got = svc.discover_all_media("own", "target", limit=20, page_size=20)
    assert len(got) == 20
    assert rec.sizes == [20, 10, 10]


def test_a_caller_asking_for_pages_below_the_floor_gets_no_retry():
    """Halving a page that is already smaller than the floor cannot help."""
    svc, rec = _service([_too_much_data()])
    with pytest.raises(requests.HTTPError):
        svc.discover_all_media("own", "target", limit=4, page_size=4)
    assert rec.sizes == [4]


# --- Meta's own words ---------------------------------------------------------


def test_the_graph_message_comes_from_the_body_not_the_status_line():
    exc = _too_much_data()
    assert "reduce the amount of data" in graph_error_message(exc)
    assert "code 1" in graph_error_message(exc)


def test_a_subcode_is_included_when_present():
    exc = _http_error(400, {"error": {"message": "Invalid OAuth", "code": 190, "error_subcode": 2069032}})
    assert graph_error_message(exc) == "Invalid OAuth (code 190, subcode 2069032)"


def test_a_non_json_body_falls_back_to_the_status_line():
    """An HTML error page or a proxy timeout leaves nothing better."""
    exc = _http_error(502)
    assert "502" in graph_error_message(exc)


def test_an_exception_with_no_response_falls_back_to_its_text():
    assert graph_error_message(ValueError("boom")) == "boom"


def test_a_body_without_an_error_block_falls_back():
    exc = _http_error(500, {"something": "else"})
    assert "500" in graph_error_message(exc)
