"""Agent document-history executors: list / read / diff checkpoints of one document.

History IS the `checkpoints` table (see SYSTEM: checkpoint-routes). Every historical
text goes through `doc_state.snapshot_text` — the one GFM projection — and every
checkpoint id is bound to the document it is addressed through.
"""
import difflib

import settings
from auto_backup import BACKUP_LABELS
from cp_store import (
    BLOB_UNAVAILABLE_DETAIL,
    BlobUnavailable,
    resolve_checkpoint_content,
)
from fastapi import HTTPException

import config
from agent import doc_state
from agent.readonly_executors import (
    _build_read_result,
    _read_gate_error,
    _ReadTarget,
    _resolve_read_document_target,
)
from db import extract_id, fetch_many, fetch_one, get_db, iso_str

_FIXED_LABELS = frozenset(b.value for b in BACKUP_LABELS)
_CHECKPOINT_NOT_FOUND = "Checkpoint not found"


async def gated_target(
    *, doc_id: str, project_id: str, user: dict, scope_root: str | None,
) -> _ReadTarget | dict:
    """The read_document gate chain (resolve → RBAC + scope + lock); soft error dict on refusal."""
    target = await _resolve_read_document_target(doc_id=doc_id)
    if isinstance(target, dict):
        return target
    err = await _read_gate_error(
        resolved=target.resolved, doc=target.doc, project_id=project_id,
        user=user, scope_root=scope_root,
    )
    return {"error": err} if err else target


async def owned_checkpoint(checkpoint_id: str, document_id: str) -> dict | None:
    """The live checkpoint row, or None when missing, deleted or of another document.

    # INVARIANT(security): a checkpoint is served only through ITS OWN document.
    # Why: every gate runs on `document_id`; a foreign checkpoint id accepted here
    # would read another document's history past that document's RBAC/scope.
    """
    cp = await fetch_one("checkpoints", checkpoint_id) if checkpoint_id else None
    if not cp or cp.get("deleted_at") or cp.get("document_id") != document_id:
        return None
    return cp


async def checkpoint_text(cp: dict) -> str:
    """The checkpoint as GFM (tables inlined); 502 when its blob is gone — never ""."""
    try:
        content = await resolve_checkpoint_content(cp)
    except BlobUnavailable:
        raise HTTPException(status_code=502, detail=BLOB_UNAVAILABLE_DETAIL)
    return doc_state.snapshot_text(content, cp.get("tables_json"))


async def _current_text(document_id: str) -> str:
    """The live state through the SAME projection as a checkpoint."""
    content, tables_json = await doc_state.resolve_live_doc_state(document_id)
    return doc_state.snapshot_text(content, tables_json)


async def _row_text(cp: dict) -> str | None:
    """A list row's text; None when its blob is gone — flagged per row, never "".

    WHY: one lost blob must not 502 the whole page and hide every restorable version.
    """
    try:
        return await checkpoint_text(cp)
    except HTTPException:
        return None


def _kind(label: str | None) -> str:
    """Fixed BackupLabel value as-is; any other label is a human's named version."""
    return label if label in _FIXED_LABELS else "manual"


def _version_ref(cp: dict) -> dict:
    return {
        "checkpoint_id": extract_id(cp["id"]),
        "created_at": iso_str(cp.get("created_at"), passthrough_str=True),
        "kind": _kind(cp.get("label")),
    }


def _line_stats(older: str, newer: str) -> tuple[int, int]:
    """(added, removed) line counts from older to newer."""
    added = removed = 0
    for line in difflib.unified_diff(older.splitlines(), newer.splitlines(), n=0, lineterm=""):
        if line.startswith(("+++", "---", "@@")):
            continue
        added += line.startswith("+")
        removed += line.startswith("-")
    return added, removed


def _version_entry(
    row: dict, text: str | None, older: str | None, names: dict[str, dict],
) -> dict:
    """One list row; null stats + `content_unavailable` when its text is unreadable."""
    both = text is not None and older is not None
    added, removed = _line_stats(older, text) if both else (None, None)
    cb = row.get("created_by")
    entry = {
        **_version_ref(row),
        "author": (names.get(cb, {}).get("name") or "System") if cb else "System",
        "comment": row.get("comment"),
        "chars": len(text) if text is not None else None,
        "lines_added": added,
        "lines_removed": removed,
    }
    if text is None:
        entry["content_unavailable"] = True
    if entry["kind"] == "manual":
        entry["label"] = row.get("label")
    return entry


async def list_document_versions_tool(
    *, doc_id: str, project_id: str, user: dict, scope_root: str | None,
    limit: int, before_checkpoint_id: str | None,
) -> dict:
    """Newest-first page of live checkpoints with per-row change stats."""
    target = await gated_target(
        doc_id=doc_id, project_id=project_id, user=user, scope_root=scope_root,
    )
    if isinstance(target, dict):
        return target
    before = None
    if before_checkpoint_id:
        before = await owned_checkpoint(before_checkpoint_id, target.resolved)
        if before is None:
            return {"error": _CHECKPOINT_NOT_FOUND}
    db = await get_db()
    # WHY limit+1: the oldest row of the page needs its next-older neighbour for stats.
    rows = await db.query(
        "SELECT id, content, content_ref, tables_json, label, comment, created_by, "
        "created_at FROM checkpoints WHERE document_id = $did AND deleted_at IS NONE "
        # WHY (created_at, id) cursor: rows sharing the cursor's timestamp are not skipped.
        + ("AND (created_at < $before OR (created_at = $before AND id < $bid)) "
           if before else "")
        + "ORDER BY created_at DESC, id DESC LIMIT $lim",
        {"did": target.resolved, "lim": limit + 1,
         "before": before.get("created_at") if before else None,
         "bid": before.get("id") if before else None},
    ) or []
    texts = [await _row_text(r) for r in rows]
    user_ids = list({r["created_by"] for r in rows if r.get("created_by")})
    names = await fetch_many("users", user_ids) if user_ids else {}
    versions = [
        _version_entry(row, texts[i], texts[i + 1] if i + 1 < len(texts) else None, names)
        for i, row in enumerate(rows[:limit])
    ]
    result: dict = {"document_id": target.resolved, "versions": versions}
    if len(rows) > limit:
        result["next_before_checkpoint_id"] = versions[-1]["checkpoint_id"]
    return result


async def read_document_version(
    target: _ReadTarget, *, version: str, offset: int, limit: int | None,
) -> dict:
    """A historical read: the checkpoint's GFM projection, sliced like a live read."""
    cp = await owned_checkpoint(version, target.resolved)
    if cp is None:
        return {"error": _CHECKPOINT_NOT_FOUND}
    result = _build_read_result(
        target.doc, target.resolved, await checkpoint_text(cp),
        offset=offset, limit=limit, slice_chars=config.AGENT_READ_SLICE_CHARS,
        max_chars=await settings.get("AGENT_READ_MAX_CHARS"),
    )
    result["version"] = _version_ref(cp)
    result["read_only"] = True
    return result


async def diff_document_versions_tool(
    *, doc_id: str, project_id: str, user: dict, scope_root: str | None,
    from_checkpoint_id: str, to: str,
) -> dict:
    """Unified diff from a checkpoint to another checkpoint or the current state."""
    target = await gated_target(
        doc_id=doc_id, project_id=project_id, user=user, scope_root=scope_root,
    )
    if isinstance(target, dict):
        return target
    from_cp = await owned_checkpoint(from_checkpoint_id, target.resolved)
    to_cp = None if to == "current" else await owned_checkpoint(to, target.resolved)
    if from_cp is None or (to != "current" and to_cp is None):
        return {"error": _CHECKPOINT_NOT_FOUND}
    old = await checkpoint_text(from_cp)
    new = await _current_text(target.resolved) if to_cp is None else await checkpoint_text(to_cp)
    to_ref = {"checkpoint_id": "current"} if to_cp is None else _version_ref(to_cp)
    # WHY: a final line without "\n" would glue onto the next diff line.
    diff = "".join(difflib.unified_diff(
        [ln + "\n" for ln in old.splitlines()], [ln + "\n" for ln in new.splitlines()],
        fromfile=from_checkpoint_id, tofile=to_ref["checkpoint_id"], n=3,
    ))
    added, removed = _line_stats(old, new)
    truncated = len(diff) > config.BACKUP_DIFF_CAP
    return {
        "document_id": target.resolved,
        "from": _version_ref(from_cp),
        "to": to_ref,
        "identical": old.splitlines() == new.splitlines(),
        "diff": diff[:config.BACKUP_DIFF_CAP],
        "truncated": truncated,
        "lines_added": added,
        "lines_removed": removed,
    }
