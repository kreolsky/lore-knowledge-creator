"""The backend relay layer after the translator's death (plan
lore-renders-dsh-conversation step 3): `driver.frames` is no longer a
VOCABULARY — it accumulates the backend's own facts off the verbatim
`dsh_event` frames, mints the two backend-product lore events, and relays
everything else byte-identical. The units under test:

- `_TurnProjection` — the accumulation (content / sources / model / the
  finished-step count / the driver_seq stamp) and the abnormal writes;
- `_relay_frame` — the dispatch (dsh_event → arm, accumulate frames, error
  frame, everything else verbatim);
- `_lore_event` — the backend-minted lore envelope, whose fractional offsets
  PIN the parity with the browser's LORE_SEQ_OFFSETS (lore-events.ts);
- the reload attach mints (`attach_reload_lore_mints`) — the backend products
  a reload re-derives from the rows, anchored where the live mints were;
- the image-gen anchors — the LAUNCHER's `resolve_image_gen_anchor` over
  the same driver replay the reload reads, and the worker's settled/running
  frame builders over that frozen anchor (one producer: live == reload by
  construction, pinned by calling both).

The offsets are mirrored LITERALS: the plugin package's import graph cannot
reach the backend, so `driver.frames._lore_event` restates
LORE_SEQ_OFFSETS (harness-driver/conversation/src/lore-events.ts). The TS
side is pinned by its own tests (map.test.ts asserts 7.5 / 9.7); THIS file
pins the Python side. Change both together or a reload lands elsewhere than
the live stream did.
"""
import json
from unittest.mock import AsyncMock

import driver.frames
from driver.timeline import DriverTimelineUnavailable


def _turn(session_id: str = "chat-1") -> driver.frames._TurnProjection:
    return driver.frames._TurnProjection(
        assistant_msg_id="m1", persist_content=AsyncMock(),
        persist_sources=AsyncMock(), persist_extras=AsyncMock(),
        persist_turn_seq=AsyncMock(), session_id=session_id,
    )


async def _relay(turn, ev):
    return await driver.frames._relay_frame(turn, ev)


def _dsh(seq, kind, data=None, **extra):
    return {"type": "dsh_event", "kind": kind, "seq": seq, "data": data, **extra}


def _assistant_message(seq, text, turn=1, step=1):
    """A settled v3 `assistant/message` — the whole step text in one event
    (deltas never enter the log; the live tail rides `dsh_stream`)."""
    return _dsh(seq, "assistant/message", {
        "turn": turn, "step": step,
        "message": {"id": f"m{seq}", "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "source": {"kind": "model", "provider": "lore", "model": "x"}},
        "stream": []}, surfaceOp="append")


def _parse(frames):
    # The relay arms speak frame DICTS (the WS vocabulary) since the SSE wire
    # died with the pump (plan agent-line-harness-lifecycle step 9).
    return list(frames)


# ─── _lore_event: the mint envelope + the offset parity pin ──────────────────


def test_lore_event_offsets_pin_the_ts_lore_seq_offsets():
    """The mirrored literals. lore-events.ts (LORE_SEQ_OFFSETS): verdictAsk
    0.5, imageGen 0.6, halt 0.7, compactionMint 0.8 — the producers mint with
    exactly these, so a reload lands where the live stream did. Changing one
    side without the other silently breaks reload==live placement."""
    assert driver.frames._lore_event("lore/verdict-ask", 10, {})["seq"] == 10.5
    assert driver.frames._lore_event("lore/image-gen", 10, {})["seq"] == 10.6
    assert driver.frames._lore_event("lore/halt", 10, {})["seq"] == 10.7
    assert driver.frames._lore_event("lore/compaction-mint", 10, {})["seq"] == 10.8


def test_lore_event_envelope_is_an_ignorable_session_event():
    frame = driver.frames._lore_event("lore/halt", 4, {"turn": None, "reason": "x"})
    assert frame["type"] == "lore/halt"
    assert frame["seq"] == 4.7
    assert frame["ignorable"] is True
    assert isinstance(frame["time"], int)
    assert frame["data"] == {"turn": None, "reason": "x"}


# ─── _TurnProjection: the accumulation ───────────────────────────────────────


async def test_assistant_message_text_accumulates_and_finalize_persists():
    """v3: the row's content comes from the SETTLED `assistant/message` (the
    whole step text per event, joined across the turn's steps) — the dead
    `assistant/chunk` kind no longer exists in the log."""
    turn = _turn()
    await _relay(turn, _assistant_message(1, "Hello"))
    await _relay(turn, _assistant_message(2, "world"))
    await turn.finalize()
    turn._persist_content.assert_awaited_once_with("m1", "Hello\n\nworld")  # type: ignore[attr-defined]


async def test_steps_join_as_separate_paragraphs():
    """A step ending in a list item must not swallow the next step's text:
    the tool run between them is dropped from the row, so the join is a
    paragraph break, with the steps' own edge newlines and blank steps
    folded away."""
    turn = _turn()
    await _relay(turn, _assistant_message(1, "Plan:\n\n1. one\n2. two\n"))
    await _relay(turn, _assistant_message(2, "  \n"))
    await _relay(turn, _assistant_message(3, "\nSaved to the doc."))
    await turn.finalize()
    turn._persist_content.assert_awaited_once_with(  # type: ignore[attr-defined]
        "m1", "Plan:\n\n1. one\n2. two\n\nSaved to the doc.")


async def test_non_text_message_blocks_accumulate_nothing():
    turn = _turn()

    def msg(content):
        return {"turn": 1, "step": 1,
                "message": {"content": content}, "stream": []}

    await _relay(turn, _dsh(1, "assistant/message", msg(
        [{"type": "reasoning", "text": "hmm"}])))
    await _relay(turn, _dsh(2, "assistant/message", msg(
        [{"type": "tool-call", "id": "t1", "name": "search"}])))
    # Non-string text and a non-list content degrade to nothing.
    await _relay(turn, _dsh(3, "assistant/message", msg(
        [{"type": "text", "text": 5}])))
    await _relay(turn, _dsh(4, "assistant/message", {"message": {"content": "flat"}}))
    await _relay(turn, _dsh(5, "assistant/message", None))
    assert turn.content_acc == []


async def test_replace_op_message_does_not_join_the_row():
    """Only the `append` surface op accumulates — dsh's own assistant node's
    filter. A replace-op re-statement of an earlier message (a rewrite of
    the surface) would otherwise land the same text in the row twice."""
    turn = _turn()
    await _relay(turn, _assistant_message(1, "once"))
    replaced = _assistant_message(2, "once")
    replaced["surfaceOp"] = {"op": "replace", "start": 1, "end": 1}
    await _relay(turn, replaced)
    await _relay(turn, _assistant_message(3, " more"))
    assert "".join(turn.content_acc) == "once more"


def test_message_text_joins_only_text_blocks():
    data = {"message": {"content": [
        {"type": "reasoning", "text": "thinking"},
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
        {"type": "text"},
        "bare string",
    ]}}
    assert driver.frames._message_text(data) == "ab"
    assert driver.frames._message_text({}) == ""


async def test_tool_result_counts_one_finished_call_per_event():
    turn = _turn()
    result = {"message": {"role": "tool", "source": {"kind": "tool", "callId": "c1"},
                          "toolCallId": "c1", "content": [{"type": "text", "text": "ok"}]}}
    await _relay(turn, _dsh(1, "tool/result", result))
    await _relay(turn, _dsh(2, "tool/result", result))
    assert turn.steps_count == 2


async def test_turn_start_sets_the_turn_coordinate_and_warns_without_one(caplog):
    turn = _turn()
    await _relay(turn, _dsh(1, "turn/start", {"turn": 3}))
    assert turn.turn_no == 3
    await _relay(turn, _dsh(9, "turn/start", {}))
    assert turn.turn_no == 3  # a missing number never clobbers the open turn


async def test_last_seq_tracks_the_window_tail():
    turn = _turn()
    await _relay(turn, _assistant_message(5, "x"))
    await _relay(turn, _dsh(2, "hook/invoked", {}))
    assert turn.last_seq == 5
    await _relay(turn, {"type": "model_update", "model": "m"})  # not a dsh event
    assert turn.last_seq == 5


async def test_turn_end_completed_is_graceful_and_stamps_driver_seq():
    turn = _turn()
    await _relay(turn, _assistant_message(1, "done"))
    frames = await _relay(turn, _dsh(9, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}))
    assert [f["type"] for f in _parse(frames)] == ["dsh_event", "done"]
    assert turn.finished and turn.finalize_pending
    assert turn.driver_seq == 9
    await turn.finalize()
    turn._persist_turn_seq.assert_awaited_once_with("m1", 9, None)  # type: ignore[attr-defined]
    turn._persist_extras.assert_not_awaited()  # type: ignore[attr-defined]


async def test_driver_seq_stamp_carries_the_turns_dsh_session():
    """The stamp is the pair: the seq plus the dsh session the turn ran in
    (a seq names a boundary only inside its own session's log)."""
    turn = driver.frames._TurnProjection(
        assistant_msg_id="m1", persist_content=AsyncMock(),
        persist_sources=AsyncMock(), persist_extras=AsyncMock(),
        persist_turn_seq=AsyncMock(), session_id="chat-1",
        dsh_session_id="chat-1~fdeadbee",
    )
    await _relay(turn, _dsh(9, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}))
    await turn.finalize()
    turn._persist_turn_seq.assert_awaited_once_with(  # type: ignore[attr-defined]
        "m1", 9, "chat-1~fdeadbee")


async def test_turn_end_max_tokens_is_graceful():
    """A max-tokens halt IS graceful — the halt card renders from dsh's own
    turn-max-tokens node; no Lore-side halt write, no lore/halt mint."""
    turn = _turn()
    frames = await _relay(turn, _dsh(9, "turn/end", {"turn": 1, "reason": {"kind": "max-tokens"}}))
    parsed = _parse(frames)
    assert len(parsed) == 2
    # The chunk-less turn's done frame still rides (contract: unconditional
    # on the graceful branch) — the store's done handler ignores "".
    assert parsed[1] == {"type": "done", "content": ""}
    turn._persist_extras.assert_not_awaited()  # type: ignore[attr-defined]
    await turn.finalize()
    turn._persist_content.assert_awaited_once()  # type: ignore[attr-defined]


async def test_turn_end_graceful_done_carries_the_joined_messages():
    """The graceful arm's second frame is the row-content producer (plan
    chat-message-content-empty-until-reload): the SAME join finalize()
    persists, so copy / create-document act on live text without a reload.
    An error turn/end mints NO done frame — the abnormal path owns its
    text and the error tail's sse_done("") stays as it was."""
    turn = _turn()
    await _relay(turn, _assistant_message(1, "Hello"))
    await _relay(turn, _assistant_message(2, "world"))
    frames = await _relay(turn, _dsh(9, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}))
    parsed = _parse(frames)
    assert parsed[1] == {"type": "done", "content": "Hello\n\nworld"}
    await turn.finalize()
    turn._persist_content.assert_awaited_once_with("m1", "Hello\n\nworld")  # type: ignore[attr-defined]

    errored = _turn()
    await _relay(errored, _assistant_message(1, "partial"))
    frames = await _relay(errored, _dsh(9, "turn/end", {
        "turn": 1, "reason": {"kind": "error", "error": {"message": "boom"}}}))
    assert [f["type"] for f in _parse(frames)] == ["dsh_event"]


async def test_turn_end_error_persists_the_abnormal_product():
    turn = _turn()
    await _relay(turn, _assistant_message(1, "partial"))
    await _relay(turn, _dsh(2, "tool/result", {"message": {
        "role": "tool", "source": {"kind": "tool", "callId": "c1"},
        "toolCallId": "c1", "content": [{"type": "text", "text": "ok"}]}}))
    await _relay(turn, _dsh(9, "turn/end", {
        "turn": 1, "reason": {"kind": "error", "error": {"message": "gateway 502"}}}))
    assert turn.errored and not turn.finalize_pending
    # The abnormal write already persisted the product; the façade gates
    # finalize() off (finalize_pending False), so this is the ONE write.
    turn._persist_content.assert_awaited_once_with("m1", "partial")  # type: ignore[attr-defined]
    extras = turn._persist_extras.call_args.args[1]  # type: ignore[attr-defined]
    # anchor_seq: the window tail the live card would have taken (see the
    # persisted INVARIANT in _persist_abnormal).
    assert extras["halt"] == {"reason": "error", "steps": 1, "anchor_seq": 9}
    turn._persist_turn_seq.assert_awaited_once_with("m1", 9, None)  # type: ignore[attr-defined]


async def test_abnormal_halt_stores_the_live_mints_anchor_and_turn():
    """The stored halt carries WHERE the live card stood, so the reload card
    lands at the same anchor instead of the log tail."""
    turn = _turn()
    await _relay(turn, _dsh(1, "turn/start", {"turn": 2}))
    await _relay(turn, _assistant_message(7, "w"))
    await _relay(turn, {"type": "error", "message": "boom", "halt_reason": "turn_timeout"})
    extras = turn._persist_extras.call_args.args[1]  # type: ignore[attr-defined]
    assert extras["halt"]["anchor_seq"] == 7
    assert extras["halt"]["turn"] == 2


async def test_turn_end_aborted_persists_without_a_message():
    turn = _turn()
    await _relay(turn, _dsh(9, "turn/end", {"turn": 1, "reason": {"kind": "aborted"}}))
    extras = turn._persist_extras.call_args.args[1]  # type: ignore[attr-defined]
    assert extras["halt"] == {"reason": "aborted", "anchor_seq": 9}


async def test_turn_end_unknown_reason_degrades_to_the_generic_sentence():
    turn = _turn()
    await _relay(turn, _dsh(9, "turn/end", {"turn": 1, "reason": {}}))
    turn._persist_content.assert_awaited_once_with(
        "m1", "The turn ended without completing (unknown reason).")  # type: ignore[attr-defined]


# ─── _relay_frame: the dispatch ───────────────────────────────────────────────


async def test_unknown_dsh_kinds_relay_verbatim_and_record_nothing():
    turn = _turn()
    frames = await _relay(turn, _dsh(12, "guard/reminder", {"note": "stay on task"}))
    assert _parse(frames) == [_dsh(12, "guard/reminder", {"note": "stay on task"})]
    assert turn.steps_count == 0 and turn.content_acc == []


async def test_unknown_typed_frames_relay_verbatim():
    turn = _turn()
    frames = await _relay(turn, {"type": "some_future_kind", "x": 1})
    assert _parse(frames) == [{"type": "some_future_kind", "x": 1}]


async def test_dsh_stream_frames_relay_untouched():
    """The transient live stream (`dsh_stream`, the assistant-stream tap the
    plugin pushes per session) rides the relay byte-identical — the module
    ARCH promise: the browser owns the transient fold, the backend derives
    nothing from it (the row content comes from the settled event)."""
    turn = _turn()
    frame = {"type": "dsh_stream", "frame": {
        "type": "chunk", "attemptId": "a1", "revision": 1, "index": 0,
        "time": 5, "chunk": {"type": "text-delta", "index": 0, "text": "x"}}}
    assert await _relay(turn, frame) == [frame]
    assert turn.content_acc == []  # a chunk NEVER accumulates: not the settled kind


async def test_model_update_and_context_usage_accumulate_and_relay():
    turn = _turn()
    mu = await _relay(turn, {"type": "model_update", "model": "deepseek/flash"})
    assert _parse(mu) == [{"type": "model_update", "model": "deepseek/flash"}]
    cu = await _relay(turn, {"type": "context_usage", "used": 1200, "cap": 64000})
    assert _parse(cu) == [{"type": "context_usage", "used": 1200, "cap": 64000}]
    assert turn.model == "deepseek/flash"
    assert turn.context_usage == {"used": 1200, "cap": 64000}


async def test_surface_metadata_rides_the_relay():
    turn = _turn()
    frames = await _relay(turn, _dsh(
        4, "user/message", {"role": "user", "content": []},
        surfaceOp={"op": "replace", "start": 1, "end": 3}, sourceEventSeqs=[1, 2, 3]))
    frame = _parse(frames)[0]
    assert frame["surfaceOp"] == {"op": "replace", "start": 1, "end": 3}
    assert frame["sourceEventSeqs"] == [1, 2, 3]


# ─── the backend-product lore mints on the live path ─────────────────────────


async def test_driver_error_frame_mints_the_halt_at_the_window_tail():
    turn = _turn()
    await _relay(turn, _dsh(1, "turn/start", {"turn": 2}))
    await _relay(turn, _assistant_message(7, "w"))
    frames = await _relay(turn, {"type": "error", "message": "boom", "halt_reason": "turn_timeout"})
    parsed = _parse(frames)
    assert [f["type"] for f in parsed] == ["error", "lore/halt"]
    assert parsed[1]["seq"] == 7.7
    assert parsed[1]["data"] == {"turn": 2, "reason": "turn_timeout", "message": "boom"}
    assert turn.errored and not turn.finalize_pending
    turn._persist_extras.assert_awaited_once()  # type: ignore[attr-defined]


async def test_driver_error_frame_without_a_tail_mints_nothing():
    """No dsh event ever relayed — no anchor exists, and a fabricated one
    would misplace the card. The error frame itself still relays."""
    turn = _turn()
    frames = await _relay(turn, {"type": "error", "message": "boom", "halt_reason": "stream_failed"})
    assert [f["type"] for f in _parse(frames)] == ["error"]


# ─── the compaction mint (window-keyed, best-effort) ─────────────────────────


async def test_compaction_end_mints_the_outcome_beside_the_frame(monkeypatch):
    calls: list[dict] = []

    async def fake_mint(payload):
        calls.append(payload)
        return {"continuation_chat_id": "cont-1", "created_continuation": True}

    monkeypatch.setattr(driver.persistence, "_record_compaction_mint_failed", AsyncMock())
    import driver.compaction as compaction
    monkeypatch.setattr(compaction, "mint_compaction_chats", fake_mint)

    turn = _turn()
    await _relay(turn, _dsh(1, "turn/start", {"turn": 4}))
    frames = await _relay(turn, _dsh(7, "compaction/end", {"compactionId": "cpt-9", "turn": 4}))
    parsed = _parse(frames)
    assert [f["type"] for f in parsed] == ["dsh_event", "lore/compaction-mint"]
    assert calls == [{"session_id": "chat-1", "fork_id": "chat-1~ccpt-9"}]
    assert parsed[1]["seq"] == 7.8
    assert parsed[1]["data"] == {"turn": 4, "compactionEntryId": "cpt-9", "mintFailed": False}


async def test_compaction_end_failure_is_reported_not_fatal(monkeypatch):
    async def fake_mint(payload):
        return {"continuation_chat_id": None, "reason": "source_chat_missing"}

    recorded: list[tuple] = []

    async def fake_record(msg_id, reason):
        recorded.append((msg_id, reason))

    monkeypatch.setattr(driver.persistence, "_record_compaction_mint_failed", fake_record)
    import driver.compaction as compaction
    monkeypatch.setattr(compaction, "mint_compaction_chats", fake_mint)

    turn = _turn()
    frames = await _relay(turn, _dsh(7, "compaction/end", {"compactionId": "cpt-9"}))
    parsed = _parse(frames)
    assert [f["type"] for f in parsed] == ["dsh_event", "lore/compaction-mint"]
    assert parsed[1]["data"]["mintFailed"] is True
    assert parsed[1]["data"]["mintReason"] == "source_chat_missing"
    assert recorded == [("m1", "source_chat_missing")]


async def test_compaction_end_raise_still_relays_the_frame(monkeypatch):
    async def boom(payload):
        raise RuntimeError("db down")

    monkeypatch.setattr(driver.persistence, "_record_compaction_mint_failed", AsyncMock())
    import driver.compaction as compaction
    monkeypatch.setattr(compaction, "mint_compaction_chats", boom)

    turn = _turn()
    frames = await _relay(turn, _dsh(7, "compaction/end", {"compactionId": "cpt-9"}))
    parsed = _parse(frames)
    assert [f["type"] for f in parsed] == ["dsh_event", "lore/compaction-mint"]
    assert parsed[1]["data"]["mintFailed"] is True
    assert "db down" in parsed[1]["data"]["mintReason"]


async def test_compaction_end_malformed_or_failed_compaction_mints_nothing():
    turn = _turn()
    frames = await _relay(turn, _dsh(7, "compaction/end", {"compactionId": "", "turn": 1}))
    assert len(_parse(frames)) == 1
    frames = await _relay(turn, _dsh(8, "compaction/end", {"compactionId": "c", "error": "boom"}))
    assert len(_parse(frames)) == 1
    frames = await _relay(turn, _dsh(9, "compaction/end", None))
    assert len(_parse(frames)) == 1


# ─── the reload attach mints ──────────────────────────────────────────────────


def _call_seq(frames, call_id):
    return next(f for f in frames
                if f.get("kind") == "tool/call" and f["data"]["callId"] == call_id)


def _result_frame(seq, call_id, text):
    """A settled v4 tool/result: a FLAT tool message (role 'tool')."""
    return _dsh(seq, "tool/result", {"message": {
        "role": "tool", "source": {"kind": "tool", "callId": call_id},
        "toolCallId": call_id, "content": [{"type": "text", "text": text}]}})


async def test_reload_halt_mints_from_the_row_column_at_the_tail(monkeypatch):
    """The turn-less halt's record: the row's `halt` column, anchored at the
    log tail — the plan's "the window tail when none exists"."""
    row = {"message_id": "m-ab", "created_at": "2", "halt": {"reason": "turn_timeout", "steps": 3}}
    await driver.frames.attach_reload_lore_mints(
        [row], chain={"m-ab"}, tail_seq=42, session_id="chat-1")
    mint = row["frames"][0]
    assert mint["type"] == "lore/halt"
    assert mint["seq"] == 42.7
    assert mint["data"] == {"turn": None, "reason": "turn_timeout", "steps": 3}


async def test_reload_halt_respects_the_active_line():
    """A halt row off the current lineage (a fork re-seeded the log) must not
    take the tail anchor — the tail belongs to the lineage the log holds."""
    off_branch = {"message_id": "m-off", "created_at": "1",
                  "halt": {"reason": "disconnected"}}
    await driver.frames.attach_reload_lore_mints(
        [off_branch], chain={"m-other"}, tail_seq=30, session_id="chat-1")
    assert off_branch.get("frames") is None


async def test_reload_halt_skips_rows_whose_turn_resolved():
    """A turn/end reason=error row carries a halt column too — but its turn
    IS in the replay and dsh's own turn-error node renders it. A mint here
    would double the card."""
    row = {"message_id": "m-err", "created_at": "1",
           "halt": {"reason": "error"}, "frames": [_dsh(9, "turn/end", {"reason": {"kind": "error"}})]}
    await driver.frames.attach_reload_lore_mints(
        [row], chain={"m-err"}, tail_seq=30, session_id="chat-1")
    assert [f["type"] for f in row["frames"]] == ["dsh_event"]


async def test_reload_halt_needs_a_numeric_tail():
    row = {"message_id": "m-ab", "created_at": "1", "halt": {"reason": "disconnected"}}
    await driver.frames.attach_reload_lore_mints(
        [row], chain={"m-ab"}, tail_seq=None, session_id="chat-1")
    assert row.get("frames") is None


async def test_reload_halt_mints_at_the_stored_anchor_with_its_turn():
    """A halt written with the live mint's anchor lands where the live card
    stood — inside its own turn, carrying the turn coordinate."""
    row = {"message_id": "m-ab", "created_at": "1",
           "halt": {"reason": "turn_timeout", "steps": 3, "anchor_seq": 7, "turn": 2}}
    await driver.frames.attach_reload_lore_mints(
        [row], chain={"m-ab"}, tail_seq=42, session_id="chat-1")
    mint = row["frames"][0]
    assert mint["type"] == "lore/halt"
    assert mint["seq"] == 7.7
    assert mint["data"] == {"turn": 2, "reason": "turn_timeout", "steps": 3}


async def test_reload_halt_mints_every_anchored_row():
    """Distinct stored anchors do not collide — every halted turn keeps its
    card, with no newest-only restriction."""
    old = {"message_id": "m-old", "created_at": "1",
           "halt": {"reason": "disconnected", "anchor_seq": 4, "turn": 1}}
    new = {"message_id": "m-new", "created_at": "2",
           "halt": {"reason": "turn_timeout", "anchor_seq": 9, "turn": 2}}
    await driver.frames.attach_reload_lore_mints(
        [old, new], chain=None, tail_seq=30, session_id="chat-1")
    assert old["frames"][0]["seq"] == 4.7
    assert old["frames"][0]["data"]["turn"] == 1
    assert new["frames"][0]["seq"] == 9.7


async def test_reload_halt_legacy_rows_keep_the_newest_only_tail():
    """Rows halted before the anchor was stored have no position but the log
    tail — so exactly one of them, the newest, still mints there."""
    old = {"message_id": "m-old", "created_at": "1", "halt": {"reason": "disconnected"}}
    new = {"message_id": "m-new", "created_at": "2", "halt": {"reason": "turn_timeout"}}
    await driver.frames.attach_reload_lore_mints(
        [old, new], chain=None, tail_seq=30, session_id="chat-1")
    assert old.get("frames") is None
    assert new["frames"][0]["seq"] == 30.7
    assert new["frames"][0]["data"]["turn"] is None


async def test_reload_halt_anchor_collision_drops_one_and_warns(caplog):
    """Two lore/halt mints at one seq would throw inside the assembler — the
    anchored row keeps the seq, the legacy tail mint is dropped, named."""
    anchored = {"message_id": "m-anch", "created_at": "1",
                "halt": {"reason": "turn_timeout", "anchor_seq": 30, "turn": 2}}
    legacy = {"message_id": "m-leg", "created_at": "2", "halt": {"reason": "disconnected"}}
    with caplog.at_level("WARNING"):
        await driver.frames.attach_reload_lore_mints(
            [anchored, legacy], chain=None, tail_seq=30, session_id="chat-1")
    assert anchored["frames"][0]["seq"] == 30.7
    assert legacy.get("frames") is None
    assert "m-leg" in caplog.text and "m-anch" in caplog.text


async def test_reload_halt_two_anchored_rows_at_one_seq_drop_the_later(caplog):
    anchored = {"message_id": "m-a", "created_at": "1",
                "halt": {"reason": "turn_timeout", "anchor_seq": 5, "turn": 1}}
    twin = {"message_id": "m-b", "created_at": "2",
            "halt": {"reason": "disconnected", "anchor_seq": 5, "turn": 2}}
    with caplog.at_level("WARNING"):
        await driver.frames.attach_reload_lore_mints(
            [anchored, twin], chain=None, tail_seq=99, session_id="chat-1")
    assert anchored["frames"][0]["seq"] == 5.7
    assert twin.get("frames") is None
    assert "m-b" in caplog.text


async def test_reload_image_gen_mints_at_the_dispatching_call():
    call = _dsh(3, "tool/call", {"turn": 1, "step": 2, "callId": "call-gen",
                                 "name": "generate_image",
                                 "arguments": json.dumps({"prompt": "a cat"})})
    # NO tool/result frame: the anchor is the dispatching call id the step
    # carries, never a parse of the result body.
    row = {
        "message_id": "m1", "created_at": "1",
        "frames": [_dsh(1, "turn/start", {"turn": 1}), call,
                   _dsh(9, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})],
        "gen_steps": [
            {"tool_call_id": "gen:run-1:refine", "tool": "refine_prompt",
             "summary": "refine prompt", "detail": "a cat, refined"},
            {"tool_call_id": "gen:run-1", "tool": "generate_image",
             "summary": "generate image", "image_ref_ids": ["ref-1", "ref-2"],
             "run_id": "run-1", "call_id": "call-gen", "title": "Мир документа"},
        ],
    }
    await driver.frames.attach_reload_lore_mints([row], chain={"m1"}, tail_seq=9, session_id="c")
    mints = [f for f in row["frames"] if f["type"] == "lore/image-gen"]
    assert len(mints) == 1
    mint = mints[0]
    assert mint["seq"] == 3.6  # the dispatching call's seq + the image-gen offset
    assert row["frames"].index(mint) == row["frames"].index(call) + 1
    assert mint["data"] == {
        "turn": 1, "runId": "run-1", "status": "done",
        "imageRefIds": ["ref-1", "ref-2"], "refine": {"ok": True, "prompt": "a cat, refined"},
        "title": "Мир документа",
    }


async def test_reload_image_gen_failed_run_carries_the_error():
    row = {
        "message_id": "m1", "created_at": "1",
        "frames": [
            _dsh(3, "tool/call", {"turn": 1, "step": 1, "callId": "call-gen",
                                  "name": "generate_image", "arguments": "{}"}),
        ],
        "gen_steps": [{
            "tool_call_id": "gen:run-f", "tool": "generate_image",
            "summary": "generate image", "detail": "comfy unreachable",
            "outcome": "failed", "run_id": "run-f", "call_id": "call-gen",
        }],
    }
    await driver.frames.attach_reload_lore_mints([row], chain={"m1"}, tail_seq=4, session_id="c")
    mint = next(f for f in row["frames"] if f["type"] == "lore/image-gen")
    assert mint["seq"] == 3.6
    assert mint["data"] == {"turn": 1, "runId": "run-f", "status": "failed",
                            "error": "comfy unreachable"}


async def test_reload_image_gen_without_its_call_mints_nothing():
    """The run's dispatching call is not in the replayed turn (truncated log,
    cross-turn attach) — no honest anchor exists. The reference itself is on
    the document; the gap is logged, never silently fabricated."""
    row = {
        "message_id": "m1", "created_at": "1",
        "frames": [_dsh(1, "turn/start", {"turn": 1})],
        "gen_steps": [{"tool": "generate_image", "run_id": "run-1",
                       "image_ref_ids": ["ref-1"]}],
    }
    await driver.frames.attach_reload_lore_mints([row], chain={"m1"}, tail_seq=1, session_id="c")
    assert not [f for f in row["frames"] if f.get("type") == "lore/image-gen"]


async def test_reload_image_gen_step_without_call_id_mints_nothing(caplog):
    """A step persisted without the dispatching call id has no anchor — even
    when a call and a result naming its run id are both in the replay, the
    result body is never parsed for one."""
    row = {
        "message_id": "m1", "created_at": "1",
        "frames": [
            _dsh(3, "tool/call", {"turn": 1, "step": 1, "callId": "call-gen",
                                  "name": "generate_image", "arguments": "{}"}),
            _result_frame(4, "call-gen", json.dumps({"status": "generating", "run_id": "run-1"})),
        ],
        "gen_steps": [{"tool": "generate_image", "run_id": "run-1",
                       "image_ref_ids": ["ref-1"]}],
    }
    await driver.frames.attach_reload_lore_mints([row], chain={"m1"}, tail_seq=4, session_id="c")
    assert not [f for f in row["frames"] if f.get("type") == "lore/image-gen"]
    assert "run-1" in caplog.text


# ─── the image-gen anchors: resolve (launcher) + settled/running frames ───────
#
# The LAUNCHER resolves the run's anchor ONCE over the driver's replay
# (resolve_image_gen_anchor — the anchor half of the reload mint) and freezes
# it into the job payload; the worker builds every frame from that anchor
# (image_gen_settled_frame / image_gen_running_frame) — no replay per phase,
# no replay for the settled card. The parity bound is the point: the settled
# frame uses the SAME builder the reload attach does, over the SAME step
# dicts — one producer, so live == reload by construction.


def _gen_steps(call_id="call-gen", *, failed=False):
    """The two chips a settled run persists (persist.py's step dicts)."""
    refine = {"tool_call_id": "gen:run-1:refine", "tool": "refine_prompt",
              "summary": "refine prompt",
              "detail": "queue full" if failed else "a cat, refined"}
    if failed:
        refine["outcome"] = "failed"
    gen = {"tool_call_id": "gen:run-1", "tool": "generate_image",
           "summary": "generate image", "run_id": "run-1",
           "call_id": call_id, "title": "Мир документа"}
    if failed:
        gen["outcome"] = "failed"
        gen["detail"] = "comfy unreachable"
    else:
        gen["image_ref_ids"] = ["ref-1", "ref-2"]
    return [refine, gen]


def _replay_with_call(call_id="call-gen", *, open_turn=False):
    """A ReplayedSession holding the run's dispatching `tool/call`. With
    `open_turn` the trailing turn carries no `end_seq` (the run finished while
    the agent turn still streams)."""
    call = _dsh(3, "tool/call", {"turn": 1, "step": 2, "callId": call_id,
                                 "name": "generate_image",
                                 "arguments": json.dumps({"prompt": "a cat"})})
    frames = [_dsh(1, "turn/start", {"turn": 1}), call]
    turn: dict = {"frames": frames}
    if open_turn:
        return call, {"turns": [turn], "tail_seq": 3}
    frames.append(_dsh(9, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}))
    turn["end_seq"] = 9
    return call, {"turns": [turn], "tail_seq": 9}


def _stub_replay(monkeypatch, replay=None, *, unavailable=False, forbidden=False, seen=None):
    """The ONE seam of the anchor tests: the driver replay fetch.
    `forbidden` proves a path never reads it; `unavailable` simulates a down
    driver line (DriverTimelineUnavailable)."""
    async def _fetch(session_id, line=None, *, since_seq=None):
        if seen is not None:
            seen.append(session_id)
        if forbidden:
            raise AssertionError("no replay read on this path")
        if unavailable:
            raise DriverTimelineUnavailable("line down")
        return replay
    monkeypatch.setattr(driver.frames, "fetch_session_entries", _fetch)


async def test_settled_frame_equals_the_reload_attach(monkeypatch):
    """The parity bound, by calling BOTH: the worker's settled frame over the
    payload anchor and the reload's attach over the same row must return
    EQUAL frames (the wall-clock `time` stamp aside — the drive's criterion; a
    literal payload assertion would drift with the payload and is
    deliberately not used)."""
    _call, replay = _replay_with_call()
    seen: list[str] = []

    async def _fetch(session_id, line=None, *, since_seq=None):
        seen.append(session_id)
        return replay
    monkeypatch.setattr(driver.frames, "fetch_session_entries", _fetch)
    anchor = await driver.frames.resolve_image_gen_anchor("src-1", "call-gen")
    assert seen == ["src-1"]
    assert anchor == {"seq": 3, "turn": 1}

    live = driver.frames.image_gen_settled_frame(anchor, _gen_steps(), "run-1")
    assert live is not None
    assert live["type"] == "lore/image-gen"
    assert live["seq"] == 3.6
    row = {"message_id": "m1", "created_at": "1",
           "frames": list(replay["turns"][0]["frames"]),
           "gen_steps": _gen_steps()}
    await driver.frames.attach_reload_lore_mints(
        [row], chain={"m1"}, tail_seq=9, session_id="c")
    reload_mint = next(f for f in row["frames"] if f["type"] == "lore/image-gen")
    live.pop("time"), reload_mint.pop("time")
    assert live == reload_mint


async def test_resolve_anchors_inside_the_open_trailing_turn(monkeypatch):
    """The run settles while the agent turn still streams (no `end_seq` on the
    trailing turn) — the dispatching call is already in the log, so the anchor
    resolves there exactly as the reload attach will."""
    _call, replay = _replay_with_call(open_turn=True)
    _stub_replay(monkeypatch, replay)
    anchor = await driver.frames.resolve_image_gen_anchor("src-1", "call-gen")
    assert anchor == {"seq": 3, "turn": 1}
    assert driver.frames.image_gen_settled_frame(
        anchor, _gen_steps(), "run-1")["seq"] == 3.6


async def test_resolve_without_a_call_id_returns_none(monkeypatch):
    """No dispatching call id — no anchor. The replay is never even read (the
    stub forbids the read)."""
    _stub_replay(monkeypatch, forbidden=True)
    assert await driver.frames.resolve_image_gen_anchor("src-1", "") is None


async def test_resolve_without_a_matching_call_returns_none(monkeypatch, caplog):
    """The replay holds no dispatching call for the call id — no honest
    anchor exists. The miss is NAMED (lineage + call id), never fabricated."""
    _call, replay = _replay_with_call(call_id="some-other-call")
    _stub_replay(monkeypatch, replay)
    with caplog.at_level("WARNING"):
        anchor = await driver.frames.resolve_image_gen_anchor("src-1", "call-gen")
    assert anchor is None
    assert "call-gen" in caplog.text


async def test_resolve_on_an_unreadable_timeline_returns_none(monkeypatch, caplog):
    """The driver could not serve the replay — best-effort anchor, loud miss."""
    _stub_replay(monkeypatch, unavailable=True)
    with caplog.at_level("WARNING"):
        anchor = await driver.frames.resolve_image_gen_anchor("src-1", "call-gen")
    assert anchor is None
    assert "call-gen" in caplog.text


def test_running_frames_ladder_below_the_settled_offset():
    """The RUNNING phase frames: the k-th phase
    sits at anchor + 0.5 + 0.1·k/(k+1) — dsh's own transient ladder formula —
    strictly above a verdict mint at the same anchor, strictly below the
    settled +0.6 mint (the reload's twin), and strictly increasing, so every
    phase of one run APPENDS into the assembler's one context (a duplicate or
    non-appended Match seq throws)."""
    anchor = {"seq": 3, "turn": 1}
    frames = [
        driver.frames.image_gen_running_frame(anchor, "run-1", phase)
        for phase in ("refining", "queued", "generating", "downloading")
    ]
    seqs = [f["seq"] for f in frames]
    assert seqs == sorted(seqs), "the ladder is not strictly increasing"
    assert all(3.5 < s < 3.6 for s in seqs), seqs
    for frame, phase in zip(frames, ("refining", "queued", "generating", "downloading")):
        assert frame["type"] == "lore/image-gen"
        assert frame["data"] == {
            "turn": 1, "runId": "run-1", "status": "running", "phase": phase,
        }
        assert frame["ignorable"] is True
    # An unknown phase still appends below the settled offset.
    unknown = driver.frames.image_gen_running_frame(anchor, "run-1", "encoding")
    assert 3.5 < unknown["seq"] < 3.6 and unknown["seq"] > seqs[-1]
    # No anchor seq (the launcher's resolve missed) — no frame at all.
    assert driver.frames.image_gen_running_frame({}, "run-1", "refining") is None
    assert driver.frames.image_gen_running_frame(
        {"seq": "3", "turn": 1}, "run-1", "refining") is None


def test_settled_frame_without_an_anchor_is_none():
    """A run whose anchor never resolved (payload anchor None) pushes no
    settled card — a reload still shows it from the row's gen_steps."""
    assert driver.frames.image_gen_settled_frame({}, _gen_steps(), "run-1") is None


async def test_reload_compaction_mint_rides_its_frame(monkeypatch):
    async def fake_outcome(session_id, compaction_id):
        assert (session_id, compaction_id) == ("src-1", "cpt-9")
        return False, None

    monkeypatch.setattr(driver.frames, "_compaction_mint_outcome", fake_outcome)
    cpt = _dsh(7, "compaction/end", {"compactionId": "cpt-9", "turn": 2})
    row = {"message_id": "m1", "created_at": "1", "frames": [cpt]}
    await driver.frames.attach_reload_lore_mints([row], chain={"m1"}, tail_seq=9, session_id="src-1")
    mint = row["frames"][1]
    assert mint["type"] == "lore/compaction-mint"
    assert mint["seq"] == 7.8
    assert mint["data"] == {"turn": 2, "compactionEntryId": "cpt-9", "mintFailed": False}


async def test_reload_compaction_mint_failure_is_reported(monkeypatch):
    async def fake_outcome(session_id, compaction_id):
        return True, "source_chat_missing"

    monkeypatch.setattr(driver.frames, "_compaction_mint_outcome", fake_outcome)
    cpt = _dsh(7, "compaction/end", {"compactionId": "cpt-9", "turn": 2})
    row = {"message_id": "m1", "created_at": "1", "frames": [cpt]}
    await driver.frames.attach_reload_lore_mints([row], chain={"m1"}, tail_seq=9, session_id="src-1")
    assert row["frames"][1]["data"]["mintFailed"] is True
    assert row["frames"][1]["data"]["mintReason"] == "source_chat_missing"


async def test_reload_compaction_skips_failed_and_malformed_frames(monkeypatch):
    async def boom(session_id, compaction_id):  # pragma: no cover — must not be called
        raise AssertionError("no outcome should be minted for these frames")

    monkeypatch.setattr(driver.frames, "_compaction_mint_outcome", boom)
    row = {"message_id": "m1", "created_at": "1", "frames": [
        _dsh(7, "compaction/end", {"compactionId": "c", "error": "compaction failed"}),
        _dsh(8, "compaction/end", {"compactionId": ""}),
        _dsh(9, "compaction/end", None),
        _dsh(None, "compaction/end", {"compactionId": "c2"}),
    ]}
    await driver.frames.attach_reload_lore_mints([row], chain={"m1"}, tail_seq=9, session_id="s")
    assert not [f for f in row["frames"] if f.get("type") == "lore/compaction-mint"]


# ─── the result-shape readers shared with the live arms ──────────────────────


def test_result_call_id_reads_the_source_only():
    from_source = {"message": {"role": "tool", "source": {"kind": "tool", "callId": "src-1"},
                               "content": []}}
    from_block = {"message": {"content": [{"type": "tool-result", "toolCallId": "blk-1"}]}}
    assert driver.frames._result_call_id(from_source) == "src-1"
    assert driver.frames._result_call_id(from_block) == ""
    assert driver.frames._result_call_id({}) == ""
