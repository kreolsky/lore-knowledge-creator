"""The per-chat `unindexed` opt-out flag and the conversation projection it rides.

`unindexed` keeps a chat out of the chat-recall search index. It is set at
create, toggled both ways by PATCH across the WHOLE conversation (every
branch and every compaction continuation), refused on a note session, and
inherited by every row minted from an unindexed one.
"""
import re
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


# ─── create + serialize ──────────────────────────────────────────────────────


async def test_unindexed_set_on_create_and_serialized(
    client, admin_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    out = await _mk_chat(client, token, pid, doc_id, unindexed=True)
    assert out["unindexed"] is True
    assert (await _row(test_db, out["session_id"]))["unindexed"] is True


async def test_unindexed_defaults_false(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    out = await _mk_chat(client, token, pid, doc_id)
    assert out["unindexed"] is False


async def test_unindexed_refused_on_note_create(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True,
              "unindexed": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422, resp.text


def test_serializer_reads_missing_flag_as_false():
    from chat_sessions.serialize import serialize_session

    assert serialize_session({"id": "chat_sessions:s1"})["unindexed"] is False


# ─── PATCH: the whole conversation, both ways ────────────────────────────────


async def test_patch_on_branch_flips_every_row_of_the_conversation(
    client, admin_user, project_with_doc, test_db,
):
    """A toggle on one branch must not leave its siblings or its compaction
    continuation searchable — and an unrelated chat is untouched."""
    pid, doc_id, uid = project_with_doc
    _, token = admin_user
    root = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    other = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    base = {"project_id": pid, "user_id": uid}
    await _mk_row(test_db, "br", {**base, "thread_id": root})
    await _mk_row(test_db, "cont", {**base, "compacted_from": "br"})

    resp = await _patch(client, token, "br", {"unindexed": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["unindexed"] is True
    for sid in (root, "br", "cont"):
        assert (await _row(test_db, sid))["unindexed"] is True, sid
    assert (await _row(test_db, other))["unindexed"] is False

    resp = await _patch(client, token, "cont", {"unindexed": False})
    assert resp.status_code == 200, resp.text
    for sid in (root, "br", "cont"):
        assert (await _row(test_db, sid))["unindexed"] is False, sid


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


# ─── inheritance at mint ─────────────────────────────────────────────────────


async def test_branch_inherits_unindexed(client, admin_user, project_with_doc, test_db):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id, unindexed=True))["session_id"]
    await test_db.query(
        "CREATE type::record('messages', 'u1') CONTENT $f",
        {"f": {"chat_id": sid, "role": "user", "content": "hi"}},
    )
    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "u1"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["unindexed"] is True
    assert (await _row(test_db, resp.json()["session_id"]))["unindexed"] is True


_MINT = re.compile(
    r"create_record\(\s*[\"']chat_sessions[\"']"
    r"|\b(?:CREATE|INSERT(?:\s+INTO)?|UPSERT)\b[^\n]{0,60}chat_sessions",
)
# Every site that mints a chat_sessions row, classified. `inherits` copies
# `unindexed` from its source row; `root-default` takes it from the request;
# `note` mints a note session, which `is_note` already keeps out of the index.
_CLASSIFIED_MINT_SITES = {
    "chat_sessions/create.py": "root-default",
    "routes/chat/branches.py": "inherits",
    "driver/compaction.py": "inherits",
    "notes_service.py": "note",
    "migrations/migrate_chat_branches_to_sessions.py": "inherits",
}


def test_every_chat_session_minting_site_is_classified():
    """A fifth minting path that does not carry `unindexed` would silently make
    an anonymous chat searchable — every site must be classified here."""
    found = {
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if _MINT.search(path.read_text(encoding="utf-8"))
    }
    assert found == set(_CLASSIFIED_MINT_SITES), found


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


async def test_conversation_of_a_lone_chat_is_itself(test_db, chain):
    from chat_sessions.conversation import conversation_session_ids

    session = await _row(test_db, "lone")
    assert await conversation_session_ids(test_db, "lone", session) == {"lone"}


def test_migration_carved_branch_inherits_unindexed():
    from migrations.migrate_chat_branches_to_sessions import _branch_data

    chat = {"project_id": "p1", "user_id": "u1", "unindexed": True}
    assert _branch_data(chat, "s1", [], "n1", set())["unindexed"] is True
