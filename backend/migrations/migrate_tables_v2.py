"""Migration: tables_v2 — rebuild every schema-v1 table as the id-keyed schema v2.

v1 (positional): ``tables[id] = Map{columns: Array<Map{w}>, rows: Array<Array<Map{t: Text}>>,
label?}``. v2 (id-keyed): see SYSTEM: table-block (backend/table_serialize.py). Per document
every v1 table is rebuilt in place under the SAME table id — cell text, column widths and the
model label carried over — and a note anchored inside a cell is rebound to the new cell's
Y.Text at the same UTF-16 offsets. A ragged v1 table is squared to its widest row (missing
cells empty, missing widths ``DEFAULT_COLUMN_WIDTH``), so no cell text is dropped.

# INVARIANT(data-loss): a document whose cell-note count differs before/after is left UNTOUCHED.
# Why: an anchor not carried into the new cell loses its place for good — an untouched v1
# table renders "awaits migration", a lost anchor renders nothing. Nothing is written, the
# document is logged ``tables_v2 deferred <doc_id>: <reason>``, and after the pass the
# migration raises ``MigrationDeferred`` so the runner retries next boot.

# INVARIANT(data-loss): note rows are written FIRST, the deterministic rebuild published SECOND.
# Why: the reverse order loses every cell anchor on a crash (the v1 items are gone once
# published). The rebuild is DETERMINISTIC — a fixed client id (``MIGRATION_CLIENT_ID``) and
# uuid5 row/column ids — so a crash between the two re-runs into byte-identical items that
# the already-written note rows point at. Publishing goes through the backplane
# (``publish_doc_update``, append=True) so a live session merges it.

Idempotent: a document with no v1 table is a no-op. Runs at boot from the runner; by hand,
``--dry-run`` prints per-document counts without writing:
    docker compose exec backend python -m migrations.migrate_tables_v2 --dry-run
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

# WHY private imports: the ASCII shadow and the relpos decoder are THE implementations
# note anchors are built and resolved with; a second copy here would drift from them.
from note_anchors import _ascii_shadow, _decode_relpos
from pycrdt import Assoc, Doc, Map, StickyIndex, Text

from db import get_db
from migrations._shared import MigrationDeferred, logger
from table_serialize import (
    DEFAULT_COLUMN_WIDTH,
    _build_table,
    cell_key,
    ordered_ids,
    table_awaits_migration,
)
from ydoc_store import load, publish_doc_update

# WHY: above 2**32 (no Yjs browser client can pick it) and distinct from
# ydoc_store.SEED_CLIENT_ID, so the migration's items are its own client's.
MIGRATION_CLIENT_ID = 10_000_000_001
_ID_NAMESPACE = uuid.UUID("5d0f6c1e-8a3b-4e29-9a57-3c2d7b4e1f60")

ItemId = tuple[int, int]


@dataclass(frozen=True)
class CellNote:
    """A live note row's anchor as stored (v1-encoded relative positions)."""
    note_id: str
    rel_start: bytes
    rel_end: bytes


@dataclass
class DocResult:
    """One document's pass. ``update`` is None when nothing is to be published."""
    tables: int = 0
    cell_notes: int = 0
    anchors: dict[str, tuple[bytes, bytes]] = field(default_factory=dict)
    update: bytes | None = None
    deferred: str | None = None


def _item_id(rel: bytes) -> ItemId | None:
    """(client, clock) an item-scoped relative position points at; None otherwise."""
    try:
        tag, client, clock = _decode_relpos(rel)
    except ValueError:
        return None
    return (client, clock) if tag == 0 else None


def _shadow(doc: Doc) -> tuple[Doc, dict[int, int]]:
    """ASCII shadow of ``doc`` (same item ids; index == UTF-16 offset) + its state vector."""
    update, state = _ascii_shadow(doc.get_update())
    shadow = Doc()
    shadow.apply_update(update)
    return shadow, state


def _char_ids(shadow: Doc, cells: dict) -> dict[ItemId, tuple]:
    """Item id of every live char in ``cells`` (key → shadow Text) → (key, UTF-16 offset)."""
    out: dict[ItemId, tuple] = {}
    with shadow.transaction():
        for key, text in cells.items():
            for i in range(len(text)):
                item = _item_id(StickyIndex.new(text, i, Assoc.AFTER).encode())
                if item is not None:
                    out[item] = (key, i)
    return out


def _v1_cells(table) -> dict[tuple[int, int], Text]:
    """(row, col) → cell Y.Text of a v1 table."""
    return {
        (r, c): cell["t"]
        for r, row in enumerate(table["rows"])
        for c, cell in enumerate(row)
        if isinstance(cell, Map) and isinstance(cell.get("t"), Text)
    }


def _v1_projection(table) -> tuple[list[int], list[list[str]]]:
    """Widths + the cell matrix of a v1 table, squared to its widest row."""
    widths = [int(col["w"]) for col in table["columns"]]
    cells = _v1_cells(table)
    matrix = [
        [str(cells[(r, c)]) if (r, c) in cells else "" for c in range(len(row))]
        for r, row in enumerate(table["rows"])
    ]
    width = max([len(widths), *(len(row) for row in matrix)])
    widths += [DEFAULT_COLUMN_WIDTH] * (width - len(widths))
    return widths, [row + [""] * (width - len(row)) for row in matrix]


def _id_factory(document_id: str, table_id: str) -> Callable[[], str]:
    counter = itertools.count()
    return lambda: str(uuid.uuid5(_ID_NAMESPACE, f"{document_id}/{table_id}/{next(counter)}"))


def _classify(notes, old_chars, migration_clock: int):
    """Split notes into (rebind: id → ((table, row, col), start, end), already-rebound ids,
    unrebindable ids). A note with neither half inside a v1 cell is not a cell note."""
    rebind, already, lost = {}, [], []
    for note in notes:
        s, e = _item_id(note.rel_start), _item_id(note.rel_end)
        if s and e and all(c == MIGRATION_CLIENT_ID and k >= migration_clock for c, k in (s, e)):
            already.append(note)  # written by a run that crashed before its publish
            continue
        hs, he = old_chars.get(s), old_chars.get(e)
        if hs is None and he is None:
            continue
        if hs and he and hs[0] == he[0] and hs[1] <= he[1]:
            rebind[note.note_id] = (hs[0], hs[1], he[1] + 1)
        else:
            lost.append(note.note_id)
    return rebind, already, lost


def _rebuild(work: Doc, document_id: str, v1_ids: list[str]) -> bytes:
    tables = work.get("tables", type=Map)
    before = work.get_state()
    with work.transaction():
        for tid in v1_ids:
            label = tables[tid].get("label")
            widths, matrix = _v1_projection(tables[tid])
            _build_table(tables, tid, widths, matrix, new_id=_id_factory(document_id, tid))
            if label is not None:
                tables[tid]["label"] = str(label)
    return work.get_update(before)


def _rebind(work: Doc, v1_ids, rebind, already) -> dict[str, tuple[bytes, bytes]] | None:
    """New anchors over the rebuilt cells, each verified to resolve back to its cell and
    offsets; None when any rebind or crashed-run anchor fails to verify."""
    shadow, _ = _shadow(work)
    tables = shadow.get("tables", type=Map)
    cells, order = {}, {}
    for tid in v1_ids:
        order[tid] = ordered_ids(tables[tid])
        cells.update({(tid, k): text for k, text in tables[tid]["cells"].items()})
    new_chars = _char_ids(shadow, cells)
    anchors = {}
    with shadow.transaction():
        for note_id, ((tid, r, c), start, end) in rebind.items():
            row_ids, col_ids = order[tid]
            key = (tid, cell_key(row_ids[r], col_ids[c]))
            rs = StickyIndex.new(cells[key], start, Assoc.AFTER).encode()
            re_ = StickyIndex.new(cells[key], end, Assoc.BEFORE).encode()
            if new_chars.get(_item_id(rs)) != (key, start) or new_chars.get(_item_id(re_)) != (key, end - 1):
                return None
            anchors[note_id] = (rs, re_)
    for note in already:
        hs, he = new_chars.get(_item_id(note.rel_start)), new_chars.get(_item_id(note.rel_end))
        if not (hs and he and hs[0] == he[0]):
            return None
    return anchors


def migrate_doc_tables(doc: Doc, document_id: str, notes: list[CellNote]) -> DocResult:
    """Plan one document's v1 → v2 rebuild. Pure: ``doc`` is not mutated; the caller
    writes ``anchors`` and publishes ``update`` (both empty when ``deferred`` is set)."""
    source = doc.get("tables", type=Map)
    v1_ids = sorted(tid for tid, table in source.items() if table_awaits_migration(table))
    result = DocResult(tables=len(v1_ids))
    if not v1_ids:
        return result
    work = Doc(client_id=MIGRATION_CLIENT_ID)
    work.apply_update(doc.get_update())
    rebind, already, lost = {}, [], []
    if notes:
        shadow, state = _shadow(work)
        shadow_tables = shadow.get("tables", type=Map)
        old_chars = _char_ids(shadow, {
            (tid, r, c): text for tid in v1_ids
            for (r, c), text in _v1_cells(shadow_tables[tid]).items()
        })
        rebind, already, lost = _classify(notes, old_chars, state.get(MIGRATION_CLIENT_ID, 0))
    result.cell_notes = len(rebind) + len(already) + len(lost)
    if lost:
        result.deferred = (
            f"{len(lost)} of {result.cell_notes} cell note(s) cannot be rebound "
            "(anchor straddles cells or a deleted char)"
        )
        return result
    update = _rebuild(work, document_id, v1_ids)
    anchors = _rebind(work, v1_ids, rebind, already) if result.cell_notes else {}
    if anchors is None:
        result.deferred = f"a rebound anchor of {result.cell_notes} cell note(s) failed to verify"
        return result
    result.anchors, result.update = anchors, update
    return result


async def _notes_by_document(db) -> dict[str, list[CellNote]]:
    rows = await db.query(
        "SELECT meta::id(id) AS nid, document_id, anchor_rel_start, anchor_rel_end "
        "FROM chat_sessions WHERE is_note = true AND deleted_at IS NONE "
        "AND anchor_rel_start IS NOT NONE AND anchor_rel_end IS NOT NONE"
    )
    out: dict[str, list[CellNote]] = {}
    for r in rows or []:
        if r.get("document_id"):
            out.setdefault(r["document_id"], []).append(
                CellNote(r["nid"], bytes(r["anchor_rel_start"]), bytes(r["anchor_rel_end"])),
            )
    return out


async def run(db, *, dry_run: bool = False, publish=publish_doc_update) -> dict[str, int]:
    """Migrate every document; returns the counts it logs. Raises ``MigrationDeferred``
    (after the whole pass) when any document was deferred and this is not a dry run.

    `publish` is the backplane fan-out; a test passes a recorder to observe what the note
    rows held at publish time.
    """
    # WHY every document (trashed ones too): an unlinked table has no anchor in the
    # text, so no read-model filter finds it, and a trashed document can be restored.
    doc_ids = await db.query("SELECT VALUE meta::id(id) FROM documents") or []
    notes = await _notes_by_document(db)
    stats = {"documents": 0, "tables": 0, "cell_notes": 0, "deferred": 0}
    for doc_id in sorted(doc_ids):
        result = migrate_doc_tables(await load(doc_id), doc_id, notes.get(doc_id, []))
        if not result.tables:
            continue
        if result.deferred:
            stats["deferred"] += 1
            logger.warning("tables_v2 deferred %s: %s", doc_id, result.deferred)
            continue
        stats["documents"] += 1
        stats["tables"] += result.tables
        stats["cell_notes"] += result.cell_notes
        logger.info("tables_v2 %s: %d table(s), %d cell note(s)", doc_id, result.tables, result.cell_notes)
        if dry_run:
            continue
        for note_id, (rel_start, rel_end) in result.anchors.items():
            await db.query(
                "UPDATE type::record('chat_sessions', $id) SET anchor_rel_start = $rs, anchor_rel_end = $re",
                {"id": note_id, "rs": rel_start, "re": rel_end},
            )
        await publish(doc_id, result.update, append=True)
    logger.info(
        "tables_v2%s: %d document(s), %d table(s), %d cell note(s) rebound, %d deferred",
        " (dry run)" if dry_run else "", stats["documents"], stats["tables"],
        stats["cell_notes"], stats["deferred"],
    )
    if stats["deferred"] and not dry_run:
        raise MigrationDeferred(f"{stats['deferred']} document(s) keep v1 tables")
    return stats


async def _migrate_tables_v2(db) -> None:
    """Runner entry."""
    await run(db)


async def _main() -> None:
    # WHY: outside the server nothing configures the migrations logger, so the counts
    # lines `run` logs — the whole output of a --dry-run — would vanish silently.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    await run(await get_db(), dry_run="--dry-run" in sys.argv[1:])


if __name__ == "__main__":
    asyncio.run(_main())
