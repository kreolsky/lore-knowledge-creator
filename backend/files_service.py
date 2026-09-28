"""Files domain service — reference reprocessing + image extraction + list serialization.

# SYSTEM: files — the cross-route files domain core. Cross-route callers import
#   this service, not a route module (no `from routes.files import _`). Low-level
#   upload/magic helpers live in files_util.py; this service holds the reference
#   file resolution chain (resolve_reference_file), the reprocess pipeline
#   (reprocess_reference + validate_reprocessable), the base64-image extraction
#   pipeline (extract_and_replace_images), and the unified reference-list serializer.
# ARCH: NO access checks live here — each caller gates at its own edge
#   (require_document_full for the REST retry route; project access for the agent
#   tool). The service is pure domain logic + dispatch.
"""

import base64
import logging
import mimetypes
import re

import settings
from fastapi import HTTPException
from files_util import save_upload, validate_magic
from transcription import enqueue_transcription

from config import IMAGE_MIMES, STORAGE_PATH
from db import fetch_one, get_db, serialize_record
from event_bus import emit
from jobs import pool as jobs_pool
from models import is_ref_row

logger = logging.getLogger(__name__)


# --- Reference file resolution ---------------------------------------------

async def resolve_reference_file(
    *, ref_id: str | None = None, project_id: str | None = None, ref: dict | None = None,
) -> tuple:
    """Resolve a reference's stored file with the full security chain, ONE home for
    the logic shared by sandbox_fetch_reference, the serve routes, and the MCP
    get_file tool.

    Returns (abs_path, mime, safe_name). Raises HTTPException on any failure.

    Pass `ref_id` (+ optional `project_id`) to fetch the row here, OR pass the
    already-fetched `ref` dict directly (then `ref_id`/`project_id` are ignored —
    used by the serve routes, which fetch + authorize upstream). When neither is
    given the lookup misses and raises a uniform 404.

    Chain:
      - fetch ref (unless passed) → assert is_reference;
      - assert ref.project_id == project_id WHEN project_id is given (cross-project
        IDOR → uniform 404, no existence oracle); project_id=None skips the match —
        the caller has bound authorization another way (the signed-URL serve route,
        whose token already binds exactly one ref_id);
      - read file_path from the row → containment under STORAGE_PATH (traversal) →
        exists-on-disk.

    # INVARIANT(security): the cross-project mismatch returns the SAME 404
    # "Reference not found" as a non-reference / unknown id — uniform by design, so
    # a caller learns nothing about whether the id exists in another project. Why: a
    # distinguishable 403 would be an existence oracle across the project boundary.
    # A copied security check is one that eventually ships without its guard, so the
    # chain lives here and the three callers reach it through this one function.
    """
    if ref is None:
        ref = await fetch_one("documents", ref_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    if project_id is not None and ref.get("project_id") != project_id:
        # Uniform not-found (no existence oracle across the project boundary).
        raise HTTPException(status_code=404, detail="Reference not found")
    rel_path = ref.get("file_path", "")
    if not rel_path:
        raise HTTPException(status_code=404, detail="No file attached")
    abs_path = (STORAGE_PATH / rel_path).resolve()
    if not abs_path.is_relative_to(STORAGE_PATH.resolve()):
        logger.error("Path traversal in reference %s file_path: %s", ref_id, rel_path)
        raise HTTPException(status_code=404, detail="Reference not found")
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="Reference file is missing on disk")
    mime = (
        ref.get("file_meta", {}).get("mime_type")
        or mimetypes.guess_type(str(abs_path))[0]
        or "application/octet-stream"
    )
    # safe_name: the sanitized basename persisted at save time (re.sub over the
    # original in files_util.save_upload). The true original name is NOT reliably
    # persisted (title may carry it only when no explicit title was given), so the
    # sanitized name is the honest default — callers that want a different download
    # name pass their own.
    return abs_path, mime, abs_path.name


# --- Reference reprocessing --------------------------------------------------

def is_convertible_reference(ref: dict) -> bool:
    """A markdown reference whose source file is a convertible binary (.docx or
    .pdf) — the converter path.

    Single definition shared by validation (acceptance) and the core (dispatch) so
    the two cannot diverge: a ref accepted as reprocessable must always route to a
    task, otherwise it would strand at processing_status='queued' with empty content
    forever (silent degradation).
    """
    return (
        ref.get("media_type") == "markdown"
        and ref.get("file_path", "").lower().endswith((".docx", ".pdf"))
    )


async def validate_reprocessable(ref: dict) -> None:
    """Pre-conditions for re-running a reference's import pipeline. Pure validation
    — NO mutation. Raises HTTPException on any refusal. Shared by the REST retry
    endpoint and the agent-only reprocess_reference tool so the two surfaces cannot
    drift on what is reprocessable.

    # Part of the files system (see SYSTEM: files) — reprocess validation shared by retry + the agent tool.
    """
    # Images are never reprocessed: there is no OCR pipeline (the import pipeline
    # does not produce text for images — they ride into the prompt as image_url), so
    # refuse BEFORE the status check. Why before: an image's processing_status is
    # None, which would otherwise yield a confusing "not in a retryable state"; the
    # useful refusal names the alternative (look at the image / ask the user).
    if ref.get("media_type") == "image":
        raise HTTPException(
            status_code=400,
            detail="Image references are not reprocessable — there is no OCR. The "
                   "image is already in the conversation; look at it, or ask the "
                   "user if you cannot see it.",
        )
    # INVARIANT: 'processing' is retryable so the user can self-unstick a ref whose
    # worker died mid-run (status frozen at 'processing', spinner forever, no restart).
    # Why: we deliberately do NOT auto-recover 'processing' refs on a timer (a live
    # worker mid-run is indistinguishable from a dead one), so manual retry is the
    # escape hatch. Accepted trade-off: retrying a still-live job costs one extra
    # sequential pass, not corruption (per-user FIFO worker, not parallel).
    if ref.get("processing_status") not in ("error", "ready", "processing"):
        raise HTTPException(status_code=400, detail="Reference is not in a retryable state")
    if not ref.get("file_path"):
        raise HTTPException(status_code=400, detail="No file attached")
    # WHY: retry dispatches by the stored file type, not by a generic flag — an
    # audio ref re-transcribes, a markdown ref whose source file is a .docx/.pdf
    # re-converts. Why: both share the queued/processing/error lifecycle but run
    # different tasks.
    media_type = ref.get("media_type")
    if media_type != "audio" and not is_convertible_reference(ref):
        raise HTTPException(status_code=400, detail="This reference type cannot be re-processed")


async def reprocess_reference(reference_id: str, ref: dict, *, user_id: str) -> dict:
    """Re-run a reference's import pipeline: audio → re-transcribe, .docx/.pdf-backed
    markdown → re-convert. Wipes the current content, sets processing_status=
    'queued', emits reference_updated, and dispatches the matching background task.

    Shared core for the REST retry endpoint (`/api/references/{id}/retry`) and the
    Agent-only `reprocess_reference` tool. NO access checks here — each caller gates at
    its own edge (require_document_full for REST; project access for the tool), and
    the image/status/file/type pre-conditions are validated (not guessed) via
    `validate_reprocessable` so a confirm-mode tool call can validate WITHOUT wiping.

    # Part of the files system (see SYSTEM: files) — reprocess core shared by retry + the agent tool.
    """
    await validate_reprocessable(ref)
    src_path = (ref.get("file_path") or "").lower()

    db = await get_db()
    # WHY the whole run cursor is cleared here: `mem_consolidated_at` marks a reference
    # CONSUMED by a consolidation run, and an `error` reference is stamp-SKIPPED by
    # `_next_portion`. Reprocessing makes the material readable
    # again — if the stamp survived, the recovered reference would be permanently
    # excluded from every future run (`mem_consolidated_at IS NONE` is the resume
    # cursor), silently losing the recovered text. Clearing it on reprocess is what
    # makes "unusable" a recoverable state rather than a one-way door.
    #
    # WHY: `mem_window_done` clears with the two stamps — this is the third writer
    # of the run cursor, alongside `_clear_consumed_stamps` / `reopen_consolidation`.
    # Why: reprocess replaces `content` wholesale, so a surviving window cursor resumes
    # the NEW text at window 2 and never reads windows 0-1 — the same silent drop the
    # other two clear paths guard against, here with re-imported material.
    await db.query(
        "UPDATE type::record('documents', $id) SET processing_status = 'queued', "
        "content = '', mem_consolidated_at = NONE, mem_consolidated_run = NONE, "
        "mem_window_done = NONE, "
        "updated_at = time::now()",
        {"id": reference_id},
    )

    await emit("reference_updated", project_id=ref.get("project_id", ""),
               reference_id=reference_id)

    if src_path.endswith((".docx", ".pdf")):
        # Dispatch is per-extension (the retry INVARIANT above): .docx →
        # convert_docx_task, .pdf → convert_pdf_task; the job_id prefix matches
        # the upload site's so a retry replaces the same job slot.
        #
        # Re-extracted images are attributed to the ORIGINAL uploader (the ref's own
        # created_by) so a retry does not re-stamp them onto whoever hit retry;
        # legacy rows without attribution fall back to the retrying user.
        task_name, prefix = (
            ("convert_pdf_task", "pdf") if src_path.endswith(".pdf")
            else ("convert_docx_task", "docx")
        )
        await jobs_pool.enqueue(task_name, reference_id,
                      ref.get("created_by") or user_id,
                      ref.get("created_by_name"), job_id=f"{prefix}:{reference_id}")
    elif await settings.get("STT_API_URL"):
        await enqueue_transcription(user_id, reference_id)
    return {"success": True}


# --- Base64 image extraction -------------------------------------------------

_REF_DEF_PATTERN = re.compile(
    r'^\[([^\]]+)\]:\s+<?(data:image/([\w+]+);base64,([^>\n]+))>?\s*$',
    re.MULTILINE | re.IGNORECASE,
)

_INLINE_IMG_PATTERN = re.compile(
    r'!\[([^\]]*)\]\((data:image/([\w+]+);base64,([^)]+))\)',
    re.IGNORECASE,
)

_MIME_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
assert _MIME_EXTENSIONS.keys() >= IMAGE_MIMES, (
    f"_MIME_EXTENSIONS missing entries for: {IMAGE_MIMES - _MIME_EXTENSIONS.keys()}"
)


async def save_base64_image(
    b64_data: str,
    mime_suffix: str,
    project_id: str,
    document_id: str | None,
    filename: str,
    *,
    created_by: str | None = None,
    created_by_name: str | None = None,
) -> tuple[str, dict]:
    """Validate, decode, and save a single base64 image as a reference.

    Returns (ref_id, serialized_record).
    Raises ValueError on validation failures.
    """
    mime = f"image/{mime_suffix}"
    if mime not in IMAGE_MIMES:
        raise ValueError(f"Unsupported image type: {mime}")

    clean_b64 = re.sub(r'\s+', '', b64_data)
    try:
        data = base64.b64decode(clean_b64)
    except Exception:
        raise ValueError("Invalid base64 image data")

    max_image_mb = await settings.get("MAX_IMAGE_SIZE_MB")
    if len(data) > max_image_mb * 1024 * 1024:
        raise ValueError(f"Image too large (max {max_image_mb}MB)")
    if not validate_magic(data, mime):
        raise ValueError("Image content does not match declared type")

    ref_id, result = await save_upload(
        data, mime, filename, project_id, document_id,
        title=filename, media_type="image", processing_status=None,
        created_by=created_by, created_by_name=created_by_name,
    )
    return ref_id, result


def _dims_from_result(result: dict) -> str:
    meta = result.get("file_meta", {})
    w, h = meta.get("width", 0), meta.get("height", 0)
    return f"{w}x{h}" if w and h else ""


async def extract_and_replace_images(
    content: str,
    project_id: str,
    document_id: str | None,
    *,
    created_by: str | None = None,
    created_by_name: str | None = None,
) -> tuple[str, list[dict]]:
    """Extract base64 images from markdown, save as refs, replace with ref: links.

    Returns (processed_content, list_of_created_ref_records).
    Raises ValueError on invalid image data.
    """
    ref_records: list[dict] = []
    label_to_ref: dict[str, str] = {}
    label_to_dims: dict[str, str] = {}
    image_counter = 0

    for m in _REF_DEF_PATTERN.finditer(content):
        label = m.group(1)
        mime_suffix = m.group(3)
        b64_data = m.group(4)

        image_counter += 1
        ext = _MIME_EXTENSIONS.get(f"image/{mime_suffix}", ".png")
        filename = f"image-{image_counter}{ext}"

        ref_id, result = await save_base64_image(
            b64_data, mime_suffix, project_id, document_id, filename,
            created_by=created_by, created_by_name=created_by_name,
        )
        ref_records.append(result)

        label_to_ref[label] = ref_id
        label_to_dims[label] = _dims_from_result(result)

    if label_to_ref:
        def _replace_ref_usage(match: re.Match) -> str:
            label = match.group(1)
            ref_id = label_to_ref.get(label)
            if ref_id is None:
                return match.group(0)
            dims = label_to_dims.get(label, "")
            alt = f"|{dims}" if dims else ""
            return f"![{alt}](ref:{ref_id})"

        content = re.sub(r'!\[([^\]]*)\]\[\]', _replace_ref_usage, content)
        content = re.sub(r'!\[([^\]]*)\]\[([^\]]+)\]', lambda m: (
            f"![|{label_to_dims.get(m.group(2), '')}](ref:{label_to_ref[m.group(2)]})"
            if m.group(2) in label_to_ref else m.group(0)
        ), content)
        content = _REF_DEF_PATTERN.sub('', content)

    inline_matches = list(_INLINE_IMG_PATTERN.finditer(content))
    for m in reversed(inline_matches):
        alt_text = m.group(1)
        mime_suffix = m.group(3)
        b64_data = m.group(4)

        image_counter += 1
        ext = _MIME_EXTENSIONS.get(f"image/{mime_suffix}", ".png")
        filename = f"image-{image_counter}{ext}"

        ref_id, result = await save_base64_image(
            b64_data, mime_suffix, project_id, document_id, filename,
            created_by=created_by, created_by_name=created_by_name,
        )
        ref_records.append(result)

        dims = _dims_from_result(result)
        alt_part = f"{alt_text}|{dims}" if alt_text and dims else (f"|{dims}" if dims else alt_text)
        replacement = f"![{alt_part}](ref:{ref_id})"
        content = content[:m.start()] + replacement + content[m.end():]

    return content, ref_records


# --- Reference list serialization --------------------------------------------

# INVARIANT(anonymous-surface): consumed by routes/public_share.py — every field this
# returns is serialized into an UNAUTHENTICATED response (the anonymous /references
# LIST on /s/:token). Do not add a field here unless it is safe to publish publicly. Why:
# the public-share data-leak class.
def serialize_ref_meta(row: dict) -> dict:
    """Metadata-only list serialization — no `content`, no `headings`.

    # WHY: LIST responses (/api/references with document_id or project_id
    # Why: LIST payloads are fetched 2-3x per doc switch; omitting content/headings keeps them small.
    # scope) carry NO `content` and NO `headings`. Content is lazy-fetched per
    # reference via GET /api/references/{id} (the single-ref serializer
    # `_serialize_ref` in routes/references.py keeps content + headings).
    # POST/PATCH/upload responses and /references/{id}/status (files.py) also carry
    # full content — this metadata-only contract is LIST-only.
    # Why: the list payload is fetched 2-3x per doc switch at ~360ms prod RTT;
    # carrying every ref's markdown body made each switch multi-MB. `has_content`
    # (computed NONE-safely in the projection) lets the frontend distinguish an
    # empty ref (false) from "content not yet loaded" (true + absent content).
    #
    # WHY this function does NOT itself drop `content`: the projection
    # (`_REF_META_SELECT` in routes/references.py) is what strips `content` from
    # authed LIST responses. This serializer passes `content` through if present.
    # The public share router (public_share.py:`public_references`) RELIES on that
    # passthrough: it issues `SELECT *` so the anonymous response carries ref bodies
    # for inline transclusion rendering on /s/:token (the authed lazy fetch would
    # 401 anonymously). Do not "fix" this serializer to drop `content` without
    # also reworking the public transclusion seed — see the WHY SELECT * comment
    # on public_references.
    """
    out = serialize_record(row, "reference_id")
    if "parent_id" in out:
        out["document_id"] = out.pop("parent_id")
    return out
