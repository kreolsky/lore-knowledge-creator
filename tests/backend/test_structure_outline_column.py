"""Plan document-outline-in-structure-map step 2: outline as an OPTIONAL column
of the structure map's ENUMERATION calls.

include_outline is accepted with start_id and REFUSED (422, naming start_id) on
a root call — a root map can emit up to STRUCTURE_ROW_CAP rows and outlines on
all of them would destroy the layering. Without the flag the row shape is
byte-identical to today (no outline keys at all). Reference rows never carry an
outline. A scoped key's enumeration stays clipped to its wall (the IDOR
INVARIANT on the module) — outlines ride the same clipped set.
"""
import hashlib
import json
import secrets

import pytest


async def _make_agent_key(
    test_db, user_id: str, project_id: str,
    label: str = "agent", scope_root: str = "",
) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"agent-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": scope_root,
        "token_hash": token_hash,
        "label": label,
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(
    client, token: str, project_id: str, title: str,
    parent_id: str | None = None, content: str = "",
) -> str:
    json = {"project_id": project_id, "title": title, "content": content}
    if parent_id:
        json["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=json, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


HEADED_CONTENT = "# Анамнез\n## Осмотр\n## Пальпация\n### Живот\n### Печень"


async def _outline_row_ids(client, tok, body: dict) -> dict:
    resp = await client.post(
        "/api/tool/get_project_structure", json=body, headers=_hdr(tok),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(out: dict, document_id: str) -> dict | None:
    return next((d for d in out["documents"] if d["document_id"] == document_id), None)


@pytest.mark.asyncio
async def test_enumeration_with_flag_carries_outlines(
    client, test_db, admin_user, project_with_doc,
):
    """start_id + include_outline: non-reference rows carry the stored outline;
    outline_hidden appears only when the degradation ladder actually dropped."""
    from doc_outline import build_outline, refresh_outline

    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    a = await _make_doc(
        client, token, pid, "A", idx_id,
        content="# Заголовок\n" + HEADED_CONTENT,
    )
    await refresh_outline(a)
    tok = await _make_agent_key(test_db, admin_uid, pid)

    out = await _outline_row_ids(client, tok, {"start_id": idx_id, "include_outline": True})
    row = _row(out, a)
    assert row is not None
    want_outline, want_hidden = build_outline("A", "# Заголовок\n" + HEADED_CONTENT)
    assert row["outline"] == want_outline
    assert row.get("outline_hidden", 0) == want_hidden


@pytest.mark.asyncio
async def test_outline_hidden_emitted_only_when_nonzero(
    client, test_db, admin_user, project_with_doc,
):
    """The counters contract: only non-zero counters are emitted — a row whose
    outline fit the budget carries no outline_hidden key."""
    from doc_outline import refresh_outline

    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    a = await _make_doc(client, token, pid, "A", idx_id, content=HEADED_CONTENT)
    await refresh_outline(a)  # short content — nothing dropped
    tok = await _make_agent_key(test_db, admin_uid, pid)

    out = await _outline_row_ids(client, tok, {"start_id": idx_id, "include_outline": True})
    row = _row(out, a)
    assert row["outline"]
    assert "outline_hidden" not in row


@pytest.mark.asyncio
async def test_without_flag_no_row_carries_outline_keys(
    client, test_db, admin_user, project_with_doc,
):
    """Without the flag the row shape is unchanged from today — no outline or
    outline_hidden key on ANY row, even rows that store an outline."""
    from doc_outline import refresh_outline

    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    a = await _make_doc(client, token, pid, "A", idx_id, content=HEADED_CONTENT)
    await refresh_outline(a)
    tok = await _make_agent_key(test_db, admin_uid, pid)

    out = await _outline_row_ids(client, tok, {"start_id": idx_id})
    assert out["documents"], "sanity: rows exist"
    for d in out["documents"]:
        assert "outline" not in d
        assert "outline_hidden" not in d


@pytest.mark.asyncio
async def test_root_call_with_flag_is_422_naming_start_id(
    client, test_db, admin_user, project_with_doc,
):
    """The failure actor: a ROOT call with include_outline is refused (422) and
    the message names the correction — pass start_id."""
    pid, _idx, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid)
    resp = await client.post(
        "/api/tool/get_project_structure", json={"include_outline": True},
        headers=_hdr(tok),
    )
    assert resp.status_code == 422, resp.text
    assert "start_id" in resp.json()["detail"]


def _mcp_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _rpc(method: str, params: dict | None = None, *, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def _result_text(resp_json: dict) -> dict:
    return json.loads(resp_json["result"]["content"][0]["text"])


def _is_error(resp_json: dict) -> bool:
    return resp_json.get("result", {}).get("isError", False)


async def _mcp_call(client, token: str, name: str, arguments: dict) -> dict:
    resp = await client.post(
        "/mcp", json=_rpc("tools/call", {"name": name, "arguments": arguments}),
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_gateway_refusal_next_action_names_start_id(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """R1 (phase-4 review): the gateway's status-keyed 422 advice is the
    full_rewrite one ("split into smaller edits") — wrong for this refusal,
    whose fix is an argument. The executor pins the directive; _err honors it."""
    pid, _idx, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_call(
        client, tok, "get_project_structure", {"include_outline": True},
    )
    assert _is_error(data)
    payload = _result_text(data)
    assert payload["status_code"] == 422
    assert payload["next_action"] == "pass a start_id"
    assert "start_id" in payload["error"]


@pytest.mark.asyncio
async def test_reference_rows_never_carry_outline(
    client, test_db, admin_user, project_with_doc,
):
    """Outlines are a non-reference column: even a reference row with a stored
    outline emits no outline keys (references are hosts' mirrors, not content)."""
    from doc_outline import refresh_outline

    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "parent_id": idx_id, "title": "R",
            "media_type": "markdown", "is_reference": True,
            "content": HEADED_CONTENT,
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    r = resp.json()["document_id"]
    await refresh_outline(r)
    tok = await _make_agent_key(test_db, admin_uid, pid)

    out = await _outline_row_ids(client, tok, {"start_id": idx_id, "include_outline": True})
    row = _row(out, r)
    assert row is not None and row["is_reference"] is True
    assert "outline" not in row
    assert "outline_hidden" not in row


@pytest.mark.asyncio
async def test_scoped_key_enumeration_stays_inside_the_wall(
    client, test_db, admin_user, project_with_doc,
):
    """The IDOR INVARIANT rides the new column: a scoped key's enumeration is
    clipped to scope_root — no row (outline-carrying or not) exists outside it,
    and the flag cannot widen anything."""
    from doc_outline import refresh_outline

    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    outside = await _make_doc(client, token, pid, "Outside", None, content=HEADED_CONTENT)
    await refresh_outline(outside)
    inside = await _make_doc(client, token, pid, "Inside", idx_id, content=HEADED_CONTENT)
    await refresh_outline(inside)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=idx_id)

    out = await _outline_row_ids(
        client, scoped, {"start_id": outside, "include_outline": True},
    )
    ids = {d["document_id"] for d in out["documents"]}
    assert outside not in ids, "the wall cannot be widened by request"
    assert ids == {idx_id, inside}
    assert _row(out, inside)["outline"]
