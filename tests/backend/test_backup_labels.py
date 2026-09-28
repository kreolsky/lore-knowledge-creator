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
async def test_auto_labels_derived_from_registry():
    """AUTO_LABELS must equal the thinnable membership derived from BACKUP_LABELS.

    Asserts over the derived source (never a literal set): a hand-edited
    AUTO_LABELS that diverges from the BackupLabel.thinnable flags fails here.
    """
    from auto_backup import AUTO_LABELS, BACKUP_LABELS

    derived = frozenset(b.value for b in BACKUP_LABELS if b.thinnable)
    assert AUTO_LABELS == derived


@pytest.mark.asyncio
async def test_agent_auto_is_registered_and_thinnable():
    """`agent-auto` must be a registered BackupLabel with thinnable=True.

    It is unbounded automatic history (one row per agent splice), exactly what
    GFS retention exists for — an unregistered agent-auto grows forever.
    """
    from auto_backup import AGENT_AUTO, AUTO_LABELS, BACKUP_LABELS

    assert AGENT_AUTO in BACKUP_LABELS
    assert AGENT_AUTO.thinnable is True
    assert AGENT_AUTO.value in AUTO_LABELS


@pytest.mark.asyncio
async def test_every_backup_label_carries_trigger():
    """Every BackupLabel must carry a non-empty trigger description.

    The module docstring's hand-written trigger registry drifted (F5); the
    description now lives on the label row itself — its single point of
    declaration.
    """
    from auto_backup import BACKUP_LABELS

    for b in BACKUP_LABELS:
        assert isinstance(b.trigger, str) and b.trigger.strip(), (
            f"BackupLabel {b.value!r} has no trigger description"
        )


@pytest.mark.asyncio
async def test_thinnable_flags_pin_each_label():
    """Each fixed label's thinnable flag must match its expected membership.

    last-session and _backup are immortal — flipping them to thinnable=True would
    silently destroy data via the GFS thinner.
    """
    from auto_backup import (
        AGENT_AUTO,
        AUTO_BACKUP,
        BEFORE_RESTORE,
        EDITOR_HANDOFF,
        LAST_SESSION,
        SAFETY_OPEN,
    )

    assert SAFETY_OPEN.thinnable is True
    assert EDITOR_HANDOFF.thinnable is True
    assert AUTO_BACKUP.thinnable is True
    assert AGENT_AUTO.thinnable is True
    assert LAST_SESSION.thinnable is False
    assert BEFORE_RESTORE.thinnable is False


@pytest.mark.asyncio
async def test_label_before_restore_constant():
    """LABEL_BEFORE_RESTORE replaces the raw '_backup' literal in checkpoints.py queries."""
    from auto_backup import LABEL_BEFORE_RESTORE

    assert LABEL_BEFORE_RESTORE == "_backup"


@pytest.mark.asyncio
async def test_back_compat_label_aliases():
    """Existing LABEL_* aliases must stay string-valued (callers write them as `.label`)."""
    from auto_backup import (
        LABEL_AGENT_AUTO,
        LABEL_AUTO_BACKUP,
        LABEL_BEFORE_RESTORE,
        LABEL_EDITOR_HANDOFF,
        LABEL_LAST_SESSION,
        LABEL_SAFETY_OPEN,
    )

    assert LABEL_SAFETY_OPEN == "safety-open"
    assert LABEL_EDITOR_HANDOFF == "editor-handoff"
    assert LABEL_AUTO_BACKUP == "auto-backup"
    assert LABEL_AGENT_AUTO == "agent-auto"
    assert LABEL_LAST_SESSION == "last-session"
    assert LABEL_BEFORE_RESTORE == "_backup"


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
