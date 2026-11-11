"""Mention extraction and edge rebuilding for documents and references."""
# ARCH: Graph-based mention tracking — parses markdown links, rebuilds
# SurrealDB doc_mentions edges atomically per save.
# Delete-all-then-recreate runs inside a transaction so a mid-batch failure
# rolls back rather than leaving the doc with zero backlinks.
# SYSTEM: mentions — graph-based mention tracking via markdown link parsing

import re

from artifacts import ARTIFACT_MEDIA_TYPE
from edge_kinds import (
    KIND_MENTION,
    KIND_TRANSCLUSION,
    STORED_EDGE_KINDS,
)
from surrealdb import AsyncSurreal

import event_bus
from db import (
    SAFE_ID_RE,
    get_db,
    record_refs,
    validate_record_id,
)
from models import is_ref_row
from transclusion_grammar import SCHEME_TABLE

# The stored edge-kind vocabulary (plan typed-edges) lives in edge_kinds — the
# ONE source the /edges route, the get_links tool schema and these rebuild
# writers all derive from. Content-derived kinds live on doc_mentions.kind;
# field-backed kinds (attachment/note/chat-context) and the content-parsed
# `external` are PROJECTED at read time by edges.py and never stored.
# Re-exported here for the existing `from mentions import KIND_MENTION` consumers.
__all__ = [
    "KIND_MENTION", "KIND_TRANSCLUSION", "STORED_EDGE_KINDS",
    "extract_doc_mentions", "extract_ref_mentions", "extract_content_edges",
    "extract_external_links", "content_edge_pairs", "content_external_urls",
    "finalize_content_edges", "rebuild_doc_mentions", "resolve_first_circle",
]

# The frontend extLink pattern (frontend/src/components/editor/link-patterns.ts)
# verbatim: `[{1,2}label]{1,2}(http…)\]?` — wiki-collapsible brackets and the
# trailing `]` of the wiki-close form are matched, the (label, url) groups kept.
_EXTERNAL_LINK_RE = re.compile(r"\[{1,2}([^\]]+)\]{1,2}\((http[^)]+)\)\]?")


def extract_doc_mentions(content: str) -> list[str]:
    # WHY: the lookahead is projected at import time from the canonical SCHEME_TABLE
    #   (is_doc_mention == False prefixes) instead of hand-written, so it cannot drift.
    #   Colon-form prefixes mean a bare doc-id that merely starts with a scheme word
    #   (e.g. `httpfoo`) is correctly captured, matching parse_target.
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
    pattern = r'!?\[(?:[^\]]*)\]\(ref:([^)]+)\)'
    raw = set(re.findall(pattern, content))
    return [m for m in raw if SAFE_ID_RE.match(m)]


def extract_content_edges(content: str) -> list[tuple[str, str]]:
    """(target_id, kind) pairs for every doc/ref link and embed in content.

    # ARCH: the ONE edge extractor (plan typed-edges) — kind comes from the
    # leading `!` ('transclusion') vs its absence ('mention'); the same target
    # may appear under BOTH kinds (two edges). The doc-form lookahead stays
    # projected from SCHEME_TABLE (see extract_doc_mentions) and the ref: form
    # is matched separately because SCHEME_TABLE classifies `ref:` as
    # is_doc_mention == False. A superset of extract_doc_mentions +
    # extract_ref_mentions (which stay for resolve_first_circle's doc/ref split):
    # every id the pair produced before still produces a pair, now with its kind.
    """
    _reject_prefixes = "|".join(
        p for p, _is_transclusion, is_doc_mention in SCHEME_TABLE if not is_doc_mention
    )
    doc_pattern = re.compile(
        r'(!?)\[(?:[^\]]+)\]\((?!' + _reject_prefixes + r')(?:doc:)?([^)]+)\)'
    )
    ref_pattern = re.compile(r'(!?)\[(?:[^\]]*)\]\(ref:([^)]+)\)')
    pairs: list[tuple[str, str]] = []
    for m in doc_pattern.finditer(content):
        if SAFE_ID_RE.match(m.group(2)):
            pairs.append((m.group(2), KIND_TRANSCLUSION if m.group(1) else KIND_MENTION))
    for m in ref_pattern.finditer(content):
        if SAFE_ID_RE.match(m.group(2)):
            pairs.append((m.group(2), KIND_TRANSCLUSION if m.group(1) else KIND_MENTION))
    # Same (target, kind) from two syntaxes (e.g. bare + doc:) is ONE edge.
    return list(dict.fromkeys(pairs))


def content_edge_pairs(content: str, *, exclude: str) -> set[tuple[str, str]]:
    """extract_content_edges as the flush/registry pair CACHE seed: the set
    with self-edges removed. Why one helper: the flush detector and the
    session-creation seed must build the SAME set or a reopen+flush rebuilds
    forever (plan typed-edges).

    # ARCH: `content` is the GFM read-model form (expand_tables) at EVERY call
    # site — the flush detector and the registry seed both pass the expanded
    # text; the anchor form cannot see a table cell's `[x](doc:id)` link.
    """
    return {
        (tid, kind) for tid, kind in extract_content_edges(content) if tid != exclude
    }


def extract_external_links(content: str) -> list[tuple[str, str]]:
    """(label, url) for every external `[label](http…)` link, deduped by URL in
    first-seen order — the Links panel's external section renders exactly this.

    # ARCH: `content` is the GFM read-model form (expand_tables of the source's
    # Y.Doc) — the anchor form cannot see a table cell's `[x](https://…)` link,
    # the exact defect this extraction exists to close (plan table-links-notes).
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _EXTERNAL_LINK_RE.finditer(content):
        if m.group(2) not in seen:
            seen.add(m.group(2))
            out.append((m.group(1), m.group(2)))
    return out


def content_external_urls(content: str) -> set[str]:
    """extract_external_links as the flush/registry URL CACHE seed — the SAME
    set-from-same-form contract as content_edge_pairs: the session-creation
    seed and the flush detector must build the SAME URL set or every reopen's
    first flush emits a false `backlinks_changed` for the source (plan
    table-links-notes).

    # ARCH: `content` is the GFM read-model form (expand_tables) at EVERY call
    # site, exactly as content_edge_pairs.
    """
    return {url for _label, url in extract_external_links(content)}


async def _rebuild_mentions(
    db: AsyncSurreal, source_table: str, edge_table: str, source_id: str, content: str,
    *, exclude_self: bool = False,
) -> list[tuple[str, str]]:
    """Delete old mention edges and recreate them (with kind) atomically."""
    # WHY: both `[t](doc:id)` and `[t](ref:id)` links create doc_mentions edges.
    # Why: references are rows in the documents table, so a referenced entity must see
    # its incoming links too — omitting ref mentions left every reference with empty
    # backlinks even when several documents linked to it. Edge target is documents:id
    # for both kinds; dedup since a target may be linked as both.
    pairs = extract_content_edges(content)
    if exclude_self:
        pairs = [(tid, kind) for tid, kind in pairs if tid != source_id]
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
    for mid, kind in pairs:
        stmts.append(
            f"RELATE {source_table}:`{safe_id}`->{edge_table}->documents:`{validate_record_id(mid)}`"
            f" CONTENT {{ kind: '{kind}' }}"
        )
    stmts.append("COMMIT TRANSACTION")
    await db.query("; ".join(stmts))
    return pairs


async def rebuild_doc_mentions(
    db: AsyncSurreal, table: str, entity_id: str, content: str,
) -> list[tuple[str, str]]:
    """Rebuild mention edges for one source row — EXCEPT an html artifact.

    # ARCH: `content` is the GFM read-model form (expand_tables of the source's
    # Y.Doc) at EVERY call site — the anchor form cannot see a table cell's
    # `[x](doc:id)` link, and an anchor-form rebuild DELETES the cell edges a
    # previous expanded-form writer created (plan table-cell-mention-edges).

    Returns the rebuilt (target_id, kind) pairs.

    # INVARIANT: an html artifact row carries NO mention edges. Why: a page
    # cannot navigate, so a document id in its JS or in
    # page text that happens to look like `[t](<id>)` is not a link; existing
    # edges are CLEARED, not kept, so the row can never hold a stale set.
    # Gated HERE, at the single rebuild chokepoint, so the flush pipeline,
    # the agent finalize tail and any future writer share one rule.
    """
    if table == "documents":
        rows = await db.query(
            "SELECT VALUE media_type FROM type::record('documents', $id)",
            {"id": entity_id},
        )
        if rows and rows[0] == ARTIFACT_MEDIA_TYPE:
            await db.query(
                "BEGIN TRANSACTION; "
                "DELETE doc_mentions WHERE in = type::record('documents', $id); "
                "COMMIT TRANSACTION",
                {"id": entity_id},
            )
            return []
    return await _rebuild_mentions(db, table, "doc_mentions", entity_id, content, exclude_self=True)


async def finalize_content_edges(
    db: AsyncSurreal, source_id: str, content: str, *,
    project_id: str | None = None, is_reference: bool = False,
) -> None:
    """rebuild mention edges + ALWAYS emit backlinks_changed_batch for the
    source + content_flushed — the ONE no-session finalize tail (plan
    table-links-notes).

    Folds the three formerly-identical tails (agent finalize_content_mutation,
    the REST PATCH document fallback, the REST PATCH reference fallback) that
    each emitted only `if pairs:` — a URL-only edit produces no pair delta, so
    the Links panel's future server-side `external` read would never refresh
    on those paths. These paths have no URL cache to diff against (they are
    rare writes), so the emit is UNCONDITIONAL for `[source, *targets]`; cost
    if wrong: one extra backlinks_changed frame + one /edges GET per viewer
    per agent/REST write.

    # ARCH: `content` is the GFM read-model form (the set_content return or the
    # agent's expand_tables output) at EVERY call site — see rebuild_doc_mentions.
    """
    pairs = await rebuild_doc_mentions(db, "documents", source_id, content)
    await event_bus.emit(
        "backlinks_changed_batch",
        document_ids=[source_id, *(tid for tid, _kind in pairs)],
    )
    await event_bus.emit(
        "content_flushed",
        entity_type="doc",
        entity_id=source_id,
        project_id=project_id,
        **({"is_reference": True} if is_reference else {}),
    )


async def _filter_alive(table: str, ids: list[str]) -> list[str]:
    """Return subset of `ids` that exist in `table` and are not soft-deleted."""
    if not ids:
        return []
    db = await get_db()
    in_clause, params = record_refs(table, ids)
    rows = await db.query(
        f"SELECT VALUE meta::id(id) FROM {table} "
        f"WHERE id IN [{in_clause}] AND deleted_at IS NONE",
        params,
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
