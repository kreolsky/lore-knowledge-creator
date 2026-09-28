"""Pydantic models: auth domain (split out of the former models.py)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

# A non-empty string, whitespace-stripped and length-bounded (used by api-key labels).
StrippedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


# Instance roles. 'moderator' is a group manager over role='user' rows
# (users.moderator_id) — NOT an instance admin: is_instance_admin stays
# role == "admin" everywhere in the access cascade (access.py).
Role = Literal["admin", "moderator", "user"]

# The ONE home for the manager-role set — auth.require_user_manager_role and
# the login / /me capability flags all read it; a re-spelled ("admin", …)
# tuple at a call site is a future drift.
USER_MANAGER_ROLES: frozenset[str] = frozenset({"admin", "moderator"})


def capability_flags(role: str | None) -> dict:
    """Per-role capability flags answered to clients (login payload, /me) —
    the frontend gates UI on these flags, never on role string comparisons."""
    return {
        "is_admin": role == "admin",
        "can_manage_users": role in USER_MANAGER_ROLES,
    }


class CreateUserAdmin(BaseModel):
    name: str = Field(max_length=100)
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)
    role: Role = "user"
    # Admin-only on create; a moderator's create forces role='user' and
    # moderator_id=<self> regardless of the body (routes/users.py).
    moderator_id: str | None = None


class UpdateUserAdmin(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=6, max_length=200)
    # ADMIN_FIELDS — a moderator whose body touches these gets 403. Presence is
    # read via model_fields_set (an explicit null clears moderator_id), never
    # via `is not None`.
    role: Role | None = None
    moderator_id: str | None = None


class CreateInvite(BaseModel):
    """POST /api/invites body — group binding for an admin's mint. A moderator's
    mint ignores it and pins their own uid (routes/invites.py)."""
    moderator_id: str | None = None


class RegisterRequest(BaseModel):
    """POST /api/register/{token} body — same field contract as CreateUserAdmin."""
    name: str = Field(max_length=100)
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)


class UpdateUser(BaseModel):
    user_facts: str | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, v: str | None) -> str | None:
        if v is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(v)
            except ZoneInfoNotFoundError:
                raise ValueError(f"Invalid IANA timezone: {v}")
        return v


class UpdateProfileName(BaseModel):
    name: str = Field(max_length=100)


class UpdateProfileEmail(BaseModel):
    email: EmailStr
    current_password: str = Field(max_length=200)


class UpdateProfilePassword(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


class UpdateProfilePin(BaseModel):
    pin: str | None = None


class VerifyPinRequest(BaseModel):
    pin: str


class RenameApiKey(BaseModel):
    label: StrippedStr


class CreateApiKey(BaseModel):
    """Mint ONE key row for a document.

    capabilities: non-empty subset of {widget, agent}. widget = audio widget on
    this doc; agent = MCP/Tool-API sandboxed to this doc's subtree (document_id
    is the universal scope root — the issuing doc). auto_apply is meaningful
    only with the agent capability (the MCP gateway's per-key write ceiling,
    read as a CEILING by the effective-apply computation).
    """
    document_id: str
    label: str = ""
    capabilities: list[str] = Field(default=["widget"])
    auto_apply: bool = False
    # INVARIANT(security): a user-issued key never expires unless the caller explicitly asks
    # for a TTL. Why: the widget/mobile client stores its key once and has no
    # re-issue flow — a silent 90-day default turned working clients into
    # "invalid key" on prod. Harness-minted agent keys keep their own TTL default
    # (see AGENT_KEY_DEFAULT_TTL_DAYS) — that divergence is intentional.
    expires_in_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, v: list[str]) -> list[str]:
        allowed = {"widget", "agent"}
        if not v or not set(v) <= allowed or len(set(v)) != len(v):
            raise ValueError(
                "capabilities must be a non-empty subset of {'widget', 'agent'}",
            )
        return v


class ApiKeyResponse(BaseModel):
    key_id: str
    label: str
    document_id: str
    capabilities: list[str]
    auto_apply: bool = False
    created_at: str
    last_used_at: str | None
    expires_at: str | None = None
