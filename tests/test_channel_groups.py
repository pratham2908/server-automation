"""Linking channels decides where videos get published, so it must be careful.

A wrong link is not a cosmetic bug: it publishes a video to a channel it does not
belong on. So suggestions are only ever suggestions, and the rules that keep a
group coherent are enforced on the model rather than at a call site.
"""

import pytest
from pydantic import ValidationError

from app.models.channel_group import ChannelGroup, ChannelGroupCreate
from app.services.channel_group_matching import (
    ChannelIdentity,
    find_matches,
    group_name,
    normalise,
    pick_primary,
    strip_noise,
)

# The real channel set, which is what these rules have to survive.
GEO_YT = ChannelIdentity("officialgeoranking", "Geo Ranking", "youtube", "@officialgeoranking")
GEO_IG = ChannelIdentity("17841434639350412", "Geo Ranking", "instagram", "@geo_ranking")
ASMR_YT = ChannelIdentity("physicsasmr_official", "Physics ASMR", "youtube", "@physicsasmr_official")
ASMR_IG = ChannelIdentity("physics_asmr", "Physics ASMR", "instagram", "@physicsasmr")
SCROLL_YT = ChannelIdentity("Scroll  and tell", "Scroll and Tell", "youtube", "@scrollandtell-z6m")
SCROLL_IG = ChannelIdentity("scroll and tell", "AI How Things Work", "instagram", "@ai_howthingswork")
ANIME = ChannelIdentity("tech_seekho", "Anime stories", "instagram", "@storyanime.e")
BLACKBOX = ChannelIdentity("The Blackbox", "The Blackbox", "youtube", None)
DREAM = ChannelIdentity("DreamScenicAi", "Dream Scenic Ai", "instagram", "@dreamscenicai")

ALL = [GEO_YT, GEO_IG, ASMR_YT, ASMR_IG, SCROLL_YT, SCROLL_IG, ANIME, BLACKBOX, DREAM]


def grouped_ids(matches):
    return {frozenset(m.channel_ids) for m in matches}


# ---------------------------------------------------------------- matching


def test_the_real_channel_set_produces_exactly_the_right_pairs():
    matches = find_matches(ALL, {("officialgeoranking", "17841434639350412"): 50})
    assert grouped_ids(matches) == {
        frozenset({"officialgeoranking", "17841434639350412"}),
        frozenset({"physicsasmr_official", "physics_asmr"}),
        frozenset({"Scroll  and tell", "scroll and tell"}),
    }


def test_unrelated_channels_are_never_grouped():
    """The three singletons must stay single — a false link publishes to the wrong place."""
    matches = find_matches([ANIME, BLACKBOX, DREAM])
    assert matches == []


def test_repost_history_outranks_a_name_match():
    """Someone copying videos by hand is a stated fact, not an inference."""
    by_name = find_matches([GEO_YT, GEO_IG])[0]
    by_repost = find_matches([GEO_YT, GEO_IG], {("officialgeoranking", "17841434639350412"): 50})[0]
    assert by_repost.confidence > by_name.confidence
    assert "50 videos" in by_repost.reason


def test_channels_with_different_names_still_match_on_their_ids():
    """Scroll and Tell / AI How Things Work differ in every way but the id."""
    matches = find_matches([SCROLL_YT, SCROLL_IG])
    assert grouped_ids(matches) == {frozenset({"Scroll  and tell", "scroll and tell"})}


def test_a_repost_link_groups_channels_that_look_nothing_alike():
    matches = find_matches([BLACKBOX, DREAM], {("The Blackbox", "DreamScenicAi"): 3})
    assert grouped_ids(matches) == {frozenset({"The Blackbox", "DreamScenicAi"})}


def test_a_chain_of_matches_becomes_one_group_not_two():
    """A brand on three platforms must not come back as overlapping pairs."""
    tiktok = ChannelIdentity("georanking_tt", "Geo Ranking", "tiktok", "@georanking")
    matches = find_matches([GEO_YT, GEO_IG, tiktok])
    assert grouped_ids(matches) == {frozenset({"officialgeoranking", "17841434639350412", "georanking_tt"})}


def test_the_result_does_not_depend_on_input_order():
    a = find_matches(ALL, {("officialgeoranking", "17841434639350412"): 50})
    b = find_matches(list(reversed(ALL)), {("officialgeoranking", "17841434639350412"): 50})
    assert grouped_ids(a) == grouped_ids(b)
    assert [m.channel_ids for m in a] == [m.channel_ids for m in b]


def test_short_tokens_are_not_evidence():
    """Two channels both called 'AI' are not the same brand."""
    assert find_matches([ChannelIdentity("x", "AI", "youtube"), ChannelIdentity("y", "AI", "instagram")]) == []


# ---------------------------------------------------------------- naming


def test_youtube_leads_a_group():
    """Long-form originates there, and every existing repost runs YouTube to Instagram."""
    assert pick_primary([GEO_IG, GEO_YT]) == "officialgeoranking"
    assert pick_primary([GEO_YT, GEO_IG]) == "officialgeoranking"


def test_a_group_takes_the_name_its_members_agree_on():
    assert group_name([GEO_YT, GEO_IG]) == "Geo Ranking"
    # Disagreeing members still yield a stable answer rather than a coin toss.
    assert group_name([SCROLL_YT, SCROLL_IG]) == group_name([SCROLL_IG, SCROLL_YT])


def test_normalising_ignores_case_spacing_and_punctuation():
    assert normalise("Scroll  and tell") == normalise("scroll and tell") == "scrollandtell"
    assert normalise("@geo_ranking") == "georanking"
    assert normalise(None) == ""


def test_platform_decoration_is_stripped():
    assert strip_noise(normalise("@physicsasmr_official")) == normalise("@physicsasmr")


# ---------------------------------------------------------------- group rules


def test_a_primary_outside_its_own_group_is_rejected():
    """It would drop out of its own expansion, silently."""
    with pytest.raises(ValidationError, match="primary_channel_id"):
        ChannelGroup(group_id="g", name="n", primary_channel_id="ghost", channel_ids=["a", "b"])


def test_a_repeated_channel_is_rejected():
    with pytest.raises(ValidationError, match="must not repeat"):
        ChannelGroup(group_id="g", name="n", primary_channel_id="a", channel_ids=["a", "a"])


def test_a_group_of_one_is_rejected_on_create():
    """There is nothing to expand to, so it would be a label pretending to be a link."""
    with pytest.raises(ValidationError):
        ChannelGroupCreate(name="n", primary_channel_id="a", channel_ids=["a"])


def test_others_excludes_the_channel_asked_about():
    group = ChannelGroup(group_id="g", name="n", primary_channel_id="a", channel_ids=["a", "b", "c"])
    assert group.others("a") == ["b", "c"]
    assert group.others("b") == ["a", "c"]
    # A channel outside the group gets everyone, which is why callers look the
    # group up by membership rather than passing an arbitrary id.
    assert group.others("z") == ["a", "b", "c"]
