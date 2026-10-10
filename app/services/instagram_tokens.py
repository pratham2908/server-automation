"""Which Instagram token a channel uses for what.

A channel can hold two tokens, because the two login types are good at different
things. Instagram Login (``graph.instagram.com``) publishes fine but, for this
app, returns no comments to read; Facebook Login (``graph.facebook.com``) reads
comments with usernames, runs competitor lookups, and can delete media. So the
channel keeps one *primary* token (what every existing feature already uses) and
optionally one *alt* token of the other login type, and each feature asks for
the provider it needs.

Everything here is pure so the rules are testable without a database.
"""

from __future__ import annotations

from typing import Any

PROVIDER_FACEBOOK = "facebook"
PROVIDER_INSTAGRAM = "instagram"
PROVIDERS = (PROVIDER_FACEBOOK, PROVIDER_INSTAGRAM)

PRIMARY_FIELD = "instagram_tokens"
ALT_FIELD = "instagram_tokens_alt"

# What each job prefers. Reading comments needs Facebook Login: with an Instagram Login token the
# comments edge returns an empty list for this app. Replying is a write, which Instagram Login does
# fine, so it prefers that and falls back to Facebook on channels that only have the one token.
PREFER_FOR_READING_COMMENTS = PROVIDER_FACEBOOK
PREFER_FOR_REPLYING = PROVIDER_INSTAGRAM

# Fields a channel document must never return to a client. Kept in one place so a
# new token slot cannot be added without the exclusion list noticing.
SECRET_CHANNEL_FIELDS = ("youtube_tokens", PRIMARY_FIELD, ALT_FIELD)


def secret_projection() -> dict[str, int]:
    """A Mongo projection that drops every token field."""
    return {field: 0 for field in SECRET_CHANNEL_FIELDS}


def provider_of(tokens: dict[str, Any] | None) -> str:
    """Documents stored before the provider field existed are Facebook Login."""
    return str((tokens or {}).get("provider") or PROVIDER_FACEBOOK)


def normalise_provider(value: str | None) -> str:
    return value if value in PROVIDERS else PROVIDER_FACEBOOK


def _usable(tokens: dict[str, Any] | None) -> bool:
    return bool(tokens and tokens.get("access_token"))


def select_slot(channel: dict[str, Any], prefer: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """The ``(field, token doc)`` to use, or ``None`` if the channel has no token.

    ``prefer`` names a provider. If a slot of that provider exists it wins; if not
    the primary is returned anyway, so a feature that merely *prefers* Facebook
    still works on a channel that only has Instagram Login.
    """
    primary = channel.get(PRIMARY_FIELD)
    alt = channel.get(ALT_FIELD)
    if prefer:
        for field, tokens in ((PRIMARY_FIELD, primary), (ALT_FIELD, alt)):
            if _usable(tokens) and provider_of(tokens) == prefer:
                return field, dict(tokens)  # type: ignore[arg-type]  # _usable() guarantees a dict
    if _usable(primary):
        return PRIMARY_FIELD, dict(primary)  # type: ignore[arg-type]
    if _usable(alt):
        return ALT_FIELD, dict(alt)  # type: ignore[arg-type]
    return None


def has_provider(channel: dict[str, Any], provider: str) -> bool:
    """Whether the channel holds a usable token of exactly this login type."""
    return any(
        _usable(channel.get(field)) and provider_of(channel.get(field)) == provider
        for field in (PRIMARY_FIELD, ALT_FIELD)
    )


def ig_user_id_for(channel: dict[str, Any], tokens: dict[str, Any]) -> str:
    """The Instagram user id to address calls with, for this particular token.

    An Instagram Login channel stores an Instagram-scoped id, which Facebook's
    Graph does not recognise; the business-account id a Facebook token needs
    lives on that token's own document. Falls back to the channel's id, which is
    right for the common case of a Facebook primary.
    """
    return str(tokens.get("instagram_user_id") or channel.get("instagram_user_id") or "")


def slot_for_store(channel: dict[str, Any], provider: str) -> str:
    """Which slot a newly supplied token of *provider* belongs in.

    Replaces the same-provider slot if there is one; otherwise fills the free one.
    A channel with no token at all gets the primary.
    """
    primary = channel.get(PRIMARY_FIELD)
    alt = channel.get(ALT_FIELD)
    if not _usable(primary):
        return PRIMARY_FIELD
    if provider_of(primary) == provider:
        return PRIMARY_FIELD
    if _usable(alt) and provider_of(alt) == provider:
        return ALT_FIELD
    return ALT_FIELD


def build_token_doc(
    access_token: str,
    provider: str,
    expires_at: str | None = None,
    instagram_user_id: str | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "access_token": access_token.strip(),
        "token_type": "bearer",
        "expires_at": expires_at,
        "provider": normalise_provider(provider),
    }
    if instagram_user_id:
        doc["instagram_user_id"] = instagram_user_id
    return doc


def summarise_slots(channel: dict[str, Any]) -> list[dict[str, Any]]:
    """Which tokens exist, without the tokens themselves."""
    rows: list[dict[str, Any]] = []
    for field, label in ((PRIMARY_FIELD, "primary"), (ALT_FIELD, "alt")):
        tokens = channel.get(field)
        if _usable(tokens):
            rows.append(
                {
                    "slot": label,
                    "provider": provider_of(tokens),
                    "expires_at": (tokens or {}).get("expires_at"),
                    "instagram_user_id": ig_user_id_for(channel, tokens or {}),
                }
            )
    return rows
