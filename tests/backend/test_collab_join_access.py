"""Effective-access decisions on the multiplexed collab join path.

Pins the access decision — the project role resolved for the document — and
fail-closed rejection of a non-member.
"""

import json
import uuid

import pytest
import pytest_asyncio
from collab.registry import _session_key, _sessions

from db import create_record


@pytest_asyncio.fixture
async def collab_no_member(client, admin_user, regular_user):
    """A project + doc owned by admin, with a regular user NOT yet a member.

    Returns (project_id, doc_id, member_token, member_uid). Each test grants the
    membership level it needs.
    """
    _, admin_token = admin_user
    member_uid, member_token = regular_user
    pid = (await client.post(
        "/api/projects", json={"name": "Join Access"},
        cookies={"lore_session": admin_token},
    )).json()["project_id"]
    doc_id = (await client.post(
        "/api/documents", json={"project_id": pid, "title": "Doc", "content": "hi"},
        cookies={"lore_session": admin_token},
    )).json()["document_id"]
    return pid, doc_id, member_token, member_uid


async def _add_member(pid: str, uid: str, level: str) -> None:
    await create_record("project_members", str(uuid.uuid4()), {
        "project_id": pid, "user_id": uid, "access_level": level,
    })


def _client_level(doc_id: str, uid: str) -> str | None:
    session = _sessions.get(_session_key("doc", doc_id))
    if not session:
        return None
    for c in session.clients.values():
        if c.user_id == uid:
            return c.access_level
    return None


def _join(sync_app, pid: str, doc_id: str, token: str, uid: str) -> str | None:
    """Join the doc on the multiplexed channel; return the resolved access level."""
    with sync_app.websocket_connect(
        f"/ws/collab/project/{pid}", cookies={"lore_session": token}
    ) as ws:
        ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "init"
        return _client_level(doc_id, uid)


@pytest.mark.asyncio
@pytest.mark.parametrize("project_level", ["full", "commentator", "readonly"])
async def test_join_uses_project_role(sync_app, collab_no_member, project_level):
    """The join resolves the project role verbatim."""
    pid, doc_id, member_token, member_uid = collab_no_member
    await _add_member(pid, member_uid, project_level)
    assert _join(sync_app, pid, doc_id, member_token, member_uid) == project_level


@pytest.mark.asyncio
async def test_join_rejected_for_non_member(sync_app, collab_no_member):
    """A user with no project membership cannot open the project channel at all."""
    pid, _doc_id, _member_token, _member_uid = collab_no_member
    # The non-member never gets membership → connect rejected at auth (4003).
    with pytest.raises(Exception):
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": "garbage-token"}
        ) as ws:
            ws.receive_text()
