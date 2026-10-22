"""Group audience sharing — live revocation over the collab WS.

Audiences-row
changes and group moves reach OPEN collab sessions at once (a project-wide
`access_changed` emit recomputes every connected client), and per-user emits
carry the RECOMPUTED level (a removed member row may leave an audience
grant). Unit mechanics of the project-wide branch live in
test_collab_access_revoke.py.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
import pytest_asyncio
from collab.registry import _session_key, _sessions
from helpers import join_collab_ws, make_token, project_collab_url
from password import hash_secret

_MOD = "ga-live-mod"
_U1 = "ga-live-u1"
_U2 = "ga-live-u2"
_X = "ga-live-x"
_OWNER = "ga-live-owner"


async def _mk_user(test_db, uid: str, *, role: str = "user", moderator_id: str | None = None) -> str:
    """Seed a user (delete-first) and return a session token."""
    from db import create_record
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    content = {
        "name": uid,
        "email": f"{uid}@galive.com",
        "password_hash": hash_secret("gp"),
        "role": role,
        "user_facts": "",
    }
    if moderator_id:
        content["moderator_id"] = moderator_id
    await create_record("users", uid, content)
    return make_token(uid, uid, role)


@pytest_asyncio.fixture
async def live_cast(test_db):
    """Moderator M; group members U1/U2; group owner O (in M's group); no-group X."""
    tokens = {
        "mod": await _mk_user(test_db, _MOD, role="moderator"),
        "u1": await _mk_user(test_db, _U1, moderator_id=_MOD),
        "u2": await _mk_user(test_db, _U2, moderator_id=_MOD),
        "x": await _mk_user(test_db, _X),
        "owner": await _mk_user(test_db, _OWNER, moderator_id=_MOD),
    }
    yield tokens


async def _mk_audience_project(client, owner_token: str, *, access: str) -> tuple[str, str]:
    """Owner O's project opened to M's group at `access`; returns (pid, doc_id)."""
    pid = (await client.post(
        "/api/projects", json={"name": "GA Live"},
        cookies={"lore_session": owner_token},
    )).json()["project_id"]
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"audiences": [{"subject": f"mod:{_MOD}", "access_level": access}]},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200, resp.text
    doc_id = (await client.post(
        "/api/documents", json={"project_id": pid, "title": "Doc", "content": "hi"},
        cookies={"lore_session": owner_token},
    )).json()["document_id"]
    return pid, doc_id


async def _mk_public_project(client, admin_token: str) -> tuple[str, str]:
    """Admin-owned project with a public readonly row; returns (pid, doc_id)."""
    pid = (await client.post(
        "/api/projects", json={"name": "GA Live Public"},
        cookies={"lore_session": admin_token},
    )).json()["project_id"]
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"audiences": [{"subject": "public", "access_level": "readonly"}]},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    doc_id = (await client.post(
        "/api/documents", json={"project_id": pid, "title": "Pub Doc", "content": "hi"},
        cookies={"lore_session": admin_token},
    )).json()["document_id"]
    return pid, doc_id


async def _mk_member_row(test_db, pid: str, uid: str, level: str) -> None:
    from db import create_record
    await create_record("project_members", str(uuid4()), {
        "project_id": pid, "user_id": uid, "access_level": level,
    })


def _recv_until(ws, msg_type: str, *, limit: int = 20) -> dict:
    """Receive frames until one of `msg_type` (drains presence noise)."""
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"no {msg_type!r} frame within {limit} frames")


async def _settle() -> None:
    """Let fire-and-forget event_bus subscribers finish BEFORE a blocking
    sync WS receive freezes the shared session loop.

    # WHY: TestClient's sync receive_text blocks the session event loop's
    # thread; a project-wide recompute subscriber needs several loop slices
    # (DB round trips) before its frame lands, so blocking too early
    # deadlocks emit→frame until the 90s zombie reaper tears the socket.
    """
    await asyncio.sleep(0.5)


def _client_level(doc_id: str, uid: str) -> str | None:
    session = _sessions.get(_session_key("doc", doc_id))
    if not session:
        return None
    for c in session.clients.values():
        if c.user_id == uid:
            return c.access_level
    return None


def _session_has_client(doc_id: str, uid: str) -> bool:
    session = _sessions.get(_session_key("doc", doc_id))
    return bool(session) and any(c.user_id == uid for c in session.clients.values())


# ─── Audience cleared / lowered: connected group clients ─────────────────────


@pytest.mark.asyncio
async def test_clearing_audience_revokes_group_member_and_keeps_row_holder(
    sync_app, client, test_db, live_cast,
):
    """Clearing the audience: a pure group viewer is revoked; a member-row
    holder is downgraded to their row level, not revoked."""
    pid, doc_id = await _mk_audience_project(client, live_cast["owner"], access="commentator")
    await _mk_member_row(test_db, pid, _U2, "readonly")
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": live_cast["u1"]},
    ) as ws1, sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": live_cast["u2"]},
    ) as ws2:
        join_collab_ws(ws1, doc_id)
        json.loads(ws1.receive_text())  # init
        join_collab_ws(ws2, doc_id)
        json.loads(ws2.receive_text())  # init
        resp = await client.patch(
            f"/api/projects/{pid}", json={"audiences": []},
            cookies={"lore_session": live_cast["owner"]},
        )
        assert resp.status_code == 200, resp.text
        await _settle()
        assert _recv_until(ws1, "access_revoked")
        msg = _recv_until(ws2, "access_changed")
        assert msg["level"] == "readonly"
        # the session's write-gate copy is downgraded with the frame
        assert _client_level(doc_id, _U2) == "readonly"


@pytest.mark.asyncio
async def test_lowering_audience_level_downgrades_connected_member(
    sync_app, client, live_cast,
):
    pid, doc_id = await _mk_audience_project(client, live_cast["owner"], access="commentator")
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": live_cast["u1"]},
    ) as ws:
        join_collab_ws(ws, doc_id)
        json.loads(ws.receive_text())  # init
        resp = await client.patch(
            f"/api/projects/{pid}",
            json={"audiences": [{"subject": f"mod:{_MOD}", "access_level": "readonly"}]},
            cookies={"lore_session": live_cast["owner"]},
        )
        assert resp.status_code == 200, resp.text
        await _settle()
        msg = _recv_until(ws, "access_changed")
        assert msg["level"] == "readonly"
        assert _client_level(doc_id, _U1) == "readonly"


# ─── Group move (the moderator_id admin write) ───────────────────────────────


@pytest.mark.asyncio
async def test_moving_user_out_of_group_revokes_their_client(
    sync_app, client, live_cast, admin_user,
):
    pid, doc_id = await _mk_audience_project(client, live_cast["owner"], access="readonly")
    _, admin_token = admin_user
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": live_cast["u1"]},
    ) as ws:
        join_collab_ws(ws, doc_id)
        json.loads(ws.receive_text())  # init
        resp = await client.patch(
            f"/api/admin/users/{_U1}", json={"moderator_id": None},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
        await _settle()
        assert _recv_until(ws, "access_revoked")


# ─── public row removal (admin write): the pre-existing gap closes ────────────


@pytest.mark.asyncio
async def test_removing_public_row_revokes_non_member_viewer(
    sync_app, client, live_cast, admin_user,
):
    _, admin_token = admin_user
    pid, doc_id = await _mk_public_project(client, admin_token)
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": live_cast["x"]},
    ) as ws:
        join_collab_ws(ws, doc_id)
        json.loads(ws.receive_text())  # init
        assert _client_level(doc_id, _X) == "readonly"
        resp = await client.patch(
            f"/api/projects/{pid}", json={"audiences": []},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
        await _settle()
        assert _recv_until(ws, "access_revoked")


# ─── Scope: a project-wide emit for X leaves Y untouched ─────────────────────


@pytest.mark.asyncio
async def test_project_wide_emit_leaves_other_project_session_untouched(
    sync_app, client, live_cast, admin_user,
):
    _, admin_token = admin_user
    pid1, doc1 = await _mk_audience_project(client, live_cast["owner"], access="readonly")
    pid2, doc2 = await _mk_public_project(client, admin_token)
    with sync_app.websocket_connect(
        project_collab_url(pid1), cookies={"lore_session": live_cast["u1"]},
    ) as ws1, sync_app.websocket_connect(
        project_collab_url(pid2), cookies={"lore_session": live_cast["u1"]},
    ) as ws2:
        join_collab_ws(ws1, doc1)
        json.loads(ws1.receive_text())  # init
        join_collab_ws(ws2, doc2)
        json.loads(ws2.receive_text())  # init
        resp = await client.patch(
            f"/api/projects/{pid1}", json={"audiences": []},
            cookies={"lore_session": live_cast["owner"]},
        )
        assert resp.status_code == 200, resp.text
        await _settle()
        assert _recv_until(ws1, "access_revoked")
        # past the revocation grace period: the P1 client is gone, the P2
        # client of the SAME user is still connected at its level
        await asyncio.sleep(1.6)
        assert not _session_has_client(doc1, _U1)
        assert _session_has_client(doc2, _U1)
        assert _client_level(doc2, _U1) == "readonly"


# ─── Per-user emits carry the RECOMPUTED level ───────────────────────────────


@pytest.mark.asyncio
async def test_member_removal_emit_keeps_audience_level(
    sync_app, client, test_db, live_cast,
):
    """Removing a member row while a higher/lower audience grant remains
    downgrades to the recomputed level — a guessed None would evict a user
    who still holds access."""
    pid, doc_id = await _mk_audience_project(client, live_cast["owner"], access="commentator")
    await _mk_member_row(test_db, pid, _U2, "full")
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": live_cast["u2"]},
    ) as ws:
        join_collab_ws(ws, doc_id)
        json.loads(ws.receive_text())  # init
        assert _client_level(doc_id, _U2) == "full"
        resp = await client.delete(
            f"/api/projects/{pid}/members/{_U2}",
            cookies={"lore_session": live_cast["owner"]},
        )
        assert resp.status_code == 200, resp.text
        await _settle()
        msg = _recv_until(ws, "access_changed")
        assert msg["level"] == "commentator"
        assert _client_level(doc_id, _U2) == "commentator"
