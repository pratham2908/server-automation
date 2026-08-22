"""Describe a source kind's settings as a form, derived from its config model.

The config models already declare every field, its type, whether it is required,
its default and what it means. Restating that in the UI would mean two places to
change per new app kind, and they would drift.

So the form is derived here and served to the frontend, which renders whatever it
is given. Adding an app kind stays what it should be: a config model, an adapter,
a registry line — and no frontend change at all.

Pure: no I/O, no database, no network.
"""

from __future__ import annotations

import typing
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from app.models.video_source import GeoRankConfig, SourceKind, VidForgeConfig

FieldType = Literal["text", "password", "number", "select"]

# Names whose values must never be echoed back. Membership decides both the input
# type and, more importantly, that the value is write-only.
SECRET_FIELDS = frozenset({"password", "api_key", "secret", "token"})

# Words that look wrong title-cased.
_ACRONYMS = {"api": "API", "url": "URL", "id": "ID", "ttl": "TTL"}

KIND_LABELS: dict[SourceKind, str] = {
    "georank": "Export feed",
    "vidforge": "VidForge",
}

KIND_DESCRIPTIONS: dict[SourceKind, str] = {
    "georank": "An app exposing the read-only export feed contract, authenticated with a shared secret.",
    "vidforge": "A VidForge studio library, authenticated with the account that owns it.",
}

CONFIG_MODELS: dict[SourceKind, type[BaseModel]] = {
    "georank": GeoRankConfig,
    "vidforge": VidForgeConfig,
}


def humanise(name: str) -> str:
    """``api_key`` -> ``API key``, ``login_path`` -> ``Login path``."""
    words = name.split("_")
    out = [_ACRONYMS.get(w, w) for w in words]
    if out[0] not in _ACRONYMS.values():
        out[0] = out[0].capitalize()
    return " ".join(out)


def _options(annotation: Any) -> list[str] | None:
    """A Literal annotation is a fixed set of choices, so offer exactly those."""
    if typing.get_origin(annotation) is Literal:
        return [str(a) for a in typing.get_args(annotation)]
    for arg in typing.get_args(annotation):
        if typing.get_origin(arg) is Literal:
            return [str(a) for a in typing.get_args(arg)]
    return None


def _field_type(annotation: Any, name: str) -> FieldType:
    if name in SECRET_FIELDS:
        return "password"
    if _options(annotation):
        return "select"
    # Optional[str] and friends: look through the union for a concrete type.
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    base = args[0] if args else annotation
    return "number" if base is int else "text"


class SourceKindField(BaseModel):
    """One input in the add-a-source form."""

    name: str
    label: str
    type: FieldType
    required: bool = Field(..., description="No default exists, so the operator must supply one")
    secret: bool = Field(..., description="Write-only — never returned once stored")
    default: str | None = Field(None, description="Prefilled value, when the model has one")
    options: list[str] | None = Field(None, description="The only accepted values, when the type is fixed")
    help: str | None = Field(None, description="What this field is for")
    advanced: bool = Field(
        ...,
        description="Has a working default, so it stays out of the way unless the app deviates",
    )


class SourceKindInfo(BaseModel):
    """Everything the UI needs to offer one kind of content app."""

    kind: SourceKind
    label: str
    description: str
    fields: list[SourceKindField]


def _describe_field(name: str, info: FieldInfo) -> SourceKindField:
    required = info.is_required()
    default = None if required or info.default is None else str(info.default)
    return SourceKindField(
        name=name,
        label=humanise(name),
        type=_field_type(info.annotation, name),
        required=required,
        secret=name in SECRET_FIELDS,
        default=default,
        options=_options(info.annotation),
        help=info.description,
        # A field with a default already works; only a deviating deployment needs it.
        advanced=not required,
    )


def describe_kind(kind: SourceKind) -> SourceKindInfo:
    model = CONFIG_MODELS[kind]
    return SourceKindInfo(
        kind=kind,
        label=KIND_LABELS[kind],
        description=KIND_DESCRIPTIONS[kind],
        fields=[
            _describe_field(name, info)
            # The discriminator is chosen by picking the kind, not typed by hand.
            for name, info in model.model_fields.items()
            if name != "kind"
        ],
    )


def describe_kinds() -> list[SourceKindInfo]:
    """Every kind the server can talk to, in a stable order."""
    return [describe_kind(k) for k in sorted(CONFIG_MODELS)]
