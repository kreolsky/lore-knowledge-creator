"""Plan retire-the-stored-turn-timeline steps 2–3 — the list projection ships
no stored timeline, and after step 3 the rows cannot carry one either.

Step 2 OMITs the three columns (`segments`, `agent_steps`, `reasoning`) from
the list projection. Step 3 drops the columns outright (the runner migration
messages_timeline_columns_drop), so seeding them in a test is no longer
possible — the schema refuses the write. What remains to guard here: `halt`
(the one timeline fact the driver's log cannot produce) still rides the wire,
and the retired keys do not. The timeline itself rides the wire as `frames`,
replayed by the client's ONE reducer.

Non-vacuity is bound per test: the halt row is read back from the DB FIRST, so
the assertion below cannot pass because the seed silently failed.
"""
import pytest


@pytest.fixture(autouse=True)
def _stub_agent_timeline(monkeypatch):
    """Serve the message list without an agent driver behind it.

    GET /messages reads the turn timeline from the driver and, finding no line
    configured, answers 502 rather than an empty thread (no silent
    degradation). Halt is exactly the fact that must ride WITHOUT the driver,
    so the stub is the honest setup — same idiom as test_chat_reasoning's
    autouse fixture (and the run-#1225 lesson it records: without it the test
    passes only where a driver line happens to be configured, i.e. green on a
    dev host, red on CI gw2 — replayed as run #1238).
    """
    import driver.timeline

    async def no_turns(session_id):
        # The ReplayedSession mapping — a truthy LIST here would crash
        # _fetch_session_turns (**(replay or {}) needs a mapping).
        return {"turns": [], "tail_seq": None}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", no_turns)


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _create_message(client, token, sid, content):
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _halt_row_from_db(test_db, mid: str) -> dict:
    rows = await test_db.query(
        "SELECT halt FROM type::record('messages', $id)",
        {"id": mid},
    )
    assert rows, f"seed row {mid} vanished"
    return rows[0]


async def test_list_still_carries_the_halt_card(
    client, admin_user, project_with_doc, test_db,
):
    """`halt` is the one timeline fact the driver's log cannot produce — the
    same projection that drops the three columns must keep shipping it."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)
    msg = await _create_message(client, token, sid, "as far as it got")
    await test_db.query(
        "UPDATE type::record('messages', $id) SET "
        "role = 'assistant', halt = { reason: 'disconnected', steps: 2 }",
        {"id": msg["message_id"]},
    )

    row = await _halt_row_from_db(test_db, msg["message_id"])
    assert (row.get("halt") or {}).get("reason") == "disconnected", (
        "seed failed — the assertion below would pass vacuously"
    )

    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    by_id = {m["message_id"]: m for m in resp.json()}
    shipped = by_id[msg["message_id"]]

    assert shipped["halt"]["reason"] == "disconnected"
    assert shipped["halt"]["steps"] == 2
    assert "segments" not in shipped
    assert "agent_steps" not in shipped
    assert "reasoning" not in shipped
