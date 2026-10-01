"""Document service — creation, sibling ordering, export + transclusion.

See SYSTEM: documents (entry: backend/documents/__init__.py).
# ARCH: centralising key assignment here prevents the recurring class of bug where
#   a non-endpoint creator forgets sort_key and leaves a NONE row that breaks
#   drag-reorder. The export/transclusion/unique-path/prefs helpers live here, not
#   in routes/documents.py, so cross-route callers import a service rather than a
#   route module (no `from routes.documents import _`).
#   Depends only on db + sibling services (no routes at import time), so the
#   pipeline and routes can import it without a cycle.

# ARCH: cross-route callers import these public names (assert_parent_valid,
#   create_with_unique_path, last_doc_rule, get_prefs_last_doc) instead of the old
#   `from routes.documents import _...` private reach-through.
"""

import logging
import re
from urllib.parse import quote

from cp_store import (
    BLOB_UNAVAILABLE_DETAIL,
    BlobUnavailable,
    resolve_checkpoint_content,
)
from fastapi import HTTPException
from sort_keys import key_before, key_between

import event_bus
from db import create_record, extract_id, fetch_one, get_db, validate_record_id
from models import is_ref_row
from transclusion_grammar import NON_DOC_TARGET

logger = logging.getLogger(__name__)


async def sibling_rows(
    project_id: str, parent_id: str | None, *, is_reference: bool = False,
) -> list[dict]:
    """Siblings of ONE kind in a group with an assigned sort_key, ascending.

    Group key = (project_id, parent_id, is_reference) — refs and child docs
    share parent_id but never a key space. The REF key space deliberately
    INCLUDES archived refs (no archived filter): a new ref's top key must never
    equal an archived ref's key (a group holding only archived refs would
    otherwise mint key_between(None, None) again) and an unarchive must not
    create an equal-key pair. "Live only" is the reorder command's check, not
    the key space's.
    """
    db = await get_db()
    # INVARIANT: tie-break equal sort_keys by id ASC. Why: must match the order the
    # user sees (projects.py list: `sort_key ASC, id ASC`; frontend tie-breaks by
    # document_id). Without it, two siblings sharing a key (concurrent-drag race) get a
    # DB-arbitrary order, so reorder computes the new key against a different neighbour
    # than displayed — non-deterministic placement (and a flaky 409 boundary).
    rows = await db.query(
        "SELECT meta::id(id) AS id, sort_key, archived FROM documents "
        "WHERE project_id = $pid AND parent_id = $par AND deleted_at IS NONE "
        "AND is_reference = $kind AND sort_key IS NOT NONE "
        "ORDER BY sort_key ASC, id ASC",
        {"pid": project_id, "par": parent_id, "kind": is_reference},
    )
    return rows or []


async def top_sibling_key(
    project_id: str, parent_id: str | None, *, is_reference: bool = False,
) -> str:
    """Key that places a row of `is_reference` kind at the TOP (newest-first) of
    its own kind's sibling group."""
    rows = await sibling_rows(project_id, parent_id, is_reference=is_reference)
    return key_before(rows[0]["sort_key"]) if rows else key_between(None, None)


def place_after(
    siblings: list[dict], moved_id: str, after_id: str | None, *,
    not_sibling_detail: str,
) -> str:
    """The ONE "place after sibling X" bounds computation (null = top).

    Drops `moved_id` from `siblings`, computes the fractional key strictly
    between the anchor and its successor, 400s with the CALLER's detail when
    `after_id` is not in the list, 409s on degenerate bounds (shared keys after
    a concurrent drag — the client refetches and retries). Shared by the reorder
    command and the tree move; their 400 detail strings pass through unchanged.
    """
    group = [s for s in siblings if s["id"] != moved_id]
    if after_id is None:
        lo, hi = None, (group[0]["sort_key"] if group else None)
    else:
        idx = next((i for i, s in enumerate(group) if s["id"] == after_id), None)
        if idx is None:
            raise HTTPException(status_code=400, detail=not_sibling_detail)
        lo = group[idx]["sort_key"]
        hi = group[idx + 1]["sort_key"] if idx + 1 < len(group) else None
    try:
        return key_between(lo, hi)
    except Exception:
        # Degenerate bounds (e.g. two siblings sharing a key after a concurrent drag):
        # surface a clean 409 so the client can refetch + retry instead of a raw 500.
        raise HTTPException(status_code=409, detail="Sibling order is stale; refetch and retry")


async def create_document(uid: str, data: dict, *, user_id: str | None = None, user_name: str | None = None) -> dict:
    """Create a document or reference row, guaranteeing a sort_key for both kinds.

    INVARIANT: every live document row, ref or not, gets a sort_key at creation time
    in its OWN key space (newest-first) unless one is passed explicitly.
    Why: a row created without sort_key stays NONE and is filtered out of its
    sibling list, so dragging another row onto it returns 400 "after_id is not a
    sibling" (extractor pipeline — the doc case), and a ref without a key has no
    panel order at all.
    """
    # INVARIANT: project_id is validated BEFORE create_record, not read for the
    # first time by the emit below. Why: the emit runs after the row is written,
    # so a caller omitting project_id would get a KeyError on an ALREADY-CREATED
    # document — a half-done create the caller cannot tell from a rejected one.
    # Crash before anything is persisted instead (no default: a document without
    # a project belongs nowhere).
    if not data.get("project_id"):
        raise ValueError(f"create_document({uid}): project_id is required")
    if data.get("sort_key") is None:
        data["sort_key"] = await top_sibling_key(
            data["project_id"], data.get("parent_id"),
            is_reference=bool(data.get("is_reference")),
        )
    record = await create_record("documents", uid, data)
    if not data.get("is_reference"):
        from history_service import log_event
        await log_event(uid, user_id, user_name or "System", "created")
    # WHY: a creation primitive emits content_flushed when the row is BORN
    # with non-empty content (after strip). Why: every unindexed-at-birth document
    # in the coverage audit (184/858) traced to a write path that set content
    # and did not emit — patching each caller is per-member drift, and the class
    # grows with every new creation path. The emit sits AFTER create_record (not
    # before): create_with_unique_path retries the WHOLE call on a path collision,
    # and a pre-write emit would fire for a row that was never created. Empty
    # content emits nothing — a job whose only work is DELETE doc_chunks on a
    # chunk-less document makes the debounce queue lie about how much is pending.
    # Content arriving AFTER the create (backfills, transcription, restore) is the
    # post-create class: those callers own their own emit; do NOT drop this guard to reach
    # them — that trades three explicit emits for a job on every empty creation.
    if (data.get("content") or "").strip():
        await event_bus.emit("content_flushed", entity_type="doc", entity_id=uid,
                             project_id=data["project_id"])
    return record


async def _emit_rename_events(
    document_id: str, project_id, title: str, *, is_reference: bool,
) -> None:
    """Broadcast half of the rename core — the event pair keyed by row kind.

    Emit-shape asymmetry: the ref event key is reference_id, not document_id
    (the project_ws allowlist drops a wrong key SILENTLY).
    # WHY: a title change emits content_flushed — inside the rename core, NOT
    # the handler tail. Why: the document title is inside the embedded
    # breadcrumb and inside _chunk_hash, so a rename genuinely changes every
    # vector of the document; references emit on rename for the same reason
    # and the asymmetry was the bug. The tail also runs for patches carrying
    # no title, which would flush on unrelated field edits; an unchanged-title
    # flush is not — the hash makes it a no-op re-embed, and the same-title
    # exit in rename_document skips even that.
    """
    if is_reference:
        await event_bus.emit("reference_renamed", project_id=project_id,
                             reference_id=document_id, title=title)
        await event_bus.emit("content_flushed", entity_type="doc", entity_id=document_id,
                             project_id=project_id, is_reference=True)
    else:
        await event_bus.emit("document_renamed", project_id=project_id,
                             document_id=document_id, title=title)
        await event_bus.emit("content_flushed", entity_type="doc", entity_id=document_id,
                             project_id=project_id)


async def rename_document(*, document_id: str, title: str) -> dict:
    """Rename a document or reference — the SOLE rename path (rename core).

    Both REST PATCH surfaces delegate here; the UPDATE is identical for docs
    and references, only the broadcast differs, and the kind is read off the
    fetched row via is_ref_row (event routing, not a parameter — the same rule
    move_document_command applies to moves). project_id likewise comes off the
    row, never from a caller argument.

    Contract:
      1. fetch_one; gone (missing or soft-deleted) → 404 "Document not found";
      2. strip; empty → 400 "title is required" (vocabulary parity with
         collab_writes create);
      3. same-title short-circuit → no UPDATE, no events, unchanged=True
         (parity with _baseline_or_reject's equality exit);
      4. UPDATE title + updated_at (the STRIPPED title is what is stored);
      5. emit by kind: doc → document_renamed + content_flushed; ref →
         reference_renamed + content_flushed(is_reference=True).
    """
    row = await fetch_one("documents", document_id)
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    new = (title or "").strip()
    if not new:
        raise HTTPException(status_code=400, detail="title is required")
    is_reference = is_ref_row(row)
    if new == (row.get("title") or ""):
        return {
            "status": "renamed", "document_id": document_id, "title": new,
            "is_reference": is_reference, "unchanged": True,
        }
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET title = $v, updated_at = time::now()",
        {"id": document_id, "v": new},
    )
    await _emit_rename_events(
        document_id, row.get("project_id"), new, is_reference=is_reference,
    )
    return {
        "status": "renamed", "document_id": document_id, "title": new,
        "is_reference": is_reference, "unchanged": False,
    }


# --- Last-accessed-doc preferences --------------------------------------------

async def set_prefs_last_doc(db, user_id: str, project_id: str, document_id: str) -> None:
    """Persist a non-member viewer's last-accessed doc into user_preferences.

    # ARCH: read-merge-write (2 round trips), NOT a single UPSERT. Why: the
    # user_preferences row may already carry unrelated UI-state keys (sidebar,
    # panel widths, etc.) saved via /api/preferences. A single
    # `UPSERT ... SET preferences = preferences || {...}` does NOT deep-merge —
    # SurrealDB resolves the self-reference `preferences` to empty during the
    # matched-row SET evaluation, clobbering the existing object. So we must
    # read the full preferences, set the one key, and write the whole object back.
    # UPSERT matches the existing (user_id, project_id) row via idx_uprefs_unique.
    """
    existing = await db.query(
        "SELECT preferences FROM user_preferences "
        "WHERE user_id = $uid AND project_id = $pid",
        {"uid": user_id, "pid": project_id},
    )
    prefs = dict(existing[0].get("preferences") or {}) if existing else {}
    prefs["last_accessed_doc_id"] = document_id
    await db.query(
        "UPSERT user_preferences SET user_id = $uid, project_id = $pid, "
        "preferences = $prefs, updated_at = time::now()",
        {"uid": user_id, "pid": project_id, "prefs": prefs},
    )


async def get_prefs_last_doc(db, user_id: str, project_id: str) -> str | None:
    """Read a non-member viewer's last-accessed doc from user_preferences."""
    rows = await db.query(
        "SELECT preferences FROM user_preferences "
        "WHERE user_id = $uid AND project_id = $pid",
        {"uid": user_id, "pid": project_id},
    )
    if not rows:
        return None
    return (rows[0].get("preferences") or {}).get("last_accessed_doc_id")


def last_doc_rule(member_last_doc: str | None, prefs_last_doc: str | None) -> str | None:
    """Canonical last-doc resolution: member row wins, else user_preferences, else drop.

    # ARCH: single source of truth for the "member row → user_preferences → drop"
    # order used by get_project (single) and _get_member_maps (batched). Both
    # must apply the SAME predicate or the project list and the project detail
    # view would disagree for the same user/project.
    """
    return member_last_doc or prefs_last_doc


async def live_doc_ids(db, doc_ids: set[str]) -> set[str]:
    """Of `doc_ids`, the ones that still resolve to a live (non-soft-deleted) row.

    The liveness predicate behind the last-accessed pointer gate — see the
    INVARIANT on the drop branch in routes/projects.py._enrich_projects.
    Validating at READ (rather than clearing the pointer at delete) heals the
    ids already dangling and covers every way a document can stop resolving,
    not just the delete paths.
    """
    ids = {d for d in doc_ids if d}
    if not ids:
        return set()
    in_clause = ",".join(
        f"type::record('documents','{validate_record_id(d)}')" for d in sorted(ids)
    )
    rows = await db.query(
        f"SELECT id FROM documents WHERE id IN [{in_clause}] AND deleted_at IS NONE"
    )
    return {extract_id(r.get("id")) for r in (rows or []) if r.get("id")}


# --- Transclusion inlining + export -------------------------------------------

_REF_IMG_PATTERN = re.compile(r"!\[([^\]]*)\]\(ref:([^)]+)\)")

# SYSTEM: transclusion — export inlining. Matches every embed node `![alt](target)`;
# the target may be `ref:<id>`, `doc:<id>` or a bare document id. Text embeds (text
# references + documents) are replaced with the source's content as a native text block;
# image references are left for _inline_ref_images (base64). External image URLs and
# `note:` targets are left untouched.
_EMBED_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


async def _inline_transclusions(content: str, project_id: str) -> str:
    """Replace document / text-reference transclusion embeds with their text content.

    The project wall: `project_id` is the EXPORTING document's project; a
    target whose project differs is skipped (left verbatim) — see the
    INVARIANT(security) on the skip lines below.

    INVARIANT: a transcluded body is inlined as the source's RAW markdown text, one
    level deep — nested embeds inside it are left as-is (mirrors the one-level cap of the
    live editor; also breaks any cycle).  Why: capping at one level mirrors the editor's render depth and guarantees any embed cycle is broken; images are skipped so the later base64 pass (_inline_ref_images) still owns them. Image references are skipped here so the later
    base64 pass (_inline_ref_images) still handles them.
    """
    async def _resolve_text(target: str) -> str | None:
        if target.startswith("ref:"):
            rec = await fetch_one("documents", target[4:])
            if not rec or not is_ref_row(rec):
                return None
            # INVARIANT(security): a by-id target outside the reading document's
            # project is skipped. Why: doc:/ref: embeds survive cross-project
            # subtree moves (documents/move.py), so without the wall an exporter
            # would inline the BODY of a document the reading project's members
            # cannot open. Project equality is the wall — per-target access
            # resolution is deliberately absent (small trusted group; N×4
            # round-trips on the export path otherwise, audit N4).
            if rec.get("project_id") != project_id:
                return None
            # INVARIANT: a soft-deleted ref/doc never inlines into export. Why: the
            # editor band hides deleted sources, so export must match the live view.
            if rec.get("deleted_at"):
                return None
            # INVARIANT: mirror the editor's refToEntry — an image ref renders as an
            # image (leave for the base64 pass); ANY other ref with text content (a text
            # reference, or an audio/video ref carrying a transcription) inlines as text.  Why: export mirrors the editor's refToEntry — images stay for the base64 pass, while text/audio/video refs carrying a transcription inline their text so export keeps what the user sees.
            # Why: the user sees the transcription in the editor band, so export must keep
            # it; only image refs and media-without-text are non-text.
            if rec.get("media_type") == "image":
                return None
            body = rec.get("content")
            return body if (body and body.strip()) else None
        if target.startswith("doc:"):
            rec = await fetch_one("documents", target[4:])
        elif target.startswith("table:"):
            # A `table:` anchor is a doc-local table, never a document. In export it is
            # already GFM-expanded upstream; guard here so a stray anchor is left verbatim
            # instead of being mis-looked-up as a document id.
            return None
        elif NON_DOC_TARGET.match(target):
            return None
        else:
            rec = await fetch_one("documents", target)
        if not rec:
            return None
        # INVARIANT(security): same wall as the ref branch — a by-id target
        # outside the reading document's project is skipped (Why: see the ref
        # branch above).
        if rec.get("project_id") != project_id:
            return None
        if rec.get("deleted_at") or is_ref_row(rec):
            return None
        return rec.get("content") or ""

    # Resolve sequentially — these share the global Surreal connection (no gather).
    replacements: dict[str, str | None] = {}
    for m in _EMBED_PATTERN.finditer(content):
        target = m.group(2)
        if target not in replacements:
            replacements[target] = await _resolve_text(target)

    def _repl(m: re.Match) -> str:
        text = replacements.get(m.group(2))
        return m.group(0) if text is None else text

    return _EMBED_PATTERN.sub(_repl, content)


async def _inline_ref_images(content: str, project_id: str) -> str:
    """Replace `![alt](ref:<id>)` IMAGE links with base64 data URIs; drop other media.

    Image references store bytes on disk (files.py serve_file); pandoc cannot
    resolve the app's internal `ref:` scheme, so unresolved links export as
    nothing. Inline each referenced IMAGE as a data URI the converter embeds.

    The project wall mirrors _inline_transclusions: `project_id` is the
    EXPORTING document's project; a ref whose project differs is skipped.

    INVARIANT: base64 inlining is for images ONLY. Why: a non-image media reference
    (audio/video) embedded as `![alt](ref:id)` must NOT be base64-dumped into the export
    (pandoc would emit a broken/huge image); such media is dropped. Unresolvable refs
    (deleted/missing file) are left untouched.
    """
    import base64
    import mimetypes as _mimetypes

    from config import STORAGE_PATH

    ref_ids = {m.group(2) for m in _REF_IMG_PATTERN.finditer(content)}
    if not ref_ids:
        return content

    data_uris: dict[str, str] = {}
    drop_ids: set[str] = set()
    for ref_id in ref_ids:
        ref = await fetch_one("documents", ref_id)
        if not ref or not is_ref_row(ref):
            continue
        # INVARIANT(security): a by-id target outside the reading document's
        # project is skipped — the embed is left verbatim, indistinguishable
        # from an unresolvable ref (no existence oracle). Why: without the wall
        # a cross-project move would ship another project's image BYTES inside
        # the export. See _inline_transclusions for the full Why.
        if ref.get("project_id") != project_id:
            continue
        rel_path = ref.get("file_path", "")
        if not rel_path:
            continue
        abs_path = (STORAGE_PATH / rel_path).resolve()
        if not abs_path.is_relative_to(STORAGE_PATH.resolve()) or not abs_path.is_file():
            continue
        mime = ref.get("file_meta", {}).get("mime_type") or _mimetypes.guess_type(str(abs_path))[0] or "application/octet-stream"
        if not mime.startswith("image/"):
            # Non-image media (audio/video): skip — drop the embed from the export.
            drop_ids.add(ref_id)
            continue
        b64 = base64.b64encode(abs_path.read_bytes()).decode("ascii")
        data_uris[ref_id] = f"data:{mime};base64,{b64}"

    def _repl(m: re.Match) -> str:
        if m.group(2) in drop_ids:
            return ""
        uri = data_uris.get(m.group(2))
        if uri is None:
            return m.group(0)
        # Alt carries the app's `alt|WxH` dimension encoding — translate to
        # pandoc's `{width=Wpx height=Hpx}` attributes so size survives export.
        alt = m.group(1)
        attrs = ""
        dim_match = re.search(r"\|(\d+)x(\d+)$", alt)
        if dim_match:
            alt = alt[: dim_match.start()]
            attrs = f"{{width={dim_match.group(1)}px height={dim_match.group(2)}px}}"
        return f"![{alt}]({uri}){attrs}"

    return _REF_IMG_PATTERN.sub(_repl, content)


async def export_document(document_id: str, format: str = "pdf", checkpoint_id: str | None = None):
    """Export document content as PDF, DOCX (via converter) or Markdown (raw).

    Reads live content from an active collab session (ydoc_store) first,
    falling back to the DB row. Returns a binary Starlette Response with
    Content-Disposition. Access (require_document_read) is enforced by the route
    wrapper BEFORE this is called.

    When `checkpoint_id` is given, exports that SNAPSHOT's content instead of the
    live document — resolved from blob-ref with an inline fallback (same scheme as
    GET /api/checkpoints/{id}), and never touches the collab session. Used by the
    History panel Download menu while a snapshot preview is open.
    """
    if format not in ("pdf", "docx", "md"):
        raise HTTPException(status_code=400, detail="format must be pdf, docx or md")

    record = await fetch_one("documents", document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Document not found")
    if record.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Document not found")

    if checkpoint_id:
        # Export the snapshot, not the live document. The snapshot is read-only
        # historical content — never consult the active collab session for it.
        cp = await fetch_one("checkpoints", checkpoint_id)
        if not cp or cp.get("document_id") != document_id:
            raise HTTPException(status_code=404, detail="Checkpoint not found")
        # Export the snapshot via the single shared helper. A post-nullout row
        # whose blob is gone has no inline fallback → BlobUnavailable → 502 (the
        # frontend DownloadMenu already surfaces a toast on non-200 export responses).
        try:
            content = await resolve_checkpoint_content(cp)
        except BlobUnavailable:
            logger.warning("Blob resolution failed for checkpoint %s export", checkpoint_id, exc_info=True)
            raise HTTPException(status_code=502, detail=BLOB_UNAVAILABLE_DETAIL)
    else:
        content = record.get("content") or ""

        from collab.registry import get_active_session
        session = get_active_session("doc", document_id)
        if session and session.clients:
            # Live branch: session.content holds raw `![label](table:id)` anchors; expand
            # them from the session's Doc. The DB branch above is already expanded (flush /
            # compact persist the GFM form), so it needs no expansion here.
            from table_serialize import expand_tables
            content = expand_tables(session.ydoc)

    # see SYSTEM: markdown content normalize — export entry point. Collapse irregular
    # post-marker spacing so EVERY exported document (esp. raw `md`, handed to the user
    # verbatim) is clean regardless of how the content was authored — manual collab
    # typing bypasses the write-side normalizers. Spacing-only (no reflow): never join
    # the author's intentional line breaks in their own document on export.
    from markdown_normalize import normalize_list_spacing
    content = normalize_list_spacing(content)

    # see SYSTEM: transclusion — inline text embeds (docs + text refs) as native text BEFORE
    # the image pass, so a text-ref embed becomes its content and only image refs remain
    # for base64 inlining. Both inliners carry the project wall; the wall's project
    # comes from the EXPORTED record itself (loaded above), never from the caller.
    content = await _inline_transclusions(content, record["project_id"])
    content = await _inline_ref_images(content, record["project_id"])

    title = record.get("title", "document")
    ext = format

    if format == "md":
        export_bytes = content.encode("utf-8")
        media_type = "text/markdown; charset=utf-8"
    else:
        import http_clients
        from docx_convert import WEB_CONVERTER_TIMEOUT

        from config import CONVERTER_URL, EXPORT_CONVERTER_TIMEOUT_S

        # The shared "docx" pool client (web timeout) with the export's own
        # longer per-request budget — the pool must not pin the export latency.
        client = http_clients.get_http_client("docx", timeout=WEB_CONVERTER_TIMEOUT)
        resp = await client.post(
            f"{CONVERTER_URL}/export",
            data={"markdown": content, "format": format},
            timeout=EXPORT_CONVERTER_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Export failed: {resp.text[:200]}")
        export_bytes = resp.content
        media_type = (
            "application/pdf" if format == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    ascii_fallback = f"document.{ext}"
    encoded_name = quote(title.encode("utf-8"), safe="")
    content_disposition = f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_name}.{ext}"

    from starlette.responses import Response as StarletteResponse
    return StarletteResponse(
        content=export_bytes,
        media_type=media_type,
        headers={"Content-Disposition": content_disposition},
    )


# --- Unique-path creation helpers ---------------------------------------------

async def generate_unique_title(project_id: str) -> tuple[str, str]:
    """Generate a unique 'Untitled N' title and path slug for a project.

    Reference-documents (is_reference=true) are excluded from collision checks:
    their synthetic _ref/<uuid>.md paths and ref-titles must not block users from
    creating regular documents named 'Untitled' or with a colliding title.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT title, path, deleted_at FROM documents "
        "WHERE project_id = $pid AND is_reference = false",
        {"pid": project_id},
    )
    existing_titles = {r["title"].lower() for r in (rows or []) if r.get("deleted_at") is None}
    existing_paths = {r["path"].lower() for r in (rows or [])}

    title = "Untitled"
    path = "untitled.md"
    counter = 1
    while title.lower() in existing_titles or path in existing_paths:
        title = f"Untitled {counter}"
        path = f"untitled-{counter}.md"
        counter += 1
    return title, path


async def generate_path_for_title(project_id: str, title: str) -> str:
    """Derive a unique path slug from the provided title."""
    slug = re.sub(r'[^\w]+', '-', re.sub(r'\.[^.]+$', '', title).lower()).strip('-') or 'untitled'
    db = await get_db()
    existing = {r["path"].lower() for r in (await db.query(
        "SELECT path FROM documents WHERE project_id = $pid AND is_reference = false",
        {"pid": project_id},
    ) or [])}
    path, n = f"{slug}.md", 1
    while path in existing:
        path, n = f"{slug}-{n}.md", n + 1
    return path


async def assert_parent_valid(parent_id: str | None, project_id: str) -> dict | None:
    """Validate a create/move target parent in a SINGLE fetch.

    Enforces the full parent invariant set:
      - 404 if the parent is missing/deleted (no dangling parent_id);
      - 404 if the parent belongs to a different project (cross-project);
      - 400 if the parent is a reference document.

    # INVARIANT(persisted): documents.parent_id never points at an is_reference=true row, never
    # crosses projects, and never dangles.  Why: a dangling or cross-project parent breaks the tree and a reference-as-parent inverts the derived/real relationship; the schema event (documents_parent_check) is the DB-level fence, and this guard surfaces its failure as a friendly HTTP error. The schema-level documents_parent_check
    # event enforces the reference rule at the DB layer too — this route guard
    # surfaces a friendly HTTP error instead of letting the event raise on the SDK
    # boundary.
    # ARCH: a missing AND a cross-project parent BOTH surface as a uniform 404
    # (never a distinct 403). Why: a caller with full access to one project must
    # not be able to probe arbitrary record-ids and distinguish "no such document"
    # from "document exists in another project" — that would be a cross-project
    # existence oracle. Both agent and REST create/move go through this single
    # helper so the two paths can never drift.
    """
    if not parent_id:
        return None
    parent = await fetch_one("documents", parent_id)
    if (not parent or parent.get("deleted_at")
            or parent.get("project_id") != project_id):
        raise HTTPException(status_code=404, detail="Parent document not found")
    if is_ref_row(parent):
        raise HTTPException(
            status_code=400, detail="Reference documents cannot be parents",
        )
    return parent


async def ancestor_chain(doc_id: str) -> list[str]:
    """Return the ancestor ids of `doc_id` (inclusive of itself), root-first.

    Used for the move cycle check (a doc may not become its own descendant).
    Bounded by SurrealDB's depth; a missing parent short-circuits to root.
    """
    chain: list[str] = []
    current = doc_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        row = await fetch_one("documents", current)
        if not row:
            break
        current = row.get("parent_id") or ""
    return chain


async def assert_no_cycle(document_id: str, new_parent_id: str | None) -> None:
    """400 when `new_parent_id` is the document itself or one of its descendants.

    # INVARIANT(corruption): every re-parent (UI PATCH and agent move) runs this
    # before writing parent_id. Why: a subtree moved into itself becomes a rootless
    # loop that vanishes from the tree; the picker's client-side exclusion is not a
    # guard for the server.
    """
    if not new_parent_id:
        return
    if new_parent_id == document_id:
        raise HTTPException(
            status_code=400, detail="A document cannot be its own parent",
        )
    if document_id in await ancestor_chain(new_parent_id):
        raise HTTPException(
            status_code=400,
            detail="Cannot move a document into its own subtree (cycle)",
        )


async def resolve_reference_host(document_id: str | None, project_id: str) -> str:
    """Resolve a reference's host (parent_id) for the project-level intent.

    "Project level" has ONE representation: parent_id = the project's index_doc_id. An
    empty/None document_id (the picker's '' sentinel, or create-without-host) means
    project level and is normalized to index_doc_id. A non-empty document_id is a real
    host and is returned unchanged.

    # INVARIANT (reference-host): paired with the documents_reference_parent_check schema
    # event, this guarantees every reference written via the public API has a real host —
    # a no-host row can never leave the REST layer. A project missing index_doc_id is a
    # corrupted state (it is set once at creation and never mutated — projects.py), so a
    # project-level create against such a project is a real error → raise loudly, never
    # silently write a no-host reference (which the event would abort anyway, but a clear
    # 400 beats an opaque event THROW at the SDK boundary).
    """
    if document_id:
        return document_id
    project = await fetch_one("projects", project_id)
    index_doc_id = (project or {}).get("index_doc_id")
    if not index_doc_id:
        raise HTTPException(
            status_code=400,
            detail="Project has no index document; cannot place a project-level reference",
        )
    return index_doc_id


async def find_reference_by_idempotency_key(project_id: str, key: str) -> dict | None:
    """Replay lookup for idempotent uploads: the live reference created under key.

    # ARCH: SELECT-before-create dedup guard (SYSTEM: recording-cache). Covers
    # the lost-2xx sequential replay; the UNIQUE index idx_documents_idem
    # hardens the simultaneous two-tab window this SELECT cannot see — the race
    # loser is served by the upload routes' catch-reselect around the create.
    Returns the raw row (RecordID id — callers extract_id it), None on miss.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT * FROM documents WHERE project_id = $project AND idempotency_key = $key "
        "AND deleted_at = NONE LIMIT 1",
        {"project": project_id, "key": key},
    )
    return rows[0] if rows else None


async def create_reference_row(
    *, ref_id: str, project_id: str, host_id: str, title: str, media_type: str,
    content: str | None = None, source_url: str | None = None,
    processing_status: str | None = None, file_path: str | None = None,
    file_meta: dict | None = None, idempotency_key: str | None = None,
    created_by: str | None = None, created_by_name: str | None = None,
) -> dict:
    """Create a reference-document row + emit reference_created (single chokepoint).

    Every reference-creation entry point (REST /api/references, file upload, agent
    collab write) delegates the canonical dict build + create_record + emit here so
    the four near-verbatim shapes cannot drift. The caller OWNS host resolution +
    error-wrapping; this takes a RESOLVED host_id verbatim and never calls
    resolve_reference_host (the agent collab path requires a real host). Optional
    fields are written ONLY when non-None; content defaults to "".

    created_by / created_by_name: the reference author, shown on the RefCard meta row
    for someone ELSE's reference. The name is denormalized to avoid an N+1 users join
    on LIST (WHY in schema.surql `created_by_name`). Unset for impersonal/legacy
    paths → the UI renders no author segment (no "System" label).

    # WHY: exactly one reference_created emit per creation. Why: a missed
    # caller-side emit delete after delegating here double-emits → the project collab
    # bridge inserts the ref twice in the panel.
    """

    # Refs get a key at birth like every live row — the TOP key of the host's
    # reference group (newest-first: a new reference lands at the top of its
    # group in the panel, key_before semantics shared with docs).
    sort_key = await top_sibling_key(project_id, host_id, is_reference=True)
    row: dict = {
        "project_id": project_id, "parent_id": host_id, "title": title,
        "content": content or "", "path": f"_ref/{ref_id}.md",
        "is_index": False, "is_reference": True, "media_type": media_type,
        "sort_key": sort_key,
    }
    if source_url is not None:
        row["source_url"] = source_url
    if processing_status is not None:
        row["processing_status"] = processing_status
    if file_path is not None:
        row["file_path"] = file_path
    if file_meta is not None:
        row["file_meta"] = file_meta
    # Upload idempotency (SYSTEM: recording-cache): the recording cache's session
    # id — written only when the caller sent one; keyless uploads stay legacy.
    if idempotency_key is not None:
        row["idempotency_key"] = idempotency_key
    # Written independently (not as a pair): a caller with a name but no id must not
    # silently drop the name — though every current caller passes both or neither.
    if created_by is not None:
        row["created_by"] = created_by
    if created_by_name is not None:
        row["created_by_name"] = created_by_name
    record = await create_record("documents", ref_id, row)
    await event_bus.emit(
        "reference_created", project_id=project_id, reference_id=ref_id,
        title=title, document_id=host_id,
        created_by=created_by, created_by_name=created_by_name,
    )
    # WHY: same creation-primitive contract as create_document — emit
    # content_flushed iff the row is born with non-empty content. Why: reference
    # creation funnels here from four callers (REST references, upload-markdown,
    # save_upload, agent collab write), and none of them emitted; 24 unembedded
    # markdown refs + the unembedded share of the 57 audio transcripts in the
    # coverage audit trace to exactly this gap. The agent caller
    # (create_reference_via_collab) delegates here, so it now emits once via the
    # primitive. save_upload creates with EMPTY content on purpose (binary
    # upload, markdown backfilled later by files_mcp_upload._store_markdown) —
    # that backfill caller owns its own emit (the post-create class).
    if (content or "").strip():
        await event_bus.emit("content_flushed", entity_type="doc", entity_id=ref_id,
                             project_id=project_id)
    return record


async def create_with_unique_path(
    doc_id: str, payload: dict, *, project_id: str, title: str,
    user_id: str | None = None, user_name: str | None = None,
) -> None:
    """Create a non-reference document, re-deriving `path` on a UNIQUE collision.

    # WHY: agent + REST create share this path-derivation+retry so the  Why: two concurrent same-title creates both read 'no existing path' (TOCTOU in generate_path_for_title); the shared retry makes the loser derive the next free path instead of 500-ing on the unique constraint.
    # TOCTOU in generate_path_for_title's read-then-decide loop (two concurrent
    # same-title creates both observe no existing path) resolves deterministically
    # instead of 500-ing on the second writer. After the first insert commits, a
    # re-run of generate_path_for_title sees the taken path and yields a suffixed
    # slug, so a bounded retry converges.
    """
    attempts = 0
    while True:
        payload["path"] = await generate_path_for_title(project_id, title)
        try:
            await create_document(doc_id, payload, user_id=user_id, user_name=user_name)
            return
        except Exception:
            attempts += 1
            # Retry on the (project_id, path) UNIQUE collision (concurrent same-title
            # creates) — a fresh slug converges. Capped at 2 attempts so a non-collision
            # persistent failure surfaces after a single retry, bounding DB amplification.
            if attempts >= 2:
                raise
