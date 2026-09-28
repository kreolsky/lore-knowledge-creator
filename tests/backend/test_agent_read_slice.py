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


def _patch_read(monkeypatch, raw_buffer: str):
    """Patch the executor's collaborators the way test_agent_read_parity does:
    fetch/access fakes + a resolve_live_doc_state returning raw_buffer."""
    from agent import readonly_executors as agent_module

    async def fake_fetch_one(_t, _r):
        return {
            "project_id": "p-1", "title": "Doc",
            "content": "GFM-DIFFERENT-FROM-RAW",
            "is_reference": False,
        }

    async def fake_access(_d, _u):
        return "full"

    async def fake_resolve_live_doc_state(_doc_id):
        return raw_buffer, "{}"

    import collab.registry as collab_module
    monkeypatch.setattr(collab_module, "get_active_session", lambda *_a: None)
    monkeypatch.setattr(agent_module, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_module, "get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve_live_doc_state)
    return agent_module


async def _read(agent_module, **kwargs):
    return await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"}, **kwargs,
    )


# ─── Slice parity against resolve_live_doc_state ──────────────────────────────


async def test_slice_is_verbatim_substring_at_offset(monkeypatch):
    """content must be the exact code-point substring of the resolve output,
    never a reflow/normalize — including astral-plane chars (a byte-slice or
    normalization would not reproduce them)."""
    # 2-code-point emoji force code-point (not UTF-16/byte) slicing.
    raw = "αβγ" * 10 + "🐺🦊🐻" * 10 + "tail" * 10
    agent_module = _patch_read(monkeypatch, raw)

    out = await _read(agent_module, offset=37, limit=23)
    assert out["content"] == raw[37:37 + 23], (
        "slice must equal resolve_live_doc_state output [offset:offset+limit] "
        "code-point for code-point"
    )
    assert out["offset"] == 37
    assert out["total_chars"] == len(raw)
    assert out["next_offset"] == 37 + 23


async def test_whole_document_when_under_threshold(monkeypatch):
    """No offset/limit + buffer under the default slice: content is the WHOLE
    buffer and next_offset is ABSENT (absence = complete, not error)."""
    raw = "short buffer\nwith two lines\n"
    agent_module = _patch_read(monkeypatch, raw)

    out = await _read(agent_module)
    assert out["content"] == raw
    assert out["offset"] == 0
    assert out["total_chars"] == len(raw)
    assert "next_offset" not in out, (
        "next_offset must be absent when the whole document is in hand"
    )


async def test_default_slice_size_from_config(monkeypatch):
    """Default truncation fires at config.AGENT_READ_SLICE_CHARS code points."""
    import config

    raw = "z" * 120
    agent_module = _patch_read(monkeypatch, raw)
    monkeypatch.setattr(config, "AGENT_READ_SLICE_CHARS", 50)

    out = await _read(agent_module)
    assert out["content"] == raw[:50]
    assert out["offset"] == 0
    assert out["total_chars"] == 120
    assert out["next_offset"] == 50


async def test_explicit_limit_overrides_default(monkeypatch):
    import config

    raw = "z" * 120
    agent_module = _patch_read(monkeypatch, raw)
    monkeypatch.setattr(config, "AGENT_READ_SLICE_CHARS", 50)

    out = await _read(agent_module, limit=30)
    assert out["content"] == raw[:30]
    assert out["next_offset"] == 30


async def test_limit_clamped_to_hard_max(monkeypatch):
    """A runaway limit arg cannot fetch the whole buffer in one call: it is
    clamped to config.AGENT_READ_MAX_CHARS."""
    import config

    raw = "z" * 120
    agent_module = _patch_read(monkeypatch, raw)
    monkeypatch.setattr(config, "AGENT_READ_SLICE_CHARS", 50)
    monkeypatch.setattr(config, "AGENT_READ_MAX_CHARS", 60)

    out = await _read(agent_module, limit=10**6)
    assert out["content"] == raw[:60]
    assert out["next_offset"] == 60


async def test_offset_past_end_clamped_self_describing(monkeypatch):
    """offset > total: clamped to total, content empty, NO next_offset — the
    response stays self-describing (offset == total_chars), never an error."""
    raw = "z" * 100
    agent_module = _patch_read(monkeypatch, raw)

    out = await _read(agent_module, offset=10**9)
    assert out["offset"] == 100
    assert out["content"] == ""
    assert out["total_chars"] == 100
    assert "next_offset" not in out


async def test_nonpositive_window_normalized(monkeypatch):
    """offset<0 → 0; limit<=0 → the default slice size (normalize the class of
    bad windows, no per-member errors)."""
    import config

    raw = "z" * 120
    agent_module = _patch_read(monkeypatch, raw)
    monkeypatch.setattr(config, "AGENT_READ_SLICE_CHARS", 50)

    out = await _read(agent_module, offset=-5, limit=0)
    assert out["content"] == raw[:50]
    assert out["offset"] == 0
    assert out["next_offset"] == 50


async def test_slice_across_table_anchor_keeps_index_on_full_buffer(monkeypatch):
    """A slice may cut a `![label](table:id)` anchor in half — content stays an
    exact substring, AND the tables index still derives from the FULL buffer
    (a sliced anchor must not silently empty the index)."""
    raw = ("head\n\n![Tasks](table:t1)\n\ntail — " + "x" * 80 + "\n")
    agent_module = _patch_read(monkeypatch, raw)

    captured: dict = {}

    async def fake_tables_field(doc_id, content, *, table_id, index_only):
        captured["content"] = content
        return []

    monkeypatch.setattr(agent_module, "_build_tables_field", fake_tables_field)

    # Cut so the boundary lands inside the anchor text.
    anchor_at = raw.index("![Tasks]")
    out = await _read(agent_module, offset=anchor_at + 2, limit=5)
    assert out["content"] == raw[anchor_at + 2:anchor_at + 7]
    assert captured["content"] == raw, (
        "the tables index must be built from the FULL buffer, not the slice"
    )
    assert out["next_offset"] == anchor_at + 7


async def test_sidecars_present_on_every_page(monkeypatch):
    """INVARIANT: `tables` / `references` do not depend on `offset` — a later page
    carries them exactly like page 0. Why this is asserted rather than optimized
    away: a sidecar that appears only at offset 0 is indistinguishable on page 2
    from "this document has no tables", the degradation next_offset exists to
    prevent (the per-page recompute cost is the DEBT marker on that line)."""
    raw = "head\n\n![Tasks](table:t1)\n\n" + "x" * 200
    agent_module = _patch_read(monkeypatch, raw)

    async def fake_tables_field(_doc_id, _content, *, table_id, index_only):
        return [{"table_id": "t1", "label": "Tasks", "n_cols": 3}]

    async def fake_references_field(_doc_id):
        return [{"document_id": "ref-1", "title": "Shot"}]

    monkeypatch.setattr(agent_module, "_build_tables_field", fake_tables_field)
    monkeypatch.setattr(agent_module, "_build_references_field", fake_references_field)

    first = await _read(agent_module, limit=50)
    later = await _read(agent_module, offset=50, limit=50)
    assert "next_offset" in later, "guard: the later page must itself be a slice"
    assert later["tables"] == first["tables"] != []
    assert later["references"] == first["references"] != []


async def test_warns_when_sidecars_outweigh_the_slice(monkeypatch, caplog):
    """The DEBT's live tripwire: `content` is bounded but the sidecars are not, so
    a read whose tables/references weigh MORE than the slice itself defeats the
    spill bound. That case — and only that case — is logged for attention."""
    import logging

    raw = "![Grid](table:t1)\n" + "y" * 500
    agent_module = _patch_read(monkeypatch, raw)

    async def fat_tables(_doc_id, _content, *, table_id, index_only):
        return [{"table_id": "t1", "label": "Grid", "n_cols": 2,
                 "rows": [{"row": i, "cells": ["cell" * 20, "cell" * 20]} for i in range(40)]}]

    async def no_references(_doc_id):
        return []

    monkeypatch.setattr(agent_module, "_build_tables_field", fat_tables)
    monkeypatch.setattr(agent_module, "_build_references_field", no_references)

    with caplog.at_level(logging.WARNING, logger=agent_module.__name__):
        out = await _read(agent_module, limit=40, tables="inline")

    assert out["content"] == raw[:40], "the slice itself is unchanged by the warning"
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        and "sidecar" in r.getMessage().lower()
    ]
    assert warnings, "an unbounded sidecar payload must be logged for attention"
    # The measurements must be in the RENDERED message: logging_conf's formatter is
    # "%(asctime)s %(name)s %(levelname)s %(message)s", so anything passed via
    # `extra=` never reaches the log and the tripwire would carry no numbers.
    assert "content_chars=40" in warnings[0], warnings[0]
    assert "sidecar_chars=" in warnings[0]


async def test_no_warning_on_the_default_index_path(monkeypatch, caplog):
    """The cheap default (index-only tables, few references) must stay silent —
    a tripwire that fires on the common case is noise, not a signal."""
    import logging

    raw = "![Grid](table:t1)\n" + "y" * 500
    agent_module = _patch_read(monkeypatch, raw)

    async def index_tables(_doc_id, _content, *, table_id, index_only):
        return [{"table_id": "t1", "label": "Grid", "n_cols": 2}]

    async def few_references(_doc_id):
        return [{"document_id": "ref-1", "title": "Shot"}]

    monkeypatch.setattr(agent_module, "_build_tables_field", index_tables)
    monkeypatch.setattr(agent_module, "_build_references_field", few_references)

    with caplog.at_level(logging.WARNING, logger=agent_module.__name__):
        await _read(agent_module, limit=40)

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_last_slice_has_no_next_offset(monkeypatch):
    """Paging stops: the final call (offset exactly at the remaining tail)
    returns the tail and NO next_offset."""
    raw = "a" * 50 + "b" * 30
    agent_module = _patch_read(monkeypatch, raw)

    out = await _read(agent_module, offset=50)
    assert out["content"] == "b" * 30
    assert out["offset"] == 50
    assert out["total_chars"] == 80
    assert "next_offset" not in out


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


@pytest.mark.asyncio
async def test_read_document_rejects_negative_offset_422(
    client, test_db, admin_user, project_with_doc,
):
    """The HTTP model validates the window: offset<0 / limit<=0 are 422s a
    caller can self-correct from (over MCP the SDK refuses the same shapes
    against the advertised inputSchema, before dispatch)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "Sliced2", "x" * 10)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/read_document",
        json={"document_id": doc_id, "offset": -1},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 422
