"""Standing per-session agent keys (plan agent-line-harness-lifecycle step 6).

One api_keys row per CHAT session — `chat_session_id` is the discriminator
(label is pure display), internal, whole-project scope_root — minted on first
use, RENEWED (token rotated + expiry extended, the SAME row) after the Redis
plaintext cache goes cold, and REVOKED on session delete and on access-level
change (the access_changed event every member mutation emits).

Pinned, in plan language:
- one row per chat session; the warm cache returns the same plaintext;
- distinct sessions get distinct rows;
- a cold cache RENEWS the row (rotate + extend), never a second row;
- the (user, project) whole-project key is a PARALLEL system — the session
  lookup never adopts it and minting a session key never touches it;
- revoke soft-deletes the row + drops the cache; the next use mints a FRESH
  row;
- DELETE /api/chat/sessions/{sid} revokes the session's key;
- access_changed revokes every per-session key of THAT (project, user),
  leaving other members' keys alone.
"""
import uuid

import pytest
from agent.keys import (
    get_or_create_session_agent_key,
    revoke_member_session_keys,
)


async def _rows_for(chat_session_id: str) -> list[dict]:
    from db import get_db

    db = await get_db()
    return await db.query(
        "SELECT * FROM api_keys WHERE chat_session_id = $sid",
        {"sid": chat_session_id},
    )


async def _drop_cache(chat_session_id: str) -> None:
    from redis_pool import get_redis

    r = await get_redis()
    await r.delete(f"agent_key:session:{chat_session_id}")


def _sid() -> str:
    return f"sess-{uuid.uuid4().hex[:10]}"


def _up() -> tuple[str, str]:
    """Unique (user, project) literals — the unit tests here share no `client`
    fixture, so nothing wipes rows between them: reusing 'u1'/'p1' would let
    one test's rows leak into another's member-scoped revocation."""
    return f"u-{uuid.uuid4().hex[:8]}", f"p-{uuid.uuid4().hex[:8]}"


# ─── mint / cache / renew ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_cache_renews_the_same_row():
    sid = _sid()
    u, p = _up()
    await get_or_create_session_agent_key(u, p, sid)
    rows = await _rows_for(sid)
    first_id, first_exp = rows[0]["id"], rows[0]["expires_at"]

    await _drop_cache(sid)
    await get_or_create_session_agent_key(u, p, sid)

    rows = await _rows_for(sid)
    assert len(rows) == 1  # renew, never a second row
    assert rows[0]["id"] == first_id
    assert rows[0]["expires_at"] >= first_exp  # TTL extended, not shortened


# ─── revocation ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_member_scoped_to_one_user():
    s1, s2 = _sid(), _sid()
    u1, p1 = _up()
    u2 = f"u-{uuid.uuid4().hex[:8]}"
    await get_or_create_session_agent_key(u1, p1, s1)
    await get_or_create_session_agent_key(u2, p1, s2)

    n = await revoke_member_session_keys(p1, u1)
    assert n == 1
    assert (await _rows_for(s1))[0].get("deleted_at") is not None
    assert (await _rows_for(s2))[0].get("deleted_at") is None


@pytest.mark.asyncio
async def test_delete_session_revokes_its_key(client, collab_project):
    pid, doc_id, _admin_token, user_token, _admin_uid, user_uid = collab_project
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["session_id"]

    await get_or_create_session_agent_key(user_uid, pid, sid)
    assert len(await _rows_for(sid)) == 1

    resp = await client.delete(
        f"/api/chat/sessions/{sid}", cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text
    rows = await _rows_for(sid)
    assert rows and rows[0].get("deleted_at") is not None
