"""MCP attach_file redeem route — the write counterpart of the signed download
route in files.py.

# SYSTEM: mcp-upload-url — file upload for MCP agents: the attach_file tool mints a
# token, this UNAUTHENTICATED multipart route redeems it into the widget's save path.

Lives in its own module (not files.py) because it is the only WRITE route that
serves an unauthenticated principal: keeping it apart from the cookie-authed
reference CRUD makes that asymmetry visible to the next reader instead of hiding it
between two `Depends(get_current_user)` handlers.

D1/D4/D5/D10:
- the ONE byte channel for every format (audio/image/docx/markdown); no tool on the
  surface accepts bytes as an argument.
- docx/markdown are KEPT as the file part (parity with the widget's upload-docx
  route): docx → save_upload + convert_docx_task; markdown → save_upload + derived
  text is the file itself.
- idempotent within the token TTL: a `jti` claim + an ATOMIC SETNX jti→created_id
  means a replay (sequential OR concurrent) of the SAME url returns the SAME node id
  with deduplicated:true. The claim is taken BEFORE a node exists, so two concurrent
  redeems of one url cannot both create a node; a creation failure releases the claim
  so a retry is not locked out for the TTL.
- a zero-byte part is refused BEFORE a node is created (per-part length does not
  exist in multipart, so this is the cheap minimum, nothing beyond it).
- the node id is the REDEEM response's `document_id` (the mint echoes `attach_to`
  only); the response is normalized so the agent sees the id without a follow-up call.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import settings
from fastapi import APIRouter, File, HTTPException, UploadFile
from files_util import (
    DOCX_MIME,
    classify_upload_kind,
    save_audio_upload,
    save_upload,
    validate_magic,
)
from inbox import KIND_REF, mark_arrived
from mcp_gateway.upload import MCP_UPLOAD_ROUTE_PREFIX, verify_upload_token
from transcription import enqueue_transcription

import event_bus
from db import fetch_one
from jobs import pool as jobs_pool
from ydoc_store import set_content

router = APIRouter()

logger = logging.getLogger(__name__)

# Margin over the token's remaining TTL for the jti dedup key. Never shorter than
# the URL it guards — a key that expires before the URL re-opens the double-create
# window at exactly the moment a slow retry lands.
_JTI_MARGIN_S = 60
# The value a redeem claims the jti slot with BEFORE a node exists. A real
# document_id is a uuid4 hex, so it never collides with this sentinel; a loser of the
# claim polls the key until it reads a real id (the winner backfilled) or the slot is
# released (the winner failed), then re-claims.
_CLAIMED_SENTINEL = "\x00claimed"
# A loser of the atomic claim polls for the winner's backfilled id. Bounded so a
# stuck winner turns into a clean 409 instead of holding the connection forever. The
# bound covers the slow real case (audio streamed under the size cap) with headroom.
_DEDUP_POLL_TIMEOUT_S = 20.0
_DEDUP_POLL_STEP_S = 0.2


@router.get(MCP_UPLOAD_ROUTE_PREFIX + "{token}")
async def redeem_mcp_upload_recipe(token: str):
    """D11: an agent holding a URL WILL probe it with GET. Return the recipe (a curl
    example) rather than a bare Method Not Allowed, so the agent learns the POST shape
    without guessing. The token is NOT verified here — a probe carries no bytes."""
    return {
        "method": "POST",
        "field_name": "file",
        "note": "POST the file as multipart/form-data to this URL (field 'file'). "
                "The signed URL in the path is the authorization — send no other header.",
        "example": 'curl -X POST "<this-url>" -F "file=@yourfile"',
    }


@router.post(MCP_UPLOAD_ROUTE_PREFIX + "{token}")
async def redeem_mcp_upload(token: str, file: UploadFile = File(...)):
    """Store a file and create a reference node, authorized ONLY by the signed token.

    Orchestrates: resolve token claims → dedup-or-create → normalize response. Each
    stage is a helper so the route reads as the protocol it implements (see the
    per-helper docstrings for the invariants they own).
    """
    claims, kind, mime, original_name, title = await _resolve_redeem_inputs(token, file)

    dedup_key = _dedup_key(claims)
    if dedup_key:
        replay = await _replay_response_or_none(dedup_key, claims, kind, title)
        if replay is not None:
            return replay

    try:
        ref_id, result = await _store_by_kind(kind, file, claims, mime, original_name, title)
    except Exception:  # noqa: BLE001 — release the claim on ANY creation failure, then re-raise
        # Release the claim so a transient failure (bad magic, disk, a dropped audio
        # stream mid-write) does not lock the SAME token out for the whole TTL — the
        # agent's legitimate retry must be able to re-claim.
        if dedup_key:
            await _release_dedup(dedup_key)
        raise

    # Backfill the dedup key with the REAL id for any later replay of the same token.
    if dedup_key:
        await _write_dedup(dedup_key, ref_id, claims)

    await _flag_redeemed_inbox(claims, ref_id)

    # Normalize the redeem response so it carries the NEW node's document_id
    # (the mint echoed attach_to only). The agent gets the id without a follow-up call.
    return {
        "document_id": ref_id,
        "title": title,
        "media_type": _media_for_kind(kind),
        "processing_status": result.get("processing_status"),
        "deduplicated": False,
    }


async def _flag_redeemed_inbox(claims: dict, ref_id: str) -> None:
    """Inbox (see SYSTEM: inbox): the redeem IS an external arrival — flag the
    minting user (the recipient) for this reference. mark_arrived consults the
    doc's refs toggle (default OFF) itself. A dedup replay never reaches this
    call (it returned above), so a replay cannot re-flag; an internal agent's
    mint (the dsh driver) never flags."""
    if claims.get("internal") is True:
        return
    await mark_arrived(
        KIND_REF, object_id=ref_id, project_id=claims["project_id"],
        document_id=claims["document_id"], recipient_id=claims["user_id"],
    )


async def _resolve_redeem_inputs(token: str, file: UploadFile):
    """Verify the signed token and classify the upload, refusing an unsupported type
    or a zero-byte part BEFORE any dedup claim or node creation.

    # INVARIANT(security): every parameter that decides WHAT is written comes from
    # the TOKEN, never from the request — project_id, host document_id, filename,
    # title and the declared mime are all mint-time claims. Why: there is no key to
    # re-check here, so a request-supplied host or type would turn a leaked URL from
    # "add this one named file to this one document" into a project-wide write. The
    # multipart part supplies bytes and nothing else; its own filename and
    # content_type are deliberately ignored.
    """
    claims = verify_upload_token(token)
    mime = claims["mime"]
    original_name = claims["filename"]
    title = claims.get("title") or original_name

    kind, max_mb = await classify_upload_kind(original_name, mime)
    if kind is None:
        # Unreachable via the mint tool (it rejects a non-binary/non-text extension
        # up front); a defensive 400 rather than a confusing downstream failure.
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {mime}")

    # Refuse a zero-byte part BEFORE claiming the dedup slot (and before a node
    # exists), so a zero-byte POST leaves no sentinel that would lock the token out
    # for the TTL. Per-part Content-Length is the cheap minimum (a truncating client
    # is undetectable at any layer).
    if getattr(file, "size", None) == 0:
        raise HTTPException(status_code=400, detail="Empty upload — no file bytes received")

    # Enforce the SAME per-kind cap the mint advertised (classify_upload_kind's
    # max_mb, shipped to the agent as max_size_mb), BEFORE the buffered branches read
    # the whole part into memory. Runs on file.size (parser-tracked; a lying
    # Content-Length cannot bypass it), folded for EVERY kind, and before the dedup
    # claim so an over-cap POST (a client error) takes/leaves no locking sentinel.
    _enforce_size_cap(file, kind, max_mb)

    return claims, kind, mime, original_name, title


def _dedup_key(claims: dict) -> str:
    """The Redis idempotency key for this token's jti, or '' when the mint carried no
    jti (older mints — no dedup, every POST creates)."""
    jti = claims.get("jti")
    return f"mcp:upload:jti:{jti}" if jti else ""


async def _replay_response_or_none(dedup_key: str, claims: dict, kind: str, title: str):
    """Claim the dedup slot; if a prior redeem of the SAME url already created a node,
    return its (status-corrected) replay response, else None (this redeem won the
    claim and must create).

    # Idempotent within the token TTL, and ATOMIC. `_claim_dedup` takes the jti
    # slot with SETNX BEFORE any node exists: the winner creates + backfills the real
    # id; a loser waits for that id (or 409s if the winner runs past the poll bound).
    """
    replay_id = await _claim_dedup(dedup_key, _dedup_ttl(claims))
    if replay_id is None:
        return None

    # Report the node's REAL processing status, not a frozen None. Why: this
    # surface treats null as TERMINAL ("no derived text expected"), so a hardcoded
    # None on a replayed audio POST tells the agent the transcript is done while it
    # is still queued — the agent stops polling and reads an empty document (the
    # stale-as-current class the project forbids). The status CHANGES after the dedup
    # value was written (queued → ready), so it is read NOW from the row.
    node = await fetch_one("documents", replay_id)
    if not node or node.get("deleted_at"):
        # The node was removed between the two POSTs — the honest answer is not a
        # replay of an id that resolves to nothing.
        raise HTTPException(status_code=404, detail="The original upload for this URL was removed")
    return {
        "document_id": replay_id,
        "title": node.get("title") or title,
        "media_type": node.get("media_type", _media_for_kind(kind)),
        "processing_status": node.get("processing_status"),
        "deduplicated": True,
    }


async def _author_kwargs(claims: dict) -> dict:
    """Author-attribution kwargs for the MINTING user (plan reference-card-author-
    nickname rule 4: agent-driven creation IS attributed — the token's user_id is the
    human the agent acts for; transcription is already enqueued for that same user).

    S1: when the minting key carried a label (an external agent key), the label is
    the byline — it rode the token's claims because the redeem route has no key to
    read (authorization is bound at mint; the byline rides the same way). A missing
    label claim (pre-S1 mints in flight, internal keys) resolves the display name
    from `users` once per redeem, as before. A missing user row → no name → the UI
    renders no author segment."""
    user_id = claims.get("user_id") or None
    if not user_id:
        return {}
    label = claims.get("agent_label")
    label = label.strip() if isinstance(label, str) else ""
    if label:
        return {"created_by": user_id, "created_by_name": label}
    row = await fetch_one("users", user_id)
    return {"created_by": user_id, "created_by_name": (row or {}).get("name")}


async def _store_by_kind(kind: str, file: UploadFile, claims: dict, mime: str,
                         original_name: str, title: str):
    """Dispatch the bytes through the EXACT widget path for the upload kind.

    # ARCH: bytes flow through save_audio_upload (streaming + magic + remux) or
    # save_upload (image/docx/markdown), so caps, ffmpeg remux, thumbnails, the
    # reference_created event and the conversion/transcription enqueue are shared.
    """
    author = await _author_kwargs(claims)
    if kind == "audio":
        return await _store_audio(file, claims, mime, original_name, title, **author)
    if kind == "docx":
        return await _store_docx(await file.read(), claims, original_name, title, **author)
    if kind == "markdown":
        return await _store_markdown(await file.read(), claims, original_name, title, **author)
    return await _store_image(await file.read(), claims, mime, original_name, title, **author)  # image


def _media_for_kind(kind: str) -> str:
    """The stored media_type for an upload kind (markdown for docx — its derived text
    IS markdown, exactly like the widget's upload-docx route)."""
    if kind == "docx":
        return "markdown"
    return kind


def _enforce_size_cap(file: UploadFile, kind: str, max_mb: int) -> None:
    """Enforce the per-kind size cap the mint advertised, on the parser-tracked part
    size, BEFORE the buffered branch reads the whole part into memory.

    # ARCH (D-A): classify_upload_kind returns the SAME (kind, max_mb) the mint
    # shipped to the agent as max_size_mb; enforcing it here (not a per-branch
    # literal) means the two edges of one upload cannot disagree. The check runs on
    # file.size — which the multipart parser tracks itself — so it rejects BEFORE
    # `_store_docx`/`_store_markdown`/`_store_image` pull the spooled part into one
    # bytes (the memory blow-up was at `await file.read()` in those branches).

    # INVARIANT(None size): a part whose size the parser could not determine must
    # NOT silently skip the cap (the `getattr(...) == 0` leniency of the zero-byte
    # check must not spread here). Why decided per kind: audio defers —
    # save_audio_upload streams + caps actual bytes by count, so a None header is
    # still bounded; the buffered kinds would pull an unbounded part into memory, so
    # they are refused with 411 (Content-Length required) rather than attempted.
    """
    max_bytes = max_mb * 1024 * 1024
    size = getattr(file, "size", None)
    if size is None:
        if kind == "audio":
            return
        raise HTTPException(
            status_code=411, detail="Content-Length is required for this upload",
        )
    if size > max_bytes:
        raise HTTPException(
            status_code=413, detail=f"File too large (max {max_mb}MB)",
        )


def _dedup_ttl(claims: dict) -> float:
    """TTL for the jti dedup key = the token's remaining life + the margin."""
    exp = claims.get("exp")
    now = time.time()
    if isinstance(exp, (int, float)):
        remaining = max(1.0, exp - now)
    else:
        try:
            remaining = max(1.0, datetime.fromisoformat(str(exp)).timestamp() - now)
        except Exception:  # noqa: BLE001 — a malformed exp falls back to the default TTL
            remaining = 900.0
    return remaining + _JTI_MARGIN_S


async def _read_dedup(key: str) -> str | None:
    """Return the stored value for the jti key (a real ref_id, the in-flight sentinel,
    or None). Best-effort: Redis is mandatory, but a read failure must not block the
    redeem (it falls back to non-atomic — Redis down = service down anyway)."""
    try:
        from backplane import get_backplane

        raw = await get_backplane().get(key)
    except Exception:  # noqa: BLE001 — dedup is best-effort
        logger.exception("dedup read failed for %s; proceeding without dedup", key)
        return None
    if not raw:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def _claim_dedup(key: str, ttl: float) -> str | None:
    """Atomically claim the jti slot BEFORE a node exists.

    Returns the created node id if a prior redeem (completed OR in-flight-then-
    completed) already owns the slot (→ the caller dedups and must NOT create); or
    None if THIS call won the claim and the caller MUST create + backfill.

    D10: the pre-fix code did GET → create → SET (unconditional), which deduped
    SEQUENTIAL replays but not CONCURRENT ones — two overlapping retries both saw the
    empty GET and both created. SETNX closes the window: only the first caller sets the
    sentinel. A loser polls for the winner's backfilled real id; if the winner FAILED
    (and released), the loser re-claims on the next loop turn rather than 409ing.
    """
    # Fast path: a prior redeem already completed (key holds a real id).
    existing = await _read_dedup(key)
    if existing and existing != _CLAIMED_SENTINEL:
        return existing
    deadline = time.monotonic() + _DEDUP_POLL_TIMEOUT_S
    from backplane import get_backplane

    while True:
        try:
            won = await get_backplane().set_nx(key, _CLAIMED_SENTINEL, ttl_s=ttl)
        except Exception:  # noqa: BLE001 — Redis down: no claim possible; create anyway
            logger.exception("dedup claim failed for %s; proceeding without claim", key)
            return None
        if won:
            return None  # we own the slot — caller creates + backfills
        # Lost the claim: another redeem holds the slot. Did it complete?
        existing = await _read_dedup(key)
        if existing and existing != _CLAIMED_SENTINEL:
            return existing
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=409,
                detail="Another upload of this URL is still finishing; retry the POST shortly",
            )
        await asyncio.sleep(_DEDUP_POLL_STEP_S)


async def _write_dedup(key: str, ref_id: str, claims: dict) -> None:
    """Backfill the created id under the jti key, overwriting the sentinel (a loser
    waiting in `_claim_dedup` reads it on its next poll turn). TTL kept from the token."""
    try:
        from backplane import get_backplane

        await get_backplane().set_ttl(key, ref_id, ttl_s=_dedup_ttl(claims))
    except Exception:  # noqa: BLE001 — dedup is best-effort
        logger.exception("dedup write failed for %s", key)


async def _release_dedup(key: str) -> None:
    """Release the claim after a failed creation so a retry can re-claim instead of
    waiting out the TTL (a 409). No-op if we never held the slot (Redis was down)."""
    try:
        from backplane import get_backplane

        await get_backplane().delete(key)
    except Exception:  # noqa: BLE001 — release is best-effort
        logger.exception("dedup release failed for %s", key)


async def _store_audio(file: UploadFile, claims: dict, mime: str,
                       original_name: str, title: str, *,
                       created_by: str | None = None,
                       created_by_name: str | None = None) -> tuple[str, dict]:
    """Audio branch — byte-for-byte the widget's: stream to disk under the audio cap,
    then queue transcription for the MINTING user (claims["user_id"]), never a
    request-supplied one."""
    ref_id, result, _ = await save_audio_upload(
        file, mime, original_name, claims["project_id"], claims["document_id"],
        title=title, processing_status="queued",
        created_by=created_by, created_by_name=created_by_name,
    )
    if await settings.get("STT_API_URL"):
        await enqueue_transcription(claims["user_id"], ref_id)
    return ref_id, result


async def _store_docx(data: bytes, claims: dict, original_name: str,
                      title: str, *, created_by: str | None = None,
                      created_by_name: str | None = None) -> tuple[str, dict]:
    """.docx branch — parity with the widget upload-docx route: store the ORIGINAL,
    create a queued markdown reference, enqueue async conversion. The original is kept
    so retries can re-run (Pandoc output is not stable across versions)."""

    if not validate_magic(data, DOCX_MIME):
        raise HTTPException(status_code=400, detail="File content is not a valid .docx")
    ref_id, result = await save_upload(
        data, DOCX_MIME, original_name, claims["project_id"], claims["document_id"],
        title=title, media_type="markdown", processing_status="queued",
        created_by=created_by, created_by_name=created_by_name,
    )
    # The name rides the task so images extracted from the docx are attributed too.
    await jobs_pool.enqueue("convert_docx_task", ref_id, claims["user_id"], created_by_name,
                  job_id=f"docx:{ref_id}")
    return ref_id, result


async def _store_markdown(data: bytes, claims: dict, original_name: str,
                          title: str, *, created_by: str | None = None,
                          created_by_name: str | None = None) -> tuple[str, dict]:
    """Markdown branch — the file IS the text (D5: md → itself). Store the original
    and set the derived content to the (normalized) file text."""
    from markdown_normalize import normalize_markdown

    text = data.decode("utf-8", errors="replace")
    ref_id, result = await save_upload(
        data, "text/markdown", original_name, claims["project_id"], claims["document_id"],
        title=title, media_type="markdown", processing_status="ready",
        created_by=created_by, created_by_name=created_by_name,
    )
    # Backfill the derived content (save_upload writes content="").
    # INVARIANT(corruption): content and ydoc_state are written by ONE canonical
    # writer — set_content(persist=True), exactly like the widget's markdown
    # branch (routes/files.py). Why: a bare `SET content` leaves ydoc_state
    # NONE, so the reference's first collab open seeds a Y.Doc that diverges
    # from the stored body the moment anything else persists state.
    normalized = normalize_markdown(text)
    await set_content(ref_id, normalized, persist=True)
    # INVARIANT: a post-create content write emits content_flushed at the write
    # site. Why: save_upload created the row with EMPTY content (the primitive's
    # non-empty guard correctly skipped it — a binary/markdown upload enqueues
    # nothing at birth), so this backfill is the FIRST time the body exists; it
    # never passes through the primitive again. Without this emit the reference
    # is persisted but never embedded — the class behind the 24 unembedded
    # markdown refs in the coverage audit.
    if normalized.strip():

        await event_bus.emit("content_flushed", entity_type="doc", entity_id=ref_id,
                             project_id=claims["project_id"])
    result = {**result, "processing_status": "ready"}
    return ref_id, result


async def _store_image(data: bytes, claims: dict, mime: str, original_name: str,
                       title: str, *, created_by: str | None = None,
                       created_by_name: str | None = None) -> tuple[str, dict]:
    """Image branch — buffered (images are small), magic-checked, then save_upload,
    which also enqueues the thumbnail job. processing_status is None (no OCR pipeline;
    an image has a file AND a null status — the two states null conflates today).
    The size cap is the SHARED pre-dispatch check (D-A, on file.size), not a branch
    literal — removed from here so the two edges cannot disagree."""
    if not validate_magic(data, mime):
        raise HTTPException(status_code=400, detail="File content does not match declared type")
    ref_id, result = await save_upload(
        data, mime, original_name, claims["project_id"], claims["document_id"],
        title=title, media_type="image", processing_status=None,
        created_by=created_by, created_by_name=created_by_name,
    )
    return ref_id, result
