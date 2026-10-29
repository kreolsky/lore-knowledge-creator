"""Phase-3 — config & knowledge as project documents (plan stage-1 §3.4).

The home agent's config is NOT files on disk — it is documents in a reserved
system subtree of the project, editable/visible as normal documents. This file
tests the backend pieces: lazy subtree init, system-prompt assembly (immutable
bootstrap tier + editable persona/rules/skills), the generalized agent_configs
coexistence (GAP-7), and the self-edit defense (system docs forced to confirm).

# ARCH (plan "agent-config-folder-subtrees"): rules / knowledge / skills are now
# three IDENTICAL folder-held subtrees. The folder carries content + children
# (plain docs, any depth, organization only); the whole live subtree is injected;
# the folder is the (append new here) target and is protected from deletion. No
# more duplicate seeded canonical children; no `# Your configuration` footer.
"""

import pytest
from agent_config import (
    DEFAULT_BOOTSTRAP_PROMPT,
    build_prompt_and_skill_docs,
    ensure_agent_system_docs,
    load_skills_subtree,
)

from db import get_db

# ─── Lazy system-subtree init ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_first_ensures_both_succeed(client, test_db, project_with_doc):
    """Two first turns of a fresh project arrive together (two quick sends):
    both lazy-inits find no skeleton and both CREATE it — the loser must take
    the winner's row, not fail the turn ("Agent setup failed")."""
    import asyncio

    pid, _idx, _admin = project_with_doc
    first, second = await asyncio.gather(
        ensure_agent_system_docs(pid), ensure_agent_system_docs(pid),
    )
    assert first == second
    db = await get_db()
    docs = await db.query(
        "SELECT count() AS n FROM documents WHERE project_id = $pid AND is_system = true GROUP ALL",
        {"pid": pid},
    )
    assert docs[0]["n"] == len(first)


# ─── Skeleton delete-guard ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_skeleton_folder_is_forbidden(
    client, test_db, admin_user, project_with_doc,
):
    """Skeleton system docs (is_system + protected role) cannot be deleted —
    deleting them left a tombstoned row holding its path, and the next agent turn
    crashed ensure on the unique index (the `Agent setup failed` root cause)."""
    pid, _idx, _admin = project_with_doc
    _, token = admin_user
    await ensure_agent_system_docs(pid)
    db = await get_db()
    rf = await db.query(
        "SELECT meta::id(id) AS id FROM documents "
        "WHERE system_role = 'rules_folder' AND project_id = $pid",
        {"pid": pid},
    )
    folder_id = rf[0]["id"]
    r = await client.delete(f"/api/documents/{folder_id}", cookies={"lore_session": token})
    assert r.status_code == 403
    # Still present + live.
    rows = await db.query(
        "SELECT deleted_at FROM type::record('documents', $id)", {"id": folder_id},
    )
    assert rows and rows[0]["deleted_at"] is None


# ─── Pi-native skills: frontmatter index instead of inlining (plan pi-native-skills) ─
#
# A skill is a document in the skills_folder subtree carrying YAML frontmatter
# (name/description/tools). Stage 1: the system prompt carries an INDEX
# (<available_skills>: name/description/location) — bodies leave the context and
# arrive on demand via the dsh `skill` loader. Absent frontmatter = not a skill.

from agent_config import build_skill_docs  # noqa: E402

# Step 3 (collapse-the-editor-harness-layer): the backend ships RAW skill
# documents; parse + validation + overlay + off-switch + served gate live in
# the plugin (harness-driver/plugin/src/skills.ts, covered by skills.test.ts).
# What stays testable HERE is the LOOKUP: the wire's three layers and their
# raw content.


async def test_build_prompt_and_skill_docs_wires_all_three_layers(
    client, test_db, project_with_doc,
):
    """The turn wire: with an EMPTY skills subtree the project layer is [] and
    the shipped layer is the repo files; the shipped packs are pinned here
    (through the test-local frontmatter reader) because the pack is the only
    route the model gets to each capability."""
    import yaml as _yaml

    def _front(content: str) -> dict:
        assert content.startswith("---\n")
        end = content.index("\n---", 4)
        return _yaml.safe_load(content[4:end])

    pid, _idx, _admin = project_with_doc
    prompt, wire = await build_prompt_and_skill_docs(pid)
    assert prompt
    assert wire["project"] == []
    shipped = {(_front(d["content"]) or {}).get("name"): d for d in wire["shipped"]}
    assert set(shipped) == {
        "memory-consolidation", "web-search", "image-generation", "sandbox",
        "deep-research", "skill-authoring", "extractor-config", "chat-recall",
        "digest-config",
    }
    meta = _front(shipped["memory-consolidation"]["content"])
    assert set(meta["tools"]) == {
        "consolidate_memory", "get_memory_facts",
        "apply_memory_verdicts", "next_reference", "get_fact_history",
        "reopen_consolidation",
    }
    assert _front(shipped["web-search"]["content"])["tools"] == [
        "web_search", "web_fetch", "sandbox_bash",
    ]
    assert _front(shipped["image-generation"]["content"])["tools"] == ["generate_image"]
    for d in shipped.values():
        assert d["location"].startswith("shipped:")
        assert d["child_docs"] == []
    # No skill documents were created in the project (the folder stays empty).
    assert build_skill_docs(await load_skills_subtree(pid)) == []


def test_bootstrap_prompt_names_only_statuses_the_tool_api_returns():
    """Every `status:"X"` the bootstrap prompt teaches must be a status some
    Tool-API surface actually returns.

    Derived on both sides — the prompt is scanned for the statuses it names and the
    Tool-API/agent modules for the statuses they emit — so a status the code stops
    returning (or starts returning) fails here instead of drifting silently into the
    model's contract. This is how `{status:"proposed"}` outlived the proposal cluster.
    """
    import re
    from pathlib import Path

    import routes
    routes_dir = Path(routes.__file__).resolve().parent
    sources = list((routes_dir / "tool_api").rglob("*.py"))
    sources += list((routes_dir / "chat" / "agent").glob("*.py"))
    emitted = {
        m.group(1)
        for src in sources
        for m in re.finditer(r'"status":\s*"([a-z_]+)"', src.read_text())
    }
    assert emitted, "no Tool-API status literals found — the scan lost its target"

    taught = set(re.findall(r'status:\s*"([a-z_]+)"', DEFAULT_BOOTSTRAP_PROMPT))
    assert taught, "the bootstrap prompt names no tool status — did the contract move?"
    assert taught <= emitted, (
        f"the bootstrap prompt teaches statuses no Tool-API path returns: "
        f"{sorted(taught - emitted)}"
    )
