"""read_document offset/limit slicing — the read-path spill bound.

# SYSTEM: chat-agent-mode — read_document slice contract tests.

The slice MUST be a code-point substring of the SAME raw-buffer projection the
edit resolver matches against (parity-by-read, tightened): every unit test here
asserts `content` against the monkeypatched `resolve_live_doc_state` output
directly, never against a literal the executor could echo back from a reflowed
projection — a normalized slice passes a literal test and still breaks editing.

Contract under test:
- `content` is always `resolve_live_doc_state(doc_id)[0][offset:offset+limit]`
  (clamped: offset<0 → 0, offset>total → total, limit<=0 → default slice size,
  limit>hard max → hard max).
- Truncation is self-describing: `offset` + `total_chars` ALWAYS present;
  `next_offset` present ONLY while content remains past the slice (absent ⇒ the
  whole document is in hand — a model must never mistake a slice for the end).
- The `tables` index keeps deriving from the FULL buffer — a slice that cuts a
  `![label](table:id)` anchor in half must not silently empty the index.
"""
import hashlib
import secrets

import pytest

# ─── Test helpers (Tool-API HTTP surface) ─────────────────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str, label: str = "agent") -> str:
    """Insert a project-scoped agent API key and return the plaintext token."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"slice-agent-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",  # project-scoped
        "token_hash": token_hash,
        "label": label,
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(client, token: str, project_id: str, title: str, content: str = "") -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


# ─── Tool-API HTTP surface: params reach the executor through the model ───────


@pytest.mark.asyncio
async def test_read_document_slice_paging_over_tool_api(
    client, test_db, admin_user, project_with_doc,
):
    """offset/limit flow through ToolReadDocument → handler → executor: two
    paged reads concatenate to exactly the one-shot whole read."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    content = "A" * 40 + "B" * 40
    doc_id = await _make_doc(client, token, pid, "Sliced", content)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    whole = await client.post(
        "/api/tool/read_document", json={"document_id": doc_id}, headers=_hdr(agent_tok),
    )
    assert whole.status_code == 200, whole.text
    whole_data = whole.json()
    assert whole_data["total_chars"] == 80
    assert "next_offset" not in whole_data

    page1 = await client.post(
        "/api/tool/read_document",
        json={"document_id": doc_id, "limit": 50},
        headers=_hdr(agent_tok),
    )
    assert page1.status_code == 200, page1.text
    p1 = page1.json()
    assert p1["content"] == "A" * 40 + "B" * 10
    assert p1["offset"] == 0
    assert p1["total_chars"] == 80
    assert p1["next_offset"] == 50

    page2 = await client.post(
        "/api/tool/read_document",
        json={"document_id": doc_id, "offset": 50},
        headers=_hdr(agent_tok),
    )
    assert page2.status_code == 200, page2.text
    p2 = page2.json()
    assert p2["content"] == "B" * 30
    assert p2["offset"] == 50
    assert "next_offset" not in p2

    assert p1["content"] + p2["content"] == whole_data["content"], (
        "paged reads must concatenate to the whole-document read"
    )
