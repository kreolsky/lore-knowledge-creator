"""Table-cell links must be doc_mentions edges in EVERY rebuild writer.

# SYSTEM: mentions — the GFM read-model form is the ONE extraction input.

The defect this pins: mention edges were rebuilt from the ANCHOR-form text
(`![label](table:id)` — cell links invisible) by the collab flush and the REST
PATCH fallback, while the agent finalize tail rebuilt from the expanded form.
The anchor-form writers DELETED the agent's cell edges on the next flush, so a
table cell's `[x](doc:target)` vanished from the backlinks/outgoing panel until
the agent wrote again (appear-on-reload, gone-after-first-flush).
"""

import json

import pytest
from collab.registry import _get_or_create_session, _session_key, _sessions
from helpers import apply_binary_edit, cell_ids

from db import extract_id, get_db
from table_serialize import expand_tables, set_table_cell
from transclusion_grammar import SCHEME_TABLE
from ydoc_store import load, set_content


async def _drop_session(doc_id: str) -> None:
    """Pop a session (server-restart simulation) — mirrors test_collab_persistence."""
    session = _sessions.pop(_session_key("doc", doc_id), None)
    if session is None:
        return
    session.stop_periodic_flush()
    if session._batch_task and not session._batch_task.done():
        session._batch_task.cancel()
    try:
        await session.unsubscribe_backplane()
    except Exception:
        pass


async def _mentions_out(doc_id: str) -> set[tuple[str, str]]:
    db = await get_db()
    rows = await db.query(
        "SELECT out, kind FROM doc_mentions WHERE in = type::record('documents', $id)",
        {"id": doc_id},
    )
    return {(extract_id(r["out"]), r.get("kind") or "mention") for r in (rows or [])}


async def _mk_doc(client, token, pid: str, title: str) -> str:
    r = await client.post(
        "/api/documents", json={"project_id": pid, "title": title, "content": "body"},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _seed_table_with_cell_link(source: str, target: str, pid: str) -> None:
    """Table whose (1,1) cell links to `target`; edges seeded the way the agent
    finalize tail seeds them (expanded form) — the state the user's doc had."""
    from agent.doc_state import finalize_content_mutation

    anchor = "Intro\n\n![t](table:t1)"
    tables = json.dumps({
        "t1": {"columns": [160, 160],
               "rows": [["Hdr", "Val"], ["see", f"[target]({target})"]]},
    })
    await set_content(source, anchor, persist=True, tables_json=tables)
    await finalize_content_mutation(source, expand_tables(await load(source)), pid)


async def _seed_table_without_link(source: str, pid: str) -> None:
    anchor = "Intro\n\n![t](table:t1)"
    tables = json.dumps({
        "t1": {"columns": [160, 160], "rows": [["Hdr", "Val"], ["see", ""]]},
    })
    await set_content(source, anchor, persist=True, tables_json=tables)


async def _seed_table_with_cell_url(source: str) -> None:
    """Table whose (1,1) cell holds an external link — no doc-target pairs."""
    anchor = "Intro\n\n![t](table:t1)"
    tables = json.dumps({
        "t1": {"columns": [160, 160], "rows": [["Hdr", "Val"], ["see", "[x](https://example.com/a)"]]},
    })
    await set_content(source, anchor, persist=True, tables_json=tables)


# After the agent wrote a table with a cell link, BOTH no-session rebuild writers
# (the collab flush and the REST PATCH fallback) rebuilt doc_mentions from the
# ANCHOR form and DELETED the cell edge; live client + real DB, the web→SurrealDB
# boundary is driven, not mocked.
# TEST-WHY: cross-process-contract — every mention rebuild writer keeps table-cell link edges (flush + REST PATCH).
@pytest.mark.asyncio
async def test_rebuild_writers_keep_table_cell_link_edges(client, collab_project, _clear_sessions):
    pid, _doc, admin_token, *_ = collab_project
    target = await _mk_doc(client, admin_token, pid, "Target")
    other = await _mk_doc(client, admin_token, pid, "Other")
    source = await _mk_doc(client, admin_token, pid, "Source")
    await _seed_table_with_cell_link(source, target, pid)
    assert (target, "mention") in await _mentions_out(source), "seed step broken"

    # Writer 1 — the collab flush (live session). A TEXT link edit is the pair
    # delta that makes the flush rebuild run.
    session = await _get_or_create_session("doc", source, "Intro")
    await apply_binary_edit(session, f"see [other]({other}) ")
    await session.flush_to_db()

    mentions = await _mentions_out(source)
    assert (other, "mention") in mentions, "the text link edit lost its own edge"
    assert (target, "mention") in mentions, \
        "a collab flush deleted the table-cell link edge"
    await _drop_session(source)

    # Writer 2 — the REST PATCH fallback (no live session). Anchor-form PATCH
    # body, exactly what the editor autosave sends.
    r = await client.patch(
        f"/api/documents/{source}",
        json={"content": "Intro plus a line\n\n![t](table:t1)"},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text

    assert (target, "mention") in await _mentions_out(source), \
        "a REST PATCH deleted the table-cell link edge"


# A widget cell edit changes ONLY the tables map: the flush early-return on
# unchanged anchor text skipped it (the cell never reached documents.content NOR
# doc_mentions), and /links read the live session's ANCHOR text and missed cell
# links while the edges had them.
# TEST-WHY: cross-process-contract — a table-only cell edit reaches documents.content, doc_mentions, and /links (live session).
@pytest.mark.asyncio
async def test_table_only_edit_reaches_read_model_edges_and_links(client, collab_project, _clear_sessions):
    from emit_recorder import EmitRecorder

    pid, _doc, admin_token, *_ = collab_project
    target = await _mk_doc(client, admin_token, pid, "Target")
    source = await _mk_doc(client, admin_token, pid, "Source")
    await _seed_table_without_link(source, pid)
    assert await _mentions_out(source) == set()

    session = await _get_or_create_session("doc", source, "Intro")
    assert set_table_cell(session.ydoc, "t1", *cell_ids(session.ydoc, "t1", 1, 1), f"[target]({target})")
    session._dirty = True
    with EmitRecorder.active() as rec:
        await session.flush_to_db()

    db = await get_db()
    row = (await db.query(
        "SELECT content FROM type::record('documents', $id)", {"id": source}
    ))[0]["content"]
    assert f"[target]({target})" in row, "table-only edit did not reach documents.content"
    assert (target, "mention") in await _mentions_out(source), \
        "table-only edit did not create the cell link edge"
    assert rec.of("backlinks_changed_batch"), \
        "panel was not nudged (no backlinks_changed_batch)"

    # /links must see the cell link on the LIVE session (a client is connected):
    # it reads the GFM read-model (expand_tables of the session Y.Doc), not the
    # anchor-form session text.
    ws = type("W", (), {"send_bytes": _noop, "send_text": _noop})()
    session.add_client(ws, "u1", "User", "full")
    r = await client.get(
        f"/api/documents/{source}/links", cookies={"lore_session": admin_token}
    )
    assert r.status_code == 200, r.text
    assert target in (r.json().get("document_ids") or []), \
        "/links missed the table-cell link on the live session"


# The registry seed and the flush detector must build the SAME pair set (the
# content_edge_pairs contract, both from the GFM read-model form); a drifted seed
# makes every flush rebuild + re-emit forever. Oracle: a rebuild ALWAYS emits
# backlinks_changed_batch (the emit is unconditional after rebuild_doc_mentions),
# so NO event on a link-free reopen edit == no spurious rebuild.
# TEST-WHY: cross-process-contract — reopen + link-free edit does NOT rebuild mentions (emit oracle, no own-module mock).
@pytest.mark.asyncio
async def test_reopen_edit_flush_no_spurious_rebuild(client, collab_project, _clear_sessions):
    from emit_recorder import EmitRecorder

    pid, _doc, admin_token, *_ = collab_project
    target = await _mk_doc(client, admin_token, pid, "Target")
    source = await _mk_doc(client, admin_token, pid, "Source")
    await _seed_table_with_cell_link(source, target, pid)

    session = await _get_or_create_session("doc", source, "Intro")
    await apply_binary_edit(session, "more ")
    await session.flush_to_db()
    await _drop_session(source)

    session2 = await _get_or_create_session("doc", source, "Intro")
    await apply_binary_edit(session2, "even ")
    with EmitRecorder.active() as rec:
        await session2.flush_to_db()
    assert not rec.of("backlinks_changed_batch"), \
        "reopen + link-free edit flushed a rebuild (seed != detector)"


# A URL-only cell edit changes no (target, kind) pair — the pair detector never
# fires — but the /edges `external` projection reads the read-model, and
# backlinks_changed_batch is the panel's only refetch trigger: the URL-set
# detector must add the source to the emit on its own. And the registry URL
# seed must equal the flush detector's set (the content_external_urls contract)
# — a drifted seed makes every reopen's first flush emit a false refresh.
# TEST-WHY: cross-process-contract — a URL-only collab edit emits backlinks_changed for the source; reopen + URL-free edit emits nothing (emit oracle, no own-module mock).
@pytest.mark.asyncio
async def test_url_only_cell_edit_emits_and_unchanged_reopen_stays_silent(
    client, collab_project, _clear_sessions,
):
    from emit_recorder import EmitRecorder

    pid, _doc, admin_token, *_ = collab_project
    source = await _mk_doc(client, admin_token, pid, "Source")
    await _seed_table_with_cell_url(source)

    session = await _get_or_create_session("doc", source, "Intro")
    assert set_table_cell(session.ydoc, "t1", *cell_ids(session.ydoc, "t1", 1, 1), "[x](https://example.com/b)")
    session._dirty = True
    with EmitRecorder.active() as rec:
        await session.flush_to_db()
    batches = rec.of("backlinks_changed_batch")
    assert batches and any(source in b["document_ids"] for b in batches), \
        "a URL-only cell edit must nudge the source's panel"

    await _drop_session(source)
    session2 = await _get_or_create_session("doc", source, "Intro")
    await apply_binary_edit(session2, "even ")
    with EmitRecorder.active() as rec2:
        await session2.flush_to_db()
    assert not rec2.of("backlinks_changed_batch"), \
        "reopen + URL-free edit emitted a URL-set false refresh (seed != detector)"


def _registry_forms(target: str, ref_a: str, ref_b: str):
    """(forms, expected_pairs, expected_external) DERIVED from SCHEME_TABLE.

    One markdown form per registry row's meaning: pair-producing prefixes get
    their mention + embed forms, reject prefixes get one rejected form, http(s)
    additionally the external form. bare-doc (derived, never listed) and the
    sized-image embed (the operator-reported form) parse through the same
    grammar and are appended. An unknown prefix raises — a new SCHEME_TABLE row
    joins this proof only by being taught a case here.
    """
    forms: list[str] = []
    pairs: set[tuple[str, str]] = set()
    external: list[tuple[str, str]] = []
    for prefix, _is_t, _is_doc in SCHEME_TABLE:
        if prefix == "doc:":
            forms += [f"[d-m](doc:{target})", f"![d-e](doc:{target})"]
            pairs |= {(target, "mention"), (target, "transclusion")}
        elif prefix == "ref:":
            forms += [f"[r-m](ref:{ref_a})", f"![r-e](ref:{ref_a})"]
            pairs |= {(ref_a, "mention"), (ref_a, "transclusion")}
        elif prefix == "table:":
            forms.append("![t-e](table:nonexistent)")
        elif prefix in ("http:", "https:"):
            label = f"{prefix[:-1]}-ext"
            url = f"{prefix}//example.com/{prefix[:-1]}"
            forms.append(f"[{label}]({url})")
            external.append((label, url))
        elif prefix in ("mailto:", "#", "data:"):
            forms.append(f"[{prefix}x]({prefix}x)")
        else:
            raise AssertionError(f"SCHEME_TABLE row {prefix!r} has no parity case")
    forms += [f"[b-m]({target})", f"![b-e]({target})", f"![img|320](ref:{ref_b})"]
    pairs |= {(target, "mention"), (target, "transclusion"), (ref_b, "transclusion")}
    return forms, pairs, external


def _entry_key(e: dict) -> str:
    return json.dumps({k: v for k, v in e.items()
                       if k not in ("title", "updated_at")}, sort_keys=True)


async def _out_edges(client, token: str, doc_id: str) -> list[dict]:
    r = await client.get(
        f"/api/documents/{doc_id}/edges",
        params={"direction": "out", "kinds": "mention,transclusion,external"},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["edges"]


# The parity proof: the SAME link syntax in
# host text and in a table cell answers IDENTICALLY on /edges — every
# SCHEME_TABLE form plus bare-doc and the sized image embed, read with a LIVE
# session (client attached) and again from the idle row. Live client + real DB;
# the flush→doc_mentions→/edges boundary is driven, not mocked. The sized
# `![img|320](ref:…)` form is the operator-reported instance (2026-11-10).
# TEST-WHY: cross-process-contract — /edges parity host vs cell for every registry link form, live session and idle row.
@pytest.mark.asyncio
async def test_edges_parity_host_vs_cell_registry_forms(client, collab_project, _clear_sessions):
    from db import create_record

    pid, _doc, token, *_ = collab_project
    target = await _mk_doc(client, token, pid, "Target")
    host = await _mk_doc(client, token, pid, "Host")
    cell_doc = await _mk_doc(client, token, pid, "CellDoc")
    # The refs' parent is a THIRD doc: attachment edges (projected from
    # parent_id) must not land on one variant only and skew the comparison.
    carrier = await _mk_doc(client, token, pid, "Carrier")
    ref_a, ref_b = "ref-a-parity", "ref-b-parity"
    for rid in (ref_a, ref_b):
        await create_record("documents", rid, {
            "project_id": pid, "parent_id": carrier, "title": rid,
            "content": "", "path": f"_ref/{rid}.md", "is_reference": True,
            "media_type": "markdown",
        })

    forms, expected_pairs, expected_external = _registry_forms(target, ref_a, ref_b)

    # HOST variant — the forms as a live-session host-text edit (the UI path).
    session_h = await _get_or_create_session("doc", host, "h")
    await apply_binary_edit(session_h, "\n".join(forms) + "\n")
    await session_h.flush_to_db()

    # CELL variant — one form per cell, applied as live-session cell edits.
    await set_content(
        cell_doc, "Intro\n\n![t](table:t1)", persist=True,
        tables_json=json.dumps({
            "t1": {"columns": [320, 120],
                   "rows": [["f", "v"]] + [["", ""] for _ in forms]},
        }),
    )
    session_c = await _get_or_create_session("doc", cell_doc, "Intro")
    for i, form in enumerate(forms):
        assert set_table_cell(session_c.ydoc, "t1", *cell_ids(session_c.ydoc, "t1", i + 1, 0), form)
    session_c._dirty = True
    await session_c.flush_to_db()

    # Stored pairs: identical AND exactly the registry-derived expectation
    # (reject forms — mailto/#/data/table — produce nothing on either side).
    assert await _mentions_out(host) == expected_pairs
    assert await _mentions_out(cell_doc) == expected_pairs

    # LIVE read — a client is attached to each session, so the external
    # projection parses the live session's expand_tables, not the DB row.
    for session in (session_h, session_c):
        ws = type("W", (), {"send_bytes": _noop, "send_text": _noop})()
        session.add_client(ws, "u1", "User", "full")
    live_host = await _out_edges(client, token, host)
    live_cell = await _out_edges(client, token, cell_doc)
    assert sorted(map(_entry_key, live_host)) == sorted(map(_entry_key, live_cell)), \
        "the same link forms answer differently in host text vs a table cell"

    expected = [
        json.dumps({"direction": "out", "kind": kind, "document_id": tid,
                    "is_reference": tid in (ref_a, ref_b),
                    "source_type": "ref" if tid in (ref_a, ref_b) else "doc",
                    "broken": False}, sort_keys=True)
        for tid, kind in expected_pairs
    ] + [
        json.dumps({"direction": "out", "kind": "external", "url": url, "label": label},
                   sort_keys=True)
        for label, url in expected_external
    ]
    assert sorted(map(_entry_key, live_host)) == sorted(expected), \
        "out entries are not exactly the registry-derived expectation"

    # IDLE read — sessions dropped; stored kinds read doc_mentions and the
    # external projection falls back to documents.content (the expanded row).
    await _drop_session(host)
    await _drop_session(cell_doc)
    idle_host = await _out_edges(client, token, host)
    idle_cell = await _out_edges(client, token, cell_doc)
    assert sorted(map(_entry_key, idle_host)) == sorted(map(_entry_key, live_host)), \
        "host entries changed once the session idled"
    assert sorted(map(_entry_key, idle_cell)) == sorted(map(_entry_key, live_cell)), \
        "cell entries changed once the session idled"

    # Incoming direction: every target sees BOTH sources under the same kinds.
    for doc_id, kinds in (
        (target, {"mention", "transclusion"}),
        (ref_a, {"mention", "transclusion"}),
        (ref_b, {"transclusion"}),
    ):
        r = await client.get(
            f"/api/documents/{doc_id}/edges",
            params={"direction": "in", "kinds": "mention,transclusion"},
            cookies={"lore_session": token},
        )
        assert r.status_code == 200, r.text
        got = {(e["kind"], e["document_id"]) for e in r.json()["edges"]}
        assert got == {(k, s) for k in kinds for s in (host, cell_doc)}, \
            f"incoming edges on {doc_id} are not parity-complete"


async def _noop(*_a, **_kw):
    pass
