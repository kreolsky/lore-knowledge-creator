"""Pydantic models: projects domain."""
from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
)


class SetProjectMember(BaseModel):
    user_id: str
    access_level: Literal["full", "commentator", "readonly"]


class TransferProjectOwner(BaseModel):
    """Admin-panel ownership transfer — the target becomes `projects.owner_id`.

    Deliberately NOT part of SetProjectMember.access_level: `owner` is not an
    access level (owner access is implied by owner_id), so widening the literal
    would leak into project_members rows and every AccessLevel consumer.
    """
    user_id: str


class InviteMember(BaseModel):
    """Self-service silent invite — email-keyed, role-bounded."""
    email: EmailStr
    access_level: Literal["full", "commentator", "readonly"]


class CancelInvite(BaseModel):
    """Query of DELETE /projects/{id}/invites.

    # WHY: extra="forbid" — a stale client still sending `document_id` gets a 422
    # instead of silently cancelling the PROJECT invite for that email.
    """
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1)


class PatchMember(BaseModel):
    access_level: Literal["full", "commentator", "readonly"]


class CreateProject(BaseModel):
    name: str = Field(min_length=2, max_length=256)


class PatchProject(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=256)
    status: Literal["active", "paused", "done"] | None = None
    project_context: str | None = None
    is_public: bool | None = None
    voice_recording_doc_id: str | None = None
    ref_image_preview: bool | None = None
