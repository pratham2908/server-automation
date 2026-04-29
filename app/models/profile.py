from datetime import datetime

from pydantic import BaseModel, Field

from app.timezone import now_ist


class ProfileCreate(BaseModel):
    email: str
    password: str
    name: str


class ProfileBase(BaseModel):
    id: str
    email: str
    name: str


class ProfileInDB(ProfileBase):
    password_hash: str
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)


class Token(BaseModel):
    access_token: str
    token_type: str
