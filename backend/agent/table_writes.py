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

from fastapi import HTTPException
from pycrdt import Map

import event_bus
from agent import collab_writes, doc_state
from table_serialize import resolve_merges

logger = logging.getLogger(__name__)


def _resolve_table_col(column: str | None, col_fallback: int | None,
                       headers: list[str]) -> int:
    """Resolve a header-name `column` to a 0-based col index; fall back to the
    numeric `col` when `column` is None/blank. Header names are matched against
    the header row, case- and whitespace-insensitive. Raises HTTPException(404) with the
    available headers when a named column is not found.

    # INVARIANT: neither coordinate → HTTPException(400), never TypeError→500.
    # Why: `col` is un-advertised, so the model sends only `column`; a proposal that
    # dropped it (col=None, column=None) must fail-soft so the agent self-corrects,
    # matching the confirm-route contract (edit-table-cell-column-apply-fix).
    """
    from fastapi import HTTPException

    if not column or not column.strip():
        if col_fallback is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "edit_table_cell needs a column name or col index"},
            )
        return col_fallback
    if not headers:
        raise HTTPException(status_code=404, detail={"error": "Table has no header row"})
    want = column.strip().lower()
    for i, h in enumerate(headers):
        if h.strip().lower() == want:
            return i
    raise HTTPException(
        status_code=404,
        detail={"error": "Unknown column", "column": column, "headers": headers},
    )


def _require_readable_table(doc, table_id: str) -> Map:
    """The live table model, or HTTPException 404 (unknown id) / 409 (unreadable format).

    # WHY: every agent table write checks this on the live ydoc BEFORE reading the
    # ``tables_json`` snapshot — capture omits an unreadable table, so a snapshot lookup
    # alone would report it as an unknown id.
    """
    from table_serialize import table_format_unknown

    tables = doc.get("tables", type=Map)
    if tables is None or table_id not in tables:
        raise HTTPException(
            status_code=404, detail={"error": "Unknown table_id", "table_id": table_id},
        )
    if table_format_unknown(tables[table_id]):
        raise HTTPException(
            status_code=409, detail={"error": "Unreadable table format", "table_id": table_id},
        )
    return tables[table_id]


def resolve_cell(doc, table_id: str, row: int, column: str | None,
                 col_fallback: int | None) -> tuple[str, str, int]:
    """The agent tool entry's position → id boundary for ONE cell: row index + header
    name (or col index) → ``(row_id, col_id, col_index)`` against the LIVE order.

    # INVARIANT(persisted): the ONLY place an agent cell position becomes ids; every
    # write below it is id-addressed. Why: a position goes stale when a peer inserts a
    # row; resolving once, under the per-doc edit lock, pins the edit to the cell the
    # agent saw.
    Raises HTTPException 404 for an unknown table / out-of-bounds cell (detail carries
    n_rows / n_cols / the target row so the model self-corrects), 400 for no column.
    """
    from table_serialize import ordered_ids, read_table_cell

    table = _require_readable_table(doc, table_id)
    row_ids, col_ids = ordered_ids(table)
    n_rows, n_cols = len(row_ids), len(col_ids)

    def _row_texts(rid: str) -> list[str]:
        return [read_table_cell(doc, table_id, rid, cid) or "" for cid in col_ids]

    headers = _row_texts(row_ids[0]) if n_rows else []
    col = _resolve_table_col(column, col_fallback, headers)
    if row < 0 or row >= n_rows or col < 0 or col >= n_cols:
        target_row = (
            [{"col": i, "t": t} for i, t in enumerate(_row_texts(row_ids[row]))]
            if 0 <= row < n_rows else None
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Cell out of bounds", "n_rows": n_rows, "n_cols": n_cols,
                "row": target_row,
            },
        )
    _reject_covered_cell(table, row, col)
    return row_ids[row], col_ids[col], col


def _covering_area(areas: list[dict], row: int, col: int) -> dict | None:
    """The accepted area that HIDES cell ``(row, col)`` — contains it and is not anchored
    on it; ``None`` for a visible cell."""
    return next(
        (a for a in areas
         if a["r0"] <= row <= a["r1"] and a["c0"] <= col <= a["c1"]
         and (row, col) != (a["r0"], a["c0"])),
        None,
    )


def _reject_covered_cell(table, row: int, col: int) -> None:
    """Refuse a cell hidden under a merged area, naming the anchor to edit instead."""
    # INVARIANT: a cell hidden under a merged area is never written by the agent.
    # Why: its text would be invisible in the editor.
    if area := _covering_area(resolve_merges(table), row, col):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Cell is covered by a merged area; edit its anchor cell",
                "anchor": {"row": area["r0"], "col": area["c0"]},
            },
        )


async def validate_table_cell_edit(
    *, document_id: str, edits: list[dict], project_id: str, user: dict,
) -> str | None:
    """Resolve + gate a batch of table-cell edits (steps 1-4 of the apply contract).
    Shared by the confirm-mode gate (routes.tool_api.edits, BEFORE `_create_proposal`) and
    `apply_edit_table_cell` — the two must never diverge (mirrors `resolve_edit_range`
    being shared by the `edit_document` gate and apply).

    Each edit dict: {table_id, row, col|column, old_value}. ALL edits are validated before
    any is applied (all-or-nothing). Each edit is resolved through `resolve_cell`, and
    the resolved `row_id` / `col_id` (plus the numeric `col`) are written back onto the
    edit dict so the apply tail and the proposal record use them. Returns
    `current_tables_json` on success. Raises HTTPException:
      404 — doc missing/deleted/cross-project, OR unknown table_id/row/col.
      403 — access below "full".
      409 — old_value stale (detail carries current_value).

    # WHY: coordinate resolution (not substring) — an empty `old_value`
    # Why: str_replace can't target an empty cell (""); coordinates resolve it correctly.
    """
    from fastapi import HTTPException

    # Shared fetch+RBAC gate. scope_root=None → the subtree wall no-ops (this is the
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

    for e in edits:
        row_id, col_id, col = resolve_cell(
            doc, e["table_id"], e["row"], e.get("column"), e.get("col"),
        )
        e["row_id"], e["col_id"], e["col"] = row_id, col_id, col
        current_value = read_table_cell(doc, e["table_id"], row_id, col_id)
        if current_value != e["old_value"]:
            raise HTTPException(
                status_code=409,
                detail={"error": "stale", "current_value": current_value},
            )
    return current_tables_json


async def route_tables_mutation(
    *, doc_id: str, table_id: str, row_id: str, col_id: str, new_value: str,
    project_id: str | None,
) -> None:
    """Single convergence path for agent table-cell mutations (mirrors
    `route_document_content` for the `content` Text — this is the `tables`-subtree
    analog). A cell edit changes `ydoc.getMap('tables')`, NOT `content`; the
    anchor is unchanged so mentions/rebuild are skipped, but `content_flushed` is
    still emitted (cell text feeds embeddings/search via `expand_tables`).

    Resolves the live session / Y.Doc itself (no dims or checkpoint needed),
    wraps the cell write in a `mutate` callback and rides
    `_tables_convergence_tail` — the ONE convergence tail shared with the
    structural applies.
    """
    from collab.registry import get_active_session

    from table_serialize import set_table_cell
    from ydoc_store import load as load_ydoc

    session = get_active_session("doc", doc_id)
    doc = session.ydoc if session is not None else await load_ydoc(doc_id)

    def _mutate(d):
        set_table_cell(d, table_id, row_id, col_id, new_value)

    await _tables_convergence_tail(
        doc_id=doc_id, doc=doc, session=session, mutate=_mutate,
        project_id=project_id,
    )


async def apply_edit_table_cell(
    *,
    edits: list[dict],
    project_id: str,
    user: dict,
    scope_root: str | None = None,
) -> dict:
    """Apply one or more table-cell edits atomically (batch-ready internally). Each edit dict: {document_id, table_id, row, col, old_value,
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

    # Shared fetch+RBAC+scope gate BEFORE acquiring the per-doc lock (mirrors
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
                doc_id=doc_id, table_id=e["table_id"], row_id=e["row_id"],
                col_id=e["col_id"], new_value=e["new_value"], project_id=project_id,
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


def _live_ids(doc, table_id: str) -> tuple[list[str], list[str]]:
    """``(row_ids, col_ids)`` of a table in the live display order (the order the
    ``tables_json`` snapshot the dims came from was projected in)."""
    from pycrdt import Map

    from table_serialize import ordered_ids

    return ordered_ids(doc.get("tables", type=Map)[table_id])


def _reject_text_into_merged_area(doc, table_id: str, at_index: int,
                                  cell_values: list[str]) -> None:
    """Refuse a column inserted strictly inside a merged area (``c0 < at_index <= c1``)
    when it would put non-empty text into a cell the area then covers. An empty insert
    is allowed: it widens the area by itself (the end is an id)."""
    for a in resolve_merges(doc.get("tables", type=Map)[table_id]):
        hidden = cell_values[a["r0"]:a["r1"] + 1]
        if a["c0"] < at_index <= a["c1"] and any(v.strip() for v in hidden):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "Column inserted inside a merged area cannot carry text into its covered cells",
                    "anchor": {"row": a["r0"], "col": a["c0"]},
                    "rows": [a["r0"], a["r1"]],
                },
            )


def reject_table_overflow(rows_matrix: list[list[str]]) -> None:
    """Reject any cell exceeding ``AGENT_TABLE_MAX_CELL_CHARS`` (resource bound — each
    cell is a Y.Text built inside one ``doc.transaction()``; an unbounded cell balloons
    the CRDT update + broadcast + persistence). No-silent-degradation: 400, not silent
    truncation. The outer row count is already capped by the model ``max_length``.

    # Public (no underscore): the collab create factories (agent.collab_writes)
    # reuse it for agent-AUTHORED bodies that convert on create
    # (``check_cell_cap``). File imports never call it — they are bounded by
    # their file-size cap upstream instead, so the same ``.md`` behaves
    # identically on every import route."""
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


async def _tables_convergence_tail(
    *, doc_id: str, doc, session, mutate, project_id: str | None,
) -> None:
    """The ONE convergence tail for EVERY `tables`-subtree mutation — the
    structural applies (add_rows / add_column / create_table via
    `_apply_tables_tool`) AND the cell-edit path (`route_tables_mutation`).
    Runs the `mutate(doc)` callback under
    the live session's `_write_lock` (broadcast + publish) or on the
    pre-loaded Y.Doc (publish + `content_flushed`).

    The doc + session are PRE-RESOLVED by the caller (`_resolve_doc_and_state`
    for the structural applies; `route_tables_mutation` resolves its own) so this
    tail never reloads the Y.Doc — ONE load per apply, not three (the apply
    function read content + tables_json from the same instance for dims +
    checkpoint).

    # WHY: the callback runs its whole mutation in ONE broadcast frame —
    # structural callbacks wrap their primitive in ONE `doc.transaction()` so the
    # update carries the entire change (no ragged intermediate — matches
    # apply_tables_json / add_table_rows INVARIANT).
    # Why: replicas never see a ragged intermediate. The anchor is unchanged for
    # add_rows/add_column; create_table splices its anchor in the SAME
    # transaction — a table anchor is NOT a `[[link]]`, so mentions stay skipped
    # (imported/agent-created tables rebuild cell links at THEIR write sites, on EXPANDED text).
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


async def _apply_tables_tool(
    *, doc_id: str, project_id: str, user: dict, scope_root: str | None,
    inside_lock,
) -> dict:
    """The structural applies' shared frame (add_table_rows / add_table_column /
    create_table): full-access gate → per-doc edit lock → ONE doc load
    (`_resolve_doc_and_state`) → the tool middle (`inside_lock(doc, content,
    tables_json) -> (table_id, preview, mutate)`: validate against the live
    dims, mint ids, build the checkpoint preview + the mutation callback) →
    pre-edit checkpoint → `_tables_convergence_tail` → presence broadcast.

    # INVARIANT(security): identical shape to `apply_edit_table_cell` — full-access
    # gate, pre-edit checkpoint, presence broadcast. Why: tables must carry the
    # same RBAC / checkpoint / broadcast guarantees as document edits.
    # ORDER: `inside_lock`'s validations run BEFORE the checkpoint, so a refused
    # validation leaves no checkpoint row and the row untouched.
    """
    from scope import gate_mutation_target

    await gate_mutation_target(
        user=user, doc_id=doc_id, project_id=project_id, scope_root=scope_root,
    )

    from doc_edit_lock import edit_lock

    async with edit_lock(doc_id):
        doc, session, content, tables_json = await _resolve_doc_and_state(doc_id)
        table_id, preview, mutate = inside_lock(doc, content, tables_json)
        await collab_writes._create_agent_pre_edit_checkpoint(
            document_id=doc_id,
            content=content,
            tables_json=tables_json,
            original_preview=preview,
        )
        await _tables_convergence_tail(
            doc_id=doc_id, doc=doc, session=session, mutate=mutate,
            project_id=project_id,
        )
    await collab_writes.broadcast_agent_presence(doc_id, user.get("user_id"))
    return {"status": "applied", "document_id": doc_id, "table_id": table_id}


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
    `tables_json`. Raises HTTPException on RBAC/not-found (404)/unreadable format (409)/over-length (400)."""
    from fastapi import HTTPException

    if not rows_matrix:
        raise HTTPException(status_code=400, detail="No rows supplied")
    reject_table_overflow(rows_matrix)

    def _inside_lock(doc, content, tables_json):
        _require_readable_table(doc, table_id)
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

        _row_ids, col_ids = _live_ids(doc, table_id)
        rows_by_id = [dict(zip(col_ids, (str(c) for c in row))) for row in rows_matrix]

        def _mutate(d):
            from table_serialize import add_table_rows

            add_table_rows(d, table_id, rows_by_id)

        return table_id, f"{table_id} +{len(rows_matrix)} rows", _mutate

    return await _apply_tables_tool(
        doc_id=doc_id, project_id=project_id, user=user, scope_root=scope_root,
        inside_lock=_inside_lock,
    )


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
    `tables_json`. Raises HTTPException on RBAC/not-found (404)/unreadable format (409)/bad index (400)."""
    from fastapi import HTTPException

    if header is not None:
        reject_table_overflow([[header]])
    if values:
        reject_table_overflow([values])

    def _inside_lock(doc, content, tables_json):
        _require_readable_table(doc, table_id)
        _n_rows, n_cols = _dims_from_tables_json(tables_json, table_id)
        resolved_index = n_cols if at_index is None else at_index
        if resolved_index < 0 or resolved_index > n_cols:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "at_index out of range", "n_cols": n_cols, "at_index": at_index,
                },
            )

        # Position → ids at the tool boundary: the gap at `resolved_index` becomes its
        # two neighbour column ids; `header` / `values` (0-based over DATA rows) become
        # texts keyed by row id in the live row order.
        row_ids, col_ids = _live_ids(doc, table_id)
        before_id = col_ids[resolved_index - 1] if resolved_index > 0 else None
        after_id = col_ids[resolved_index] if resolved_index < len(col_ids) else None
        cell_values = [header or ""] + list(values or [])
        _reject_text_into_merged_area(doc, table_id, resolved_index, cell_values)
        texts = dict(zip(row_ids, cell_values))

        def _mutate(d):
            from table_serialize import add_table_column

            add_table_column(d, table_id, before_id=before_id, after_id=after_id, texts=texts)

        return table_id, f"{table_id} +1 col @ {resolved_index}", _mutate

    return await _apply_tables_tool(
        doc_id=doc_id, project_id=project_id, user=user, scope_root=scope_root,
        inside_lock=_inside_lock,
    )


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
    from textmatch import _section_end_offset

    from table_serialize import create_table_entry, table_anchor

    if not rows_matrix:
        raise HTTPException(status_code=400, detail="create_table requires rows (rows[0] = header)")
    reject_table_overflow(rows_matrix)

    def _inside_lock(doc, content, tables_json):
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

        return (
            table_id,
            f"create_table {label_resolved} ({len(rows_matrix)} rows)",
            _splice_and_build,
        )

    return await _apply_tables_tool(
        doc_id=doc_id, project_id=project_id, user=user, scope_root=scope_root,
        inside_lock=_inside_lock,
    )
