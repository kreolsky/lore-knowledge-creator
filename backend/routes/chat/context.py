"""Context assembly for chat completions — system prompt, documents, references, retrieval."""
# SYSTEM: chat-context — LLM context assembly (system prompt, docs, refs, retrieval)
# ARCH: Context pins SCOPE, not bodies. A pinned document/reference contributes
# its title + id and a read_document steer; the body never rides the prompt — the
# agent fetches it with its read tools, inside dsh's own context budget. The ONE exception is image bytes:
# no read tool can return binary, so a pinned image reference still delivers its
# normalized variant via ContextResult.image_parts.
# ARCH: Context assembly order — document scope → reference scope (+ image bytes).
# ARCH: Context is strictly opt-in. Primary document/reference is only included when
# explicitly present in document_ids/reference_ids arrays. Nothing is injected implicitly.
# INVARIANT: context fetch failures surface as warnings — never silent.
# Why: silent context drops made the LLM answer about content it never received.

import asyncio
import base64
import logging
from dataclasses import dataclass, field

import driver.client
import settings
from chat_sessions.serialize import build_ref_map
from thumbnails import get_or_render_model_variant

from config import (
    STORAGE_PATH,
)
from db import fetch_one, get_db, get_descendant_ids
from models import CompletionRequest, is_ref_row
from routes.chat.serializers import _validate_image_url

logger = logging.getLogger(__name__)


def _resolve_context_doc(
    fetched: dict, doc_id: str, project_id: str | None, *, expect_ref: bool | None,
) -> tuple[dict | None, dict | None]:
    """Validate a fetched context document and return (doc, warning).

    doc is None when a warning is emitted (fetch failure / missing / dropped).
    expect_ref: None = no kind check, True = must be a reference, False = must be a doc.

    Single ladder replacing the four duplicated isinstance/None/deleted_at/project
    blocks (system prompt, primary, documents, references). Warning codes and
    detail strings are preserved exactly — tests and the frontend depend on them.
    """
    if isinstance(fetched, Exception):
        logger.warning("Failed to fetch context doc %s: %s", doc_id, fetched)
        return None, {"code": "context_fetch_failed", "id": doc_id, "detail": str(fetched)}
    if fetched is None:
        return None, {"code": "context_fetch_failed", "id": doc_id, "detail": "document not found"}
    if expect_ref is not None and is_ref_row(fetched) != expect_ref:
        return None, {"code": "context_doc_dropped", "id": doc_id, "detail": "not a reference" if expect_ref else "not a document"}
    if fetched.get("deleted_at") or fetched.get("project_id") != project_id:
        reason = "deleted" if fetched.get("deleted_at") else "wrong project"
        return None, {"code": "context_doc_dropped", "id": doc_id, "detail": reason}
    return fetched, None


@dataclass
class ContextResult:
    system_prefix: list[dict]
    sources: list[dict]
    warnings: list[dict] = field(default_factory=list)
    # WHY: context image
    # references cannot ride system_prompt (dsh renders images only from the
    # `prompt` field → harness.prompt). Their image_url data-URI parts (plus a
    # short label part each) accumulate here so prepare_agent_turn can merge them
    # into AgentTurnPlan.prompt. The text framing for the same ref still rides
    # system_prefix as a normal {role:"system"} message.
    image_parts: list[dict] = field(default_factory=list)
    # WHY (one gate read): the
    # DRIVER's vision reply for this turn's model, resolved once in
    # build_context and threaded here so the attachment strip
    # (prepare_agent_turn) and the drop warning (completions) never re-ask.
    vision_ok: bool = False


def _add_manual_source(result: ContextResult, doc: dict, doc_id: str, kind: str) -> None:
    """Record a pinned document/reference as a manual (non-retrieved) source row."""
    # INVARIANT: a manually-added id wins over any retrieval hit for the same id (dedup, no duplicate row).
    # Why: the user explicitly attached the whole document — it must show without the auto-found chain icon.
    if any(s["id"] == doc_id for s in result.sources):
        return
    result.sources.append({
        "kind": kind,
        "id": doc_id,
        "title": doc.get("title") or "Untitled",
        "heading": "",
        "snippet": "",  # manual context = pinned scope; the Sources panel renders title/heading only.
        "offset_start": 0,
        "offset_end": 0,
        "score": 0.0,
        "retrieved": False,
    })


async def _resolve_vision_ok(body: CompletionRequest, session: dict) -> bool:
    """The DRIVER's vision reply for this turn's model — the ONE capability read."""
    # ARCH: an image pinned in context must
    # only reach a vision-capable model — a text-only model rejects any image_url
    # part and fails the whole turn. Resolved ONCE per turn HERE off the DRIVER's
    # capability reply (agent_capability → the plugin's gateway /v1/models
    # resolution — the ONE source) and threaded on ContextResult.vision_ok so
    # every image gate — the
    # context refs below, the user-attachment strip in prepare_agent_turn, the
    # drop warning in completions — reads ONE reply. A DRIVER outage raises
    # (explicit, pre-stream); a GATEWAY the driver cannot read answers
    # vision=false → strip + warn (the preserved safe direction). Read through
    # the OWNING module's attribute (never a top-level `from driver.client
    # import agent_capability` — a frozen binding makes the test patch on
    # driver.client.agent_capability succeed while inert).
    cap_reply = await driver.client.agent_capability(
        body.model or session.get("model") or await settings.get("CHAT_MODEL")
    )
    return cap_reply.get("vision") is True


async def _resolve_agent_target(session: dict, project_id: str | None) -> str | None:
    """The document the agent operates on this turn; None on a note or targetless session.

    Resolved once, early, so it rides the single fetch + ref_map call.
    """
    # WHY: target_doc_id is trusted ONLY if it is a descendant of
    # document_id (a genuine child reference) — otherwise the session's
    # document_id is the real working scope. Why: a poisoned target_doc_id
    # (forwarded from an unrelated active session by the old "+ Add chat" path)
    # would otherwise leak as the agent target until the backfill migration runs.
    # Guards old chats + future drift.
    # WHY: only a NOTE session has no agent target (notes carry no target_doc_id
    # and never call the LLM). Every AI (non-note) session resolves its target here.
    if session.get("is_note"):
        return None
    tid = session.get("target_doc_id")
    if not tid:
        return None
    primary_did = session.get("document_id")
    if tid != primary_did and primary_did:
        descendant_ids = await get_descendant_ids(primary_did, project_id or "")
        return tid if tid in descendant_ids else primary_did
    return tid


async def _fetch_context_rows(fetch_ids: list[str]) -> dict[str, dict | Exception | None]:
    """Parallel I/O: fetch every pinned row concurrently (validation + titles +
    image file paths; never bodies). A failed fetch maps to its exception."""
    if not fetch_ids:
        return {}
    results = await asyncio.gather(
        *[fetch_one("documents", did) for did in fetch_ids],
        return_exceptions=True,
    )
    return dict(zip(fetch_ids, results))


async def _pin_row(
    result: ContextResult, row: dict | Exception | None, doc_id: str, project_id: str | None,
    *, expect_ref: bool | None, as_ref: bool | None,
) -> bool:
    """Pin one fetched row into the context, or record its warning.

    `as_ref=None` formats by the row's own kind (the primary document). Returns
    True only when the row was pinned as a reference.
    """
    doc, warning = _resolve_context_doc(row, doc_id, project_id, expect_ref=expect_ref)
    if warning:
        result.warnings.append(warning)
        return False
    if as_ref is None:
        as_ref = is_ref_row(doc)
    if as_ref:
        await _format_ref_doc(doc, doc_id, project_id, result.system_prefix, result.warnings,
                              result.image_parts, result.vision_ok)
        _add_manual_source(result, doc, doc_id, "reference")
    else:
        _format_doc(doc, doc_id, project_id, result.system_prefix, result.warnings)
        _add_manual_source(result, doc, doc_id, "document")
    return as_ref


# ARCH: no system-prompt injection happens in build_context. The persona enters the agent
# prompt via exactly ONE injection point —
# build_agent_system_prompt(selected_persona_id) — threaded from
# session.system_prompt_id in prepare_agent_turn. A second injection site
# would concatenate two persona docs into the agent path.
# WHY: sources record only documents explicitly pinned via context_ids.
# Why: a source row marks the doc as attached context (scope pinned, body
# fetched on demand); the agent target is read on demand via read_document
# when not in context_ids (only a title anchor is sent), so it has nothing
# attached to record. The agent target is
# NOT added as a source on its own: when it is not in context_ids only a title
# anchor ("# Current document" in completions_turn) is sent and the agent
# reads content on demand via read_document — nothing to render as attached
# context. When the target IS in context_ids, the context-doc/ref loop
# already records exactly one source row for it (opt-in, deduped).
# ARCH: no per-turn RAG. Search is the agent's on-demand `search_materials`
# tool (model decides, with a model-formed + history-normalized query) —
# nothing is injected unconditionally per turn. Explicit context assembly
# (open doc + its refs + user-added context_ids) is the whole
# automatic context surface.
async def build_context(body: CompletionRequest, project_id: str | None, session: dict) -> ContextResult:
    """Build system-context messages with explicit warning propagation.

    Two-phase approach:
      Phase 1 — parallel I/O: gather all document fetches concurrently.
      Phase 2 — sequential formatting: iterate results, apply guards/formatting,
                emit warnings for failures/drops.
    """
    result = ContextResult(system_prefix=[], sources=[], vision_ok=await _resolve_vision_ok(body, session))
    # ARCH: unified context_ids; split via single ref_map call for prompt formatting.
    context_ids = list(body.context_ids or [])
    primary_did = session.get("document_id")
    agent_target = await _resolve_agent_target(session, project_id)
    db_for_refmap = await get_db()
    ref_map = await build_ref_map(db_for_refmap, *context_ids, primary_did, agent_target) if (context_ids or primary_did or agent_target) else {}
    document_ids = [i for i in context_ids if not is_ref_row(ref_map.get(i) or {})]
    reference_ids = [i for i in context_ids if is_ref_row(ref_map.get(i) or {})]

    # WHY: Primary document/reference gets special formatting (empty-content
    # message, image handling) but is only included when explicitly present in
    # document_ids or reference_ids. Nothing is added to context implicitly.
    primary_pinned = bool(primary_did) and primary_did in context_ids
    head = [primary_did] if primary_pinned else []
    fetched = await _fetch_context_rows(list(dict.fromkeys([*head, *document_ids, *reference_ids])))

    primary_is_ref = False
    if primary_pinned:
        primary_is_ref = await _pin_row(result, fetched.get(primary_did), primary_did, project_id,
                                        expect_ref=None, as_ref=None)
    for doc_id in document_ids:
        if primary_did and not primary_is_ref and doc_id == primary_did:
            continue
        await _pin_row(result, fetched.get(doc_id), doc_id, project_id, expect_ref=None, as_ref=False)
    for ref_id in reference_ids:
        if primary_did and primary_is_ref and ref_id == primary_did:
            continue
        await _pin_row(result, fetched.get(ref_id), ref_id, project_id, expect_ref=True, as_ref=True)
    return result


def _scope_line(kind: str, title: str, doc_id: str) -> str:
    """The pinned-scope sentence shared by documents and text references.

    Scope, not bodies: the pinned id + title ride the prompt; the content is the
    agent's to fetch via read_document (which also serves the freshly-typed live
    text, so no live-merge is needed here).
    """
    return (
        f'The user pinned the {kind} "{title}" (id: {doc_id}) to this conversation. '
        "Its content is not in this prompt: read_document it before you answer "
        "about it, and again before you edit it."
    )


def _format_doc(doc: dict, doc_id: str, project_id: str | None,
                 system_prefix: list[dict], warnings: list[dict]) -> None:
    """Pin a regular document's scope into the system prefix (title + id only)."""
    system_prefix.append({"role": "system", "content": _scope_line(
        "document", doc.get("title") or "Untitled", doc_id)})


async def _format_ref_doc(ref: dict, ref_id: str, project_id: str | None,
                     system_prefix: list[dict], warnings: list[dict],
                     image_parts: list[dict] | None = None,
                     vision_ok: bool = True) -> None:
    """Pin a reference's scope into the system prefix; image refs deliver bytes.

    Scope, not bodies: the reference's TEXT never rides the prompt (the agent
    reads it on demand). The IMAGE bytes still ride `image_parts` — no read tool
    returns binary, so the prompt channel is the only delivery (the agreed image
    exception). `image_parts` is optional so direct callers without image
    delivery (and legacy tests) keep working; when None it is ignored.

    `vision_ok`: when False the active model cannot take images — the image part
    is dropped and a `image_not_delivered` warning is appended (no-silent-
    degradation). When True the NORMALIZED model variant is delivered (pixel-capped,
    PNG/JPEG only — never the raw upload, never WebP).
    """
    ref_title = ref.get("title") or "Untitled"
    media_type = ref.get("media_type", "markdown")
    scope = _scope_line("reference", ref_title, ref_id)
    sink = image_parts if image_parts is not None else None

    if media_type == "image" and ref.get("file_path"):
        file_full = STORAGE_PATH / ref["file_path"]
        max_image_mb = await settings.get("MAX_IMAGE_SIZE_MB")
        max_bytes = max_image_mb * 1024 * 1024
        if file_full.is_file() and file_full.stat().st_size <= max_bytes:
            if not vision_ok:
                # Non-vision model: do not deliver the image (would 400/hallucinate).
                warnings.append({"code": "image_not_delivered", "id": ref_id,
                                 "detail": "active model does not support image input"})
                system_prefix.append({"role": "system", "content": scope})
                return
            # Vision-capable: deliver the normalized variant (cached beside _thumb).
            src_mime = (ref.get("file_meta") or {}).get("mime_type", "image/png")
            result = await asyncio.to_thread(
                get_or_render_model_variant, project_id or "", ref_id, file_full, src_mime)
            if result is None:
                # WebP that could not be transcoded — drop + warn (never send verbatim WebP).
                logger.warning("Image ref %s undeliverable (transcode failed)", ref_id)
                warnings.append({"code": "image_not_delivered", "id": ref_id,
                                 "detail": "image format unsupported by model pipeline"})
                system_prefix.append({"role": "system", "content": scope})
                return
            data, out_mime = result
            data_uri = f"data:{out_mime};base64,{base64.b64encode(data).decode()}"
            _validate_image_url(data_uri)
            # The image IS delivered, so the framing says so — image-aware, never
            # the generic fallback, and never the text body (scope, not bodies).
            system_prefix.append({"role": "system", "content":
                f'The user is viewing an image reference "{ref_title}" '
                f"(id: {ref_id}). Describe and discuss the image."})
            if sink is not None:
                sink.append({
                    "type": "text",
                    "text": f'[Context — image reference "{ref_title}" (id: {ref_id})]',
                })
                sink.append({"type": "image_url", "image_url": {"url": data_uri}})
        elif file_full.is_file():
            logger.warning("Reference image too large for chat: %s (%d bytes, limit %d MB)", ref_id, file_full.stat().st_size, max_image_mb)
            system_prefix.append({"role": "system", "content": scope})
        else:
            logger.warning("Reference image file not found: %s", file_full)
            system_prefix.append({"role": "system", "content": scope})
    else:
        system_prefix.append({"role": "system", "content": scope})
