"""The add-a-source form is derived from the config models, so it must stay true.

If this drifts, the UI asks for the wrong things: a missing required field makes
a kind unconnectable, and a secret leaking a default would put a credential in
the page source.
"""

from app.models.video_source import GeoRankConfig, VidForgeConfig
from app.services.video_sources.schema import (
    CONFIG_MODELS,
    SECRET_FIELDS,
    describe_kind,
    describe_kinds,
    humanise,
)


def fields_of(kind: str) -> dict:
    return {f.name: f for f in describe_kind(kind).fields}


def test_every_registered_kind_is_describable():
    from app.services.video_sources import known_kinds

    assert sorted(CONFIG_MODELS) == sorted(known_kinds())
    assert [k.kind for k in describe_kinds()] == sorted(known_kinds())


def test_the_form_asks_only_for_what_the_operator_knows():
    """Required fields are the credentials; everything else has a working default."""
    assert [n for n, f in fields_of("vidforge").items() if not f.advanced] == ["email", "password"]
    assert [n for n, f in fields_of("georank").items() if not f.advanced] == ["api_key"]


def test_the_discriminator_is_never_an_input():
    """It is chosen by picking a kind, not typed in."""
    for kind in CONFIG_MODELS:
        assert "kind" not in fields_of(kind)


def test_secrets_are_marked_and_typed_as_secrets():
    for kind, secret in (("vidforge", "password"), ("georank", "api_key")):
        field = fields_of(kind)[secret]
        assert field.secret is True
        assert field.type == "password"
        # A default here would be a credential shipped to every browser.
        assert field.default is None


def test_a_fixed_choice_becomes_a_select_with_its_options():
    auth = fields_of("georank")["auth_style"]
    assert auth.type == "select"
    assert auth.options == ["bearer", "api_key_header"]


def test_an_integer_setting_is_typed_as_a_number():
    assert fields_of("vidforge")["page_limit"].type == "number"


def test_defaults_match_the_model_they_came_from():
    """The whole point of deriving: one source of truth for what a default is."""
    assert fields_of("vidforge")["app_key"].default == VidForgeConfig.model_fields["app_key"].default
    assert fields_of("georank")["list_path"].default == GeoRankConfig.model_fields["list_path"].default


def test_help_text_comes_from_the_model_description():
    assert fields_of("vidforge")["app_key"].help == VidForgeConfig.model_fields["app_key"].description


def test_a_kind_can_be_built_from_its_own_required_fields_alone():
    """Whatever the form marks required must be enough to construct the config."""
    for kind, model in CONFIG_MODELS.items():
        required = [f.name for f in describe_kind(kind).fields if f.required]
        model(**dict.fromkeys(required, "x"))


def test_every_secret_field_name_is_recognised_as_one():
    """A config gaining a credential under a new name must not slip through."""
    for model in CONFIG_MODELS.values():
        for name in model.model_fields:
            if any(word in name for word in ("password", "secret", "token")) or name == "api_key":
                assert name in SECRET_FIELDS, f"{name} is credential-shaped but not marked secret"


def test_labels_read_as_english():
    assert humanise("api_key") == "API key"
    assert humanise("login_path") == "Login path"
    assert humanise("email") == "Email"
    assert humanise("base_url") == "Base URL"
