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


# ─── _TurnProjection: the accumulation ───────────────────────────────────────


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


async def test_turn_start_sets_the_turn_coordinate_and_warns_without_one(caplog):
    turn = _turn()
    await _relay(turn, _dsh(1, "turn/start", {"turn": 3}))
    assert turn.turn_no == 3
    await _relay(turn, _dsh(9, "turn/start", {}))
    assert turn.turn_no == 3  # a missing number never clobbers the open turn


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


# ─── the compaction mint (window-keyed, best-effort) ─────────────────────────


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


async def test_compaction_end_malformed_or_failed_compaction_mints_nothing():
    turn = _turn()
    frames = await _relay(turn, _dsh(7, "compaction/end", {"compactionId": "", "turn": 1}))
    assert len(_parse(frames)) == 1
    frames = await _relay(turn, _dsh(8, "compaction/end", {"compactionId": "c", "error": "boom"}))
    assert len(_parse(frames)) == 1
    frames = await _relay(turn, _dsh(9, "compaction/end", None))
    assert len(_parse(frames)) == 1


# ─── the reload attach mints ──────────────────────────────────────────────────


def _result_frame(seq, call_id, text):
    """A settled v4 tool/result: a FLAT tool message (role 'tool')."""
    return _dsh(seq, "tool/result", {"message": {
        "role": "tool", "source": {"kind": "tool", "callId": call_id},
        "toolCallId": call_id, "content": [{"type": "text", "text": text}]}})


async def test_reload_halt_mints_at_the_stored_anchor_with_its_turn():
    """A halt written with the live mint's anchor lands where the live card
    stood — inside its own turn, carrying the turn coordinate."""
    row = {"message_id": "m-ab", "created_at": "1",
           "halt": {"reason": "turn_timeout", "steps": 3, "anchor_seq": 7, "turn": 2}}
    await driver.frames.attach_reload_lore_mints(
        [row], tail_seq=42, session_id="chat-1")
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
        [old, new], tail_seq=30, session_id="chat-1")
    assert old["frames"][0]["seq"] == 4.7
    assert old["frames"][0]["data"]["turn"] == 1
    assert new["frames"][0]["seq"] == 9.7


async def test_reload_halt_legacy_rows_keep_the_newest_only_tail():
    """Rows halted before the anchor was stored have no position but the log
    tail — so exactly one of them, the newest, still mints there."""
    old = {"message_id": "m-old", "created_at": "1", "halt": {"reason": "disconnected"}}
    new = {"message_id": "m-new", "created_at": "2", "halt": {"reason": "turn_timeout"}}
    await driver.frames.attach_reload_lore_mints(
        [old, new], tail_seq=30, session_id="chat-1")
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
            [anchored, legacy], tail_seq=30, session_id="chat-1")
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
            [anchored, twin], tail_seq=99, session_id="chat-1")
    assert anchored["frames"][0]["seq"] == 5.7
    assert twin.get("frames") is None
    assert "m-b" in caplog.text


async def test_reload_image_gen_step_without_call_id_mints_nothing(monkeypatch, caplog):
    """A step persisted without the dispatching call id has no anchor — even
    when a call and a result naming its run id are both in the replay, the
    result body is never parsed for one."""
    _stub_live_refs(monkeypatch)
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
    await driver.frames.attach_reload_lore_mints([row], tail_seq=4, session_id="c")
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


def _stub_live_refs(monkeypatch, live=None, *, boom=False, seen=None):
    """The ONE seam of the reload mint's deleted-image filter: the live-ref
    query (`driver.frames._live_image_ref_ids`). Default (`live=None`) echoes
    every id as live; `boom` simulates the query failing; `seen` records each
    page's queried ids."""
    async def _live(ids):
        if seen is not None:
            seen.append(list(ids))
        if boom:
            raise RuntimeError("db down")
        return set(ids) if live is None else live
    monkeypatch.setattr(driver.frames, "_live_image_ref_ids", _live)


def _row_with_image_run(ref_ids, *, call_id="call-gen"):
    """One assistant row holding a settled image run over `ref_ids`, with the
    dispatching call in its frames (the mint's anchor)."""
    return {
        "message_id": "m1", "created_at": "1",
        "frames": [
            _dsh(1, "turn/start", {"turn": 1}),
            _dsh(3, "tool/call", {"turn": 1, "step": 2, "callId": call_id,
                                  "name": "generate_image", "arguments": "{}"}),
        ],
        "gen_steps": [{"tool": "generate_image", "run_id": "run-1",
                       "call_id": call_id, "image_ref_ids": list(ref_ids)}],
    }


async def test_reload_mint_live_query_failure_keeps_ids_verbatim(monkeypatch, caplog):
    """The live-ref query failing must not blank the page's images — the mint
    degrades to today's verbatim ids, and the miss is logged, never silent."""
    row = _row_with_image_run(["ref-1", "ref-2"])
    _stub_live_refs(monkeypatch, boom=True)
    with caplog.at_level("WARNING"):
        await driver.frames.attach_reload_lore_mints(
            [row], tail_seq=9, session_id="c")
    mint = next(f for f in row["frames"] if f["type"] == "lore/image-gen")
    assert mint["data"]["imageRefIds"] == ["ref-1", "ref-2"]
    assert "deletedByUser" not in mint["data"]


async def test_resolve_without_a_call_id_returns_none(monkeypatch):
    """No dispatching call id — no anchor. The replay is never even read (the
    stub forbids the read)."""
    _stub_replay(monkeypatch, forbidden=True)
    assert await driver.frames.resolve_image_gen_anchor("src-1", "") is None


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


async def test_reload_compaction_mint_rides_its_frame(monkeypatch):
    async def fake_outcome(session_id, compaction_id):
        assert (session_id, compaction_id) == ("src-1", "cpt-9")
        return False, None

    monkeypatch.setattr(driver.frames, "_compaction_mint_outcome", fake_outcome)
    cpt = _dsh(7, "compaction/end", {"compactionId": "cpt-9", "turn": 2})
    row = {"message_id": "m1", "created_at": "1", "frames": [cpt]}
    await driver.frames.attach_reload_lore_mints([row], tail_seq=9, session_id="src-1")
    mint = row["frames"][1]
    assert mint["type"] == "lore/compaction-mint"
    assert mint["seq"] == 7.8
    assert mint["data"] == {"turn": 2, "compactionEntryId": "cpt-9", "mintFailed": False}


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
    await driver.frames.attach_reload_lore_mints([row], tail_seq=9, session_id="s")
    assert not [f for f in row["frames"] if f.get("type") == "lore/compaction-mint"]


# ─── the result-shape readers shared with the live arms ──────────────────────


def test_result_call_id_reads_the_source_only():
    from_source = {"message": {"role": "tool", "source": {"kind": "tool", "callId": "src-1"},
                               "content": []}}
    from_block = {"message": {"content": [{"type": "tool-result", "toolCallId": "blk-1"}]}}
    assert driver.frames._result_call_id(from_source) == "src-1"
    assert driver.frames._result_call_id(from_block) == ""
    assert driver.frames._result_call_id({}) == ""


async def test_live_image_ref_ids_against_the_real_delete(client, collab_ref_project):
    """The live-ref query itself, over a real database and the real delete
    route: a reference deleted through DELETE /api/references/{id} drops out,
    a live one stays, an id with no row is not live. Every other filter test
    stubs this seam, so a wrong id shape here would turn every reloaded image
    run into the all-deleted note while they all stayed green."""
    import uuid

    pid, doc_id, deleted_ref, admin_token, *_ = collab_ref_project
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": doc_id, "title": "Kept Ref",
              "media_type": "markdown", "is_reference": True, "content": "x"},
        cookies={"lore_session": admin_token},
    )
    kept_ref = resp.json()["document_id"]
    resp = await client.delete(f"/api/references/{deleted_ref}",
                               cookies={"lore_session": admin_token})
    assert resp.status_code == 200, resp.text

    missing = str(uuid.uuid4())
    live = await driver.frames._live_image_ref_ids([deleted_ref, kept_ref, missing])
    assert live == {kept_ref}
