"""Single source of truth for backup-label metadata.

# WHY: AUTO_LABELS (the thinnable-set contract consumed by the GFS thinner) is
# DERIVED from BackupLabel.thinnable, and every fixed label — including the
# agent-written `agent-auto` — must live in BACKUP_LABELS. A label written as a
# bare literal at a call site (the original F3/F6 bug) is invisible to AUTO_LABELS:
# forget registration and you get unbounded growth or DATA LOSS (thinned an
# immortal row). These tests pin the derived set and bind the one call site that
# used to write a stray literal, so the contract cannot silently drift.
"""

import pytest


@pytest.mark.asyncio
async def test_agent_pre_edit_checkpoint_writes_registered_label(monkeypatch, emit_recorder):
    """collab_writes must write a label that is a member of BACKUP_LABELS.

    Binds the agent pre-edit checkpoint call site to the registry — this is the
    test that would have caught the original stray `"agent-auto"` literal (F6).
    """
    captured: dict = {}

    async def fake_create_checkpoint(**kwargs):
        captured.update(kwargs)
        return {"checkpoint_id": "cp-1", "label": kwargs.get("label")}

    import cp_store
    monkeypatch.setattr(cp_store, "create_checkpoint", fake_create_checkpoint)

    import deps
    monkeypatch.setattr(deps, "json_safe", lambda x: x)

    from agent.collab_writes import _create_agent_pre_edit_checkpoint

    await _create_agent_pre_edit_checkpoint(
        document_id="doc-42",
        content="Hello world",
        tables_json=None,
        original_preview="Hello",
    )

    from auto_backup import BACKUP_LABELS

    registered = {b.value for b in BACKUP_LABELS}
    assert captured["label"] in registered
