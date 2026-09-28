"""Tests for agent-tool usage telemetry (plan agent-tool-telemetry).

Covers:
- Task 4 unit tests: error_kind mapping, validator-feedback boundary, detail
  clamping, fire-and-forget swallowing.
- Task 3: get_agent_context reads X-Agent-Call-Id / X-Agent-Session-Id.
- Task 5 integration tests: the @track_agent_tool decorator records one row per
  call with the right outcome/error_kind/validator-feedback, propagates
  call_id/session_id, clamps >4KB args, and a telemetry-store failure never
  surfaces as an HTTP error (telemetry INVARIANT).
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets

import pytest

# ─── Test helpers (mirror test_tool_api.py) ──────────────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str, label: str = "agent") -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"tm-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": label,
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str, call_id: str = "call-1", session_id: str = "sess-1") -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Agent-Call-Id": call_id,
        "X-Agent-Session-Id": session_id,
    }


async def _make_doc(client, token: str, project_id: str, title: str, content: str = "") -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _fetch_tool_telemetry(test_db, call_id: str) -> list[dict]:
    # WHY: the @track_agent_tool decorator writes its row fire-and-forget (product
    # INVARIANT — telemetry must never block the tool call), so the HTTP response
    # returns before the background insert runs. On a busy loop (full suite / -n) the
    # query races ahead of the write and sees 0 rows. Drain the in-flight tasks first;
    # they are already in _pending_telemetry by the time client.post returns (spawned
    # in the wrapper's finally). return_exceptions preserves "telemetry never raises".
    from routes.tool_api_telemetry import _pending_telemetry
    if _pending_telemetry:
        await asyncio.gather(*list(_pending_telemetry), return_exceptions=True)
    return await test_db.query(
        "SELECT * FROM telemetry_event WHERE category = 'agent_tool' "
        "AND kind = 'call' AND detail.call_id = $cid",
        {"cid": call_id},
    )


# ─── Task 4: unit tests ───────────────────────────────────────────────────────


class _FakeBody:
    """Stand-in for a pydantic body model with a model_dump()."""

    def model_dump(self, exclude_none: bool = False) -> dict:  # noqa: ARG002
        return {"document_id": "d1", "old_string": "a", "new_string": "b", "apply": "auto"}


def test_error_kind_for_status_mapping():
    from routes.tool_api_telemetry import error_kind_for
    assert error_kind_for(404) == "not_found"
    assert error_kind_for(403) == "access_denied"
    assert error_kind_for(409) == "conflict"
    assert error_kind_for(422) == "validation"
    assert error_kind_for(429) == "rate_limit"
    assert error_kind_for(500) == "server_error"
    assert error_kind_for(502) == "server_error"
    # unmapped 4xx (e.g. 400) and None → None
    assert error_kind_for(400) is None
    assert error_kind_for(None) is None


def test_is_validator_feedback_boundary():
    from routes.tool_api_telemetry import is_validator_feedback
    # Self-correctable validator feedback: 404/409/422.
    assert is_validator_feedback("not_found") is True
    assert is_validator_feedback("conflict") is True
    assert is_validator_feedback("validation") is True
    # NOT self-correctable: 403/429/5xx.
    assert is_validator_feedback("access_denied") is False
    assert is_validator_feedback("rate_limit") is False
    assert is_validator_feedback("server_error") is False
    assert is_validator_feedback(None) is False


def test_build_detail_clamps_large_args():
    from routes.tool_api_telemetry import build_detail

    class _Big:
        def model_dump(self, exclude_none: bool = False) -> dict:  # noqa: ARG002
            return {"blob": "x" * 10_000}

    outcome = {"ok": True, "status": None, "detail": None, "result_status": "applied"}
    detail = build_detail(
        "edit_document", _Big(), outcome, latency_ms=12,
        ctx={"user_id": "u", "project_id": "p", "call_id": "c", "session_id": "s"},
    )
    # The row is kept, but the noisy args blob is replaced by the truncate marker.
    assert detail["args"] == {"_truncated": True}
    assert detail["tool"] == "edit_document"
    assert detail["outcome"] == "ok"
    assert detail["result_status"] == "applied"
    assert detail["call_id"] == "c"
    assert detail["session_id"] == "s"


def test_build_detail_error_path_fields():
    from routes.tool_api_telemetry import build_detail

    outcome = {
        "ok": False, "status": 409, "detail": "edit range error",
        "result_status": None,
    }
    detail = build_detail(
        "edit_document", _FakeBody(), outcome, latency_ms=5,
        ctx={"user_id": "u", "project_id": "p", "call_id": "c", "session_id": None},
    )
    assert detail["outcome"] == "error"
    assert detail["http_status"] == 409
    assert detail["error_kind"] == "conflict"
    assert detail["is_validator_feedback"] is True
    assert detail["error_detail"] == "edit range error"
    assert detail["result_status"] is None
    assert detail["session_id"] is None


def test_build_detail_carries_read_window_shape():
    """A read's window shape (`total_chars` + whether the answer was truncated)
    rides the SAME row as its `offset`/`limit` args, so "did the model page, and
    how far" is one query instead of a guess. A tool whose result has no window
    contributes neither key."""
    from routes.tool_api_telemetry import build_detail

    ctx = {"user_id": "u", "project_id": "p", "call_id": "c", "session_id": "s"}
    sliced = build_detail(
        "read_document", _FakeBody(),
        {"ok": True, "status": None, "detail": None, "result_status": None,
         "result_shape": {"total_chars": 161972, "truncated": True}},
        latency_ms=3, ctx=ctx,
    )
    assert sliced["total_chars"] == 161972
    assert sliced["truncated"] is True

    whole = build_detail(
        "read_document", _FakeBody(),
        {"ok": True, "status": None, "detail": None, "result_status": None,
         "result_shape": {"total_chars": 80, "truncated": False}},
        latency_ms=3, ctx=ctx,
    )
    assert whole["truncated"] is False

    shapeless = build_detail(
        "edit_document", _FakeBody(),
        {"ok": True, "status": None, "detail": None, "result_status": "applied"},
        latency_ms=3, ctx=ctx,
    )
    assert "total_chars" not in shapeless
    assert "truncated" not in shapeless


@pytest.mark.asyncio
async def test_decorator_records_read_window_shape(
    client, test_db, admin_user, project_with_doc,
):
    """The decorator derives the window shape from the handler's OWN result, so a
    truncated read is countable in telemetry without re-reading the document."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "Sliced", "A" * 80)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/read_document", json={"document_id": doc_id, "limit": 50},
        headers=_hdr(agent_tok, call_id="call-sliced"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["next_offset"] == 50, "guard: the call under test must truncate"

    rows = await _fetch_tool_telemetry(test_db, "call-sliced")
    assert len(rows) == 1
    d = rows[0]["detail"]
    assert d["tool"] == "read_document"
    assert d["total_chars"] == 80
    assert d["truncated"] is True
    # The window the model ASKED for rides the same row (args side).
    assert d["args"]["limit"] == 50

    resp = await client.post(
        "/api/tool/read_document", json={"document_id": doc_id},
        headers=_hdr(agent_tok, call_id="call-whole"),
    )
    assert resp.status_code == 200, resp.text
    rows = await _fetch_tool_telemetry(test_db, "call-whole")
    assert len(rows) == 1
    # SurrealDB strips false/null from flexible objects on some paths — .get keeps
    # the assertion about the VALUE, not about the storage representation.
    assert rows[0]["detail"].get("truncated") in (False, None)
    assert rows[0]["detail"]["total_chars"] == 80


@pytest.mark.asyncio
async def test_spawn_swallows_record_failure(monkeypatch):
    """A telemetry-store failure MUST NOT propagate (telemetry INVARIANT)."""
    from routes import tool_api_telemetry as mod

    async def _boom(_rows):
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "record_telemetry_events", _boom)

    # Should return promptly without raising; the exception is logged + swallowed.
    # record_agent_tool_call is SYNC (it schedules a fire-and-forget task) — do not await.
    mod.record_agent_tool_call(
        {"tool": "x"}, {"user_id": "u", "project_id": "p"},
    )
    # Drain the fire-and-forget task set so the swallowed exception has run.
    for _ in range(10):
        await asyncio.sleep(0)


# ─── Task 3: get_agent_context reads correlation headers ─────────────────────


@pytest.mark.asyncio
async def test_get_agent_context_reads_correlation_headers(test_db, admin_user, project_with_doc):
    from agent.context import get_agent_context

    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    ctx = await get_agent_context(
        authorization=f"Bearer {token}",
        x_agent_call_id="call-hdr-1",
        x_agent_session_id="sess-hdr-1",
    )
    assert ctx["call_id"] == "call-hdr-1"
    assert ctx["session_id"] == "sess-hdr-1"


@pytest.mark.asyncio
async def test_get_agent_context_headers_default_none(test_db, admin_user, project_with_doc):
    from agent.context import get_agent_context

    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    ctx = await get_agent_context(authorization=f"Bearer {token}")
    assert ctx.get("call_id") is None
    assert ctx.get("session_id") is None


# ─── Task 5: @track_agent_tool integration tests ─────────────────────────────


@pytest.mark.asyncio
async def test_decorator_success_records_one_row(
    client, test_db, admin_user, project_with_doc,
):
    """A successful mutating call writes exactly one agent_tool/call row."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "Src", "hello world")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "hello", "new_string": "hi",
              "apply": "auto"},
        headers=_hdr(agent_tok, call_id="call-ok"),
    )
    assert resp.status_code == 200, resp.text

    rows = await _fetch_tool_telemetry(test_db, "call-ok")
    assert len(rows) == 1
    d = rows[0]["detail"]
    assert d["tool"] == "edit_document"
    assert d["outcome"] == "ok"
    assert d["result_status"] == "applied"
    # error_kind/http_status are None on the ok path → SurrealDB strips nulls from
    # flexible objects, so the key may be absent (.get covers both null and missing).
    assert d.get("error_kind") is None
    assert d.get("http_status") is None
    assert d["is_validator_feedback"] is False
    assert isinstance(d["latency_ms"], int) and d["latency_ms"] >= 0
    assert d["call_id"] == "call-ok"
    assert d["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_decorator_404_validator_feedback(
    client, test_db, admin_user, project_with_doc,
):
    """A missing document → 404 → not_found validator feedback (self-correctable)."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/read_document",
        json={"document_id": "does-not-exist-xyz"},
        headers=_hdr(agent_tok, call_id="call-404"),
    )
    assert resp.status_code == 404

    rows = await _fetch_tool_telemetry(test_db, "call-404")
    assert len(rows) == 1
    d = rows[0]["detail"]
    assert d["outcome"] == "error"
    assert d["http_status"] == 404
    assert d["error_kind"] == "not_found"
    assert d["is_validator_feedback"] is True


@pytest.mark.asyncio
async def test_decorator_409_edit_range_conflict(
    client, test_db, admin_user, project_with_doc,
):
    """A bad old_string → 409 edit-range → conflict feedback.

    NOTE: apply=auto — the mid-turn gate holds a confirm-mode call with driver
    correlation headers BEFORE the handler runs (the verdict decides whether the
    handler is called at all), so edit-range validation no longer happens before
    confirmation. The 409 classification intent is what this test pins; the auto
    path produces the same edit-range error.
    """
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "Src", "alpha beta")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "missing", "new_string": "X",
              "apply": "auto"},
        headers=_hdr(agent_tok, call_id="call-409"),
    )
    assert resp.status_code == 409, resp.text

    rows = await _fetch_tool_telemetry(test_db, "call-409")
    assert len(rows) == 1
    d = rows[0]["detail"]
    assert d["error_kind"] == "conflict"
    assert d["is_validator_feedback"] is True


@pytest.mark.asyncio
async def test_decorator_422_validation(
    client, test_db, admin_user, project_with_doc,
):
    """A reference without parent_id (the host) → 422 → validation feedback."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/create_document",
        json={"title": "Orphan", "content": "x", "parent_id": None,
              "node_type": "reference", "apply": "auto"},
        headers=_hdr(agent_tok, call_id="call-422"),
    )
    assert resp.status_code == 422

    rows = await _fetch_tool_telemetry(test_db, "call-422")
    assert len(rows) == 1
    d = rows[0]["detail"]
    assert d["error_kind"] == "validation"
    assert d["is_validator_feedback"] is True


@pytest.mark.asyncio
async def test_decorator_403_not_validator_feedback(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """An access-denied edit (403) is NOT self-correctable → feedback False."""
    from db import create_record

    pid, _, admin_uid = project_with_doc
    user_uid, _ = regular_user
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "OwnerDoc", "secret content")
    # Give the regular user only 'readonly' access (no edit).
    await create_record("project_members", f"pm-{pid}-{user_uid}-view", {
        "project_id": pid, "user_id": user_uid, "access_level": "readonly",
    })
    # Mint the agent key for the VIEWER user.
    agent_tok = await _make_agent_key(test_db, user_uid, pid)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "secret", "new_string": "out",
              "apply": "auto"},
        headers=_hdr(agent_tok, call_id="call-403"),
    )
    assert resp.status_code == 403, resp.text

    rows = await _fetch_tool_telemetry(test_db, "call-403")
    assert len(rows) == 1
    d = rows[0]["detail"]
    assert d["error_kind"] == "access_denied"
    assert d["is_validator_feedback"] is False


@pytest.mark.asyncio
async def test_decorator_clamps_oversized_args(
    client, test_db, admin_user, project_with_doc,
):
    """args > 4KB → row kept, the LARGE value is dropped but SHORT scalars survive
    (plan failed-tool-calls-must-look-failed: the 36-char run_id that held a diagnosis
    previously vanished with the whole blob). `new_string` (10 KB) is dropped;
    `document_id` + `old_string` survive; the row is marked truncated."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "Src", "prefix anchor text here suffix")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "anchor text here",
              "new_string": "x" * 10_000, "apply": "auto"},
        headers=_hdr(agent_tok, call_id="call-big"),
    )
    assert resp.status_code == 200, resp.text

    rows = await _fetch_tool_telemetry(test_db, "call-big")
    assert len(rows) == 1
    args = rows[0]["detail"]["args"]
    assert args["_truncated"] is True
    # The large value is dropped (the noise)…
    assert "new_string" not in args
    # …the short scalars survive (the diagnosis fields).
    assert args.get("document_id") == doc_id
    assert args.get("old_string") == "anchor text here"


@pytest.mark.asyncio
async def test_decorator_telemetry_failure_does_not_break_request(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """If record_telemetry_events raises, the HTTP call still returns its result."""
    from routes import tool_api_telemetry as mod

    async def _boom(_rows):
        raise RuntimeError("telemetry store down")

    monkeypatch.setattr(mod, "record_telemetry_events", _boom)

    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "Src", "hello world")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "hello", "new_string": "hi",
              "apply": "auto"},
        headers=_hdr(agent_tok, call_id="call-swallow"),
    )
    # The tool call succeeds despite the telemetry-store failure.
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"


@pytest.mark.asyncio
async def test_decorator_bodyless_post_records_row(
    client, test_db, admin_user, project_with_doc,
):
    """get_project_structure (body optional) still records a telemetry row."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/get_project_structure",
        headers=_hdr(agent_tok, call_id="call-struct"),
    )
    assert resp.status_code == 200, resp.text

    rows = await _fetch_tool_telemetry(test_db, "call-struct")
    assert len(rows) == 1
    assert rows[0]["detail"]["tool"] == "get_project_structure"
    assert rows[0]["detail"]["outcome"] == "ok"
