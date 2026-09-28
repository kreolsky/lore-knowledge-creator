"""Parity-by-read contract: `read_document` MUST serve the SAME raw-buffer
projection the edit resolver matches against.

# SYSTEM: chat-agent-mode — parity contract tests.

Plan "ethereal-munching-tome" Part A: the first-edit miss happens because the
agent copies `old_string` from a GFM read-model (the `documents.content` field
that `merge_live_content` returns when no live session exists), while the
resolver splices against the raw Y.Doc buffer. A verbatim copy of GFM is not
byte-verbatim in raw-buffer form (table anchors → flat `| … |`, list-spacing /
escaping), so the resolver 409s.

These tests bind the contract: `read_document.content` MUST equal
`resolve_live_doc_state(doc_id)[0]`, byte-for-byte, in the no-live-session
branch (the divergent one). Chat context no longer carries bodies at all
(scope pinning), so read_document is the single body path this contract guards.
"""

# ─── read_document raw-buffer parity (root-cause fix) ────────────────────────


async def test_read_document_returns_raw_buffer_when_no_live_session(monkeypatch):
    """No live session: read_document.content must equal the raw Y.Doc buffer
    (resolve_live_doc_state's first element), NOT the GFM read-model.

    This is the root-cause fix. The divergent branch was: merge_live_content
    returned `documents.content` (GFM) when no session was active, while the
    resolver loaded the Y.Doc and matched against raw bytes. A faithful
    old_string copied from GFM was not byte-verbatim in raw form → 409.
    """
    from agent import readonly_executors as agent_module

    raw_buffer = "![Tasks](table:t1)\n\nsome - raw - buffer"
    gfm_content = "| Tasks | col |\n| --- | --- |\n| a | b |\n\nsome - raw - buffer"

    async def fake_fetch_one(_t, _r):
        # documents.content carries the GFM read-model (what merge_live_content
        # returned in the bug). resolve_live_doc_state must NOT agree with it.
        return {
            "project_id": "p-1", "title": "Doc", "content": gfm_content,
            "is_reference": False,
        }

    async def fake_access(_d, _u):
        return "full"

    async def fake_resolve_live_doc_state(_doc_id):
        return raw_buffer, "{}"

    # No live session → resolve_live_doc_state is the source.
    import collab.registry as collab_module
    monkeypatch.setattr(collab_module, "get_active_session", lambda *_a: None)

    monkeypatch.setattr(agent_module, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_module, "get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve_live_doc_state)

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"},
    )
    assert out["content"] == raw_buffer, (
        "read_document.content MUST be byte-equal to resolve_live_doc_state's "
        "raw buffer — divergence is the first-miss 409 root cause"
    )
    assert out["content"] != gfm_content, (
        "GFM read-model must NOT leak through read_document in the no-session branch"
    )


async def test_read_document_content_byte_equal_to_resolver_buffer(monkeypatch):
    """Whatever the resolver sees, read_document returns verbatim — copy-paste
    from a read is now byte-verbatim by construction."""
    from agent import readonly_executors as agent_module

    # Pick a raw buffer that the GFM round-trip would mangle (table anchor +
    # raw text that list-spacing normalize would alter).
    raw_buffer = "intro\n\n![data](table:t-main)\n\ntail line one\ntail line two\n"

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

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"},
    )
    assert out["content"] == raw_buffer


async def test_read_document_preserves_title_and_metadata_under_raw_buffer(monkeypatch):
    """Switching content to the raw buffer must NOT drop title / parent_id /
    is_reference — those still come from the doc record, only content changes."""
    from agent import readonly_executors as agent_module

    from db import extract_id

    raw_buffer = "raw only"

    async def fake_fetch_one(_t, _r):
        return {
            "project_id": "p-1", "title": "T", "content": "GFM",
            "is_reference": True,
            "parent_id": "documents:parent-1",
            "is_index": True, "is_system": False,
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

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"},
    )
    assert out["content"] == raw_buffer
    assert out["title"] == "T"
    assert out["is_reference"] is True
    assert out["parent_id"] == extract_id("documents:parent-1")
    assert out["is_index"] is True

