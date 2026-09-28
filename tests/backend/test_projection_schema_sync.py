"""Schema-sync contract test for hand-written document projections.

Hand-written projected column lists (db.fetch_doc_meta, the batch SELECT in
routes/documents.py, references.py) are SQL over the `documents` table — a schema
rename/add has NO compile-time signal and silently omits a field an access check
depends on (runtime-only failure). This test fails the moment a projection constant
references a column that is not a declared DEFINE FIELD on `documents` in
surreal/schema.surql.

The projection constants are the single source of truth per projection (centralized
in db._DOC_PROJECTIONS); this test imports THEM rather than re-parsing SQL strings.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import db
from db import _DOC_PROJECTIONS, split_schema_statements

# DEFINE FIELD [IF NOT EXISTS | OVERWRITE] <name> ON documents ...
_DOC_FIELD_RE = re.compile(
    r"DEFINE\s+FIELD\s+(?:IF\s+NOT\s+EXISTS\s+|OVERWRITE\s+)?([A-Za-z_]\w*)\s+ON\s+documents\b",
    re.IGNORECASE,
)

# db lives in backend/db/__init__.py (a package); .parent.parent.parent reaches the
# repo root that holds surreal/schema.surql (was .parent.parent when db was a flat
# backend/db.py module).
SCHEMA_PATH = pathlib.Path(db.__file__).resolve().parent.parent.parent / "surreal" / "schema.surql"


def _document_fields() -> set[str]:
    """Return every declared DEFINE FIELD name on the documents table."""
    fields: set[str] = set()
    for stmt in split_schema_statements(SCHEMA_PATH.read_text()):
        m = _DOC_FIELD_RE.search(stmt)
        if m:
            fields.add(m.group(1))
    return fields


def _check_projection(name: str, columns: tuple[str, ...], fields: set[str]) -> None:
    """Assert every plain column in a projection is a declared documents field.

    `id` is the implicit record id (no DEFINE FIELD) — the only allowed non-schema column.
    """
    for col in columns:
        if col == "id":
            continue
        assert col in fields, (
            f"projection {name!r} reads documents column {col!r} which is NOT a declared "
            "DEFINE FIELD in surreal/schema.surql — add the field or drop the column."
        )


def test_schema_parser_extracts_known_document_fields():
    # Sanity: the parser must surface documents fields we know exist, else the subset
    # check below is vacuously green on a broken parser.
    fields = _document_fields()
    for known in (
        "project_id", "parent_id", "title", "is_reference", "media_type",
        "file_path", "file_meta", "source_url", "processing_status",
        "deleted_at", "sort_key", "is_system", "system_role",
    ):
        assert known in fields, f"schema parser missed known documents field {known!r}"


def test_document_projections_subset_of_schema_fields():
    fields = _document_fields()
    assert _DOC_PROJECTIONS, "no projections registered — registry is empty"
    for name, columns in _DOC_PROJECTIONS.items():
        _check_projection(name, columns, fields)


def test_registry_covers_every_columns_constant():
    # Exhaustiveness by construction: every db.*_COLUMNS tuple MUST be a value in
    # _DOC_PROJECTIONS, else a new projection constant used in a route SELECT would
    # silently bypass this test (reintroducing the drift it exists to catch).
    columns_constants = {
        name: getattr(db, name)
        for name in dir(db)
        if name.endswith("_COLUMNS") and isinstance(getattr(db, name), tuple)
    }
    assert columns_constants, "no *_COLUMNS constants found in db"
    registered = set(_DOC_PROJECTIONS.values())
    for name, cols in columns_constants.items():
        assert cols in registered, (
            f"db.{name} is not registered in _DOC_PROJECTIONS — the schema-sync test won't "
            "cover it; add it to the registry."
        )


def test_detector_rejects_unknown_column():
    # Negative: guard the detector — a projection naming an unknown column must fail,
    # else a future regression could slip through silently.
    fields = _document_fields()
    with pytest.raises(AssertionError):
        _check_projection("bogus", ("id", "__definitely_not_a_documents_field__"), fields)
