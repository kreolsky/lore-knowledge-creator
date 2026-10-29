"""Tests for the shared extractor resolver + dry-run entrypoint (plan:
mcp-run-extractor-parity).

The editor's `POST /agent-config/run` and the new MCP `run_extractor` tool MUST
resolve extraction params from the SAME helper so the two paths can never drift.
These tests pin that contract: the resolver returns exactly what the old inline
lookup produced, the dry entrypoint runs the flow WITHOUT document/event side
effects, and the no-config / not-a-reference / no-parent refusals match the editor.
"""
from unittest.mock import AsyncMock, patch

import pytest

from db import create_record, get_db

# ─── helpers ─────────────────────────────────────────────────────────────────


async def _seed_ref_under_source(pid: str, source_doc_id: str, ref_id: str) -> None:
    await create_record("documents", source_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Source",
        "content": "", "path": f"{source_doc_id}.md", "is_index": False,
    })
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": source_doc_id, "title": "Ref",
        "content": "some transcription", "path": f"_ref/{ref_id}.md",
        "is_index": False, "is_reference": True, "media_type": "markdown",
    })


async def _seed_agent_config(
    source_doc_id: str, pid: str, config_doc_id: str, target_doc_id: str,
    *, model: str | None = None, title_template: str | None = None,
) -> None:
    await create_record("documents", config_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Config",
        "content": "", "path": f"{config_doc_id}.md", "is_index": False,
    })
    await create_record("documents", target_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Target",
        "content": "", "path": f"{target_doc_id}.md", "is_index": False,
    })
    await create_record("agent_configs", f"ac-{config_doc_id}", {
        "document_id": source_doc_id,
        "config_doc_id": config_doc_id,
        "target_doc_id": target_doc_id,
        "project_id": pid,
        "trigger_event": "transcription_complete",
        "title_template": title_template,
        "model": model,
    })


# ─── resolve_extractor_params ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_reference_without_parent_is_400(admin_user, project_with_doc, test_db):
    """A reference with no parent document cannot resolve a source → 400.

    Defense-in-depth: the documents_reference_parent_check invariant now makes a no-host
    reference impossible to create, so this branch is unreachable in steady state. The
    guard stays (and stays tested) against any row that predates/bypasses the invariant;
    the orphan is seeded by temporarily suspending the event, then re-defining it.
    """
    import pathlib

    from fastapi import HTTPException
    from pipeline.extractor.params import resolve_extractor_params

    from db import split_schema_statements

    pid, _, admin_uid = project_with_doc
    db = await get_db()
    await db.query("REMOVE EVENT IF EXISTS documents_reference_parent_check ON documents")
    try:
        await create_record("documents", "orphan-ref", {
            "project_id": pid, "parent_id": None, "title": "Orphan Ref",
            "content": "", "path": "orphan.md", "is_index": False,
            "is_reference": True, "media_type": "markdown",
        })
    finally:
        evt = next(
            s for s in split_schema_statements(pathlib.Path("/surreal/schema.surql").read_text())
            if "documents_reference_parent_check" in s
        )
        await db.query(evt)

    with pytest.raises(HTTPException) as exc:
        await resolve_extractor_params("orphan-ref", {"user_id": admin_uid})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_denies_non_full_access(admin_user, project_with_doc, regular_user):
    """A user without full project access is refused — the resolver carries the
    editor's require_project_full gate, not just the read it does for config."""
    from fastapi import HTTPException
    from pipeline.extractor.params import resolve_extractor_params

    pid, _, _ = project_with_doc
    other_uid = regular_user[0]
    db = await get_db()
    # Make the regular user a READ-ONLY member of the project.
    pm_id = f"pm-{other_uid}"
    await db.query("DELETE type::record('project_members', $id)", {"id": pm_id})
    await db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = $lvl",
        {"id": pm_id, "pid": pid, "uid": other_uid, "lvl": "readonly"},
    )
    await _seed_ref_under_source(pid, "src-ro", "ref-ro")
    await _seed_agent_config("src-ro", pid, "cfg-ro", "tgt-ro")

    with pytest.raises(HTTPException) as exc:
        await resolve_extractor_params("ref-ro", {"user_id": other_uid})
    assert exc.value.status_code == 403


# ─── run_extractor_dry ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_forwards_model_into_flow(project_with_doc):
    """The params.model is forwarded into the flow's shared dict (the lever the
    benchmark/parity check depends on for override experiments)."""
    from pipeline.extractor import runner
    from pipeline.extractor.params import ExtractorParams

    captured = {}

    async def fake_run_async(shared):
        captured.update(shared)

    flow = AsyncMock()
    flow.run_async.side_effect = fake_run_async

    params = ExtractorParams(
        reference_id="r", source_doc_id="s", config_doc_id="c",
        target_doc_id="t", project_id=project_with_doc[0],
        title_template=None, model="local/experimental",
    )
    with patch("pipeline.extractor.runner.create_extractor_flow", return_value=flow):
        await runner.run_extractor_dry(params)

    assert captured["model"] == "local/experimental"
