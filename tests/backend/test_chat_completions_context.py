"""Tests for completions context handling — Spec §4.2/§5.

Stage 9: Pi is the only line; context documents fold into the composed
`system_prompt` (not a messages[] history). Verifies that the unified
context_ids array is split correctly by ref_map and that each kind receives the
right prompt formatting (document vs reference). The composed prompt is read
off the /followup payload — the driver-owned turn's one wire contract (the
retired SSE arm exposed the same string as stream kwargs).
"""

from test_driver_channel import _env, _turn_end
from test_harness_turn import _acquirable, _until_async


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


async def _create_doc(client, token, pid, *, content="Doc body", title="Doc title"):
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "content": content},
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


async def _completions(client, token, sid, body, env):
    """Start one driver-owned turn and return the composed `system_prompt` it
    was sent with (context documents fold into it, not a messages[] history).
    The string rides the /followup payload; the turn is then closed through
    the fake channel socket so the lock releases before the next test."""
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=body,
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True
    assert len(env.followups.payloads) == 1
    sock = env.connector.sockets[0]
    for frame in ({"type": "model_update", "model": "test"}, _turn_end(1)):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _acquirable(sid))
    return env.followups.payloads[0]["system_prompt"]


async def test_doc_ids_in_context_get_document_formatting(
    client, admin_user, project_with_doc, harness_env,
):
    """§4.2/§5: doc IDs in context_ids → a scope line (title + id), not the body."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    extra_doc = await _create_doc(client, token, pid, content="Dragons exist.", title="Lore")
    sid = await _create_session(client, token, pid, doc_id)

    joined = await _completions(client, token, sid, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [extra_doc],
    }, env=harness_env)
    assert "Lore" in joined
    assert extra_doc in joined
    assert "Dragons exist." not in joined


async def test_ref_ids_in_context_get_reference_formatting(
    client, admin_user, project_with_doc, harness_env,
):
    """§4.2/§5: ref IDs in context_ids → a scope line (title + id), not the body."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id, content="Sword facts.", title="SwordRef")
    sid = await _create_session(client, token, pid, doc_id)

    joined = await _completions(client, token, sid, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [ref_id],
    }, env=harness_env)
    assert "SwordRef" in joined
    assert ref_id in joined
    assert "Sword facts." not in joined


async def test_primary_doc_included_only_when_in_context_ids(
    client, admin_user, project_with_doc, harness_env,
):
    """§5 (nothing implicit): primary doc must be explicit in context_ids — and
    even then only its scope (title + id) rides, never the body."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"title": "Primary Scope", "content": "Primary body"},
        cookies={"lore_session": token},
    )
    sid = await _create_session(client, token, pid, doc_id)

    joined = await _completions(client, token, sid, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [doc_id],
    }, env=harness_env)
    assert 'pinned the document "Primary Scope"' in joined
    assert "Primary body" not in joined


async def test_primary_excluded_when_not_in_context_ids(
    client, admin_user, project_with_doc, harness_env,
):
    """§5: nothing implicit — empty context_ids means no pinned scope line. (The
    working doc still appears in the '# Current document' section; that is the
    session scope, not pinned context.)"""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"title": "Implicit Scope", "content": "Implicit body"},
        cookies={"lore_session": token},
    )
    sid = await _create_session(client, token, pid, doc_id)

    joined = await _completions(client, token, sid, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [],
    }, env=harness_env)
    assert "pinned the document" not in joined
    assert "Implicit body" not in joined


async def test_mixed_doc_ref_context_split_correctly(
    client, admin_user, project_with_doc, harness_env,
):
    """§3/§5: unified array correctly split into docs vs refs via ref_map."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    extra_doc = await _create_doc(client, token, pid, content="Doc content here.", title="DocXyz")
    ref_id = await _create_ref(client, token, pid, doc_id, content="Ref content here.", title="RefXyz")
    sid = await _create_session(client, token, pid, doc_id)

    joined = await _completions(client, token, sid, {
        "messages": [{"role": "user", "content": "Hi"}],
        "context_ids": [extra_doc, ref_id],
    }, env=harness_env)
    assert "DocXyz" in joined
    assert "RefXyz" in joined
    assert "Doc content here." not in joined
    assert "Ref content here." not in joined
