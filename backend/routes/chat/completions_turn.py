"""Agent-turn assembly for the chat completion stream (the agent line).

Pure PREP layer of the chat-completions system: builds the inputs to the
driver-owned turn (the `AgentTurnPlan` DTO) without touching the HTTP I/O,
which stays in `completions.py`. The public entry `prepare_agent_turn` (and
`AgentTurnPlan`) are re-exported from `completions`.
"""
import asyncio
import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

import settings
from agent.apply_policy import resolve_apply_mode
from agent_config import build_prompt_and_skill_docs

import access
import db
from db import fetch_one, parse_iso
from models import CompletionRequest, is_ref_row
from routes.chat.serializers import _validate_image_url

logger = logging.getLogger(__name__)


def _section_fingerprint(name: str, text: str) -> str:
    """`name:len:sha8` for one system-prompt section — the prompt-cache instrument.

    # WHY: a turn-varying byte in ANY appended section silently voids the
    # provider's prefix cache (the whole tail + tool schemas + history get
    # re-tokenized) and nothing else reports it. Per-section fingerprints
    # name the moving section directly — the `agent turn prompt sections` log
    # line is the only instrument.
    """
    return f"{name}:{len(text)}:{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def _working_doc_head(doc: dict, doc_id: str) -> str:
    """The section heading, the working-doc line and its worked link example."""
    is_ref = is_ref_row(doc)
    section = (
        "# Current document\n"
        f"- This chat is about: {doc.get('title') or 'Untitled'} "
        f"(id: {doc_id}, kind: {'reference' if is_ref else 'document'})\n"
    )
    # A worked link on live data: THIS document's real link and real transclusion,
    # written out. Not an example — a true statement about this turn, so there is
    # no placeholder id to mis-copy (the bootstrap's `[text](<id>)` slot is a real
    # collision: `<...>` is legal CommonMark, and a weak model that copies the slot
    # writes `(<uuid>)` verbatim, which fails SAFE_ID_RE and renders broken).
    # WHY: here and NOT in the bootstrap — the bootstrap is the cached prefix;
    # this section is rebuilt every turn anyway. Constant shape, emitted every
    # turn, for the same reason as the open-doc anchor below: a line that
    # appears/disappears between turns is itself a noise signal for a weak model.
    # WHY: the fallback label — a `[`/`]` in the live title would break the
    # example, so the label degrades to the fixed words `this document`.
    link_label = doc.get("title") or "Untitled"
    if "[" in link_label or "]" in link_label:
        link_label = "this document"
    link_dest = f"ref:{doc_id}" if is_ref else doc_id
    return section + (
        f"  As a link it is written [{link_label}]({link_dest}); "
        f"inlined into a page it is ![{link_label}]({link_dest}).\n"
    )


async def _target_line(session: dict, doc_id: str, project_id: str) -> str:
    """The agent's editable target — only when it differs from the working
    document AND is a genuine descendant of document_id. Guards against a
    poisoned target_doc_id leaking into the prompt as the agent's editable focus."""
    target_doc_id = session.get("target_doc_id")
    if not target_doc_id or target_doc_id == doc_id:
        return ""
    if target_doc_id not in await db.get_descendant_ids(doc_id, project_id):
        return ""
    target_doc = await fetch_one("documents", target_doc_id)
    if not target_doc or target_doc.get("deleted_at"):
        return ""
    target_kind = "reference" if is_ref_row(target_doc) else "document"
    return (
        "- When this request asks for a change, it lands here: "
        f"{target_doc.get('title') or 'Untitled'} "
        f"(id: {target_doc_id}, {target_kind})\n"
    )


async def _open_doc_lines(open_doc_id: str | None, doc_id: str, user) -> str:
    """Live open document (per-turn, client-supplied — the doc actually shown in
    the central panel right now)."""
    # ARCH: this is per-turn and ephemeral — it NEVER mutates document_id or
    # target_doc_id (the session pin is the stable scope by design). It is the
    # intended target when the user says "the open document".
    # WHY: emit the open-doc anchor on EVERY turn open_doc_id is present —
    # even when it equals the working doc (then a "same as Working in" line).
    # Why: a line that appears/disappears between turns is itself a noise signal
    # for a weak model; a constant-shaped ground-truth anchor overrides stale
    # references to a previously-open doc still sitting in the agent history tree
    # (a user decision).
    if not open_doc_id:
        return ""
    if open_doc_id == doc_id:
        return f"- Open on the user's screen right now: this same document (id: {open_doc_id}).\n"
    open_doc = await fetch_one("documents", open_doc_id)
    detail = f"(id: {open_doc_id})"
    # INVARIANT(security): the title is revealed only when get_document_access is
    # truthy (same-project + accessible); an inaccessible/cross-project id yields
    # an id-only line, never the title, and never a write. Why: no new leak
    # surface from a poisoned open_doc_id.
    accessible = bool(open_doc and not open_doc.get("deleted_at")
                      and await access.get_document_access(open_doc_id, user))
    if accessible:
        open_kind = "reference" if is_ref_row(open_doc) else "document"
        detail = f"{open_doc.get('title') or 'Untitled'} (id: {open_doc_id}, kind: {open_kind})"
    lines = (
        f"- Open on the user's screen right now: {detail}. When the user "
        "says \"the open document\", this is the one they mean.\n"
    )
    # Read-first instruction only for a doc the agent can actually reach —
    # steering a weak model to read_document on a deleted/inaccessible id
    # just burns a turn on a guaranteed refusal.
    if accessible:
        lines += (
            "  Its text is not in this prompt: read_document it before you "
            "answer about it, and again before you edit it.\n"
        )
    return lines


async def _build_current_document_section(
    session: dict, project_id: str, user, open_doc_id: str | None = None,
) -> str | None:
    """Build the '# Current document' prompt section for an agent turn.

    # ARCH: the working document is session.document_id (the parent/scope doc),
    # NEVER target_doc_id. target_doc_id is the agent's editable target and is
    # shown separately ONLY when it differs from document_id AND is a genuine
    # descendant of document_id (a child reference the agent may edit this turn).
    # Why: a poisoned target_doc_id (forwarded from an unrelated active session
    # by the old "+ Add chat" path) must never become the primary "current
    # document" the agent believes it is working in.
    #
    # Returns None when document_id is absent or the working doc is
    # missing/deleted (the section is omitted but the agent still runs).
    """
    doc_id = session.get("document_id")
    if not doc_id:
        return None
    doc = await fetch_one("documents", doc_id)
    if not doc or doc.get("deleted_at"):
        logger.warning("Agent turn: working doc %s missing/deleted", doc_id)
        return None
    return (
        _working_doc_head(doc, doc_id)
        + await _target_line(session, doc_id, project_id)
        + await _open_doc_lines(open_doc_id, doc_id, user)
    )


def _parse_utc_datetime(value) -> datetime | None:
    """Coerce a stored created_at (SDK datetime or ISO string) to aware UTC.

    Returns None on anything unparseable — a broken anchor date must degrade
    to "no anchor", never fail the turn."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = parse_iso(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _resolve_user_timezone(user: dict) -> tzinfo:
    """The turn user's timezone (users.timezone, IANA) — UTC on any failure.

    # WHY: a convenience must never 500 a turn — NULL/garbage/fetch failure
    # degrades to UTC with ONE warning, never raises. (The JWT does not
    # carry the timezone, so the row is read once per turn; a second read of
    # the same row behind _token_version_cache is the accepted cost.)
    """
    user_id = user.get("user_id") if isinstance(user, dict) else None
    tz_name: str | None = None
    try:
        row = await fetch_one("users", user_id) if user_id else None
        tz_name = (row or {}).get("timezone")
        if not tz_name:
            logger.warning(
                "agent turn stamps: no timezone for user %s; using UTC", user_id,
            )
            return timezone.utc
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — garbage tz name or fetch failure
        logger.warning(
            "agent turn stamps: timezone resolve failed for user %s (%r); using UTC",
            user_id, tz_name,
        )
        return timezone.utc


def _turn_time_stamps(
    session: dict, body_parent_id_is_none: bool, user_tz: tzinfo,
) -> list[str]:
    """Time-stamp lines for the turn's `lore-time` context message — session
    anchor first (ROOT turns only), send stamp last. Pure: same inputs → same bytes, so a root
    fork re-sends a byte-identical anchor.

    # ARCH: time rides the HISTORY zone (the driver's append-only session
    # tree), never the system prompt — the prompt base is the cross-session
    # cache prefix and the tail is append-only, so a date there busts or voids
    # the shared prefix. Written once at send, never re-rendered → zero cache
    # impact (~15 tokens/turn).
    # WHY the anchor renders from chat_sessions.created_at, never turn time:
    # a session's first turn must re-send identical anchor bytes, and only
    # the session row is stable. Root condition: a genuine first turn (the
    # client's null tail hint, no continuation) appends a root entry
    # (anchor); a compaction continuation appends at the checkpoint and gets
    # NO anchor.
    """
    stamps: list[str] = []
    if body_parent_id_is_none and not session.get("compacted_from"):
        created = _parse_utc_datetime(session.get("created_at"))
        if created is not None:
            stamps.append(
                f"[chat started {created.astimezone(user_tz).isoformat(timespec='seconds')}]"
            )
    stamps.append(f"[sent {datetime.now(user_tz).isoformat(timespec='seconds')}]")
    return stamps


async def _build_last_user_prompt(
    body_messages: list, vision_ok: bool = True, user_msg_id: str | None = None,
) -> str | list:
    """Build the LAST user turn's content in its multimodal shape — RAW user
    text, plus only the attached files' workspace paths (_prompt_text).

    The full history is never replayed to dsh (history is canonical in the agent
    session tree). Only the last user message travels as `prompt` —
    a plain string, or a ContentPart[] (text + image_url parts) when images are
    attached. Returns "" for an empty body.

    Attachments are normalized (pixel-capped, PNG/JPEG only — never WebP) before
    delivery. When `vision_ok` is False (a
    non-vision model) image parts are stripped entirely; the caller surfaces a
    warning so the drop is never silent.

    On image turns the text part ends with the `[attached images: …]` marker
    naming the send's message id (`user_msg_id` — the id the persisted user row
    takes): the address `import_file(message_id, attachment_index)` resolves.
    Emitted also when `vision_ok` is False (the image is stripped from the
    prompt but still importable from the stored row), and NEVER written to the
    stored row (its `content` stays what the person typed).

    # WHY no time stamps here: they ride `AgentTurnPlan.time_stamps` — see
    # ARCH in harness-driver/plugin/src/time-stamps.ts.
    """
    if not body_messages:
        return ""
    last = body_messages[-1]
    n_images = len(last.images or [])
    if last.images and vision_ok:
        return await _multimodal_parts(last, user_msg_id=user_msg_id)
    return _prompt_text(last, message_id=user_msg_id, n_images=n_images)


def _human_size(size: int) -> str:
    """Bytes as the model reads them best: `812 B`, `4.2 KB`, `37.5 MB`."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _prompt_text(last, message_id: str | None = None, n_images: int = 0) -> str:
    """The last user turn's text part: the raw text, then the attached files'
    workspace paths when the person dropped any, then — on image turns — the
    one-line address marker that lets the agent save an attached image
    (`import_file(message_id, attachment_index)`, resolved from the stored row).

    # WHY here and not in the stored row: `user_text` (the bubble) stays what the
    # person typed; only the model needs the paths/marker, and it needs them on
    # the turn they were sent with — the files are working material for THAT
    # request, and the image address is importable material of the SAME request.
    # WHY the marker only with a message_id: the id is the send's
    # `user_msg_id` (the id the persisted row takes); without one (older
    # callers, no-id stubs) the text is byte-identical to the pre-marker
    # prompt. The count is the ATTACHED count — import reads the stored images
    # array, so an image normalization later drops from vision stays counted
    # (and addressable).
    """
    # WHY getattr: bare SimpleNamespace test stubs of a message predate `files`
    # (same defence as the vision_ok getattr below); ChatMessage always has it.
    files = getattr(last, "files", None)
    text = last.content
    if files:
        lines = [f"- {f.path} ({f.name}, {f.mime}, {_human_size(f.size)})" for f in files]
        block = "Attached files (in your sandbox workspace):\n" + "\n".join(lines)
        text = f"{text}\n\n{block}" if text else block
    if n_images and message_id:
        marker = (
            f"[attached images: message_id={message_id}, count={n_images} — "
            "save one with import_file(message_id, attachment_index)]"
        )
        text = f"{text}\n\n{marker}" if text else marker
    return text


async def _multimodal_parts(last, user_msg_id: str | None = None) -> list[dict]:
    """The multimodal ContentPart[] for the last user turn: raw text part
    first, normalized image parts after (attachments stay last).

    # INVARIANT: normalization runs in a thread, never inline on the event loop.
    # Why: _normalize_attachment is a full Pillow decode→LANCZOS→encode — measured
    # 242ms at 1600x1200, 2.7s at 4000x3000, and it runs on EVERY turn (attachment
    # variants are not cached). Inline it froze WS collab, presence and all other
    # HTTP for that whole time.
    """
    parts: list[dict] = [
        {"type": "text", "text": _prompt_text(last, message_id=user_msg_id,
                                              n_images=len(last.images))}
    ]
    n_imgs = 0
    # Settings resolve BEFORE the thread hop — the thread has no loop to await on
    # (SYSTEM: instance-settings).
    caps = await settings.get_all(["MODEL_IMAGE_MAX_PIXELS", "MODEL_IMAGE_JPEG_QUALITY"])
    normalized = await asyncio.to_thread(
        lambda: [
            _normalize_attachment(
                i,
                max_pixels=caps["MODEL_IMAGE_MAX_PIXELS"],
                jpeg_quality=caps["MODEL_IMAGE_JPEG_QUALITY"],
            )
            for i in last.images
        ]
    )
    for norm in normalized:
        if norm is None:
            continue  # WebP untranscodable — dropped
        _validate_image_url(norm)
        parts.append({"type": "image_url", "image_url": {"url": norm}})
        n_imgs += 1
    logger.info(
        "_build_last_user_prompt: multimodal prompt with %d image(s), total parts=%d",
        n_imgs, len(parts),
    )
    return parts


# data:image/<sub>;base64,<payload> — attachment images arrive as these data URIs.
_ATTACHMENT_DATA_URI_RE = re.compile(r"^data:(image/[a-z+]+);base64,(.+)$", re.IGNORECASE)


def _normalize_attachment(
    data_uri: str, *, max_pixels: int, jpeg_quality: int,
) -> str | None:
    """Downscale a data-URI image attachment for model delivery.

    Returns a normalized data URI (PNG/JPEG only — never WebP), the original
    data URI on a non-fatal Pillow failure (corrupt but safe-format → pass through),
    or None when the source is WebP and could not be transcoded (caller drops + warns).

    `max_pixels`/`jpeg_quality` arrive RESOLVED from the caller: this runs inside
    asyncio.to_thread (no loop to await on), so the settings read happens before
    the thread hop.
    """
    from thumbnails import downscale_bytes

    m = _ATTACHMENT_DATA_URI_RE.match(data_uri)
    if not m:
        return data_uri  # non-data-URI (HTTPS etc.) — validated upstream, pass through
    mime, b64 = m.group(1), m.group(2)
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return data_uri
    result = downscale_bytes(raw, mime, max_pixels=max_pixels, jpeg_quality=jpeg_quality)
    if result is None:
        return None
    out, out_mime = result
    return f"data:{out_mime};base64,{base64.b64encode(out).decode()}"


@dataclass
class AgentTurnPlan:
    """Inputs to the turn hand-off, assembled by `prepare_agent_turn`.
    Carrying them as a DTO makes the Agent-turn setup testable without driving a
    live turn."""
    system_prompt: str
    apply_mode: str
    tools: list[dict]
    # The turn contract carries NO messages[] —
    # only the last user turn (`prompt`, multimodal shape). History is canonical
    # in the agent session tree, NOT replayed.
    prompt: str | list = ""
    # The turn's time-stamp lines (root anchor + send stamp — `_turn_time_stamps`),
    # never inside `prompt`: the plugin emits them as their own `lore-time`
    # context message — see ARCH in harness-driver/plugin/src/time-stamps.ts.
    time_stamps: list[str] = field(default_factory=list)
    # The RAW skills wire for the agent payload — threaded from the SAME
    # config-subtree load that built `system_prompt`, so the turn does ONE walk
    # (not two) and the wire stays byte-stable with the prompt. The plugin
    # parses it; the payload
    # field is named `skills`. A real dict, not a tuple
    # default: a mutable-dict field must not alias a shared tuple.
    skill_docs: dict = field(default_factory=dict)
    # Skill names or masks the session's persona preloads (its frontmatter
    # `skills`): the plugin activates each one that resolves in the catalog and
    # injects its body as a `lore-preload` context message — see ARCH in
    # harness-driver/plugin/src/preload.ts. Never a system-prompt edit. The
    # default `main-*` mask is appended here by the caller
    # (completions_harness._preload_default_skills) — ONE owner.
    preload_skills: list[str] = field(default_factory=list)
    # The persona's skill visibility (its frontmatter `allow` / `deny`): names
    # or prefix masks, sent RAW — the plugin expands them against the catalog
    # it resolves (persona_settings.py ARCH). Empty `allow` = everything.
    skill_allow: list[str] = field(default_factory=list)
    skill_deny: list[str] = field(default_factory=list)
    # False = the default `main-*` skills are not preloaded for this persona;
    # they stay visible and the agent loads them when a request needs them.
    preload_defaults: bool = True
    # The persona's chat-memory token budget (0 = memory off) — the input the
    # memory block is built from.
    chat_memory_budget: int = 0
    # The persona's auto-recall token budget (0 = off) — the input the recall
    # block is built from; it runs only while memory is on.
    chat_recall_budget: int = 0
    # The persona's memory-block order ("recent" | "document").
    chat_memory_order: str = "recent"
    # Why the persona's memory is off although asked for (PersonaSettings.memory_error).
    memory_error: str | None = None
    # A bad `chat_memory_order` — memory stays on with "recent"; relayed as a turn warning.
    memory_order_warning: str | None = None
    # Why the persona's auto-recall is off although asked for
    # (PersonaSettings.recall_error); relayed as a turn warning.
    recall_warning: str | None = None
    # The memory record to store on the chat row once the send is accepted
    # (chat_sessions.memory); None = nothing to record this turn.
    memory_record: dict | None = None
    # The recall record for the same moment (chat_sessions.recall); written
    # only with hits.
    recall_record: dict | None = None
    # The persona's chat-memory block ("" = none this turn); the plugin injects
    # it as a `lore-memory` context message under the same log-scan rule.
    memory_block: str = ""
    # The persona's auto-recall block ("" = none this turn); the plugin injects
    # it as a `lore-recall` context message beside the memory one.
    recall_block: str = ""


# ARCH: turn-varying blocks are APPENDED to the base, never prepended — the
# base (bootstrap+persona+rules) is the prompt-cache prefix, and a
# turn-varying block leading it re-tokenizes the whole stable tier whenever
# it changes. This is the backend half of
# the prompt-cache closure: the driver registers Lore's prompt as its
# COMPLETE section and contributes nothing of its own (the comment above
# `agentCtx.systemPrompt.section` in harness-driver/plugin/src/index.ts), and
# a prepend here would undo that work from the other side. A regression shows
# up in this module's own `agent turn prompt sections` log — per-section
# len+digest — the only instrument: a turn-varying byte in any appended
# section silently voids the provider's prefix cache, and nothing else
# reports it.
# Measured against the gateway: the served layout is [tools][system][history],
# so a moved byte here re-tokenizes the rest of the system prompt AND the whole
# history behind it — the tool schemas ahead of it survive.
async def _assemble_system_prompt(
    base: str, *, session: dict, body: CompletionRequest, project_id: str, user: dict,
) -> str:
    """Append the turn-varying sections (current document) to the cached base
    and log their fingerprints."""
    # ARCH: the working document is session.document_id (the parent/scope doc),
    # NEVER target_doc_id — see _build_current_document_section.
    current_doc_section = await _build_current_document_section(
        session, project_id, user, open_doc_id=body.open_doc_id,
    )
    appended = [
        ("current_doc", current_doc_section),
    ]
    system_prompt = base
    sections = [_section_fingerprint("base", base)]
    for name, text in appended:
        if text:
            system_prompt += "\n\n" + text
            sections.append(_section_fingerprint(name, text))
    logger.info(
        "agent turn prompt sections: total=%d %s",
        len(system_prompt), " ".join(sections),
    )
    return system_prompt


async def _build_turn_prompt(*, body: CompletionRequest, vision_ok: bool) -> str | list:
    """The last user turn as `prompt`: vision-gated — the RAW user content
    (the time stamps ride `time_stamps`, not the text).

    The turn contract sends only the LAST user turn as `prompt`
    (multimodal shape), never the full history. History is canonical in the agent
    session tree. Falls back to "" on an empty body.
    """
    # Gate attachments on the driver's vision reply (_resolve_vision_ok in
    # completions — the ONE capability read per turn).
    # The send's user row id — the address the image marker names. getattr
    # defends bare SimpleNamespace test stubs without the field.
    user_msg_id = getattr(body, "user_msg_id", None)
    return await _build_last_user_prompt(
        body.messages, vision_ok=vision_ok,
        user_msg_id=str(user_msg_id) if user_msg_id else None,
    )


async def _resolve_turn_time_stamps(
    *, session: dict, body: CompletionRequest, user: dict,
) -> list[str]:
    """The turn's time ground: send stamp every turn, session anchor on root
    turns (the `_turn_time_stamps` contract — pure, same inputs → same bytes)."""
    user_tz = await _resolve_user_timezone(user)
    # (getattr defends bare SimpleNamespace test stubs without parent_id.)
    return _turn_time_stamps(
        session,
        body_parent_id_is_none=getattr(body, "parent_id", None) is None,
        user_tz=user_tz,
    )


async def prepare_agent_turn(
    *,
    session: dict,
    body: CompletionRequest,
    vision_ok: bool,
    project_id: str,
    user: dict,
    toolset: list[dict],
    capability_is_mutating: bool,
) -> AgentTurnPlan:
    """Assemble the Agent-turn inputs: the agent system prompt (base +
    current-document section), the resolved apply-mode, the
    OpenAI-format conversation, and the toolset. Kept out of the turn
    transport so the route is a thin `prepare → hand off` pair and the prompt
    assembly is unit-testable without a live driver."""
    base_prompt, skill_docs, persona = await build_prompt_and_skill_docs(
        project_id, selected_persona_id=session.get("system_prompt_id"),
    )
    # The payload's skills field is the RAW lookup from the SAME load (no second
    # subtree walk): {project, instance, shipped, instance_tombstones}. Parsing,
    # the overlay, the admin off-switch and the served-toolset gate all run
    # PLUGIN-side — Python parses nothing.
    system_prompt = await _assemble_system_prompt(
        base_prompt, session=session, body=body, project_id=project_id, user=user,
    )
    apply_mode = resolve_apply_mode(
        ui_preference="auto" if body.auto_apply else "confirm",
        is_system=False,
    ).mode
    return AgentTurnPlan(
        system_prompt=system_prompt,
        apply_mode=apply_mode,
        tools=toolset,
        prompt=await _build_turn_prompt(body=body, vision_ok=vision_ok),
        time_stamps=await _resolve_turn_time_stamps(session=session, body=body, user=user),
        skill_docs=skill_docs,
        preload_skills=list(persona.skills),
        skill_allow=list(persona.skill_allow),
        skill_deny=list(persona.skill_deny),
        preload_defaults=persona.preload_defaults,
        chat_memory_budget=persona.chat_memory_budget,
        chat_memory_order=persona.chat_memory_order,
        chat_recall_budget=persona.chat_recall_budget,
        memory_error=persona.memory_error,
        memory_order_warning=next(
            (w for w in persona.warnings if w.startswith("chat_memory_order")), None),
        recall_warning=persona.recall_error,
    )
