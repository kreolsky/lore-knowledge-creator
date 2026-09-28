"""Documents command-layer structure tests.

Pins the split of routes/documents.py into thin HTTP handlers + the
backend/documents/ command layer: each handler enforces access then
delegates, and the cross-route batch-delete (routes/references.py) reaches
the COMMAND module, not a route module. Behavior contracts (status codes,
events, payload shapes) are pinned by the HTTP-surface suite
(test_documents*.py, test_documents_batch.py, test_batch_delete_event.py);
these tests pin the STRUCTURE so a regression to fat handlers is visible.
"""

from unittest.mock import AsyncMock, patch

from agent_config import PROTECTED_SYSTEM_ROLES
from documents.delete import _is_protected_skeleton
from documents.update import record_doc_open
from routes.documents import (
    create_document_endpoint,
    delete_document,
    delete_documents_batch,
    get_documents_batch,
    patch_document,
    reorder_document,
)

from models import (
    CreateDocument,
    DeleteDocumentsRequest,
    DeleteReferencesRequest,
    DocumentBatchRequest,
    PatchDocument,
    ReorderDocument,
)

# ─── handlers delegate to the command layer ──────────────────────────────────


async def test_patch_document_delegates_to_command():
    body = PatchDocument(title="t")
    with patch("routes.documents.require_document_full", new_callable=AsyncMock), \
         patch("routes.documents.update_document_command",
               new_callable=AsyncMock, return_value={"document_id": "d1"}) as cmd:
        result = await patch_document("d1", body, user={"user_id": "u1"})
    cmd.assert_awaited_once_with("d1", body)
    assert result == {"document_id": "d1"}


async def test_create_document_delegates_to_command():
    body = CreateDocument(project_id="p1", title="T")
    user = {"user_id": "u1", "name": "N"}
    with patch("routes.documents.require_project_full", new_callable=AsyncMock), \
         patch("routes.documents.create_document_command",
               new_callable=AsyncMock, return_value={"document_id": "d1"}) as cmd:
        result = await create_document_endpoint(body, user=user)
    cmd.assert_awaited_once_with(body, user_id="u1", user_name="N")
    assert result == {"document_id": "d1"}


async def test_batch_fetch_delegates_to_command():
    body = DocumentBatchRequest(ids=["d1", "d2"])
    user = {"user_id": "u1"}
    with patch("routes.documents.fetch_content_batch",
               new_callable=AsyncMock, return_value={"items": []}) as cmd:
        result = await get_documents_batch(body, user=user)
    cmd.assert_awaited_once_with(["d1", "d2"], user)
    assert result == {"items": []}


async def test_reorder_delegates_to_command():
    body = ReorderDocument(after_id="d2")
    with patch("routes.documents.require_document_full", new_callable=AsyncMock), \
         patch("routes.documents.reorder_document_command",
               new_callable=AsyncMock, return_value={"document_id": "d1"}) as cmd:
        result = await reorder_document("d1", body, user={"user_id": "u1"})
    cmd.assert_awaited_once_with("d1", "d2")
    assert result == {"document_id": "d1"}


async def test_delete_delegates_to_command_subtree_mode():
    """Default mode gates require_project_full on the target's project and
    forwards delete_children=True."""
    with patch("routes.documents.get_doc_project_id",
               new_callable=AsyncMock, return_value="p1") as gp, \
         patch("routes.documents.require_project_full", new_callable=AsyncMock) as gate, \
         patch("routes.documents.delete_document_command",
               new_callable=AsyncMock, return_value={"success": True}) as cmd:
        result = await delete_document("d1", user={"user_id": "u1"})
    cmd.assert_awaited_once_with("d1", delete_children=True)
    gp.assert_awaited_once_with("d1")
    gate.assert_awaited_once_with("p1", {"user_id": "u1"})
    assert result == {"success": True}


async def test_delete_delegates_to_command_lift_mode():
    """delete_children=false keeps the require_document_full gate on the target."""
    with patch("routes.documents.require_document_full", new_callable=AsyncMock) as gate, \
         patch("routes.documents.delete_document_command",
               new_callable=AsyncMock, return_value={"success": True}) as cmd:
        result = await delete_document("d1", delete_children=False, user={"user_id": "u1"})
    cmd.assert_awaited_once_with("d1", delete_children=False)
    gate.assert_awaited_once_with("d1", {"user_id": "u1"})
    assert result == {"success": True}


async def test_batch_delete_delegates_to_command():
    body = DeleteDocumentsRequest(document_ids=["d1"])
    user = {"user_id": "u1"}
    with patch("routes.documents.delete_documents_batch_command",
               new_callable=AsyncMock, return_value={"deleted": 1, "skipped": 0}) as cmd:
        result = await delete_documents_batch(body, user=user)
    cmd.assert_awaited_once_with(["d1"], user, delete_children=True)
    assert result == {"deleted": 1, "skipped": 0}


async def test_references_batch_delete_reaches_the_service_not_a_route():
    """The cross-route reach-through (`from routes.documents import …`) is gone:
    references batch-delete delegates to documents.delete with ids + user verbatim."""
    from routes.references import delete_references_batch

    body = DeleteReferencesRequest(reference_ids=["r1", "r2"])
    user = {"user_id": "u1"}
    with patch("documents.delete.delete_documents_batch_command",
               new_callable=AsyncMock, return_value={"deleted": 2, "skipped": 0}) as cmd:
        result = await delete_references_batch(body, user)
    cmd.assert_awaited_once_with(["r1", "r2"], user)
    assert result == {"deleted": 2, "skipped": 0}


# ─── record_doc_open: owner / member / non-member pointer branches ───────────


class TestRecordDocOpen:
    async def test_owner_pointer_lives_on_the_project_row(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value=[])
        with patch("documents.update.fetch_one", new_callable=AsyncMock,
                   return_value={"owner_id": "u1"}) as fo:
            await record_doc_open(db, {"user_id": "u1"}, "p1", "d1")
        fo.assert_awaited_once_with("projects", "p1")
        stmt = db.query.call_args.args[0]
        assert "UPDATE projects SET last_accessed_doc_id" in stmt
        assert db.query.await_count == 1  # no membership write, no prefs write

    async def test_member_pointer_lives_on_the_membership_row(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value=[{"id": "m1"}])
        with patch("documents.update.fetch_one", new_callable=AsyncMock,
                   return_value={"owner_id": "someone_else"}):
            await record_doc_open(db, {"user_id": "u1"}, "p1", "d1")
        stmt = db.query.call_args.args[0]
        assert "UPDATE project_members SET last_accessed_doc_id" in stmt
        assert db.query.await_count == 1  # membership row existed: no prefs fallback

    async def test_nonmember_viewer_falls_back_to_user_preferences(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value=[])
        with patch("documents.update.fetch_one", new_callable=AsyncMock,
                   return_value={"owner_id": "someone_else"}), \
             patch("documents.update.set_prefs_last_doc",
                   new_callable=AsyncMock) as prefs:
            await record_doc_open(db, {"user_id": "u1"}, "p1", "d1")
        prefs.assert_awaited_once_with(db, "u1", "p1", "d1")


# ─── protected-skeleton guard ─────────────────────────────────────────────────


def test_protected_skeleton_true_for_a_protected_system_role():
    role = sorted(PROTECTED_SYSTEM_ROLES)[0]
    assert _is_protected_skeleton({"is_system": True, "system_role": role}) is True


def test_protected_skeleton_false_for_plain_or_nonprotected_docs():
    assert _is_protected_skeleton(None) is False
    assert _is_protected_skeleton({"is_system": False, "system_role": "root"}) is False
    outside = "definitely-not-a-role"
    assert outside not in PROTECTED_SYSTEM_ROLES
    assert _is_protected_skeleton({"is_system": True, "system_role": outside}) is False
