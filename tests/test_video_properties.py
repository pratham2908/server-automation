"""Owner-defined video properties: defining them, tagging videos, filtering, and the routes."""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.services.video_properties import (
    PropertyDef,
    PropertyDefIn,
    PropertyValue,
    PropertyValueIn,
    clean_assignment,
    define_properties,
    parse_property_filter,
    property_filter_query,
    removed_ids,
)
from tests.mongo_fakes import DB


def _ids():
    counter = itertools.count(1)
    return lambda: f"id{next(counter)}"


NICHE = PropertyDef(
    id="niche",
    name="Niche",
    values=[PropertyValue(id="dsa", label="DSA"), PropertyValue(id="path", label="Pathfinding")],
)


class TestDefine:
    def test_mints_ids_for_new_and_keeps_existing(self):
        defs = define_properties(
            [
                PropertyDefIn(
                    id="niche",
                    name=" Niche ",
                    values=[PropertyValueIn(id="dsa", label="DSA"), PropertyValueIn(label="Graphs")],
                )
            ],
            _ids(),
        )
        assert defs[0].id == "niche" and defs[0].name == "Niche"
        assert [(v.id, v.label) for v in defs[0].values] == [("dsa", "DSA"), ("id1", "Graphs")]

    def test_duplicate_names_or_labels_are_refused_ignoring_case(self):
        with pytest.raises(ValueError):
            define_properties([PropertyDefIn(name="Niche"), PropertyDefIn(name="niche")], _ids())
        with pytest.raises(ValueError):
            define_properties(
                [PropertyDefIn(name="N", values=[PropertyValueIn(label="DSA"), PropertyValueIn(label="dsa")])], _ids()
            )

    def test_ids_that_could_reshape_a_mongo_path_are_rejected(self):
        for bad in ("$where", "a.b", "Niche"):
            with pytest.raises(ValidationError):
                PropertyDefIn(id=bad, name="x")


def test_removed_ids_reports_dropped_properties_and_values():
    new = [PropertyDef(id="niche", name="Niche", values=[PropertyValue(id="dsa", label="DSA")])]
    assert removed_ids([NICHE], new) == (set(), {"niche": {"path"}})
    assert removed_ids([NICHE], []) == ({"niche"}, {})


def test_assignment_drops_unknowns_and_keeps_the_property_order():
    assert clean_assignment([NICHE], {"niche": ["path", "nope", "dsa", "dsa"], "ghost": ["x"]}) == {
        "niche": ["dsa", "path"]
    }
    assert clean_assignment([NICHE], {"niche": []}) == {}


def test_filter_parsing_skips_anything_unsafe():
    assert parse_property_filter("niche:dsa,path;series:cities") == {"niche": ["dsa", "path"], "series": ["cities"]}
    assert parse_property_filter("$where:1;a.b:c;niche:;:x;niche2:$bad,ok") == {"niche2": ["ok"]}
    assert parse_property_filter(None) == {}
    assert property_filter_query({"niche": ["dsa"], "series": ["c"]}) == {
        "properties.niche": {"$in": ["dsa"]},
        "properties.series": {"$in": ["c"]},
    }


# --- routes ----------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    from app import main
    from app.database import get_db
    from app.dependencies import verify_api_key

    db = DB(
        channels=[{"channel_id": "c1", "video_properties": [NICHE.model_dump()]}],
        videos=[
            {"channel_id": "c1", "video_id": "v1", "properties": {"niche": ["dsa", "path"]}},
            {"channel_id": "c1", "video_id": "v2", "properties": {"niche": ["path"]}},
        ],
    )
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_api_key] = lambda: None
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(verify_api_key, None)


def test_tagging_a_video_stores_only_known_values(client):
    http, db = client
    res = http.put("/api/v1/channels/c1/videos/v2/properties", json={"properties": {"niche": ["dsa", "zzz"]}})
    assert res.status_code == 200 and res.json()["properties"] == {"niche": ["dsa"]}
    assert db.videos.docs[1]["properties"] == {"niche": ["dsa"]}
    assert http.put("/api/v1/channels/c1/videos/nope/properties", json={"properties": {}}).status_code == 404


def test_removing_a_property_untags_its_videos(client):
    http, db = client
    res = http.put("/api/v1/channels/c1/video-properties", json={"properties": []})
    assert res.status_code == 200 and res.json()["properties"] == []
    assert all("niche" not in (v.get("properties") or {}) for v in db.videos.docs)


def test_a_duplicate_name_is_a_422(client):
    http, _ = client
    res = http.put("/api/v1/channels/c1/video-properties", json={"properties": [{"name": "A"}, {"name": "a"}]})
    assert res.status_code == 422


def test_removing_a_value_untags_only_that_value(client):
    http, db = client
    res = http.put(
        "/api/v1/channels/c1/video-properties",
        json={"properties": [{"id": "niche", "name": "Niche", "values": [{"id": "dsa", "label": "DSA"}]}]},
    )
    assert res.status_code == 200
    assert [v["properties"] for v in db.videos.docs] == [{"niche": ["dsa"]}, {"niche": []}]
