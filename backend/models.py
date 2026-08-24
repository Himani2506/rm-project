from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StatusUpdate(BaseModel):
    status: Literal["active", "debarred"]


class BulkStatusUpdate(BaseModel):
    ids: list[int] = Field(min_length=1)
    status: Literal["active", "debarred"]


class LoginRequest(BaseModel):
    username: str
    password: str
