"""Empty-wipe REST PATCH guards — documents + references.

A REST PATCH whose content is empty/whitespace-only against a NON-empty entity
with NO live collab session is the wire signature of the empty-view checkpoint
bug (plan: .kilo/plans/empty-checkpoint-wipe.md). The backend must reject it
(409 + a telemetry row) instead of wiping; with a live session + client the
existing ignore-the-echo behavior is unchanged.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

FULL = "Full document body that must survive an empty-wipe PATCH."


async def _make_full_doc(client, cookies, pid: str) -> str:
    doc_id = (await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Wipe target"},
        cookies=cookies,
    )).json()["document_id"]
    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"content": FULL}, cookies=cookies,
    )
    assert resp.status_code == 200
    return doc_id


async def _make_full_ref(client, cookies, pid: str, host_id: str) -> str:
    resp = await client.post(
        "/api/references",
        json={
            "project_id": pid, "document_id": host_id, "title": "Ref target",
            "media_type": "markdown", "content": FULL,
        },
        cookies=cookies,
    )
    assert resp.status_code == 200
    return resp.json()["reference_id"]


async def _content(test_db, entity_id: str) -> str:
    rows = await test_db.query(
        "SELECT content FROM type::record('documents', $id)", {"id": entity_id},
    )
    return rows[0]["content"]


async def _wipe_telemetry(test_db, entity_id: str) -> list:
    return await test_db.query(
        "SELECT * FROM telemetry_event WHERE kind = 'empty_wipe_rejected' "
        "AND entity_id = $e",
        {"e": entity_id},
    )


def _live_session() -> SimpleNamespace:
    """A collab session with a connected client — the echo-ignore regime."""
    return SimpleNamespace(content=FULL, clients={"someone"})


# ─── documents ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_empty_with_ydoc_state_rejected(client, admin_user, project_with_doc, test_db):
    """Rejection must not depend on ydoc_state being NONE — a doc with a real
    persisted Y.Doc is the common case for a long-lived document."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id = await _make_full_doc(client, cookies, pid)

    from ydoc_store import set_content
    await set_content(doc_id, FULL, persist=True)
    rows = await test_db.query(
        "SELECT ydoc_state FROM type::record('documents', $id)", {"id": doc_id},
    )
    assert rows[0]["ydoc_state"], "fixture sanity: ydoc_state must be persisted"

    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"content": ""}, cookies=cookies,
    )

    assert resp.status_code == 409
    assert await _content(test_db, doc_id) == FULL
    assert len(await _wipe_telemetry(test_db, doc_id)) == 1


@pytest.mark.asyncio
async def test_patch_empty_live_session_ignored(client, admin_user, project_with_doc, test_db):
    """With a live session + client the REST content is a redundant echo — the
    existing ignore behavior is unchanged and produces NO telemetry row."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id = await _make_full_doc(client, cookies, pid)

    with patch("collab.registry.get_active_session", return_value=_live_session()):
        resp = await client.patch(
            f"/api/documents/{doc_id}", json={"content": ""}, cookies=cookies,
        )

    assert resp.status_code == 200
    assert await _content(test_db, doc_id) == FULL
    assert await _wipe_telemetry(test_db, doc_id) == []


# ─── sibling fields survive the 409 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_ref_patch_empty_with_title_still_applies_title(client, admin_user, project_with_doc, test_db):
    """Symmetric: a reference PATCH's title applies even when its content part
    is refused by the empty-wipe guard."""
    pid, host_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    ref_id = await _make_full_ref(client, cookies, pid, host_id)

    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"content": "", "title": "Renamed Ref"}, cookies=cookies,
    )

    assert resp.status_code == 409
    rows = await test_db.query(
        "SELECT title, content FROM type::record('documents', $id)", {"id": ref_id},
    )
    assert rows[0]["title"] == "Renamed Ref"
    assert rows[0]["content"] == FULL


# ─── references ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ref_patch_empty_no_session_rejected(client, admin_user, project_with_doc, test_db):
    pid, host_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    ref_id = await _make_full_ref(client, cookies, pid, host_id)

    resp = await client.patch(
        f"/api/references/{ref_id}", json={"content": ""}, cookies=cookies,
    )

    assert resp.status_code == 409
    assert "refusing to wipe" in resp.json()["detail"]
    assert await _content(test_db, ref_id) == FULL
    rows = await _wipe_telemetry(test_db, ref_id)
    assert len(rows) == 1
    assert rows[0]["category"] == "documents"


@pytest.mark.asyncio
async def test_ref_patch_whitespace_no_session_rejected(client, admin_user, project_with_doc, test_db):
    pid, host_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    ref_id = await _make_full_ref(client, cookies, pid, host_id)

    resp = await client.patch(
        f"/api/references/{ref_id}", json={"content": " \n "}, cookies=cookies,
    )

    assert resp.status_code == 409
    assert await _content(test_db, ref_id) == FULL
    assert len(await _wipe_telemetry(test_db, ref_id)) == 1


@pytest.mark.asyncio
async def test_ref_patch_empty_live_session_ignored(client, admin_user, project_with_doc, test_db):
    pid, host_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    ref_id = await _make_full_ref(client, cookies, pid, host_id)

    with patch("collab.registry.get_active_session", return_value=_live_session()):
        resp = await client.patch(
            f"/api/references/{ref_id}", json={"content": ""}, cookies=cookies,
        )

    assert resp.status_code == 200
    assert await _content(test_db, ref_id) == FULL
    assert await _wipe_telemetry(test_db, ref_id) == []
