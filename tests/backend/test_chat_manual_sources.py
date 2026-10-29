"""Tests for manual context sources — verifies that manually-added
documents/references appear on the assistant row's persisted sources with
retrieved=False, are deduplicated against retrieval hits, and that
system_prompt_id is excluded.

The turn is driver-owned: every completion is driven through the standing
channel (the harness_env recipe — fake connector/replay + post_followup at
the module's own seams, the claim and the relay arms persisting to the REAL
test DB). The sources are asserted where they live: the assistant row (the
claim's preamble frame carries the same list on the project WS).
"""
from uuid import uuid4

import pytest
from test_driver_channel import _env, _turn_end
from test_harness_turn import _claim_of, _list_messages, _row_stamped, _until_async


@pytest.fixture(autouse=True)
def _stub_agent_timeline(monkeypatch):
    """Serve the message list without an agent driver behind it.

    GET /messages reads the turn timeline from the driver and, finding no line
    configured, answers 502 rather than an empty thread (routes/chat/messages.py:372
    — no silent degradation). These tests assert over PERSISTED rows, so they stub
    the read the way test_chat_timeline_attach.py does. Why autouse: without it the
    tests pass only where a driver line happens to be configured, which is how they
    went green on a dev host and red on CI worker gw2 in run #1225.
    """
    import driver.timeline

    async def no_turns(session_id):
        # The ReplayedSession mapping — a truthy LIST here would crash
        # _fetch_session_turns (**(replay or {}) needs a mapping).
        return {"turns": [], "tail_seq": None}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", no_turns)


async def _drive_turn(client, token, sid, env, body, *, turn_end_seq=1):
    """Drive one driver-owned turn through the REAL relay arms (the harness
    recipe): POST the completion (JSON accept, the send is queued), push the
    driver's frames through the fake channel socket — the claim mints the
    pair and opens the turn, turn/end closes it — and settle on the row's
    end stamp, written by the close's finalize persist. Manual context
    sources ride the claim onto the assistant row; the tests assert the row.

    `turn_end_seq` must EXCEED every seq the session's channel already
    delivered — the channel's seq-anchored dedup silently drops a replayed
    seq, and the turn would never close."""
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={"user_msg_id": str(uuid4()), **body},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True
    payload = env.followups.payloads[-1]
    sock = env.connector.sockets[0]
    for frame in (_claim_of(payload), _turn_end(turn_end_seq, "completed")):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _row_stamped(payload["assistant_msg_id"], turn_end_seq))


async def _assistant_sources(client, token, sid) -> list[dict]:
    """The LAST assistant row's persisted sources panel."""
    msgs = await _list_messages(client, token, sid)
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    return assistant.get("sources") or []


async def _create_ref(client, token, pid, parent_id, *, content="Ref body", title="Ref title"):
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid,
            "parent_id": parent_id,
            "title": title,
            "media_type": "markdown",
            "is_reference": True,
            "content": content,
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def _create_agent_session(client, token, pid, *, target_doc_id, document_id):
    """Open an agent-mode session pinned to target_doc_id."""
    resp = await client.post(
        "/api/chat/sessions",
        json={
            "project_id": pid,
            "document_id": document_id,
            "mode": "agent",
            "target_doc_id": target_doc_id,
            "model": "test",
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["session_id"]


# ─── Agent-mode target source is opt-in ─────────────────────────────────────
#
# INVARIANT: sources record only documents whose content was actually injected
# into the prompt (i.e. present in context_ids). The agent target is NOT added as
# a source on its own — only when the user explicitly attaches it via
# context_ids. The target's title anchor ("# Current document" in the prompt)
# still informs the agent of its working document; it is simply not rendered as
# attached context.


async def test_agent_reference_target_kind_is_reference(
    client, admin_user, project_with_doc, harness_env,
):
    """Agent target pointing at an is_reference=true doc AND in context_ids →
    kind=reference (the source row is produced by the context-reference loop)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id, content="Ref body content", title="AgentRefTarget")
    sid = await _create_agent_session(client, token, pid, target_doc_id=ref_id, document_id=ref_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Rewrite this"}],
        "context_ids": [ref_id],
        "selection": {"doc_id": ref_id, "from_cp": 0, "to_cp": 3, "original_text": "Ref", "version": 1},
    })
    sources = await _assistant_sources(client, token, sid)
    target_sources = [s for s in sources if s["id"] == ref_id]
    assert len(target_sources) == 1
    assert target_sources[0]["retrieved"] is False
    assert target_sources[0]["kind"] == "reference"
