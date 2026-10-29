"""Tests for the agent tool surface + edit resolution (incl. full-rewrite ban)
and the direct/auto apply path (de-escape + per-doc edit lock).

# SYSTEM: chat-agent-multitool-tests — unit tests for the agent primitives.

Unit-level by design (mirrors test_chat_agent_mode.py): pure helpers + monkeypatched
apply endpoints. The confirm-proposal apply path was deleted with the proposal
cluster; these cover the retained surface (tool schemas, resolve_edit_range,
the auto apply path, and the per-doc edit lock).
"""

import asyncio

import pytest

from config import AGENT_FULL_REWRITE_FRACTION

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


# ─── read_document: per-doc access gate (#3) ────────────────────────────────


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
