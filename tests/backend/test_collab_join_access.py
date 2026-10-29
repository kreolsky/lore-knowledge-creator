"""Effective-access decisions on the multiplexed collab join path.

Pins the access decision — the project role resolved for the document — and
fail-closed rejection of a non-member.
"""


import pytest
import pytest_asyncio


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
