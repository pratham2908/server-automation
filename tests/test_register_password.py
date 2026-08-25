"""Unit cover for the app-level channel-registration password gate.

`_verify_register_password` is the security boundary for `POST /api/v1/channels/`
— it must fail closed when unconfigured and reject a wrong/missing password with
403 (never 401, which the frontend treats as an auth failure and logs out on).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from app.routers import channels


def _set_password(monkeypatch, value: str | None) -> None:
    monkeypatch.setattr(
        channels,
        "get_settings",
        lambda: SimpleNamespace(CHANNEL_REGISTER_PASSWORD=value),
    )


class TestRegisterPasswordGate:
    def test_unconfigured_disables_registration(self, monkeypatch):
        """No server password set → registration is off for everyone."""
        _set_password(monkeypatch, None)
        with pytest.raises(HTTPException) as exc:
            channels._verify_register_password("anything")
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    def test_empty_config_disables_registration(self, monkeypatch):
        _set_password(monkeypatch, "")
        with pytest.raises(HTTPException) as exc:
            channels._verify_register_password("")
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    def test_wrong_password_rejected(self, monkeypatch):
        _set_password(monkeypatch, "correct-horse")
        with pytest.raises(HTTPException) as exc:
            channels._verify_register_password("wrong")
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    def test_missing_password_rejected(self, monkeypatch):
        _set_password(monkeypatch, "correct-horse")
        with pytest.raises(HTTPException) as exc:
            channels._verify_register_password(None)
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    def test_correct_password_passes(self, monkeypatch):
        """The one path that must NOT raise."""
        _set_password(monkeypatch, "correct-horse")
        channels._verify_register_password("correct-horse")
