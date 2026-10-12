"""Unit tests for completions' decomposed turn helpers (R1 chat round).

`create_completion` was a 307-line route. The extracted turn phases are pinned
here at the unit level: session guards, the driver-owned send's failure ladder
+ followup payload (run_harness_turn), and the claim-time row mint
(driver.persistence._mint_claim_rows). Live coverage is test_harness_turn's.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from helpers import pinned_chat_api
from routes.chat.completions import (
    _AgentTurn,
    _validate_chat_turn,
)
from routes.chat.completions_harness import run_harness_turn


def _turn(**kw) -> _AgentTurn:
    base = dict(
        session_id="s1",
        session={"project_id": "p1", "document_id": None, "system_prompt_id": None},
        body=SimpleNamespace(
            parent_id=None,
            messages=[SimpleNamespace(content="hi", images=["data:image/png;base64,x"])],
        ),
        user={"user_id": "u1"},
        model="m1",
        project_id="p1",
        toolset=[{"function": {"name": "read_document"}}],
        capability_is_mutating=False,
        ctx=SimpleNamespace(
            sources=[{"id": "d1", "title": "Doc"}],
            warnings=[{"code": "w", "id": None, "detail": "x"}],
        ),
        user_msg_id="um1",
        assistant_msg_id="am1",
    )
    base.update(kw)
    return _AgentTurn(**base)


# ─── _validate_chat_turn ─────────────────────────────────────────────────────


class TestValidateChatTurn:
    async def test_note_session_rejected_400(self):
        with pytest.raises(HTTPException) as e:
            await _validate_chat_turn({"is_note": True}, {"user_id": "u1"})
        assert e.value.status_code == 400
        assert "Note sessions" in e.value.detail

    async def test_unconfigured_api_does_not_gate_the_turn(self):
        # The retired CHAT_API_URL gate: the turn is DRIVER-executed and the
        # driver's own line gate (resolve_driver_line → None) answers
        # availability — an unset AI_API_URL must not refuse here.
        await self._turn_with_empty_api_passes()

    async def test_rate_limit_exceeded_429(self):
        with patch("routes.chat.completions.check_completion_rate_limit",
                   return_value=False):
            with pytest.raises(HTTPException) as e:
                await _validate_chat_turn({}, {"user_id": "u1"})
        assert e.value.status_code == 429

    async def test_plain_session_passes(self):
        await self._turn_with_empty_api_passes()

    async def _turn_with_empty_api_passes(self):
        # One shared seam (import-shape zero-net-growth): the empty-API pin is
        # part of the PASS both callers assert — an unset AI_API_URL must not
        # reject the turn.
        with pinned_chat_api(""), \
             patch("routes.chat.completions.check_completion_rate_limit",
                   return_value=True):
            await _validate_chat_turn({}, {"user_id": "u1"})


# ─── run_harness_turn (the send hand-off unit level) ─────────────────────────


def _line():
    from driver.client import DriverLine
    return DriverLine(name="pi", url="http://pi.test", secret="s")


def _accept_patches(plan, followup):
    return (
        patch("routes.chat.completions_harness.ensure_fanout",
              new_callable=AsyncMock, return_value=True),
        patch("routes.chat.completions_harness.prepare_agent_turn",
              new_callable=AsyncMock, return_value=plan),
        patch("driver.timeline.post_followup", followup),
        patch("agent.keys.get_or_create_session_agent_key",
              new_callable=AsyncMock, return_value="key1"),
    )


class TestRunHarnessTurn:
    """The send's hand-off at the module's own seams (fanout, prepare, agent
    key, followup). The live channel path — the claim, frames, persistence —
    is test_harness_turn's."""

    async def test_setup_failure_answers_500_and_records_it(self):
        recorder = AsyncMock()
        with patch("routes.chat.completions_harness.ensure_fanout",
                   new_callable=AsyncMock, return_value=True), \
             patch("routes.chat.completions_harness.prepare_agent_turn",
                   new_callable=AsyncMock, side_effect=RuntimeError("prep")), \
             patch("driver.persistence._record_turn_error", recorder):
            with pytest.raises(HTTPException) as e:
                await run_harness_turn(_turn(line=_line()))
        assert e.value.status_code == 500
        assert "Agent setup failed" in e.value.detail
        assert recorder.await_args.kwargs["reason"] == "agent_setup_failed"

    async def test_accepted_send_carries_the_claim_facts_and_the_message_id(self):
        from routes.chat.completions_turn import AgentTurnPlan

        followup = AsyncMock(return_value={
            "accepted": True, "dsh_session_id": "dsh-1", "message_id": "dm-7"})
        plan = AgentTurnPlan(system_prompt="sp", apply_mode="auto", tools=[])
        p1, p2, p3, p4 = _accept_patches(plan, followup)
        with p1, p2, p3, p4:
            resp = await run_harness_turn(_turn(line=_line()))

        assert json.loads(resp.body) == {
            "accepted": True, "user_msg_id": "um1",
            "assistant_msg_id": "am1", "dsh_session_id": "dsh-1",
            "message_id": "dm-7",
        }
        payload = followup.call_args.args[0]
        assert payload["session_id"] == "s1"
        assert payload["system_prompt"] == "sp"
        assert payload["agent_key"] == "key1"
        assert payload["harness"] is True
        # The facts `lore/claim` returns verbatim: the row pair and the
        # preamble are minted from them at claim time.
        assert {k: payload[k] for k in (
            "user_msg_id", "assistant_msg_id", "user_text", "images",
            "sources", "warnings")} == {
            "user_msg_id": "um1", "assistant_msg_id": "am1", "user_text": "hi",
            "images": ["data:image/png;base64,x"],
            "sources": [{"id": "d1", "title": "Doc"}],
            "warnings": [{"code": "w", "id": None, "detail": "x"}],
        }

    async def test_config_refusal_is_a_422_with_the_drivers_reason(self):
        from driver.timeline import DriverTurnRefused
        from routes.chat.completions_turn import AgentTurnPlan

        followup = AsyncMock(side_effect=DriverTurnRefused(
            "Set the chat model in Admin panel → Models & APIs"))
        plan = AgentTurnPlan(system_prompt="sp", apply_mode="auto", tools=[])
        p1, p2, p3, p4 = _accept_patches(plan, followup)
        with p1, p2, p3, p4, \
             patch("driver.persistence._record_turn_error", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as e:
                await run_harness_turn(_turn(line=_line()))
        assert e.value.status_code == 422
        assert e.value.detail == "Set the chat model in Admin panel → Models & APIs"

    async def test_unconfigured_line_is_a_503_before_any_driver_call(self):
        fanout = AsyncMock()
        with patch("routes.chat.completions_harness.ensure_fanout", fanout):
            with pytest.raises(HTTPException) as e:
                await run_harness_turn(_turn(line=None))
        assert e.value.status_code == 503
        fanout.assert_not_awaited()


# ─── _mint_claim_rows (the pair, written at dsh's claim) ─────────────────────
# Against the REAL test DB: the head lookup and the tail read are queries whose
# semantics (ORDER BY, deleted_at, type::record ids) a fake would only mirror.


async def _chat(db, cid: str, **fields) -> None:
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": cid, "f": {"project_id": "p1", "user_id": "u1", "title": "t", **fields}},
    )


async def _row(db, mid: str, cid: str, parent: str | None, role: str = "user") -> None:
    await db.query(
        "CREATE type::record('messages', $id) CONTENT $f",
        {"id": mid, "f": {"chat_id": cid, "parent_id": parent, "role": role, "content": "c"}},
    )


async def _rows(db, cid: str) -> list[dict]:
    return await db.query(
        "SELECT meta::id(id) AS mid, parent_id, role, content, model, images "
        "FROM messages WHERE chat_id = $cid ORDER BY created_at",
        {"cid": cid},
    ) or []


def _claim(**kw) -> dict:
    return {"user_msg_id": f"um-{uuid4()}", "assistant_msg_id": f"am-{uuid4()}",
            "user_text": "hello", "images": [], "model": "m1", **kw}


class TestMintClaimRows:
    async def test_pair_appends_at_the_chats_tail(self, test_db):
        from driver.persistence import _mint_claim_rows

        cid = f"chat-{uuid4()}"
        await _chat(test_db, cid)
        await _row(test_db, "a", cid, None)
        await _row(test_db, "b", cid, "a", role="assistant")
        claim = _claim()

        head = await _mint_claim_rows(cid, claim)

        assert head["cid"] == cid and head["user_id"] == "u1"
        rows = await _rows(test_db, cid)
        user, assistant = rows[-2], rows[-1]
        assert (user["mid"], user["parent_id"], user["role"], user["content"]) == (
            claim["user_msg_id"], "b", "user", "hello")
        assert (assistant["mid"], assistant["parent_id"], assistant["role"]) == (
            claim["assistant_msg_id"], claim["user_msg_id"], "assistant")
        assert assistant["model"] == "m1" and assistant["content"] == ""
        assert user.get("images") is None

    async def test_first_turn_has_no_parent_and_keeps_its_images(self, test_db):
        from driver.persistence import _mint_claim_rows

        cid = f"chat-{uuid4()}"
        await _chat(test_db, cid)
        claim = _claim(images=["data:image/png;base64,x"])

        await _mint_claim_rows(cid, claim)

        user = (await _rows(test_db, cid))[0]
        assert user.get("parent_id") is None
        assert user["images"] == ["data:image/png;base64,x"]

    async def test_a_live_continuation_is_the_head(self, test_db):
        """A message queued before a compaction answers in the continuation;
        a deleted continuation is never the head."""
        from driver.persistence import _mint_claim_rows

        src = f"chat-{uuid4()}"
        await _chat(test_db, src)
        cont, dead = f"cont-{uuid4()}", f"dead-{uuid4()}"
        await _chat(test_db, cont, compacted_from=src)
        await _chat(test_db, dead, compacted_from=src)
        await test_db.query(
            "UPDATE type::record('chat_sessions', $id) SET deleted_at = time::now()",
            {"id": dead})

        head = await _mint_claim_rows(src, _claim())

        assert head["cid"] == cont
        assert len(await _rows(test_db, cont)) == 2
        assert await _rows(test_db, src) == []

    async def test_no_live_chat_mints_nothing(self, test_db):
        from driver.persistence import _mint_claim_rows

        claim = _claim()
        assert await _mint_claim_rows(f"gone-{uuid4()}", claim) is None
        assert await test_db.query(
            "SELECT * FROM type::record('messages', $id)",
            {"id": claim["user_msg_id"]}) == []
