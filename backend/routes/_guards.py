"""Shared entity guards — the fetch → uniform 404 → access-require prologue.

# ARCH: every route that addresses an entity by id owes the same prologue:
# fetch the row, refuse with a uniform 404 (no existence oracle — a missing,
# invalid-shaped and soft-deleted row are indistinguishable), then require
# access. The fetch and the 404 live here once; the access require stays at
# the caller because its CHOICE (read / full / owner, on the row's own id or
# via a FK) is policy, not boilerplate. Internal extraction in the shape of
# routes/_access_grants.py (no SYSTEM marker).
"""

from collections.abc import Callable

from fastapi import HTTPException

from access import require_document_full, require_document_read
from db import fetch_one
from models import is_ref_row


async def guard_row(
    table: str, entity_id: str, *, not_found: str,
    ok: Callable[[dict], bool] | None = None,
) -> dict:
    """Fetch one row or raise the caller's uniform 404 — no existence oracle.

    `not_found` is the entity's established 404 detail ("Checkpoint not
    found", …); `ok` folds extra row predicates (the is_reference shape)
    into the SAME 404 so an invalid row is indistinguishable from a missing
    one. The access require that follows is the caller's — see the module
    ARCH note.
    """
    row = await fetch_one(table, entity_id)
    if not row or (ok is not None and not ok(row)):
        raise HTTPException(status_code=404, detail=not_found)
    return row


async def require_reference(reference_id: str, user: dict, *, write: bool = False) -> dict:
    """Fetch a reference-document, uniform-404 on non-reference rows, require access.

    The references/files surface guard: references are `documents` rows with
    is_reference=true (SYSTEM: references), so a plain document id must 404
    exactly like an unknown one. `write` selects the full-access require.
    """
    ref = await guard_row(
        "documents", reference_id, not_found="Reference not found", ok=is_ref_row,
    )
    if write:
        await require_document_full(reference_id, user)
    else:
        await require_document_read(reference_id, user)
    return ref
