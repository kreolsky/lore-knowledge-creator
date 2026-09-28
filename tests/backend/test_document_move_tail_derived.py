"""The move's denormalisation tail is asserted from the schema, not a list.

SYSTEM: documents (move). A cross-project move (documents/move.py
_rewrite_denormalized) must carry EVERY table that denormalizes BOTH
project_id and document_id — its rows are otherwise left pointing at the
source project for a document that now lives in the target. The both-field
set is PARSED from surreal/schema.surql, so the next both-field table
someone defines fails this file before it fails a user. Tables with
project_id only (pipeline_schedules, telemetry_event) are out of the set by
construction — no allowlist to drift.
"""

import inspect
import re
from pathlib import Path

import documents.move as move_module

# In the backend/test container the schema is mounted here (conftest applies
# it from the same path).
_SCHEMA = Path("/surreal/schema.surql")
# `DEFINE FIELD [IF NOT EXISTS] <name> ON <table> TYPE …` — tolerate the
# optional IF NOT EXISTS and the schema's irregular inner spacing.
_FIELD_RE = re.compile(r"DEFINE FIELD (?:IF NOT EXISTS\s+)?(\S+)\s+ON\s+(\S+)")


def _both_field_tables() -> set[str]:
    """Tables carrying BOTH project_id and document_id fields, per the schema."""
    text = re.sub(r"--[^\n]*", "", _SCHEMA.read_text())  # drop comment lines
    fields: dict[str, set[str]] = {}
    for m in _FIELD_RE.finditer(text):
        fields.setdefault(m.group(2), set()).add(m.group(1))
    return {t for t, fs in fields.items() if {"project_id", "document_id"} <= fs}


def test_both_field_set_is_nonempty_and_is_exactly_the_five():
    """The parse cannot pass vacuously: the set must equal the five known
    tables. A schema change that adds or loses a both-field table fails HERE
    with the diff visible — carry the new table in documents/move.py's
    _rewrite_denormalized in the SAME change and update this literal."""
    assert _both_field_tables() == {
        "agent_configs", "api_keys", "chat_sessions",
        "doc_chunks", "document_shares",
    }


def test_every_both_field_table_is_named_by_the_move_tail():
    """Each both-field table appears in an UPDATE|DELETE statement of
    move.py's denormalisation gather."""
    src = inspect.getsource(move_module)
    tables = _both_field_tables()
    assert tables, "both-field parse returned nothing — the schema regex broke"
    missing = [
        t for t in sorted(tables)
        if not re.search(rf"(?:UPDATE|DELETE)\s+{t}\b", src)
    ]
    assert not missing, (
        "schema.surql tables with BOTH project_id and document_id that "
        f"documents/move.py's denormalisation tail never touches: {missing}. "
        "A cross-project move would strand their rows in the source project — "
        "add them to _rewrite_denormalized."
    )
