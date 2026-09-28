"""Project memory — Stage 1: the reserved `Memory` folder + the documents fields.

Plan `.kilo/plans/1784100000000-project-memory-mvp.md` Stage 1; design record
`1784000000000-project-memory-concept.md` §1 + P12.

The load-bearing test in this file is `test_memory_is_not_injected_into_the_prompt`:
Memory is the FOURTH skeleton folder and the only mechanism keeping it out of the
agent prompt is an ABSENCE (it is not in `_REQUIRED_ROLES`). An absence does not
survive a "unify the four folders" refactor on its own, so it is asserted over the
ASSEMBLED prompt — a loader-shape assertion would not see that regression.
"""

import pytest
from agent_config import (
    _MEMORY_ROLES,
    _REQUIRED_ROLES,
    PROTECTED_SYSTEM_ROLES,
    SYSTEM_DOC_ROLES,
    build_agent_system_prompt,
    ensure_agent_system_docs,
    load_agent_system_docs,
)

from db import create_record, get_db

# ─── The role joins the vocabulary + the protected set, never the injected set ──


def test_memory_folder_role_posture():
    """`memory_folder` is valid vocabulary and delete-protected, but NEVER injected.

    Derived over `_MEMORY_ROLES` rather than the literal so a second memory role is
    covered the day it is added (testing.md: assert over the derived source).
    """
    for role in _MEMORY_ROLES:
        assert role in SYSTEM_DOC_ROLES, f"{role} missing from the role vocabulary"
        assert role in PROTECTED_SYSTEM_ROLES, f"{role} is not delete-protected"
        assert role not in _REQUIRED_ROLES, f"{role} must never be injected"


# ─── Seeding ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_seeds_the_memory_folder(test_db, project_with_doc):
    """`Memory` exists as a child of the system root after lazy init."""
    pid, _idx, _admin = project_with_doc
    roles = await ensure_agent_system_docs(pid)
    assert roles["memory_folder"] == f"sys-memory_folder-{pid}"

    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, title, parent_id, is_system, content "
        "FROM documents WHERE project_id = $pid AND system_role = 'memory_folder' "
        "AND deleted_at IS NONE",
        {"pid": pid},
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "Memory"
    assert rows[0]["is_system"] is True
    assert rows[0]["parent_id"] == roles["system_root"]
    # Seeded EMPTY — the folder holds fact docs, not prose.
    assert not (rows[0].get("content") or "").strip()


@pytest.mark.asyncio
async def test_reinit_does_not_duplicate_the_memory_folder(test_db, project_with_doc):
    pid, _idx, _admin = project_with_doc
    first = await ensure_agent_system_docs(pid)
    second = await ensure_agent_system_docs(pid)
    assert first["memory_folder"] == second["memory_folder"]

    db = await get_db()
    rows = await db.query(
        "SELECT count() AS n FROM documents WHERE project_id = $pid "
        "AND system_role = 'memory_folder' AND deleted_at IS NONE GROUP ALL",
        {"pid": pid},
    )
    assert rows[0]["n"] == 1


@pytest.mark.asyncio
async def test_memory_folder_is_delete_guarded(
    client, test_db, admin_user, project_with_doc,
):
    """The delete-guard covers Memory via the DERIVED PROTECTED_SYSTEM_ROLES.

    Asserted through the real HTTP delete path, not against the frozenset: the
    defect class is a route that reads a different set.
    """
    pid, _idx, _admin = project_with_doc
    _, token = admin_user
    roles = await ensure_agent_system_docs(pid)
    folder_id = roles["memory_folder"]

    r = await client.delete(
        f"/api/documents/{folder_id}", cookies={"lore_session": token},
    )
    assert r.status_code == 403

    db = await get_db()
    rows = await db.query(
        "SELECT deleted_at FROM type::record('documents', $id)", {"id": folder_id},
    )
    assert rows and rows[0]["deleted_at"] is None


# ─── The carve-out: Memory is retrieved, never injected ──────────────────────


def test_renderer_never_injects_memory_even_when_handed_it():
    """THE decisive gate. `build_agent_system_prompt` must not render memory content
    even when it is handed to it pre-resolved.

    Why a PURE-function assertion and not only the end-to-end one below: the
    non-injection rests on three independent absences (see the INVARIANT on
    `_MEMORY_ROLES`) — the loader's role filter, the BFS folder list, and this
    renderer's section loop. A refactor that touches only one or two injects
    nothing, so the end-to-end test passes on a half-done "unify the folders"
    change and only fails once all three land. This assertion removes the loader
    from the equation entirely: whatever any future resolver decides to put in
    `docs_by_role`, the renderer is still the last gate and it must hold alone.

    Verified to fail: adding ("Memory", "memory_folder", "memory_children") to the
    section loop turns this red.
    """
    marker = "MEMORY-BODY-MUST-NOT-RENDER"
    prompt = build_agent_system_prompt({
        "rules_folder": {"id": "rf", "title": "Rules", "content": "a rule"},
        "rules_children": [],
        "memory_folder": {"id": "mf", "title": "Memory", "content": ""},
        "memory_children": [
            {"id": "e1", "title": "Гарет", "content": marker},
        ],
    })
    assert "a rule" in prompt, "the Rules section stopped rendering"
    assert marker not in prompt
    assert "# Memory" not in prompt


@pytest.mark.asyncio
async def test_memory_is_not_injected_into_the_prompt(test_db, project_with_doc):
    """The same rule end-to-end: fact docs really under `Memory` reach NO part of the
    assembled prompt, while a Rules child at the same depth DOES.

    This is the integration half — it proves the three seams are jointly clear on
    real data. The renderer assertion above is what catches a PARTIAL refactor;
    keep both.
    """
    pid, _idx, _admin = project_with_doc
    roles = await ensure_agent_system_docs(pid)

    memory_marker = "FACT-BODY-MUST-NOT-BE-INJECTED"
    rules_marker = "RULES-CHILD-MUST-BE-INJECTED"

    for n in range(3):
        await create_record("documents", f"mem-fact-{n}", {
            "project_id": pid, "parent_id": roles["memory_folder"],
            "title": f"Тема {n}: суть", "content": f"{memory_marker}-{n}",
            "path": f".lore/system/memory_folder/fact-{n}", "is_index": False,
            "is_memory": True, "mem_active": True,
            "mem": {"merge_count": 0, "version_history": [],
                    "provenance": {"run_id": "seed", "sources": []}},
        })
    await create_record("documents", "rules-child-visible", {
        "project_id": pid, "parent_id": roles["rules_folder"],
        "title": "A rule", "content": rules_marker,
        "path": ".lore/system/rules_folder/a-rule", "is_index": False,
    })

    docs = await load_agent_system_docs(pid)
    prompt = build_agent_system_prompt(docs)

    assert rules_marker in prompt, "the Rules subtree stopped being injected"
    assert memory_marker not in prompt, (
        "Memory content reached the system prompt — it is retrieved, never injected "
        "(design P12). Check that memory_folder stayed out of _REQUIRED_ROLES and "
        "out of build_agent_system_prompt's section loop."
    )
    # The folder itself must not appear either — a titled empty section is still a
    # foothold for a later 'and its children' refactor.
    assert "memory_folder" not in prompt
    assert "# Memory" not in prompt


# ─── The documents fields exist on the SCHEMAFULL table ──────────────────────


@pytest.mark.asyncio
async def test_memory_fields_persist_on_documents(test_db, project_with_doc):
    """The memory fields round-trip through the real SCHEMAFULL table.

    backend.md: a missing DEFINE FIELD passes every fake-DB test and 500s in prod,
    so this writes and reads back against the real schema. `mem` is FLEXIBLE (like
    `file_meta`), which is what lets the fact-metadata shape live without declaring
    every nested field. A fact's body IS the fact (`content`); `mem` carries its
    metadata (provenance, survivor fields, version history) — never claims.
    """
    pid, _idx, _admin = project_with_doc
    mem = {
        "merge_count": 0,
        "created_at": "2026-08-02T00:00:00Z",
        "last_verified_at": "2026-08-02T00:00:00Z",
        "version_history": [],
        "superseded_by": None,
        "supersede_reason": None,
        "superseded_at": None,
        "provenance": {
            "run_id": "run-1",
            "sources": [{"kind": "reference", "id": "ref-scene-7"}],
        },
    }
    await create_record("documents", "mem-fields-doc", {
        "project_id": pid, "parent_id": None, "title": "Гарет: плащ",
        "content": "Гарет носит красный плащ", "path": "mem-fields-doc",
        "is_index": False,
        "is_memory": True, "mem_active": True, "mem": mem,
    })

    db = await get_db()
    rows = await db.query(
        "SELECT is_memory, mem_active, mem "
        "FROM type::record('documents', $id)", {"id": "mem-fields-doc"},
    )
    row = rows[0]
    assert row["is_memory"] is True
    assert row["mem_active"] is True
    # The body IS the fact.
    stored_mem = row["mem"]
    assert stored_mem["provenance"]["sources"] == [{"kind": "reference", "id": "ref-scene-7"}]
    assert stored_mem["version_history"] == []
    assert "claims" not in stored_mem  # the entity level is gone


@pytest.mark.asyncio
async def test_memory_fields_absent_on_ordinary_documents(test_db, project_with_doc):
    """An ordinary document keeps the fields NONE — no non-option DEFAULT.

    Why: the sort_key silent-write trap (schema.surql) — a non-option DEFAULT
    applies only on CREATE and makes later UPDATEs silently fail. Every consumer
    must read NONE as "not a memory doc", exactly as `archived` does.
    """
    pid, _idx, _admin = project_with_doc
    await create_record("documents", "plain-doc-no-mem", {
        "project_id": pid, "parent_id": None, "title": "Plain",
        "content": "x", "path": "plain-doc-no-mem", "is_index": False,
    })
    db = await get_db()
    rows = await db.query(
        "SELECT is_memory, mem_active, mem "
        "FROM type::record('documents', $id)", {"id": "plain-doc-no-mem"},
    )
    row = rows[0]
    assert not row.get("is_memory")
    assert row.get("mem_active") is None
    assert row.get("mem") is None

