"""PR4 R3: the create-document apply entry point (Tool-API direct) delegates to
ONE `create_document_via_collab`. The copy-pasted create body is deleted; the
path converges on the single source.

Also covers review finding #7: the shared content-mutation tail (CRDT route →
set_content fallback → mention rebuild → emit) is extracted so the edit apply
path (`tool_api_surface.apply_edit_to_document`) calls the one convergence helper.

These are structural / delegation tests: they prove a single source of truth
without depending on the dev DB's live CRDT backplane.
"""
import inspect

import pytest
from emit_recorder import EmitRecorder


async def _raw_parent(test_db, doc_id):
    """Stored parent_id of a raw row (None = at root; option<string> field)."""
    rows = await test_db.query(
        "SELECT parent_id FROM type::record('documents', $id)", {"id": doc_id},
    )
    return rows[0].get("parent_id")


# ─── Defect: index-doc parent normalises to root (documents only) ────────────


@pytest.mark.asyncio
async def test_document_created_under_index_stored_at_root(
    client, test_db, admin_user, project_with_doc,
):
    """A DOCUMENT created with parent_id = the index doc is stored at the
    project root: the tree renders the index row childless, so a document
    under it exists but is invisible."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user

    r = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Under index", "parent_id": idx_id},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["document_id"]
    assert await _raw_parent(test_db, doc_id) is None

    # Read-back contract: a root doc's GET row carries no parent_id key.
    g = await client.get(
        f"/api/documents/{doc_id}", cookies={"lore_session": token},
    )
    assert g.status_code == 200, g.text
    assert g.json().get("parent_id") is None


@pytest.mark.asyncio
async def test_reference_created_under_index_keeps_index_host(
    client, test_db, admin_user, project_with_doc,
):
    """CARVE-OUT: for a reference, parent_id = index_doc_id is the canonical
    project-level host (resolve_reference_host / documents_reference_parent_
    check) — it must NOT be normalised to root."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user

    r = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Project-level ref",
              "is_reference": True, "media_type": "markdown", "content": "b",
              "parent_id": idx_id},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    ref_id = r.json()["document_id"]
    assert await _raw_parent(test_db, ref_id) == idx_id


@pytest.mark.asyncio
async def test_agent_create_under_index_stores_root_and_emits_root(
    test_db, project_with_doc,
):
    """The agent create (Tool-API single source) stores root AND broadcasts
    parent_id=None — the client tree inserts from the event, so emitting the
    raw index id would keep the doc invisible until reload."""
    pid, idx_id, uid = project_with_doc
    from agent.collab_writes import create_document_via_collab

    with EmitRecorder.active() as emitted:
        created = await create_document_via_collab(
            title="Agent under index", content="", parent_id=idx_id,
            project_id=pid, user={"user_id": uid, "name": "t"},
        )
    assert await _raw_parent(test_db, created["doc_id"]) is None
    events = emitted.of("document_created")
    assert events, "create emitted no document_created"
    assert events[0]["parent_id"] is None


def test_create_document_via_collab_exists_in_collab_writes():
    """The shared create primitive has one owner: collab_writes (its home since
    plan fewer-layers; apply_edit_to_document stays in tool_api_surface)."""
    from agent import collab_writes

    assert hasattr(collab_writes, "create_document_via_collab")


async def test_tool_api_direct_create_delegates_to_shared_primitive():
    """_apply_create_document_direct must call create_document_via_collab (no
    inline create_with_unique_path / emit body left in tool_api)."""
    from unittest.mock import AsyncMock, patch

    from routes.tool_api import _common as tool_api_common

    src = inspect.getsource(tool_api_common._apply_create_document_direct)
    assert "create_document_via_collab" in src, (
        "Tool-API direct create must delegate to the shared primitive"
    )
    # The duplicated inline body must be gone from tool_api.
    assert "create_with_unique_path" not in src, (
        "create_with_unique_path body removed from tool_api direct path"
    )

    # Delegation behavior: patching the shared primitive short-circuits the create.
    with patch(
        "agent.collab_writes.create_document_via_collab",
        new=AsyncMock(return_value={"doc_id": "D1", "sort_key": "z"}),
    ) as m, patch("routes.tool_api._common.get_project_access", new=AsyncMock(return_value="full")):
        result = await (tool_api_common._apply_create_document_direct(
            title="T", content="C", parent_id=None,
            project_id="p1", user={"user_id": "u1", "name": "n"},
        ))
        assert result["doc_id"] == "D1"
        assert m.await_count == 1
        _, kwargs = m.call_args
        assert kwargs["title"] == "T" and kwargs["content"] == "C"


def test_create_primitive_emits_document_created():
    """The single source owns the document_created emit (sort_key invariant) so
    both entry points produce identical event shapes."""
    from agent import collab_writes

    src = inspect.getsource(collab_writes.create_document_via_collab)
    assert "create_with_unique_path" in src
    assert '"document_created"' in src or "'document_created'" in src
    assert "sort_key" in src


async def test_a_created_document_with_content_enters_the_search_index(project_with_doc):
    """A document the agent CREATES with a body must be embedded, exactly like one it
    edits. The embedding debounce listens on `content_flushed`; a create that only
    emits `document_created` leaves the body invisible to search until a human happens
    to edit it — which is how 58 of 61 memory facts ended up unsearchable."""

    pid, _idx, uid = project_with_doc
    from agent.collab_writes import create_document_via_collab

    with EmitRecorder.active() as emitted:
        created = await create_document_via_collab(
            title="Indexed on create", content="A body worth finding.",
            parent_id=None, project_id=pid, user={"user_id": uid, "name": "t"},
        )
    flushed = emitted.of("content_flushed")
    assert flushed, "create emitted no content_flushed — the body is never embedded"
    assert flushed[0]["entity_id"] == created["doc_id"]
    assert flushed[0]["project_id"] == pid


async def test_an_empty_created_document_is_not_flushed(project_with_doc):
    """No body, nothing to embed — the flush is for content, not for existence."""

    pid, _idx, uid = project_with_doc
    from agent.collab_writes import create_document_via_collab

    with EmitRecorder.active() as emitted:
        await create_document_via_collab(
            title="Empty on purpose", content="", parent_id=None,
            project_id=pid, user={"user_id": uid, "name": "t"},
        )
    assert not emitted.of("content_flushed")


async def test_create_unwraps_placeholder_bracket_link_destinations():
    """A `(<id>)` link the model copied from a prompt placeholder is stored
    unwrapped — the create primitive is one of the three agent write sites for
    the unwrap."""
    from unittest.mock import AsyncMock, patch

    from agent.collab_writes import create_document_via_collab

    with patch("documents.service.assert_parent_valid", new=AsyncMock()), \
            patch("documents.service.create_with_unique_path", new=AsyncMock()) as create_row, \
            patch("agent.doc_state.finalize_content_mutation",
                  new=AsyncMock()), \
            EmitRecorder.active():
        await create_document_via_collab(
            title="T", content="[text](<abc-def>) and ![a|800x600](<ref:xyz_1>)",
            parent_id=None, project_id="p1", user={"user_id": "u1"},
        )
    payload = create_row.await_args.args[1]
    assert payload["content"] == "[text](abc-def) and ![a|800x600](ref:xyz_1)"
