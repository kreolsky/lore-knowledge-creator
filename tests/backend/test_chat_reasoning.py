"""Tests for reasoner-model chain-of-thought streaming.

Step 3 (lore-renders-dsh-conversation): reasoning flows VERBATIM through the
relay inside the settled `assistant/message` content (a `{type:'reasoning'}`
block — v3 killed the assistant/chunk kind; deltas never enter the log, the
live tail rides `dsh_stream` frames this test does not exercise) and is
NEVER sent back to the model — only the last user turn travels as `prompt`
(history is canonical in the Pi session tree), so the model cannot see its
own prior CoT.

The SSE arm is gone (plan agent-line-harness-lifecycle step 9): the turn is
driver-owned, driven here through the standing channel (the harness_env
recipe — fake connector/replay + post_followup at the module's own seams,
the relay arms persisting to the REAL test DB). The verbatim reasoning
frames are read off the owner's project-WS chat_frame envelopes, the
outbound contract off the followup payload, the product off the row.
"""
import re

import pytest
from test_driver_channel import _env, _turn_end
from test_harness_turn import (
    _content_landed,
    _list_messages,
    _open_ws,
    _recv_chat_frames,
    _settle,
    _until_async,
)


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


# The per-turn send stamp rides the outbound prompt tail (UTC in tests).
SENT_STAMP_RE = re.compile(
    r"\[sent \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\]$"
)


def _reasoning_message(seq: int, cot: str, answer: str) -> dict:
    """One settled v3 `assistant/message` — the CoT rides a `reasoning`
    content block beside the answer's `text` block, verbatim through the
    relay."""
    return {"type": "dsh_event", "kind": "assistant/message", "seq": seq,
            "data": {"turn": 1, "step": 1,
                     "message": {"id": f"m{seq}", "role": "assistant",
                                 "content": [
                                     {"type": "reasoning", "text": cot},
                                     {"type": "text", "text": answer}],
                                 "source": {"kind": "model", "provider": "lore",
                                            "model": "reasoner"}},
                     "stream": []},
            "surfaceOp": "append"}


async def test_reasoning_streamed_and_excluded_from_history(
    sync_app, client, admin_user, project_with_doc, harness_env,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    env = harness_env

    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "reasoner",
              "reasoning_effort": "high"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["session_id"]

    owner = _open_ws(sync_app, pid, token)
    try:
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={"messages": [{"role": "user", "content": "Q?"}]},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is True

        # Feed the driver's frames through the REAL relay arms (the standing
        # channel's socket): model_update opens the bound turn, the settled
        # message flows through the accumulation arm, turn/end closes it.
        # Reasoning rides the message's content verbatim (the browser's
        # assembler keeps the blocks); the arm accumulates content only —
        # reasoning is never written to the row (the reload trace is the
        # driver's log).
        sock = env.connector.sockets[0]
        for frame in (
            {"type": "model_update", "model": "reasoner"},
            _reasoning_message(1, "First I think.\nThen more.", "The answer."),
            _turn_end(2),
        ):
            sock.push(_env(sid, frame))
        # Let the recv → dispatch → pump → send chain flush BEFORE the
        # blocking receive (receive_text parks the test loop; the pump needs
        # loop time to deliver — the test_chat_fanout settling pattern).
        await _settle()

        # (a) the settled message relays VERBATIM, carrying the CoT in its
        # content blocks — preamble first, one writer, one order.
        got = _recv_chat_frames(owner[1], 5)
        assert got[0]["session_id"] == sid
        assert got[0]["frame"]["type"] == "ids"
        assert got[0]["frame"]["assistant_message_id"] == body["assistant_msg_id"]
        assert got[1]["frame"] == {"type": "model_update", "model": "reasoner"}
        assert got[2]["frame"]["data"]["message"]["content"] == [
            {"type": "reasoning", "text": "First I think.\nThen more."},
            {"type": "text", "text": "The answer."}]
        assert got[3]["frame"]["kind"] == "turn/end"
        assert got[4]["frame"] == {"type": "done", "content": "The answer."}
    finally:
        owner[0].__exit__(None, None, None)

    # (b) the persisted assistant message row has the content; reasoning is
    # NOT written to the row (the driver's log owns the reload trace).
    await _until_async(lambda: _content_landed(client, token, sid, "The answer."))
    msgs = await _list_messages(client, token, sid)
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    assert assistant["content"] == "The answer."
    assert "First I" not in assistant["content"]

    # (c) the outbound turn contract carries only the last user turn as `prompt` —
    # never a messages[] history or a reasoning key — so the model can't see its
    # own prior CoT (Stage 9 / decision 5). The send stamp rides the tail; the
    # session's pinned reasoning effort rides the payload (absent = Default —
    # pinned by test_reasoning_effort).
    payload = env.followups.payloads[0]
    assert payload["prompt"].startswith("Q?")
    assert SENT_STAMP_RE.search(payload["prompt"])
    assert "messages" not in payload
    assert payload["reasoning_effort"] == "high"
