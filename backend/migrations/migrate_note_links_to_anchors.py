"""Migration: note_links_to_anchors — turn `[label](note:ID)` body links into note anchors.

A note used to be anchored by a markdown link written INTO the document text; it is
now a Yjs RelativePosition pair on the note row (backend/note_anchors.py), painted
over the text by the editor. Per document: build the anchor on the link's label,
then surgically delete only the closing `](note:ID)` and the opening `[` (both the
`[label]` and `[[label]]` forms), so the label's items — and the anchor bound to
them — survive. A link whose note is gone, lives in another document, or was
already anchored by an earlier link in the same text is stripped to its label;
an empty-label link is removed whole. Finally drops the retired
`anchor_offset_*` columns (schema.surql carries the same REMOVEs for fresh DBs).

# INVARIANT(data-loss): per document the note rows are written FIRST, the link
# removal is published SECOND, through the backplane (`publish_doc_update`,
# append=True), never straight to ydoc_state. Why: a crash between the two leaves
# the link in the text and a re-run rebuilds the anchor (an idempotent overwrite)
# and removes it; the reverse order loses the anchor for good. The backplane makes
# a live session merge the removal instead of re-flushing its stale text over it.

Last, `documents.content` (the read model) is rewritten from the migrated doc.

Idempotent: a document with no link left is a no-op. Runs at boot from the
runner; by hand, `--dry-run` prints the counts without writing:
    docker compose exec backend python -m migrations.migrate_note_links_to_anchors --dry-run
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field

from chat_sessions.create import cap_anchor_quote
from note_anchors import anchor_space, build_anchor, cp_to_utf16
from pycrdt import Doc
from textmatch import surgical_splice_text

from db import get_db
from migrations._shared import logger
from table_serialize import expand_tables
from ydoc_store import get_text, load, publish_doc_update

# The forms frontend/src/components/editor/link-patterns.ts `noteLink` matched,
# plus the empty label the delete-time strip removed.
_NOTE_LINK = re.compile(r"(\[\[?)([^\]]*)(\]\]?\(note:([^)\s]+)\))")


@dataclass(frozen=True)
class NoteAnchor:
    """The columns one migrated note row receives."""
    rel_start: bytes
    rel_end: bytes
    quote: str


@dataclass
class DocResult:
    """What one document's pass produced; `update` is None when the text is unchanged."""
    anchors: dict[str, NoteAnchor] = field(default_factory=dict)
    update: bytes | None = None
    stripped: int = 0


def migrate_doc_links(doc: Doc, document_id: str, note_hosts: dict[str, str]) -> DocResult:
    """Anchor and remove every note link in one loaded Y.Doc, IN PLACE.

    `note_hosts` maps a live note's id to its host document id. Pure over the doc —
    the caller persists `anchors` and publishes `update`.
    """
    text = get_text(doc)
    current = str(text)
    matches = list(_NOTE_LINK.finditer(current))
    result = DocResult()
    if not matches:
        return result
    space = anchor_space(doc)
    for m in matches:
        note_id, label = m.group(4), m.group(2)
        anchorable = (
            label and note_hosts.get(note_id) == document_id and note_id not in result.anchors
        )
        if not anchorable:
            result.stripped += 1
            continue
        rel_start, rel_end = build_anchor(
            space, cp_to_utf16(current, m.start(2)), cp_to_utf16(current, m.end(2)),
        )
        result.anchors[note_id] = NoteAnchor(rel_start, rel_end, cap_anchor_quote(label))
    before = doc.get_state()
    with doc.transaction():
        for m in reversed(matches):
            # Closing part first: the opening bracket's offset stays valid.
            surgical_splice_text(text, m.start(3), m.end(3), "")
            surgical_splice_text(text, m.start(1), m.end(1), "")
    result.update = doc.get_update(before)
    return result


async def _candidate_documents(db) -> tuple[list[str], dict[str, str]]:
    """(documents that may hold a note link, live note id → host document id)."""
    notes = await db.query(
        "SELECT meta::id(id) AS nid, document_id FROM chat_sessions "
        "WHERE is_note = true AND deleted_at IS NONE"
    )
    note_hosts = {r["nid"]: r["document_id"] for r in (notes or []) if r.get("document_id")}
    # WHY both sources: documents.content is a compacted read-model and lags the
    # update log; a note's host document is where its editor wrote the link.
    linked = await db.query(
        "SELECT VALUE meta::id(id) FROM documents WHERE string::contains(content ?? '', '(note:')"
    )
    return sorted(set(linked or []) | set(note_hosts.values())), note_hosts


async def run(db, *, dry_run: bool = False, publish=publish_doc_update) -> dict[str, int]:
    """Migrate every candidate document; returns the counts it logs.

    `publish` is the backplane fan-out (`ydoc_store.publish_doc_update`); a test
    passes a recorder to observe what the note rows held at publish time.
    """
    doc_ids, note_hosts = await _candidate_documents(db)
    stats = {"documents": 0, "anchored": 0, "stripped": 0}
    for doc_id in doc_ids:
        doc = await load(doc_id)
        result = migrate_doc_links(doc, doc_id, note_hosts)
        if result.update is None:
            continue
        stats["documents"] += 1
        stats["anchored"] += len(result.anchors)
        stats["stripped"] += result.stripped
        if dry_run:
            continue
        for note_id, anchor in result.anchors.items():
            await db.query(
                "UPDATE type::record('chat_sessions', $id) SET anchor_rel_start = $rs, "
                "anchor_rel_end = $re, anchor_quote = $q",
                {"id": note_id, "rs": anchor.rel_start, "re": anchor.rel_end, "q": anchor.quote},
            )
        await publish(doc_id, result.update, append=True)
        # WHY: documents.content is the read model search, embeddings and every
        # content reader see; the log append alone leaves the link there until the
        # document's next compaction, which an unopened document may never reach.
        await db.query(
            "UPDATE type::record('documents', $id) SET content = $content",
            {"id": doc_id, "content": expand_tables(doc)},
        )
    logger.info(
        "note_links_to_anchors%s: %d document(s), %d note(s) anchored, "
        "%d link(s) stripped to their label (note gone, or outside its host document)",
        " (dry run)" if dry_run else "", stats["documents"], stats["anchored"], stats["stripped"],
    )
    return stats


async def _migrate_note_links_to_anchors(db) -> None:
    """Runner entry: migrate the links, then drop the retired offset columns."""
    await run(db)
    await db.query("REMOVE FIELD IF EXISTS anchor_offset_start ON chat_sessions")
    await db.query("REMOVE FIELD IF EXISTS anchor_offset_end ON chat_sessions")


async def _main() -> None:
    # WHY: outside the server nothing configures the migrations logger, so the counts
    # line `run` logs — the whole output of a --dry-run — would vanish silently.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    await run(await get_db(), dry_run="--dry-run" in sys.argv[1:])


if __name__ == "__main__":
    asyncio.run(_main())
