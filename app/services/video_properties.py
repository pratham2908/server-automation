"""Owner-defined properties on a channel's videos — niche, series, format, whatever the owner tracks.

Pure: no I/O. Each channel keeps its own list of properties, each with its own
values, and a video can carry several values of one property. Only the owner sets
them; nothing automatic writes here, unlike the AI-filled content params.

Properties and values have stable ids, and a video stores ids, so renaming a value
never has to touch the videos tagged with it. A video's assignment lives in
``video.properties`` as ``{property_id: [value_id, ...]}``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel, Field

MAX_PROPERTIES = 20
MAX_VALUES = 50
# Ids become part of Mongo field paths (``properties.<id>``), so they are held to plain
# lowercase letters and digits: no dots, no ``$``, nothing that could reshape a query.
ID_PATTERN = r"^[a-z0-9]{1,32}$"
_ID = re.compile(ID_PATTERN)


class PropertyValue(BaseModel):
    id: str
    label: str


class PropertyDef(BaseModel):
    id: str
    name: str
    values: list[PropertyValue]


class PropertyValueIn(BaseModel):
    # Absent for a value being added; kept for one being renamed, so its videos stay tagged.
    id: str | None = Field(default=None, pattern=ID_PATTERN)
    label: str = Field(min_length=1, max_length=60)


class PropertyDefIn(BaseModel):
    id: str | None = Field(default=None, pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=40)
    values: list[PropertyValueIn] = Field(default_factory=list, max_length=MAX_VALUES)


def define_properties(incoming: Sequence[PropertyDefIn], new_id: Callable[[], str]) -> list[PropertyDef]:
    """The channel's new property list: ids kept or minted, names trimmed, duplicates refused.

    Raises ValueError on two properties with one name, or two values with one label in a
    property (compared ignoring case), since a filter couldn't tell them apart.
    """
    if len(incoming) > MAX_PROPERTIES:
        raise ValueError(f"At most {MAX_PROPERTIES} properties per channel")
    out: list[PropertyDef] = []
    names: set[str] = set()
    for prop in incoming:
        name = prop.name.strip()
        if not name or name.lower() in names:
            raise ValueError(f"Property names must be unique and not blank: {prop.name!r}")
        names.add(name.lower())
        labels: set[str] = set()
        values: list[PropertyValue] = []
        for value in prop.values:
            label = value.label.strip()
            if not label or label.lower() in labels:
                raise ValueError(f"Values of {name!r} must be unique and not blank: {value.label!r}")
            labels.add(label.lower())
            values.append(PropertyValue(id=value.id or new_id(), label=label))
        out.append(PropertyDef(id=prop.id or new_id(), name=name, values=values))
    return out


def removed_ids(old: Sequence[PropertyDef], new: Sequence[PropertyDef]) -> tuple[set[str], dict[str, set[str]]]:
    """Properties dropped outright, and values dropped from properties that remain."""
    new_by_id = {p.id: p for p in new}
    gone_props = {p.id for p in old if p.id not in new_by_id}
    gone_values: dict[str, set[str]] = {}
    for prop in old:
        kept = new_by_id.get(prop.id)
        if kept is None:
            continue
        kept_ids = {v.id for v in kept.values}
        dropped = {v.id for v in prop.values if v.id not in kept_ids}
        if dropped:
            gone_values[prop.id] = dropped
    return gone_props, gone_values


def clean_assignment(defs: Sequence[PropertyDef], assignment: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """A video's assignment with unknown properties and values dropped, duplicates removed, empties left out.

    Values keep the order the property lists them in, so a video reads the same however it was tagged.
    """
    out: dict[str, list[str]] = {}
    for prop in defs:
        chosen = set(assignment.get(prop.id) or [])
        ordered = [v.id for v in prop.values if v.id in chosen]
        if ordered:
            out[prop.id] = ordered
    return out


def parse_property_filter(raw: str | None) -> dict[str, list[str]]:
    """``"p1:v1,v2;p2:v3"`` → ``{"p1": ["v1", "v2"], "p2": ["v3"]}``; malformed parts are skipped.

    Anything that isn't a well-formed id is dropped, since the property id lands in a field path.
    """
    out: dict[str, list[str]] = {}
    for part in (raw or "").split(";"):
        prop, _, values = part.partition(":")
        prop = prop.strip()
        ids = [v.strip() for v in values.split(",") if _ID.match(v.strip())]
        if _ID.match(prop) and ids:
            out[prop] = ids
    return out


def property_filter_query(filters: Mapping[str, Sequence[str]]) -> dict[str, dict[str, list[str]]]:
    """The Mongo filter for videos that match: any chosen value of a property, and every property chosen."""
    return {f"properties.{prop}": {"$in": list(values)} for prop, values in filters.items() if values}
