"""Tests for the agent tool surface + edit resolution (incl. full-rewrite ban)
and the direct/auto apply path (de-escape + per-doc edit lock).

# SYSTEM: chat-agent-multitool-tests — unit tests for the agent primitives.

Unit-level by design (mirrors test_chat_agent_mode.py): pure helpers + monkeypatched
apply endpoints. The confirm-proposal apply path was deleted with the proposal
cluster; these cover the retained surface (tool schemas, resolve_edit_range,
the auto apply path, and the per-doc edit lock).
"""

import asyncio
import json

import pytest

from config import AGENT_FULL_REWRITE_FRACTION

# ─── Config constants present ───────────────────────────────────────────────
# plan: remove-ask-line-mode-axis (audit §B): test_multi_tool_system_prompt_loaded
# asserted on PROMPT_CHAT_AGENT_MULTI_TOOL_SYSTEM_PROMPT — a dead prompt that never
# reached an agent turn (the live prompt is the AGENT_BOOTSTRAP_PROMPT setting, in
# agent_config.py). Deleted with the constant (D7).


# ─── Single persona-injection contract (no double-inject) ───────────────────
# Plan "unify-agent-config": there is now ONE injection point for the persona —
# build_agent_system_prompt. The legacy context.py system-prompt injection (which
# concatenated a SECOND persona doc into the Pi path) is deleted. These two
# tests pin the contract end-to-end.


@pytest.mark.asyncio
async def test_selected_persona_injected_exactly_once(client, test_db, project_with_doc):
    """The selected persona text appears EXACTLY once in the assembled Pi system
    prompt — never twice (the double-inject bug). build_agent_system_prompt is the
    sole injection point."""
    from agent_config import DEFAULT_BOOTSTRAP_PROMPT, build_agent_system_prompt

    persona_text = "UNIQUE-PERSONA-MARKER-7c3a"
    prompt = build_agent_system_prompt(DEFAULT_BOOTSTRAP_PROMPT,
        {
            "personas": [
                {"id": "p1", "title": "Editor", "content": persona_text},
            ],
            "rules_folder": {"id": "rf", "title": "Rules", "content": "Be brief."},
            "rules_children": [{"id": "r1", "title": "Extra", "content": "And careful."}],
        },
        selected_persona_id="p1",
    )
    assert prompt.count(persona_text) == 1


@pytest.mark.asyncio
async def test_context_no_longer_injects_system_prompt_id(client, test_db, project_with_doc):
    """The double-inject fix: build_context must NOT add a system-prefix entry for
    body.system_prompt_id anymore. The persona enters the prompt only via
    build_agent_system_prompt (selected_persona_id). Confirming system_prefix is free
    of the persona doc content closes the regression window."""
    from routes.chat.context import build_context

    from models import CompletionRequest

    pid, idx_id, _admin = project_with_doc
    # Create a persona doc under the reserved system subtree.
    persona_id = "sys-persona-dbl"
    await test_db.query(
        "CREATE type::record('documents', $id) SET project_id = $pid, "
        "parent_id = NONE, title = 'Editor persona', content = $c, "
        "path = '.lore/system/system_prompt/dbl', "
        "is_system = true, system_role = 'persona', is_reference = false",
        {"id": persona_id, "pid": pid, "c": "DBL-INJECT-MARKER-f0a1"},
    )
    body = CompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        system_prompt_id=persona_id,
    )
    session = {"document_id": idx_id, "project_id": pid}
    ctx = await build_context(body, pid, session)
    # No system-prefix message carries the persona content.
    for msg in ctx.system_prefix:
        text = msg.get("content") if isinstance(msg.get("content"), str) else ""
        assert "DBL-INJECT-MARKER-f0a1" not in text
    # And no warning was emitted for the system_prompt_id (it is simply no longer
    # a context concern — the resolver path for it is gone).
    assert not any(w.get("id") == persona_id for w in ctx.warnings)


# ─── Tool schemas ────────────────────────────────────────────────────────────


def test_agent_tool_set_capability_surface():
    # ARCH (minimal-tool-set plan + agent-table-read-and-cell-edit plan +
    # mcp-editor-tools-part1 + mcp-editor-tools-upload-document): the
    # minimal-necessary capability surface. Each is a capability, never a param alias.
    from agent.tools import AGENT_TOOLS
    names = {t["function"]["name"] for t in AGENT_TOOLS}
    # D14: import_file moved off AGENT_TOOLS (Pi-only now); MCP serves attach_file.
    assert names == {
        "search_materials", "read_document", "get_project_structure",
        "edit_document", "create_document", "edit_table_cell",
        "create_table", "add_table_rows", "add_table_column",
        "append_to_document", "move_document", "rename_document",
    }
    for t in AGENT_TOOLS:
        assert t["type"] == "function"
        json.dumps(t)  # OpenAI wire-serializable


def test_agent_tool_kinds_classified():
    # sandbox_bash (SYSTEM: agent-sandbox) is mutating for sequential execution only —
    # served via agent_toolset, never from AGENT_TOOLS. See test_agent_tool_surface.
    # sandbox_fetch_reference (sandbox-file-bridge plan) rides the same property for
    # sequential workspace writes.
    from agent.tools import MUTATING_TOOLS
    assert MUTATING_TOOLS == {
        "create_document", "edit_document", "edit_table_cell",
        "create_table", "add_table_rows", "add_table_column",
        "append_to_document", "move_document", "rename_document",
        "import_file",
        "sandbox_bash", "sandbox_fetch_reference",
        # generate_image (comfyui-agent-image-gen): mutating for sequential ComfyUI
        # execution; Pi-only (served via agent_toolset, never from AGENT_TOOLS).
        "generate_image",
        # reprocess_reference (agent-reference-text-is-canon): mutating for
        # sequential execution (one content-wipe+queue at a time); Pi-only.
        "reprocess_reference",
        # apply_memory_verdicts (project-memory-mvp): mutating — it writes claims and
        # re-renders entity bodies. Membership buys sequential execution in the Pi
        # driver, which matters here: two concurrent applies over one entity are a
        # read-compute-write race that the in-process lock only covers per replica.
        "apply_memory_verdicts",
        # reopen_consolidation (reopen-consolidation-tool): mutating — it moves the run
        # cursor (clears the consumed-stamps); Pi-only.
        "reopen_consolidation",
        # save_skill (skill-authoring-skill): mutating — it writes the Skills
        # folder under the apply gate; Pi-only.
        "save_skill",
    }


def test_holdable_tools_are_exactly_the_apply_bearing_mutating_set():
    # The mid-turn gate can HOLD only a mutating tool whose request model carries
    # `apply` (the driver announces awaiting_verdict for these and only these — a
    # card for sandbox_bash / import_file / generate_image would claim a call is
    # held that already ran). Derived from the registry, never hand-listed; this
    # pins the exact set so a tool silently gaining or losing `apply` fails loud.
    from agent.tools import HOLDABLE_TOOLS, MUTATING_TOOLS
    assert HOLDABLE_TOOLS <= MUTATING_TOOLS
    assert HOLDABLE_TOOLS == {
        "create_document", "edit_document", "edit_table_cell",
        "create_table", "add_table_rows", "add_table_column",
        "append_to_document", "move_document", "rename_document",
        # reprocess_reference (agent-reference-text-is-canon) + reopen_consolidation
        # (reopen-consolidation-tool): both mutating AND apply-bearing — the gate
        # really holds them when the driver asks for confirmation.
        "reprocess_reference", "reopen_consolidation",
        # save_skill (skill-authoring-skill): mutating + apply-bearing — one
        # consent covers the head and the Spec child together.
        "save_skill",
    }


def test_full_rewrite_fraction_configured():
    assert 0.0 < AGENT_FULL_REWRITE_FRACTION <= 1.0


# ─── resolve_edit_range — str_replace resolution + full-rewrite ban ────────


def test_resolve_edit_range_unique_match():
    from agent.edit_primitives import resolve_edit_range
    content = "Hello cruel world"
    out = resolve_edit_range(content, "cruel", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert out == (6, 11, False)


def test_resolve_edit_range_zero_matches():
    from agent.edit_primitives import resolve_edit_range
    assert resolve_edit_range("Hello world", "missing", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == "not_found"


def test_resolve_edit_range_ambiguous():
    from agent.edit_primitives import resolve_edit_range
    content = "the cat and the dog"
    assert resolve_edit_range(content, "the", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == "ambiguous"


def test_resolve_edit_range_rejects_full_rewrite():
    # INVARIANT: full-rewrite ban (plan §3). old_string covering >= fraction of doc.
    from agent.edit_primitives import resolve_edit_range
    content = "abcdefghij" * 10  # 100 chars
    old = content[: int(len(content) * AGENT_FULL_REWRITE_FRACTION) + 5]
    assert resolve_edit_range(content, old, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == "full_rewrite"


def test_edit_miss_detail_appends_nearest_snippet():
    # 2A: a not_found miss carries a nearest-match snippet so a weak model can
    # self-correct. The message is a SUPERSET of edit_range_error_detail.
    from agent.edit_primitives import (
        edit_miss_detail,
        edit_range_error_detail,
    )
    content = "# Bestiary\nGoblins are weak.\nOrcs are strong.\n"
    detail = edit_miss_detail(content, "Goblins are feeble", "not_found")
    assert detail.startswith(edit_range_error_detail("not_found"))
    assert "Goblins are weak." in detail, "nearest-match line must be surfaced"


def test_edit_miss_detail_ambiguous_hint():
    from agent.edit_primitives import edit_miss_detail
    detail = edit_miss_detail("the cat and the dog", "the", "ambiguous")
    assert "add surrounding context" in detail


def test_resolve_edit_range_allows_large_but_under_fraction():
    from agent.edit_primitives import resolve_edit_range
    # Each line unique so a 50-char prefix cannot recur.
    content = "\n".join(f"line {i:04d} unique marker" for i in range(200))
    old = content[:50]
    assert resolve_edit_range(content, old, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == (0, 50, False)


# ─── search_materials: per-doc access gate (#1 access leak) ─────────────────


def _mk_search_mocks(monkeypatch, *, direct_rows, access_map, semantic_hits=None):
    """Wire _search_materials_exec: direct DB layer + semantic layer + per-doc access."""
    import types as _types

    from agent import search_exec as search_module

    class _FakeDB:
        async def query(self, _q, _params):
            return list(direct_rows)

    async def fake_get_db():
        return _FakeDB()

    async def fake_document_access(doc_id, _user):
        return access_map.get(doc_id)

    async def fake_retrieve(**_kw):
        return _types.SimpleNamespace(error=None, hits=list(semantic_hits or []))

    # _search_materials_exec moved to search_exec (plan: chat-large-file-decomposition);
    # its call-time deps (get_db / get_document_access) resolve off search_exec's
    # namespace, so patch them there.
    monkeypatch.setattr(search_module, "get_db", fake_get_db)
    monkeypatch.setattr(search_module, "get_document_access", fake_document_access)
    import retrieval
    monkeypatch.setattr(retrieval, "retrieve_context", fake_retrieve)


async def test_search_excludes_hits_without_per_doc_access(monkeypatch):
    """#1: a direct-match hit the user lacks per-doc access to must NOT surface,
    even though it is in the session project."""
    from agent import readonly_executors as agent_module

    direct_rows = [
        {"id": "doc-ok", "title": "Public lore", "content": "dragons here",
         "is_reference": False, "parent_id": "doc-ok"},
        {"id": "doc-secret", "title": "Secret lore", "content": "dragons hidden",
         "is_reference": False, "parent_id": "doc-secret"},
    ]
    _mk_search_mocks(
        monkeypatch, direct_rows=direct_rows,
        access_map={"doc-ok": "full", "doc-secret": None},  # no access to secret
    )

    out = await agent_module._search_materials_exec(
        project_id="p-1", user={"user_id": "u-1"}, query="dragons",
    )
    ids = {h["doc_id"] for h in out["hits"]}
    assert "doc-ok" in ids
    assert "doc-secret" not in ids


async def test_search_direct_layer_surfaces_freshly_created_doc(monkeypatch):
    """Embedding-lag: a just-created doc has a direct title/body match but no
    semantic hit yet — it must still be returned by the direct layer."""
    from agent import readonly_executors as agent_module

    direct_rows = [
        {"id": "doc-new", "title": "Brand New", "content": "freshly written griffin",
         "is_reference": False, "parent_id": "doc-new"},
    ]
    _mk_search_mocks(
        monkeypatch, direct_rows=direct_rows,
        access_map={"doc-new": "full"}, semantic_hits=[],  # embeddings lag → empty
    )

    out = await agent_module._search_materials_exec(
        project_id="p-1", user={"user_id": "u-1"}, query="griffin",
    )
    assert {h["doc_id"] for h in out["hits"]} == {"doc-new"}
    assert out["hits"][0]["source"] == "direct"


# ─── read_document: per-doc access gate (#3) ────────────────────────────────


async def test_read_document_rejects_no_access(monkeypatch):
    from agent import readonly_executors as agent_module

    async def fake_fetch_one(_t, _r):
        return {"project_id": "p-1", "title": "Secret", "content": "x"}

    async def fake_access(_d, _u):
        return None

    monkeypatch.setattr(agent_module, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_module, "get_document_access", fake_access)

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"},
    )
    assert "error" in out


async def test_read_document_rejects_cross_project(monkeypatch):
    from agent import readonly_executors as agent_module

    async def fake_fetch_one(_t, _r):
        return {"project_id": "p-OTHER", "title": "Elsewhere", "content": "x"}

    async def fake_access(_d, _u):
        return "full"

    monkeypatch.setattr(agent_module, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_module, "get_document_access", fake_access)

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"},
    )
    assert "error" in out

async def test_auto_apply_edit_escaped_new_string_deescaped(monkeypatch):
    """Same symmetric de-escape on the AUTO path (apply_edit_to_document)."""
    from agent import collab_writes as cw
    from agent import tool_api_surface as tus_module

    captured = {}

    async def fake_fetch_one(_tbl, _id):
        return {"project_id": "p-1", "content": ""}

    async def fake_access(_doc_id, _user):
        return "full"

    async def fake_resolve_live_doc_state(_doc_id):
        captured["live_content"] = "title\n---\n\nbody trailing"
        return captured["live_content"], "{}"

    async def fake_checkpoint(**kwargs):
        return {"checkpoint_id": "cp-1"}

    async def fake_route_edits(*, doc_id, edits, project_id):
        # B1: surgical path passes the fragment + range; reconstruct the full doc.
        # apply_edit_to_document coalesces to a one-element batch.
        c = captured["live_content"]
        e = edits[0]
        captured["applied_content"] = c[:e["from_cp"]] + e["new_text"] + c[e["to_cp"]:]
        return True

    async def fake_presence(_doc_id, _uid):
        return None

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve_live_doc_state)
    monkeypatch.setattr(cw, "_create_agent_pre_edit_checkpoint", fake_checkpoint)
    monkeypatch.setattr("agent.doc_state.route_document_edits", fake_route_edits)
    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)

    result = await tus_module.apply_edit_to_document(
        doc_id="doc-A",
        old_string="title\\n---\\n\\nbody",
        new_text="title\\n\\nbody",
        project_id="p-1",
        user={"user_id": "u-1"},
    )
    assert result["status"] == "applied", result
    assert captured["applied_content"] == "title\n\nbody trailing"
    assert "\\n" not in captured["applied_content"]


async def test_auto_apply_edit_nbsp_content_vs_plain_space_folds(monkeypatch):
    """AUTO path: the live doc holds a NBSP (Russian typography) where the model
    sends a plain space. The fold matches and the edit applies."""
    from agent import collab_writes as cw
    from agent import tool_api_surface as tus_module

    captured = {}

    async def fake_fetch_one(_tbl, _id):
        return {"project_id": "p-1", "content": ""}

    async def fake_access(_doc_id, _user):
        return "full"

    async def fake_resolve_live_doc_state(_doc_id):
        captured["live_content"] = "цена 100 рублей за штуку падинг"
        return captured["live_content"], "{}"

    async def fake_checkpoint(**kwargs):
        return {"checkpoint_id": "cp-1"}

    async def fake_route_edits(*, doc_id, edits, project_id):
        c = captured["live_content"]
        e = edits[0]
        captured["applied_content"] = c[:e["from_cp"]] + e["new_text"] + c[e["to_cp"]:]
        return True

    async def fake_presence(_doc_id, _uid):
        return None

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve_live_doc_state)
    monkeypatch.setattr(cw, "_create_agent_pre_edit_checkpoint", fake_checkpoint)
    monkeypatch.setattr("agent.doc_state.route_document_edits", fake_route_edits)
    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)

    result = await tus_module.apply_edit_to_document(
        doc_id="doc-A",
        old_string="цена 100 рублей",   # plain space, model cannot see the NBSP
        new_text="цена 200 рублей",
        project_id="p-1",
        user={"user_id": "u-1"},
    )
    assert result["status"] == "applied", result
    assert captured["applied_content"] == "цена 200 рублей за штуку падинг"


# ─── doc-edit-lock: parallel edits to one doc must not lose updates (Fix A) ────


def _mk_doc_edit_mocks(monkeypatch, *, live_states, access="full"):
    """Wire `apply_edit_to_document` (tool_api_surface) against a shared mutable
    `live_states` dict keyed by doc_id. The fakes SUSPEND (asyncio.sleep(0)) so
    concurrent gather calls actually contend on the read-compute-write window —
    without those yield points the async fakes run synchronously and a lost-update
    would NOT reproduce (the test would be vacuous)."""
    from agent import collab_writes as cw

    async def fake_fetch_one(table, rid):
        if table == "documents":
            return {"project_id": "p-1", "content": live_states[rid]}
        return None

    async def fake_access(_did, _user):
        return access

    async def fake_resolve_live_doc_state(doc_id):
        # SUSPEND so concurrent readers all observe C0 before any writer commits.
        await asyncio.sleep(0)
        return live_states[doc_id], "{}"

    async def fake_checkpoint(**kw):
        return {"checkpoint_id": "cp"}

    async def fake_route_edits(*, doc_id, edits, project_id):
        await asyncio.sleep(0)  # SUSPEND so writers interleave (last-writer-wins)
        cur = live_states[doc_id]
        # Descending-offset apply (mirrors route_document_edits' real contract).
        for e in sorted(edits, key=lambda x: x["from_cp"], reverse=True):
            cur = cur[:e["from_cp"]] + (e["new_text"] or "") + cur[e["to_cp"]:]
        live_states[doc_id] = cur
        return True

    async def fake_presence(*_a, **_kw):
        return None

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve_live_doc_state)
    monkeypatch.setattr(cw, "_create_agent_pre_edit_checkpoint", fake_checkpoint)
    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)
    # apply_edit_to_document → apply_edits_to_document → route_document_edits (the
    # batch convergence helper). Mock it directly (it no longer goes through
    # apply_external_content_change — the batch path splices + publishes in-house).
    monkeypatch.setattr("agent.doc_state.route_document_edits", fake_route_edits)


async def test_parallel_edits_to_same_doc_all_apply(monkeypatch):
    """Regression core: 3 disjoint old_string edits to ONE doc issued via
    asyncio.gather (pi-agent-core runs tool calls in parallel by default). Before
    the per-doc asyncio.Lock all three read the same C0 and the last writer wins —
    only one edit survives (lost-update). After Fix A.1 the lock serializes the
    read-compute-write so each edit re-resolves against the prior mutation."""
    from agent.tool_api_surface import apply_edit_to_document

    live_states = {"d1": "alpha X gamma Y beta"}
    _mk_doc_edit_mocks(monkeypatch, live_states=live_states)

    edits = [("alpha", "ALPHA"), ("gamma", "GAMMA"), ("beta", "BETA")]
    results = await asyncio.gather(*[
        apply_edit_to_document(
            doc_id="d1", old_string=o, new_text=n,
            project_id="p-1", user={"user_id": "u-1"},
        )
        for (o, n) in edits
    ])

    assert all(r["status"] == "applied" for r in results)
    final = live_states["d1"]
    assert "ALPHA" in final and "GAMMA" in final and "BETA" in final, (
        f"lost-update: expected all 3 edits present, got {final!r}"
    )


async def test_doc_edit_lock_does_not_block_different_docs(monkeypatch):
    """2 edits to 2 DIFFERENT docs via gather — both must apply. Guards against an
    accidental global lock: the registry keys by doc_id, so edits to distinct docs
    acquire distinct locks and never wait on each other."""
    from agent.tool_api_surface import apply_edit_to_document

    live_states = {"d1": "alpha tail", "d2": "beta tail"}
    _mk_doc_edit_mocks(monkeypatch, live_states=live_states)

    results = await asyncio.gather(
        apply_edit_to_document(
            doc_id="d1", old_string="alpha", new_text="ALPHA",
            project_id="p-1", user={"user_id": "u-1"},
        ),
        apply_edit_to_document(
            doc_id="d2", old_string="beta", new_text="BETA",
            project_id="p-1", user={"user_id": "u-1"},
        ),
    )

    assert all(r["status"] == "applied" for r in results)
    assert live_states["d1"] == "ALPHA tail"
    assert live_states["d2"] == "BETA tail"
