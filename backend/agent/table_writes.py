"""Agent table-cell mutation path (validate + convergence + apply).

Part of the chat-agent-mode system: the table-cell edit family — header-column
resolution, batch validate/stale-check, the `tables`-subtree convergence, and the
atomic apply entry. Internal extraction — no SYSTEM marker; this module OWNS its
names.

WHY (imports): live state comes from `agent.doc_state` and the checkpoint/presence
helpers from `agent.collab_writes`, both reached as module attributes at call time
so a test patches the owning module. The fetch+RBAC+scope gate is the shared
scope.gate_mutation_target.
"""
import logging

import event_bus
from agent import collab_writes, doc_state

logger = logging.getLogger(__name__)


def _resolve_table_col(doc, table_id: str, column: str | None, col_fallback: int | None,
                       n_rows: int, n_cols: int) -> int:
    """Resolve a header-name `column` (3A) to a 0-based col index; fall back to the
    numeric `col` when `column` is None/blank. Header names are matched against
    rows[0], case- and whitespace-insensitive. Raises HTTPException(404) with the
    available headers when a named column is not found.

    # INVARIANT: neither coordinate → HTTPException(400), never TypeError→500.
    # Why: `col` is un-advertised, so the model sends only `column`; a proposal that
    # dropped it (col=None, column=None) must fail-soft so the agent self-corrects,
    # matching the confirm-route contract (edit-table-cell-column-apply-fix).
    """
    from fastapi import HTTPException

    from table_serialize import read_table_cell

    if not column or not column.strip():
        if col_fallback is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "edit_table_cell needs a column name or col index"},
            )
        return col_fallback
    if n_rows == 0:
        raise HTTPException(status_code=404, detail={"error": "Table has no header row", "table_id": table_id})
    want = column.strip().lower()
    headers = [str(read_table_cell(doc, table_id, 0, i) or "") for i in range(n_cols)]
    for i, h in enumerate(headers):
        if h.strip().lower() == want:
            return i
    raise HTTPException(
        status_code=404,
        detail={"error": "Unknown column", "column": column, "headers": headers},
    )


async def validate_table_cell_edit(
    *, document_id: str, edits: list[dict], project_id: str, user: dict,
) -> str | None:
    """Resolve + gate a batch of table-cell edits (steps 1-4 of the apply contract).
    Shared by the confirm-mode gate (routes.tool_api.edits, BEFORE `_create_proposal`) and
    `apply_edit_table_cell` — the two must never diverge (mirrors `resolve_edit_range`
    being shared by the `edit_document` gate and apply).

    Each edit dict: {table_id, row, col, old_value}. ALL edits are validated before
    any is applied (all-or-nothing). Returns `current_tables_json` on success.
    Raises HTTPException:
      404 — doc missing/deleted/cross-project, OR unknown table_id/row/col
            (detail carries n_rows/n_cols/the target row so the model self-corrects).
      403 — access below "full".
      409 — old_value stale (detail carries current_value).

    # WHY: coordinate resolution (not substring) — an empty `old_value`
    # Why: str_replace can't target an empty cell (""); coordinates resolve it correctly.
    # ("" for an empty cell) resolves correctly, unlike a str_replace scan.
    """
    from fastapi import HTTPException
    from pycrdt import Map

    # M7: shared fetch+RBAC gate. scope_root=None → the subtree wall no-ops (this is the
    # confirm-time validate; the scope wall was already applied at the route). 404 on
    # missing/cross-project, 403 unless per-doc access is full.
    from scope import gate_mutation_target
    from table_serialize import read_table_cell

    await gate_mutation_target(
        user=user, doc_id=document_id, project_id=project_id, scope_root=None,
    )

    _current_content, current_tables_json = await doc_state.resolve_live_doc_state(document_id)

    from collab.registry import get_active_session

    from ydoc_store import load as load_ydoc

    session = get_active_session("doc", document_id)
    doc = session.ydoc if session is not None else await load_ydoc(document_id)
    tables = doc.get("tables", type=Map)

    for e in edits:
        table_id, row, old_value = e["table_id"], e["row"], e["old_value"]
        if tables is None or table_id not in tables:
            raise HTTPException(
                status_code=404,
                detail={"error": "Unknown table_id", "table_id": table_id},
            )
        rows_root = tables[table_id].get("rows")
        n_rows = len(rows_root) if rows_root is not None else 0
        n_cols = len(rows_root[0]) if n_rows else 0
        # 3A (ergonomics): resolve a header-NAME `column` to a numeric col index
        # (rows[0] is the header). `column` wins over the numeric `col` fallback.
        # The resolved index is written back onto the edit dict so the apply tail
        # (route_tables_mutation) and the proposal record use it too.
        col = _resolve_table_col(doc, table_id, e.get("column"), e.get("col"), n_rows, n_cols)
        e["col"] = col
        if row < 0 or row >= n_rows or col < 0 or col >= n_cols:
            target_row = (
                [{"col": i, "t": str(rows_root[row][i]["t"])} for i in range(len(rows_root[row]))]
                if rows_root is not None and 0 <= row < n_rows
                else None
            )
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "Cell out of bounds", "n_rows": n_rows, "n_cols": n_cols,
                    "row": target_row,
                },
            )
        current_value = read_table_cell(doc, table_id, row, col)
        if current_value != old_value:
            raise HTTPException(
                status_code=409,
                detail={"error": "stale", "current_value": current_value},
            )
    return current_tables_json


async def route_tables_mutation(
    *, doc_id: str, table_id: str, row: int, col: int, new_value: str, project_id: str | None,
) -> None:
    """Single convergence path for agent table-cell mutations (mirrors
    `route_document_content` for the `content` Text — this is the `tables`-subtree
    analog). A cell edit changes `ydoc.getMap('tables')`, NOT `content`; the
    anchor is unchanged so mentions/rebuild are skipped, but `content_flushed` is
    still emitted (cell text feeds embeddings/search via `expand_tables`).

    Live session present: mutate `session.ydoc` inside `_write_lock` (mirrors
    `apply_external_content_change`) — broadcasts locally then fans out to other
    replicas via `ydoc_store.publish_doc_update`. No live session: load the
    persisted Y.Doc, mutate, publish (append-log + backplane), emit
    `content_flushed`.
    """
    from collab.registry import get_active_session
    from collab.sync import MSG_SYNC, create_update_message, wrap_binary

    from table_serialize import set_table_cell
    from ydoc_store import load as load_ydoc
    from ydoc_store import publish_doc_update

    session = get_active_session("doc", doc_id)
    if session is not None:
        async with session._write_lock:
            set_table_cell(session.ydoc, table_id, row, col, new_value)
            update = session.ydoc.get_update()
            session._dirty = True
            resync_msg = wrap_binary(doc_id, MSG_SYNC, create_update_message(update))
        await session.broadcast_binary(resync_msg)
        await publish_doc_update(doc_id, update)
        session._updates_since_compact += 1
        return

    doc = await load_ydoc(doc_id)
    set_table_cell(doc, table_id, row, col, new_value)
    update = doc.get_update()
    await publish_doc_update(doc_id, update)

    await event_bus.emit(
        "content_flushed",
        entity_type="doc",
        entity_id=doc_id,
        project_id=project_id,
    )


async def apply_edit_table_cell(
    *,
    edits: list[dict],
    project_id: str,
    user: dict,
    scope_root: str | None = None,
) -> dict:
    """Apply one or more table-cell edits atomically (batch-ready internally —
    plan decision 7). Each edit dict: {document_id, table_id, row, col, old_value,
    new_value}. Validates + stale-checks ALL edits first, then applies them in ONE
    checkpoint + ONE transaction (all-or-nothing). The single-cell `edit_table_cell`
    tool wraps a list of one.

    # INVARIANT(security): identical shape to `apply_edit_to_document` — full-access gate,
    # Why: tables must carry the same RBAC / stale-check / checkpoint / broadcast guarantees as document edits.
    # stale-check via coordinate resolution, pre-edit checkpoint, presence
    # broadcast. Raises HTTPException on RBAC/not-found/stale (see
    # `validate_table_cell_edit`).
    """
    from fastapi import HTTPException

    if not edits:
        raise HTTPException(status_code=400, detail="No edits supplied")

    doc_id = edits[0]["document_id"]
    if any(e["document_id"] != doc_id for e in edits):
        raise HTTPException(status_code=400, detail="Batch edits must target one document")

    # M7: shared fetch+RBAC+scope gate BEFORE acquiring the per-doc lock (mirrors
    # apply_edit_to_document). Rejecting an invalid / unauthorized / cross-project
    # doc_id here keeps the Redis keyspace free of garbage lock keys for targets
    # that would never apply. validate_table_cell_edit (inside the lock, below)
    # re-checks these as defense-in-depth.
    from scope import gate_mutation_target

    await gate_mutation_target(
        user=user, doc_id=doc_id, project_id=project_id, scope_root=scope_root,
    )

    from doc_edit_lock import edit_lock

    # ARCH: per-doc Redis lock — the SAME lock as the text-edit paths. A table-cell
    # edit and a text edit on one doc both touch the document's convergence tail
    # (the Y.Doc + update-log + content_flushed emit), so they must serialize the
    # same way (validate/stale-check reads the live state, then checkpoint + mutate)
    # — on any replica.
    async with edit_lock(doc_id):
        current_tables_json = await validate_table_cell_edit(
            document_id=doc_id, edits=edits, project_id=project_id, user=user,
        )
        current_content, _ = await doc_state.resolve_live_doc_state(doc_id)

        noop = all(e["new_value"] == e["old_value"] for e in edits)
        if noop:
            return {"status": "applied", "noop": True, "document_id": doc_id}

        preview = ", ".join(f"{e['table_id']}[{e['row']},{e['col']}]" for e in edits)[:80]
        await collab_writes._create_agent_pre_edit_checkpoint(
            document_id=doc_id,
            content=current_content,
            tables_json=current_tables_json,
            original_preview=preview,
        )
        for e in edits:
            await route_tables_mutation(
                doc_id=doc_id, table_id=e["table_id"], row=e["row"], col=e["col"],
                new_value=e["new_value"], project_id=project_id,
            )
    await collab_writes.broadcast_agent_presence(doc_id, user.get("user_id"))
    last = edits[-1]
    return {
        "status": "applied", "document_id": doc_id,
        "table_id": last["table_id"], "row": last["row"], "col": last["col"],
    }


async def _resolve_doc_and_state(doc_id: str):
    """Resolve the live Y.Doc for a structural mutation in ONE load: the active
    session's ydoc, or a freshly loaded doc. Returns ``(doc, session, content,
    tables_json)`` — content + tables_json read from that SAME instance so the caller,
    the checkpoint, and the convergence tail all share one load (no redundant DB
    round-trips + update-log replays under the per-doc lock).

    Reads content via ``str(doc.get('content'))`` + tables via ``capture_tables_json(doc)``
    — the same direct-from-Y.Doc read ``resolve_live_doc_state`` does (preserves its
    INVARIANT: never the derived GFM read-model).
    Why: read the live CRDT Text directly (like resolve_live_doc_state); the
    derived GFM read-model can lag the live doc, so editing from it would splice
    stale text."""
    from collab.registry import get_active_session
    from pycrdt import Text

    from table_serialize import capture_tables_json
    from ydoc_store import load as load_ydoc

    session = get_active_session("doc", doc_id)
    doc = session.ydoc if session is not None else await load_ydoc(doc_id)
    content = str(doc.get("content", type=Text))
    tables_json = capture_tables_json(doc)
    return doc, session, content, tables_json


def _dims_from_tables_json(tables_json: str, table_id: str) -> tuple[int, int]:
    """Derive ``(n_rows, n_cols)`` from a ``capture_tables_json`` snapshot.

    Width comes from the ``columns`` array (the AUTHORITATIVE source — matches the
    frontend ``getColumns(table).length`` and the stated "width from LIVE columns"
    contract), NOT from ``rows[0]`` (which diverges for a ragged header or a 0-row
    table). Falls back to the rows matrix only when ``columns`` is absent. Raises
    HTTPException(404) on unknown table_id or a 0-column table.

    # WHY (width source): read ``columns``, not ``rows[0]``. Why: a 0-row-but-
    # has-columns table (reachable — the frontend ``removeRow`` has no minimum-row
    # guard) must still be repopulatable; reading ``rows[0]`` would report n_cols=0 and
    # 404 a table that genuinely has columns. A ragged header would otherwise pad/reject
    # new rows against the wrong width, corrupting the model.
    """
    import json

    from fastapi import HTTPException

    data = json.loads(tables_json) if tables_json else {}
    if table_id not in data:
        raise HTTPException(
            status_code=404, detail={"error": "Unknown table_id", "table_id": table_id},
        )
    model = data[table_id]
    cols = model.get("columns") or []
    rows = model.get("rows") or []
    n_cols = len(cols)
    if n_cols == 0 and rows:  # legacy/defensive fallback when columns is absent
        n_cols = max(len(r) for r in rows)
    if n_cols == 0:
        raise HTTPException(
            status_code=404,
            detail={"error": "Table has no columns", "table_id": table_id, "n_rows": len(rows)},
        )
    return len(rows), n_cols


def _reject_table_overflow(rows_matrix: list[list[str]]) -> None:
    """Reject any cell exceeding ``AGENT_TABLE_MAX_CELL_CHARS`` (resource bound — each
    cell is a Y.Text built inside one ``doc.transaction()``; an unbounded cell balloons
    the CRDT update + broadcast + persistence). No-silent-degradation: 400, not silent
    truncation. The outer row count is already capped by the model ``max_length``."""
    from fastapi import HTTPException

    from config import AGENT_TABLE_MAX_CELL_CHARS

    for row in rows_matrix:
        for cell in row:
            if len(str(cell)) > AGENT_TABLE_MAX_CELL_CHARS:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "cell too large",
                        "max": AGENT_TABLE_MAX_CELL_CHARS, "len": len(str(cell)),
                    },
                )


async def _route_tables_structural(
    *, doc_id: str, doc, session, mutate, project_id: str | None,
) -> None:
    """Convergence tail for a STRUCTURAL table mutation (add_rows / add_column /
    create_table). The structural analog of `route_tables_mutation` (the cell-edit
    path): runs the `mutate(doc)` callback under the live session's `_write_lock`
    (broadcast + publish) or on the pre-loaded Y.Doc (publish + `content_flushed`).

    The doc + session are PRE-RESOLVED by the caller (`_resolve_doc_and_state`) so this
    tail never reloads the Y.Doc — ONE load per apply, not three (the apply function
    read content + tables_json from the same instance for dims + checkpoint).

    # WHY: the callback runs its structural primitive inside ONE `doc.transaction()`
    # so the broadcast update carries the whole change in one frame (no ragged
    # intermediate — matches apply_tables_json / add_table_rows INVARIANT).
    # Why: one transaction makes the broadcast atomic — replicas see the whole
    # structural change in one frame, never a ragged intermediate. The anchor is
    # unchanged for add_rows/add_column (mentions/rebuild skipped, `content_flushed`
    # still emitted); create_table also splices a content anchor inside the SAME callback
    # transaction — a table anchor is NOT a `[[link]]`, so mention rebuild stays skipped.
    """
    from collab.sync import MSG_SYNC, create_update_message, wrap_binary

    from ydoc_store import publish_doc_update

    if session is not None:
        async with session._write_lock:
            mutate(doc)
            update = doc.get_update()
            session._dirty = True
            resync_msg = wrap_binary(doc_id, MSG_SYNC, create_update_message(update))
        await session.broadcast_binary(resync_msg)
        await publish_doc_update(doc_id, update)
        session._updates_since_compact += 1
        return

    mutate(doc)
    update = doc.get_update()
    await publish_doc_update(doc_id, update)

    await event_bus.emit(
        "content_flushed",
        entity_type="doc",
        entity_id=doc_id,
        project_id=project_id,
    )


async def apply_add_table_rows(
    *,
    doc_id: str,
    table_id: str,
    rows_matrix: list[list[str]],
    project_id: str,
    user: dict,
    scope_root: str | None = None,
) -> dict:
    """Append N rows at the bottom of an existing table (the apply primitive for the
    agent ``add_table_rows`` tool). Mirrors `apply_edit_table_cell`'s convergence tail.

    Width comes from the LIVE `columns` (read under the per-doc lock via the
    tables_json snapshot). Shorter caller rows pad empty; an over-length row is rejected
    with 400 (No-silent-degradation). Empty `rows` → 400. Checkpoint captures BEFORE
    `tables_json`. Raises HTTPException on RBAC/not-found (404)/over-length (400)."""
    from fastapi import HTTPException

    if not rows_matrix:
        raise HTTPException(status_code=400, detail="No rows supplied")
    _reject_table_overflow(rows_matrix)

    from scope import gate_mutation_target

    await gate_mutation_target(
        user=user, doc_id=doc_id, project_id=project_id, scope_root=scope_root,
    )

    from doc_edit_lock import edit_lock

    async with edit_lock(doc_id):
        doc, session, content, tables_json = await _resolve_doc_and_state(doc_id)
        _n_rows, n_cols = _dims_from_tables_json(tables_json, table_id)
        for row in rows_matrix:
            if len(row) > n_cols:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "Row wider than the table", "n_cols": n_cols,
                        "row_len": len(row),
                    },
                )
        await collab_writes._create_agent_pre_edit_checkpoint(
            document_id=doc_id,
            content=content,
            tables_json=tables_json,
            original_preview=f"{table_id} +{len(rows_matrix)} rows",
        )

        def _mutate(d):
            from table_serialize import add_table_rows

            add_table_rows(d, table_id, rows_matrix, width=n_cols)

        await _route_tables_structural(
            doc_id=doc_id, doc=doc, session=session, mutate=_mutate, project_id=project_id,
        )
    await collab_writes.broadcast_agent_presence(doc_id, user.get("user_id"))
    return {"status": "applied", "document_id": doc_id, "table_id": table_id}


async def apply_add_table_column(
    *,
    doc_id: str,
    table_id: str,
    at_index: int | None,
    header: str | None,
    values: list[str] | None,
    project_id: str,
    user: dict,
    scope_root: str | None = None,
) -> dict:
    """Insert one column into an existing table (the apply primitive for the agent
    ``add_table_column`` tool). Mirrors `apply_edit_table_cell`'s convergence tail.

    ``at_index=None`` resolves to the END (``n_cols``) after reading the live dims —
    matching frontend ``addColumn``'s ``at ?? columns.length`` default. An explicit
    ``at_index`` must be in ``[0, n_cols]`` (else 400). Checkpoint captures BEFORE
    `tables_json`. Raises HTTPException on RBAC/not-found (404)/bad index (400)."""
    from fastapi import HTTPException

    if header is not None:
        _reject_table_overflow([[header]])
    if values:
        _reject_table_overflow([values])

    from scope import gate_mutation_target

    await gate_mutation_target(
        user=user, doc_id=doc_id, project_id=project_id, scope_root=scope_root,
    )

    from doc_edit_lock import edit_lock

    async with edit_lock(doc_id):
        doc, session, content, tables_json = await _resolve_doc_and_state(doc_id)
        n_rows, n_cols = _dims_from_tables_json(tables_json, table_id)
        resolved_index = n_cols if at_index is None else at_index
        if resolved_index < 0 or resolved_index > n_cols:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "at_index out of range", "n_cols": n_cols, "at_index": at_index,
                },
            )
        await collab_writes._create_agent_pre_edit_checkpoint(
            document_id=doc_id,
            content=content,
            tables_json=tables_json,
            original_preview=f"{table_id} +1 col @ {resolved_index}",
        )

        def _mutate(d):
            from table_serialize import add_table_column

            add_table_column(
                d, table_id, at_index=resolved_index, header=header, values=values, n_rows=n_rows,
            )

        await _route_tables_structural(
            doc_id=doc_id, doc=doc, session=session, mutate=_mutate, project_id=project_id,
        )
    await collab_writes.broadcast_agent_presence(doc_id, user.get("user_id"))
    return {"status": "applied", "document_id": doc_id, "table_id": table_id}


async def apply_create_table(
    *,
    doc_id: str,
    label: str,
    rows_matrix: list[list[str]],
    section: str | None,
    project_id: str,
    user: dict,
    scope_root: str | None = None,
) -> dict:
    """Append a NEW table block (header + initial rows) + its anchor into a document
    (the apply primitive for the agent ``create_table`` tool).

    Generates the ``uuid4`` table id at apply time. Resolves the insertion offset
    (doc tail, or end of `section` via the shared `_section_end_offset` in textmatch) — pre-validated
    at apply-start (404 on unknown section, so the agent self-corrects in-turn), then
    re-resolved against LIVE content inside the mutation callback (TOCTOU-safe under the
    write lock). In ONE ``doc.transaction()`` the callback splices the
    ``![label](table:id)`` anchor into the ``content`` Y.Text AND builds the table model
    via `create_table_entry` — atomic so anchor + model land in one broadcast frame (no
    orphan window). Checkpoint captures BEFORE `content` + `tables_json`. `content_flushed`
    is emitted (mentions/rebuild skipped — a table anchor is not a ``[[link]]``).

    Raises HTTPException on RBAC/not-found/unknown-section (404)."""
    from uuid import uuid4

    from fastapi import HTTPException
    from pycrdt import Text

    from table_serialize import create_table_entry, table_anchor

    if not rows_matrix:
        raise HTTPException(status_code=400, detail="create_table requires rows (rows[0] = header)")
    _reject_table_overflow(rows_matrix)

    from scope import gate_mutation_target

    await gate_mutation_target(
        user=user, doc_id=doc_id, project_id=project_id, scope_root=scope_root,
    )

    from doc_edit_lock import edit_lock
    from textmatch import _section_end_offset

    async with edit_lock(doc_id):
        doc, session, content, tables_json = await _resolve_doc_and_state(doc_id)
        # Pre-validate the section against the apply-start content (404 on unknown —
        # self-correct in-turn, before any checkpoint). The offset itself is re-resolved
        # against LIVE content inside the mutation callback (TOCTOU-safe under the lock).
        if section:
            try:
                _section_end_offset(content, section)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        table_id = str(uuid4())
        label_resolved = label or "Table"

        await collab_writes._create_agent_pre_edit_checkpoint(
            document_id=doc_id,
            content=content,
            tables_json=tables_json,
            original_preview=f"create_table {label_resolved} ({len(rows_matrix)} rows)",
        )

        # WHY: the offset is resolved against LIVE content inside the callback (under the
        # session write lock / on the freshly-loaded doc) so a concurrent collab edit
        # cannot insert at a stale position. `head` (the content prefix up to the offset)
        # is reused for the UTF-8 byte offset — no second full-content materialization.
        # WHY: the anchor splice + the model build run in ONE `doc.transaction()` so a
        # replica never receives the model without its anchor (or vice versa) — an
        # intermediate orphan renders as an error in the widget (table-block orphan
        # INVARIANT). A table anchor is NOT a `[[link]]`/transclusion, so mention
        # rebuild is correctly skipped (only `content_flushed` is emitted by the tail).
        def _splice_and_build(d):
            live = str(d.get("content", type=Text))
            offset = _section_end_offset(live, section) if section else len(live)
            head = live[:offset]
            byte_offset = len(head.encode("utf-8"))
            trailing_nl = len(head) - len(head.rstrip("\n"))
            sep = "\n" * max(0, 2 - trailing_nl)
            anchor_text = sep + table_anchor(label_resolved, table_id)
            content_text = d.get("content", type=Text)
            with d.transaction():
                content_text.insert(byte_offset, anchor_text)
                create_table_entry(d, table_id, rows_matrix)

        await _route_tables_structural(
            doc_id=doc_id, doc=doc, session=session, mutate=_splice_and_build,
            project_id=project_id,
        )
    await collab_writes.broadcast_agent_presence(doc_id, user.get("user_id"))
    return {"status": "applied", "document_id": doc_id, "table_id": table_id}
