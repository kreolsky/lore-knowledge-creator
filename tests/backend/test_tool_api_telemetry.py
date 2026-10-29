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


# ─── Task 5: @track_agent_tool integration tests ─────────────────────────────


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
