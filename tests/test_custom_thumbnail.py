"""A thumbnail supplied at upload time beats the one the analysis extracts.

The UI reads ``ai_packaging.thumbnail_url`` in preference to the video's own
``thumbnail_url``, so storing a custom image is not enough on its own — the
analysis has to stand down, or it silently overrides the uploader's choice on
the next run.
"""

from __future__ import annotations

import io
import pathlib

import pytest

from app.services.video_service import VideoService


class _FakeR2:
    def __init__(self, fail: bool = False):
        self.uploaded: list[str] = []
        self.fail = fail

    def upload_video(self, _fh, key):
        if self.fail:
            raise RuntimeError("R2 is down")
        self.uploaded.append(key)

    def generate_presigned_url(self, key, expires_in=3600):
        return f"https://r2.example/{key}?exp={expires_in}"


def _service(r2=None):
    svc = VideoService.__new__(VideoService)
    svc.r2 = r2 if r2 is not None else _FakeR2()
    return svc


def test_a_supplied_thumbnail_is_stored_under_its_own_key():
    """Distinct from {video_id}.jpg, which the analysis writes — otherwise the
    extracted frame would overwrite the uploader's image."""
    r2 = _FakeR2()
    stored = _service(r2)._store_custom_thumbnail("c1", "v1", io.BytesIO(b"img"))

    assert r2.uploaded == ["c1/thumbnails/v1-custom.jpg"]
    assert stored is not None
    key, url = stored
    assert key == "c1/thumbnails/v1-custom.jpg"
    assert "v1-custom.jpg" in url


def test_the_key_comes_back_so_the_url_can_be_reminted():
    """Instagram fetches the cover at publish time, which can be weeks after
    upload — by then the presigned URL stored on the video has expired."""
    stored = _service()._store_custom_thumbnail("c1", "v1", io.BytesIO(b"img"))
    assert stored is not None and stored[0] == "c1/thumbnails/v1-custom.jpg"


def test_no_thumbnail_supplied_stores_nothing():
    r2 = _FakeR2()
    assert _service(r2)._store_custom_thumbnail("c1", "v1", None) is None
    assert r2.uploaded == []


def test_a_storage_failure_does_not_take_the_upload_with_it():
    """The video landed; a missing thumbnail is not worth failing that over."""
    assert _service(_FakeR2(fail=True))._store_custom_thumbnail("c1", "v1", io.BytesIO(b"img")) is None


def test_the_url_is_presigned_for_the_sigv4_maximum():
    stored = _service()._store_custom_thumbnail("c1", "v1", io.BytesIO(b"img"))
    assert stored is not None and "exp=604800" in stored[1]


# ------------------------------------------------------------------
# The analysis must leave a custom thumbnail alone
# ------------------------------------------------------------------


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or []

    async def find_one(self, query, _projection=None, sort=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    async def update_one(self, query, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                for dotted, val in (update.get("$set") or {}).items():
                    cur = d
                    parts = dotted.split(".")
                    for part in parts[:-1]:
                        cur = cur.setdefault(part, {})
                    cur[parts[-1]] = val
                return

    async def insert_one(self, doc):
        self.docs.append(doc)

    def find(self, _query, _projection=None):
        docs = self.docs

        class _Cursor:
            async def to_list(self, length=None):
                return list(docs)[:length] if length else list(docs)

        return _Cursor()


class _AnalysisDB:
    def __init__(self, video):
        self.videos = _Collection([video])
        self.channels = _Collection([{"channel_id": "c1", "platform": "youtube"}])
        self.posting_queue = _Collection()
        self.pacing_templates = _Collection()


class _Gemini:
    async def analyze_video_retention(self, *_a, **_kw):
        return {
            "packaging": {"suggested_titles": ["AI title"], "best_thumbnail_timestamp": 4.2},
            "pacing_analysis": {},
        }


async def _run(video, monkeypatch, tmp_path):
    """Drive run_retention_analysis over fakes, recording thumbnail extraction."""
    import app.services.retention_analysis as ra

    extracted: list = []

    def fake_extract(path, ts, out):
        extracted.append((path, ts))
        pathlib.Path(out).write_bytes(b"frame")
        return True

    monkeypatch.setattr(ra, "extract_thumbnail", fake_extract)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    db = _AnalysisDB(video)
    await ra.run_retention_analysis("c1", "v1", db, _FakeR2(), _Gemini(), local_video_path=str(clip))
    return extracted, db.videos.docs[0]


@pytest.mark.asyncio
async def test_a_custom_thumbnail_stops_the_analysis_extracting_one(monkeypatch, tmp_path):
    video = {
        "channel_id": "c1",
        "video_id": "v1",
        "status": "processing",
        "r2_object_key": "c1/v1.mp4",
        "thumbnail_url": "https://r2.example/c1/thumbnails/v1-custom.jpg",
        "custom_thumbnail": True,
    }
    extracted, stored = await _run(video, monkeypatch, tmp_path)

    assert extracted == []
    assert "thumbnail_url" not in (stored.get("ai_packaging") or {})
    assert "v1-custom.jpg" in stored["thumbnail_url"]


@pytest.mark.asyncio
async def test_without_one_the_analysis_still_extracts(monkeypatch, tmp_path):
    video = {"channel_id": "c1", "video_id": "v1", "status": "processing", "r2_object_key": "c1/v1.mp4"}
    extracted, stored = await _run(video, monkeypatch, tmp_path)

    assert len(extracted) == 1
    assert extracted[0][1] == 4.2  # the timestamp the model chose
    assert "thumbnails/v1.jpg" in (stored.get("ai_packaging") or {}).get("thumbnail_url", "")
