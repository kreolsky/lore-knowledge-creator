"""GFM serialization / parsing for the table block object, and the id-only primitive surface.

Pure-logic focus: the table model (rows = list[list[str]] cell bodies) round-trips
to a GFM table and back. Escaping is the real work and is pinned here — `|` and
intra-cell `\n` are where GFM serializers break.
"""
import json
import re

import pytest
from helpers import parse_gfm_table

from table_serialize import import_gfm_tables, serialize_table

# ── serialize_table: model → GFM ──────────────────────────────────────────────


def test_empty_model_returns_empty_string():
    assert serialize_table([]) == ""


# ── parse_gfm_table: GFM → model (import / round-trip) ─────────────────────────


def test_parse_does_not_drop_all_empty_data_row():
    # An all-empty data row (`|   |   |`) is a DATA row, not the separator — must survive
    # (the bug dropped it entirely). Assert row count + boundary rows, not exact empty-cell
    # whitespace (a separate stripping concern).
    rows = parse_gfm_table("| a | b |\n| --- | --- |\n|   |   |\n| c | d |")
    assert len(rows) == 3  # header + empty row + last row — NOT 2
    assert rows[0] == ["a", "b"]
    assert rows[2] == ["c", "d"]


# ── import_gfm_tables: the block scanner (fence skip, outer-pipe-optional
# rows, embed mask). Parametrized over ONE fixture table so the frontend twin
# test mirrors it case for case.

# The fixture table: (case, markdown in, expected rewritten text, expected
# matrices in document order). `{TABLE}` stands for the one anchor
# `![Table](table:<uuid>)` the pass mints.
# BYTE-IDENTICAL FRONTEND TWIN: frontend/src/components/editor/live-preview/
# gfm-table-import.test.ts carries a copy of this table (the backend and
# frontend containers do not mount each other's trees, so the shared fixture
# cannot be one file). A case changed HERE changes THERE in the same step —
# the two parsers are pinned to agree byte-for-byte (the docstring promise on
# table_serialize._split_row).
GFM_INGRESS_FIXTURE = [
    (
        "fenced-table-stays-text",
        "```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```",
        "```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```",
        [],
    ),
    (
        "no-outer-pipe-converts",
        "a | b\n--- | ---\n1 | 2",
        "{TABLE}",
        [[["a", "b"], ["1", "2"]]],
    ),
    (
        "setext-no-pipe-stays-heading",
        "a\n---",
        "a\n---",
        [],
    ),
    (
        "setext-with-pipe-stays-heading",
        "a | b\n---",
        "a | b\n---",
        [],
    ),
    (
        "missing-trailing-pipe-keeps-last-char",
        "| a | b\n| --- | --- |\n| 1 | 2",
        "{TABLE}",
        [[["a", "b"], ["1", "2"]]],
    ),
    (
        "image-embed-pipe-stays-one-cell",
        "| a | b |\n| --- | --- |\n| ![i|10x10](ref:x) | c |",
        "{TABLE}",
        [[["a", "b"], ["![i|10x10](ref:x)", "c"]]],
    ),
    (
        "list-nested-table-converts",
        "- item\n  | a | b |\n  | --- | --- |\n  | 1 | 2 |",
        "- item\n{TABLE}",
        [[["a", "b"], ["1", "2"]]],
    ),
]

_ANCHOR_RE = re.compile(r"!\[Table\]\(table:[0-9a-f]{32}\)")


# The scanner's boundary rules (fence skip, outer-pipe-optional rows, embed
# mask) are pure text logic where an edge silently corrupts an import; the
# fixture is the byte-identical twin of the frontend gfm-table-import.test.ts
# copy (cross-producer agreement).
# TEST-WHY: pure-logic-edge — the fixture pins the scanner's boundary rules and the cross-producer byte agreement.
@pytest.mark.parametrize(
    "case,markdown,expected_text,expected_matrices", GFM_INGRESS_FIXTURE,
)
def test_import_gfm_tables_fixture(case, markdown, expected_text, expected_matrices):
    rewritten, tables_json = import_gfm_tables(markdown)
    assert _ANCHOR_RE.sub("{TABLE}", rewritten) == expected_text
    assert [m["rows"] for m in json.loads(tables_json).values()] == expected_matrices


# The birth-width formula: (case, matrix, expected px widths).
# BYTE-IDENTICAL FRONTEND TWIN: table-widths.test.ts BIRTH_WIDTH_FIXTURE
# (the containers do not mount each other's trees). A case changed HERE changes THERE.
BIRTH_WIDTH_FIXTURE: list[tuple[str, list[list[str]], list[int]]] = [
    ("empty-2x2-equal", [["", ""], ["", ""]], [422, 422]),
    ("narrow-hash-wide-text", [["#", "Description"], ["1", "a" * 100]], [59, 785]),
    ("names", [["Name", "Role"], ["Aragorn", "Ranger"], ["Legolas", "Elf"]], [454, 390]),
    ("multiline-longest-trimmed-line", [["a\nbbbbbb  ", "cc"]], [563, 281]),
    ("ragged-row", [["x", "y", "z"], ["only"]], [338, 253, 253]),
    ("remainder-to-widest-first", [["a", "b", "c"]], [282, 281, 281]),
    ("code-points-not-bytes", [["ёжик", "ab"]], [482, 362]),
    ("too-many-columns-floor-at-min", [["x"] * 25], [40] * 25),
]


# TEST-WHY: pure-logic-edge — pins the birth-width formula and its byte agreement with the frontend twin.
@pytest.mark.parametrize("case,matrix,expected", BIRTH_WIDTH_FIXTURE)
def test_birth_column_widths_fixture(case, matrix, expected):
    from table_serialize import birth_column_widths

    assert birth_column_widths(matrix) == expected


# TEST-WHY: pure-logic-edge — the file-import birth path writes the formula, not auto columns.
def test_import_gfm_tables_born_with_birth_widths():
    _, tables_json = import_gfm_tables("| # | Description |\n| --- | --- |\n| 1 | " + "a" * 100 + " |")
    assert [m["columns"] for m in json.loads(tables_json).values()] == [[59, 785]]


# Whole-matrix projections: positional by nature (GFM rows, a birth matrix); every
# other public primitive addresses a cell by (table_id, row_id, col_id). The agent
# tool entry, the other position boundary, lives in agent.table_writes.resolve_cell.
_MATRIX_PROJECTIONS = {
    "serialize_table", "birth_column_widths", "freeze_auto_widths", "create_table_entry", "parse_gfm_table",
}


# TEST-WHY: pure-logic-edge — walks the module's real surface so a new positional primitive fails here, not in a peer's merged table.
def test_primitives_address_cells_by_id_only():
    import inspect

    import table_serialize as ts

    walked, positional, matrix = set(), [], []
    for name, fn in inspect.getmembers(ts, inspect.isfunction):
        if name.startswith("_") or fn.__module__ != ts.__name__:
            continue
        walked.add(name)
        for p in inspect.signature(fn).parameters.values():
            ann = str(p.annotation)
            if re.search(r"\bint\b", ann) and name not in _MATRIX_PROJECTIONS:
                positional.append(f"{name}({p.name}: {ann})")
            if "list[list[" in ann and name not in _MATRIX_PROJECTIONS:
                matrix.append(f"{name}({p.name}: {ann})")
    assert {"read_table_cell", "set_table_cell", "add_table_rows", "add_table_column"} <= walked
    assert positional == []
    assert matrix == []
