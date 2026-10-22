"""Pydantic models: projects domain."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

# The storable audience subjects: 'public' (every logged-in user), a moderator
# group 'mod:<uid>', an admin group 'group:<id>'. The model-access subjects
# 'hidden'/'tools' and arbitrary strings never get stored (model_access.py).
_AUDIENCE_SUBJECT_RE = re.compile(r"^(public|mod:\S+|group:\S+)$")


class Audience(BaseModel):
    """One row of a project's audiences list — a subject opened at a level.

    extra='forbid': an unknown key in a row is a malformed write (422), never a
    silently-dropped field; the DB-level element schema cannot refuse unknown
    keys (SurrealDB 2.x stores them), so this model is the refusing layer.
    """
    model_config = ConfigDict(extra="forbid")
    subject: str
    access_level: Literal["full", "commentator", "readonly"]

    @field_validator("subject")
    @classmethod
    def _subject_shape(cls, v: str) -> str:
        if not _AUDIENCE_SUBJECT_RE.match(v):
            raise ValueError(
                "subject must be 'public', 'mod:<id>' or 'group:<id>'"
            )
        return v


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
    description: str | None = Field(default=None, max_length=2000)


class PatchProject(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "paused", "done"] | None = None
    project_context: str | None = None
    # The FULL audiences list, sent whole; clearing is `[]`. An absent key means
    # "untouched"; an explicit null is a malformed write (422 below). Diff-vs-
    # stored authorization (admin-only 'public', own-subject for a non-admin
    # owner, live moderator / existing group for an admin) lives in
    # patch_project's _guard_audiences — the model only shapes the wire.
    audiences: list[Audience] | None = None
    voice_recording_doc_id: str | None = None
    ref_image_preview: bool | None = None

    @model_validator(mode="after")
    def _audiences_rules(self) -> "PatchProject":
        if "audiences" in self.model_fields_set and self.audiences is None:
            raise ValueError("audiences must be a list; send [] to clear")
        if self.audiences is not None:
            subjects = [row.subject for row in self.audiences]
            if len(subjects) != len(set(subjects)):
                raise ValueError("duplicate audience subject in one body")
        return self
