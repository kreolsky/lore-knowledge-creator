"""comfy_config_docs_drop: the per-project Tools/Comfy docs are tombstoned with
their role kept; other system docs and user docs are untouched; a re-run is a
no-op."""
import pytest
from agent_config import ensure_agent_system_docs

from db import create_record, get_db
from migrations.migrate_comfy_config_docs_drop import _migrate_comfy_config_docs_drop

_CHAIN = (
    ("tools_folder", "system_root", ".lore/system/tools", ""),
    ("comfy", "tools_folder", ".lore/system/tools/comfy", ""),
    ("comfy_prompt", "comfy", ".lore/system/tools/comfy/prompt", ""),
    ("comfy_workflow", "comfy", ".lore/system/tools/comfy/workflow", '{"1": {}}'),
    ("comfy_settings", "comfy", ".lore/system/tools/comfy/settings", ""),
)


async def _seed_legacy_chain(project_id: str) -> dict[str, str]:
    """Plant the chain the old seeder wrote (same deterministic ids and paths)."""
    ids = dict(await ensure_agent_system_docs(project_id))
    for role, parent_role, path, content in _CHAIN:
        doc_id = f"sys-{role}-{project_id}"
        await create_record("documents", doc_id, {
            "project_id": project_id, "parent_id": ids[parent_role],
            "title": role, "content": content, "path": path,
            "is_system": True, "system_role": role,
        })
        ids[role] = doc_id
    return ids


async def _state(db, doc_id: str) -> dict:
    rows = await db.query(
        "SELECT deleted_at, system_role, content FROM type::record('documents', $id)",
        {"id": doc_id},
    )
    assert rows, f"row {doc_id} was dropped, not tombstoned"
    return rows[0]


@pytest.mark.asyncio
async def test_tombstones_chain_keeps_role_and_leaves_others(test_db, project_with_doc):
    pid, idx, _admin = project_with_doc
    ids = await _seed_legacy_chain(pid)
    db = await get_db()

    await _migrate_comfy_config_docs_drop(db)

    for role, *_ in _CHAIN:
        row = await _state(db, ids[role])
        assert row["deleted_at"] is not None, f"{role} still live"
        assert row["system_role"] == role
    assert (await _state(db, ids["comfy_workflow"]))["content"] == '{"1": {}}'
    for role in ("system_root", "rules_folder", "memory_folder"):
        assert (await _state(db, ids[role]))["deleted_at"] is None, f"{role} touched"
    assert (await _state(db, idx))["deleted_at"] is None


@pytest.mark.asyncio
async def test_second_run_is_a_noop(test_db, project_with_doc):
    pid, _idx, _admin = project_with_doc
    ids = await _seed_legacy_chain(pid)
    db = await get_db()
    await _migrate_comfy_config_docs_drop(db)
    first = (await _state(db, ids["comfy"]))["deleted_at"]

    await _migrate_comfy_config_docs_drop(db)

    assert (await _state(db, ids["comfy"]))["deleted_at"] == first


def test_registered_in_registry():
    from migrations.runner import _MIGRATIONS

    assert "comfy_config_docs_drop" in [name for name, _ in _MIGRATIONS]
