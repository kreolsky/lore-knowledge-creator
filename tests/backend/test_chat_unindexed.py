"""The per-chat `unindexed` opt-out flag and the conversation projection it rides.

`unindexed` keeps a chat out of the chat-recall search index. It is set at
create, toggled both ways by PATCH across the WHOLE conversation (every
branch and every compaction continuation), refused on a note session, and
inherited by every row minted from an unindexed one.
"""
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "backend"
if not BACKEND.is_dir():  # inside the backend container: /tests + /app
    BACKEND = Path("/app")


async def _mk_chat(client, token, pid, doc_id, **extra):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test", **extra},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _patch(client, token, sid, body):
    return await client.patch(
        f"/api/chat/sessions/{sid}", json=body, cookies={"lore_session": token},
    )


async def _row(test_db, sid):
    rows = await test_db.query(
        "SELECT * FROM type::record('chat_sessions', $id)", {"id": sid})
    return rows[0]


async def _mk_row(test_db, sid, fields):
    await test_db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": sid, "f": {"title": "", "model": "m", **fields}},
    )


# ─── PATCH: the whole conversation, both ways ────────────────────────────────


async def test_patch_unindexed_on_note_is_400(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    note = (await _mk_chat(client, token, pid, doc_id, is_note=True))["session_id"]
    resp = await _patch(client, token, note, {"unindexed": True})
    assert resp.status_code == 400, resp.text


async def test_patch_unindexed_never_touches_another_users_rows(
    client, admin_user, project_with_doc, test_db,
):
    """A forged thread_id pointing at the caller's root must not let the
    toggle write a row the caller does not own."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    root = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_row(test_db, "foreign", {"project_id": pid, "user_id": "someone-else",
                                       "thread_id": root})
    resp = await _patch(client, token, root, {"unindexed": True})
    assert resp.status_code == 200, resp.text
    assert (await _row(test_db, "foreign"))["unindexed"] is False


# ─── conversation_session_ids ────────────────────────────────────────────────


@pytest.fixture
async def chain(test_db):
    """root (pre-migration: thread_id NONE) ─ branch b1
       b1 ─compaction→ c1 ─compaction→ c2   (continuation of a branch, twice)
       c1 ─ branch cb   (branch of a continuation)
       plus an unrelated chat and another user's row on the same thread."""
    base = {"project_id": "p1", "user_id": "u1"}
    rows = {
        "root": {**base},
        "b1": {**base, "thread_id": "root"},
        "c1": {**base, "compacted_from": "b1"},
        "c2": {**base, "compacted_from": "c1"},
        "cb": {**base, "thread_id": "c1"},
        "lone": {**base, "thread_id": "lone"},
        "alien": {"project_id": "p1", "user_id": "u2", "thread_id": "root"},
    }
    for sid, fields in rows.items():
        await _mk_row(test_db, sid, fields)
    yield {"root", "b1", "c1", "c2", "cb"}
    # test_db is shared across tests; only the `client` fixture wipes tables.
    await test_db.query("DELETE chat_sessions")


@pytest.mark.parametrize("start", ["root", "b1", "c1", "c2", "cb"])
async def test_conversation_closure_from_any_member(test_db, chain, start):
    from chat_sessions.conversation import conversation_session_ids

    session = await _row(test_db, start)
    assert await conversation_session_ids(test_db, start, session) == chain
