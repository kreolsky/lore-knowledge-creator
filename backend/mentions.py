"""Mention extraction and edge rebuilding for documents and references."""
# ARCH: Graph-based mention tracking — parses markdown links, rebuilds
# SurrealDB doc_mentions edges atomically per save.
# Delete-all-then-recreate runs inside a transaction so a mid-batch failure
# rolls back rather than leaving the doc with zero backlinks.
# SYSTEM: mentions — graph-based mention tracking via markdown link parsing

import re

from surrealdb import AsyncSurreal

from db import get_db, validate_record_id
from models import is_ref_row


def extract_doc_mentions(content: str) -> list[str]:
    from db import SAFE_ID_RE

    # WHY: the lookahead is projected at import time from the canonical SCHEME_TABLE
    #   (is_doc_mention == False prefixes) instead of hand-written, so it cannot drift.
    #   Colon-form prefixes mean a bare doc-id that merely starts with a scheme word
    #   (e.g. `httpfoo`) is correctly captured, matching parse_target.
    from transclusion_grammar import SCHEME_TABLE
    _reject_prefixes = "|".join(
        p for p, _is_transclusion, is_doc_mention in SCHEME_TABLE if not is_doc_mention
    )
    pattern = r'\[(?:[^\]]+)\]\((?!' + _reject_prefixes + r')([^)]+)\)'
    raw = set(re.findall(pattern, content))
    result = []
    for m in raw:
        id_str = m.removeprefix("doc:") if m.startswith("doc:") else m
        if SAFE_ID_RE.match(id_str):
            result.append(id_str)
    return result


def extract_ref_mentions(content: str) -> list[str]:
    """Extract reference IDs from `[text](ref:id)` and `![alt](ref:id)` markdown patterns."""
    from db import SAFE_ID_RE
    pattern = r'!?\[(?:[^\]]*)\]\(ref:([^)]+)\)'
    raw = set(re.findall(pattern, content))
    return [m for m in raw if SAFE_ID_RE.match(m)]


async def _rebuild_mentions(
    db: AsyncSurreal, source_table: str, edge_table: str, source_id: str, content: str,
    *, exclude_self: bool = False,
) -> list[str]:
    """Delete old mention edges and recreate them atomically."""
    # WHY: both `[t](doc:id)` and `[t](ref:id)` links create doc_mentions edges.
    # Why: references are rows in the documents table, so a referenced entity must see
    # its incoming links too — omitting ref mentions left every reference with empty
    # backlinks even when several documents linked to it. Edge target is documents:id
    # for both kinds; dedup since a target may be linked as both.
    mention_ids = list(dict.fromkeys(extract_doc_mentions(content) + extract_ref_mentions(content)))
    if exclude_self:
        mention_ids = [m for m in mention_ids if m != source_id]
    safe_id = validate_record_id(source_id)
    # INVARIANT(data-loss): mention rebuild is all-or-nothing — never delete old edges
    # without committing the new ones. Why: a mid-batch RELATE failure used to
    # wipe all backlinks for the doc. The DELETE + RELATEs MUST stay inside a
    # single-string BEGIN/COMMIT — a SurrealDB error inside the block rolls the
    # whole thing back. NOTE: separate BEGIN/ops/COMMIT query() RPCs do NOT share
    # a transaction scope (the DELETE commits and is not rolled back) — only one
    # joined string works. The shared db.run_in_transaction() helper does the same
    # for bind-var statements; this site hand-rolls it because the RELATE list is
    # built from interpolated record IDs rather than bind params.
    stmts = [
        "BEGIN TRANSACTION",
        f"DELETE {edge_table} WHERE in = type::record('{source_table}', '{safe_id}')",
    ]
    for mid in mention_ids:
        stmts.append(
            f"RELATE {source_table}:`{safe_id}`->{edge_table}->documents:`{validate_record_id(mid)}`"
        )
    stmts.append("COMMIT TRANSACTION")
    await db.query("; ".join(stmts))
    return mention_ids


async def rebuild_doc_mentions(db: AsyncSurreal, table: str, entity_id: str, content: str) -> list[str]:
    return await _rebuild_mentions(db, table, "doc_mentions", entity_id, content, exclude_self=True)


async def _filter_alive(table: str, ids: list[str]) -> list[str]:
    """Return subset of `ids` that exist in `table` and are not soft-deleted."""
    if not ids:
        return []
    db = await get_db()
    in_clause = ",".join(f"type::record('{table}','{validate_record_id(i)}')" for i in ids)
    rows = await db.query(
        f"SELECT VALUE meta::id(id) FROM {table} "
        f"WHERE id IN [{in_clause}] AND deleted_at IS NONE"
    )
    return list(rows or [])


async def resolve_first_circle(content: str, self_id: str) -> dict:
    """Parse content for `[text](id)` and `[text](ref:id)` markdown links;
    filter against existing, non-soft-deleted documents; exclude self_id.

    # WHY: Moved from documents.py to break the circular dependency that
    # forced references.py to use a runtime `from routes.documents import`.
    """
    doc_ids = [d for d in extract_doc_mentions(content) if d != self_id]
    ref_ids = [r for r in extract_ref_mentions(content) if r != self_id]
    alive_docs = set(await _filter_alive("documents", doc_ids))
    alive_refs_raw = await _filter_alive("documents", ref_ids)
    alive_refs: list[str] = []
    if alive_refs_raw:
        db = await get_db()
        ref_flags = await db.query(
            "SELECT meta::id(id) AS id, is_reference FROM documents "
            "WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
            {"ids": alive_refs_raw},
        )
        alive_refs = [r["id"] for r in (ref_flags or []) if is_ref_row(r)]
    return {
        "document_ids": list(alive_docs),
        "reference_ids": alive_refs,
    }
