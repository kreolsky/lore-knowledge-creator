"""Tests for manual context sources — verifies that manually-added
documents/references appear on the assistant row's persisted sources with
retrieved=False, are deduplicated against retrieval hits, and that
system_prompt_id is excluded.

The turn is driver-owned (plan agent-line-harness-lifecycle step 9 retired
the SSE arm): every completion is driven through the standing channel (the
harness_env recipe — fake connector/replay + post_followup at the module's
own seams, the relay arms persisting to the REAL test DB). The sources the
retired SSE preamble streamed are asserted where they live now: the assistant
row (the preamble frame carries the same list on the project WS).
"""
import pytest
from test_driver_channel import _env, _turn_end
from test_harness_turn import _acquirable, _list_messages, _until_async


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
    recipe): POST the completion (JSON accept), push the driver's frames
    through the fake channel socket — model_update opens the bound turn,
    turn/end closes it — and settle on the turn lock's release, which the
    channel fires only AFTER the close's finalize persist ran. Manual context
    sources ride the bound turn's projection onto the assistant row; the
    tests assert the row.

    `turn_end_seq` must EXCEED every seq the session's channel already
    delivered — the channel's seq-anchored dedup silently drops a replayed
    seq, the turn (and its lock) would never close."""
    # The linear-turn contract (plan chat-branch-sessions): a turn's parent
    # names the session's live TAIL — read it off the message list the way
    # the client does (an empty session posts the genuine first turn).
    msgs = await _list_messages(client, token, sid)
    if msgs:
        body = {**body, "parent_id": msgs[-1]["message_id"]}
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=body,
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True
    sock = env.connector.sockets[0]
    for frame in (
        {"type": "model_update", "model": "test"},
        _turn_end(turn_end_seq, "completed"),
    ):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _acquirable(sid))


async def _assistant_sources(client, token, sid) -> list[dict]:
    """The LAST assistant row's persisted sources panel."""
    msgs = await _list_messages(client, token, sid)
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    return assistant.get("sources") or []


async def _create_doc(client, token, pid, *, content="Doc body", title="Doc title"):
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


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


async def _create_session(client, token, pid, document_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": document_id, "model": "test"},
        cookies={"lore_session": token},
    )
    return resp.json()["session_id"]


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


async def test_context_doc_in_sources(
    client, admin_user, project_with_doc, harness_env,
):
    """Manual context document lands on the row's sources with retrieved=False."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    extra_doc = await _create_doc(client, token, pid, content="Manual context.", title="ManualDoc")
    sid = await _create_session(client, token, pid, doc_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [extra_doc],
    })
    sources = await _assistant_sources(client, token, sid)
    assert len(sources) >= 1
    manual = [s for s in sources if s["id"] == extra_doc]
    assert len(manual) == 1
    assert manual[0]["retrieved"] is False
    assert manual[0]["kind"] == "document"
    assert manual[0]["title"] == "ManualDoc"


async def test_context_ref_in_sources(
    client, admin_user, project_with_doc, harness_env,
):
    """Manual context reference lands on the row's sources with
    kind=reference, retrieved=False."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id, content="Manual ref.", title="ManualRef")
    sid = await _create_session(client, token, pid, doc_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [ref_id],
    })
    sources = await _assistant_sources(client, token, sid)
    manual = [s for s in sources if s["id"] == ref_id]
    assert len(manual) == 1
    assert manual[0]["retrieved"] is False
    assert manual[0]["kind"] == "reference"


async def test_primary_doc_in_sources_only_when_in_context(
    client, admin_user, project_with_doc, harness_env,
):
    """Primary doc in sources only when present in context_ids."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "Primary body"},
        cookies={"lore_session": token},
    )
    sid = await _create_session(client, token, pid, doc_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [doc_id],
    })
    sources = await _assistant_sources(client, token, sid)
    assert any(s["id"] == doc_id for s in sources)

    # Second turn on the same session: its turn_end seq must clear the first
    # turn's (the channel's seq-anchored dedup would drop a replayed seq).
    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [],
    }, turn_end_seq=2)
    sources2 = await _assistant_sources(client, token, sid)
    assert not any(s["id"] == doc_id for s in sources2)


async def test_dedup_manual_overrides_retrieval(
    client, admin_user, project_with_doc, harness_env,
):
    """Doc in context_ids appears once as a manual source (retrieved=False).

    Note: forced per-turn semantic injection was removed (plan §4) — search is now
    the agent's on-demand search_materials tool. A manual context entry still wins
    and is the single source row for that id."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    extra_doc = await _create_doc(client, token, pid, content="Overlap content.", title="OverlapDoc")
    sid = await _create_session(client, token, pid, doc_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [extra_doc],
    })
    sources = await _assistant_sources(client, token, sid)
    matching = [s for s in sources if s["id"] == extra_doc]
    assert len(matching) == 1
    assert matching[0]["retrieved"] is False


async def test_system_prompt_excluded_from_sources(
    client, admin_user, project_with_doc, harness_env,
):
    """system_prompt_id does NOT appear in sources."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    prompt_doc = await _create_doc(client, token, pid, content="System instructions.", title="SysPrompt")
    sid = await _create_session(client, token, pid, doc_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "system_prompt_id": prompt_doc,
        "context_ids": [],
    })
    sources = await _assistant_sources(client, token, sid)
    assert not any(s["id"] == prompt_doc for s in sources)


# ─── Agent-mode target source is opt-in ─────────────────────────────────────
#
# INVARIANT: sources record only documents whose content was actually injected
# into the prompt (i.e. present in context_ids). The agent target is NOT added as
# a source on its own — only when the user explicitly attaches it via
# context_ids. The target's title anchor ("# Current document" in the prompt)
# still informs the agent of its working document; it is simply not rendered as
# attached context.


async def test_agent_target_absent_with_empty_context(
    client, admin_user, project_with_doc, harness_env,
):
    """Agent-mode completion with target_doc_id set and EMPTY context_ids →
    the target doc is NOT recorded as a source (its content is not injected)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    target = await _create_doc(client, token, pid, content="Hello world body", title="AgentTarget")
    sid = await _create_agent_session(client, token, pid, target_doc_id=target, document_id=target)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Rewrite this"}],
        "context_ids": [],
        "selection": {"doc_id": target, "from_cp": 0, "to_cp": 5, "original_text": "Hello", "version": 1},
    })
    sources = await _assistant_sources(client, token, sid)
    target_sources = [s for s in sources if s["id"] == target]
    assert target_sources == [], f"target leaked into sources with empty context: {target_sources}"


async def test_agent_target_deduped_when_also_in_context(
    client, admin_user, project_with_doc, harness_env,
):
    """Agent target also present in context_ids → only one source row (no duplicate)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    target = await _create_doc(client, token, pid, content="Hello world body", title="AgentTarget")
    sid = await _create_agent_session(client, token, pid, target_doc_id=target, document_id=target)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Rewrite this"}],
        "context_ids": [target],
        "selection": {"doc_id": target, "from_cp": 0, "to_cp": 5, "original_text": "Hello", "version": 1},
    })
    sources = await _assistant_sources(client, token, sid)
    target_sources = [s for s in sources if s["id"] == target]
    assert len(target_sources) == 1


async def test_agent_target_absent_when_other_doc_in_context(
    client, admin_user, project_with_doc, harness_env,
):
    """Agent session with a manual context doc X but target NOT in context_ids →
    sources contain X only; the target is absent (the manual-context path still
    works while the target is dropped)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    target = await _create_doc(client, token, pid, content="Hello world body", title="AgentTarget")
    other = await _create_doc(client, token, pid, content="Other body", title="OtherDoc")
    sid = await _create_agent_session(client, token, pid, target_doc_id=target, document_id=target)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [other],
    })
    sources = await _assistant_sources(client, token, sid)
    assert [s for s in sources if s["id"] == other], "manual context doc missing"
    assert not [s for s in sources if s["id"] == target], "target leaked while other doc in context"


async def test_non_agent_completion_has_no_target_source(
    client, admin_user, project_with_doc, harness_env,
):
    """Negative: non-agent completion is unchanged — no spurious target source injected."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    # A normal (non-agent) session opened on doc_id.
    sid = await _create_session(client, token, pid, doc_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [],
    })
    sources = await _assistant_sources(client, token, sid)
    # No sources at all when nothing is in context and retrieval is off.
    assert not any(s["id"] == doc_id for s in sources)


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


async def test_agent_reference_target_absent_with_empty_context(
    client, admin_user, project_with_doc, harness_env,
):
    """Sibling: ref target with EMPTY context_ids → ref NOT in sources
    (content not injected → no source row)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id, content="Ref body content", title="AgentRefTarget")
    sid = await _create_agent_session(client, token, pid, target_doc_id=ref_id, document_id=ref_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Rewrite this"}],
        "context_ids": [],
        "selection": {"doc_id": ref_id, "from_cp": 0, "to_cp": 3, "original_text": "Ref", "version": 1},
    })
    sources = await _assistant_sources(client, token, sid)
    assert not [s for s in sources if s["id"] == ref_id]


async def test_manual_sources_persist_on_message(
    client, admin_user, project_with_doc, harness_env,
):
    """Regression (f6dc9fe): manual context sources must be PERSISTED on the
    assistant message row, not only relayed live — so a reload (GET /messages)
    still shows the Sources panel."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    extra_doc = await _create_doc(client, token, pid, content="Persist me.", title="PersistDoc")
    sid = await _create_session(client, token, pid, doc_id)

    await _drive_turn(client, token, sid, harness_env, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [extra_doc],
    })

    msgs = await _list_messages(client, token, sid)
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert assistant, "no assistant message persisted"
    persisted = assistant[-1].get("sources") or []
    manual = [s for s in persisted if s["id"] == extra_doc]
    assert len(manual) == 1, f"manual source not persisted: {persisted}"
    assert manual[0]["retrieved"] is False
