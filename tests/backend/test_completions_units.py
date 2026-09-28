"""Unit tests for completions' decomposed turn helpers (R1 chat round).

`create_completion` was a 307-line route. The extracted turn phases are pinned
here at the unit level: session guards, message-row creation, the driver-owned
turn's failure ladder + followup payload (run_harness_turn), the preamble frame
shapes, and the turn-lock heartbeat's beat budget. The SSE orchestrator died
with the per-turn stream (plan agent-line-harness-lifecycle step 9); its live
coverage is test_harness_turn's, and the pieces that survived the cut are
pinned here without a channel.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from helpers import pinned_chat_api
from routes.chat.completions import (
    _AgentTurn,
    _create_turn_messages,
    _validate_chat_turn,
)
from routes.chat.completions_harness import _preamble_frames, run_harness_turn

import config


def _turn(**kw) -> _AgentTurn:
    base = dict(
        session_id="s1",
        session={"project_id": "p1", "document_id": None, "system_prompt_id": None},
        body=SimpleNamespace(
            parent_id=None,
            messages=[SimpleNamespace(content="hi", images=None)],
        ),
        user={"user_id": "u1"},
        model="m1",
        project_id="p1",
        toolset=[{"function": {"name": "read_document"}}],
        capability_is_mutating=False,
        ctx=SimpleNamespace(sources=[], warnings=[]),
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


# ─── _create_turn_messages ───────────────────────────────────────────────────


class TestCreateTurnMessages:
    def _body(self, images):
        return SimpleNamespace(
            parent_id="par",
            messages=[SimpleNamespace(content="hello", images=images)],
        )

    async def test_creates_user_and_assistant_rows(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value=[{}, {}])
        await _create_turn_messages(db, "s1", self._body(None), "m1", "um", "am")
        sql, params = db.query.call_args.args
        assert "role = 'user'" in sql and "role = 'assistant'" in sql
        assert "images" not in sql
        assert params == {"uid": "um", "aid": "am", "cid": "s1", "pid": "par",
                          "uc": "hello", "model": "m1"}

    async def test_images_column_only_when_attached(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value=[{}, {}])
        await _create_turn_messages(
            db, "s1", self._body(["data:image/png;base64,x"]), "m1", "um", "am",
        )
        sql, params = db.query.call_args.args
        assert ", images = $imgs" in sql
        assert params["imgs"] == ["data:image/png;base64,x"]

    async def test_sdk_error_string_raises_500(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value="SDK error")
        with pytest.raises(HTTPException) as e:
            await _create_turn_messages(db, "s1", self._body(None), "m1", "um", "am")
        assert e.value.status_code == 500


# ─── _preamble_frames (the retired SSE preamble's frame-dict survivor) ───────


class TestPreambleFrames:
    def test_ids_sources_warnings_in_order(self):
        turn = _turn(ctx=SimpleNamespace(
            sources=[{"id": "d1", "title": "Doc"}],
            warnings=[{"code": "w", "id": None, "detail": "x"}],
        ))
        assert _preamble_frames(turn) == [
            {"type": "ids", "user_message_id": "um1",
             "assistant_message_id": "am1"},
            {"type": "sources", "sources": [{"id": "d1", "title": "Doc"}]},
            {"type": "context_warning", "code": "w", "id": None, "detail": "x"},
        ]

    def test_bare_turn_is_ids_only(self):
        assert _preamble_frames(_turn()) == [
            {"type": "ids", "user_message_id": "um1",
             "assistant_message_id": "am1"},
        ]


# ─── run_harness_turn (the turn hand-off unit level) ─────────────────────────


class TestRunHarnessTurn:
    """The driver-owned turn's hand-off at the module's own seams (fanout,
    prepare, agent key, channel, followup). The live channel path — frames,
    persistence, the lock's release on turn end — is test_harness_turn's."""

    def _line(self):
        from driver.client import DriverLine
        return DriverLine(name="pi", url="http://pi.test", secret="s")

    async def test_setup_failure_answers_500_with_cleanup(self):
        db = AsyncMock()
        teardown = AsyncMock()
        deleter = AsyncMock()
        channel = MagicMock()
        with patch("routes.chat.completions_harness.ensure_fanout",
                   new_callable=AsyncMock, return_value=True), \
             patch("routes.chat.completions_harness.prepare_agent_turn",
                   new_callable=AsyncMock, side_effect=RuntimeError("prep")), \
             patch("routes.chat.completions_harness.get_driver_channel",
                   return_value=channel), \
             patch("routes.chat.completions_harness._teardown_turn_lock", teardown), \
             patch("routes.chat.completions_harness._delete_empty_assistant",
                   deleter), \
             patch("driver.persistence._record_turn_error", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as e:
                await run_harness_turn(_turn(line=self._line()), db, "tok")
        assert e.value.status_code == 500
        assert "Agent setup failed" in e.value.detail
        # No silent degradation: the owner saw error + done through the
        # listener queue, and the placeholder row + lock were torn down.
        emitted = channel.emit_frames.call_args.args[1]
        assert [f["type"] for f in emitted] == ["error", "done"]
        teardown.assert_awaited_once_with("s1", "tok", None)
        deleter.assert_awaited_once_with(db, "am1")

    async def test_accepted_turn_posts_payload_and_defers_release_to_on_end(self):
        from routes.chat.completions_turn import AgentTurnPlan

        db = AsyncMock()
        teardown = AsyncMock()
        channel = MagicMock()
        followup = AsyncMock(
            return_value={"accepted": True, "dsh_session_id": "dsh-1"})
        plan = AgentTurnPlan(system_prompt="sp", apply_mode="auto", tools=[])
        with patch("routes.chat.completions_harness.ensure_fanout",
                   new_callable=AsyncMock, return_value=True), \
             patch("routes.chat.completions_harness.prepare_agent_turn",
                   new_callable=AsyncMock, return_value=plan), \
             patch("routes.chat.completions_harness.get_driver_channel",
                   return_value=channel), \
             patch("driver.timeline.post_followup", followup), \
             patch("routes.chat.completions_harness._teardown_turn_lock", teardown), \
             patch("agent.keys.get_or_create_session_agent_key",
                   new_callable=AsyncMock, return_value="key1"):
            resp = await run_harness_turn(_turn(line=self._line()), db, "tok")

            assert json.loads(resp.body) == {
                "accepted": True, "user_msg_id": "um1",
                "assistant_msg_id": "am1", "dsh_session_id": "dsh-1",
            }
            payload = followup.call_args.args[0]
            assert payload["session_id"] == "s1"
            assert payload["system_prompt"] == "sp"
            assert payload["assistant_msg_id"] == "am1"
            assert payload["agent_key"] == "key1"
            assert payload["harness"] is True
            # The preamble (ids first) rides the SAME listener queue, BEFORE the
            # followup POST — the queue is the one ordering point.
            assert channel.emit_frames.call_args.args[1][0]["type"] == "ids"
            # The lock outlives the HTTP answer: released only when the CHANNEL
            # closes the turn (the bind's on_end), never at return. The on_end
            # exercise stays INSIDE the patch context — outside it the module
            # global is the real teardown again and the mock never sees the call.
            teardown.assert_not_awaited()
            on_end = channel.bind_turn.call_args.kwargs["on_end"]
            await on_end()
            teardown.assert_awaited_once()
            heartbeat = teardown.call_args.args[2]
            heartbeat.cancel()  # the mocked teardown skipped its cancellation


class TestHeartbeatTurnLock:
    async def test_beats_cover_the_whole_held_window(self, monkeypatch):
        """The heartbeat loop must cover TURN_MAX_WALL_S + TURN_HOLD_MAX_S (the
        wall-clock worst case of a progress-extended, hold-paused turn), not
        the pre-split total alone — sized at 300 only, the lock expired
        mid-hold at ~340s and admitted a second concurrent turn on the same
        session."""
        import math

        # The heartbeat fn + the constants/extend seam it reads live in
        # turn_lock (moved there when the SSE pump died) — patch the module
        # whose function reads them.
        import turn_lock

        monkeypatch.setattr(config, "TURN_MAX_WALL_S", 0.2)
        monkeypatch.setattr(config, "TURN_HOLD_MAX_S", 0.4)
        monkeypatch.setattr(config, "TURN_LOCK_HEARTBEAT_S", 0.05)

        extends = AsyncMock()
        monkeypatch.setattr(turn_lock, "extend_turn_lock", extends)

        await turn_lock._heartbeat_turn_lock("s1", "tok")

        expected = math.ceil((0.2 + 0.4) / 0.05) + 1
        assert extends.await_count >= expected - 1, (
            f"{extends.await_count} beats do not cover the held worst case "
            f"({expected - 1} beats needed for 0.6s at 0.05s cadence)"
        )
