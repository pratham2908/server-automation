"""A sync must never insert the same YouTube video twice.

The uploads playlist is paginated live. When it shifts between page requests
YouTube returns one item on two consecutive pages — in practice the one on the
page boundary. That id then lands in two different 50-id detail batches, comes
back as two identical videos, and the sync inserts both under different
video_ids.

That is what put two "Why Canada has 60% of the World's Lakes" rows on
officialgeoranking, created 20 milliseconds apart, and re-created them on every
sync while the video still existed on YouTube.
"""

from unittest.mock import MagicMock

from app.services.video_service import VideoService


def build_youtube(pages: list[list[str]]) -> MagicMock:
    """A fake YouTube client whose uploads playlist returns *pages* of ids."""
    yt = MagicMock()
    calls = {"n": 0}

    def playlist_list(**kwargs):
        index = calls["n"]
        calls["n"] += 1
        page = pages[index] if index < len(pages) else []
        request = MagicMock()
        request.execute.return_value = {
            "items": [{"contentDetails": {"videoId": v}} for v in page],
            **({"nextPageToken": f"p{index + 1}"} if index + 1 < len(pages) else {}),
        }
        return request

    def videos_list(**kwargs):
        request = MagicMock()
        # YouTube collapses repeats inside one call, so a batch answers with the
        # unique ids it was given — the duplication only shows across batches.
        ids = list(dict.fromkeys(kwargs["id"].split(",")))
        request.execute.return_value = {
            "items": [
                {
                    "id": v,
                    "snippet": {"title": f"video {v}", "description": "", "publishedAt": None},
                    "statistics": {},
                    "contentDetails": {},
                    "status": {"privacyStatus": "public"},
                }
                for v in ids
            ]
        }
        return request

    yt._youtube.playlistItems.return_value.list.side_effect = playlist_list
    yt._youtube.videos.return_value.list.side_effect = videos_list
    return yt


def fetch(pages: list[list[str]]) -> list[dict]:
    service = VideoService.__new__(VideoService)  # no DB needed for this helper
    return service._fetch_all_youtube_videos(build_youtube(pages), "UCabcdefghijklmnopqrstu")


def test_an_id_repeated_across_pages_yields_one_video():
    """The real failure: the same id ends a page and starts the next one."""
    first = [f"v{i}" for i in range(50)]
    second = ["v49", "v50", "v51"]  # v49 came back at the top of page two

    videos = fetch([first, second])
    ids = [v["youtube_video_id"] for v in videos]

    assert len(ids) == len(set(ids)), f"duplicate returned: {ids}"
    assert ids.count("v49") == 1
    assert set(ids) == set(first) | set(second)


def test_order_is_preserved_so_newest_stays_first():
    videos = fetch([["a", "b", "c"], ["c", "d"]])
    assert [v["youtube_video_id"] for v in videos] == ["a", "b", "c", "d"]


def test_a_clean_playlist_is_untouched():
    videos = fetch([["a", "b"], ["c"]])
    assert [v["youtube_video_id"] for v in videos] == ["a", "b", "c"]


def test_an_id_repeated_far_apart_still_collapses():
    """Not just the page seam — any repeat must collapse."""
    videos = fetch([["a", "b", "c"], ["d", "a"]])
    assert [v["youtube_video_id"] for v in videos] == ["a", "b", "c", "d"]
