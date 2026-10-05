"""Pydantic models: chat domain (split out of models.py)."""
from __future__ import annotations

from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    model_validator,
)

from models.tools import RegionRef


class SessionCreate(BaseModel):
    project_id: str
    document_id: str | None = None
    reference_id: str | None = None
    # WHY: None = backend resolves via Reference -> parent Document -> CHAT_MODEL fallback.  Why: None means 'inherit' (let the resolver pick) rather than pin a model; an explicit value pins it — same inherit-vs-pin contract as system_prompt_id.
    # "" is treated as a missing value too (resolver kicks in) -- falsy semantics.
    model: str | None = None
    system_prompt_id: str | None = None
    # ARCH: the per-session reasoning effort.
    # None = Default (no reasoning_effort on the wire, the provider's default
    # applies); an explicit
    # value pins it. NOT inherited like model/system_prompt_id — a donor row's
    # model may differ from the resolved one, and a stale level under a foreign
    # model is exactly the hazard the reset-on-model-change decision fights.
    # Stored only for non-note sessions (notes never call the LLM).
    reasoning_effort: str | None = None
    # WHY: when set and resolvable to caller's own non-deleted session in same project,
    # inheritance fields are taken from THAT session instead of "latest of scope".  Why: forking from a specific parent inherits its model/system_prompt rather than the default latest-of-scope walk; the own / non-deleted / same-project checks prevent a cross-user, cross-project, or tombstoned parent reference.
    parent_session_id: str | None = None
    # Note-chat semantics: is_note=true binds the session to an anchored selection
    # in document_id; anchors are stored in Unicode code points and converted to
    # UTF-16 only at the API boundary in client.ts.
    is_note: bool = False
    anchor_offset_start: int | None = Field(default=None, ge=0)
    anchor_offset_end: int | None = Field(default=None, ge=0)
    anchor_rel_start: str | None = None
    anchor_rel_end: str | None = None
    # ARCH: the agent-only create fields are
    # now UNCONDITIONAL for non-note sessions (every AI chat is an agent chat).
    # target_doc_id pins the agent to a specific document (the only document it
    # may propose edits to); it defaults to reference_id or document_id in
    # _check_invariants. agent_auto persists the "Full auto" dropdown choice
    # has_region marks a pinned-region agent session. None of these
    # are written for note sessions (the `if not is_note` guard in create_session).
    target_doc_id: str | None = None
    # ARCH (agent_auto persistence): persists the agent-role dropdown's
    # "Full auto" choice on creation. Default false (confirm mode is safer). Only
    # meaningful for agent sessions; serialized back by serialize_session.
    agent_auto: bool = False
    # ARCH: a pinned-region agent chat constrains the
    # agent to edit ONLY inside a frontend-tracked text span (has_region=true forces
    # confirm mode + a per-apply containment check). The RelativePosition pair is
    # frontend-owned (localStorage); the backend persists only this server-
    # authoritative flag so confirm-forcing survives cross-device reload.
    has_region: bool = False

    @model_validator(mode="after")
    def _check_invariants(self) -> "SessionCreate":
        # INVARIANT: at least one of document_id / reference_id must be provided  Why: a chat session needs a document context to scope against (the doc it's about, or one of its references); without either there is no anchor and no retrieval/inherit target.
        if not self.document_id and not self.reference_id:
            raise ValueError("Either document_id or reference_id is required")
        # INVARIANT: note sessions can be anchored (selection-bound) OR anchorless
        # (document-wide / pipeline-error). Both anchor offsets must be set together
        # or both omitted — never one without the other.  Why: anchorless notes are first-class (document-wide / pipeline-error), so the two offsets set together or both omit — a lone offset is a half-specified range.
        # Why: anchorless notes are a first-class feature (57b1366) used by the
        # "Add Note" button in NotesPanel; a previous fix (134c8cd) required
        # anchors unconditionally and broke anchorless creation with a 422.
        if self.is_note:
            start_set = self.anchor_offset_start is not None
            end_set = self.anchor_offset_end is not None
            if start_set != end_set:
                raise ValueError("anchor_offset_start and anchor_offset_end must be set together")
            if start_set and end_set:
                if self.anchor_offset_end < self.anchor_offset_start:
                    raise ValueError("anchor_offset_end must be >= anchor_offset_start")
                if not self.document_id:
                    raise ValueError("Note sessions must specify document_id (not reference_id)")
        # ARCH: there is no mode gate here — with one AI line every non-note
        # session is an agent session, so both rules key on the non-note axis:
        #
        # 1. target_doc_id defaulting: a non-note session without an explicit
        #    target_doc_id defaults it to reference_id or document_id. Without
        #    this the agent has no pinned document.
        # 2. has_region is incompatible with a note. The explicit
        #    `has_region + is_note` guard is the ONLY thing preventing a
        #    pinned-region flag landing on a note row.
        if not self.is_note and not self.target_doc_id:
            # Default target to whatever the session is opened against.
            self.target_doc_id = self.reference_id or self.document_id
        # INVARIANT: a pinned region only makes sense for an agent that edits
        # documents. Why: it forces confirm mode + a per-apply containment check,
        # neither of which applies to note sessions.
        if self.is_note and self.has_region:
            raise ValueError("has_region is incompatible with note sessions")
        return self


class SessionUpdate(BaseModel):
    title: str | None = None
    model: str | None = None
    system_prompt_id: str | None = None
    # ARCH (plan reasoning-effort-selector): PATCH-able like system_prompt_id
    # (model_fields_set semantics — explicit null clears). Null is ALWAYS legal
    # (Default, and the model-change reset rides it); a non-null value is
    # validated by update_session against the TARGET model's advertised list —
    # 400 outside it.
    reasoning_effort: str | None = None
    # ARCH: Unified context array (Spec §5). Backend stores as `context_document_ids` column;
    # the field accepts both doc and ref IDs in original insertion order.
    context_ids: list[str] | None = None
    # ARCH (agent_auto persistence): PATCH-able like model/system_prompt_id.
    # The surviving selector axis (confirm <-> auto-apply); the Ask/Agent `mode`
    # axis left the wire. agent_auto is forced
    # false on a note session by update_session (defense-in-depth).
    agent_auto: bool | None = None
    # ARCH: PATCH-able to clear the pin (unpin). The
    # pin itself is set at create; a PATCH only ever clears it (has_region=false) to
    # re-enable auto-apply and drop the containment constraint.
    has_region: bool | None = None
    # ARCH (plan chat-reparent-from-header): PATCH-able like system_prompt_id
    # (model_fields_set semantics — explicit null clears to project-level).
    # update_session guards the write: 400 for note sessions (anchored by a
    # note: link inside the owning document's content) and reference-scoped
    # sessions (the column holds the REFERENCE id); a non-null target requires
    # at least commentator access, else 403.
    document_id: str | None = None


class MessageCreate(BaseModel):
    parent_id: str | None = None
    role: Literal["user", "assistant", "system"] = "user"
    content: str = ""
    images: list[str] | None = None
    # Optional explicit author for note-chats (multiple users post into one session).
    # When NONE, falls back to session.user_id via resolve_message_author().
    author_id: str | None = None


class MessageUpdate(BaseModel):
    content: str = ""
    sources: list[dict] | None = None


class ChatMessage(BaseModel):
    # WHY: Typed model prevents arbitrary dicts from reaching the LLM API.
    role: Literal["system", "user", "assistant"]
    content: str
    images: list[str] | None = None


class CompletionRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = None
    parent_id: str | None = None
    images: list[str] | None = None
    # ARCH: Unified context array (Spec §5). Backend splits doc vs ref via ref_map for prompt formatting.
    context_ids: list[str] | None = None
    # ARCH: the selected persona is sourced ONLY from
    # session.system_prompt_id (threaded into build_agent_system_prompt by
    # prepare_agent_turn). There is no per-request persona override — the session
    # is the single source of truth for selection.
    # ARCH: search is now the agent's on-demand search_materials tool (model decides,
    # or user says "поищи в…"), with a model-formed query. The
    # per-turn forced semantic injection + the docs/refs toggle booleans were removed
    # — the `corpus` switch lives on the tool args instead.
    # ARCH: Primary document/reference must be explicitly included in context_ids
    # to be added to context. Nothing is injected implicitly.
    # ARCH: AS (auto-apply) request — the user asks
    # the Agent line to apply writes directly instead of proposing. The backend
    # grants it only for full-access users (see
    # agent.apply_policy.resolve_apply_mode). Per-turn + ephemeral.
    auto_apply: bool = False
    # ARCH: when the active session is pinned, the
    # frontend resolves its Yjs RelativePosition pair to a live region and attaches
    # it here per turn. `prepare_agent_turn` injects a "# Pinned fragment" prompt
    # hint from it; `resolve_apply_mode` forces confirm regardless of auto_apply.
    # The apply path re-resolves old_string against LIVE content — this text is a
    # prompt hint only, never trusted for enforcement (enforcement uses the apply
    # body's region).
    region: RegionRef | None = None
    # ARCH: the live document open in the
    # central panel right now, resolved per-turn at send time from
    # currentReference ?? currentDocument. Ephemeral — NEVER mutates the session
    # pin (document_id) or target_doc_id; surfaced to the agent as a prompt line
    # only (access-gated title), so "save this to the open document" resolves to
    # the doc actually on screen instead of the frozen session pin.
    open_doc_id: str | None = None


class AgentConfigCreate(BaseModel):
    document_id: str
    config_doc_id: str
    target_doc_id: str
    trigger_event: str = "transcription_complete"
    title_template: str | None = None
    model: str | None = None


class AgentConfigResponse(BaseModel):
    config_id: str
    document_id: str
    config_doc_id: str
    target_doc_id: str
    project_id: str
    trigger_event: str
    title_template: str | None
    model: str | None
    created_at: str


class AgentRunRequest(BaseModel):
    # ARCH: Backwards-compatible alias. Clients in the wild still POST `reference_id`;
    # new clients use `document_id`. The migration runner already maps refs->documents,
    # so either id is valid for the new shape. Drop the alias one release after the
    # frontend migration ships.
    document_id: str = Field(validation_alias=AliasChoices("document_id", "reference_id"))


# ─── Author resolution helper (single source of truth) ─────────────────────
# INVARIANT: every code path that needs a message's effective author calls this
# helper -- no inline `message.get("author_id") or session["user_id"]` repeats.  Why: centralizes the author_id-or-session-owner fallback so the inherit rule can't drift across call sites; an inline repeat would diverge when the rule changes.
def resolve_message_author(message: dict, session: dict) -> str:
    """Effective author of a message. Falls back to session owner when NONE."""
    return message.get("author_id") or session["user_id"]
