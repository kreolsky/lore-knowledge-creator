"""PR5 R6: the inline Agent-turn preparation inside `completions.stream_sse` is
extracted into a testable `prepare_agent_turn()` returning an `AgentTurnPlan` DTO.
The SSE layer then just iterates `stream_agent_turn` with the plan's fields.

Unit-tested without SSE: the prompt assembly (context injection + current-doc
section), apply-mode resolution, and message/tool wiring are all verifiable
directly from the DTO.

The turn-time stamp suite (send stamp every turn, `[chat started ...]` anchor
on root turns) lives here too — the stamps ride `plan.time_stamps` and reach
the model as their OWN `lore-time` context message, never the prompt (see
`_turn_time_stamps` in completions_turn; the plugin's time-stamps.ts emits the
message).
"""
import inspect
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# [sent 2026-09-01T23:14:05+03:00] — ISO 8601, seconds, explicit offset.
SENT_STAMP_RE = re.compile(
    r"\[sent \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\]$"
)


def _fake_body(*, messages=None, auto_apply=False, parent_id=None, model=None):
    # region mirrors CompletionBody.region (default None — non-pinned session);
    # context_ids mirrors CompletionRequest.context_ids (the agent Parent-line gate).
    return SimpleNamespace(
        messages=messages or [], auto_apply=auto_apply, region=None,
        context_ids=None, open_doc_id=None, parent_id=parent_id, model=model,
    )


def _fake_ctx(system_prefix=None):
    return SimpleNamespace(system_prefix=system_prefix or [], sources=[], warnings=[])


async def _run_turn(
    *, session=None, body=None, ctx=None, user_row=None, fetch_raises=False,
):
    """prepare_agent_turn with DB/prompt internals patched; `fetch_one` stands
    in for the per-turn users-row read (timezone resolution)."""
    from routes.chat.completions import prepare_agent_turn

    fetch = (
        AsyncMock(side_effect=RuntimeError("db down"))
        if fetch_raises else AsyncMock(return_value=user_row)
    )
    with patch(
        "routes.chat.completions_turn.build_prompt_and_skill_docs",
        new=AsyncMock(return_value=("BASE_PROMPT", {})),
    ), patch(
        "routes.chat.completions_turn._build_current_document_section",
        new=AsyncMock(return_value=None),
    ), patch(
        "routes.chat.completions_turn.fetch_one", new=fetch,
    ):
        return await prepare_agent_turn(
            session=session if session is not None else {"document_id": "d1"},
            body=body, ctx=ctx or _fake_ctx(), project_id="p1",
            user={"user_id": "u1"}, toolset=[], capability_is_mutating=True,
        )


async def test_prepare_agent_turn_assembles_prompt_and_apply_mode():
    """Context text + current-doc section are appended after the cacheable base;
    apply_mode resolves via apply_policy; messages/tools pass through."""
    from routes.chat.completions import AgentTurnPlan, prepare_agent_turn

    body = _fake_body(
        messages=[SimpleNamespace(role="user", content="hi", images=None)],
        auto_apply=True,
    )
    ctx = _fake_ctx(system_prefix=[{"role": "system", "content": "CTX"}])

    with patch(
        "routes.chat.completions_turn.build_prompt_and_skill_docs",
        new=AsyncMock(return_value=("BASE_PROMPT", {})),
    ), patch(
        "routes.chat.completions_turn._build_current_document_section",
        new=AsyncMock(return_value="# Current document\nDocX"),
    ), patch(
        "routes.chat.completions_turn.fetch_one", new=AsyncMock(return_value=None),
    ):
        plan = await prepare_agent_turn(
            session={"document_id": "d1"}, body=body, ctx=ctx, project_id="p1",
            user={"user_id": "u1"}, toolset=[{"function": {"name": "edit_document"}}],
            capability_is_mutating=True,
        )

    assert isinstance(plan, AgentTurnPlan)
    # The cacheable base leads; every turn-varying block trails it, most volatile
    # last. Asserted as an ORDER over the assembled string, not as a prefix check:
    # the point of the ordering is what precedes what.
    assert plan.system_prompt.startswith("BASE_PROMPT")
    assert (
        plan.system_prompt.index("BASE_PROMPT")
        < plan.system_prompt.index("CTX")
        < plan.system_prompt.index("# Current document")
    )
    assert plan.system_prompt.endswith("# Current document\nDocX")
    # Full access, no debug, opted in via auto_apply → auto.
    assert plan.apply_mode == "auto"
    # Stage 9: only the LAST user turn travels as `prompt` (no history replay).
    # The prompt is the RAW user text — the time ground rides plan.time_stamps
    # (pinned by the stamp suite below).
    assert plan.prompt == "hi"
    assert plan.tools == [{"function": {"name": "edit_document"}}]


async def test_prepare_agent_turn_truncates_oversized_context():
    """Context beyond CHAT_MAX_AGENT_CONTEXT_CHARS is truncated with a marker."""
    from routes.chat import completions as C

    body = _fake_body()
    ctx = _fake_ctx(system_prefix=[{"role": "system", "content": "X" * 100000}])

    with patch(
        "routes.chat.completions_turn.build_prompt_and_skill_docs",
        new=AsyncMock(return_value=("BASE", [])),
    ), patch(
        "routes.chat.completions_turn._build_current_document_section",
        new=AsyncMock(return_value=None),
    ), patch(
        "routes.chat.completions_turn.fetch_one", new=AsyncMock(return_value=None),
    ):
        plan = await C.prepare_agent_turn(
            session={"document_id": "d1"}, body=body, ctx=ctx, project_id="p1",
            user={"user_id": "u1"}, toolset=[], capability_is_mutating=True,
        )
    assert "[…context truncated]" in plan.system_prompt
    # editor access → confirm.
    assert plan.apply_mode == "confirm"


def test_stream_sse_agent_branch_is_thin():
    """After extraction, the completion branch delegates to prepare_agent_turn
    and iterates the plan — no inline prompt assembly."""
    from routes.chat import completions as C

    src = inspect.getsource(C.create_completion)
    assert "prepare_agent_turn" in src, (
        "the completion branch must delegate to prepare_agent_turn"
    )
    # Stage 9 → part B: the dispatch resolves the line —
    # nothing after that resolution may re-derive context.
    branch = src[src.index("resolve_driver_line"):]
    assert "CHAT_MAX_AGENT_CONTEXT_CHARS" not in branch, (
        "context truncation logic moved into prepare_agent_turn"
    )


# ─── Turn time ground: send stamp + session-start anchor ─────────────────────


async def test_send_stamp_rides_time_stamps_not_the_prompt():
    """Every turn: the prompt is the raw user text; the send stamp rides
    plan.time_stamps — user-tz ISO 8601, seconds, explicit offset."""
    body = _fake_body(
        messages=[SimpleNamespace(role="user", content="hello", images=None)],
    )
    plan = await _run_turn(body=body, user_row={"timezone": "Europe/Moscow"})
    assert plan.prompt == "hello"
    assert len(plan.time_stamps) == 1, plan.time_stamps
    assert SENT_STAMP_RE.search(plan.time_stamps[0]), plan.time_stamps


async def test_multimodal_prompt_is_raw_and_attachments_stay_last():
    """Multimodal turns: parts[0].text is the raw user text (no stamp bytes);
    attachments stay LAST (the ctx-image merge order is untouched); the
    stamps ride plan.time_stamps."""
    body = _fake_body(
        messages=[SimpleNamespace(
            role="user", content="describe this",
            images=["data:image/png;base64,AAAA"],
        )],
        model="local/orange/chat",
    )
    plan = await _run_turn(body=body, user_row={"timezone": "Europe/Moscow"})
    assert isinstance(plan.prompt, list)
    assert plan.prompt[0]["type"] == "text"
    assert plan.prompt[0]["text"] == "describe this"
    assert [p["type"] for p in plan.prompt[1:]] == ["image_url"], plan.prompt
    assert len(plan.time_stamps) == 1
    assert SENT_STAMP_RE.search(plan.time_stamps[0])


async def test_root_turn_anchor_from_session_created_at_in_user_tz():
    """Root turn (parent None, no compacted_from): `[chat started ...]`
    precedes `[sent ...]`, rendered from session.created_at in the user tz."""
    body = _fake_body(
        messages=[SimpleNamespace(role="user", content="hi", images=None)],
    )
    session = {
        "document_id": "d1",
        "created_at": datetime(2026, 8, 31, 15, 5, 0, tzinfo=timezone.utc),
    }
    plan = await _run_turn(
        session=session, body=body, user_row={"timezone": "Europe/Moscow"},
    )
    assert plan.prompt == "hi"
    assert len(plan.time_stamps) == 2, plan.time_stamps
    assert plan.time_stamps[0] == "[chat started 2026-08-31T18:05:00+03:00]"
    assert SENT_STAMP_RE.search(plan.time_stamps[1])


async def test_non_root_turn_gets_no_anchor():
    """parent set → not a root turn: send stamp only, no `[chat started`."""
    body = _fake_body(
        messages=[SimpleNamespace(role="user", content="hi", images=None)],
        parent_id="msg_1",
    )
    session = {
        "document_id": "d1",
        "created_at": datetime(2026, 8, 31, 15, 5, 0, tzinfo=timezone.utc),
    }
    plan = await _run_turn(
        session=session, body=body, user_row={"timezone": "Europe/Moscow"},
    )
    assert "[chat started" not in "".join(plan.time_stamps)
    assert len(plan.time_stamps) == 1
    assert SENT_STAMP_RE.search(plan.time_stamps[0])


async def test_compaction_continuation_root_gets_no_anchor():
    """parent None BUT compacted_from set — the leaf sits on the compaction
    checkpoint; appending an anchor would mis-date the continued chat."""
    body = _fake_body(
        messages=[SimpleNamespace(role="user", content="hi", images=None)],
    )
    session = {
        "document_id": "d1", "compacted_from": "src-1",
        "created_at": datetime(2026, 8, 31, 15, 5, 0, tzinfo=timezone.utc),
    }
    plan = await _run_turn(
        session=session, body=body, user_row={"timezone": "Europe/Moscow"},
    )
    assert "[chat started" not in "".join(plan.time_stamps)
    assert len(plan.time_stamps) == 1
    assert SENT_STAMP_RE.search(plan.time_stamps[0])


async def test_root_fork_anchor_bytes_are_stable():
    """A root fork re-sends byte-identical anchor bytes: the anchor renders
    from chat_sessions.created_at, never from turn time. The str shape of
    created_at covers the ISO-string parse path."""
    session = {"document_id": "d1", "created_at": "2026-08-31T15:05:00+00:00"}
    anchors = []
    for _ in range(2):
        body = _fake_body(
            messages=[SimpleNamespace(role="user", content="q", images=None)],
        )
        plan = await _run_turn(
            session=session, body=body, user_row={"timezone": "Europe/Moscow"},
        )
        assert plan.prompt == "q"
        assert len(plan.time_stamps) == 2, plan.time_stamps
        anchors.append(plan.time_stamps[0])
    assert anchors[0] == anchors[1] == "[chat started 2026-08-31T18:05:00+03:00]"


async def test_tz_null_invalid_and_fetch_failure_fall_back_to_utc(caplog):
    """users.timezone NULL / garbage / fetch raises → stamp in UTC (+00:00),
    no raise, exactly ONE warning per turn."""
    body = _fake_body(
        messages=[SimpleNamespace(role="user", content="hi", images=None)],
    )
    cases = [
        {"user_row": {"timezone": None}},
        {"user_row": {"timezone": "Mars/Olympus_Mons"}},
        {"fetch_raises": True},
    ]
    for case in cases:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="routes.chat.completions_turn"):
            plan = await _run_turn(body=body, **case)
        assert len(plan.time_stamps) == 1, plan.time_stamps
        assert SENT_STAMP_RE.search(plan.time_stamps[0]), plan.time_stamps
        assert plan.time_stamps[0].endswith("+00:00]"), plan.time_stamps
        warnings = [r for r in caplog.records if "timezone" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


async def test_images_only_turn_prompt_has_empty_text_and_stamps_survive():
    """Images-only turn (content ""): the text part is empty and the stamps
    still ride time_stamps; attachments stay last."""
    body = _fake_body(
        messages=[SimpleNamespace(
            role="user", content="", images=["data:image/png;base64,AAAA"],
        )],
        model="local/orange/chat",
    )
    plan = await _run_turn(body=body, user_row={"timezone": "Europe/Moscow"})
    assert isinstance(plan.prompt, list)
    text_part = plan.prompt[0]
    assert text_part["type"] == "text"
    assert text_part["text"] == ""
    assert [p["type"] for p in plan.prompt[1:]] == ["image_url"]
    assert len(plan.time_stamps) == 1
    assert SENT_STAMP_RE.search(plan.time_stamps[0])


async def test_prompt_stays_raw_and_source_message_object_untouched():
    """The DB row content stays raw AND the delivered prompt equals it: no
    stamp is glued onto the user's text, and body.messages[-1].content is
    never mutated in place."""
    msg = SimpleNamespace(role="user", content="hello", images=None)
    body = _fake_body(messages=[msg])
    plan = await _run_turn(body=body, user_row={"timezone": "Europe/Moscow"})
    assert plan.prompt == "hello"
    assert SENT_STAMP_RE.search(plan.time_stamps[0])
    assert msg.content == "hello"
    assert body.messages[-1].content == "hello"


async def test_build_last_user_prompt_returns_raw_content():
    """The prompt builder's contract is the RAW last user content — the
    stamp channel is plan.time_stamps, not the prompt."""
    from routes.chat.completions_turn import _build_last_user_prompt

    msg = SimpleNamespace(role="user", content="x", images=None)
    out = await _build_last_user_prompt([msg])
    assert out == "x"
    assert msg.content == "x"
