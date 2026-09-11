"""The per-kind adapter layer must keep apps interchangeable — and secrets in.

Content apps disagree on everything: cursors vs page numbers, ``id`` vs ``_id``,
milliseconds vs seconds, a static secret vs an account login. The adapters exist
so none of that reaches the importer, the worker, or the UI.

The two failures worth guarding against are a normalisation drift that silently
mangles a field (a duration in the wrong unit renders every clip as 0:00) and a
credential reaching an API response.
"""

import pytest
from pydantic import ValidationError

from app.models.video_source import GeoRankConfig, VideoSource, VidForgeConfig
from app.services.video_source_service import parse_source, to_public
from app.services.video_sources import SourceUnavailableError, adapter_for, known_kinds
from app.services.video_sources.georank import GeoRankAdapter
from app.services.video_sources.vidforge import VidForgeAdapter

SECRET = "Pt02vSXYZ8UgNJquVWZA"
PASSWORD = "sup3r-secret-pw"


def georank_source() -> VideoSource:
    return VideoSource(
        source_id="s-geo",
        channel_id="ch",
        name="Renderer",
        base_url="https://geo.example.com/",
        config=GeoRankConfig(api_key=SECRET),
    )


def vidforge_source(**overrides) -> VideoSource:
    config = {"email": "a@b.com", "password": PASSWORD, **overrides}
    return VideoSource(
        source_id="s-vf",
        channel_id="ch",
        name="Studio",
        base_url="https://vf.example.com",
        config=VidForgeConfig(**config),
    )


# ---------------------------------------------------------------- registry


def test_every_kind_resolves_to_an_adapter():
    for source in (georank_source(), vidforge_source()):
        assert adapter_for(source).kind == source.kind


def test_known_kinds_covers_both_configs():
    assert set(known_kinds()) == {"georank", "vidforge"}


# ---------------------------------------------------------------- secrecy


@pytest.mark.parametrize("source,secret", [(georank_source(), SECRET), (vidforge_source(), PASSWORD)])
def test_public_projection_never_carries_a_credential(source, secret):
    public = to_public(source)
    assert secret not in public.model_dump_json()
    # The config is not a field at all, so no future secret can leak through it.
    assert "config" not in type(public).model_fields


def test_credential_hint_redacts_the_secret():
    hint = adapter_for(georank_source()).credential_hint(georank_source())
    assert SECRET not in hint
    assert hint.endswith(SECRET[-4:])


# ---------------------------------------------------------------- normalisation


def test_georank_normalises_the_feed_shape():
    v = GeoRankAdapter.normalise(
        {
            "id": "r1",
            "title": "Sea 1",
            "status": "completed",
            "durationMs": 8200,
            "thumbnailUrl": "https://x/t.jpg",
            "alreadySentToChannel": True,
            "externalVideoId": "our-uuid",
        }
    )
    assert (v.id, v.title, v.duration_ms) == ("r1", "Sea 1", 8200)
    assert v.already_sent_to_channel is True
    assert v.external_video_id == "our-uuid"


def test_vidforge_converts_seconds_to_milliseconds():
    """The single most damaging drift: same field name, different unit."""
    v = VidForgeAdapter.normalise({"_id": "x", "name": "Lake 1", "duration": 8.2}, "alreadySentToChannel")
    assert v.duration_ms == 8200


def test_vidforge_normalises_its_own_field_names():
    v = VidForgeAdapter.normalise(
        {
            "_id": "abc",
            "name": "Mountain 1",
            "status": "completed",
            "fileSizeBytes": 5451263,
            "alreadySentToChannel": True,
        },
        "alreadySentToChannel",
    )
    assert (v.id, v.title, v.size_bytes) == ("abc", "Mountain 1", 5451263)
    assert v.already_sent_to_channel is True
    # VidForge stores no link back to what it created here, so pushes are undetectable.
    assert v.external_video_id is None


def test_vidforge_reads_the_configured_sent_flag():
    """The flag name is configuration, not a constant — renaming it must not require code."""
    raw = {"_id": "abc", "name": "n", "deliveredToChannel": True, "alreadySentToChannel": False}
    assert VidForgeAdapter.normalise(raw, "deliveredToChannel").already_sent_to_channel is True
    assert VidForgeAdapter.normalise(raw, "alreadySentToChannel").already_sent_to_channel is False


def test_missing_optional_fields_do_not_break_normalisation():
    assert GeoRankAdapter.normalise({"id": "r"}).title == "Untitled"
    assert VidForgeAdapter.normalise({"_id": "r"}, "sent").duration_ms is None


# ---------------------------------------------------------------- config


def test_base_url_loses_its_trailing_slash():
    # Paths are joined verbatim, so a trailing slash would produce '//api'.
    assert georank_source().base_url == "https://geo.example.com"


def test_base_url_must_be_absolute():
    with pytest.raises(ValidationError):
        VideoSource(
            source_id="s", channel_id="c", name="n", base_url="geo.example.com", config=GeoRankConfig(api_key="k")
        )


def test_a_path_we_substitute_an_id_into_must_have_the_placeholder():
    with pytest.raises(ValidationError):
        GeoRankConfig(api_key="k", detail_path="/api/ext/videos")


def test_an_empty_mark_path_disables_the_callback_rather_than_failing():
    source = vidforge_source(mark_imported_path="")
    assert adapter_for(source).supports_mark_imported(source) is False


def test_the_kind_discriminator_picks_the_right_config():
    doc = {
        "source_id": "s",
        "channel_id": "c",
        "name": "n",
        "base_url": "https://x.example.com",
        "config": {"kind": "vidforge", "email": "a@b.com", "password": "p"},
    }
    source = parse_source(doc)
    assert isinstance(source.config, VidForgeConfig)
    assert source.kind == "vidforge"


def test_a_pre_kind_document_fails_with_a_pointer_to_the_migration():
    doc = {
        "source_id": "old",
        "channel_id": "c",
        "name": "n",
        "base_url": "https://x.example.com",
        "api_key": SECRET,
        "list_path": "/api/ext/videos",
    }
    with pytest.raises(ValueError, match="migrate_video_sources"):
        parse_source(doc)


# ---------------------------------------------------------------- capabilities


def test_only_apps_that_push_carry_duplicate_risk():
    """Decides whether a missed callback reaches the error queue or just the job."""
    assert adapter_for(georank_source()).pushes_to_us is True
    assert adapter_for(vidforge_source()).pushes_to_us is False


# ---------------------------------------------------------------- token cache


def test_changing_credentials_invalidates_the_cached_token():
    """A credential change must not keep reading the previous account's library.

    The cache lives for the token's lifetime, so keying it on the source alone
    would serve the old account for minutes after the config already named a new
    one — silently listing the wrong videos for a channel.
    """
    from app.services.video_sources.vidforge import _cache_key

    before = _cache_key(vidforge_source())
    after = _cache_key(vidforge_source(email="someone.else@example.com"))
    assert before != after

    # The app key selects the library too, so it is part of the account identity.
    assert _cache_key(vidforge_source()) != _cache_key(vidforge_source(app_key="shorts"))


# ---------------------------------------------------------------- marking by hand


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass


@pytest.mark.asyncio
async def test_georank_omits_the_video_id_when_marking_by_hand(monkeypatch):
    """An operator retiring a video has no local video to name.

    Sending ``externalVideoId: null`` would assert a link that does not exist, so
    the optional body is omitted entirely instead.
    """
    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return _Response(200)

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    source = georank_source()
    assert await GeoRankAdapter().mark_imported(source, "r1", None) is None
    assert captured["json"] == {}
    assert captured["url"].endswith("/api/ext/videos/r1/imported")


@pytest.mark.asyncio
async def test_georank_sends_the_video_id_when_we_have_one(monkeypatch):
    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None):
        captured["json"] = json
        return _Response(200)

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    await GeoRankAdapter().mark_imported(georank_source(), "r1", "our-uuid")
    assert captured["json"] == {"externalVideoId": "our-uuid"}


# ---------------------------------------------------------------- grouping


def test_only_apps_that_bundle_their_output_name_a_group():
    """The noun drives whether the UI groups at all, so an app without one is flat."""
    assert adapter_for(vidforge_source()).group_noun == "Episode"
    assert adapter_for(georank_source()).group_noun is None


def test_takes_of_one_episode_share_a_group_and_a_label():
    """Each export is named after its clip, with the clip id appended.

    Left in, the id makes every take read as a different title, so three renders
    of one episode would look like three unrelated videos.
    """
    from app.services.video_sources.vidforge import episode_label

    raw = [
        {"_id": "a", "name": "Why Birds Don't Fry (6a85f6aec7ac28f0e2efbd12)", "sourceEpisodeId": "ep1"},
        {"_id": "b", "name": "Why Birds Don't Fry (6a85f6aec7ac28f0e2efbd99)", "sourceEpisodeId": "ep1"},
    ]
    videos = [VidForgeAdapter.normalise(r, "sent") for r in raw]

    assert {v.group_id for v in videos} == {"ep1"}
    assert {v.group_label for v in videos} == {"Why Birds Don't Fry"}
    # The full name stays on the video — the label is for the bundle, not the take.
    assert videos[0].title != videos[1].title
    assert episode_label("Why Birds Don't Fry (6a85f6aec7ac28f0e2efbd12)") == "Why Birds Don't Fry"


def test_a_video_in_no_episode_is_ungrouped():
    v = VidForgeAdapter.normalise({"_id": "x", "name": "how egg is made"}, "sent")
    assert v.group_id is None
    assert v.group_label is None


def test_a_name_that_is_only_an_id_keeps_its_name():
    """Stripping must never leave a video with a blank label."""
    v = VidForgeAdapter.normalise({"_id": "x", "name": "(6a85f6aec7ac28f0e2efbd12)", "sourceEpisodeId": "ep1"}, "sent")
    assert v.group_label == "(6a85f6aec7ac28f0e2efbd12)"


def test_a_title_with_its_own_parentheses_is_left_alone():
    """Only a trailing 24-hex id is a render suffix; real titles keep their brackets."""
    v = VidForgeAdapter.normalise(
        {"_id": "x", "name": "How Planes Fly (The Real Reason)", "sourceEpisodeId": "ep1"}, "sent"
    )
    assert v.group_label == "How Planes Fly (The Real Reason)"


# ---------------------------------------------------------------- generation


class _FakeResponse:
    """Just enough of httpx.Response for the generation helpers."""

    def __init__(self, payload, *, json_error: bool = False):
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


def _generating_source(**gen_overrides) -> VideoSource:
    """A GeoRank source that can render, with the capability fully configured."""
    gen = {
        "create_path": "/api/ext/renders",
        "status_path": "/api/ext/renders/{id}",
        "eta_minutes": 15,
        **gen_overrides,
    }
    return VideoSource(
        source_id="s-geo",
        channel_id="ch",
        name="Renderer",
        base_url="https://geo.example.com",
        config=GeoRankConfig(api_key=SECRET, generation=gen),
    )


def _stub(adapter, response):
    """Replace the adapter's authenticated call with a canned response."""
    calls = []

    async def fake(source, method, path, *, json_body=None):
        calls.append((method, path, json_body))
        return response

    adapter.authed_request = fake  # type: ignore[method-assign]
    return calls


def test_generation_is_opt_in_per_source():
    """A source with no generation block must never be asked to render."""
    assert GeoRankAdapter().supports_generation(georank_source()) is False
    assert GeoRankAdapter().supports_generation(_generating_source()) is True
    assert VidForgeAdapter().supports_generation(vidforge_source()) is False


def test_public_projection_reports_the_capability_and_its_eta():
    plain = to_public(georank_source())
    assert plain.supports_generation is False
    assert plain.generation_eta_minutes is None

    capable = to_public(_generating_source())
    assert capable.supports_generation is True
    assert capable.generation_eta_minutes == 15
    # Still no secret, however the projection grew.
    assert SECRET not in capable.model_dump_json()


@pytest.mark.asyncio
async def test_request_generation_posts_and_returns_the_job_id():
    adapter = GeoRankAdapter()
    calls = _stub(adapter, _FakeResponse({"id": "job-7"}))

    job_id = await adapter.request_generation(_generating_source())

    assert job_id == "job-7"
    assert calls == [("POST", "/api/ext/renders", {})]


@pytest.mark.asyncio
async def test_request_generation_accepts_a_wrapped_job():
    """A deployment that wraps its response should not fail an accepted render."""
    adapter = GeoRankAdapter()
    _stub(adapter, _FakeResponse({"job": {"id": "job-9"}}))
    assert await adapter.request_generation(_generating_source()) == "job-9"


@pytest.mark.asyncio
async def test_request_generation_tolerates_an_app_that_returns_nothing_pollable():
    adapter = GeoRankAdapter()
    _stub(adapter, _FakeResponse(None, json_error=True))
    # Accepted, but unpollable — the catalogue check is what will notice it land.
    assert await adapter.request_generation(_generating_source()) == ""


@pytest.mark.asyncio
async def test_request_generation_refuses_a_source_without_the_capability():
    with pytest.raises(SourceUnavailableError):
        await GeoRankAdapter().request_generation(georank_source())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"status": "completed"}, "completed"),
        ({"status": "COMPLETED"}, "completed"),
        ({"status": "failed"}, "failed"),
        ({"status": "cancelled"}, "failed"),
        ({"status": "rendering"}, "pending"),
        ({}, "pending"),
        ({"job": {"status": "completed"}}, "completed"),
    ],
)
async def test_generation_state_maps_app_status_onto_ours(payload, expected):
    adapter = GeoRankAdapter()
    _stub(adapter, _FakeResponse(payload))
    assert await adapter.generation_state(_generating_source(), "job-1") == expected


@pytest.mark.asyncio
async def test_generation_state_is_pending_when_the_app_cannot_be_asked():
    """No status endpoint, or no job id, must not read as a failed render."""
    adapter = GeoRankAdapter()
    _stub(adapter, _FakeResponse({"status": "failed"}))

    no_status_path = _generating_source(status_path="")
    assert await adapter.generation_state(no_status_path, "job-1") == "pending"
    assert await adapter.generation_state(_generating_source(), "") == "pending"
