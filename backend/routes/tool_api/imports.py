"""Tool-API import tool — import_file (md · docx · text · binary → document or reference).

# SYSTEM: tool-api — import surface (import_file).
# ARCH:
# ONE thin wrapper around ONE shared `_import_document` core, discriminated by
# `is_reference` (exactly as create_document already discriminates doc vs reference).
# The core funnels every supported input through the EXISTING import pipeline
# (is_text_bytes gate → conditional normalize_markdown → docx converter →
# extract_and_replace_images) + the widget's binary save path, then the
# single-source-of-truth record factory (create_document_via_collab /
# create_reference_via_collab / save_upload). No new business logic.
#
# Auto-only on BOTH surfaces (no ProposalKind): the bytes are user-supplied data,
# not a text edit to confirm — a multi-MB blob has no place in a proposal, and the
# agent gets the result immediately. MCP force-auto + the read-only key gate live
# in dispatch.py, so this
# handler applies directly (no _resolve_apply_or_force / no confirm branch).
#
# Plain `content` (authored text) is NOT a
# tool input — authored text goes to create_document. The `content` parameter on
# `_import_document` is retained because `_resolve_sandbox_path` produces it for a
# text/.md file read from the workspace (a FILE import, not authored text).
#
# Import DAG: imports _router + _common only; reaches tool_api_surface (record
# factories) + files_util (save_upload) + files (extract_and_replace_images) +
# docx_convert lazily inside the handlers. No domain imports another domain.
"""
import asyncio
import base64
import logging
import os
import re

import settings
from agent.context import get_agent_context
from api_key_auth import agent_author_name

# docx_convert is a stateless leaf (imports only config + httpx) — importing it at
# module level matches routes.files and keeps `post_docx_to_converter` patchable by
# its used-here name (tests patch routes.tool_api.imports.post_docx_to_converter).
from docx_convert import WEB_CONVERTER_TIMEOUT, post_docx_to_converter
from fastapi import Depends, HTTPException
from files_util import (
    ARCHIVE_MIME,
    DOCX_MIME,
    IMAGE_EXT_BY_MIME,
    detect_media_type,
    is_text_bytes,
    mime_for_filename,
    normalize_audio_upload,
    save_upload,
    validate_magic,
)
from markdown_normalize import normalize_markdown

from access import get_project_access
from models import ToolImportFile, ToolReprocessReference, is_ref_row
from routes.tool_api._common import (
    _refuse_unconfirmable,
)
from routes.tool_api_telemetry import track_agent_tool

logger = logging.getLogger(__name__)


def _default_title(filename: str) -> str:
    """Title fallback = the filename stem (matches upload-markdown)."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    return stem.strip() or "Untitled"


# The 400 detail every "a binary is never a document" rejection shares (the .zip
# extension dispatch and the image/audio fallback below) — ONE literal so the two
# sites cannot drift. Names node_type as the fix (see the test pinning the wording).
_BINARY_NOT_A_DOCUMENT_DETAIL = (
    "Binary (image/audio/archive) is not a document; pass "
    "node_type: \"reference\" to upload it as a reference"
)




async def _sandbox_path_max_bytes(filename: str) -> int:
    """Pick the size cap for a sandbox_path read from the filename's media category.

    # WHY: the cap is decided from the EXTENSION
    # (extension → mime → media type → its existing cap), NOT from magic — magic
    # validation needs the bytes, and reading a 2 GB file to learn its type is the
    # OOM risk the stat-before-read cap exists to prevent. Reuses mime_for_filename
    # + the existing MAX_*_SIZE_MB settings; no new constants. The existing
    # post-decode caps in _import_document still apply (defense in depth).
    """
    caps = await settings.get_all([
        "MAX_DOCX_SIZE_MB", "MAX_ARCHIVE_SIZE_MB", "MAX_IMAGE_SIZE_MB",
        "MAX_AUDIO_SIZE_MB", "MAX_MARKDOWN_SIZE_MB",
    ])
    mime = mime_for_filename(filename)
    if mime == DOCX_MIME:
        return caps["MAX_DOCX_SIZE_MB"] * 1024 * 1024
    if mime == ARCHIVE_MIME:
        return caps["MAX_ARCHIVE_SIZE_MB"] * 1024 * 1024
    media = detect_media_type(mime)
    if media == "image":
        return caps["MAX_IMAGE_SIZE_MB"] * 1024 * 1024
    if media == "audio":
        return caps["MAX_AUDIO_SIZE_MB"] * 1024 * 1024
    # text / markdown / unknown-text → the markdown cap.
    return caps["MAX_MARKDOWN_SIZE_MB"] * 1024 * 1024


async def _decode_base64(content_base64: str) -> bytes:
    """Decode a (whitespace-tolerant) base64 string; 400 on malformed input.

    # ARCH (memory-DoS guard): the base64 is fully resident as a Python str (Pydantic
    # parsed it from the agent/MCP JSON args) and decode allocates a second ~0.75×
    # buffer, so cap the encoded length BEFORE allocating the decoded bytes — bigger
    # than every post-decode cap (docx/image/audio, read per-call so test
    # monkeypatches apply), accounting for the ~4/3 base64 expansion. The
    # format-specific caps still govern stored size. Note this cap is NOT what bounds
    # the MCP path: /mcp is not in main._UPLOAD_PATHS, so the 1MB _JSON_MAX_BYTES
    # middleware refuses such a body long before this guard is reached — which is why
    # anything larger (all audio) uses the signed-URL transport in
    # mcp_gateway/upload.py instead of this argument.
    """
    caps = await settings.get_all([
        "MAX_ARCHIVE_SIZE_MB", "MAX_DOCX_SIZE_MB", "MAX_AUDIO_SIZE_MB",
        "MAX_IMAGE_SIZE_MB",
    ])
    max_decoded_mb = max(caps.values())
    if len(content_base64) > max_decoded_mb * 1024 * 1024 * 4 // 3:
        raise HTTPException(status_code=413, detail="File too large")
    try:
        return base64.b64decode(re.sub(r"\s+", "", content_base64))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 content")


async def _resolve_attachment(
    ctx: dict, index: int, filename: str,
) -> tuple[str, str]:
    """Resolve `attachment_index` (an image attached to the current chat turn) to
    (content_base64, filename). In-chat-agent ONLY — `attachment_index` is never
    advertised over MCP and MCP sets no session_id.

    # ARCH: the attachment is
    # resolved here in the DURABLE backend, not in the dsh driver's transient prompt
    # state. Why: tsx-watch over the macOS Docker bind mount silently missed the
    # driver edit, breaking the feature with a confusing 422 — a resolution that
    # depends on the driver's live reload is too fragile. The backend resolves from
    # the persisted user message (messages.images via ctx["session_id"], set from
    # X-Agent-Session-Id) so a missed reload can't silently regress it. The resolved
    # bytes then flow through the EXISTING binary path (_create_binary_reference) —
    # no new business logic.

    # WHY (which message): the model only ever sees the LAST user turn's
    # images (_build_last_user_prompt), so "latest user message WITH images" is
    # exactly the model's attachment. Why the images-non-empty filter: a mid-turn
    # 2nd plain-text user message would otherwise shadow the image-bearing turn.
    # The user message is persisted before the agent turn fires, so `images` is
    # present here.
    """
    from db import get_db

    session_id = ctx.get("session_id")
    if not session_id:
        raise HTTPException(
            status_code=400,
            detail="attachment_index is only available in a chat session (in-chat agent)",
        )
    # INVARIANT(security): attachments are read only from a session the caller
    # OWNS. Why: the messages query below keys on the raw X-Agent-Session-Id
    # value — without this check a caller tagging another user's session id
    # silently imported THAT user's attached images (cross-user image read).
    # Refused with the same 400 the absent header gets: "not a resolvable chat
    # session of yours" is one condition with several spellings, and none may
    # leak what the other session holds.
    from agent.context import owned_session_row

    if await owned_session_row(ctx) is None:
        raise HTTPException(
            status_code=400,
            detail="attachment_index is only available in a chat session (in-chat agent)",
        )
    db = await get_db()
    rows = await db.query(
        # WHY select created_at: SurrealDB requires ORDER BY fields be in the
        # projection (same constraint as the messages-preview query).
        "SELECT images, created_at FROM messages "
        "WHERE chat_id = $sid AND role = 'user' AND deleted_at IS NONE "
        "AND array::len(images ?? []) > 0 "
        "ORDER BY created_at DESC LIMIT 1",
        {"sid": session_id},
    )
    images = (rows[0].get("images") if rows else None) or []
    if index < 0 or index >= len(images):
        raise HTTPException(
            status_code=400,
            detail=f"attachment_index {index} is out of range (0..{max(0, len(images) - 1)})",
        )
    data_uri = images[index]
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        raise HTTPException(status_code=400, detail="Attached image is not a valid data URI")
    header, sep, payload = data_uri.partition(",")
    if not sep or "base64" not in header:
        raise HTTPException(status_code=400, detail="Attached image is not a base64 data URI")
    mime = header[len("data:"):].split(";")[0] or "application/octet-stream"
    # Derive the extension from the data-URI mime (the source of truth on THIS path
    # — the bytes are persisted alongside their mime header). IMAGE_EXT_BY_MIME is
    # the single-source ext map and enforces the project's jpg-not-jpeg convention
    # (image/jpeg → "jpg"); the subtype fallback covers a mime with no ext entry.
    ext = IMAGE_EXT_BY_MIME.get(mime) or (mime.split("/")[1] if "/" in mime else "bin")
    # WHY: on the attachment_index path the extension ALWAYS comes from the
    # data-URI mime; a model-supplied `filename` contributes its STEM ONLY.
    # Why: the model cannot see the bytes, so its filename is always a GUESS, while
    # the backend ALREADY KNOWS the true mime (the persisted data-URI header). A
    # guess that won over the known truth failed magic validation ~always (telemetry:
    # import_file 3 ok / 5 error; the 3 ok were lucky ext guesses). This path has
    # regressed twice, so the rule is pinned here. The filename-as-stem rule does
    # NOT leak to content_base64 / sandbox_path, where the extension IS the only type
    # signal — the change stays inside _resolve_attachment.
    # Example: character_art.png + jpeg bytes → character_art.jpg.
    stem = os.path.splitext(os.path.basename((filename or "").strip()))[0]
    resolved_filename = f"{stem}.{ext}" if stem else f"attachment-{index}.{ext}"
    return payload, resolved_filename


def _categorise_upload_bytes(raw: bytes, filename: str) -> tuple[str | None, str | None]:
    """bytes → the import dispatcher's input form: (content, content_base64),
    exactly one populated.

    # ARCH: extracted from
    _resolve_sandbox_path, which inlined this categoriser for the sandbox byte
    channel — the widget upload route now shares it so BOTH external byte
    channels (agent workspace file, widget multipart) route a file to
    _import_document identically.

    # WHY branch on category rather than always returning base64:
    `content_base64` in _import_document is hardwired to mean binary/.docx (it
    never runs normalize_markdown). A text/.md file MUST go through `content` so
    the shared text gate (is_text_bytes) + the conditional normalize_markdown
    run — handing it through base64 would misroute it to the binary path and 400
    a .md as "binary is not a document". The category is read from the EXTENSION
    (not magic — magic needs the bytes, and reading a 2 GB file to learn its type
    is the OOM risk the stat-before-read cap exists to prevent).

    An extension that claims text but whose bytes are binary (is_text_bytes
    fails) is handed to base64 so _import_document rejects it with its own
    message rather than a confusing UTF-8 decode error.
    """
    mime = mime_for_filename(filename)
    if mime in (DOCX_MIME, ARCHIVE_MIME):
        # archive binary → the binary save path (content_base64). Never the text
        # path: is_text_bytes would happily accept a zip of text files as "text".
        return None, base64.b64encode(raw).decode()
    if detect_media_type(mime) is not None:
        # image / audio binary → the binary save path (content_base64).
        return None, base64.b64encode(raw).decode()
    # text / markdown → the text path so normalize_markdown + is_text_bytes run.
    text = is_text_bytes(raw)
    if text is not None:
        return text, None
    return None, base64.b64encode(raw).decode()


async def _resolve_sandbox_path(
    ctx: dict, sandbox_path: str, filename: str,
) -> tuple[str | None, str | None]:
    """Resolve `sandbox_path` (a file already in the workspace) to the right import
    form. Returns (content, content_base64) with exactly one set.

    # ARCH: the SFTP outbound form. The backend reads the
    # workspace file over SFTP — NOT exec, so the bytes bypass MAX_OUTPUT_CHARS and
    # never inflate 33% over base64-on-stdout. The per-form console gate rides on
    # read_workspace_file itself (it calls _require_console_key), so this resolution
    # refusing a non-console key IS the per-form gate — content_base64 still serves
    # those keys.
    """
    from routes.tool_api.sandbox.files import read_workspace_file

    max_bytes = await _sandbox_path_max_bytes(filename)
    raw = await read_workspace_file(ctx, sandbox_path, max_bytes=max_bytes)
    return _categorise_upload_bytes(raw, filename)


async def _convert_docx_sync(data: bytes, filename: str) -> str:
    """Synchronously convert a .docx (ZIP) to Markdown via the stateless converter.

    # WHY: safe in the web process under MAX_DOCX_SIZE_MB + WEB_CONVERTER_TIMEOUT
    # (proven by extract-text). MCP has no status-poll tool, so the agent gets the
    # result immediately — no async convert_docx_task path. Mirrors
    # routes.files.extract_text_for_insert's docx branch.
    """
    try:
        return await post_docx_to_converter(data, filename, timeout=WEB_CONVERTER_TIMEOUT)
    except (RuntimeError, asyncio.TimeoutError):
        logger.warning("upload docx conversion failed (filename=%s)", filename, exc_info=True)
        raise HTTPException(status_code=502, detail="Document conversion failed")


async def _extract_images(
    content: str, project_id: str, parent_id: str | None, *,
    created_by: str | None = None, created_by_name: str | None = None,
):
    """extract_and_replace_images wrapper — maps its ValueError to a clean 400.

    Images extracted during import attach to the SAME parent as the new entity
    (matches upload-markdown); no need to pre-generate the new doc/ref id.
    """
    from files_service import extract_and_replace_images

    try:
        return await extract_and_replace_images(
            content, project_id, parent_id,
            created_by=created_by, created_by_name=created_by_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _create_binary_reference(
    data: bytes,
    filename: str,
    title: str,
    document_id: str | None,
    project_id: str,
    user: dict,
    author_name: str | None = None,
) -> dict:
    """Binary image/audio/archive → reference, reusing the widget's save path (magic
    validation + thumbnail/transcription). Reference-only — a binary is never a
    document, so this is reached only via import_file(node_type="reference").
    Archives land as `media_type: "file"` (generic downloadable binary): no
    thumbnail, no transcription, processing_status None.

    `author_name` (S1): the DISPLAY-axis byline (agent key label at the surface);
    None keeps the user's name.

    # WHY: MIME is sniffed from the filename EXTENSION (the widget's
    # content_type equivalent), then validate_magic confirms the bytes — magic
    # alone cannot disambiguate RIFF (webp vs wav), so the extension carries the
    # type intent and magic rejects a mismatched payload.
    """
    mime = mime_for_filename(filename)
    media_type = detect_media_type(mime)
    if media_type is None:
        if mime == ARCHIVE_MIME:
            media_type = "file"
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported binary type: {mime or filename or 'unknown'}",
            )
    original_name = filename or f"upload.{mime.split('/')[-1]}"
    byline = author_name if author_name is not None else user.get("name")

    if media_type == "file":
        max_archive_mb = await settings.get("MAX_ARCHIVE_SIZE_MB")
        if len(data) > max_archive_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File too large (max {max_archive_mb}MB)")
        if not validate_magic(data, mime):
            raise HTTPException(status_code=400, detail="File content does not match declared type")
        ref_id, _result = await save_upload(
            data, mime, original_name, project_id, document_id,
            title=title, media_type="file", processing_status=None,
            created_by=user.get("user_id"), created_by_name=byline,
        )
        # save_upload derives nothing for "file" (no thumbnail, no transcription);
        # it emits reference_created like every media type.
        return {"status": "applied", "reference_id": ref_id, "doc_id": ref_id}

    if media_type == "audio":
        max_audio_mb = await settings.get("MAX_AUDIO_SIZE_MB")
        if len(data) > max_audio_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File too large (max {max_audio_mb}MB)")
        if not validate_magic(data, mime):
            raise HTTPException(status_code=400, detail="File content does not match declared type")
        data, mime, original_name = await normalize_audio_upload(data, mime, original_name)
        ref_id, _result = await save_upload(
            data, mime, original_name, project_id, document_id,
            title=title, media_type="audio", processing_status="queued",
            created_by=user.get("user_id"), created_by_name=byline,
        )
        if await settings.get("STT_API_URL"):
            from transcription import enqueue_transcription

            await enqueue_transcription(user.get("user_id", ""), ref_id)
        return {"status": "applied", "reference_id": ref_id, "doc_id": ref_id}

    # image
    max_image_mb = await settings.get("MAX_IMAGE_SIZE_MB")
    if len(data) > max_image_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (max {max_image_mb}MB)")
    if not validate_magic(data, mime):
        raise HTTPException(status_code=400, detail="File content does not match declared type")
    ref_id, _result = await save_upload(
        data, mime, original_name, project_id, document_id,
        title=title, media_type="image", processing_status=None,
        created_by=user.get("user_id"), created_by_name=byline,
    )
    # save_upload enqueues the thumbnail for images + emits reference_created.
    return {"status": "applied", "reference_id": ref_id, "doc_id": ref_id}


async def _import_document(
    *,
    is_reference: bool,
    binary_allowed: bool,
    filename: str,
    content: str | None,
    content_base64: str | None,
    title: str,
    parent_id: str | None,
    document_id: str | None,
    project_id: str,
    user: dict,
    scope_root: str | None,
    author_name: str | None = None,
) -> dict:
    """Shared import executor — ONE pipeline, branched on `is_reference` at the
    record factory.

    `author_name` (S1): the DISPLAY-axis byline for every reference this import
    creates (the making agent key's label at the surface); None keeps the user's
    name. `created_by` (rights axis) always stays the user id.

    Format dispatch (mirrors routes.files.extract_text_for_insert):
      - `content` (text)  → is_text_bytes gate → conditional normalize_markdown.
      - `content_base64`  → `.zip` extension → archive reference (media_type
        "file"); else ZIP magic (validate_magic/DOCX_MIME) → sync docx converter
        → normalize_markdown (always for docx); else binary image/audio
        (reference-only, gated by `binary_allowed`).

    The cleaned Markdown then hits extract_and_replace_images and the
    single-source-of-truth record factory (create_document_via_collab /
    create_reference_via_collab). Caller owns the project-access check; the
    factory re-checks parent/host validity + scope.

    # INVARIANT (shared with every import entry point — upload-markdown,
    # extract-text, convert_docx_task): normalize_markdown runs ONLY for
    # .md/.markdown filenames. Why: reflowing arbitrary .txt/code/verse would join
    # meaningful line breaks. The .docx path always normalizes (Pandoc output).
    """
    from agent.collab_writes import (
        create_document_via_collab,
        create_reference_via_collab,
    )

    filename = (filename or "").strip()
    resolved_title = (title or "").strip() or _default_title(filename)

    text: str | None = None
    if content_base64 is not None:
        raw = await _decode_base64(content_base64)
        # INVARIANT (zip/docx collision): archive dispatch runs on the EXTENSION
        # and BEFORE the docx magic check — .zip and .docx share the PK\x03\x04
        # signature, so magic-first would funnel every archive into the docx
        # converter (a confusing 502). The extension carries the intent: a docx
        # renamed .zip is an archive; a .zip never reaches the converter.
        if filename.lower().endswith(".zip"):
            if not binary_allowed:
                raise HTTPException(
                    status_code=400, detail=_BINARY_NOT_A_DOCUMENT_DETAIL,
                )
            return await _create_binary_reference(
                raw, filename, resolved_title, document_id, project_id, user,
                author_name=author_name,
            )
        if validate_magic(raw, DOCX_MIME):
            # .docx path (ZIP container) — SYNC converter, always normalize.
            max_docx_mb = await settings.get("MAX_DOCX_SIZE_MB")
            if len(raw) > max_docx_mb * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"File too large (max {max_docx_mb}MB)")
            text = normalize_markdown(await _convert_docx_sync(raw, filename))
        else:
            # binary image/audio — reference-only path (part1 Commit B fold-in).
            if not binary_allowed:
                raise HTTPException(
                    status_code=400, detail=_BINARY_NOT_A_DOCUMENT_DETAIL,
                )
            return await _create_binary_reference(
                raw, filename, resolved_title, document_id, project_id, user,
                author_name=author_name,
            )
    else:
        # text/markdown input.
        if not content or not content.strip():
            raise HTTPException(status_code=400, detail="content is empty")
        raw_bytes = content.encode("utf-8")
        max_md_mb = await settings.get("MAX_MARKDOWN_SIZE_MB")
        if len(raw_bytes) > max_md_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File too large (max {max_md_mb}MB)")
        text = is_text_bytes(raw_bytes)
        if text is None:
            raise HTTPException(status_code=400, detail="content is not valid UTF-8 text")
        if filename.endswith(".md") or filename.endswith(".markdown"):
            text = normalize_markdown(text)

    # Cleaning choke point shared by every import entry point: extract base64
    # images → image refs, replace with ref: links. Images attach to the SAME
    # parent as the new entity: the tree parent for a document, the host for a
    # reference (matches upload_markdown_reference — never orphaned at the root).
    image_parent = document_id if is_reference else parent_id
    processed, image_refs = await _extract_images(
        text, project_id, image_parent,
        created_by=user.get("user_id"),
        created_by_name=(
            author_name if author_name is not None else user.get("name")
        ),
    )

    if is_reference:
        created = await create_reference_via_collab(
            document_id=document_id, title=resolved_title, content=processed,
            media_type="markdown", source_url=None, project_id=project_id,
            user=user, scope_root=scope_root, author_name=author_name,
        )
        rid = created["doc_id"]
        return {
            "status": "applied", "reference_id": rid, "doc_id": rid,
            "image_references": image_refs,
        }
    created = await create_document_via_collab(
        title=resolved_title, content=processed, parent_id=parent_id,
        project_id=project_id, user=user, scope_root=scope_root,
    )
    return {
        "status": "applied", "doc_id": created["doc_id"],
        "sort_key": created.get("sort_key"), "image_references": image_refs,
    }


@track_agent_tool("import_file")
async def tool_import_file(
    body: ToolImportFile, ctx: dict = Depends(get_agent_context),
):
    """Import a FILE as a new document (node_type="document", the default) or
    reference (node_type="reference", attached to parent_id = the host).

    Auto-only (applies directly on both surfaces — no ProposalKind): the bytes are
    user-supplied data, not a text edit to confirm. Binary image/audio/archive
    requires node_type="reference" (a binary is never a document); the error names
    the fix.
    Reversible in the Lore History panel for a document body / markdown reference;
    binary references are deleted separately.

    Plan tool-surface-consolidation Step 2b: merges upload_document +
    upload_reference. D2: authored text is NOT an input (use create_document) —
    only `content_base64` / `sandbox_path` / `attachment_index` reach the pipeline.
    """
    user = ctx["user"]
    access = await get_project_access(ctx["project_id"], user)
    if access != "full":
        raise HTTPException(status_code=403, detail="Full project access required to create")
    # content_base64 is no longer a tool input (the model cannot author
    # bytes). It is produced INTERNALLY by the two byte channels: attachment_index
    # (an attached chat image) and sandbox_path (a workspace file, which may also
    # produce `content` for text/.md). `_import_document` still accepts it as an
    # internal parameter — only the surface input is gone.
    content_base64: str | None = None
    filename = body.filename
    # Resolution order: attachment_index (in-chat image) → sandbox_path (workspace
    # file). Each produces (content, content_base64) with exactly one populated; the
    # XOR model validator guarantees only one input form was supplied.
    content: str | None = None
    if body.attachment_index is not None:
        # Resolve the attached image's bytes here (durable), then let the existing
        # binary path handle it. attachment_index only makes sense for a reference
        # (an image is never a document); a non-reference call is rejected below by
        # binary_allowed=False, so this stays consistent.
        content_base64, filename = await _resolve_attachment(
            ctx, body.attachment_index, filename,
        )
    elif body.sandbox_path is not None:
        # Resolve the workspace file via SFTP (console gate rides inside
        # read_workspace_file). Branches on category: text → content, binary/docx
        # → content_base64. A text/.md sandbox file flows through `content` so the
        # shared text gate + conditional normalize_markdown still run.
        content, content_base64 = await _resolve_sandbox_path(
            ctx, body.sandbox_path, filename,
        )
    # node_type is the ONLY kind name; parent_id is BOTH placements (tree parent
    # for a document, host for a reference) — discriminated here so the shared
    # executor keeps its internal parameter shape unchanged.
    is_reference = body.node_type == "reference"
    return await _import_document(
        is_reference=is_reference,
        binary_allowed=is_reference,
        filename=filename,
        content=content,
        content_base64=content_base64,
        title=body.title,
        parent_id=None if is_reference else body.parent_id,
        document_id=body.parent_id if is_reference else None,
        project_id=ctx["project_id"],
        user=user,
        scope_root=ctx.get("scope_root"),
        # S1: the byline is the making key's label (owner's name when the key has
        # none — internal/blank). Rights axis (created_by) is unchanged.
        author_name=agent_author_name(ctx),
    )

@track_agent_tool("reprocess_reference")
async def tool_reprocess_reference(
    body: ToolReprocessReference, ctx: dict = Depends(get_agent_context),
):
    """Re-run a reference's import pipeline (audio → re-transcribe, .docx →
    re-convert). The sanctioned alternative to re-deriving a reference's text from
    its binary in the sandbox.

    Agent-only (never on MCP): the wipe of `content` is not History-recoverable for a
    reference, so confirmation is the gate. The dsh driver builds the request URL
    generically from the tool name, so this route IS the wiring (parity with
    import_file). Delegates to the shared `_reprocess_reference` core (routes.files),
    reused by the REST retry endpoint; project access is checked here (the tool's
    edge), the access check is deliberately NOT unified with the REST edge.
    """
    from files_service import validate_reprocessable

    from db import fetch_one

    user = ctx["user"]
    access = await get_project_access(ctx["project_id"], user)
    if access != "full":
        raise HTTPException(
            status_code=403,
            detail="Full project access required to reprocess a reference",
        )
    ref = await fetch_one("documents", body.reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    # INVARIANT: bind the target to the agent key's project before the wipe. Why:
    # full access to THIS project must never authorize a content-destroying write on
    # a reference living in another project. Uniform 404 (no existence oracle),
    # parity with load_owned_proposal. Mirrors require_document_full on the REST
    # retry edge.
    if ref.get("project_id") != ctx["project_id"]:
        raise HTTPException(status_code=404, detail="Reference not found")
    # Validate (image refusal, retryable state, file type) BEFORE the apply gate so a
    # confirm-mode call does not propose a reprocess that could never apply.
    await validate_reprocessable(ref)
    return await _propose_or_apply_reprocess(ctx, body, access, ref, user)


async def _propose_or_apply_reprocess(
    ctx: dict, body: ToolReprocessReference, access: str, ref: dict, user: dict,
) -> dict:
    """Resolve apply-mode for a validated reprocess: auto runs the core directly;
    confirm refuses with the ask signal (409 confirmation_required) — the wipe is
    not History-recoverable, so it stays gated until the user approves."""
    from agent.apply_policy import resolve_apply_mode

    if resolve_apply_mode(
        ui_preference=body.apply.value, is_system=False,
    ).mode != "auto":
        _refuse_unconfirmable("reprocess_reference")

    from files_service import reprocess_reference

    await reprocess_reference(
        body.reference_id, ref, user_id=user.get("user_id", ""),
    )
    return {"status": "applied", "reference_id": body.reference_id}
