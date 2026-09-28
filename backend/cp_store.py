"""Content-addressable blob store for checkpoint data.

# SYSTEM: cp-store — content-addressable, compressed, immutable blob I/O
# ARCH: Single authority for all checkpoint blob reads/writes. Dedup is global
#   (id = sha256 of the original content). Only 'full' compressed blobs are
#   stored — delta chains are not needed at this data volume.
# INVARIANT(data-loss): blobs are immutable and never deleted; identical content is stored
#   once (id = sha256 of the RECONSTRUCTED full content, never of the stored diff).
#   Why: data layer must preserve every version forever (user rule); dedup keyed
#   on full-content hash is what bounds growth.
# INVARIANT(corruption): content_ref == content_hash whenever both are set (same sha256 of
#   the full content). Why: they are one identity split across two fields only to
#   let null content_ref mark un-migrated legacy rows; drift means a wiring bug.
"""
import hashlib
import logging
from uuid import uuid4

import zstandard
from content_hash import hash_content

from db import (
    _extract_query_raw_errors,
    create_record,
    fetch_one,
    get_db,
    serialize_record,
)

logger = logging.getLogger(__name__)

# Detail string for HTTP 502 when a checkpoint blob cannot be resolved and no
# inline fallback exists. The frontend (HistoryPanel / SnapshotPreviewBanner /
# DownloadMenu) depends on this exact value. Single source — see resolve_checkpoint_content.
BLOB_UNAVAILABLE_DETAIL = "checkpoint_blob_unavailable"


class BlobUnavailable(Exception):
    """Raised by resolve_checkpoint_content when a checkpoint's content_ref cannot be
    resolved AND no inline legacy content exists to fall back to. Callers map this:
    HTTP read/restore/export paths → HTTPException(502, detail=BLOB_UNAVAILABLE_DETAIL);
    validate_checkpoint_integrity → the diagnostic `reason: blob_unreadable` dict."""

_zstd_compressor = zstandard.ZstdCompressor()
_zstd_decompressor = zstandard.ZstdDecompressor()


# hash_content is re-exported from the content_hash leaf (single source); kept here
# for backward compatibility with existing `from cp_store import hash_content` callers.


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _raise_on_query_raw_error(raw, *, context: str) -> None:
    """Inspect a surrealdb query_raw() response and raise on any failure.

    surrealdb 2.0.0's query() RAISES on errors (Pre-flight outcome A — see module
    docstring), so the historical "silent blob loss" does not occur today. This
    helper is the defence-in-depth contract: query_raw NEVER raises; it returns a
    top-level `error` dict (parse/transport) or a per-statement `result` list whose
    entries carry `status == "ERR"`. Both are surfaced via the SHARED extractor in
    db.contract (also used by run_in_transaction) so the failure-mode contract cannot
    drift between the two write paths.
    """
    errors = _extract_query_raw_errors(raw)
    if errors:
        raise RuntimeError(
            f"{context}: " + "; ".join(
                str(e.get("result") if e.get("result") is not None else e.get("message", e))
                for e in errors
            )
        )


async def resolve_checkpoint_content(record: dict) -> str:
    """Resolve a checkpoint row's text: blob-ref preferred, inline fallback for legacy rows.

    Single source of the "no silent degradation" read contract. Returns the inline
    `content` (legacy row) when `content_ref` is unset; otherwise resolves the blob.
    On a blob-resolution failure: falls back to inline content ONLY when a legacy row
    actually carries it; otherwise raises `BlobUnavailable` (never returns "" for a
    post-nullout row whose blob is gone). All read/restore/export/validate paths go
    through this so the gate condition + detail cannot drift across call sites.
    """
    content_ref = record.get("content_ref")
    if not content_ref:
        return record.get("content") or ""
    try:
        return await get_content(content_ref)
    except Exception:
        if record.get("content"):
            return record["content"]
        raise BlobUnavailable(content_ref)


async def put_content(text: str) -> str:
    """Store text as a compressed blob, deduplicating on content hash.

    Returns the sha256 hex digest (used as both blob id and content_hash).
    If the blob already exists, this is a no-op (pure dedup).

    # INVARIANT(data-loss): a blob-write failure must NEVER produce a checkpoint row pointing
    # at a missing blob. Why: the only copy of "before-loss" content lives in the
    # blob; a checkpoint row referencing a never-written blob is silent data loss.
    # The UPSERT therefore goes through query_raw + explicit status check (hardening
    # — see module docstring / Pre-flight outcome A) so a write error raises instead
    # of returning the hash. Downstream (create_checkpoint) propagates the raise →
    # manual POST 500s, arq backup tasks fail terminally (no spam-retry).
    """
    raw = text.encode("utf-8")
    content_hash = _hash_bytes(raw)
    db = await get_db()

    # Cheap fast-path: skip recompression when the (immutable) blob already exists.
    # Stays on db.query() — a swallowed failure here just falls through to the UPSERT
    # (safe: the UPSERT is idempotent on the hash id, so a redundant write is harmless).
    existing = await db.query(
        "SELECT id FROM type::record('cp_blobs', $hash)",
        {"hash": content_hash},
    )
    if existing:
        return content_hash

    # WHY: UPSERT, not SELECT-then-CREATE — content-addressable write must be a
    # single atomic statement. Two concurrent puts of identical content otherwise
    # both pass the SELECT and race a duplicate CREATE on the same id; the second
    # raises "record already exists", which db.query() silently swallows. UPSERT on
    # the hash id is an idempotent no-op when the (immutable) blob already exists.
    compressed = _zstd_compressor.compress(raw)
    result = await db.query_raw(
        "UPSERT type::record('cp_blobs', $hash) CONTENT {"
        "  kind: 'full',"
        "  base_ref: NONE,"
        "  depth: 0,"
        "  data: $data,"
        "  size: $size"
        "}",
        {"hash": content_hash, "data": compressed, "size": len(raw)},
    )
    _raise_on_query_raw_error(result, context="cp_blobs UPSERT")
    return content_hash


async def get_content(content_ref: str) -> str:
    """Resolve a blob reference to the original text.

    Decompresses the blob identified by content_ref (sha256 hex digest).
    """
    db = await get_db()
    rows = await db.query(
        "SELECT data, kind FROM type::record('cp_blobs', $ref)",
        {"ref": content_ref},
    )
    if not rows:
        raise ValueError(f"cp_blobs:{content_ref} not found")
    row = rows[0]
    blob_data = row["data"]
    if isinstance(blob_data, str):
        blob_data = blob_data.encode("utf-8")
    return _zstd_decompressor.decompress(blob_data).decode("utf-8")


# ── Unified checkpoint writer ─────────────────────────────────────────────────


async def _checkpoint_blob_fields(content: str, tables_json: str | None) -> dict:
    """Compute the blob + hash fields for a checkpoint WITHOUT writing the row.

    Performs the content blob write (put_content, outside any transaction — blob I/O is
    global/dedup, not transactional) and returns the field dict to merge into a CREATE or
    UPSERT. `tables_json` is hashed inline (NOT stored in a blob — small; keeps restore a
    single read). Split out so restore_checkpoint can run the row write inside its atomic
    transaction with the documents restore (see create_checkpoint for the non-tx paths).
    """
    content_ref = await put_content(content)
    fields: dict = {"content_ref": content_ref, "content_hash": content_ref}
    if tables_json is not None:
        fields["tables_json"] = tables_json
        fields["tables_hash"] = hash_content(tables_json)
    return fields


async def checkpoint_row_fields(
    content: str,
    tables_json: str | None,
    *,
    document_id: str,
    label: str | None,
    comment: str | None,
    created_by: str | None,
) -> dict:
    """Build the FULL checkpoint field dict — the single source for every row write.

    Returns row-identity fields (document_id, label, comment, created_by) merged with the
    blob fields from `_checkpoint_blob_fields` (content_ref + content_hash, and
    tables_json + tables_hash when captured). Performs the content blob write (outside any
    transaction). Post C3-A this dict does NOT contain inline `content`.

    # WHY (shared field set): BOTH checkpoint row writers — `create_checkpoint`
    # (CREATE / UPSERT) and `restore_checkpoint`'s `_backup` row (UPDATE SET / CREATE
    # CONTENT) — consume this builder. Why: the restore path used to hand-build the field
    # list in two separate branches, duplicating what the writer owns; adding a field to
    # the writer silently missed the restore backup. A field added here now appears in
    # every checkpoint row including the before-restore safety backup.
    """
    fields: dict = {
        "document_id": document_id,
        "label": label or "",
        "comment": comment,
        "created_by": created_by,
    }
    fields.update(await _checkpoint_blob_fields(content, tables_json))
    return fields


async def create_checkpoint(
    *,
    document_id: str,
    content: str,
    tables_json: str | None,
    label: str | None,
    comment: str | None,
    created_by: str | None,
    id: str | None = None,
) -> dict:
    """Single authority for EVERY checkpoint creation path.

    # see SYSTEM: cp-store — unified checkpoint writer.
    # ARCH: all 7 creation paths (manual, before-restore, 4 autos, agent pre-edit) go
    #   through this writer so they persist an identical field set: content_ref +
    #   content_hash (blob + integrity) and, when captured, tables_json + tables_hash
    #   (full table state). This also fixes the pre-existing `agent-auto` gap (it used to
    #   skip the blob + hash entirely).
    # INVARIANT(corruption): content_ref == content_hash (cp_store blob identity). tables_hash, when  Why: content_ref and content_hash name the same cp_store blob; a mismatch restores the wrong payload, and tables_hash (== sha256(tables_json) when captured) must agree or the tables restore diverges.
    #   present, == sha256(tables_json). `tables_json is None` means "not captured"
    #   (legacy-safe) and omits both tables fields — restore then leaves the tables map
    #   untouched. Why None-not-"{}": distinguishes "no tables on the doc" ("{}", hashed)
    #   from "this path didn't capture tables" (None, legacy).
    # INVARIANT(corruption): this writer NEVER touches a Y.Doc. Table capture happens on the hot path
    #   (web process) where the live ydoc lives and is passed in as a string; the arq
    #   worker only forwards the captured string.  Why: the worker has no live CollabSession; loading a Y.Doc there would read a stale snapshot, so capture happens on the web hot path and the worker only forwards the string. Loading a Y.Doc on the worker would read
    #   a newer state than the captured text → anchor↔table-id drift.
    # INVARIANT(data-loss): NEVER write inline `content`. Why: content lives ONLY in the content_ref
    #   blob (dedup-keyed, immutable); the inline column is legacy-only for pre-migration
    #   rows. Writing it double-stores every document body (~2× the blob payload) and
    #   re-feeds the null-out migration that is supposed to converge. All readers resolve
    #   via content_ref first with inline fallback, so omitting it on new rows is safe.

    The row id is `id` when provided (deterministic uuid5 — last-session upsert collapses
    concurrent triggers to one row), else a fresh uuid4.

    Event emission stays at the call sites (they already emit `checkpoint_created`); this
    writer has no event_bus dependency, keeping the import graph acyclic (auto_backup →
    cp_store, never the reverse).

    Returns the serialized checkpoint row (WITHOUT user_name — the caller attaches it when
    relevant, e.g. last-session). The caller then re-fetches if it needs the full row.
    """
    fields = await checkpoint_row_fields(
        content, tables_json,
        document_id=document_id, label=label, comment=comment, created_by=created_by,
    )

    if id is None:
        record_id = str(uuid4())
        await create_record("checkpoints", record_id, fields)
    else:
        record_id = id
        db = await get_db()
        # UPSERT on the deterministic id (mirrors the last-session contract): a subsequent
        # trigger for the same (doc, editor) updates the row in place, keeping its
        # checkpoint_id stable so the panel reconciles by id.
        set_parts = ", ".join(
            f"{k} = ${k}" for k in fields
        ) + ", created_at = time::now(), deleted_at = NONE"
        params = {"id": record_id, **fields}
        await db.query(
            f"UPSERT type::record('checkpoints', $id) SET {set_parts}",
            params,
        )

    record = await fetch_one("checkpoints", record_id)
    if not record:
        raise RuntimeError(f"create_checkpoint: checkpoints:{record_id} not found after write")
    return serialize_record(record, "checkpoint_id")

