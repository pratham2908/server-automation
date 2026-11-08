import pytest
from pydantic import ValidationError

from app.routers.channels import ChannelUpdate


def test_email_is_trimmed_and_lowercased():
    assert ChannelUpdate(email="  Official.Ranking3D@Gmail.com ").email == "official.ranking3d@gmail.com"


def test_empty_email_is_kept_so_it_can_clear():
    update = ChannelUpdate(email="")
    assert update.model_dump(exclude_none=True) == {"email": ""}


def test_missing_email_is_not_updated():
    assert ChannelUpdate(paused=True).model_dump(exclude_none=True) == {"paused": True}


@pytest.mark.parametrize("bad", ["nope", "a@b", "a b@c.com"])
def test_invalid_email_rejected(bad):
    with pytest.raises(ValidationError):
        ChannelUpdate(email=bad)
