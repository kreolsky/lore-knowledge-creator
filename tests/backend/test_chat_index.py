"""chat-recall index — the (user, project) reconcile of chat_chunks against live messages.

The index is a disposable cache: the pair task embeds what is pending under
the current model, purges what is no longer indexable, reuses a stored vector
for an equal (content_hash, model), holds a per-run chunk budget, and while
the admin disabled the `chat-recall` skill (or embeddings are unconfigured)
embeds nothing but still purges what is no longer indexable. Every writer only
posts the trigger with the stable job_id.
"""

import pytest
import pytest_asyncio
from helpers import fake_embedding_provider

import jobs.pool
import jobs.tasks.chat_index as chat_index
from jobs.tasks.chat_index import chat_index_sweep_task, index_chat_messages

UID, PID = "idx-user", "idx-project"


@pytest.fixture
def embed(monkeypatch):
    """The embedding provider at its boundary: one 8-dim vector per text, calls recorded."""
    return fake_embedding_provider(monkeypatch, lambda _: [0.5] * 8)


@pytest.fixture
def posted(monkeypatch):
    """Every enqueue, recorded instead of reaching Redis."""
    calls: list[tuple] = []

    async def _enqueue(name, *args, job_id=None, **_):
        calls.append((name, args, job_id))

    monkeypatch.setattr(jobs.pool, "enqueue", _enqueue)
    return calls


@pytest_asyncio.fixture(autouse=True)
async def _clean(test_db):
    yield
    await test_db.query(
        "DELETE chat_chunks; DELETE messages; DELETE chat_sessions; DELETE instance_skills")


async def _session(db, sid, *, uid=UID, pid=PID, **fields):
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": sid, "f": {"user_id": uid, "project_id": pid, "title": sid,
                          "model": "m", **fields}},
    )


async def _msg(db, mid, chat_id, content, *, role="user", created_at=None, **fields):
    await db.query(
        "CREATE type::record('messages', $id) CONTENT $f",
        {"id": mid, "f": {"chat_id": chat_id, "role": role, "content": content, **fields}},
    )
    if created_at:
        await db.query("UPDATE type::record('messages', $id) SET created_at = <datetime>$t",
                       {"id": mid, "t": created_at})


async def _chunks(db, **where):
    cond = " AND ".join(f"{k} = ${k}" for k in where) or "true"
    return await db.query(
        f"SELECT * FROM chat_chunks WHERE {cond} ORDER BY message_id, ord", where) or []


async def _run(uid=UID, pid=PID):
    await index_chat_messages({}, uid, pid)


# ─── reconcile ───────────────────────────────────────────────────────────────


async def test_run_budget_is_held_and_the_rest_waits(test_db, embed):
    budget = chat_index.CHAT_INDEX_RUN_BUDGET
    await _session(test_db, "s1")
    # budget + 1 one-chunk messages, the oldest one first in time.
    await test_db.query(
        "FOR $i IN 0..$n { CREATE messages CONTENT { chat_id: 's1', role: 'user', "
        "content: string::concat('line number ', <string>$i), "
        "created_at: time::now() + <duration>string::concat(<string>$i, 's') } }",
        {"n": budget + 1},
    )
    oldest = (await test_db.query(
        "SELECT meta::id(id) AS id FROM messages ORDER BY created_at LIMIT 1"))[0]["id"]
    await _run()
    first = {r["message_id"] for r in await _chunks(test_db)}
    assert len(first) == budget
    assert oldest not in first  # newest first; the leftover waits
    await _run()
    assert len({r["message_id"] for r in await _chunks(test_db)}) == budget + 1


async def _disable_skill(db):
    await db.query(
        "CREATE instance_skills CONTENT {name: 'chat-recall', enabled: false, updated_by: 'admin'}")


async def test_disabled_skill_embeds_nothing_and_posts_nothing(test_db, embed, posted):
    await _disable_skill(test_db)
    await _session(test_db, "s1")
    await _msg(test_db, "m1", "s1", "pending while disabled")
    await _run()
    await chat_index_sweep_task({})
    assert await _chunks(test_db) == []
    assert embed.await_count == 0
    assert posted == []


@pytest.mark.parametrize("off", ["skill-disabled", "embeddings-unconfigured"])
async def test_deleted_chat_is_purged_while_indexing_is_off(test_db, embed, off):
    # What the user deleted or marked unindexed leaves the store even while the
    # index cannot embed; a live message's chunks stay for re-enable.
    await _session(test_db, "s1")
    await _session(test_db, "s2")
    await _msg(test_db, "m1", "s1", "kept")
    await _msg(test_db, "m2", "s2", "deleted later")
    await _run()
    embedded_while_on = embed.await_count
    if off == "skill-disabled":
        await _disable_skill(test_db)
    else:
        # The embed fixture's config probe, now answering "unset".
        chat_index.embeddings._ensure_config.side_effect = (
            chat_index.embeddings.EmbeddingConfigError("no url"))
    await test_db.query("UPDATE type::record('chat_sessions', 's2') SET deleted_at = time::now()")
    await _run()
    assert {r["message_id"] for r in await _chunks(test_db)} == {"m1"}
    await _msg(test_db, "m3", "s1", "said while off")
    await test_db.query("UPDATE type::record('chat_sessions', 's1') SET unindexed = true")
    await chat_index_sweep_task({})
    assert await _chunks(test_db) == []
    assert embed.await_count == embedded_while_on  # nothing embedded while off


# ─── the sweep ───────────────────────────────────────────────────────────────


async def test_sweep_posts_only_pairs_with_work(test_db, embed, posted):
    await _session(test_db, "done")
    await _msg(test_db, "d1", "done", "already indexed")
    await _run()
    await _session(test_db, "lagging", uid="lagger")
    await _msg(test_db, "l1", "lagging", "never indexed")
    await chat_index_sweep_task({})
    assert posted == [("index_chat_messages", ("lagger", PID), f"chat_index:lagger:{PID}")]


# ─── the writer triggers ─────────────────────────────────────────────────────


async def _mk_chat(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _index_posts(posted, uid, pid):
    return [p for p in posted
            if p == ("index_chat_messages", (uid, pid), f"chat_index:{uid}:{pid}")]


async def test_message_patch_posts_the_reconcile(
    client, admin_user, project_with_doc, test_db, posted,
):
    pid, doc_id, uid = project_with_doc
    _, token = admin_user
    sid = await _mk_chat(client, token, pid, doc_id)
    await _msg(test_db, "u1", sid, "before")
    resp = await client.patch("/api/chat/messages/u1", json={"content": "after"},
                              cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    assert _index_posts(posted, uid, pid)
