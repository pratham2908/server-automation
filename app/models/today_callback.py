"""What a source app POSTs to its one-time callback URL when today's video is settled.

The ready shape is deliberately the same as the ``/today`` 200 body
(``{"status": "ready", "video": {"id": ...}}``) so an app can post exactly what it
would have answered a poll with. Extra fields (``source``, ``formatId``, the rest
of ``video``) are ignored rather than refused, for the same reason.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CallbackVideo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # The app's own render id — the one its catalogue lists and ``/videos/{id}`` serves.
    id: str = Field(..., min_length=1, max_length=200)
    title: str | None = None


class TodayCallbackBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["ready", "failed"]
    video: CallbackVideo | None = None
    # Why it failed, for the daily email. Required in spirit, defaulted in practice.
    error: str | None = Field(None, max_length=2000)
    # The same explanation ``/today`` carries ("No format is scheduled today…").
    reason: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _ready_names_a_video(self) -> TodayCallbackBody:
        if self.status == "ready" and self.video is None:
            raise ValueError('a "ready" callback must name the video: {"video": {"id": "..."}}')
        return self


CallbackAction = Literal["importing", "polling", "failed_recorded", "slot_closed"]


class TodayCallbackReceipt(BaseModel):
    received: bool
    # What we did with it: importing it, going back to asking (we could not take
    # the video it named), recording the failure.
    action: CallbackAction
